"""
The NetSuite write: approved quantities, one batch PATCH per PO (Phase 4 decision 2).

`write_po_batch` is the ONE function the review app may call from the write
side. `test_review_app_never_imports_netsuite_client` pins its name and
signature and fails if the app imports anything else from here, from
`netsuite_client`, or from `matcher`/`ingest`. Wired to the review app's Approve
on 2026-11-02.
`write_approved_quantity` -- one line, one request -- is the internal helper
for the batch's per-line fallback.

## The batch, per PO (RUNBOOK section 11 decision 2, measured 2026-11-02)

1. **Re-reads the PO once.**
2. **Refuses** each line the per-line rules below refuse -- closed, no longer
   open, gone, edited since the proposal -- recording it, with the same words.
3. **Sends ONE PATCH** with every remaining line's quantity and nothing else.
   About 7 s whatever the line count, against about 9 s a line one by one.
4. **Re-reads the PO again and records what actually landed**, line by line.
   A 204 is never taken as proof; a timed-out request is not taken as failure.
   The line holding the value just sent is the test either way.

**A 400 rejects the whole batch and applies nothing.** If its `o:errorPath`
names a line, that line is recorded REFUSED with NetSuite's message and the
rest is resent -- at most `MAX_BATCH_REQUESTS` requests in all, then stop and
report. If it names no line, every remaining line falls back to
`write_approved_quantity`, one request each.

`write_attempts` gets one row PER LINE, never per request, all rows of one
request sharing its idempotency key -- so a batch stays auditable line by line.

## What the per-line helper does

1. **Re-reads the line from NetSuite**, never trusting stored values. The review
   screen shows state as of processing, and nothing refreshes it.
2. **Refuses** when the line is closed, not open, or gone, or when its quantity
   no longer equals the `current_quantity` the proposal was worked out from.
   That means someone else edited the line, and their edit is not ours to
   overwrite. A refusal sends nothing.
3. **Sends exactly one field: `quantity`.** No date field, `expectedReceiptDate`
   included (Kiko, 2026-11-02). `_assert_quantity_only` raises before anything
   is sent if the payload ever holds another key. `custcol_sd_fg_excluderepspark`
   is never sent; `normalize_line_fields` would reject it anyway.
4. **Records** a `write_attempts` row for every outcome that got as far as
   NetSuite: SUCCESS, ALREADY_PRESENT, CONFIRMED (the line already held the
   approved figure, so nothing was sent), REFUSED (with the reason) or FAILED
   (with the error; `{}` as the payload when the client proved nothing left).
   `previous_quantity` is the value read in step 1, which makes it the
   "previous" in the result page's "previous -> new".

An expected refusal or a NetSuite failure is RETURNED, never raised, so the
caller can show it per line and carry on with the rest of the PO. Raising is
reserved for programming errors: an unknown change id, or a payload that
breaks the quantity-only rule.

## What "previous" means and does not mean

It is the value NetSuite held a moment before the PATCH, not the proposal-time
`current_quantity`. The two are equal on a write (step 2 guarantees it), but a
refusal records what the line ACTUALLY held, which is the number the reviewer
needs to see.

## Retrying after a failure that may have landed

A FAILED write can still have reached NetSuite: a timeout or a 5xx after the
PATCH left, or a 204 the closing re-read disagreed with. A retry is safe anyway.
The re-read sees the line already at the new value, which no longer equals the
proposal's base, so nothing is written twice. If an earlier attempt of the same
proposal **failed after the request was sent** with exactly that value, the
line is settled as ALREADY_PRESENT (`_proven_earlier_attempt`, migration 0015);
otherwise it is refused as "already shows". A request the client proved never
left (`nc.request_was_sent(exc) is False`) is recorded with `{}` as its payload,
so it never qualifies, and the batch does not re-read after it: a line holding
the value then was put there by someone else, not by this tool.

## Two calls on one change (two tabs, a double click)

The PATCH is an absolute value, so NetSuite ends up right either way, and the
later re-read is refused as a conflict. The RECORD is protected too: `_insert`
decides the state from the row as it is at record time, a SUCCESS always lands,
and a refusal never overwrites a WRITTEN status, so neither order of recording
can drop a real write from the accumulation base
(`test_two_calls_on_one_line_never_lose_the_write`).

## Never raises for a NetSuite or database problem

Any exception from the client is recorded as FAILED. If recording itself fails
after NetSuite accepted the write, the result is `WRITTEN_NOT_RECORDED`, saying
the line changed and not to retry. A recording failure is never ONLY returned:
`_surface_unrecorded` logs it (CRITICAL if the write reached or may have reached
NetSuite), appends it to the unrecorded-writes file the review app shows on every
page, and keeps it in `unrecorded_failures` for tests.

## Production

Refused unless `PO_AGENT_ALLOW_PRODUCTION_WRITES` names the production account
(`_account_refusal`). Sandbox first, always.

## Not handled

- The per-line helper does no read-back after its PATCH (the batch does). A
  discard there would surface as `DISPUTED` on the next slip.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import netsuite_client as nc
import schema as sc
from schema import audit_log, proposed_changes, shipment_pos, shipments, write_attempts

logger = logging.getLogger(__name__)

#: `LineWriteResult.status` values.
WRITTEN = "WRITTEN"
REFUSED = "REFUSED"
FAILED = "FAILED"
#: NetSuite accepted the write and the database could not record it. The line
#: IS changed; the accumulation base does not know. Never retried by the tool,
#: and a retry cannot settle it as ALREADY_PRESENT unless an EARLIER attempt of
#: the same proposal is on record -- this one, by definition, is not.
WRITTEN_NOT_RECORDED = "WRITTEN_NOT_RECORDED"
#: A retry found the line already holding exactly what an earlier, unconfirmed
#: attempt of the SAME proposal sent (Kiko, 2026-11-02). "The line holds the
#: intended value; the tool cannot prove it made the change." Settled -- WRITTEN,
#: counted by the accumulation base -- but never reported or recorded as SUCCESS.
#: Status and `write_attempts.outcome` share the name.
ALREADY_PRESENT = "ALREADY_PRESENT"
#: The re-read found the line already holding the approved figure, unchanged
#: since the proposal (Kiko, 2026-11-04). Nothing is sent: a same-value PATCH
#: changes nothing, and recording it as a write would claim one. Settled like
#: ALREADY_PRESENT -- WRITTEN, counted by the accumulation base -- and never
#: recorded or shown as a write. Status and `write_attempts.outcome` share the
#: name (migration 0016).
CONFIRMED = "CONFIRMED"

#: The audit event for a line refused BEFORE NetSuite was contacted (no live
#: connection, production writes off, a database precondition). There is no
#: `write_attempts` row for these -- that table holds attempts that reached
#: NetSuite -- so this event is where the reason survives a restart, and where the
#: review app's result page reads it (Kiko, 2026-11-04).
NOT_SENT_EVENT = "QUANTITY_WRITE_NOT_SENT"

#: Production writes are refused unless this names the production account id.
PRODUCTION_WRITES_ENV = "PO_AGENT_ALLOW_PRODUCTION_WRITES"

#: The only field this path sends.
SENT_FIELDS = frozenset({nc.NS_QUANTITY})

#: Fields this path must never send. The three date fields: no dates from this
#: path (2026-11-02), and production PO 1624's lines 29-44 carry human-entered
#: overrides in the custom pair. `custcol_sd_fg_excluderepspark`: Paula manages
#: it by hand; it stays out of scope entirely (RUNBOOK section 12 item 2).
NEVER_SENT = frozenset({
    nc.NS_EXPECTED_RECEIPT_DATE,
    nc.NS_OVERRIDE_EXPECTED_RECEIPT,
    nc.NS_UPDATED_RECEIPT_DATE,
    "custcol_sd_fg_excluderepspark",
})

#: The actor on audit rows. A write is a SYSTEM step: the human act was the
#: approval, already recorded with its reviewer.
ACTOR = "write_path"

#: PATCH requests one batch may send, resends after a 400 included. The cap is
#: what stops a PO whose lines keep failing one by one from looping.
MAX_BATCH_REQUESTS = 3

#: Full closing reads tried before giving up (each already retried by the client
#: on a timeout). A closing read that never answers leaves lines that probably
#: landed recorded FAILED until a retry settles them as ALREADY_PRESENT -- so the
#: read is worth a second go first. Measured: 60 s timeouts about 1 in 15.
CLOSING_READ_ATTEMPTS = 2

_ATTEMPT_NO_RETRIES = 3
_WORKFLOW_BY_ORIGIN = {"VENDOR_EMAIL": "PACKING_SLIP", "PAULA_DIRECTED": "PAULA_DIRECTED"}

_live: Optional[nc.NetSuiteClient] = None


class WritePathViolation(Exception):
    """A payload broke the quantity-only rule. A programming error, never a refusal."""


@dataclass(frozen=True)
class LineWriteResult:
    """One line's outcome, ready to display. `message` is plain English."""

    change_id: str
    po: str  # "PO 1662"
    line: Optional[str]  # NetSuite line number, as a string
    status: str  # WRITTEN / ALREADY_PRESENT / REFUSED / FAILED / WRITTEN_NOT_RECORDED
    message: str
    previous_quantity: Optional[Decimal] = None
    new_quantity: Optional[Decimal] = None
    error_kind: Optional[str] = None
    #: None when nothing was recorded: a refusal decided from the database alone,
    #: before NetSuite was contacted.
    attempt_no: Optional[int] = None


@dataclass(frozen=True)
class PoWriteResult:
    """One PO's outcome: a result per approved line, in line order."""

    po: str  # "PO 1662"
    lines: tuple  # LineWriteResult, one per line approved for writing
    #: Batch PATCH requests sent, resends included. The per-line fallback's own
    #: requests are not counted here; each of its lines carries its own result.
    batch_requests: int = 0
    fell_back_to_per_line: bool = False

    def with_status(self, status: str) -> list:
        return [r for r in self.lines if r.status == status]


def write_po_batch(
    engine, shipment_po_id: str, *, client=None, now: Optional[dt.datetime] = None
) -> PoWriteResult:
    """
    Write every approved line of one PO in one PATCH. See the module docstring.

    The review app's ONE entry point to the write side. `client` exists for
    tests; the app must not pass it (the guard test checks).
    """
    now = now or _utcnow()
    head, rows = _load_po(engine, shipment_po_id)
    po = f"PO {head.po_number_key}"
    results: dict = {}

    def where(row):
        return f"{po} line {row.ns_line_id}"

    def new_of(row):
        return Decimal(row.approved_quantity) if row.approved_quantity is not None else None

    def done(requests=0, fell_back=False) -> PoWriteResult:
        return PoWriteResult(po, tuple(results[r.id] for r in rows if r.id in results),
                             requests, fell_back)

    candidates = []
    for row in rows:
        refusal = _precondition_refusal(row, where(row))
        if refusal:
            results[row.id] = _refused_before_contact(engine, row, now, po, new_of(row),
                                                      refusal)
        else:
            candidates.append(row)
    if not candidates:
        return done()

    client = client or _live_client()
    refusal = _account_refusal(client, po)
    if refusal:
        for row in candidates:
            results[row.id] = _refused_before_contact(engine, row, now, po, new_of(row),
                                                      refusal)
        return done()

    # 1. Re-read the PO once.
    internal_id = head.ns_internal_id
    try:
        before = {ln.line_id: ln for ln in
                  client.get_purchase_order_lines_by_internal_id(internal_id)}
    except Exception as exc:  # noqa: BLE001 -- a result for every line, never a raise
        for row in candidates:
            results[row.id] = _Attempt(engine, row, now, po, new_of(row)).failed(
                exc, previous=None, sent={},
                message=f"Couldn't read {po} from NetSuite, so nothing was written to line "
                        f"{row.ns_line_id}. {_plain_error(exc, after_send=False)}")
        return done()

    # 2. Refuse what the re-read says is not ours, in the per-line words.
    pending: dict = {}  # line id -> (row, previous, new, the fields sent for it)
    for row in candidates:
        line = before.get(str(row.ns_line_id))
        new = new_of(row)
        previous = _quantity_of(line) if line is not None else None
        refusal = _reread_refusal(row, line, po, where(row), new)
        if refusal:
            matched = _proven_earlier_attempt(engine, row, line, new)
            attempt = _Attempt(engine, row, now, po, new)
            results[row.id] = (attempt.already_present(previous, matched, where(row))
                               if matched is not None
                               else attempt.refused(refusal[0], previous, refusal[1]))
            continue
        if previous == new:
            # Not refused, so the line still holds the proposal's base -- and the
            # base IS the approved figure. Decimal comparison: numeric, so 5 and
            # 5.000 are equal. Nothing to send; confirmed, never written.
            results[row.id] = _Attempt(engine, row, now, po, new).confirmed(previous, where(row))
            continue
        fields = _quantity_payload(new)
        _assert_quantity_only(fields)
        pending[str(row.ns_line_id)] = (row, previous, new, nc.normalize_line_fields(fields))

    # The existence check, made structural for the batch: every line sent is on
    # the fresh read. An unknown line number is a NEW line to NetSuite.
    unread = set(pending) - set(before)
    if unread:
        raise WritePathViolation(f"Refusing to send lines not on the fresh read: {sorted(unread)}")

    # 3. One PATCH; on a 400 naming a line, drop it and resend, up to the cap.
    requests, outcome, send_exc, accepted_status, key = 0, None, None, None, None
    while pending and requests < MAX_BATCH_REQUESTS:
        requests += 1
        key = str(uuid.uuid4())
        try:
            response = client.update_po_lines(
                internal_id, {ln: p[3] for ln, p in pending.items()}, idempotency_key=key)
            outcome, accepted_status = "accepted", response.get("status_code")
            break
        except Exception as exc:  # noqa: BLE001 -- dispatched below; a timeout may have landed
            send_exc = exc
            if nc.request_was_sent(exc) is False:
                # Provably never left: nothing to confirm, nothing to credit.
                outcome = "not_sent"
                requests -= 1
                break
            if nc.request_may_have_applied(exc) is not False:
                # An earlier try of THIS request went out and got no 4xx (a timeout,
                # a drop, a 5xx), so it may have landed -- whatever the final answer
                # says about the last try. The closing re-read decides. Unknown
                # (None: not raised through the client) counts as "may have".
                outcome = "unconfirmed"
                break
            if isinstance(exc, (nc.NetSuitePermissionError, nc.NetSuiteAuthError)):
                # A permission refusal, or a 401 the token refresh did not cure, with
                # no earlier try that may have landed: NetSuite applied nothing. No
                # closing re-read to credit a value someone else typed, and never
                # ALREADY_PRESENT evidence (`_error_kind` says PERMISSION).
                outcome = "permission"
                break
            if not isinstance(exc, nc.NetSuiteAPIError):
                outcome = "unconfirmed"  # untagged or unknown: may have landed
                break
            named = [ln for ln in _error_lines(exc) if ln in pending]
            if not named:
                outcome, send_exc = "fallback", exc
                break
            # Every line the 400 names goes at once: dropping one per request
            # would spend the cap on a single response that named several.
            for ln in named:
                row, previous, new, sent = pending.pop(ln)
                results[row.id] = _Attempt(engine, row, now, po, new,
                                           idempotency_key=key).refused(
                    "VALIDATION", previous,
                    f"NetSuite refused {where(row)} ({_error_text(exc, ln)}), so nothing was "
                    "written to it. The rest of the PO was sent without it.",
                    sent=sent, http_status=exc.status_code, detail=f"{type(exc).__name__}: {exc}")
            send_exc = None  # resending: this 400 is settled line by line above

    if not pending:
        return done(requests)  # every line refused or dropped: nothing to confirm

    if outcome is None:
        # The cap: every request was a 400 naming a different line.
        for row, previous, new, sent in pending.values():
            results[row.id] = _Attempt(engine, row, now, po, new, idempotency_key=key).unwritten(
                "VALIDATION", previous, sent,
                f"NetSuite rejected this PO's batch {MAX_BATCH_REQUESTS} times, naming a "
                f"different line each time, so the tool stopped. Nothing was written to "
                f"{where(row)}; it can be tried again.")
        return done(requests)

    if outcome == "not_sent":
        # No closing re-read: a line found holding the value now was put there by
        # someone else, and must not be recorded as this tool's SUCCESS. Recorded
        # FAILED with nothing sent, so it can never justify ALREADY_PRESENT either.
        for row, previous, new, _sent in pending.values():
            results[row.id] = _Attempt(engine, row, now, po, new, idempotency_key=key).failed(
                send_exc, previous=previous, sent={},
                message=_not_sent_message(send_exc, where(row)))
        return done(requests)

    if outcome == "permission":
        for row, previous, new, sent in pending.values():
            results[row.id] = _Attempt(engine, row, now, po, new, idempotency_key=key).failed(
                send_exc, previous=previous, sent=sent,
                message=f"Couldn't write {where(row)}. {_plain_error(send_exc, after_send=True)}")
        return done(requests)

    if outcome == "fallback":
        # A 400 naming no line: nothing was applied, and nothing says which line
        # caused it. Each line goes on its own, through the per-line helper, which
        # re-reads and records for itself.
        for row, _previous, _new, _sent in pending.values():
            results[row.id] = write_approved_quantity(engine, row.id, client=client, now=now)
        return done(requests, fell_back=True)

    # 4. Re-read and record what actually landed -- after a 204 AND after a timeout.
    after, read_exc = None, None
    for _ in range(CLOSING_READ_ATTEMPTS):
        try:
            after = {ln.line_id: ln for ln in
                     client.get_purchase_order_lines_by_internal_id(internal_id)}
            break
        except Exception as exc:  # noqa: BLE001
            read_exc = exc
    if after is None:
        # Nothing can say what landed. Not inferred from a 204: recorded FAILED,
        # with the message saying to check NetSuite first. If it did land, a retry
        # finds the value this attempt sent and records ALREADY_PRESENT (RUNBOOK
        # section 11 decision 2) -- the row written here is what qualifies it.
        exc = read_exc
        for row, previous, new, sent in pending.values():
            said = ("NetSuite accepted the write" if outcome == "accepted"
                    else "NetSuite didn't confirm the write")
            results[row.id] = _Attempt(engine, row, now, po, new, idempotency_key=key).unwritten(
                "TRANSIENT", previous, sent,
                f"{said} to {where(row)}, but the tool couldn't re-read the PO to see whether "
                "it landed. Check the line in NetSuite before trying again.",
                http_status=accepted_status, detail=f"closing re-read: {type(exc).__name__}: {exc}")
        return done(requests)

    for ln, (row, previous, new, sent) in pending.items():
        landed = after.get(ln)
        actual = _quantity_of(landed) if landed is not None else None
        attempt = _Attempt(engine, row, now, po, new, idempotency_key=key)
        if actual == new:
            message = f"{where(row)}: quantity {_fmt(previous)} → {_fmt(new)}."
            if outcome == "unconfirmed":
                message += " NetSuite didn't confirm the write, but the PO now shows it."
            if row.date_write_status not in ("NONE", "WRITTEN"):
                message += (" A receipt date recorded with it was not written: this tool does "
                            "not write dates to NetSuite.")
            results[row.id] = attempt.succeeded(
                previous, sent, accepted_status if outcome == "accepted" else None, message)
            continue
        shows = (f"now shows {_fmt(actual)}, not {_fmt(new)}" if landed is not None
                 else "is no longer on the PO")
        if outcome == "accepted":
            results[row.id] = attempt.unwritten(
                "OTHER", previous, sent,
                f"NetSuite accepted the write to {where(row)}, but the line {shows}. Nothing "
                "is recorded as written; check the line in NetSuite.",
                http_status=accepted_status,
                detail=f"closing re-read disagrees: sent {_fmt(new)}, read {_fmt(actual)}")
        else:
            results[row.id] = attempt.unwritten(
                _error_kind(send_exc), previous, sent,
                f"Couldn't write {where(row)}: NetSuite didn't confirm the write, and the line "
                f"{shows}. Trying again is safe.",
                http_status=_http_status(send_exc), detail=f"{type(send_exc).__name__}: {send_exc}")
    return done(requests)


def write_approved_quantity(
    engine, change_id: str, *, client=None, now: Optional[dt.datetime] = None
) -> LineWriteResult:
    """
    Write one approved line's quantity to NetSuite, in its own request.

    The INTERNAL helper for `write_po_batch`'s per-line fallback (a 400 naming
    no line). The review app calls `write_po_batch`, never this.
    """
    now = now or _utcnow()
    row = _load(engine, change_id)
    po = f"PO {row.po_number_key}"
    where = f"{po} line {row.ns_line_id}"
    new = Decimal(row.approved_quantity) if row.approved_quantity is not None else None

    refusal = _precondition_refusal(row, where)
    if refusal:
        return _refused_before_contact(engine, row, now, po, new, refusal)

    client = client or _live_client()
    refusal = _account_refusal(client, where)
    if refusal:
        return _refused_before_contact(engine, row, now, po, new, refusal)
    attempt = _Attempt(engine, row, now, po, new)

    # 1. Re-read, immediately before writing. Any exception is caught, not only
    # NetSuite's own: the caller is mid-PO and must get a result for every line.
    try:
        lines = client.get_purchase_order_lines_by_internal_id(row.ns_internal_id)
    except Exception as exc:  # noqa: BLE001
        return attempt.failed(exc, previous=None, sent={},
                              message=f"Couldn't read {where} from NetSuite, so nothing was "
                                      f"written. {_plain_error(exc, after_send=False)}")

    line = next((ln for ln in lines if ln.line_id == str(row.ns_line_id)), None)
    previous = _quantity_of(line) if line is not None else None

    # 2. Refuse what is not ours to write -- including a line the re-read did not
    # find, which is what stops the tool creating PO lines (see `_reread_refusal`).
    refusal = _reread_refusal(row, line, po, where, new)
    if refusal:
        matched = _proven_earlier_attempt(engine, row, line, new)
        if matched is not None:
            return attempt.already_present(previous, matched, where)
        return attempt.refused(refusal[0], previous, refusal[1])
    if previous == new:
        # Unchanged since the proposal, and already the approved figure (numeric
        # Decimal comparison). Nothing to send: confirmed, never written.
        return attempt.confirmed(previous, where)

    # 3. Quantity, and only quantity.
    fields = _quantity_payload(new)
    _assert_quantity_only(fields)
    sent = nc.normalize_line_fields(fields)
    try:
        response = client.update_po_line(row.ns_internal_id, row.ns_line_id, fields,
                                         by_internal_id=True,
                                         idempotency_key=attempt.idempotency_key)
    except Exception as exc:  # noqa: BLE001 -- see the re-read above
        if nc.request_was_sent(exc) is False:
            return attempt.failed(exc, previous=previous, sent={},
                                  message=_not_sent_message(exc, where))
        return attempt.failed(exc, previous=previous, sent=sent,
                              message=f"Couldn't write {where}. "
                                      f"{_plain_error(exc, after_send=True)}")

    message = f"{where}: quantity {_fmt(previous)} → {_fmt(new)}."
    if row.date_write_status not in ("NONE", "WRITTEN"):
        # Only a row approved before 2026-11-02 can carry one.
        message += (" A receipt date recorded with it was not written: this tool does "
                    "not write dates to NetSuite.")
    return attempt.succeeded(previous, sent, response.get("status_code"), message)


# ---------------------------------------------------------------------------
# reading the row, and what can be refused before NetSuite is contacted
# ---------------------------------------------------------------------------


def _row_select():
    pc, sp, s = proposed_changes, shipment_pos, shipments
    return (select(pc.c.id, pc.c.state, pc.c.ns_line_id, pc.c.current_quantity,
                   pc.c.approved_quantity, pc.c.quantity_approved_by,
                   pc.c.quantity_write_status,
                   pc.c.date_write_status, pc.c.shipment_id, sp.c.po_number_key,
                   sp.c.ns_internal_id, s.c.origin)
            .select_from(pc.join(sp, pc.c.shipment_po_id == sp.c.id)
                           .join(s, pc.c.shipment_id == s.c.id)))


def _load(engine, change_id: str):
    with engine.connect() as conn:
        row = conn.execute(_row_select().where(proposed_changes.c.id == change_id)
                           ).one_or_none()
    if row is None:
        raise LookupError(f"No proposed_changes row {change_id!r}.")
    return row


def _load_po(engine, shipment_po_id: str):
    """The PO, and its lines approved for a quantity write, in line order.

    Lines not approved -- blocked, pending, no-change, rejected, already
    written -- are not part of a write at all and are not returned."""
    sp = shipment_pos
    with engine.connect() as conn:
        head = conn.execute(select(sp.c.id, sp.c.po_number_key, sp.c.ns_internal_id)
                            .where(sp.c.id == shipment_po_id)).one_or_none()
        if head is None:
            raise LookupError(f"No shipment_pos row {shipment_po_id!r}.")
        rows = conn.execute(
            _row_select()
            .where(proposed_changes.c.shipment_po_id == shipment_po_id)
            .where(proposed_changes.c.quantity_write_status.in_(("APPROVED", "FAILED")))
            # Not a line the reviewer left as it is (DISCARDED keeps its FAILED status
            # as history); WRITTEN with a stale status is refused by the precondition.
            .where(proposed_changes.c.state != sc.STATE_DISCARDED)
        ).all()
    return head, sorted(rows, key=lambda r: (_line_sort_key(r.ns_line_id), r.id))


def _line_sort_key(line_id) -> tuple:
    text = str(line_id or "")
    return (0, int(text), "") if text.isdigit() else (1, 0, text)


def _refused_before_contact(engine, row, now, po: str, new, message: str) -> LineWriteResult:
    """
    A line refused before NetSuite was contacted: a database precondition, no live
    connection, or production writes switched off. No `write_attempts` row --
    nothing reached NetSuite, and a REFUSED row would read as "refused every time"
    and stop it being sent again once the cause is fixed. The reason goes to
    `audit_log` as `NOT_SENT_EVENT` instead, so it survives a restart (Kiko,
    2026-11-04); the line's state is left as it was.

    A failure to record it is logged, never raised: nothing was sent, so there is
    nothing in NetSuite the database fails to know about, and the caller must
    still get a result for every line.
    """
    try:
        with engine.begin() as conn:
            conn.execute(audit_log.insert(), {
                "id": sc.new_id(), "occurred_at": now,
                "workflow": _WORKFLOW_BY_ORIGIN[row.origin], "actor": ACTOR,
                "actor_kind": "SYSTEM", "event": NOT_SENT_EVENT,
                "message_id": None, "shipment_id": row.shipment_id, "change_id": row.id,
                "from_state": row.state, "to_state": row.state,
                "detail_json": json.dumps({"new_quantity": _num(new), "message": message}),
            })
    except Exception:  # noqa: BLE001 -- logged; the per-line result still comes back
        logger.exception("could not record why %s line %s was not sent", po, row.ns_line_id)
    return LineWriteResult(row.id, po, row.ns_line_id, REFUSED, message, new_quantity=new)


def _precondition_refusal(row, where: str) -> Optional[str]:
    """A reason not to contact NetSuite at all, or None. No `write_attempts` row is
    recorded for these; `_refused_before_contact` records the reason in the audit."""
    if row.quantity_write_status == "WRITTEN":
        return f"{where} was already written, so nothing was sent again."
    # APPROVED or WRITE_FAILED only. A WRITTEN row with an approved scope is
    # inconsistent -- nothing re-approves a WRITTEN row since the date scope went
    # (2026-11-02) -- and failing it would need WRITTEN -> WRITE_FAILED, which is
    # not legal.
    if (row.quantity_write_status not in ("APPROVED", "FAILED")
            or row.state not in (sc.STATE_APPROVED, sc.STATE_WRITE_FAILED)
            or row.approved_quantity is None or not row.quantity_approved_by):
        return f"{where} isn't approved for writing, so nothing was sent."
    if not row.ns_internal_id:
        return f"{where} has no NetSuite record id on file, so nothing was sent."
    return None


def _reread_refusal(row, line, po: str, where: str, new) -> Optional[tuple[str, str]]:
    """
    Why the fresh read says this line is not ours to write, as (error_kind,
    message), or None. Shared by the batch and the per-line helper, so both
    refuse the same lines in the same words.
    """
    # LOAD-BEARING. NetSuite treats a PATCH entry with an unknown line number as
    # ADDING a new line: `{"line": 9999, "quantity": 5}` failed in sandbox
    # (2026-11-02) only because a new line needs an Amount. This check is what
    # stops the tool creating PO lines. Anything that widens the payload -- more
    # fields, or a batch -- must still send only lines present on the fresh read.
    # Phase 3 requirements section 14.
    if line is None:
        return "OTHER", (f"{po} no longer has a line {row.ns_line_id} in NetSuite, so "
                         "nothing was written.")
    previous = _quantity_of(line)
    if line.closed:
        return "LINE_CLOSED", f"{where} is closed in NetSuite, so nothing was written."
    if not line.is_open:
        return "LINE_CLOSED", (f"{where} is no longer open in NetSuite (nothing is left to "
                               "receive on it), so nothing was written.")
    base = Decimal(row.current_quantity) if row.current_quantity is not None else None
    if base is None:
        return "CONFLICT", (
            f"There is no record of what {where} showed when this change was worked out, "
            "so the tool can't tell whether someone has edited it since. Nothing was written.")
    if previous != base:
        if previous == new:
            return "CONFLICT", (
                f"{where} already shows {_fmt(previous)}, the quantity being written. An "
                "earlier attempt may have reached NetSuite even though it reported a "
                "failure, or someone entered it by hand. Nothing was written; check the "
                "line in NetSuite.")
        return "CONFLICT", (
            f"{where} now shows {_fmt(previous)} in NetSuite, but this change was "
            f"worked out when it showed {_fmt(base)}. Someone has edited it since, so "
            "nothing was written.")
    return None


def _proven_earlier_attempt(engine, row, line, new) -> Optional[int]:
    """
    The attempt_no of an earlier attempt that justifies ALREADY_PRESENT, or None.

    Narrow by design (Kiko, 2026-11-02): **a matching value alone never means
    success.** All of these must hold:
    - the re-read refused ONLY because the line already holds the value being
      written -- open, not closed, moved off the proposal's base, onto `new`;
    - a `write_attempts` row exists for THIS proposal (`change_id`), QUANTITY scope;
    - it recorded sending EXACTLY `new` (its `payload_json`);
    - it **failed after the request was sent**, precisely: outcome FAILED, and
      a non-empty `payload_json`. The write path records the payload only when
      the client could NOT rule out that the PATCH reached NetSuite
      (`nc.request_was_sent` is not False); a request it proved never left -- a
      local error, a connect timeout, DNS, a refused connection -- is recorded
      with `{}` (Kiko, 2026-11-04: a local error cannot be evidence our write
      landed). And not a permission refusal or a 400 (VALIDATION), which NetSuite
      answered by applying nothing. A REFUSED row never qualifies, nor a row from
      another proposal on the same line.

    **Deliberately kept:** a 204 whose closing re-read disagreed (FAILED/OTHER,
    payload set). NetSuite said it accepted the write, which is stronger evidence
    it landed than a timeout, not weaker (Kiko, 2026-11-04).

    Someone typing the same number by hand, with no such attempt on record, is
    still refused as "already shows".
    """
    if line is None or line.closed or not line.is_open or new is None:
        return None
    base = Decimal(row.current_quantity) if row.current_quantity is not None else None
    if base is None or _quantity_of(line) != new or new == base:
        return None
    wa = write_attempts
    with engine.connect() as conn:
        rows = conn.execute(
            select(wa.c.attempt_no, wa.c.outcome, wa.c.payload_json, wa.c.error_kind)
            .where(wa.c.change_id == row.id).where(wa.c.scope == "QUANTITY")
            # The same PO and line too: an attempt aimed elsewhere, were a proposal
            # ever re-targeted, is not evidence about this line.
            .where(wa.c.ns_internal_id == row.ns_internal_id)
            .where(wa.c.ns_line_id == row.ns_line_id)
            .where(wa.c.outcome == "FAILED")
            .order_by(wa.c.attempt_no.desc())
        ).all()
    for attempt in rows:
        try:
            sent = json.loads(attempt.payload_json or "{}")
        except ValueError:
            continue
        if (_reached_netsuite(attempt.outcome, sent, attempt.error_kind) == "unknown"
                and sent.get(nc.NS_QUANTITY) is not None
                and Decimal(str(sent[nc.NS_QUANTITY])) == new):
            return attempt.attempt_no
    return None


def _account_refusal(client, where: str) -> Optional[str]:
    """
    Sandbox first, always (CLAUDE.md working agreement). A production account is
    refused unless `PRODUCTION_WRITES_ENV` names THAT account -- the same shape as
    `PO_AGENT_MIGRATE_CONFIRM`: no generic force flag, and set for the cutover,
    not left lying in a shell. The production role's View permission (RUNBOOK
    section 12 item 5) is a second layer behind this, never a substitute for it.
    """
    config = getattr(client, "config", None)
    if config is None:
        return f"There is no live NetSuite connection configured, so {where} was not sent."
    if config.is_sandbox:
        return None
    if os.environ.get(PRODUCTION_WRITES_ENV, "").strip() == config.account_id:
        return None
    return (f"{where} was not sent: the tool is connected to the production NetSuite "
            f"account ({config.account_id}), and production writes are not switched on.")


# ---------------------------------------------------------------------------
# recording an attempt
# ---------------------------------------------------------------------------


class _Attempt:
    """Records one attempt's row, state change and audit entry, and builds the result."""

    def __init__(self, engine, row, now, po, new, idempotency_key=None):
        self.engine, self.row, self.now, self.po, self.new = engine, row, now, po, new
        #: A batch passes its request's key, so every line of one PATCH shares it.
        self.idempotency_key = idempotency_key or str(uuid.uuid4())

    def succeeded(self, previous, sent, http_status, message) -> LineWriteResult:
        return self._record("SUCCESS", WRITTEN, previous=previous, sent=sent,
                            http_status=http_status, error_kind=None, detail=None,
                            message=message)

    def refused(self, error_kind, previous, message, *, sent=None, http_status=None,
                detail=None) -> LineWriteResult:
        """Not applied. `sent` stays {} when the re-read refused it before sending;
        a line NetSuite rejected inside a batch records what was sent for it."""
        return self._record("REFUSED", REFUSED, previous=previous, sent=sent or {},
                            http_status=http_status, error_kind=error_kind,
                            detail=detail or message, message=message)

    def already_present(self, previous, matched_attempt_no, where) -> LineWriteResult:
        """The line holds exactly what attempt `matched_attempt_no` of this proposal
        sent. Nothing is sent now; the line is settled, and says it is unproven."""
        message = (f"{where} already shows {_fmt(self.new)}, the quantity an earlier attempt "
                   f"(attempt {matched_attempt_no}) sent without NetSuite confirming it. "
                   "Recorded as already present: the line holds the intended value, but the "
                   "tool can't prove that attempt made the change.")
        return self._record("ALREADY_PRESENT", ALREADY_PRESENT, previous=previous, sent={},
                            http_status=None, error_kind=None,
                            detail=f"matches attempt {matched_attempt_no}, which sent "
                                   f"{_fmt(self.new)} unconfirmed", message=message)

    def confirmed(self, previous, where) -> LineWriteResult:
        """The line already holds the approved figure and has not moved since the
        proposal. Nothing is sent; the line is settled as confirmed, not written."""
        message = (f"{where} already holds {_fmt(self.new)}, the approved figure, so nothing "
                   "was sent. Confirmed as it is: no change was needed.")
        return self._record("CONFIRMED", CONFIRMED, previous=previous, sent={},
                            http_status=None, error_kind=None,
                            detail=f"re-read {_fmt(previous)} equals the approved "
                                   f"{_fmt(self.new)}", message=message)

    def unwritten(self, error_kind, previous, sent, message, *, http_status=None,
                  detail=None) -> LineWriteResult:
        """FAILED for a reason that is not one exception: the closing re-read
        disagreeing, or the batch stopping at its request cap."""
        return self._record("FAILED", FAILED, previous=previous, sent=sent,
                            http_status=http_status, error_kind=error_kind,
                            detail=detail or message, message=message)

    def failed(self, exc, *, previous, sent, message) -> LineWriteResult:
        return self._record("FAILED", FAILED, previous=previous, sent=sent,
                            http_status=_http_status(exc), error_kind=_error_kind(exc),
                            detail=f"{type(exc).__name__}: {exc}", message=message)

    def _record(self, outcome, status, *, previous, sent, http_status, error_kind,
                detail, message) -> LineWriteResult:
        row = self.row
        attempt_no = None
        try:
            for tries in range(1, _ATTEMPT_NO_RETRIES + 1):
                try:
                    attempt_no = self._insert(outcome, previous, sent, http_status,
                                              error_kind, detail, message)
                    break
                except (IntegrityError, _RowMoved):
                    # attempt_no is MAX+1 and the row's state is read inside the
                    # transaction, so a concurrent attempt on the same change can
                    # win either race. Recompute from what it left.
                    if tries == _ATTEMPT_NO_RETRIES:
                        raise
        except Exception as exc:  # noqa: BLE001 -- the caller must get a result for every line
            # Caught so the caller still gets a per-line result -- and SURFACED, never
            # silent: logged at error level or above, appended to the unrecorded-writes
            # file the review app shows, and kept for tests (`unrecorded_failures`).
            _surface_unrecorded(self, outcome, previous, sent, http_status, error_kind,
                                message, exc)
            if outcome == "SUCCESS":
                # The line IS changed. Saying so plainly matters more than anything:
                # a retry would be refused (the re-read sees the new value), and the
                # accumulation base does not know this write happened.
                status = WRITTEN_NOT_RECORDED
                message += (" NetSuite accepted the write, but the tool could not record "
                            "it. Don't try again; tell Kiko.")
            elif outcome in ("ALREADY_PRESENT", "CONFIRMED"):
                # Not settled after all: the record that would settle it is missing.
                # Nothing was sent, and the earlier attempt is still on record, so a
                # retry reaches the same conclusion and records it.
                status = FAILED
                message += (" The tool could not record that, so the line is not settled "
                            "yet. Trying again is safe: nothing is sent.")
            else:
                message += " (The tool could not record this attempt.)"
        return LineWriteResult(row.id, self.po, row.ns_line_id, status, message,
                               previous_quantity=previous, new_quantity=self.new,
                               error_kind=error_kind, attempt_no=attempt_no)

    def _insert(self, outcome, previous, sent, http_status, error_kind, detail,
                message) -> int:
        """
        One transaction: the attempt row, the state change, the audit row.

        The state change is decided from the row AS IT IS NOW, not as `_load` saw
        it, because another call may have recorded since (two tabs, a double
        click). Two rules make the outcome order-independent:

        - **A SUCCESS always lands, as WRITTEN.** NetSuite holds the value; the row
          must say so or the accumulation base (`ingest._line_history`, which needs
          `quantity_write_status = 'WRITTEN'`) loses a real write.
        - **A non-success never overwrites a WRITTEN status.** The attempt row is
          kept as history, and the state is left as the successful call set it.

        The update is conditional on the state and status just read, so a call
        that slips in between is detected (`_RowMoved`) and the decision re-made.
        """
        row, wa, pc = self.row, write_attempts, proposed_changes
        with self.engine.begin() as conn:
            now_row = conn.execute(
                select(pc.c.state, pc.c.quantity_write_status)
                .where(pc.c.id == row.id)).one()
            from_state, from_status = now_row.state, now_row.quantity_write_status
            if outcome in ("SUCCESS", "ALREADY_PRESENT", "CONFIRMED"):
                # Always WRITTEN -- ALREADY_PRESENT and CONFIRMED settle the line
                # too, each under its own name. A date scope can only exist on a row approved
                # before 2026-11-02 (the review app no longer records one), and no
                # date is ever written, so keeping such a row APPROVED would leave
                # it approved with nothing writable, forever. Its date columns stay
                # as recorded, unused (RUNBOOK section 4).
                to_status, to_state = "WRITTEN", sc.STATE_WRITTEN
            elif from_status == "WRITTEN":
                to_status, to_state = from_status, from_state
            else:
                # A refusal is a WRITE_FAILED too (CLAUDE.md gotchas): retryable, or
                # given up on by a human, and never silently left APPROVED.
                to_status, to_state = "FAILED", sc.STATE_WRITE_FAILED

            attempt_no = (conn.execute(
                select(func.max(wa.c.attempt_no))
                .where(wa.c.change_id == row.id).where(wa.c.scope == "QUANTITY")
            ).scalar() or 0) + 1
            conn.execute(wa.insert(), {
                "id": sc.new_id(), "change_id": row.id, "scope": "QUANTITY",
                "attempt_no": attempt_no, "ns_internal_id": row.ns_internal_id,
                "ns_line_id": row.ns_line_id, "payload_json": json.dumps(sent),
                "idempotency_key": self.idempotency_key, "outcome": outcome,
                "previous_quantity": previous, "http_status": http_status,
                "error_kind": error_kind, "error_detail": detail,
                "attempted_at": self.now,
            })
            if to_state != from_state:
                sc.assert_transition(conn, from_state, to_state)
            moved = conn.execute(
                pc.update()
                .where(pc.c.id == row.id)
                .where(pc.c.state == from_state)
                .where(pc.c.quantity_write_status == from_status)
                .values(state=to_state, quantity_write_status=to_status, updated_at=self.now)
            ).rowcount
            if moved != 1:
                raise _RowMoved(row.id)  # rolls the whole transaction back
            conn.execute(audit_log.insert(), {
                "id": sc.new_id(), "occurred_at": self.now,
                "workflow": _WORKFLOW_BY_ORIGIN[row.origin], "actor": ACTOR,
                "actor_kind": "SYSTEM", "event": f"QUANTITY_WRITE_{outcome}",
                "message_id": None, "shipment_id": row.shipment_id, "change_id": row.id,
                "from_state": from_state, "to_state": to_state,
                "detail_json": json.dumps({
                    "attempt_no": attempt_no, "previous_quantity": _num(previous),
                    "new_quantity": _num(self.new), "sent": sent,
                    "error_kind": error_kind, "message": message,
                }),
            })
        return attempt_no


class _RowMoved(Exception):
    """The row changed between reading its state and updating it, inside one attempt."""


# ---------------------------------------------------------------------------
# a recording failure is never silent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnrecordedFailure:
    """One attempt whose `write_attempts` row could not be written."""

    seq: int  # increasing per process; lets a caller ask "anything since N?"
    change_id: str
    po: str
    line: Optional[str]
    outcome: str  # SUCCESS = the write reached NetSuite with no record: the worst case
    error: BaseException  # the swallowed exception itself, for re-raising in tests


#: Every recording failure caught in this process, oldest first. Tests re-raise
#: the exception from here so the harness sees it (a dropped database link is
#: otherwise swallowed and looks like an assertion failure). Grows only on a
#: failure, which is rare by definition.
unrecorded_failures: list = []


def unrecorded_seq() -> int:
    """The seq of the latest recording failure in this process, or 0."""
    return unrecorded_failures[-1].seq if unrecorded_failures else 0


def _reached_netsuite(outcome, sent, error_kind):
    """True, False, or "unknown" -- did this attempt change NetSuite?

    SUCCESS: yes. REFUSED: no (refused before sending, or a 400 that applies
    nothing). FAILED with nothing sent -- `{}`, which includes a request the
    client proved never left the machine -- or refused by a permission error or a
    400: no. Any other FAILED that sent something -- a timeout, a dropped link,
    an error after the request left -- may have landed: "unknown", which is
    alarmed exactly like True. Overstating is the safe direction."""
    if outcome == "SUCCESS":
        return True
    if outcome == "ALREADY_PRESENT":
        return "unknown"  # the line holds our value; the tool cannot say who put it there
    if outcome == "REFUSED" or not sent or error_kind in ("PERMISSION", "VALIDATION"):
        return False
    return "unknown"


def _surface_unrecorded(attempt, outcome, previous, sent, http_status, error_kind,
                        message, exc) -> None:
    """Log, append to the unrecorded-writes file, and keep. Never raises: it runs
    inside `_record`'s handler, and a raise here would cost the caller every
    per-line result still to come -- after NetSuite was written."""
    try:
        import config

        row = attempt.row
        reached = _reached_netsuite(outcome, sent, error_kind)
        entry = {
            "at": attempt.now.isoformat(), "outcome": outcome,
            "reached_netsuite": reached, "change_id": row.id, "po": attempt.po,
            "line": row.ns_line_id, "ns_internal_id": row.ns_internal_id,
            "previous_quantity": _num(previous), "new_quantity": _num(attempt.new),
            "sent": sent, "http_status": http_status, "error_kind": error_kind,
            "idempotency_key": attempt.idempotency_key, "message": message,
            "error": f"{type(exc).__name__}: {exc}",
        }
        # `logging.config.fileConfig` disables every logger that already exists
        # unless told otherwise, and Alembic's env.py calls it: in a process that
        # ran a migration first, this line was silently dropped (found by the full
        # suite, 2026-11-02). Deliberately re-enabled, module-wide, every time.
        logger.disabled = False
        logger.log(
            logging.ERROR if reached is False else logging.CRITICAL,
            "%s: write_attempts NOT recorded for change %s (%s line %s, outcome %s): %s",
            {True: "WRITE REACHED NETSUITE WITH NO RECORD",
             "unknown": "WRITE MAY HAVE REACHED NETSUITE, WITH NO RECORD",
             False: "attempt not recorded"}[reached],
            row.id, attempt.po, row.ns_line_id, outcome, entry["error"], exc_info=exc)
        unrecorded_failures.append(UnrecordedFailure(
            unrecorded_seq() + 1, row.id, attempt.po, row.ns_line_id, outcome, exc))
        path = config.unrecorded_writes_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except Exception:  # noqa: BLE001 -- the log line above is then the only record
            logger.critical("and the unrecorded write could not be appended to %s either: %s",
                            path, json.dumps(entry, default=str), exc_info=True)
    except Exception:  # noqa: BLE001 -- the last resort: one line, whatever failed above
        logging.getLogger("write_path").critical(
            "write_attempts NOT recorded for change %s (outcome %s), and surfacing it failed "
            "too: %r", getattr(getattr(attempt, "row", None), "id", "?"), outcome, exc,
            exc_info=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _quantity_payload(new: Decimal) -> dict:
    return {nc.NS_QUANTITY: new}


def _assert_quantity_only(fields: dict) -> None:
    """Raise before sending if the payload holds anything but quantity."""
    extra = set(fields) - SENT_FIELDS
    if extra or not fields:
        banned = sorted(extra & NEVER_SENT)
        raise WritePathViolation(
            f"The write path sends quantity and nothing else; this payload also holds "
            f"{sorted(extra) or '(nothing at all)'}"
            + (f", of which {banned} must never be sent" if banned else "")
            + ". Nothing was sent.")


def _quantity_of(line: nc.POLine) -> Decimal:
    """The line's quantity as NetSuite sent it. `POLine.quantity` truncates to int,
    which could hide a fractional edit from the conflict check."""
    raw = (line.raw or {}).get(nc.NS_QUANTITY)
    return Decimal(str(raw if raw is not None else line.quantity))


def _error_kind(exc: BaseException) -> str:
    kind = _error_kind_of_last_try(exc)
    # A "nothing applied" kind speaks for the LAST try only. If an earlier try of
    # this request went out and got no 4xx (a timeout, a drop, a 5xx), it may have
    # landed: TRANSIENT keeps the attempt what it is -- unconfirmed, retryable, and
    # evidence a later ALREADY_PRESENT may rest on. Recording PERMISSION or
    # VALIDATION would say "nothing applied" of a write that may have landed and
    # strand the line (review, 2026-11-04). Other kinds already mean "may have".
    if kind in ("PERMISSION", "VALIDATION") and nc.request_may_have_applied(exc) is not False:
        return "TRANSIENT"  # None (unknown) counts as "may have", like `request_was_sent`
    return kind


def _error_kind_of_last_try(exc: BaseException) -> str:
    # NetSuiteAuthError with the permission refusals (Kiko, 2026-11-04): with no
    # earlier try that may have landed, NetSuite applied nothing -- no token was
    # obtained, or it answered 401 and the refresh failed. ("A 401 the refresh DID
    # cure, then 401 again" already arrives as a NetSuitePermissionError from
    # `_raise_for_response`.) As "OTHER" it read as "may have landed" and could
    # justify ALREADY_PRESENT.
    if isinstance(exc, (nc.NetSuitePermissionError, nc.NetSuiteAuthError)):
        return "PERMISSION"
    if isinstance(exc, nc.NetSuiteTransientError):
        return "TRANSIENT"
    if isinstance(exc, nc.NetSuiteAPIError) and exc.status_code and 400 <= exc.status_code < 500:
        return "VALIDATION"
    return "OTHER"


def _http_status(exc: BaseException) -> Optional[int]:
    if isinstance(exc, nc.NetSuiteAPIError):
        return exc.status_code
    if isinstance(exc, nc.NetSuiteTransientError):
        return exc.last_status
    return None


_LINE_IN_ERROR_PATH = re.compile(r"@\.line\s*==\s*(\d+)")


def _error_details(exc: BaseException) -> list:
    payload = getattr(exc, "payload", None)
    return (payload.get("o:errorDetails") or []) if isinstance(payload, dict) else []


def _error_lines(exc: BaseException) -> list:
    """Every line a 400 names in `o:errorPath` (`item.items[?(@.line==3)]`), in
    order, without repeats. Measured 2026-11-02: NetSuite names the offending
    entry this way. A path in any other form names nothing here, and the batch
    falls back to per-line writes -- slow, never wrong."""
    lines: list = []
    for detail in _error_details(exc):
        for match in _LINE_IN_ERROR_PATH.finditer(str(detail.get("o:errorPath") or "")):
            if match.group(1) not in lines:
                lines.append(match.group(1))
    return lines


def _error_text(exc: BaseException, line: Optional[str] = None) -> str:
    """NetSuite's own sentence for a 400 -- the one about `line`, if it named it."""
    details = _error_details(exc)
    if line is not None:
        for detail in details:
            path = str(detail.get("o:errorPath") or "")
            if line in [m.group(1) for m in _LINE_IN_ERROR_PATH.finditer(path)] \
                    and detail.get("detail"):
                return str(detail["detail"]).strip()
    for detail in details:
        if detail.get("detail"):
            return str(detail["detail"]).strip()
    return "NetSuite gave no reason"


def _plain_error(exc: BaseException, *, after_send: bool) -> str:
    kind = _error_kind(exc)
    if isinstance(exc, nc.NetSuiteAuthError) and kind == "PERMISSION":
        return ("NetSuite didn't accept the tool's login, so nothing was changed. Let Kiko "
                "know, and send it again once the login is fixed.")
    if kind == "PERMISSION":
        return ("NetSuite refused: the tool's NetSuite role doesn't have permission to "
                "change this PO.")
    if kind == "TRANSIENT":
        if after_send:
            return ("NetSuite didn't confirm the write. It may or may not have been "
                    "applied. Trying again is safe: the tool re-reads the line first and "
                    "won't write over a change.")
        return "NetSuite didn't respond after several tries."
    return "NetSuite returned an error; the details are in the write history."


def _not_sent_message(exc: BaseException, where: str) -> str:
    """A write the client proved never left the machine (`nc.request_was_sent`)."""
    if _error_kind(exc) == "TRANSIENT":
        return f"Nothing was sent to {where}: NetSuite couldn't be reached. Trying again is safe."
    # A local error (configuration, credentials, a refused payload) fails the same
    # way on every try until someone fixes it, so this does not invite a retry.
    return (f"Nothing was sent to {where}: the tool hit an error before sending, and "
            "trying again will fail the same way until it's fixed. Let Kiko know; the "
            "details are in the write history.")


def _fmt(value: Optional[Decimal]) -> str:
    if value is None:
        return "?"
    return format(value.normalize(), "f")


def _num(value: Optional[Decimal]) -> Any:
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def _live_client() -> nc.NetSuiteClient:
    global _live
    if _live is None:
        _live = nc.NetSuiteClient(config=nc.NetSuiteConfig.from_env())
    return _live


def _utcnow() -> dt.datetime:
    """UTC, stored naive -- the database's convention (`ingest._utcnow`)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
