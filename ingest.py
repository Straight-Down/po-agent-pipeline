"""
Persist a parsed shipment into the database (Phase 2, item 4).

The join between two halves that already worked separately: `claude_extractor` /
`document_parsers` on one side, migration 0001 on the other. Nothing here reads
email or writes to NetSuite -- it classifies attachments, parses the packing list,
asks the matcher what changes the slip implies, and writes the result as
`proposed_changes` rows for a human to review.

## Idempotency is the design, not a safety net

Re-ingesting the same document must produce the same rows. Three mechanisms, in
the order they fire:

1. **`messages.graph_message_id`** -- the same message, redelivered. Graph's
   `Mail.Read` cannot mark a message read or move it, so the mailbox cannot hold
   "already processed"; the database has to.
2. **`shipments.primary_attachment_sha`** -- the same *content*, whatever message
   carried it. This is the one that catches Paula forwarding vendor mail, where a
   new message id arrives with an identical attachment. It short-circuits **before
   parsing**, so a re-ingest costs no extractor tokens at all.
3. **`ux_proposed_changes_canonical_key`** -- the backstop. If the first two were
   ever bypassed, a re-parse that renders `NEW  INDIGO` differently still collides
   with the existing row rather than creating a phantom one, because identity is
   the canonical key and never a digest of the row.

## What this deliberately does not do

- **Never creates a PO line.** A vendor line with no NetSuite counterpart becomes
  `NEEDS_ATTENTION`; there is no insert path and no state for one.
- **Never derives a date.** `vendor_etd`/`vendor_eta` are written to `shipments`
  as reference text. No date reaches a `proposed_changes` row: the schema's
  `ck_proposed_changes_date_needs_human` would reject it anyway.
- **Never touches `custcol_sd_fg_excluderepspark`.**
- **Never writes to NetSuite.** Reads only, and the matcher is handed a mock-mode
  client (see `_fetch_po_lines`) which refuses writes structurally.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

import matcher as mt
import schema as sc
from netsuite_client import NetSuiteClient, NetSuiteError, POLine
from schema import (
    attachments,
    audit_log,
    change_candidates,
    message_attachments,
    messages,
    proposed_changes,
    shipment_pos,
    shipment_sources,
    shipments,
)

logger = logging.getLogger(__name__)

#: Extraction confidences that set `proposed_changes.needs_review` on a line.
#: Derived from the line's own confidence rather than invented: the document-level
#: `ParseResult.needs_review` fires on 100% of real documents and so carries no
#: per-line information (RUNBOOK section 7).
REVIEW_CONFIDENCES = ("medium", "low")


@dataclass
class SourceMessage:
    """
    The email a shipment arrived on.

    Supplied by the caller. There is no Graph client yet (Phase 2 item 2), so in
    this task it is either constructed by hand or omitted entirely -- an ingest
    with `message=None` writes no `messages` row and relies on content dedup
    alone.
    """

    graph_message_id: str
    mailbox: str
    received_at: dt.datetime
    subject: str = ""
    from_address: str = ""
    internet_message_id: Optional[str] = None
    forwarded_by: Optional[str] = None
    sent_at: Optional[dt.datetime] = None


@dataclass
class IngestReport:
    """What one ingest did, in enough detail to explain itself without the DB."""

    shipment_id: Optional[str] = None
    created: bool = False
    reason: str = ""
    rows: dict = field(default_factory=dict)
    states: dict = field(default_factory=dict)
    po_resolution: dict = field(default_factory=dict)
    #: How many item records were read for colour names, and what they said.
    colour_reads: int = 0
    colour_names: dict = field(default_factory=dict)
    unpopulated: list = field(default_factory=list)
    parse_warnings: list = field(default_factory=list)

    def summary(self) -> str:
        rows = ", ".join(f"{k}={v}" for k, v in sorted(self.rows.items()) if v)
        states = ", ".join(f"{k}={v}" for k, v in sorted(self.states.items()))
        return f"{'created' if self.created else 'no-op'}: {rows or 'no rows'}" + (
            f" | states: {states}" if states else ""
        )


def sha256_file(path: Union[str, Path]) -> str:
    """Content hash -- the dedup axis that survives a re-forward."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _utcnow() -> dt.datetime:
    """Timestamps are generated here, never by the database -- see schema.py."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def _identity_tranids(po_key: str) -> list[str]:
    """
    Default PO lookup: try the printed number exactly as it appeared.

    **This will fail against real NetSuite data, and that is deliberate.** Vendors
    print `1662`; NetSuite stores `PO0001662`, and querying the bare number returns
    a *successful* empty result. The transformation (extract digits -> zero-pad ->
    prefix) is a known Phase 2 blocker: it needs Setup > Company >
    Auto-Generated Numbers read, validation against several hundred real tranIds,
    and Paula or Brandon's confirmation. Guessing `"PO"` + 7 digits from one sample
    is exactly what the build plan says not to do.

    So the rule is not baked in here. `tranid_resolver` is the seam it will occupy
    once confirmed; until then an unresolvable PO is a defined outcome
    (`resolution_status = 'NOT_FOUND'`), not a crash.
    """
    return [po_key]


# ---------------------------------------------------------------------------
# NetSuite reads
# ---------------------------------------------------------------------------


def _fetch_po_lines(
    client: Optional[NetSuiteClient],
    po_keys: Sequence[str],
    tranid_resolver: Callable[[str], list[str]],
) -> tuple[dict, dict]:
    """
    Resolve each printed PO number and read its lines.

    Returns `(lines_by_printed_key, resolution)`. Resolution failure is per PO and
    never fatal: one unresolvable PO on a six-PO slip must not cost the other five
    (`shipment_pos.resolution_status` exists for exactly this).

    The lines come back keyed by the number **as printed**, because that is what
    the extracted lines carry and what the matcher will look up.
    """
    lines_by_key: dict[str, list[POLine]] = {}
    resolution: dict[str, dict] = {}

    for key in po_keys:
        record: dict = {
            "status": "UNRESOLVED",
            "ns_tranid": None,
            "ns_internal_id": None,
            "strategy": None,
            "detail": "",
        }
        if client is None or client.is_mock:
            # Stub mode: no NetSuite read happened at all. Recorded as UNRESOLVED
            # rather than NOT_FOUND -- "we did not look" and "we looked and it is
            # not there" are different facts and the review screen should not
            # conflate them.
            record["detail"] = "no live NetSuite client supplied"
            if client is not None and client.is_mock:
                lines_by_key[key] = client.get_purchase_order(key)
                if lines_by_key[key]:
                    record.update(status="RESOLVED", ns_tranid=key, strategy="mock")
            resolution[key] = record
            continue

        for candidate in tranid_resolver(key):
            try:
                internal_id = client.resolve_po_internal_id(candidate)
            except NetSuiteError as exc:
                record["detail"] = str(exc).splitlines()[0][:200]
                continue
            try:
                lines_by_key[key] = client.get_purchase_order(candidate)
            except NetSuiteError as exc:
                record.update(status="NOT_FOUND", detail=str(exc).splitlines()[0][:200])
                break
            record.update(
                status="RESOLVED",
                ns_tranid=candidate,
                ns_internal_id=internal_id,
                strategy=client.last_lookup_strategy,
                detail="",
            )
            break
        else:
            record["status"] = "NOT_FOUND"
        resolution[key] = record

    return lines_by_key, resolution


# ---------------------------------------------------------------------------
# row builders
# ---------------------------------------------------------------------------


def _upsert_attachment(conn, path: Path, classification, now: dt.datetime) -> str:
    """One row per distinct CONTENT. Re-forwarded bytes reuse the existing row."""
    sha = sha256_file(path)
    existing = conn.execute(
        attachments.select().with_only_columns(attachments.c.content_sha256)
        .where(attachments.c.content_sha256 == sha)
    ).scalar()
    if existing:
        return sha
    conn.execute(attachments.insert(), {
        "content_sha256": sha,
        "byte_size": path.stat().st_size,
        "doc_type": _doc_type_value(classification),
        "doc_type_reason": (classification.reason or "")[:1000] if classification else None,
        "open_failure_reason": (
            classification.unreadable_reason if classification else None
        ),
        "banned_as_data_source": bool(
            classification is not None
            and classification.doc_type.value == "inspection_report"
        ),
        # Where the bytes live TODAY: the local corpus path. Azure Blob at Phase 4
        # (rationale section 13). Not a placeholder -- this is the real location.
        "stored_uri": str(path.resolve()),
        "first_seen_at": now,
    })
    return sha


def _doc_type_value(classification) -> str:
    """Map the classifier's DocType onto the schema's CHECK-constrained set."""
    if classification is None:
        return "OTHER"
    if classification.unreadable_reason:
        return "UNREADABLE"
    mapping = {
        "packing_list": "PACKING_LIST",
        "commercial_invoice": "COMMERCIAL_INVOICE",
        "shipping_advice": "SHIPPING_ADVICE",
        "shipping_schedule": "SHIPPING_SCHEDULE",
        "payment_request": "PAYMENT_REQUEST",
        "inspection_report": "INSPECTION_REPORT",
    }
    return mapping.get(classification.doc_type.value, "OTHER")


def _upsert_message(conn, message: SourceMessage, now: dt.datetime) -> tuple[str, bool]:
    """Dedup axis 1. Returns `(message_id, created)`."""
    existing = conn.execute(
        messages.select().with_only_columns(messages.c.id)
        .where(messages.c.graph_message_id == message.graph_message_id)
    ).scalar()
    if existing:
        return existing, False
    message_id = sc.new_id()
    conn.execute(messages.insert(), {
        "id": message_id,
        "graph_message_id": message.graph_message_id,
        "internet_message_id": message.internet_message_id,
        "mailbox": message.mailbox,
        "subject": message.subject or None,
        "from_address": message.from_address or None,
        "forwarded_by": message.forwarded_by,
        "sent_at": message.sent_at,
        "received_at": message.received_at,
        "ingested_at": now,
    })
    return message_id, True


def _link_attachment(conn, message_id: str, sha: str, filename: str) -> bool:
    already = conn.execute(
        message_attachments.select().with_only_columns(message_attachments.c.id)
        .where(message_attachments.c.message_id == message_id)
        .where(message_attachments.c.content_sha256 == sha)
    ).scalar()
    if already:
        return False
    conn.execute(message_attachments.insert(), {
        "id": sc.new_id(), "message_id": message_id,
        "content_sha256": sha, "filename": filename[:500],
    })
    return True


def _audit(conn, *, workflow, actor, actor_kind, event, now, **refs) -> None:
    """Append-only. Nothing in this module ever updates or deletes these rows."""
    conn.execute(audit_log.insert(), {
        "id": sc.new_id(),
        "occurred_at": now,
        "workflow": workflow,
        "actor": actor,
        "actor_kind": actor_kind,
        "event": event,
        "message_id": refs.get("message_id"),
        "shipment_id": refs.get("shipment_id"),
        "change_id": refs.get("change_id"),
        "from_state": refs.get("from_state"),
        "to_state": refs.get("to_state"),
        "detail_json": json.dumps(refs["detail"]) if refs.get("detail") else None,
    })


def _change_row(change: mt.ProposedChange, line: dict, shipment_id, po_id, sha, now) -> dict:
    """
    One `proposed_changes` row from one matcher output plus its source line.

    Canonical key columns come from `canonical.py` via the matcher's own key
    functions, so the row is keyed exactly the way the match was made -- and the
    verbatim `src_*` columns keep what the vendor printed, untouched. Those two
    facts are the whole point of the pair of column groups.
    """
    balance = change.line_balance or {}
    return {
        "id": sc.new_id(),
        "shipment_id": shipment_id,
        "shipment_po_id": po_id,
        "state": change.status,
        # canonical -- casefolded, '' where the document said nothing
        "key_style": mt.canonical(change.style_number),
        "key_color": mt.canonical(change.color),
        "key_size": mt._size_key(change.size),
        # verbatim -- never compared, only displayed and audited
        "src_style_text": change.style_number,
        "src_color_text": change.color,
        "src_size_text": change.size,
        "src_quantity_text": (
            None if line.get("quantity") is None else str(line["quantity"])
        ),
        "source_hint": (line.get("source_hint") or None),
        "source_sha256": sha,
        # the five review figures (change 6); outstanding is derived by v_review_lines
        "ns_line_id": change.line_id,
        "ns_item_internal_id": None,  # see IngestReport.unpopulated
        "current_quantity": balance.get("line_quantity"),
        "current_quantity_received": balance.get("quantity_received"),
        "proposed_quantity": change.proposed_quantity,
        # NetSuite's date state at proposal time, for display beside the references
        "current_expected_receipt_date": _as_date(change.current_expected_receipt_date),
        "current_updated_receipt_date": _as_date(change.current_updated_receipt_date),
        "current_override_flag": change.current_override_flag,
        "ns_line_is_open": None,  # see IngestReport.unpopulated
        "ns_line_closed": change.line_closed,
        # calibration: the tool's claim. The human half stays NULL until the
        # review UI fills it in -- that is the Phase 3 acceptance criterion.
        "extraction_confidence": change.extraction_confidence,
        "extraction_note": (change.extraction_note or None),
        "needs_review": change.extraction_confidence in REVIEW_CONFIDENCES,
        "attention_reason": (change.attention_reason or None),
        "created_at": now,
        "updated_at": now,
    }


def _as_date(value: Optional[str]) -> Optional[dt.date]:
    return dt.date.fromisoformat(value) if value else None


# ---------------------------------------------------------------------------
# the entry point
# ---------------------------------------------------------------------------


def ingest_shipment(
    engine,
    attachment_paths: Sequence[Union[str, Path]],
    *,
    message: Optional[SourceMessage] = None,
    client: Optional[NetSuiteClient] = None,
    extractor=None,
    actor: str = "system",
    tranid_resolver: Callable[[str], list[str]] = _identity_tranids,
    cross_check: bool = True,
    now: Optional[dt.datetime] = None,
) -> IngestReport:
    """
    Classify, parse, match and persist one shipment. Returns what it did.

    Idempotent: called twice on the same documents, the second call parses nothing
    and writes nothing but returns the first shipment's id with `created=False`.
    """
    from attachment_classifier import classify_attachments
    from document_parsers import parse_shipment_email

    now = now or _utcnow()
    paths = [Path(p) for p in attachment_paths]
    report = IngestReport()

    classification = classify_attachments(paths, extractor=extractor)
    primary = classification.primary
    by_path = {c.path.resolve(): c for c in classification.selected + classification.excluded}

    with engine.begin() as conn:
        shas = {}
        for path in paths:
            shas[path.resolve()] = _upsert_attachment(
                conn, path, by_path.get(path.resolve()), now
            )
        message_id = None
        if message is not None:
            message_id, _created = _upsert_message(conn, message, now)
            for path in paths:
                _link_attachment(conn, message_id, shas[path.resolve()], path.name)

        primary_sha = shas.get(primary.path.resolve()) if primary else None

        # Dedup axis 2, and the reason it is checked HERE: before the extractor
        # runs. A re-ingest of forwarded mail costs no tokens.
        if primary_sha:
            existing = conn.execute(
                shipments.select().with_only_columns(shipments.c.id)
                .where(shipments.c.primary_attachment_sha == primary_sha)
                .where(shipments.c.superseded_by_shipment_id.is_(None))
            ).scalar()
            if existing:
                report.shipment_id = existing
                report.created = False
                report.reason = (
                    f"already ingested: content {primary_sha[:12]}... is the primary "
                    f"document of shipment {existing}. Nothing parsed, nothing written."
                )
                _audit(conn, workflow="PACKING_SLIP", actor=actor, actor_kind="SYSTEM",
                       event="INGEST_SKIPPED_DUPLICATE", now=now,
                       message_id=message_id, shipment_id=existing,
                       detail={"primary_attachment_sha": primary_sha})
                report.rows = {"audit_log": 1}
                return report

    # Parsing happens outside the transaction: it makes network calls to the
    # Anthropic API and can take tens of seconds, and holding a write transaction
    # open across that is a bad habit even on SQLite.
    parsed = parse_shipment_email(paths, extractor=extractor, cross_check=cross_check)
    report.parse_warnings = list(parsed.warnings)

    po_keys = sorted({str(ln.get("po_number") or "").strip() for ln in parsed.lines if ln.get("po_number")})
    lines_by_key, resolution = _fetch_po_lines(client, po_keys, tranid_resolver)
    report.po_resolution = resolution

    # Per-PO colour vocabularies, built with the LIVE client because the matcher is
    # handed a mock one. Cached by item id for the whole ingest, then discarded.
    #
    # Built LAZILY, per PO: only if some colour on the slip does not already match a
    # code on that PO. A code-printing vendor therefore costs ZERO item reads, which
    # is the point -- eager construction would charge every shipment for a lookup
    # most of them never consult.
    colour_cache: dict = {}
    colour_lookups = {}
    if client is not None and not client.is_mock:
        for key, lines in lines_by_key.items():
            printed = {
                mt.canonical(ln.get("color"))
                for ln in parsed.lines
                if str(ln.get("po_number") or "").strip() == key
            }
            codes = {mt.canonical(line.color) for line in lines}
            if not printed - codes:
                continue
            colour_lookups[key] = mt.build_colour_lookup(client, lines, cache=colour_cache)
        report.colour_reads = len(colour_cache)
        report.colour_names = {
            key: dict(sorted(lookup.display.items()))
            for key, lookup in colour_lookups.items()
        }
        missing = sorted({
            code for lookup in colour_lookups.values() for code in lookup.missing_names
        })
        if missing:
            report.unpopulated.append(
                "colour codes on these POs whose item carries no colour name, so a "
                f"printed NAME cannot resolve to them (code matching still works): {missing}"
            )

    # The matcher is handed a MOCK client holding exactly the lines already read.
    # Two reasons: it cannot make a surprise network call mid-diff, and an
    # unresolvable PO arrives as an empty line list (-> NEEDS_ATTENTION) instead of
    # an exception that would abort the whole shipment.
    changes = mt.build_proposed_changes(
        parsed.lines,
        NetSuiteClient(mock_data=lines_by_key),
        eta=parsed.ship_info.get("eta"),
        etd=parsed.ship_info.get("etd"),
        shipment_needs_manual_entry=parsed.needs_manual_entry,
        colour_lookups=colour_lookups,
    )

    counts = {k: 0 for k in ("shipments", "shipment_sources", "shipment_pos",
                             "proposed_changes", "change_candidates", "audit_log")}
    counts["attachments"] = len(shas)
    counts["messages"] = 1 if message_id else 0
    counts["message_attachments"] = len(shas) if message_id else 0
    states: dict[str, int] = {}

    with engine.begin() as conn:
        shipment_id = sc.new_id()
        conn.execute(shipments.insert(), {
            "id": shipment_id,
            "origin": "VENDOR_EMAIL" if message_id else "PAULA_DIRECTED",
            "message_id": message_id,
            "primary_attachment_sha": primary_sha if message_id else None,
            "source_set_hash": hashlib.sha256(
                "".join(sorted(shas.values())).encode()
            ).hexdigest(),
            "vendor_name": parsed.vendor_name or None,
            # Reference only. Never promoted to a receipt date, and no column on
            # proposed_changes could hold one if it were.
            #
            # Stored EXACTLY as the vendor printed it -- "2026/6/27 19:40", not
            # "2026-06-27". The matcher normalises to ISO for display, and the
            # review screen can do the same; but normalising on the way in would
            # discard the printed time, which is real source text. Same rule as the
            # src_* columns on a line: keep what the document said, derive the rest.
            "vendor_etd": parsed.ship_info.get("etd"),
            "vendor_eta": parsed.ship_info.get("eta"),
            "parser": parsed.parser or None,
            "extractor_model": getattr(extractor, "model", None),
            "extractor_prompt_version": None,  # see IngestReport.unpopulated
            "doc_needs_review": bool(parsed.needs_review),
            "needs_manual_entry": bool(parsed.needs_manual_entry),
            "parse_warnings_json": json.dumps(parsed.warnings) if parsed.warnings else None,
            "parse_notes_json": json.dumps(parsed.notes) if parsed.notes else None,
            "line_count": len(parsed.lines),
            "unit_total": sum(ln.get("quantity") or 0 for ln in parsed.lines),
            "created_by": actor,
            "created_at": now,
        })
        counts["shipments"] = 1

        for item in classification.selected + classification.excluded:
            sha = shas.get(item.path.resolve())
            if sha is None:
                continue
            role = ("PRIMARY" if primary is not None and item.path == primary.path
                    else "CROSS_CHECK" if item in classification.selected
                    else "EXCLUDED")
            conn.execute(shipment_sources.insert(), {
                "id": sc.new_id(), "shipment_id": shipment_id, "content_sha256": sha,
                "role": role,
                "exclusion_reason": (
                    getattr(item, "excluded_reason", None) if role == "EXCLUDED" else None
                ),
                "agreement_json": None,  # see IngestReport.unpopulated
            })
            counts["shipment_sources"] += 1

        po_ids = {}
        for key in po_keys:
            info = resolution.get(key, {})
            printed = next(
                (str(ln.get("po_number")) for ln in parsed.lines
                 if str(ln.get("po_number") or "").strip() == key), key
            )
            po_id = sc.new_id()
            conn.execute(shipment_pos.insert(), {
                "id": po_id, "shipment_id": shipment_id,
                "po_number_printed": printed[:120], "po_number_key": key[:40],
                "ns_tranid": info.get("ns_tranid"),
                "ns_internal_id": info.get("ns_internal_id"),
                "resolution_status": info.get("status", "UNRESOLVED"),
                "resolution_strategy": info.get("strategy"),
                "resolved_at": now if info.get("status") == "RESOLVED" else None,
            })
            po_ids[key] = po_id
            counts["shipment_pos"] += 1

        for change, line in zip(changes, parsed.lines):
            key = str(line.get("po_number") or "").strip()
            po_id = po_ids.get(key)
            if po_id is None:
                # A line with no PO number at all. The matcher already flags it;
                # there is no shipment_pos row to hang it on, so it cannot be
                # persisted as a change. Surfaced rather than dropped silently.
                report.unpopulated.append(
                    f"line {line.get('style_number')}/{line.get('color')}/"
                    f"{line.get('size')} has no PO number, so it has no shipment_pos "
                    "parent and was NOT persisted"
                )
                continue
            # The seeded transition table is the authority for this, including the
            # initial insert -- see schema.assert_transition.
            sc.assert_transition(conn, sc.STATE_INSERT, change.status)
            row = _change_row(change, line, shipment_id, po_id, shas.get(
                primary.path.resolve()) if primary else None, now)
            conn.execute(proposed_changes.insert(), row)
            counts["proposed_changes"] += 1
            states[change.status] = states.get(change.status, 0) + 1

            for candidate in change.candidate_lines:
                conn.execute(change_candidates.insert(), {
                    "id": sc.new_id(), "change_id": row["id"],
                    "ns_line_id": str(candidate["line_id"]),
                    "quantity": candidate.get("quantity"),
                    "quantity_received": candidate.get("quantity_received"),
                    "quantity_billed": candidate.get("quantity_billed"),
                    "expected_receipt_date": _as_date(candidate.get("expected_receipt_date")),
                    "updated_receipt_date": _as_date(candidate.get("updated_receipt_date")),
                    "override_expected_receipt": candidate.get("override_expected_receipt"),
                    "rate": candidate.get("rate"),
                    "is_open": bool(candidate.get("is_open")),
                    "selected": False,
                })
                counts["change_candidates"] += 1

        _audit(conn, workflow="PACKING_SLIP", actor=actor, actor_kind="SYSTEM",
               event="SHIPMENT_INGESTED", now=now, message_id=message_id,
               shipment_id=shipment_id,
               detail={"parser": parsed.parser, "lines": len(parsed.lines),
                       "states": states, "po_keys": po_keys})
        counts["audit_log"] = 1

    report.shipment_id = shipment_id
    report.created = True
    report.rows = counts
    report.states = states
    report.unpopulated.extend(_unpopulated_columns(client))
    return report


def _unpopulated_columns(client: Optional[NetSuiteClient]) -> list[str]:
    """
    Schema columns this pipeline cannot fill yet, and why.

    Reported rather than defaulted. A plausible-looking default in one of these
    would be indistinguishable from a real value later, which is the failure mode
    worth avoiding -- an empty column is a question, a wrong one is not.
    """
    gaps = [
        "proposed_changes.ns_item_internal_id -- POLine carries item_internal_id "
        "but matcher.ProposedChange does not surface it; needs a matcher field",
        "proposed_changes.ns_line_is_open -- POLine.is_open exists (change 5) but "
        "ProposedChange exposes only line_closed; needs a matcher field",
        "shipments.extractor_prompt_version -- claude_extractor has no prompt "
        "version constant to read",
        "shipment_sources.agreement_json -- the cross-check result is currently a "
        "warning string on ParseResult, not structured data",
        "messages.* -- no Graph client yet (Phase 2 item 2); the caller supplies a "
        "SourceMessage or omits it",
        "proposed_changes.human_verdict / verdict_by / verdict_at -- filled by the "
        "review UI (Phase 3 acceptance criterion), by design",
        "proposed_changes.approved_* / confirmed_receipt_date / *_write_status -- "
        "filled at approval and write-back (Phase 3), by design",
    ]
    if client is None or client.is_mock:
        gaps.append(
            "NetSuite snapshot columns (ns_line_id, current_quantity, "
            "current_quantity_received, current_*_date, current_override_flag, "
            "ns_line_closed) -- no live NetSuite client was supplied, so nothing "
            "was read and these stay NULL"
        )
    return gaps
