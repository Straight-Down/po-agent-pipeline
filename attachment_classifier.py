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
   candidate, not a verdict; every case is settled by looking at content.

   Stated as the rule the code now enforces: **a filename hint may prioritise or
   deprioritise an attachment. It may never exclude one.** Only content, or a
   file that will not open, excludes. This is not a refinement — an earlier
   version let a confident-looking filename skip the content check entirely, and
   a file named `... - Clearance Invoice.xlsx` was dropped unopened. It held
   three packing-list sheets with full per-size grids, each headed
   `Packing list`. The content check is the part of this classifier that
   demonstrably works; short-circuiting it with the signal known to be
   unreliable inverted the design.

   The cost is one preview per readable attachment instead of per suspicious
   one. That is a few thousand tokens in a single API call, against the price of
   discarding a vendor's only size-level source.

2. **Inspection reports are never a data source.** Paula's explicit ruling
   (2026-08-11), not a design preference open to revisiting. An inspection
   report is excluded here regardless of what it contains — even though the
   W600001 one demonstrably held correct size data. If the actual packing list
   can't resolve to style/colour/size lines, the shipment goes to manual entry;
   the gap is never filled from a QC document, and never guessed at by splitting
   a colour total proportionally across sizes.

The classifier is deliberately conservative about what it promotes: a document
is only selected as a shipment-data source if it opens, is not banned, and its
content carries per-size quantities. Its TYPE is recorded but does not gate
(2026-09-23) -- a workbook typed as an invoice can still hold the only
size-level sheets for a PO. Which sheets of it are read is decided per sheet.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from canonical import canonical
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


def classifier_prompt_hash() -> str:
    """
    SHA-256 of everything that defines the question the content check asks.

    The system prompt AND the response schema, because the schema's field
    descriptions are instructions too -- `has_size_breakdown`'s is the sentence
    that decides the gate. Persisted beside every stored verdict so a verdict can
    be traced to the exact wording that produced it.
    """
    payload = CLASSIFIER_SYSTEM_PROMPT + "\n" + json.dumps(
        _ContentVerdicts.model_json_schema(), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StoredVerdict:
    """
    A content verdict already on record for some bytes (`attachments` row).

    **Why reuse rather than re-ask.** The content check is a model call, and a
    model call is not a function: on 2026-09-23 the same workbook, with the same
    code, came back `commercial_invoice` on one call and `packing_list` on
    another -- in the SAME ingest, because the verdict was being asked for twice.
    Once a verdict exists for a SHA-256 it is the verdict for those bytes, so a
    re-extraction reproduces the original gate decision instead of re-rolling it.

    Only the model's part is stored. The filename rules (including the
    inspection-report ban) and the can-it-open check are deterministic and free,
    so they are recomputed every time and never come from here.
    """

    doc_type: str  # a DocType value, e.g. "packing_list"
    has_size_breakdown: bool
    rationale: str  # the model's own sentence, verbatim
    model: Optional[str]
    prompt_hash: str


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
    #: The name a HUMAN would recognise, which is not always the path's own.
    #: Once attachments are stored content-addressed (`poller.BlobStore`) the
    #: path IS a SHA-256 and carries no signal whatever -- so the vendor's
    #: filename has to travel separately from the bytes. Defaults to the path's
    #: own name, which is what every caller meant before the store existed.
    display_name: str = ""
    #: What the NAME suggested, kept even after content overrides `doc_type`.
    #: Retained for two reasons: it is the audit trail for a disagreement (the
    #: reviewer sees that the name said invoice and the content said packing
    #: list), and it is the only thing a filename is now allowed to influence --
    #: the ordering in `ClassificationResult.primary` -- with ONE exception that
    #: comes from a person rather than the data: a name that says inspection
    #: report excludes the file outright (`is_banned`, Paula's ruling).
    filename_hint: Optional[DocType] = None
    #: SHA-256 of the bytes -- the key a stored verdict is looked up by.
    content_sha256: Optional[str] = None
    #: Provenance of the CONTENT verdict, set only when one was applied.
    #: `verdict_source` is "claude" (asked this run) or "stored" (reused from the
    #: attachment row); None means no model verdict exists for this item -- banned
    #: by filename, unreadable, no preview, or the call failed -- and such an item
    #: must never be persisted as a reusable verdict.
    content_rationale: Optional[str] = None
    classifier_model: Optional[str] = None
    classifier_prompt_hash: Optional[str] = None
    verdict_source: Optional[str] = None

    @property
    def usable_as_shipment_data(self) -> bool:
        """
        Whether this attachment may be parsed for shipment quantities.

        Requires three things: it opens, it is not a banned document, and the
        content check found **per-size quantities** in it. `doc_type` is NOT one
        of them -- it is recorded as metadata and no longer gates.

        Why the type stopped gating: a workbook is one file with one type but
        several sheets. PO 1624's clearance workbook is a commercial invoice
        sheet plus three size-level packing sheets; the classifier said so in its
        own rationale ("per-PO 'Packing list' sheets whose row 28 headers list
        footwear sizes 8-14"), set `has_size_breakdown`, typed the FILE
        `commercial_invoice`, and the type check threw the packing sheets away.
        Which sheets of an admitted workbook to read is decided per sheet, by
        `SectionClassification.is_shipment_data`, inside the extractor.

        **The ban now carries weight on its own.** It used to be belt and braces
        behind the packing-list test; with that test gone it is the only thing
        keeping an inspection report out. It fires on either signal -- the
        filename's claim, or the content's -- because Paula's ruling is
        "regardless of content", and an inspection report identified by what it
        contains is still an inspection report.
        """
        return (
            self.unreadable_reason is None
            and not self.is_banned
            and self.has_size_breakdown
        )

    @property
    def is_banned(self) -> bool:
        """Paula's permanent ban, by the filename's claim or by content."""
        return (self.doc_type in BANNED_AS_DATA_SOURCE
                or self.filename_hint in BANNED_AS_DATA_SOURCE)

    @property
    def excluded_reason(self) -> str:
        """
        Why this attachment was not parsed. Empty for an admitted one.

        Mirrors `usable_as_shipment_data` test for test, so the reason can only
        fire for a file the gate actually refused. The old "not a packing list"
        wording is gone with the type test it described: a commercial invoice
        WITH per-size quantities is admitted now, and one without is excluded for
        lacking sizes, which is the real reason -- its type is stated alongside as
        context, not as the cause.
        """
        if self.unreadable_reason:
            return f"could not open: {self.unreadable_reason}"
        if self.is_banned:
            banned = (self.doc_type if self.doc_type in BANNED_AS_DATA_SOURCE
                      else self.filename_hint)
            return (
                f"{banned.value} — permanently excluded as a shipment-data source "
                f"(Paula's ruling 2026-08-11), regardless of content"
            )
        if not self.has_size_breakdown:
            return f"no per-size quantities ({self.doc_type.value}): {self.reason}"
        return ""


@dataclass
class ClassificationResult:
    selected: list[AttachmentClassification] = field(default_factory=list)
    excluded: list[AttachmentClassification] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Tokens the content-check call spent, as a delta. Carried on the result
    #: because the caller that classifies is no longer the one that totals the
    #: shipment's cost -- `parse_shipment_email` adds it back in. Empty when no
    #: call was made (every verdict stored, or content check disabled).
    usage: dict = field(default_factory=dict)

    @property
    def primary(self) -> Optional[AttachmentClassification]:
        """
        The attachment to parse. Prefers a style/colour/size rollup over
        carton-by-carton detail: both extract correctly (verified live on
        Symmetry's pair, which agreed exactly), but the rollup is already in the
        target shape and costs far fewer tokens.

        This ordering is the whole of what a filename is allowed to do. Among
        attachments that content has already confirmed carry per-size
        quantities, one whose *name* also says packing list goes first, and one whose
        name said invoice goes last. Deprioritising, never excluding: a
        misleadingly named file that content vouched for is still selected, still
        parsed, and still available as a cross-check.
        """
        if not self.selected:
            return None
        return sorted(
            self.selected,
            key=lambda c: (
                not c.is_rollup,
                c.filename_hint != DocType.PACKING_LIST,
                c.display_name,
            ),
        )[0]

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
            bits.append(f"primary: {self.primary.display_name}")
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

#: Letter-size spellings vendors use that the account's own list does not hold.
#: NetSuite is canonical on `2X`/`3X`; Inprotex writes `XXL`/`XXXL`, and other
#: vendors write `OS`/`ONE SIZE` for what NetSuite calls `ALL`. These are
#: *recognition* aliases for finding a header row, NOT a mapping to a NetSuite
#: size -- that is `matcher.SIZE_ALIASES`, which is not touched here.
_EXTRA_SIZE_SPELLINGS = {
    "XXL", "XXXL", "XXXXL", "2XL", "3XL", "4XL", "4X", "OS", "ONE SIZE",
}


def _size_vocabulary() -> frozenset[str]:
    """
    The account's real size labels, in canonical form, plus vendor spellings.

    Read from the generated `netsuite_size_list.json` snapshot of
    `customlist_psgss_product_size` -- not a hardcoded set. A hardcoded set is
    what caused this detector to be blind to every numeric size the company
    sells; see `size_vocabulary.py`. If the snapshot is missing the detector
    falls back to letter sizes alone and says so loudly, because a silent
    fallback would restore the original bug.
    """
    import size_vocabulary as sv

    try:
        return sv.size_labels_canon(extra=_EXTRA_SIZE_SPELLINGS)
    except sv.SizeVocabularyUnavailable as exc:
        logger.warning(
            "%s -- size-header detection is running on vendor letter-size spellings "
            "ONLY, so numerically-sized sheets (footwear, bottoms) will not be "
            "recognised. Refresh the snapshot.",
            exc,
        )
        return frozenset(canonical(t) for t in _EXTRA_SIZE_SPELLINGS)


#: How many distinct size labels must appear on one row for it to be the size
#: header.
_SIZE_HEADER_MIN_TOKENS = 3


def _classify_size_cell(text: str, vocabulary: frozenset[str]) -> tuple[Optional[str], bool]:
    """
    `(label, is_bare_number)` for one cell, or `(None, False)` if not a size.

    A bare number is flagged because it is the only ambiguous kind. `S` or
    `32-34` in a cell means one thing; `12` could equally be a carton count, a
    quantity or a shoe size, and the caller applies an extra test to those.
    """
    import size_vocabulary as sv

    stripped = str(text or "").strip()
    if not stripped:
        return None, False

    numeric = sv.numeric_label(stripped)
    if numeric is not None:
        canon = canonical(numeric)
        return (canon if canon in vocabulary else None), True

    canon = canonical(stripped)
    return (canon if canon in vocabulary else None), False


def _find_size_header_row(grid: Any, vocabulary: Optional[frozenset[str]] = None) -> Optional[int]:
    """
    1-based index of the row that looks like a sheet's size header, if any.

    Vendors put pages of letterhead above the actual table -- Inprotex's PACKING
    tab has its size columns well below the first 20 rows. Previewing only the
    top of the sheet therefore shows no size evidence and the classifier
    concludes, correctly but uselessly, that it cannot see any. Finding this row
    puts the decisive evidence in front of it instead.

    Cell text is compared in CANONICAL form against the account's own size list.
    Two separate failures made that necessary. The first was encoding: a
    full-width `２Ｘ` or a double-spaced `One  Size` went unrecognised, which
    canonicalisation fixed. The second was vocabulary, and it was worse -- the
    list was hand-written letter sizes, so **every numeric size the company
    sells was invisible**. Footwear's `8`…`14` row and Tainan's `30`…`42` waist
    row both read as "no size evidence", the sheets classified as sizeless
    packing lists, and two vendors' only size-level source was dropped.

    Bare numbers need one more test than letters do, because a row of quantities
    or carton counts is also a row of bare numbers. Two things separate them:

      - **List validity.** A quantity happens to be a valid size only by
        coincidence. On Tainan's own sheet, the quantity row `30 | 30 | 3 | 90 |
        6` yields just two list-valid values and the net-weight row `0.39 …
        0.48` yields none.
      - **Monotonicity.** A size scale is printed in ascending order; quantities
        are in whatever order the cartons came out. Required only when *every*
        hit is a bare number -- a row containing `S` or `32-34` has already
        identified itself.

    Neither test is airtight, and a deliberately adversarial row (ascending,
    all-list-valid quantities) would still match. That is an acceptable residual
    because of where this sits: a hit only adds a preview region for the
    classifier to read, so a false positive costs a few hundred tokens, while a
    false negative sends a whole shipment to manual entry.
    """
    rows = size_header_rows(grid, vocabulary)
    return rows[0][0] if rows else None


def size_header_rows(
    grid: Any, vocabulary: Optional[frozenset[str]] = None
) -> list[tuple[int, list[int]]]:
    """
    EVERY size-header row on the sheet, as `(1-based row, [0-based columns])`.

    `_find_size_header_row` takes the first of these; callers that need the
    *columns* need all of them, because one sheet can carry more than one size
    header over different column spans. Tainan's does exactly that: the carton
    grid's waists sit at I..O and the recap block repeats them at K..Q. Anything
    reasoning about "which columns hold quantities" has to see both, or it will
    look in the wrong half of the sheet.
    """
    if vocabulary is None:
        vocabulary = _size_vocabulary()

    found: list[tuple[int, list[int]]] = []
    for index, row in enumerate(grid.rows, start=1):
        labels: set[str] = set()
        columns: list[int] = []
        numeric_run: list[tuple[int, float]] = []
        all_numeric = True
        for column, cell in enumerate(row):
            label, is_number = _classify_size_cell(cell, vocabulary)
            if label is None:
                continue
            labels.add(label)
            columns.append(column)
            if is_number:
                numeric_run.append((column, float(str(cell).strip())))
            else:
                all_numeric = False

        if len(labels) < _SIZE_HEADER_MIN_TOKENS:
            continue
        if all_numeric and not _ascends(numeric_run):
            continue
        found.append((index, columns))
    return found


def quantity_columns(grid: Any, vocabulary: Optional[frozenset[str]] = None) -> set[int]:
    """
    The 0-based columns that hold per-size quantities on this sheet.

    This is what "a shipped column" means concretely, and it is the discriminator
    that makes a fractional-quantity signal usable: a packing sheet is full of
    legitimately fractional numbers -- net and gross weights, cubic metres, unit
    prices -- and they live in their own columns, outside the size grid. Without
    this restriction that signal fired 47 times on one clean document.

    **Only the FULLEST header runs count**, and only the columns where a label
    actually sat. Both restrictions were learned by measuring. `size_header_rows`
    is deliberately permissive because a false positive there only adds a preview
    region, and on Inprotex's carton-per-row sheet it finds 48 "headers" -- rows
    where a carton number, a net weight and a gross weight happen to be three
    ascending values that all exist in the size list (`E=4, N=6, O=7`). Taking
    every such row, and filling in the columns between the outermost labels,
    dragged the carton-number, line-total and both weight columns into the
    "shipped" region and produced seven spurious notes. A real size header carries
    the whole run (6 or 7 labels); a coincidence carries exactly the minimum three.
    """
    spans = [cols for _row, cols in size_header_rows(grid, vocabulary) if cols]
    if not spans:
        return set()
    widest = max(len(cols) for cols in spans)
    return {c for cols in spans if len(cols) == widest for c in cols}


def _ascends(cells: Sequence[tuple[int, float]]) -> bool:
    """Do these (column, value) pairs strictly ascend left to right?"""
    values = [value for _column, value in sorted(cells)]
    return all(earlier < later for earlier, later in zip(values, values[1:]))


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
    #: "claude" (asked now), "stored" (replayed from `SheetVerdicts`), or "none"
    #: for a placeholder the model never saw (an empty section). Only a model's
    #: answer is ever stored as a verdict.
    source: str = "claude"

    @property
    def is_shipment_data(self) -> bool:
        """
        Whether this section may be extracted for shipment quantities.

        A packing list, carrying per-size quantities, and not a banned type.

        **No longer the same test as `usable_as_shipment_data`** (it was, until
        2026-09-23). The file-level gate dropped its type check so a workbook
        that is an invoice on its first sheet can still be admitted for its
        packing sheets; THIS test, per sheet, keeps the type check, and is what
        stops that workbook's invoice sheet being read as data. Changing it was
        not part of that ruling -- an invoice view that repeats per-size figures
        is exactly how Inprotex's workbook once produced a 4x-inflated total.
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
            SectionClassification(label, DocType.OTHER, False, "no readable content",
                                  source="none")
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
        by_label.get(label, SectionClassification(label, DocType.OTHER, False,
                                                  "no readable content", source="none"))
        for label, _ in sections
    ]


@dataclass(frozen=True)
class StoredSheetVerdict:
    """
    One worksheet's content verdict, keyed (bytes SHA-256, sheet, prompt hash, model).

    The per-sheet counterpart of `StoredVerdict`, for the same reason: a model
    call is not a function. Since the file gate stopped checking type
    (2026-09-23), the per-sheet check is the ONLY thing keeping a workbook's
    COMMERCIAL INVOICE sheet out of the quantities -- and until migration 0012 it
    was re-rolled on every ingest. A verdict is reused only when all four key
    parts match; a new prompt or a new model asks again.
    """

    sheet: str
    doc_type: str  # a DocType value
    has_size_breakdown: bool
    rationale: str  # the model's own sentence, verbatim
    model: Optional[str]
    prompt_hash: str

    def as_section(self) -> SectionClassification:
        try:
            doc_type = DocType(self.doc_type)
        except ValueError:
            doc_type = DocType.OTHER
        return SectionClassification(self.sheet, doc_type, self.has_size_breakdown,
                                     self.rationale, source="stored")

    def to_json(self) -> dict:
        return {"sheet": self.sheet, "doc_type": self.doc_type,
                "has_size_breakdown": self.has_size_breakdown, "rationale": self.rationale,
                "model": self.model, "prompt_hash": self.prompt_hash}

    @classmethod
    def from_json(cls, data: dict) -> "StoredSheetVerdict":
        return cls(sheet=str(data["sheet"]), doc_type=str(data["doc_type"]),
                   has_size_breakdown=bool(data["has_size_breakdown"]),
                   rationale=str(data.get("rationale") or ""), model=data.get("model"),
                   prompt_hash=str(data["prompt_hash"]))


class SheetVerdicts:
    """
    Per-sheet verdicts on record for one ingest, plus the ones it makes.

    `stored` is what the database held when the ingest began, by SHA-256.
    `fresh` collects verdicts asked for during the ingest, for the caller to
    persist. `lookup` consults both, so a second parse of the same bytes inside
    one ingest (a cross-check) replays rather than re-asks too.
    """

    def __init__(self, stored: Optional[dict] = None):
        self.stored: dict[str, list[StoredSheetVerdict]] = dict(stored or {})
        self.fresh: dict[str, list[StoredSheetVerdict]] = {}

    def lookup(self, sha: str, sheet: str, *, model: Optional[str],
               prompt_hash: str) -> Optional[StoredSheetVerdict]:
        for verdict in self.fresh.get(sha, []) + self.stored.get(sha, []):
            if ((verdict.sheet, verdict.model or "", verdict.prompt_hash)
                    == (sheet, model or "", prompt_hash)):
                return verdict
        return None

    def record(self, sha: str, verdict: StoredSheetVerdict) -> None:
        self.fresh.setdefault(sha, []).append(verdict)


def merge_sheet_verdicts(existing: Sequence[StoredSheetVerdict],
                         fresh: Sequence[StoredSheetVerdict]) -> list[StoredSheetVerdict]:
    """`existing` with `fresh` added; a fresh verdict REPLACES one with the same key."""

    def key(verdict: StoredSheetVerdict) -> tuple:
        return (verdict.sheet, verdict.model or "", verdict.prompt_hash)

    merged = {key(v): v for v in existing}
    merged.update({key(v): v for v in fresh})
    return sorted(merged.values(), key=key)


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
    from claude_extractor import FORMAT_PDF, WORKBOOK_FORMATS, sniff_format

    fmt = sniff_format(path)
    try:
        if fmt in WORKBOOK_FORMATS:
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

        if fmt == FORMAT_PDF:
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

    Routes on the file's own signature, not its extension. That matters here more
    than anywhere: this function is the ONLY thing allowed to exclude an
    attachment before content is read, so getting "unopenable" wrong is getting
    an exclusion wrong. A legacy `.xls` used to land on openpyxl and report
    itself unreadable, which excluded a perfectly good packing list.
    """
    from claude_extractor import (
        FORMAT_PDF,
        WORKBOOK_FORMATS,
        DocumentUnreadable,
        open_pdf,
        read_workbook_grids,
        sniff_format,
    )

    fmt = sniff_format(path)
    try:
        if fmt in WORKBOOK_FORMATS:
            # Reads the whole workbook rather than just opening it. Costs more
            # than the old `read_only` open, and buys the thing that open alone
            # missed: a file that opens but whose sheets cannot be read.
            read_workbook_grids(path)
            return None
        if fmt == FORMAT_PDF:
            with open_pdf(path) as pdf:
                _ = len(pdf.pages)
            return None
    except DocumentUnreadable as exc:
        return exc.reason
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return (
        f"not a workbook or a PDF — the file's leading bytes match no format this "
        f"pipeline can read (extension {path.suffix or 'none'!r})"
    )


def classify_attachments(
    paths: Sequence[Union[str, Path]],
    extractor: Any = None,
    use_content_check: bool = True,
    display_names: Optional[dict] = None,
    stored_verdicts: Optional[dict] = None,
) -> ClassificationResult:
    """
    Classify a shipment email's attachments and select which to parse.

    Filename rules decide the unambiguous cases for free. Anything ambiguous, or
    any filename-claimed packing list (whose size-level-ness must be verified),
    goes to a single Claude call over short previews.

    `use_content_check=False` keeps it entirely free/offline, at the cost of
    trusting filenames — usable for tests, not recommended in the pipeline.

    `display_names` maps a resolved path to the name a human would recognise.
    **It exists because the bytes and the name stopped being the same object.**
    Attachments fetched from the mailbox are stored content-addressed, so their
    path is a SHA-256 with no extension-independent signal in it; passing that as
    the filename silently disabled every filename rule in this module -- the
    inspection-report ban, the payment-request rule, the rollup preference, the
    Inprotex trap. Nothing failed; the classifier simply lost one of its two
    inputs and said nothing, which is why it survived a live run and was visible
    only in a warning string. Defaults to each path's own name.

    `stored_verdicts` maps a content SHA-256 to a `StoredVerdict` already on
    record. An attachment whose bytes have one is NOT sent to Claude: the stored
    verdict is applied through the same code as a fresh one, so the gate decides
    exactly as it did the first time. Only attachments with no stored verdict go
    in the (single) content call.
    """
    names = {Path(k).resolve(): v for k, v in (display_names or {}).items()}
    stored = stored_verdicts or {}
    result = ClassificationResult()
    candidates: list[AttachmentClassification] = []
    needs_content: list[AttachmentClassification] = []

    for raw in paths:
        path = Path(raw)
        if not path.exists():
            result.warnings.append(f"attachment not found, skipped: {path}")
            continue

        # The `ambiguous` flag is deliberately not read here any more. It used to
        # decide who got a content check; now everyone does, so there is nothing
        # left for it to gate. It remains part of `classify_by_filename`'s answer
        # because "the name genuinely cannot decide this" is still true and still
        # worth stating in the reason text.
        shown = names.get(path.resolve(), path.name)
        doc_type, _ambiguous, reason = classify_by_filename(shown)
        item = AttachmentClassification(
            path=path,
            doc_type=doc_type,
            # A filename can never establish this; assume false until content says otherwise.
            has_size_breakdown=False,
            reason=reason,
            method="filename",
            display_name=shown,
            is_rollup=looks_like_rollup(shown),
            filename_hint=doc_type,
            content_sha256=_sha256_file(path),
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
                f"COULD NOT OPEN {shown}: {failure}. Excluded from this shipment; the other "
                f"attachments were still processed. If this was meant to be the packing list, "
                f"ask the vendor to resend it."
            )
            candidates.append(item)
            continue

        # EVERY readable attachment goes to the content check, whatever its name
        # said. A filename hint that looked confident is exactly the case that
        # went wrong: "... - Clearance Invoice.xlsx" matched the invoice rule,
        # skipped this step, and took three packing-list sheets down with it.
        # The name's only remaining jobs are to seed a prior (overridden below by
        # whatever the content says) and to order the survivors.
        #
        # ...unless these bytes already HAVE a content verdict. Then it is
        # applied, not re-asked: asking twice is how one workbook came to be both
        # a packing list and a commercial invoice in the same run.
        prior = stored.get(item.content_sha256) if use_content_check else None
        if prior is not None:
            _apply_verdict(
                item, prior.doc_type, prior.has_size_breakdown, prior.rationale,
                model=prior.model, prompt_hash=prior.prompt_hash, source="stored",
            )
        else:
            needs_content.append(item)
        candidates.append(item)

    if needs_content and use_content_check:
        _apply_content_verdicts(needs_content, extractor, result.warnings, result)
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
            f"{result.primary.display_name} as primary. Others available as cross-checks: "
            + ", ".join(c.display_name for c in result.cross_checks)
        )
    return result


def _sha256_file(path: Path) -> str:
    """Content hash of an attachment -- the key its stored verdict lives under."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _apply_verdict(
    item: AttachmentClassification,
    doc_type: str,
    has_size_breakdown: bool,
    rationale: str,
    *,
    model: Optional[str],
    prompt_hash: Optional[str],
    source: str,
) -> None:
    """
    Apply one content verdict to an item. The ONLY place a verdict lands.

    A fresh verdict and a stored one go through this same function, so replaying
    a stored verdict cannot decide differently from the call that produced it:
    same override of the filename's type, same size claim, same reason text.
    """
    try:
        content_type = DocType(doc_type)
    except ValueError:
        content_type = DocType.OTHER
    if content_type != item.doc_type:
        item.reason = (
            f"content says {content_type.value} (filename suggested {item.doc_type.value}): "
            f"{rationale}"
        )
        item.doc_type = content_type
    else:
        item.reason = f"{item.reason}; content confirms: {rationale}"
    item.has_size_breakdown = bool(has_size_breakdown)
    item.method = "filename+content"
    item.content_rationale = rationale
    item.classifier_model = model
    item.classifier_prompt_hash = prompt_hash
    item.verdict_source = source


def _apply_content_verdicts(
    items: list[AttachmentClassification],
    extractor: Any,
    warnings: list[str],
    result: Optional[ClassificationResult] = None,
) -> None:
    """One Claude call over all previews; falls back to filename-only on failure."""
    previews = [(item, _preview(item.path)) for item in items]
    usable = [(item, text) for item, text in previews if text.strip()]
    for item, text in previews:
        item.preview_chars = len(text)
        if not text.strip():
            item.reason += " (no readable preview — could not verify content)"
            warnings.append(
                f"{item.display_name}: no extractable text to classify from; not selected as a "
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
            # The LABEL the model sees. `display_name`, not `path.name`: once
            # attachments are stored content-addressed the path is a SHA-256, and
            # labelling the evidence with a hash strips the filename out of the
            # one call that is allowed to weigh it. Not cosmetic -- it flipped
            # Symmetry's rollup from PACKING_LIST to SHIPPING_ADVICE, which
            # excluded it from packing-list duty and from the rollup preference.
            + "\n".join(f"  {i}. {item.display_name}" for i, (item, _) in enumerate(usable, 1)),
        }
    ]
    for i, (item, text) in enumerate(usable, 1):
        content.append(
            {
                "type": "text",
                "text": f"\n===== ATTACHMENT {i}: {item.display_name} =====\n{text}",
            }
        )

    usage_before = dict(getattr(extractor, "last_usage", {}) or {})
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
    finally:
        # A delta, not the extractor's running total (`last_usage` accumulates).
        # Recorded even when the call raised: an attempt may have billed first.
        if result is not None:
            after = dict(getattr(extractor, "last_usage", {}) or {})
            result.usage = {k: after.get(k, 0) - usage_before.get(k, 0)
                            for k in set(after) | set(usage_before)
                            if after.get(k, 0) - usage_before.get(k, 0) != 0}

    if len(verdicts.verdicts) != len(usable):
        warnings.append(
            f"classifier returned {len(verdicts.verdicts)} verdicts for {len(usable)} attachments; "
            f"ignoring them and falling back to filenames"
        )
        return

    model = getattr(extractor, "model", None)
    prompt_hash = classifier_prompt_hash()
    for (item, _text), verdict in zip(usable, verdicts.verdicts):
        _apply_verdict(
            item, verdict.doc_type, verdict.has_size_breakdown, verdict.reason,
            model=model, prompt_hash=prompt_hash, source="claude",
        )
