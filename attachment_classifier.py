"""
Attachment classification — decide which of a shipment email's attachments is
the actual packing list, and parse only that.

Why this exists as its own step: Symmetry's real shipment email carried **six**
attachments — a commercial invoice, the actual packing list (rollup), the
carton-by-carton packing detail, an ocean schedule, a vendor payment request,
and two final inspection reports. Feeding all of them to the extractor is wrong
in both directions: it wastes tokens on documents with no line data, and it
risks pulling shipment quantities out of a document that isn't authoritative.

Two hard rules, both from real mistakes rather than theory:

1. **Filename alone cannot decide this.** The trap is concrete:
   - `SD #1720, 1721 INVOICE, PACKING LIST.pdf` says "PACKING LIST" but is a
     customs invoice whose quantities stop at style+colour. Parsing it is what
     produced the wrong conclusion that Symmetry sends no size breakdown.
   - `0626...Invoice_Packing.xlsx` (Inprotex) also says "Invoice" — and *is* the
     real, size-level packing slip, validated 77/77.
   Identical filename signals, opposite answers. So a filename match is a
   candidate, not a verdict; ambiguous cases are settled by looking at content.

2. **Inspection reports are never a data source.** Paula's explicit ruling
   (2026-08-11), not a design preference open to revisiting. An inspection
   report is excluded here regardless of what it contains — even though the
   W600001 one demonstrably held correct size data. If the actual packing list
   can't resolve to style/colour/size lines, the shipment goes to manual entry;
   the gap is never filled from a QC document, and never guessed at by splitting
   a colour total proportionally across sizes.

The classifier is deliberately conservative about what it promotes: a document
is only selected as a shipment-data source if it is a packing list AND appears to
carry per-size quantities.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class DocType(str, Enum):
    PACKING_LIST = "packing_list"
    COMMERCIAL_INVOICE = "commercial_invoice"
    INSPECTION_REPORT = "inspection_report"
    PAYMENT_REQUEST = "payment_request"
    SHIPPING_SCHEDULE = "shipping_schedule"
    SHIPPING_ADVICE = "shipping_advice"
    OTHER = "other"


#: Never a shipment-data source, whatever it contains. See rule 2 above.
BANNED_AS_DATA_SOURCE = {DocType.INSPECTION_REPORT}

#: Filename patterns, most specific first. `ambiguous=True` means "do not trust
#: this verdict; confirm against content".
_FILENAME_RULES: list[tuple[str, DocType, bool, str]] = [
    (r"inspection", DocType.INSPECTION_REPORT, False, "filename says inspection report"),
    (r"payment\s*request|remittance|proforma", DocType.PAYMENT_REQUEST, False, "filename says payment request"),
    (r"schedule|booking\s*confirm|sailing", DocType.SHIPPING_SCHEDULE, False, "filename says schedule/booking"),
    (r"shipping\s*advice|arrival\s*notice|pre[\s-]*alert", DocType.SHIPPING_ADVICE, False, "filename says shipping advice"),
    # "actual packing" is Symmetry's own naming for the real thing.
    (r"actual\s*packing", DocType.PACKING_LIST, False, "filename says 'actual packing'"),
    # Both words present -> genuinely undecidable from the name (see rule 1).
    (r"(?=.*invoice)(?=.*packing)", DocType.PACKING_LIST, True,
     "filename contains BOTH 'invoice' and 'packing' — undecidable from the name, checked content"),
    (r"packing|pack[\s_-]*list|\bp/?l\b", DocType.PACKING_LIST, True, "filename says packing list"),
    (r"invoice|\binv\b|commercial", DocType.COMMERCIAL_INVOICE, False, "filename says invoice"),
]

#: Hints that a packing list is the style/colour/size rollup rather than the
#: carton-by-carton detail. Both are usable; the rollup is preferred as primary
#: because it already matches the target schema.
_ROLLUP_HINTS = (r"covering", r"summary", r"recap", r"breakdown", r"rollup")


class _ContentVerdict(BaseModel):
    """Content-based classification for one attachment."""

    doc_type: str = Field(
        description="One of: packing_list, commercial_invoice, inspection_report, "
        "payment_request, shipping_schedule, shipping_advice, other."
    )
    has_size_breakdown: bool = Field(
        description="True only if the document gives quantities broken out per SIZE "
        "(e.g. XS/S/M/L/XL columns or one row per size). False if quantities stop at "
        "style or colour level, however detailed the rest of it is."
    )
    reason: str = Field(
        description="One short sentence citing what in the document decided it — "
        "a heading, a column header, a total row."
    )


class _ContentVerdicts(BaseModel):
    verdicts: list[_ContentVerdict] = Field(
        description="One verdict per document, in the same order the documents were given."
    )


CLASSIFIER_SYSTEM_PROMPT = """\
You classify attachments from an apparel vendor's shipment email so a purchase-\
order pipeline knows which one to parse for per-size shipped quantities.

For each document you are given a short preview of its beginning. Decide:

1. What kind of document it is.

2. Whether it breaks quantities out **per size**. This is the decisive question, \
and filenames lie about it in both directions — a file called "INVOICE, PACKING \
LIST" may be a customs invoice whose quantities stop at style and colour, while \
a file called "Invoice_Packing" may be a genuine size-level packing list. Judge \
only by what the content shows: look for size column headers (XS/S/M/L/XL/2XL) \
or one row per size. A document with cartons, weights, totals and colours but no \
size dimension has NO size breakdown.

Answer from the preview alone. If the preview is too short or ambiguous to tell, \
say so in the reason and set has_size_breakdown to false — a document wrongly \
promoted to "size-level source" would feed wrong quantities into an ERP, whereas \
one wrongly held back just gets flagged for a human."""


@dataclass
class AttachmentClassification:
    path: Path
    doc_type: DocType
    has_size_breakdown: bool
    reason: str
    method: str  # "filename" | "content" | "filename+content"
    is_rollup: bool = False
    preview_chars: int = 0
    #: Set when the file cannot be opened at all (corrupt, truncated, encrypted).
    unreadable_reason: Optional[str] = None

    @property
    def usable_as_shipment_data(self) -> bool:
        """
        Whether this attachment may be parsed for shipment quantities.

        Requires: a packing list, carrying per-size quantities, and not a banned
        document type. The ban is checked even though an inspection report would
        normally fail the packing-list test anyway — belt and braces on a rule
        that came from a person, not from the data.
        """
        return (
            self.unreadable_reason is None
            and self.doc_type == DocType.PACKING_LIST
            and self.has_size_breakdown
            and self.doc_type not in BANNED_AS_DATA_SOURCE
        )

    @property
    def excluded_reason(self) -> str:
        if self.unreadable_reason:
            return f"could not open: {self.unreadable_reason}"
        if self.doc_type in BANNED_AS_DATA_SOURCE:
            return (
                f"{self.doc_type.value} — permanently excluded as a shipment-data source "
                f"(Paula's ruling 2026-08-11), regardless of content"
            )
        if self.doc_type != DocType.PACKING_LIST:
            return f"not a packing list ({self.doc_type.value}): {self.reason}"
        if not self.has_size_breakdown:
            return f"packing list but no per-size quantities: {self.reason}"
        return ""


@dataclass
class ClassificationResult:
    selected: list[AttachmentClassification] = field(default_factory=list)
    excluded: list[AttachmentClassification] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def primary(self) -> Optional[AttachmentClassification]:
        """
        The attachment to parse. Prefers a style/colour/size rollup over
        carton-by-carton detail: both extract correctly (verified live on
        Symmetry's pair, which agreed exactly), but the rollup is already in the
        target shape and costs far fewer tokens.
        """
        if not self.selected:
            return None
        return sorted(self.selected, key=lambda c: (not c.is_rollup, c.path.name))[0]

    @property
    def cross_checks(self) -> list[AttachmentClassification]:
        """Other usable packing lists — available to verify the primary against."""
        primary = self.primary
        return [c for c in self.selected if c is not primary]

    @property
    def needs_manual_entry(self) -> bool:
        """
        True when nothing in the email can supply per-size quantities.

        Per Paula's ruling this is where the shipment stops and a human takes
        over: the size gap is never filled from an inspection report and never
        inferred by splitting a colour total across sizes.
        """
        return not self.selected

    def summary(self) -> str:
        bits = [f"{len(self.selected)} usable / {len(self.excluded)} excluded"]
        if self.primary:
            bits.append(f"primary: {self.primary.path.name}")
        if self.needs_manual_entry:
            bits.append("NO SIZE-LEVEL SOURCE -> manual entry")
        return " | ".join(bits)


# ---------------------------------------------------------------------------


def classify_by_filename(name: str) -> tuple[DocType, bool, str]:
    """Returns (doc_type, ambiguous, reason). `ambiguous` means confirm by content."""
    lowered = name.lower()
    for pattern, doc_type, ambiguous, reason in _FILENAME_RULES:
        if re.search(pattern, lowered):
            return doc_type, ambiguous, reason
    return DocType.OTHER, True, "filename gives no usable signal"


def looks_like_rollup(name: str) -> bool:
    lowered = name.lower()
    return any(re.search(p, lowered) for p in _ROLLUP_HINTS)


#: Per-sheet/per-page preview budget, and how many to sample.
_PREVIEW_ROWS_PER_SHEET = 16
_PREVIEW_CHARS_PER_PART = 1800
_PREVIEW_MAX_PARTS = 6

#: Size labels seen across real vendor documents. Used to locate a sheet's size
#: header row, which is the single most decisive thing a classifier can see.
_SIZE_TOKENS = {
    "XS", "S", "M", "L", "XL", "XXL", "XXXL", "2X", "3X", "4X",
    "2XL", "3XL", "4XL", "OS", "ONE SIZE",
}

#: How many size labels must appear on one row for it to be the size header.
_SIZE_HEADER_MIN_TOKENS = 3


def _find_size_header_row(grid: Any) -> Optional[int]:
    """
    1-based index of the row that looks like a sheet's size header, if any.

    Vendors put pages of letterhead above the actual table -- Inprotex's PACKING
    tab has its size columns well below the first 20 rows. Previewing only the
    top of the sheet therefore shows no size evidence and the classifier
    concludes, correctly but uselessly, that it cannot see any. Finding this row
    puts the decisive evidence in front of it instead.
    """
    for index, row in enumerate(grid.rows, start=1):
        hits = {
            str(cell).strip().upper()
            for cell in row
            if cell and str(cell).strip().upper() in _SIZE_TOKENS
        }
        if len(hits) >= _SIZE_HEADER_MIN_TOKENS:
            return index
    return None


def sheet_preview(grid: Any) -> str:
    """
    Classification preview for ONE worksheet.

    Factored out of `_preview` so that a whole-file preview and a single-sheet
    preview are produced by the same code -- including the size-header seek, which
    is the evidence classification actually turns on.
    """
    body = grid.render(1, min(_PREVIEW_ROWS_PER_SHEET, grid.n_rows))
    parts = [
        f"----- sheet '{grid.name}' (first rows of {grid.n_rows}) -----\n"
        + body[:_PREVIEW_CHARS_PER_PART]
    ]
    header_row = _find_size_header_row(grid)
    if header_row and header_row > _PREVIEW_ROWS_PER_SHEET:
        lo = max(1, header_row - 1)
        hi = min(grid.n_rows, header_row + 3)
        parts.append(
            f"----- sheet '{grid.name}', size-header region (rows {lo}-{hi}) -----\n"
            + grid.render(lo, hi)[:_PREVIEW_CHARS_PER_PART]
        )
    return "\n".join(parts)


@dataclass
class SectionClassification:
    """A classification verdict for one named section of a container document."""

    label: str  # e.g. a worksheet name
    doc_type: DocType
    has_size_breakdown: bool
    reason: str

    @property
    def is_shipment_data(self) -> bool:
        """
        Whether this section may be extracted for shipment quantities.

        Deliberately the same test as `AttachmentClassification.usable_as_shipment_data`:
        a packing list, carrying per-size quantities, and not a banned type.
        """
        return (
            self.doc_type == DocType.PACKING_LIST
            and self.has_size_breakdown
            and self.doc_type not in BANNED_AS_DATA_SOURCE
        )

    @property
    def skip_reason(self) -> str:
        """Why this section was not extracted. Empty string if it was."""
        if self.doc_type in BANNED_AS_DATA_SOURCE:
            return f"{self.doc_type.value} — never a shipment-data source (Paula's ruling)"
        if self.doc_type != DocType.PACKING_LIST:
            return f"classified {self.doc_type.value}, not a packing list"
        if not self.has_size_breakdown:
            return "packing list but no per-size quantities"
        return ""


def classify_sections(
    sections: Sequence[tuple[str, str]], extractor: Any = None
) -> list[SectionClassification]:
    """
    Classify named text sections using the SAME content-based path as whole
    attachments -- identical system prompt, identical schema, identical decision
    rule. No new heuristics, no size-header sniffing of its own.

    Exists because a multi-sheet workbook is not one document, it is N documents
    in a container, and there is no filename to lean on at sheet level. Judging
    purely on content is what this classifier is already good at: it correctly
    overrode a filename claiming "PACKING LIST" on a file that was a commercial
    invoice, and it correctly accepted a sheet named `PO#1657`.

    `sections` is [(label, rendered_text), ...]. **All sections go in ONE API
    call**, so classifying a workbook costs one call regardless of sheet count.
    Returns one verdict per input section, in input order.
    """
    usable = [(label, text) for label, text in sections if text.strip()]
    if not usable:
        return [
            SectionClassification(label, DocType.OTHER, False, "no readable content")
            for label, _ in sections
        ]

    if extractor is None:
        from claude_extractor import ClaudeExtractor

        extractor = ClaudeExtractor()

    content = [
        {
            "type": "text",
            "text": f"{len(usable)} section(s) of one container document to classify, in order:\n"
            + "\n".join(f"  {i}. {label}" for i, (label, _) in enumerate(usable, 1)),
        }
    ]
    for i, (label, text) in enumerate(usable, 1):
        content.append({"type": "text", "text": f"\n===== SECTION {i}: {label} =====\n{text}"})

    verdicts = extractor._parse_with_retry(
        schema=_ContentVerdicts,
        system=[
            {
                "type": "text",
                "text": CLASSIFIER_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        content=content,
    )

    if len(verdicts.verdicts) != len(usable):
        raise ValueError(
            f"classifier returned {len(verdicts.verdicts)} verdicts for {len(usable)} sections"
        )

    by_label: dict[str, SectionClassification] = {}
    for (label, _text), verdict in zip(usable, verdicts.verdicts):
        try:
            doc_type = DocType(verdict.doc_type)
        except ValueError:
            doc_type = DocType.OTHER
        by_label[label] = SectionClassification(
            label=label,
            doc_type=doc_type,
            has_size_breakdown=verdict.has_size_breakdown,
            reason=verdict.reason,
        )
    return [
        by_label.get(label, SectionClassification(label, DocType.OTHER, False, "no readable content"))
        for label, _ in sections
    ]


def _preview(path: Path, max_chars: int = 9000) -> str:
    """
    A short text preview, enough to classify without paying to read it all.

    **Samples every sheet / several pages, not just the first.** Reading only the
    first part misclassifies exactly the file this project cares most about:
    Inprotex's workbook opens on a `COMMERCIAL INVOICE` sheet and keeps its
    size-level data on a separate `PACKING` tab, so a first-sheet-only preview
    concluded "commercial invoice, no sizes" and would have excluded the one
    vendor whose parser is fully validated.
    """
    try:
        if path.suffix.lower() in (".xlsx", ".xlsm"):
            from claude_extractor import read_workbook_grids

            grids = [g for g in read_workbook_grids(path) if not g.is_empty]
            if not grids:
                return ""
            names = ", ".join(f"'{g.name}'" for g in grids)
            parts = [f"[workbook with {len(grids)} non-empty sheet(s): {names}]"]
            for grid in grids[:_PREVIEW_MAX_PARTS]:
                parts.append(sheet_preview(grid))
            if len(grids) > _PREVIEW_MAX_PARTS:
                parts.append(f"[{len(grids) - _PREVIEW_MAX_PARTS} further sheet(s) not previewed]")
            return "\n".join(parts)[:max_chars]

        if path.suffix.lower() == ".pdf":
            from claude_extractor import read_pdf_layouts

            pages = read_pdf_layouts(path)
            if not pages:
                return ""
            parts = [f"[PDF with {len(pages)} page(s) of text]"]
            for label, text in pages[:_PREVIEW_MAX_PARTS]:
                parts.append(f"----- {label} -----\n" + text[:_PREVIEW_CHARS_PER_PART])
            if len(pages) > _PREVIEW_MAX_PARTS:
                parts.append(f"[{len(pages) - _PREVIEW_MAX_PARTS} further page(s) not previewed]")
            return "\n".join(parts)[:max_chars]
    except Exception as exc:  # noqa: BLE001 -- an unreadable preview is a classification input
        logger.warning("Could not preview %s: %s", path.name, exc)
        return ""
    return ""


def open_failure_reason(path: Path) -> Optional[str]:
    """
    Why this attachment cannot be opened, or None if it opens fine.

    Called during triage so a corrupt or password-protected attachment is
    reported as its own specific condition ("could not open: encrypted") rather
    than being indistinguishable from a document that simply has no size data.
    """
    from claude_extractor import DocumentUnreadable, open_pdf, open_workbook

    suffix = path.suffix.lower()
    try:
        if suffix in (".xlsx", ".xlsm"):
            open_workbook(path, data_only=True, read_only=True).close()
            return None
        if suffix == ".pdf":
            with open_pdf(path) as pdf:
                _ = len(pdf.pages)
            return None
    except DocumentUnreadable as exc:
        return exc.reason
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return None


def classify_attachments(
    paths: Sequence[Union[str, Path]],
    extractor: Any = None,
    use_content_check: bool = True,
) -> ClassificationResult:
    """
    Classify a shipment email's attachments and select which to parse.

    Filename rules decide the unambiguous cases for free. Anything ambiguous, or
    any filename-claimed packing list (whose size-level-ness must be verified),
    goes to a single Claude call over short previews.

    `use_content_check=False` keeps it entirely free/offline, at the cost of
    trusting filenames — usable for tests, not recommended in the pipeline.
    """
    result = ClassificationResult()
    candidates: list[AttachmentClassification] = []
    needs_content: list[AttachmentClassification] = []

    for raw in paths:
        path = Path(raw)
        if not path.exists():
            result.warnings.append(f"attachment not found, skipped: {path}")
            continue

        doc_type, ambiguous, reason = classify_by_filename(path.name)
        item = AttachmentClassification(
            path=path,
            doc_type=doc_type,
            # A filename can never establish this; assume false until content says otherwise.
            has_size_breakdown=False,
            reason=reason,
            method="filename",
            is_rollup=looks_like_rollup(path.name),
        )

        # An inspection report is settled: banned regardless of content, so don't
        # spend a content check on it.
        if doc_type in BANNED_AS_DATA_SOURCE:
            candidates.append(item)
            continue

        # A file that won't open at all is its own specific condition. Report it
        # and keep going -- one corrupt attachment must not abort the batch.
        failure = open_failure_reason(path)
        if failure:
            item.unreadable_reason = failure
            item.reason = f"could not open: {failure}"
            result.warnings.append(
                f"COULD NOT OPEN {path.name}: {failure}. Excluded from this shipment; the other "
                f"attachments were still processed. If this was meant to be the packing list, "
                f"ask the vendor to resend it."
            )
            candidates.append(item)
            continue

        # Everything that could plausibly be the packing list needs its size-level
        # claim verified, and anything ambiguous needs its type verified.
        if ambiguous or doc_type == DocType.PACKING_LIST:
            needs_content.append(item)
        candidates.append(item)

    if needs_content and use_content_check:
        _apply_content_verdicts(needs_content, extractor, result.warnings)
    elif needs_content:
        # No content check: trust the filename's type, and accept a size claim
        # only for names that clearly mean the real packing list.
        for item in needs_content:
            if item.doc_type == DocType.PACKING_LIST:
                item.has_size_breakdown = True
                item.reason += " (assumed size-level: content check disabled)"
                item.method = "filename"
        result.warnings.append(
            "content check disabled — attachment types and size-level claims were taken from "
            "filenames alone, which is known to be unreliable in both directions"
        )

    for item in candidates:
        (result.selected if item.usable_as_shipment_data else result.excluded).append(item)

    if result.needs_manual_entry:
        result.warnings.append(
            "no attachment provides per-size quantities — this shipment cannot be resolved to "
            "style/colour/size lines and must go to manual entry. Per Paula's ruling the gap is "
            "NOT filled from an inspection report and NOT inferred by splitting colour totals."
        )
    if len(result.selected) > 1:
        result.warnings.append(
            f"{len(result.selected)} usable packing lists found; parsing "
            f"{result.primary.path.name} as primary. Others available as cross-checks: "
            + ", ".join(c.path.name for c in result.cross_checks)
        )
    return result


def _apply_content_verdicts(
    items: list[AttachmentClassification], extractor: Any, warnings: list[str]
) -> None:
    """One Claude call over all previews; falls back to filename-only on failure."""
    previews = [(item, _preview(item.path)) for item in items]
    usable = [(item, text) for item, text in previews if text.strip()]
    for item, text in previews:
        item.preview_chars = len(text)
        if not text.strip():
            item.reason += " (no readable preview — could not verify content)"
            warnings.append(
                f"{item.path.name}: no extractable text to classify from; not selected as a "
                f"shipment-data source"
            )

    if not usable:
        return

    if extractor is None:
        from claude_extractor import ClaudeExtractor

        extractor = ClaudeExtractor()

    content = [
        {
            "type": "text",
            "text": f"{len(usable)} attachment(s) to classify, in order:\n"
            + "\n".join(f"  {i}. {item.path.name}" for i, (item, _) in enumerate(usable, 1)),
        }
    ]
    for i, (item, text) in enumerate(usable, 1):
        content.append(
            {
                "type": "text",
                "text": f"\n===== ATTACHMENT {i}: {item.path.name} =====\n{text}",
            }
        )

    try:
        verdicts = extractor._parse_with_retry(
            schema=_ContentVerdicts,
            system=[
                {
                    "type": "text",
                    "text": CLASSIFIER_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            content=content,
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(
            f"content-based classification failed ({type(exc).__name__}: {exc}); fell back to "
            f"filenames, which is unreliable — verify the selection before trusting it"
        )
        return

    if len(verdicts.verdicts) != len(usable):
        warnings.append(
            f"classifier returned {len(verdicts.verdicts)} verdicts for {len(usable)} attachments; "
            f"ignoring them and falling back to filenames"
        )
        return

    for (item, _text), verdict in zip(usable, verdicts.verdicts):
        try:
            content_type = DocType(verdict.doc_type)
        except ValueError:
            content_type = DocType.OTHER
        if content_type != item.doc_type:
            item.reason = (
                f"content says {content_type.value} (filename suggested {item.doc_type.value}): "
                f"{verdict.reason}"
            )
            item.doc_type = content_type
        else:
            item.reason = f"{item.reason}; content confirms: {verdict.reason}"
        item.has_size_breakdown = verdict.has_size_breakdown
        item.method = "filename+content"
