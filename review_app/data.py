"""
What the review screens read, and the two decisions they write.

Imports `schema` and nothing that can reach NetSuite. An approval here is a row
update and an audit row. The write that follows it is `write_path.write_po_batch`,
called by `app.py` -- the one function this package may take from the write side
-- and its outcome is read back here from `write_attempts` (`write_results`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select

import schema as sc
from review_app import wording
from schema import (
    attachments,
    audit_log,
    change_candidates,
    message_attachments,
    messages,
    proposed_changes,
    shipment_pos,
    shipments,
    write_attempts,
)

#: States a line can be in while it still waits on a human.
REVIEW_STATES = (
    sc.STATE_PENDING_REVIEW, sc.STATE_NO_CHANGE, sc.STATE_NEEDS_ATTENTION,
    sc.STATE_NEEDS_RESOLUTION, sc.STATE_NEEDS_ASSIGNMENT, sc.STATE_MANUAL_ENTRY_REQUIRED,
)
#: Lines needing a choice between NetSuite lines. There is still no picker (it has
#: never met real data; Requirements §3), so they cannot be approved. Since
#: 2026-11-02 they are shown and LEFT OUT of an Approve, like a blocked line --
#: the PO page no longer refuses to load.
REFUSED_STATES = (sc.STATE_NEEDS_ASSIGNMENT, sc.STATE_NEEDS_RESOLUTION)

WORKFLOW = "PACKING_SLIP"


class ReviewRefused(Exception):
    """
    Something the app will not do. `message` is what Paula reads: what happened and
    what, if anything, to do. `detail` is the technical cause, shown only under a
    collapsed Details toggle. `status` is the HTTP status.
    """

    def __init__(self, message: str, status: int = 409, *, detail: str = "",
                 title: str = "Nothing was saved"):
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.title = title


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@dataclass
class Line:
    row: dict
    #: "approve" | "accept_no_change" | "block" | "refused" | "decided"
    disposition: str
    block_reason: str = ""

    def __getattr__(self, name: str) -> Any:  # row columns read as attributes
        try:
            return self.row[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    @property
    def awaiting(self) -> bool:
        return self.row["state"] in REVIEW_STATES and self.row["human_verdict"] is None

    @property
    def no_action_needed(self) -> bool:
        """Already received in NetSuite, and ordered, received and the slip all agree
        (2026-10-21). Never shown to the reviewer and never part of a verdict: like a
        PO with no open line, it appears only on the diagnostic page."""
        return (self.row["state"] == sc.STATE_NO_CHANGE
                and self.row["accumulation_basis"] == "PRE_EXISTING_RECEIPT"
                and self.row["human_verdict"] is None)

    @property
    def flagged(self) -> bool:
        return self.row["state"] == sc.STATE_NEEDS_ATTENTION

    @property
    def slip_quantity(self) -> Optional[Decimal]:
        return _decimal(self.row["src_quantity_text"])

    @property
    def outstanding(self) -> Optional[Decimal]:
        if self.row["current_quantity"] is None:
            return None
        return Decimal(self.row["current_quantity"]) - Decimal(
            self.row["current_quantity_received"] or 0)

    @property
    def plain_reasons(self) -> list[str]:
        """What Paula reads about this line; the raw reason goes under Details."""
        return wording.reasons(self.row["attention_reason"], self.row)

    @property
    def exclusion_short(self) -> str:
        """Why Approve leaves this line out, in a few words; empty if it doesn't."""
        if self.disposition not in ("block", "refused"):
            return ""
        return wording.EXCLUSION_SHORT[wording.block_kind(self)]

    @property
    def label(self) -> str:
        parts = [self.row["src_style_text"], self.row["src_color_text"], self.row["src_size_text"]]
        return " / ".join(str(p) for p in parts if p not in (None, ""))


@dataclass
class Summary:
    """§4: ordered versus shipped, for the lines on this slip."""

    lines_on_slip: int
    shipped_total: Decimal
    ordered_on_matched: Decimal
    shipped_on_matched: Decimal
    matched_lines: int
    over: int
    under: int
    equal: int

    @property
    def variance(self) -> Optional[Decimal]:
        return self.shipped_on_matched - self.ordered_on_matched if self.matched_lines else None

    @property
    def variance_pct(self) -> Optional[float]:
        if not self.matched_lines or not self.ordered_on_matched:
            return None
        return float((self.shipped_on_matched - self.ordered_on_matched) / self.ordered_on_matched)

    @property
    def direction(self) -> str:
        if not self.matched_lines:
            return "no line matched a NetSuite line, so nothing to compare"
        if self.over and not self.under:
            return f"one-sided: OVER on every differing line ({self.over} over, {self.equal} equal)"
        if self.under and not self.over:
            return f"one-sided: UNDER on every differing line ({self.under} under, {self.equal} equal)"
        if not self.over and not self.under:
            return "every matched line ships exactly its ordered quantity"
        return f"mixed: {self.over} over, {self.under} under, {self.equal} equal"


@dataclass
class PoView:
    shipment_po: dict
    shipment: dict
    message: Optional[dict]
    lines: list[Line]
    warnings: list[str] = field(default_factory=list)
    #: Whether any CANDIDATE line (`change_candidates`) is open. A line with no
    #: chosen target keeps its open state there, not on the row (Requirements §2).
    candidate_open: bool = False
    #: The shipment's SHIPMENT_ROWS_LOST audit detail, if it lost over a third of
    #: its lines. Structured, so the message is generated, not parsed from prose.
    rows_lost: Optional[dict] = None

    @property
    def rows_lost_message(self) -> Optional[str]:
        return wording.rows_lost_message(self.rows_lost)

    @property
    def block_explanations(self) -> list[tuple[str, int]]:
        return wording.block_explanations(self.excluded)

    @property
    def summary_sentence(self) -> str:
        return wording.summary_sentence(self.summary)

    @property
    def id(self) -> str:
        return self.shipment_po["id"]

    @property
    def refused_lines(self) -> list[Line]:
        return [ln for ln in self.lines if ln.disposition == "refused"]

    @property
    def shown_lines(self) -> list[Line]:
        """Every line the reviewer sees and decides on: all but `no_action_lines`."""
        return [ln for ln in self.lines if not ln.no_action_needed]

    @property
    def no_action_lines(self) -> list[Line]:
        """Already received and agreeing: listed on the diagnostic page only."""
        return [ln for ln in self.lines if ln.no_action_needed]

    @property
    def awaiting(self) -> list[Line]:
        return [ln for ln in self.shown_lines if ln.awaiting]

    @property
    def blocking(self) -> list[Line]:
        return [ln for ln in self.lines if ln.disposition == "block"]

    @property
    def writable(self) -> list[Line]:
        """The covered lines an Approve actually sends to NetSuite (not the
        no-change lines, which it only accepts)."""
        return [ln for ln in self.covered if ln.disposition == "approve"]

    @property
    def unwritten(self) -> list[Line]:
        """Approved lines not written to NetSuite yet: failed, refused, or never
        sent. They keep the PO in the queue until written or left as they are --
        a failed write must not drop out of sight (partial-failure ruling)."""
        return [ln for ln in self.lines
                if ln.row["state"] in (sc.STATE_APPROVED, sc.STATE_WRITE_FAILED)
                and ln.row["quantity_write_status"] in ("APPROVED", "FAILED")]

    @property
    def excluded(self) -> list[Line]:
        """Lines an Approve leaves out: blocked, or needing a choice between NetSuite
        lines. Shown and listed, never written, and left undecided -- so the PO stays
        in the queue for them until they are rejected or fixed in NetSuite."""
        return [ln for ln in self.shown_lines if ln.disposition in ("block", "refused")]

    @property
    def covered(self) -> list[Line]:
        """The lines an Approve records a verdict on. Never a no-action line."""
        return [ln for ln in self.shown_lines if ln.disposition in ("approve", "accept_no_change")]

    @property
    def flagged_covered(self) -> int:
        return sum(1 for ln in self.covered if ln.flagged)

    @property
    def can_approve(self) -> bool:
        """Anything to approve. Excluded lines no longer disable it (2026-11-02):
        they are left out of the write and listed. Still per PO, no per-line ticks."""
        return bool(self.covered)

    @property
    def can_reject(self) -> bool:
        return bool(self.awaiting)

    @property
    def changing(self) -> list[Line]:
        """Covered lines whose NetSuite quantity Approve actually changes. A line
        approved at the figure NetSuite already holds is confirmed, not sent, and changes
        nothing, so it is counted with the lines that stay as they are."""
        return [ln for ln in self.writable
                if _decimal(ln.row["proposed_quantity"]) != _decimal(ln.row["current_quantity"])]

    @property
    def approve_label(self) -> str:
        """Changes and acceptances counted SEPARATELY (2026-11-04): one total
        implied every covered line changes in NetSuite, which it does not."""
        c = len(self.changing)
        k, f, x = len(self.covered) - c, self.flagged_covered, len(self.excluded)
        parts = []
        if c:
            parts.append(f"{wording.lines_word(c)} change{'s' if c == 1 else ''} in NetSuite")
        if k:
            stays = "stays as it is" if k == 1 else "stay as they are"
            parts.append(f"{wording.lines_word(k) if not c else k} {stays}")
        return ("Approve: " + ", ".join(parts) + (f" ({f} flagged)" if f else "")
                + (f" — {x} can't be changed" if x else ""))

    @property
    def no_action_possible(self) -> bool:
        """
        No line of this PO has an OPEN matched NetSuite line -- counting candidate
        lines too. NEEDS_RESOLUTION has no target (so `ns_line_is_open` is NULL)
        but two or more OPEN candidates; reading the row alone parked it as closed.

        Also true when nothing the reviewer would see is still waiting: a PO whose
        only awaiting lines need no action (already received, figures agree) has
        nothing to decide, and goes to the diagnostic page like a closed one.

        Never true while an approved line is unwritten: that PO has something to
        do (send again, or leave as it is), and belongs in the queue.
        """
        if self.unwritten:
            return False
        shown = self.shown_lines
        nothing_open = not self.candidate_open and not any(ln.row["ns_line_is_open"]
                                                           for ln in shown)
        return nothing_open or not any(ln.awaiting for ln in shown)

    @property
    def flagged_lines(self) -> list[Line]:
        return [ln for ln in self.shown_lines if ln.flagged]

    @property
    def other_lines(self) -> list[Line]:
        return [ln for ln in self.shown_lines if not ln.flagged]

    @property
    def summary(self) -> Summary:
        with_slip = [ln for ln in self.lines if ln.slip_quantity is not None]
        matched = [ln for ln in with_slip if ln.row["current_quantity"] is not None]
        over = sum(1 for ln in matched if ln.slip_quantity > Decimal(ln.row["current_quantity"]))
        under = sum(1 for ln in matched if ln.slip_quantity < Decimal(ln.row["current_quantity"]))
        return Summary(
            lines_on_slip=len(with_slip),
            shipped_total=sum((ln.slip_quantity for ln in with_slip), Decimal(0)),
            ordered_on_matched=sum((Decimal(ln.row["current_quantity"]) for ln in matched),
                                   Decimal(0)),
            shipped_on_matched=sum((ln.slip_quantity for ln in matched), Decimal(0)),
            matched_lines=len(matched), over=over, under=under,
            equal=len(matched) - over - under,
        )

    @property
    def fingerprint(self) -> str:
        """Changes whenever any line's state or verdict does -- a stale form is refused."""
        parts = sorted(f"{ln.row['id']}:{ln.row['state']}:{ln.row['human_verdict']}"
                       for ln in self.lines)
        return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _decimal(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value).strip())
    except Exception:  # noqa: BLE001 -- unparseable slip text simply has no figure
        return None


def _disposition(row: dict) -> tuple[str, str]:
    state = row["state"]
    if row["human_verdict"] is not None or state not in REVIEW_STATES:
        return "decided", ""
    if state in REFUSED_STATES:
        return "refused", ""
    if state == sc.STATE_NO_CHANGE:
        return "accept_no_change", ""
    if state == sc.STATE_MANUAL_ENTRY_REQUIRED:
        return "block", "no acceptable source document -- keyed by hand; reject the PO to close it"
    # PENDING_REVIEW or NEEDS_ATTENTION: approvable only with an open target line
    # and a quantity to approve.
    if row["ns_line_id"] is None:
        return "block", "no NetSuite line to write to (no open line matched)"
    if not row["ns_line_is_open"]:
        return "block", "the matched NetSuite line is not open"
    if row["proposed_quantity"] is None:
        basis = row["accumulation_basis"]
        if basis == "PRE_EXISTING_RECEIPT":
            return "block", ("PRE_EXISTING_RECEIPT: already received, and ordered or the slip "
                             "disagrees with received -- nothing proposed, by design")
        if basis == "DISPUTED":
            return "block", "DISPUTED: nothing proposed -- NetSuite and this tool's record disagree"
        if wording.is_below_ordered(row):
            return "block", ("below ordered: nothing proposed -- the slip would lower the "
                             "line's ordered quantity, which the tool never does (2026-11-09)")
        return "block", "no quantity proposed"
    return "approve", ""


def _lines(conn, shipment_po_id: str) -> list[Line]:
    rows = conn.execute(
        select(proposed_changes).where(proposed_changes.c.shipment_po_id == shipment_po_id)
        .order_by(proposed_changes.c.key_style, proposed_changes.c.key_color,
                  proposed_changes.c.key_size, proposed_changes.c.key_recap_label)
    ).mappings().all()
    out = []
    for row in rows:
        disposition, reason = _disposition(dict(row))
        out.append(Line(dict(row), disposition, reason))
    return out


def load_po(conn, shipment_po_id: str) -> Optional[PoView]:
    po = conn.execute(select(shipment_pos).where(shipment_pos.c.id == shipment_po_id)).mappings().first()
    if po is None:
        return None
    ship = conn.execute(select(shipments).where(shipments.c.id == po["shipment_id"])).mappings().one()
    message = None
    if ship["message_id"]:
        message = conn.execute(select(messages).where(messages.c.id == ship["message_id"])).mappings().first()
    warnings = json.loads(ship["parse_warnings_json"] or "[]")
    candidate_open = conn.execute(
        select(change_candidates.c.id)
        .join(proposed_changes, change_candidates.c.change_id == proposed_changes.c.id)
        .where(proposed_changes.c.shipment_po_id == shipment_po_id)
        # `== True`, not `.is_(True)`: that renders `IS 1`, which SQLite accepts
        # and SQL Server rejects ("Incorrect syntax near '1'"). Every PO page
        # failed on SQL Server until 2026-10-12, unseen: this app's tests built
        # their own SQLite engine.
        .where(change_candidates.c.is_open == True)  # noqa: E712
        .limit(1)
    ).first() is not None
    lost = conn.execute(
        select(audit_log.c.detail_json)
        .where(audit_log.c.shipment_id == ship["id"])
        .where(audit_log.c.event == "SHIPMENT_ROWS_LOST")
    ).scalar()
    return PoView(dict(po), dict(ship), dict(message) if message else None,
                  _lines(conn, shipment_po_id), warnings, candidate_open,
                  json.loads(lost) if lost else None)


@dataclass
class QueueEntry:
    view: PoView

    @property
    def escalated(self) -> bool:
        return self.view.rows_lost is not None

    @property
    def status(self) -> str:
        """One plain line for the queue: what reviewing this PO involves."""
        v = self.view
        unwritten = (f"{wording.lines_word(len(v.unwritten))} approved but not written to "
                     "NetSuite" if v.unwritten else "")
        if not v.can_approve:
            return unwritten or "Nothing to approve (open to see why)"
        flagged, excluded = v.flagged_covered, len(v.excluded)
        base = f"{wording.lines_word(len(v.covered))} to approve"
        return ((f"{unwritten}; " if unwritten else "") + base
                + (f", {flagged} to check first" if flagged else "")
                + (f", {excluded} can't be changed" if excluded else ""))


def list_queue(conn) -> list[QueueEntry]:
    """Every PO-in-a-shipment with a line still awaiting a human -- or an approved
    line not yet written to NetSuite, which needs one too."""
    pc = proposed_changes
    ids = set(conn.execute(
        select(pc.c.shipment_po_id).distinct()
        .where(pc.c.state.in_(REVIEW_STATES))
        .where(pc.c.human_verdict.is_(None))
    ).scalars().all())
    ids |= set(conn.execute(
        select(pc.c.shipment_po_id).distinct()
        .where(pc.c.state.in_((sc.STATE_APPROVED, sc.STATE_WRITE_FAILED)))
        .where(pc.c.quantity_write_status.in_(("APPROVED", "FAILED")))
    ).scalars().all())
    views = [load_po(conn, i) for i in ids]
    entries = [QueueEntry(v) for v in views if v is not None]
    return sorted(entries, key=lambda e: (
        str((e.view.message or {}).get("received_at") or ""), e.view.shipment_po["po_number_key"]))


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def _audit(conn, *, now, reviewer, event, line: Line, to_state, detail):
    conn.execute(audit_log.insert(), {
        "id": sc.new_id(), "occurred_at": now, "workflow": WORKFLOW,
        "actor": reviewer, "actor_kind": "HUMAN", "event": event,
        "message_id": None, "shipment_id": line.row["shipment_id"],
        "change_id": line.row["id"], "from_state": line.row["state"], "to_state": to_state,
        "detail_json": json.dumps(detail, default=str),
    })


def _load_for_decision(conn, shipment_po_id: str, fingerprint: str) -> PoView:
    view = load_po(conn, shipment_po_id)
    if view is None:
        raise ReviewRefused("This PO isn't in your review list.", 404,
                            detail=f"no shipment_pos row {shipment_po_id!r}")
    if fingerprint != view.fingerprint:
        raise ReviewRefused(
            "Something on this PO changed since you opened it, so nothing was saved. "
            "Reload the page and decide again.", 409,
            detail="state fingerprint mismatch: a line's state or verdict moved")
    return view


def approve(engine, shipment_po_id: str, *, reviewer: str, fingerprint: str,
            now: dt.datetime) -> dict:
    """
    Record Paula's approval of one PO: states and verdicts, nothing else. The
    caller (`app.py`) then writes the approved quantities to NetSuite. Every
    covered line gets a verdict (Requirements §1), including NO_CHANGE lines
    accepted as they are. Excluded lines (`PoView.excluded`) get nothing: they are
    not approved, not written, and stay awaiting a decision.

    **Quantity only** (2026-11-02). A line with a quantity to approve gets the
    QUANTITY scope and goes APPROVED; a NO_CHANGE line gets its verdict and keeps
    NO_CHANGE. There is no receipt date: this tool writes none, and approving
    one used to leave lines APPROVED with nothing a write could ever send. So a
    line reaches APPROVED here only with `approved_quantity` set.
    """
    counts = {"approved": 0, "accepted_unchanged": 0}
    with engine.begin() as conn:
        view = _load_for_decision(conn, shipment_po_id, fingerprint)
        if not view.can_approve:
            raise ReviewRefused(
                "There's nothing on this PO to approve. Reload the page to see why.", 409,
                detail="; ".join(f"{ln.label}: {ln.block_reason or ln.state}"
                                 for ln in view.excluded) or "nothing to approve")
        for line in view.covered:
            row = line.row
            values: dict[str, Any] = {"human_verdict": "ACCEPTED", "verdict_by": reviewer,
                                      "verdict_at": now, "updated_at": now}
            scopes = []
            if line.disposition == "approve":
                # ALWAYS the quantity scope, even when the figure already matches
                # NetSuite. A flagged (NEEDS_ATTENTION) line can carry a correct
                # number; approving it without a scope left it NEEDS_ATTENTION with a
                # verdict -- off the queue, never approved. APPROVED means "a human
                # approved at least one scope", so this line gets one.
                values.update(approved_quantity=row["proposed_quantity"],
                              quantity_approved_by=reviewer, quantity_approved_at=now,
                              quantity_write_status="APPROVED")
                scopes.append("QUANTITY")
            to_state = sc.STATE_APPROVED if scopes else row["state"]
            if to_state != row["state"]:
                sc.assert_transition(conn, row["state"], to_state)
                values["state"] = to_state
            conn.execute(proposed_changes.update().where(proposed_changes.c.id == row["id"])
                         .values(**values))
            _audit(conn, now=now, reviewer=reviewer, event="REVIEW_APPROVED", line=line,
                   to_state=to_state, detail={
                       "scopes": scopes, "approved_quantity": values.get("approved_quantity"),
                       "flagged": line.flagged,
                       "attention_reason": row["attention_reason"],
                   })
            counts["approved" if scopes else "accepted_unchanged"] += 1
    return counts


def clean_reject_reason(reason: str) -> str:
    """The reason as it will be stored, or a refusal. Checked at BOTH steps of a
    Reject, so a blank reason is refused before the confirmation is shown."""
    reason = (reason or "").strip()
    if not reason:
        raise ReviewRefused("Please say why you're rejecting this PO, then try again.", 400)
    if len(reason) > 1000:
        raise ReviewRefused("Please keep the reason under 1,000 characters.", 400)
    return reason


def reject_preview(conn, shipment_po_id: str, *, reason: str,
                   fingerprint: str) -> tuple[PoView, str, str]:
    """
    What a Reject WOULD cover, for the confirmation step (Kiko, 2026-11-09).
    Records nothing. Returns `(view, sentence, cleaned_reason)`.

    The same lines `reject` discards (`view.awaiting`), and the same "would have
    changed NetSuite" count the Approve button uses (`view.changing`), so the
    confirmation cannot disagree with either. The fingerprint is checked here AND
    again by `reject`, so a PO that moves between the two steps is refused.
    """
    reason = clean_reject_reason(reason)
    view = _load_for_decision(conn, shipment_po_id, fingerprint)
    if not view.can_reject:
        raise ReviewRefused("This PO has already been decided. Nothing to do.", 409)
    awaiting_ids = {ln.row["id"] for ln in view.awaiting}
    changing = sum(1 for ln in view.changing if ln.row["id"] in awaiting_ids)
    sentence = wording.reject_confirmation(
        len(view.awaiting), changing, every_line=len(view.awaiting) == len(view.shown_lines))
    return view, sentence, reason


def reject(engine, shipment_po_id: str, *, reviewer: str, reason: str, fingerprint: str,
           now: dt.datetime) -> int:
    """Discard every awaiting line of one PO, with the reason on each line and in the audit.
    Reached only through the confirmation step (`reject_preview`) in the app."""
    reason = clean_reject_reason(reason)
    with engine.begin() as conn:
        view = _load_for_decision(conn, shipment_po_id, fingerprint)
        if not view.can_reject:
            raise ReviewRefused("This PO has already been decided. Nothing to do.", 409)
        for line in view.awaiting:
            sc.assert_transition(conn, line.row["state"], sc.STATE_DISCARDED)
            conn.execute(proposed_changes.update().where(proposed_changes.c.id == line.row["id"])
                         .values(state=sc.STATE_DISCARDED, human_verdict="REJECTED",
                                 human_verdict_note=reason, verdict_by=reviewer,
                                 verdict_at=now, updated_at=now))
            _audit(conn, now=now, reviewer=reviewer, event="REVIEW_REJECTED", line=line,
                   to_state=sc.STATE_DISCARDED, detail={"reason": reason})
        return len(view.awaiting)


# ---------------------------------------------------------------------------
# What the write did: the result page, rebuilt from the database every time
# ---------------------------------------------------------------------------

#: `write_attempts.outcome` -> the result page's group. ALREADY_PRESENT is its own
#: group, never "written": the tool cannot prove it made that change. CONFIRMED is
#: its own group too: nothing was sent, so it is never a write (2026-11-04).
OUTCOME_GROUP = {"SUCCESS": "written", "ALREADY_PRESENT": "already_present",
                 "CONFIRMED": "confirmed", "REFUSED": "refused", "FAILED": "failed"}

#: Outcomes that settle a line. A WRITTEN line is shown by one of these.
SETTLING_OUTCOMES = ("SUCCESS", "ALREADY_PRESENT", "CONFIRMED")

#: The groups in the order the result page shows them, with their headings.
RESULT_GROUPS = (
    ("written", "Written to NetSuite"),
    ("already_present", "Already showing the new quantity"),
    ("confirmed", "Confirmed: NetSuite already had the approved figure"),
    ("refused", "Refused, nothing written"),
    ("failed", "Not written: something went wrong"),
    ("not_sent", "Approved, not sent to NetSuite yet"),
    ("excluded", "Left as they are: can't be changed here"),
    ("given_up", "Left as they are: you stopped trying"),
    ("no_change", "No change needed"),
    ("rejected", "Rejected"),
)

#: Shown when an attempt's own sentence is missing. Never the raw error: that is
#: in the write history, behind the scenes.
NO_MESSAGE = "Not written. The details are in the tool's write history."


@dataclass
class ResultLine:
    line: Line
    group: str  # a RESULT_GROUPS key
    previous: Optional[Decimal]  # what NetSuite held just before the write
    new: Optional[Decimal]  # the approved quantity
    message: str  # plain English
    attempted_at: Optional[dt.datetime] = None

    @property
    def arrow(self) -> str:
        """"line 18: 44 → 52" -- the previous value is what a reversal goes back to."""
        prev = "?" if self.previous is None else _fmt_qty(self.previous)
        new = "?" if self.new is None else _fmt_qty(self.new)
        return f"line {self.line.ns_line_id}: {prev} → {new}"


def _fmt_qty(value) -> str:
    return format(Decimal(value).normalize(), "f")


def write_results(conn, shipment_po_id: str) -> list:
    """
    Every line the reviewer sees, with what the write did to it, from the LATEST
    `write_attempts` row of each line -- so the page survives a refresh and stays
    reachable from the PO. Why an approved line was not sent at all (refused before
    NetSuite was contacted) comes from its latest `QUANTITY_WRITE_NOT_SENT` audit
    event, so it survives a restart too (2026-11-04; it used to be held in memory).
    """
    view = load_po(conn, shipment_po_id)
    if view is None:
        return []
    ids = [ln.row["id"] for ln in view.shown_lines]
    written = {ln.row["id"] for ln in view.shown_lines if ln.row["state"] == sc.STATE_WRITTEN}
    latest: dict = {}
    if ids:
        for row in conn.execute(
            select(write_attempts).where(write_attempts.c.change_id.in_(ids))
            .where(write_attempts.c.scope == "QUANTITY")
        ).mappings():
            # A WRITTEN line is shown by the attempt that settled it. A later
            # attempt can exist -- a second tab or a double submit racing the
            # first, refused as "already shows" -- and `write_path` keeps the line
            # WRITTEN; taking that newest row showed a written line as refused.
            if row["change_id"] in written and row["outcome"] not in SETTLING_OUTCOMES:
                continue
            if row["attempt_no"] > latest.get(row["change_id"], {}).get("attempt_no", 0):
                latest[row["change_id"]] = dict(row)
    messages_by = _attempt_messages(conn, ids)
    not_sent = _not_sent_reasons(conn, ids)
    out = []
    for ln in view.shown_lines:
        attempt = latest.get(ln.row["id"])
        new = ln.row["approved_quantity"]
        reason = not_sent.get(ln.row["id"])
        unwritten = (ln.row["state"] in (sc.STATE_APPROVED, sc.STATE_WRITE_FAILED)
                     and ln.row["quantity_write_status"] in ("APPROVED", "FAILED"))
        if (attempt is not None and unwritten and reason is not None
                and reason[1] > attempt["attempted_at"]):
            # Refused before contact AFTER its last attempt (production writes
            # switched off since, say): the newer reason is the one that applies.
            out.append(ResultLine(ln, "not_sent", ln.row["current_quantity"], new, reason[0]))
        elif attempt is not None:
            # "Previous" is what a reversal goes back to. For ALREADY_PRESENT the
            # attempt re-read the NEW value (that is the point of it), so the
            # before is what the line held when the proposal was made.
            previous = (ln.row["current_quantity"] if attempt["outcome"] == "ALREADY_PRESENT"
                        else attempt["previous_quantity"])
            group = ("given_up" if ln.row["state"] == sc.STATE_DISCARDED
                     else OUTCOME_GROUP[attempt["outcome"]])
            out.append(ResultLine(
                ln, group, previous, new,
                messages_by.get((ln.row["id"], attempt["attempt_no"])) or NO_MESSAGE,
                attempt["attempted_at"]))
        elif ln.disposition in ("block", "refused"):
            out.append(ResultLine(ln, "excluded", ln.row["current_quantity"], None,
                                  ln.exclusion_short))
        elif (ln.row["state"] == sc.STATE_DISCARDED and ln.row["human_verdict"] == "ACCEPTED"
              and ln.row["quantity_write_status"] in ("APPROVED", "FAILED")):
            # Given up with NO attempt row: refused before NetSuite was contacted
            # (no connection, production writes off), then left as it is. Checked
            # before "not sent", which would otherwise claim it is still waiting.
            out.append(ResultLine(ln, "given_up", ln.row["current_quantity"], new,
                                  "Approved, then left as it is. Nothing was sent to "
                                  "NetSuite for it."))
        elif ln.row["quantity_write_status"] in ("APPROVED", "FAILED"):
            out.append(ResultLine(ln, "not_sent", ln.row["current_quantity"], new,
                                  reason[0] if reason is not None
                                  else "Approved, but not sent to NetSuite yet."))
        elif ln.row["human_verdict"] == "REJECTED":
            out.append(ResultLine(ln, "rejected", ln.row["current_quantity"], None,
                                  ln.row["human_verdict_note"] or ""))
        elif ln.row["human_verdict"]:
            out.append(ResultLine(ln, "no_change", ln.row["current_quantity"], None,
                                  "Already correct in NetSuite; nothing to write."))
    # In NetSuite line order ("line 18: 44 → 52"), which is how she finds them
    # there; lines with no NetSuite line last.
    out.sort(key=lambda r: (r.line.ns_line_id is None,
                            int(r.line.ns_line_id) if str(r.line.ns_line_id or "").isdigit()
                            else 0, str(r.line.ns_line_id or "")))
    return out


#: Literal, not imported: the review app may take `write_po_batch` and nothing else
#: from the write side (`test_review_app_never_imports_netsuite_client`).
NOT_SENT_EVENT = "QUANTITY_WRITE_NOT_SENT"


def _not_sent_reasons(conn, change_ids: list) -> dict:
    """change id -> (message, when) of its LATEST not-sent event: a refusal decided
    before NetSuite was contacted, which leaves no `write_attempts` row."""
    if not change_ids:
        return {}
    out: dict = {}
    for row in conn.execute(
        select(audit_log.c.change_id, audit_log.c.occurred_at, audit_log.c.detail_json)
        .where(audit_log.c.change_id.in_(change_ids))
        .where(audit_log.c.event == NOT_SENT_EVENT)
        .order_by(audit_log.c.occurred_at)
    ):
        try:
            message = json.loads(row.detail_json or "{}").get("message") or ""
        except ValueError:
            continue
        if message:
            out[row.change_id] = (message, row.occurred_at)  # ascending: last wins
    return out


def _attempt_messages(conn, change_ids: list) -> dict:
    """(change id, attempt no) -> the plain message `write_path` recorded for it."""
    if not change_ids:
        return {}
    out = {}
    for row in conn.execute(
        select(audit_log.c.change_id, audit_log.c.detail_json)
        .where(audit_log.c.change_id.in_(change_ids))
        .where(audit_log.c.event.like("QUANTITY_WRITE_%"))
    ):
        try:
            detail = json.loads(row.detail_json or "{}")
        except ValueError:
            continue
        if detail.get("attempt_no") is not None:
            out[(row.change_id, detail["attempt_no"])] = detail.get("message") or ""
    return out


def has_write_results(conn, shipment_po_id: str) -> bool:
    """Whether this PO has anything for the result page: an attempt, or an approved
    line not yet sent."""
    return conn.execute(
        select(proposed_changes.c.id)
        .where(proposed_changes.c.shipment_po_id == shipment_po_id)
        .where(proposed_changes.c.quantity_write_status != "NONE")
        .limit(1)
    ).first() is not None


def _unwritten(conn, shipment_po_id: str) -> list:
    """(change id, latest attempt outcome or None) for each approved, unwritten line."""
    pc, wa = proposed_changes, write_attempts
    rows = conn.execute(
        select(pc.c.id).where(pc.c.shipment_po_id == shipment_po_id)
        .where(pc.c.state.in_((sc.STATE_APPROVED, sc.STATE_WRITE_FAILED)))
        .where(pc.c.quantity_write_status.in_(("APPROVED", "FAILED")))
    ).scalars().all()
    out = []
    for change_id in rows:
        latest = conn.execute(
            select(wa.c.outcome).where(wa.c.change_id == change_id)
            .where(wa.c.scope == "QUANTITY").order_by(wa.c.attempt_no.desc()).limit(1)
        ).scalar()
        out.append((change_id, latest))
    return out


def retryable_count(conn, shipment_po_id: str) -> int:
    """Lines worth sending again: unwritten, and NOT refused. A refused line --
    closed, edited in NetSuite, or rejected by NetSuite -- is refused again on
    every try; it needs a person, and `give_up` is the way out for it."""
    return sum(1 for _cid, outcome in _unwritten(conn, shipment_po_id) if outcome != "REFUSED")


def unwritten_count(conn, shipment_po_id: str) -> int:
    return len(_unwritten(conn, shipment_po_id))


def give_up(engine, shipment_po_id: str, *, reviewer: str, now: dt.datetime) -> int:
    """
    Leave every approved-but-unwritten line of one PO as it is in NetSuite: the
    reviewer's decision to stop trying (WRITE_FAILED / APPROVED -> DISCARDED, both
    legal HUMAN transitions). The line's attempts stay as history; nothing is sent.
    Without this, a line that can never be written -- closed, edited elsewhere --
    would keep its PO in the queue for good.
    """
    pc = proposed_changes
    count = 0
    with engine.begin() as conn:
        for change_id, _outcome in _unwritten(conn, shipment_po_id):
            row = conn.execute(select(pc).where(pc.c.id == change_id)).mappings().one()
            sc.assert_transition(conn, row["state"], sc.STATE_DISCARDED)
            conn.execute(pc.update().where(pc.c.id == change_id)
                         .values(state=sc.STATE_DISCARDED, updated_at=now))
            conn.execute(audit_log.insert(), {
                "id": sc.new_id(), "occurred_at": now, "workflow": WORKFLOW,
                "actor": reviewer, "actor_kind": "HUMAN", "event": "WRITE_GIVEN_UP",
                "message_id": None, "shipment_id": row["shipment_id"], "change_id": change_id,
                "from_state": row["state"], "to_state": sc.STATE_DISCARDED,
                "detail_json": json.dumps({"approved_quantity": row["approved_quantity"]},
                                          default=str),
            })
            count += 1
    return count


# ---------------------------------------------------------------------------
# Source documents
# ---------------------------------------------------------------------------

_UNSAFE = re.compile(r'[\x00-\x1f\x7f"\\;]')


def safe_filename(name: Any) -> str:
    """
    A vendor-supplied filename made safe for a response header.

    The filename is attacker-controlled -- it came in on an email -- and it goes
    into `Content-Disposition`. So: basename only (both separators), every control
    character removed (CR and LF are what make header injection possible), quotes,
    backslashes and semicolons removed, length capped. Empty becomes "document".
    """
    text = str(name or "")
    text = re.split(r"[\\/]", text)[-1]
    text = _UNSAFE.sub("", text)
    text = " ".join(text.split()).strip(" .")[:150]
    return text or "document"


def content_disposition(name: Any) -> str:
    safe = safe_filename(name)
    # Only non-ASCII characters become "_" in the fallback; the full name travels,
    # percent-encoded, in filename*.
    ascii_name = "".join(ch if ord(ch) < 128 else "_" for ch in safe)
    return (f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{urllib.parse.quote(safe, safe='')}")


def resolve_source(conn, change_id: str, blob_root: Path) -> tuple[Path, str]:
    """
    The file behind ONE proposal, found through that proposal's `source_sha256`
    and nothing else. Raises ReviewRefused (404/403/409) for every other case.

    The hash is checked here and the file is streamed afterwards, so bytes swapped
    in between would be served. Acceptable for a local, single-operator tool whose
    store has no concurrent writer; revisit if the route is ever reachable from more
    than one process. The same bytes attached under several names get the
    alphabetically first name -- display only; the hash is what is verified.
    """
    sha = conn.execute(select(proposed_changes.c.source_sha256)
                       .where(proposed_changes.c.id == change_id)).scalar()
    if not sha:
        raise ReviewRefused("There's no document on file for this line.", 404,
                            title="Can't open the document",
                            detail="proposed_changes.source_sha256 is NULL")
    stored = conn.execute(select(attachments.c.stored_uri)
                          .where(attachments.c.content_sha256 == sha)).scalar()
    if not stored:
        raise ReviewRefused("The document for this line can't be found. Let Kiko know.", 404,
                            title="Can't open the document",
                            detail=f"no attachments.stored_uri for {sha}")
    path = Path(stored).resolve()
    root = Path(blob_root).resolve()
    if not path.is_relative_to(root):
        raise ReviewRefused("This document can't be opened. Let Kiko know.", 403,
                            title="Can't open the document",
                            detail=f"stored path {path} is outside the blob root {root}")
    if not path.is_file():
        raise ReviewRefused("The document for this line can't be found. Let Kiko know.", 404,
                            title="Can't open the document",
                            detail=f"{path} does not exist")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != sha:
        raise ReviewRefused(
            "This document doesn't match the one that was received, so it won't be "
            "opened. Let Kiko know.", 409, title="Can't open the document",
            detail=f"sha256 of {path} is not the recorded {sha}")
    name = conn.execute(select(message_attachments.c.filename)
                        .where(message_attachments.c.content_sha256 == sha)
                        .order_by(message_attachments.c.filename)).scalar()
    return path, name or path.name
