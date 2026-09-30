"""
What the review screens read, and the two decisions they write.

Imports `schema` and nothing that can reach NetSuite. An approval here is a row
update and an audit row; the write path that would act on it does not exist in
this package.
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
)

#: States a line can be in while it still waits on a human.
REVIEW_STATES = (
    sc.STATE_PENDING_REVIEW, sc.STATE_NO_CHANGE, sc.STATE_NEEDS_ATTENTION,
    sc.STATE_NEEDS_RESOLUTION, sc.STATE_NEEDS_ASSIGNMENT, sc.STATE_MANUAL_ENTRY_REQUIRED,
)
#: v1 does not render these: both need a picker that has never met real data
#: (Requirements §3). A PO holding one is REFUSED, never shown with the lines missing.
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
    def override_protected(self) -> bool:
        """A person already overrode this line's receipt date (2026-09-30 guard)."""
        return bool(self.row["current_override_flag"] or self.row["current_updated_receipt_date"])

    @property
    def plain_reasons(self) -> list[str]:
        """What Paula reads about this line; the raw reason goes under Details."""
        return wording.reasons(self.row["attention_reason"])

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
        return wording.block_explanations(self.blocking)

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
    def awaiting(self) -> list[Line]:
        return [ln for ln in self.lines if ln.awaiting]

    @property
    def blocking(self) -> list[Line]:
        return [ln for ln in self.lines if ln.disposition == "block"]

    @property
    def covered(self) -> list[Line]:
        """The lines an Approve records a verdict on."""
        return [ln for ln in self.lines if ln.disposition in ("approve", "accept_no_change")]

    @property
    def flagged_covered(self) -> int:
        return sum(1 for ln in self.covered if ln.flagged)

    @property
    def can_approve(self) -> bool:
        return not self.refused_lines and not self.blocking and bool(self.covered)

    @property
    def can_reject(self) -> bool:
        return not self.refused_lines and bool(self.awaiting)

    @property
    def approve_label(self) -> str:
        n, f = len(self.covered), self.flagged_covered
        return f"Approve {n} line{'s' if n != 1 else ''}" + (f" ({f} flagged)" if f else "")

    @property
    def no_action_possible(self) -> bool:
        """
        No line of this PO has an OPEN matched NetSuite line -- counting candidate
        lines too. NEEDS_RESOLUTION has no target (so `ns_line_is_open` is NULL)
        but two or more OPEN candidates; reading the row alone parked it as closed.
        """
        return not self.candidate_open and not any(ln.row["ns_line_is_open"]
                                                   for ln in self.lines)

    @property
    def flagged_lines(self) -> list[Line]:
        return [ln for ln in self.lines if ln.flagged]

    @property
    def other_lines(self) -> list[Line]:
        return [ln for ln in self.lines if not ln.flagged]

    @property
    def override_protected_covered(self) -> list[Line]:
        return [ln for ln in self.covered if ln.override_protected]

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
            return "block", ("needs the line's total confirmed once (PRE_EXISTING_RECEIPT); "
                             "that input is not in v1")
        if basis == "DISPUTED":
            return "block", "DISPUTED: nothing proposed -- NetSuite and this tool's record disagree"
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
        .where(change_candidates.c.is_open.is_(True))
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
        if v.refused_lines:
            return "Can't be reviewed here yet"
        if not v.can_approve:
            return "Nothing to approve yet (open to see why)"
        flagged = v.flagged_covered
        base = f"{wording.lines_word(len(v.covered))} to approve"
        return base + (f", {flagged} to check first" if flagged else "")


def list_queue(conn) -> list[QueueEntry]:
    """Every PO-in-a-shipment with at least one line still awaiting a human."""
    ids = conn.execute(
        select(proposed_changes.c.shipment_po_id).distinct()
        .where(proposed_changes.c.state.in_(REVIEW_STATES))
        .where(proposed_changes.c.human_verdict.is_(None))
    ).scalars().all()
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
    if view.refused_lines:
        raise ReviewRefused(
            "This PO can't be reviewed here yet. Let Kiko know.", 409,
            detail="it has NEEDS_ASSIGNMENT / NEEDS_RESOLUTION lines, which v1 refuses")
    if fingerprint != view.fingerprint:
        raise ReviewRefused(
            "Something on this PO changed since you opened it, so nothing was saved. "
            "Reload the page and decide again.", 409,
            detail="state fingerprint mismatch: a line's state or verdict moved")
    return view


def parse_receipt_date(text: str) -> Optional[dt.date]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ReviewRefused("That receipt date doesn't look right. Pick it with the date "
                            "picker and try again.", 400,
                            detail=f"expected YYYY-MM-DD, got {text!r}") from exc


def approve(engine, shipment_po_id: str, *, reviewer: str, receipt_date: str,
            fingerprint: str, now: dt.datetime) -> dict:
    """
    Record Paula's approval of one PO. Sets states and verdicts; writes NOTHING
    anywhere else. Every covered line gets a verdict (Requirements §1), including
    NO_CHANGE lines accepted as they are.
    """
    date = parse_receipt_date(receipt_date)
    counts = {"approved": 0, "accepted_unchanged": 0, "date_applied": 0, "date_skipped": []}
    with engine.begin() as conn:
        view = _load_for_decision(conn, shipment_po_id, fingerprint)
        if not view.can_approve:
            raise ReviewRefused(
                "This PO can't be approved. Reload the page to see why.", 409,
                detail="; ".join(f"{ln.label}: {ln.block_reason}" for ln in view.blocking)
                or "nothing to approve")
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
            skipped = None
            if date is not None:
                if line.override_protected:
                    skipped = (f"line already carries a human override (override="
                               f"{row['current_override_flag']}, updated receipt date="
                               f"{row['current_updated_receipt_date']}); date not applied")
                    counts["date_skipped"].append(line.label)
                else:
                    values.update(confirmed_receipt_date=date, date_approved_by=reviewer,
                                  date_approved_at=now, date_write_status="APPROVED")
                    scopes.append("DATE")
                    counts["date_applied"] += 1
            to_state = sc.STATE_APPROVED if scopes else row["state"]
            if to_state != row["state"]:
                sc.assert_transition(conn, row["state"], to_state)
                values["state"] = to_state
            conn.execute(proposed_changes.update().where(proposed_changes.c.id == row["id"])
                         .values(**values))
            _audit(conn, now=now, reviewer=reviewer, event="REVIEW_APPROVED", line=line,
                   to_state=to_state, detail={
                       "scopes": scopes, "approved_quantity": values.get("approved_quantity"),
                       "receipt_date": date.isoformat() if date and "DATE" in scopes else None,
                       "date_skipped": skipped, "flagged": line.flagged,
                       "attention_reason": row["attention_reason"],
                   })
            counts["approved" if scopes else "accepted_unchanged"] += 1
    return counts


def reject(engine, shipment_po_id: str, *, reviewer: str, reason: str, fingerprint: str,
           now: dt.datetime) -> int:
    """Discard every awaiting line of one PO, with the reason on each line and in the audit."""
    reason = (reason or "").strip()
    if not reason:
        raise ReviewRefused("Please say why you're rejecting this PO, then try again.", 400)
    if len(reason) > 1000:
        raise ReviewRefused("Please keep the reason under 1,000 characters.", 400)
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
