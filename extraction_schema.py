"""
Schemas for vendor-document extraction.

Two layers:

  1. Pydantic models (`PackingSlipExtraction`, `ShippingAdviceExtraction`) — the
     contract the Anthropic API is constrained to via structured outputs. The
     model cannot return a shape that doesn't validate.
  2. `ParseResult` — the normalized handoff to the rest of the pipeline. Its
     `lines` are plain dicts with exactly the keys `matcher.py` reads
     (po_number, style_number, color, size, quantity), plus extra provenance
     keys the matcher ignores and the review UI can display.

Design notes that matter downstream:

  - **Sizes and colors are emitted verbatim as the vendor printed them.** The
    extractor must NOT normalize `XXL` -> `2X`; `matcher.py` owns that mapping
    (`SIZE_ALIASES`), and it was validated against real NetSuite data. Two
    components normalizing the same field independently is how silent
    mismatches get introduced.
  - **Nothing is ever silently dropped.** Every line carries a `confidence`,
    and anything the model saw that looked like line data but couldn't parse
    goes in `unparsed_regions`. Both feed `needs_review`, which routes the
    shipment to a human — the same principle as `NEEDS_ATTENTION` in
    `matcher.py`.
  - Every field is required (no Optional). Structured outputs require
    `additionalProperties: false` and complete `required` lists, so "unknown"
    is expressed as an empty string or a low-confidence flag rather than a
    missing key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

Confidence = Literal["high", "medium", "low"]

#: Lines at or below this confidence must be reviewed by a human before any
#: NetSuite write, regardless of whether the matcher found a clean match.
REVIEW_CONFIDENCE = {"medium", "low"}


class ExtractedLine(BaseModel):
    """One PO/style/color/size/quantity row read off a vendor packing slip."""

    po_number: str = Field(
        description="The purchase order number exactly as printed, digits only "
        "if the sheet shows e.g. 'PO#1662' (emit '1662'). Empty string if the "
        "row's PO cannot be determined — set confidence to 'low' if so."
    )
    style_number: str = Field(
        description="Style number exactly as printed, e.g. 'M120246'. No prefix, "
        "no description text."
    )
    color: str = Field(
        description="Colour code exactly as the vendor printed it, e.g. 'TID'. "
        "Do not translate, expand, or normalize it."
    )
    size: str = Field(
        description="Size label exactly as the vendor printed it, e.g. 'XXL'. "
        "Do NOT convert to another convention (do not turn XXL into 2X) — a "
        "downstream component owns that mapping."
    )
    quantity: int = Field(
        description="Units shipped for this PO/style/colour/size. Use 0 only if "
        "the figure is genuinely unreadable, and set confidence to 'low'."
    )
    confidence: Confidence = Field(
        description="'high' only when every field above is unambiguous in the "
        "sheet. 'medium' if you inferred a field from layout or context. 'low' "
        "if you are guessing any field. Never mark a guess 'high'."
    )
    note: str = Field(
        description="Empty string when confidence is 'high'. Otherwise one short "
        "sentence on exactly what was ambiguous, for the human reviewer."
    )
    source_hint: str = Field(
        description="Where this came from, as SHEET!CELL or SHEET!ROW, e.g. "
        "'PACKING!R42'. Lets a reviewer find the row in the original file."
    )


class PackingSlipExtraction(BaseModel):
    """Everything read off one packing-slip worksheet."""

    vendor_name: str = Field(
        description="Vendor/shipper name if the sheet states one, else empty string."
    )
    document_summary: str = Field(
        description="One sentence describing the sheet's layout, so a human can "
        "sanity-check that it was read the way they'd expect."
    )
    lines: list[ExtractedLine] = Field(
        description="Every PO/style/colour/size/quantity row found. One entry per "
        "size per colour per style per PO — long format, not a size-across grid. "
        "Omit rows whose quantity is zero or blank."
    )
    unparsed_regions: list[str] = Field(
        description="Anything that looked like shipment line data but which you "
        "could NOT confidently turn into a line above. Describe each by location "
        "and what blocked you. This list existing is fine; silently dropping data "
        "is not. Empty list if everything parsed cleanly."
    )
    warnings: list[str] = Field(
        description="Anything else a human should know before trusting this "
        "extraction — e.g. totals that don't add up, duplicate rows, unit "
        "ambiguity. Empty list if none."
    )


class ShippingAdviceExtraction(BaseModel):
    """Shipment-level fields off a shipping advice / freight document."""

    invoice_no: str = Field(description="Vendor invoice number, or empty string.")
    hawb: str = Field(description="House air waybill number, or empty string.")
    mawb: str = Field(description="Master air waybill number, or empty string.")
    etd: str = Field(
        description="Estimated time of departure, as printed. Empty string if absent."
    )
    eta: str = Field(
        description="Estimated time of arrival at the destination port, as "
        "printed. Empty string if absent. Do not compute or adjust it."
    )
    confidence: Confidence = Field(
        description="'high' only if ETD/ETA/HAWB were explicitly labelled. 'low' "
        "if you inferred which date is which from position alone."
    )
    note: str = Field(
        description="Empty string when confidence is 'high', else one short "
        "sentence on what was ambiguous."
    )
    warnings: list[str] = Field(
        description="Anything else a reviewer should know. Empty list if none."
    )


# ---------------------------------------------------------------------------
# Normalized pipeline handoff
# ---------------------------------------------------------------------------


@dataclass
class ParseResult:
    """
    What the parsing layer hands to the matcher and the review step.

    `lines` are matcher-ready dicts. `needs_review` is the single flag Phase 2/3
    should gate on — it is True if anything at all was uncertain, and it is
    deliberately conservative.
    """

    lines: list[dict] = field(default_factory=list)
    ship_info: dict = field(default_factory=dict)
    parser: str = ""  # which path produced this, e.g. "inprotex-deterministic"
    vendor_name: str = ""
    document_summary: str = ""
    unparsed_regions: list[str] = field(default_factory=list)

    #: Things a human must look at before this is trusted. Feeds `needs_review`.
    warnings: list[str] = field(default_factory=list)

    #: Informational only -- which parser was chosen and why. Deliberately does
    #: NOT feed `needs_review`: routing through the Claude extractor is the
    #: normal path for most vendors, so if it tripped the flag the flag would be
    #: True on every shipment and would stop meaning anything.
    notes: list[str] = field(default_factory=list)

    usage: dict = field(default_factory=dict)  # token/cost accounting, empty for free paths

    #: True when no attachment could supply per-size quantities, so the shipment
    #: must be keyed in by hand. Per Paula's ruling (2026-08-11) the size gap is
    #: never filled from an inspection report and never inferred by splitting a
    #: colour total across sizes — this is a full stop, not a degraded mode.
    needs_manual_entry: bool = False

    @property
    def low_confidence_lines(self) -> list[dict]:
        return [ln for ln in self.lines if ln.get("confidence", "high") in REVIEW_CONFIDENCE]

    @property
    def needs_review(self) -> bool:
        """
        True if a human must look before this is trusted.

        Conservative by design: any low/medium-confidence line, any region the
        extractor couldn't parse, any warning, or an empty result all trip it.
        An empty result counts because "we found nothing" and "this file has
        nothing in it" are indistinguishable from here.
        """
        return bool(
            self.needs_manual_entry
            or self.low_confidence_lines
            or self.unparsed_regions
            or self.warnings
            or not self.lines
        )

    def review_summary(self) -> str:
        """One-line human summary, for logs and the review digest."""
        bits = [f"{len(self.lines)} line(s) via {self.parser or 'unknown parser'}"]
        if self.low_confidence_lines:
            bits.append(f"{len(self.low_confidence_lines)} low-confidence")
        if self.unparsed_regions:
            bits.append(f"{len(self.unparsed_regions)} unparsed region(s)")
        if self.warnings:
            bits.append(f"{len(self.warnings)} warning(s)")
        bits.append("NEEDS REVIEW" if self.needs_review else "clean")
        return " | ".join(bits)


#: Confidence ordering — a collapse takes the WORST (highest rank) of its inputs.
#: A merge must never upgrade confidence: if any contributing row was uncertain,
#: the merged row is at least that uncertain.
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


def _confidence_rank(value: str) -> int:
    return _CONFIDENCE_RANK.get(str(value or "high").strip().lower(), len(_CONFIDENCE_RANK))


def _worst_confidence(values: list[str]) -> str:
    return max(values, key=_confidence_rank) if values else "high"


def aggregate_lines(
    lines: list[dict], document_label: str = ""
) -> tuple[list[dict], list[str]]:
    """
    Collapse duplicate (PO, style, colour, size) rows within ONE document, summing
    quantities, and return a deterministically ordered list.

    Two reasons this is correct semantics rather than a workaround:

    1. **Carton-detail documents legitimately repeat a key.** Symmetry's "Actual
       Packing" file lists one row per carton, so the same style/colour/size
       appears across many cartons and the shipped quantity for that key IS the
       sum. Emitting them separately would hand `build_proposed_changes` several
       partial quantities for one NetSuite line.
    2. **The extractor's row splitting is not deterministic.** The same Symmetry
       document produced 26 rows on one run and 25 on three others — always 1669
       units, always 25 keys once aggregated. Without this step, two runs of one
       document yield different `proposed_changes` row counts, which breaks
       idempotency on retry and makes line-count assertions flaky.

    Rules, each chosen deliberately:

    - **Rows with an empty size are never collapsed.** Size is part of the key, so
      collapsing on a blank would merge genuinely unrelated rows into one bucket.
      They pass through individually and keep whatever flags they carried.
    - **Confidence takes the minimum** (worst) across merged rows; a collapse can
      only ever lower it. There is no per-line `needs_review` field to OR — that
      flag lives on `ParseResult` and is already derived from the confidences and
      warnings of every line, so lowering confidence here propagates to it.
    - **Notes and source hints are unioned**, preserving each contributing row's
      provenance so a reviewer can still find every original row.
    - **Output is sorted by the key** (then by quantity and source hint as
      tiebreakers). Row *order* matters for idempotency as much as row count.
    - **Single document only.** Never call this across documents: two documents
      describing the same shipment must be reconciled and compared, not silently
      summed into each other.

    Returns (aggregated_lines, warnings). Every collapse is logged with its key
    and row count, plus a per-document total — a rising collapse rate is a signal
    about prompt stability and we do not want to lose sight of it.
    """
    label = f"{document_label}: " if document_label else ""

    collapsible: dict[tuple, list[dict]] = {}
    passthrough: list[dict] = []
    order: list[tuple] = []

    for line in lines:
        size = str(line.get("size") or "").strip()
        if not size:
            # Sizeless rows are kept individually and stay flagged; see rules above.
            passthrough.append(dict(line))
            continue
        key = (
            str(line.get("po_number") or "").strip(),
            str(line.get("style_number") or "").strip(),
            str(line.get("color") or "").strip(),
            size,
        )
        if key not in collapsible:
            collapsible[key] = []
            order.append(key)
        collapsible[key].append(line)

    merged: list[dict] = []
    collapsed_rows = 0
    collapsed_keys = 0

    for key in order:
        group = collapsible[key]
        first = dict(group[0])
        if len(group) == 1:
            merged.append(first)
            continue

        collapsed_keys += 1
        collapsed_rows += len(group) - 1
        total = sum(int(g.get("quantity") or 0) for g in group)
        confidence = _worst_confidence([str(g.get("confidence") or "high") for g in group])

        notes = []
        for g in group:
            note = str(g.get("note") or "").strip()
            if note and note not in notes:
                notes.append(note)
        hints = []
        for g in group:
            hint = str(g.get("source_hint") or "").strip()
            if hint and hint not in hints:
                hints.append(hint)

        first.update(
            quantity=total,
            confidence=confidence,
            note="; ".join(notes),
            source_hint=", ".join(hints),
        )
        merged.append(first)
        logger.info(
            "%scollapsed %d rows into one for key %s -> quantity %d, confidence %s",
            label, len(group), key, total, confidence,
        )

    def sort_key(line: dict) -> tuple:
        return (
            str(line.get("po_number") or "").strip(),
            str(line.get("style_number") or "").strip(),
            str(line.get("color") or "").strip(),
            str(line.get("size") or "").strip(),
            int(line.get("quantity") or 0),
            str(line.get("source_hint") or ""),
        )

    out = sorted(merged + passthrough, key=sort_key)

    warnings: list[str] = []
    if collapsed_keys:
        message = (
            f"aggregated {collapsed_rows} duplicate row(s) into {collapsed_keys} "
            f"style/colour/size key(s), summing quantities. This is expected on "
            f"carton-level documents where one key spans several cartons."
        )
        warnings.append(message)
        logger.info(
            "%s%d rows collapsed across %d key(s); %d line(s) out of %d input row(s)",
            label, collapsed_rows, collapsed_keys, len(out), len(lines),
        )
    if passthrough:
        warnings.append(
            f"{len(passthrough)} extracted row(s) have no size and were NOT aggregated — "
            f"a blank size cannot be used as a merge key, so they are preserved "
            f"individually and stay flagged for review."
        )

    return out, warnings


def meaningful(strings: list[str]) -> list[str]:
    """
    Drop entries with no alphanumeric content from a model-produced string list.

    Observed in a real run: a long `warnings` list came back containing a few
    degenerate fragments (a bare `","`) alongside the genuine entries. Those
    carry no information but would show up as empty bullets in the reviewer's
    digest. Anything with a letter or digit in it is kept verbatim — this trims
    noise, it does not summarize, merge, or suppress real warnings.
    """
    return [s.strip() for s in strings if any(ch.isalnum() for ch in s)]


def line_to_dict(line: ExtractedLine) -> dict[str, Any]:
    """
    Convert a validated model line into the dict shape `matcher.py` reads.

    The first five keys are what the matcher uses; the rest is provenance for
    the human review step, which the matcher ignores.
    """
    return {
        "po_number": line.po_number.strip(),
        "style_number": line.style_number.strip(),
        "color": line.color.strip(),
        "size": line.size.strip(),
        "quantity": line.quantity,
        "confidence": line.confidence,
        "note": line.note.strip(),
        "source_hint": line.source_hint.strip(),
    }


def deterministic_line_to_dict(raw: dict, source_hint: str = "") -> dict[str, Any]:
    """
    Same shape, for lines produced by a deterministic parser.

    Deterministic output is marked `high` confidence with no note: the Inprotex
    parser was hand-verified line-for-line against the vendor's own summary
    email (77/77), so its output is not a guess. If a deterministic parser ever
    returns a line with a missing field, `document_parsers` rejects the whole
    file and re-runs it through Claude rather than emitting a bad "high".
    """
    return {
        "po_number": str(raw.get("po_number") or "").strip(),
        "style_number": str(raw.get("style_number") or "").strip(),
        "color": str(raw.get("color") or "").strip(),
        "size": str(raw.get("size") or "").strip(),
        "quantity": int(raw["quantity"]),
        "confidence": "high",
        "note": "",
        "source_hint": source_hint,
    }
