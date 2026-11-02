"""
The NetSuite write: one approved line, quantity only (Phase 4 decision 2).

`write_approved_quantity` is the ONE function the review app may call from the
write side. `test_review_app_never_imports_netsuite_client` pins its name and
signature and fails if the app imports anything else from here, from
`netsuite_client`, or from `matcher`/`ingest`. Not wired to Approve yet.

## What it does, per line

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
   NetSuite: SUCCESS, REFUSED (with the reason) or FAILED (with the error).
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
PATCH left. A retry is safe anyway. The re-read sees the line already at the
new value, which no longer equals the proposal's base, so it is refused with a
message saying so, rather than written twice or reported as a clean success.
The accumulation base (`ingest._line_history`) then has no SUCCESS row for it,
and the next slip on that line is flagged `DISPUTED` rather than silently
summed. That is noisy, and it is the safe direction.

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
the line changed and not to retry.

## Production

Refused unless `PO_AGENT_ALLOW_PRODUCTION_WRITES` names the production account
(`_account_refusal`). Sandbox first, always.

## Not handled

- No read-back after the PATCH. NetSuite answers 204 with no body; Phase 1
  showed the role's quantity writes are not silently discarded. A later
  discard would surface as `DISPUTED` on the next slip.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
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
#: IS changed; the accumulation base does not know. Never retried by the tool.
WRITTEN_NOT_RECORDED = "WRITTEN_NOT_RECORDED"

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
    status: str  # WRITTEN / REFUSED / FAILED / WRITTEN_NOT_RECORDED
    message: str
    previous_quantity: Optional[Decimal] = None
    new_quantity: Optional[Decimal] = None
    error_kind: Optional[str] = None
    #: None when nothing was recorded: a refusal decided from the database alone,
    #: before NetSuite was contacted.
    attempt_no: Optional[int] = None


def write_approved_quantity(
    engine, change_id: str, *, client=None, now: Optional[dt.datetime] = None
) -> LineWriteResult:
    """
    Write one approved line's quantity to NetSuite. See the module docstring.

    `client` exists for tests. The review app must not pass it (the guard test
    checks); when omitted, a live client is built from `.env`.
    """
    now = now or _utcnow()
    row = _load(engine, change_id)
    po = f"PO {row.po_number_key}"
    where = f"{po} line {row.ns_line_id}"
    new = Decimal(row.approved_quantity) if row.approved_quantity is not None else None

    refusal = _precondition_refusal(row, where)
    if refusal:
        return LineWriteResult(change_id, po, row.ns_line_id, REFUSED, refusal,
                               new_quantity=new)

    client = client or _live_client()
    refusal = _account_refusal(client, where)
    if refusal:
        return LineWriteResult(change_id, po, row.ns_line_id, REFUSED, refusal,
                               new_quantity=new)
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
    if line is None:
        return attempt.refused("OTHER", previous=None, message=(
            f"{po} no longer has a line {row.ns_line_id} in NetSuite, so nothing was written."))
    previous = _quantity_of(line)

    # 2. Refuse what is not ours to write.
    if line.closed:
        return attempt.refused("LINE_CLOSED", previous, (
            f"{where} is closed in NetSuite, so nothing was written."))
    if not line.is_open:
        return attempt.refused("LINE_CLOSED", previous, (
            f"{where} is no longer open in NetSuite (nothing is left to receive on it), "
            "so nothing was written."))
    base = Decimal(row.current_quantity) if row.current_quantity is not None else None
    if base is None:
        return attempt.refused("CONFLICT", previous, (
            f"There is no record of what {where} showed when this change was worked out, "
            "so the tool can't tell whether someone has edited it since. Nothing was written."))
    if previous != base:
        if previous == new:
            message = (
                f"{where} already shows {_fmt(previous)}, the quantity being written. An "
                "earlier attempt may have reached NetSuite even though it reported a "
                "failure, or someone entered it by hand. Nothing was written; check the "
                "line in NetSuite.")
        else:
            message = (
                f"{where} now shows {_fmt(previous)} in NetSuite, but this change was "
                f"worked out when it showed {_fmt(base)}. Someone has edited it since, so "
                "nothing was written.")
        return attempt.refused("CONFLICT", previous, message)

    # 3. Quantity, and only quantity.
    fields = _quantity_payload(new)
    _assert_quantity_only(fields)
    sent = nc.normalize_line_fields(fields)
    try:
        response = client.update_po_line(row.ns_internal_id, row.ns_line_id, fields,
                                         by_internal_id=True,
                                         idempotency_key=attempt.idempotency_key)
    except Exception as exc:  # noqa: BLE001 -- see the re-read above
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


def _load(engine, change_id: str):
    pc, sp, s = proposed_changes, shipment_pos, shipments
    with engine.connect() as conn:
        row = conn.execute(
            select(pc.c.id, pc.c.state, pc.c.ns_line_id, pc.c.current_quantity,
                   pc.c.approved_quantity, pc.c.quantity_approved_by,
                   pc.c.quantity_write_status,
                   pc.c.date_write_status, pc.c.shipment_id, sp.c.po_number_key,
                   sp.c.ns_internal_id, s.c.origin)
            .select_from(pc.join(sp, pc.c.shipment_po_id == sp.c.id)
                           .join(s, pc.c.shipment_id == s.c.id))
            .where(pc.c.id == change_id)
        ).one_or_none()
    if row is None:
        raise LookupError(f"No proposed_changes row {change_id!r}.")
    return row


def _precondition_refusal(row, where: str) -> Optional[str]:
    """A reason not to contact NetSuite at all, or None. Nothing is recorded for these."""
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

    def __init__(self, engine, row, now, po, new):
        self.engine, self.row, self.now, self.po, self.new = engine, row, now, po, new
        self.idempotency_key = str(uuid.uuid4())

    def succeeded(self, previous, sent, http_status, message) -> LineWriteResult:
        return self._record("SUCCESS", WRITTEN, previous=previous, sent=sent,
                            http_status=http_status, error_kind=None, detail=None,
                            message=message)

    def refused(self, error_kind, previous, message) -> LineWriteResult:
        return self._record("REFUSED", REFUSED, previous=previous, sent={},
                            http_status=None, error_kind=error_kind, detail=message,
                            message=message)

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
        except Exception:  # noqa: BLE001 -- the caller must get a result for every line
            logger.exception("write_attempts not recorded for change %s (%s)", row.id, outcome)
            if outcome == "SUCCESS":
                # The line IS changed. Saying so plainly matters more than anything:
                # a retry would be refused (the re-read sees the new value), and the
                # accumulation base does not know this write happened.
                status = WRITTEN_NOT_RECORDED
                message += (" NetSuite accepted the write, but the tool could not record "
                            "it. Don't try again; tell Kiko.")
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
            if outcome == "SUCCESS":
                # Always WRITTEN. A date scope can only exist on a row approved
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
    if isinstance(exc, nc.NetSuitePermissionError):
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


def _plain_error(exc: BaseException, *, after_send: bool) -> str:
    kind = _error_kind(exc)
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
