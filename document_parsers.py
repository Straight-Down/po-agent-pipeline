"""
Vendor-document router: known format -> free deterministic parser, everything
else -> Claude-assisted extractor.

This is the entry point the rest of the pipeline should call. It owns the
decision of which parser runs, validates whatever comes back, and normalizes it
into a single `ParseResult` (see `extraction_schema.py`).

Routing, for both document types:

    packing slip (.xlsx)          shipping advice (.pdf)
    ----------------------        ----------------------------
    matches Inprotex layout?      deterministic regex extracts
      yes -> parse_packing_slip     ETD + ETA + HAWB?
              (free, validated)       yes -> use it (free)
      no   -> Claude extractor        no  -> Claude on the text
    deterministic output fails             -> Claude on the PDF itself
    validation -> Claude extractor           if the text layer is empty

The deterministic paths are a fast/free special case for formats already
verified against real documents — not a template to replicate per vendor. Paula
confirmed every vendor's layout differs, so the Claude path is the primary one
(architecture doc section 4.1).

Two invariants held throughout:

  - A deterministic parser's output is only trusted if it validates completely.
    A partial read is routed to Claude instead of being emitted as
    high-confidence, because "high confidence" from the Inprotex parser means
    "hand-verified 77/77 against the vendor's own summary email" and a
    half-parsed file has not earned that.
  - Anything uncertain sets `needs_review`. Nothing is dropped silently.

CLI:
    python document_parsers.py <packing_slip.xlsx> [<shipping_advice.pdf>] [--json out.json]
    python document_parsers.py <file.xlsx> --force-claude
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional, Sequence, Union

import carton_backing
from canonical import canonical_key
from claude_extractor import (
    FORMAT_PDF,
    FORMAT_XLSX,
    WORKBOOK_FORMATS,
    ClaudeExtractor,
    DocumentUnreadable,
    ExtractionError,
    NoPackingSheetFound,
    credentials_available,
    open_pdf,
    open_workbook,
    sniff_format,
)
from extraction_schema import (
    ParseResult,
    aggregate_lines,
    ShippingAdviceExtraction,
    deterministic_line_to_dict,
    enforce_size_composition,
    line_to_dict,
    meaningful,
)

logger = logging.getLogger(__name__)

#: Markers that identify Inprotex's PACKING-tab layout. All must be present.
INPROTEX_MARKERS = ("PO#", "STYLE#", "C/NO.", "COLOR", "TOTAL")
INPROTEX_SHEET = "PACKING"

#: How many rows of the candidate sheet to scan when sniffing the format.
SNIFF_ROWS = 200


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def looks_like_inprotex(xlsx_path: Union[str, Path]) -> tuple[bool, str]:
    """
    Does this workbook match the one layout we have a validated parser for?

    Deliberately strict: a false positive here means running a parser tuned to a
    different layout, which produces confidently wrong lines. A false negative
    just costs one Claude call.

    Answers False for anything that is not an OOXML workbook, without opening it.
    Inprotex sends `.xlsx`, and the deterministic parser is built on openpyxl, so
    a legacy `.xls` cannot be this layout by construction -- saying so plainly is
    better than reporting it as a file that would not open.
    """
    if sniff_format(xlsx_path) != FORMAT_XLSX:
        return False, "not an OOXML .xlsx workbook, so not the Inprotex layout"

    try:
        wb = open_workbook(xlsx_path, data_only=True, read_only=True)
    except DocumentUnreadable as exc:
        return False, f"could not open workbook: {exc.reason}"

    try:
        if INPROTEX_SHEET not in wb.sheetnames:
            return False, f"no '{INPROTEX_SHEET}' sheet (sheets: {wb.sheetnames})"
        ws = wb[INPROTEX_SHEET]
        seen: set[str] = set()
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= SNIFF_ROWS:
                break
            for cell in row:
                if isinstance(cell, str):
                    upper = cell.strip().upper()
                    for marker in INPROTEX_MARKERS:
                        if marker in upper:
                            seen.add(marker)
        missing = [m for m in INPROTEX_MARKERS if m not in seen]
        if missing:
            return False, f"'{INPROTEX_SHEET}' sheet present but missing markers: {missing}"
        return True, f"matched Inprotex layout ({INPROTEX_SHEET} sheet, all markers present)"
    finally:
        wb.close()


def validate_deterministic_lines(raw_lines: list[dict]) -> list[str]:
    """
    Check a deterministic parser's output is complete. Returns problem strings.

    Any problem routes the whole file to Claude — a validated-format parser that
    partially fails has hit a layout variant it wasn't built for, and its other
    lines are no longer trustworthy either.
    """
    problems: list[str] = []
    if not raw_lines:
        return ["parser returned no lines"]
    for i, line in enumerate(raw_lines):
        for key in ("po_number", "style_number", "color", "size"):
            if not str(line.get(key) or "").strip():
                problems.append(f"line {i}: empty {key} ({line!r})")
        qty = line.get("quantity")
        if not isinstance(qty, (int, float)) or qty <= 0:
            problems.append(f"line {i}: non-positive/invalid quantity {qty!r}")
    return problems[:10]  # cap the noise; the first few identify the pattern


# ---------------------------------------------------------------------------
# Packing slips
# ---------------------------------------------------------------------------


#: Token accounting that is free and never confused with "not measured".
FREE_USAGE = {"input_tokens": 0, "output_tokens": 0}


def usage_delta(before: dict, after: dict) -> dict:
    """
    Tokens spent BY ONE PARSE, not by the extractor's whole lifetime.

    `ClaudeExtractor.last_usage` is a misnomer: it ACCUMULATES across every call
    the instance ever makes and is never reset. Both call sites here used to copy
    it wholesale into `ParseResult.usage`, so a document parsed late in a run
    reported the running total of everything before it. That is why per-document
    cost could not be recovered from a completed run and had to be guessed from
    character counts.

    Subtracting a snapshot taken immediately before the parse gives the real
    figure. Keys present only in `after` count in full.
    """
    return {k: after.get(k, 0) - before.get(k, 0)
            for k in set(after) | set(before)
            if after.get(k, 0) - before.get(k, 0) != 0} or dict(FREE_USAGE)


def parse_packing_slip(
    xlsx_path: Union[str, Path],
    extractor: Optional[ClaudeExtractor] = None,
    force: Optional[str] = None,
    **extract_kwargs: Any,
) -> ParseResult:
    """
    Parse a vendor packing slip (.xlsx or .pdf), routing to the cheapest parser
    that fits.

    PDFs go straight to the Claude path: there is no validated deterministic
    parser for one, and PDF packing lists are a primary case (Symmetry sends
    theirs as a PDF), not an edge case.

    force="claude" or force="deterministic" overrides the routing (useful for
    A/B-ing the extractor against the known-good format).
    """
    path = Path(xlsx_path)
    if not path.exists():
        raise FileNotFoundError(f"packing slip not found: {path}")

    # `notes` is informational routing detail; `warnings` means a human must look.
    notes: list[str] = []
    warnings: list[str] = []
    # Snapshot BEFORE any model call, so `usage` below is this document's spend
    # rather than the extractor's lifetime running total. See `usage_delta`.
    usage_before = dict(getattr(extractor, "last_usage", {}) or {}) if extractor else {}
    # By signature, not by suffix -- see claude_extractor.sniff_format.
    fmt = sniff_format(path)
    is_pdf = fmt == FORMAT_PDF

    if is_pdf:
        if force == "deterministic":
            raise ExtractionError(
                f"{path.name}: force='deterministic' is not available for PDFs -- no validated "
                "deterministic packing-list parser exists for a PDF."
            )
        notes.append("PDF packing list -- Claude path (no deterministic parser for PDFs)")
    elif force != "claude":
        matched, reason = looks_like_inprotex(path)
        notes.append(f"format sniff: {reason}")
        if matched or force == "deterministic":
            result = _try_deterministic_packing_slip(path, notes, warnings)
            if result is not None:
                return result
        # Falls through to Claude; notes/warnings explain why.

        if force == "deterministic":
            raise ExtractionError(
                f"{path.name}: force='deterministic' but the deterministic parser could not "
                f"produce a valid result. Notes: {notes + warnings}"
            )

    extractor = extractor or ClaudeExtractor()
    if is_pdf:
        extraction = extractor.extract_pdf_packing_list(path)
    else:
        extraction = extractor.extract_workbook(path, **extract_kwargs)

    # Aggregate duplicate style/colour/size keys before anything downstream sees
    # the lines. Deliberately placed here rather than inside the extractor so it
    # covers the xlsx AND pdf paths with one implementation. NOT applied in
    # parse_shipment_documents -- that spans several documents, which must be
    # reconciled against each other, never summed together.
    rows = [line_to_dict(l) for l in extraction.lines]
    # The hard constraint on two-axis sizes, BEFORE aggregation: aggregation keys
    # on `size`, so a rejected composition must already be flagged rather than
    # merging into a neighbour as though it were a settled size.
    comp_warnings = enforce_size_composition(rows)
    lines, agg_warnings = aggregate_lines(rows, document_label=path.name)
    return ParseResult(
        lines=lines,
        parser="claude-assisted",
        vendor_name=extraction.vendor_name,
        document_summary=extraction.document_summary,
        unparsed_regions=meaningful(extraction.unparsed_regions),
        warnings=warnings + meaningful(extraction.warnings) + comp_warnings + agg_warnings,
        notes=notes,
        usage=usage_delta(usage_before, extractor.last_usage),
    )


def _try_deterministic_packing_slip(
    path: Path, notes: list[str], warnings: list[str]
) -> Optional[ParseResult]:
    """
    Run the Inprotex parser; return None if it isn't usable.

    A file that sniffed as a known format and then failed to parse is an
    anomaly, so that goes in `warnings` (a human should know the file changed
    shape) rather than in the informational `notes`.
    """
    from parse_packing_slip import parse_packing_sheet

    try:
        raw_lines = parse_packing_sheet(str(path))
    except Exception as exc:  # noqa: BLE001 -- any failure means "use Claude"
        warnings.append(
            f"file matched the Inprotex layout but its parser raised {type(exc).__name__}: {exc} "
            f"-- routed to Claude instead. The vendor may have changed their template."
        )
        logger.warning("Inprotex parser failed on %s: %s", path.name, exc)
        return None

    problems = validate_deterministic_lines(raw_lines)
    if problems:
        warnings.append(
            f"file matched the Inprotex layout but its parser produced {len(raw_lines)} line(s) "
            f"that failed validation ({problems}) -- routed to Claude instead so nothing is "
            f"emitted as unearned high-confidence. The vendor may have changed their template."
        )
        logger.warning("Inprotex parser output failed validation on %s: %s", path.name, problems)
        return None

    logger.info("Parsed %s with the deterministic Inprotex parser (%d lines)", path.name, len(raw_lines))
    # Same aggregation and deterministic ordering as the Claude path, so both
    # produce comparable, retry-stable output.
    det_lines, det_warnings = aggregate_lines(
        [
            # A real row, not "recap": a row-less hint is UNVERIFIABLE to the
            # carton-backing check, which would flag every line of the one
            # hand-verified parser for review.
            deterministic_line_to_dict(
                l, source_hint=(f"{INPROTEX_SHEET}!R{l['sheet_row']}" if l.get("sheet_row")
                                else f"{INPROTEX_SHEET}!recap"))
            for l in raw_lines
        ],
        document_label=path.name,
    )
    warnings.extend(det_warnings)
    return ParseResult(
        lines=det_lines,
        parser="inprotex-deterministic",
        vendor_name="Inprotex",
        document_summary=(
            f"Inprotex '{INPROTEX_SHEET}' tab, read by the validated deterministic parser "
            f"(hand-checked 77/77 against the vendor's own summary email)."
        ),
        warnings=warnings,
        notes=notes,
        # EXPLICIT zero, not an empty dict. Free and unmeasured must not look
        # alike: `{}` reads as "nobody recorded this", and the deterministic
        # parser genuinely spent nothing. The distinction is the whole reason
        # the column exists.
        usage=dict(FREE_USAGE),
    )


# ---------------------------------------------------------------------------
# Shipping advice
# ---------------------------------------------------------------------------

#: Fields that must all be present for the deterministic PDF path to be trusted.
REQUIRED_SHIP_FIELDS = ("etd", "eta", "hawb")


def extract_pdf_text(pdf_path: Union[str, Path]) -> str:
    """Full text layer of a PDF. Raises DocumentUnreadable if it won't open."""
    with open_pdf(pdf_path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def parse_shipping_advice(
    pdf_path: Union[str, Path],
    extractor: Optional[ClaudeExtractor] = None,
    force: Optional[str] = None,
) -> tuple[dict, str, list[str]]:
    """
    Parse a shipping advice PDF.

    Returns (ship_info, parser_name, warnings). `ship_info` keys match what
    `matcher.py` and the old `parse_shipping_advice_pdf` produce: invoice_no,
    hawb, mawb, etd, eta — plus `confidence` and `note` for the review step.
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"shipping advice not found: {path}")

    warnings: list[str] = []

    if force != "claude":
        from parse_packing_slip import parse_shipping_advice_pdf

        try:
            info = parse_shipping_advice_pdf(str(path))
        except Exception as exc:  # noqa: BLE001
            info = {}
            warnings.append(f"deterministic PDF parser raised {type(exc).__name__}: {exc}")

        # The parser reports how it resolved ETD/ETA (missing labels, ambiguous
        # columns, multi-leg routing). Surface those rather than dropping them --
        # they are exactly the conditions a reviewer needs to know about.
        for note in info.pop("parse_notes", []):
            warnings.append(f"shipping advice: {note}")

        missing = [f for f in REQUIRED_SHIP_FIELDS if not info.get(f)]
        if info and not missing:
            logger.info("Parsed %s with the deterministic regex parser", path.name)
            return (
                {**info, "confidence": "high", "note": ""},
                "regex-deterministic",
                warnings,
            )
        if info:
            warnings.append(
                f"deterministic PDF parser missing {missing} -- routed to Claude instead"
            )
        if force == "deterministic":
            raise ExtractionError(
                f"{path.name}: force='deterministic' but required fields {missing} were not found."
            )

    extractor = extractor or ClaudeExtractor()
    text = ""
    try:
        text = extract_pdf_text(path)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"PDF text extraction raised {type(exc).__name__}: {exc}")

    if text.strip():
        extraction = extractor.extract_shipping_advice_text(text, source_file=path.name)
        parser = "claude-assisted-text"
    else:
        warnings.append(
            "PDF has no extractable text layer (likely a scan) -- sent the document itself to "
            "Claude, which costs more tokens than the text path"
        )
        extraction = extractor.extract_shipping_advice_pdf(path.read_bytes(), source_file=path.name)
        parser = "claude-assisted-pdf"

    return _ship_info_from_extraction(extraction), parser, warnings + meaningful(extraction.warnings)


def _ship_info_from_extraction(extraction: ShippingAdviceExtraction) -> dict:
    return {
        "invoice_no": extraction.invoice_no or None,
        "hawb": extraction.hawb or None,
        "mawb": extraction.mawb or None,
        "etd": extraction.etd or None,
        "eta": extraction.eta or None,
        "confidence": extraction.confidence,
        "note": extraction.note,
    }


# ---------------------------------------------------------------------------
# Multi-document shipments
# ---------------------------------------------------------------------------


def build_source_documents(paths: Sequence[Union[str, Path]]) -> tuple[list, list[str]]:
    """
    Render each path (xlsx or pdf) into SourceDocument(s) for a combined call.

    Returns (sources, warnings). PDF pages with no text layer are reported in
    warnings rather than silently skipped -- a photo-only page could be the one
    holding the size table.
    """
    from claude_extractor import (
        SourceDocument,
        read_pdf_layouts,
        read_workbook_grids,
        visible_grids,
    )

    sources: list[SourceDocument] = []
    warnings: list[str] = []

    for raw in paths:
        path = Path(raw)
        if not path.exists():
            raise FileNotFoundError(f"source document not found: {path}")

        # One unopenable attachment must flag itself and let the batch continue --
        # a corrupt or password-protected file is a routine vendor-email event,
        # not a reason to abandon the other attachments.
        fmt = sniff_format(path)
        try:
            if fmt in WORKBOOK_FORMATS:
                grids, hidden_notes = visible_grids(
                    [g for g in read_workbook_grids(path) if not g.is_empty], path.name)
                warnings.extend(f"{path.name}: {note}" for note in hidden_notes)
                if not grids:
                    warnings.append(f"{path.name}: no non-empty visible worksheets")
                for grid in grids:
                    sources.append(
                        SourceDocument(
                            label=f"{path.name} (sheet '{grid.name}')",
                            rendered=grid.render(),
                            kind="spreadsheet",
                        )
                    )
                continue
            if fmt != FORMAT_PDF:
                raise ExtractionError(
                    f"unsupported source document type: {path.name} (its leading bytes "
                    f"match no workbook or PDF format)"
                )
        except DocumentUnreadable as exc:
            warnings.append(
                f"COULD NOT OPEN {path.name}: {exc.reason}. Skipped; the remaining "
                f"document(s) were still processed."
            )
            logger.warning("Skipping unreadable document %s: %s", path.name, exc.reason)
            continue

        if fmt == FORMAT_PDF:
            try:
                with open_pdf(path) as pdf:
                    total_pages = len(pdf.pages)
                pages = read_pdf_layouts(path)
            except DocumentUnreadable as exc:
                warnings.append(
                    f"COULD NOT OPEN {path.name}: {exc.reason}. Skipped; the remaining "
                    f"document(s) were still processed."
                )
                logger.warning("Skipping unreadable document %s: %s", path.name, exc.reason)
                continue
            if not pages:
                warnings.append(
                    f"{path.name}: no extractable text on any of its {total_pages} page(s) "
                    f"(likely a scan) -- its content is NOT included in this extraction"
                )
            skipped = total_pages - len(pages)
            if pages and skipped:
                warnings.append(
                    f"{path.name}: {skipped} of {total_pages} page(s) had no text layer and were "
                    f"not sent (typically photo/measurement pages, but verify none held line data)"
                )
            for page_label, rendered in pages:
                sources.append(
                    SourceDocument(
                        label=f"{path.name} ({page_label})", rendered=rendered, kind="pdf"
                    )
                )
        # Unsupported extensions already raised inside the try above -- an
        # ExtractionError there is a caller mistake, not an unreadable file, so it
        # deliberately isn't swallowed by the DocumentUnreadable handler.

    return sources, warnings


def parse_shipment_documents(
    paths: Sequence[Union[str, Path]],
    focus: str = "",
    extractor: Optional[ClaudeExtractor] = None,
    allow_excluded_sources: bool = False,
) -> ParseResult:
    """
    Extract one shipment from several complementary documents in a single call.

    *** NOT WIRED INTO THE LIVE PIPELINE — see the ruling below. ***

    This was built when Symmetry appeared to send a packing list with no size
    breakdown, so the size detail had to be joined from a final inspection
    report. **Both halves of that premise turned out to be wrong:**

      - The document tested was `SD #1720, 1721 INVOICE, PACKING LIST.pdf`, a
        customs *invoice*. Symmetry's real packing lists (`SD Actual Packing
        Covering ...` and `SD Actual Packing ...`) do carry full style/colour/
        size detail, and both extract correctly and agree exactly.
      - **Paula has ruled inspection reports permanently out of scope as a data
        source** (2026-08-11). Not a design choice this code may revisit.

    So it stays available for genuine multi-document cases (a packing list split
    across two files, say) but it must NOT be used to fill size gaps. If a
    vendor's actual packing list can't resolve to style/colour/size lines, the
    shipment goes to manual entry — see `parse_shipment_email`, which is the
    entry point the pipeline should call.

    `allow_excluded_sources=True` is required to pass a banned document type, and
    exists only for offline experiments. The live path never sets it.
    """
    from attachment_classifier import BANNED_AS_DATA_SOURCE, classify_by_filename

    banned = []
    for raw in paths:
        doc_type, _ambiguous, _reason = classify_by_filename(Path(raw).name)
        if doc_type in BANNED_AS_DATA_SOURCE:
            banned.append((Path(raw).name, doc_type.value))
    if banned and not allow_excluded_sources:
        listing = ", ".join(f"{name} ({kind})" for name, kind in banned)
        raise ExtractionError(
            f"refusing to extract shipment data from: {listing}.\n"
            "Inspection reports are permanently excluded as a shipment-data source (Paula's "
            "ruling 2026-08-11) — a size gap is never filled from one, and never inferred by "
            "splitting a colour total across sizes. If the actual packing list cannot resolve to "
            "style/colour/size lines, flag the shipment NEEDS_ATTENTION for manual entry.\n"
            "(allow_excluded_sources=True exists for offline experiments only.)"
        )

    sources, warnings = build_source_documents(paths)
    if not sources:
        raise ExtractionError(
            f"no readable content in {[Path(p).name for p in paths]}: {warnings}"
        )

    extractor = extractor or ClaudeExtractor()
    usage_before = dict(getattr(extractor, "last_usage", {}) or {})
    extraction = extractor.extract_documents(sources, focus=focus)

    # Same hard constraint as the single-document path. NOT followed by
    # aggregation here, deliberately -- this path spans several documents, which
    # must be reconciled against each other rather than summed.
    rows = [line_to_dict(l) for l in extraction.lines]
    comp_warnings = enforce_size_composition(rows)

    return ParseResult(
        lines=rows,
        parser="claude-assisted-multidoc",
        vendor_name=extraction.vendor_name,
        document_summary=extraction.document_summary,
        unparsed_regions=meaningful(extraction.unparsed_regions),
        warnings=warnings + meaningful(extraction.warnings) + comp_warnings,
        notes=[f"combined {len(sources)} rendered source(s): " + "; ".join(s.label for s in sources)]
        + ([f"scope limited to: {focus}"] if focus else []),
        usage=usage_delta(usage_before, extractor.last_usage),
    )


def parse_shipping_info_from_documents(
    paths: Sequence[Union[str, Path]], extractor: Optional[ClaudeExtractor] = None
) -> tuple[dict, list[str]]:
    """
    Pull ETD/ETA/HAWB/invoice out of documents that aren't a shipping advice.

    Some vendors never send one. Legendz, for instance, states the whole shipment
    header in a free-text cell on the packing-slip sheet itself
    ("... Vessel: LURLINE/102E; ETD 2026/8/5 ETA2026/8/16 ..."), which no
    label-anchored table parser can reach. The deterministic path doesn't apply,
    so this goes straight to Claude and inherits its confidence flagging.
    """
    sources, warnings = build_source_documents(paths)
    if not sources:
        return {}, warnings

    extractor = extractor or ClaudeExtractor()
    combined = "\n\n".join(f"===== {s.label} =====\n{s.rendered}" for s in sources)
    extraction = extractor.extract_shipping_advice_text(
        combined, source_file=", ".join(Path(p).name for p in paths)
    )
    return _ship_info_from_extraction(extraction), warnings + meaningful(extraction.warnings)


# ---------------------------------------------------------------------------
# Shipment email — the entry point the pipeline should call
# ---------------------------------------------------------------------------


def parse_shipment_email(
    attachment_paths: Sequence[Union[str, Path]],
    extractor: Optional[ClaudeExtractor] = None,
    cross_check: bool = False,
    display_names: Optional[dict] = None,
    classification=None,
    sheet_verdicts=None,
) -> ParseResult:
    """
    Process one shipment email's attachments end to end.

    Classifies the attachments, parses ONLY the actual packing list, and pulls
    the vendor's ETD/ETA as reference information. Real emails carry documents
    that must not be parsed for shipment data — Symmetry's had six attachments:
    an invoice, two packing lists, an ocean schedule, a payment request and two
    inspection reports. See `attachment_classifier` for the selection rules.

    If nothing in the email supplies per-size quantities, the result carries
    `needs_manual_entry` and no lines. That is a deliberate stopping point: the
    gap is never filled from an inspection report and never inferred by splitting
    a colour total across sizes (Paula, 2026-08-11).

    `cross_check=True` also parses any secondary packing list and reports whether
    the two agree — useful when a vendor sends both a rollup and carton detail.

    `classification` is the caller's `ClassificationResult` for exactly these
    attachments. **`ingest_shipment` always passes it, and must.** Classifying
    here as well meant two independent model calls per ingest: one decided what
    was recorded, the other what was parsed, and on 2026-09-23 they disagreed on
    PO 1624 -- 44 lines parsed from a document the database said was excluded.
    Omitted, this classifies itself, for standalone use (the CLI below).

    `sheet_verdicts` (an `attachment_classifier.SheetVerdicts`) replays per-sheet
    verdicts already on record and collects new ones -- see
    `ClaudeExtractor._select_packing_sheets`. `ingest_shipment` always passes it.
    """
    from attachment_classifier import DocType, classify_attachments

    extractor = extractor or ClaudeExtractor()
    # EVERYTHING this call spends, classification included. The per-document
    # figures are ATTRIBUTABLE spend; this is TOTAL spend, and the two differ by
    # the calls belonging to no single document -- the classification pass and
    # the shipping-info extraction. Reporting only the attributable sum as "what
    # the shipment cost" understates it plausibly, which is the failure this
    # whole feature exists to stop.
    call_usage_before = dict(getattr(extractor, "last_usage", {}) or {})
    # Spend that happened BEFORE this call and belongs in its total: the caller's
    # classification pass. Zero when this function classifies for itself, because
    # then `call_usage_before` already brackets that call.
    prior_spend: dict = {}
    if classification is None:
        # `display_names` carries the vendor's filenames when the bytes are stored
        # content-addressed and the path is a hash -- see classify_attachments.
        classification = classify_attachments(attachment_paths, extractor=extractor,
                                              display_names=display_names)
    else:
        prior_spend = dict(getattr(classification, "usage", {}) or {})

    def spent_so_far() -> dict:
        return _add_usage(
            usage_delta(call_usage_before, getattr(extractor, "last_usage", {}) or {}),
            prior_spend,
        )

    shown = {Path(k).resolve(): v for k, v in (display_names or {}).items()}

    def named(path) -> str:
        return shown.get(Path(path).resolve(), Path(path).name)

    notes = [f"attachment triage: {classification.summary()}"]
    for item in classification.excluded:
        notes.append(f"not parsed — {named(item.path)}: {item.excluded_reason}")
    warnings = list(classification.warnings)

    if classification.needs_manual_entry:
        result = ParseResult(
            lines=[],
            parser="attachment-triage-only",
            notes=notes,
            warnings=warnings
            + [
                "MANUAL ENTRY REQUIRED: no attachment in this email provides per-size "
                "quantities, so no style/colour/size lines could be produced. Paula must enter "
                "this shipment by hand. Do not infer sizes from an inspection report or by "
                "splitting colour totals."
            ],
        )
        result.needs_manual_entry = True
        # Classification ran and billed even though nothing was parsed. No
        # primary exists to attribute it to, so it lands on the total only.
        result.total_usage = spent_so_far()
        return result

    primary = classification.primary
    # Only passed when given: a workbook routed to the deterministic parser or a
    # PDF has no sheet selection to replay.
    sheet_kwargs = {"sheet_verdicts": sheet_verdicts} if sheet_verdicts is not None else {}
    try:
        result = parse_packing_slip(primary.path, extractor=extractor, **sheet_kwargs)
    except NoPackingSheetFound as exc:
        # An email that produces no reviewable artifact is the one failure mode
        # this architecture exists to prevent -- everywhere else, uncertainty
        # routes to a human (NEEDS_ATTENTION on unmatched sizes, PENDING_REVIEW by
        # default). So a workbook whose sheets are all non-packing becomes the
        # manual-entry outcome rather than an exception nobody sees.
        #
        # The per-sheet diagnostic is the whole value here, so it is carried over
        # intact -- the full message AND one warning per sheet naming its
        # predicted type. Flattening this to "extraction failed" would throw away
        # the only information that makes the decision reviewable.
        per_sheet = [
            f"sheet '{v.label}': predicted type {v.doc_type.value}"
            + (f" — {v.skip_reason}" if v.skip_reason else "")
            + (f"; classifier said: {v.reason}" if v.reason else "")
            for v in exc.verdicts
        ]
        logger.warning("%s: no packing sheet found; routed to manual entry", named(exc.path))
        result = ParseResult(
            lines=[],
            parser="attachment-triage-only (no packing sheet in workbook)",
            notes=notes + [f"selected attachment: {named(primary.path)}"],
            warnings=warnings
            + [
                "MANUAL ENTRY REQUIRED: the selected attachment "
                f"({named(exc.path)}) contains no worksheet that classifies as a packing list "
                "with per-size quantities, so no style/colour/size lines could be produced. "
                "This is NOT an empty shipment. Paula must enter it by hand. Do not infer "
                "sizes from an inspection report or by splitting colour totals."
            ]
            + per_sheet
            + [f"full extractor diagnostic: {exc}"],
        )
        result.needs_manual_entry = True
        # IT SPENT TOKENS BEFORE IT RAISED -- `classify_sections` ran and billed.
        # Leaving `source_usage` empty made `_doc_usage`'s PRIMARY fallback write
        # (0, 0), so a real cost read as "genuinely free": the same
        # unknown-becomes-zero confusion the three-state design exists to prevent,
        # on the one path that had not been given the treatment the cross-check
        # failure path already had. Reachable on any multi-sheet workbook whose
        # sheets all classify as non-packing.
        # The per-document figure is THIS document's own spend (the section
        # classification), so it excludes the caller's attachment triage; the
        # total includes it.
        result.source_usage[str(Path(primary.path).resolve())] = usage_delta(
            call_usage_before, getattr(extractor, "last_usage", {}) or {})
        result.total_usage = spent_so_far()
        return result
    result.notes = notes + result.notes

    # CARTON BACKING (Paula, 2026-09-23): a quantity row is shipment data only if
    # cartons in the same document back it. Unbacked rows leave the shipment here,
    # named in the warnings; rows with nothing to check against stay, flagged.
    # See carton_backing.py -- it reads no labels, only the grid's arithmetic.
    result.extracted_line_count = len(result.lines)
    # Evidence is the other SELECTED attachments only -- never an excluded one,
    # so an inspection report can never back a quantity (Paula, 2026-08-11).
    backing = carton_backing.check_lines(
        result.lines, primary.path,
        evidence=[(other.path, named(other.path)) for other in classification.cross_checks])
    result.lines = backing.kept
    result.removed_lines.extend(
        # Reason text is keyed in `_REMOVAL_WORDING` -- change both together.
        {"line": line, "reason": "NOT SHIPMENT DATA (no carton backing)"}
        for line in backing.dropped)
    result.warnings.extend(backing.warnings)
    result.notes.extend(backing.notes)
    result.warnings = warnings + result.warnings

    # The PRIMARY's own spend, before any cross-check adds to it. `parse_packing_slip`
    # always sets `usage` -- its own delta, or FREE_USAGE on the deterministic
    # route -- so there is no empty case to guard here.
    result.source_usage[str(Path(primary.path).resolve())] = dict(result.usage)

    if cross_check and classification.cross_checks:
        for other in classification.cross_checks:
            # EACH cross-check gets its OWN delta. Folding them into the primary's
            # figure is what made "what did the discarded work cost" unanswerable:
            # a cross-check is a full extraction whose lines are then thrown away,
            # and that is precisely the number worth seeing.
            before = dict(getattr(extractor, "last_usage", {}) or {}) if extractor else {}
            try:
                secondary = parse_packing_slip(other.path, extractor=extractor, **sheet_kwargs)
            except ExtractionError as exc:
                result.warnings.append(f"cross-check against {named(other.path)} failed: {exc}")
                # Recorded even on failure: it was attempted, and an attempt that
                # raised may still have spent tokens before it did.
                result.source_usage[str(Path(other.path).resolve())] = usage_delta(
                    before, getattr(extractor, "last_usage", {}) or {})
                continue
            # `secondary.usage` is already this document's own delta --
            # `parse_packing_slip` snapshots and subtracts internally -- so it is
            # used directly. `before` exists for the failure path above, where no
            # ParseResult comes back to carry a figure.
            result.source_usage[str(Path(other.path).resolve())] = dict(secondary.usage)
            # The same carton rule on the cross-check, so the comparison is between
            # two documents' SHIPMENT data -- not the primary's shipment against
            # the secondary's shipment-plus-order-plus-later-goods.
            secondary_backing = carton_backing.check_lines(secondary.lines, other.path)
            # Named even though a cross-check proposes nothing: rows removed from
            # the comparison change what "agrees" means, and a removal nobody is
            # told about is the thing this module promises never to do.
            result.warnings.extend(f"cross-check {named(other.path)}: {w}"
                                   for w in secondary_backing.warnings)
            result.warnings.extend(_compare_line_sets(result.lines, secondary_backing.kept,
                                                      named(other.path)))

    # Vendor dates: reference only. The diff engine never proposes a receipt date
    # from them (see matcher.py); they exist for Paula to read while she types the
    # actual date.
    # Searched across SELECTED as well as excluded. Since the file gate stopped
    # checking type (2026-09-23), a shipping advice that repeats per-size figures
    # is admitted as a data source -- and looking only in `excluded` would then
    # lose the one document that actually carries the dates.
    advice_sources = [
        c.path
        for c in classification.selected + classification.excluded
        if c.doc_type == DocType.SHIPPING_ADVICE
    ]
    ship_paths = advice_sources or [primary.path]
    try:
        ship_info, ship_warnings = parse_shipping_info_from_documents(ship_paths, extractor=extractor)
        result.ship_info = ship_info
        result.warnings.extend(ship_warnings)
        result.notes.append(
            "vendor ETD/ETA read from "
            + ", ".join(p.name for p in ship_paths)
            + " — REFERENCE ONLY; Paula enters the actual receipt date"
        )
    except Exception as exc:  # noqa: BLE001 -- missing dates must not lose the quantities
        result.warnings.append(
            f"could not read vendor ETD/ETA ({type(exc).__name__}: {exc}); quantities are "
            f"unaffected, and the receipt date was always Paula's to enter anyway"
        )

    # LAST, so it includes everything: classification, every document, and the
    # shipping-info extraction above. Set any earlier and it silently omits
    # whatever comes after -- which it did, missing the shipping-info call and
    # reporting a total that looked right and was short.
    result.total_usage = spent_so_far()
    return result


#: A shipment that loses MORE than this fraction of its extracted lines is
#: escalated at the shipment level, not just warned about line by line.
LOST_ROWS_ESCALATION_FRACTION = 1 / 3


def removal_escalation(extracted: int, removed: Sequence[dict]) -> Optional[str]:
    """
    The shipment-level escalation when too much of a document was removed, or None.

    Each removal is already named, row by row, in the warnings. That is loud per
    line and quiet in aggregate: fifteen `NOT SHIPMENT DATA` warnings read the
    same as one. Losing more than a third of what was extracted says something
    about the DOCUMENT -- a layout the carton rule misreads, the wrong sheet, a
    slip that is mostly a later shipment -- so it is raised as its own headline.
    """
    if extracted <= 0 or len(removed) <= extracted * LOST_ROWS_ESCALATION_FRACTION:
        return None
    # The REASON leads, with the rows it removed: "16 of 44" alone says how much
    # was lost and nothing about why, and the why is what a reviewer acts on.
    by_reason: dict[str, list[dict]] = {}
    for item in removed:
        by_reason.setdefault(item["reason"], []).append(item["line"])
    why = "; ".join(
        f"{len(lines)} removed as {_REMOVAL_WORDING.get(reason, reason)}"
        + (f" (rows: {', '.join(rows)})"
           if (rows := sorted({str(ln.get('source_hint')) for ln in lines
                               if ln.get("source_hint")})) else "")
        for reason, lines in sorted(by_reason.items()))
    return (
        f"SHIPMENT ESCALATION -- more than a third of this shipment's extracted lines "
        f"were REMOVED: {why}. That is {len(removed)} of {extracted} "
        f"({len(removed) / extracted:.0%}). Check the source document before approving "
        "anything on it: a loss this size can mean the document was misread rather than "
        "that most of it was not this shipment. Each removed row is named in the "
        "warnings below.")


#: How each `removed_lines` reason reads in the escalation headline.
_REMOVAL_WORDING = {
    "NOT SHIPMENT DATA (no carton backing)":
        "NOT SHIPMENT DATA -- quantity rows no carton in the document backs",
    "no PO number": "having no PO number to attach them to",
}


def _add_usage(a: dict, b: dict) -> dict:
    """Two usage deltas summed key by key. `b` empty returns `a` unchanged."""
    if not b:
        return a
    return {k: a.get(k, 0) + b.get(k, 0) for k in set(a) | set(b)
            if a.get(k, 0) + b.get(k, 0) != 0} or dict(FREE_USAGE)


def _compare_line_sets(primary: list[dict], secondary: list[dict], label: str) -> list[str]:
    """
    Aggregate both line sets by key and report any disagreement -- on the
    PRIMARY's POs only.

    Two fixes (2026-09-28), both from comparisons that reported noise as findings:

      - **PO numbers go through `po_number_key`**, as everywhere else that groups
        by PO. Canonical text alone made `PO0001624` and `1624` different keys, and
        PO 1624's cross-check "DISAGREED on 14 keys" that were the same lines.
      - **Only the POs the primary covers are compared.** One email can carry
        documents for unrelated POs (the 1720/1721 message also held Inprotex's
        and Legendz's workbooks); comparing against those reported "DISAGREES on
        102 keys" about lines neither document was ever meant to share. The lines
        left out are COUNTED in the result, so the scoping is visible.
    """
    from netsuite_client import po_number_key

    def key_of(ln: dict) -> tuple:
        # Canonical key: a cross-check between two documents must not report a
        # disagreement merely because one printed "NEW  INDIGO" and the other
        # "NEW INDIGO", or one printed the PO padded and the other not.
        return (po_number_key(str(ln.get("po_number") or "")),
                *canonical_key(ln["style_number"], ln["color"], ln["size"]))

    def agg(lines: list[dict]) -> dict:
        out: dict[tuple, int] = {}
        for ln in lines:
            key = key_of(ln)
            out[key] = out.get(key, 0) + (ln.get("quantity") or 0)
        return out

    # A line with no resolvable PO keys to '' -- which is NOT a PO the primary
    # covers. Letting '' into scope would pair two unrelated no-PO rows by
    # style/colour/size alone and report agreement nobody established.
    scope = {key_of(ln)[0] for ln in primary} - {""}
    in_scope = [ln for ln in secondary if key_of(ln)[0] in scope]
    outside = sorted({key_of(ln)[0] or "(no PO)" for ln in secondary
                      if key_of(ln)[0] not in scope})
    scoped = (f"; {len(secondary) - len(in_scope)} line(s) on PO(s) {', '.join(outside)} "
              "not compared -- the primary does not cover them") if outside else ""
    if not in_scope:
        return [f"cross-check against {label}: NOT COMPARED -- it covers none of the "
                f"primary's PO(s) ({', '.join(sorted(scope)) or 'none'}; it has "
                f"{len(secondary)} line(s) on {', '.join(outside) or 'no PO'})"]

    a = agg([ln for ln in primary if key_of(ln)[0] in scope])
    b = agg(in_scope)
    if a == b:
        return [
            f"cross-check against {label}: agrees exactly "
            f"({len(a)} style/colour/size keys, {sum(a.values())} units){scoped}"
        ]
    diffs = {k: (a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}
    return [
        f"cross-check against {label}: DISAGREES on {len(diffs)} key(s) — "
        f"{dict(list(diffs.items())[:5])}"
        + (" (first 5 shown)" if len(diffs) > 5 else "")
        + ". Totals "
        f"{sum(a.values())} vs {sum(b.values())}. A human should decide which document is "
        f"right{scoped}."
    ]


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------


def parse_vendor_documents(
    xlsx_path: Union[str, Path],
    pdf_path: Optional[Union[str, Path]] = None,
    extractor: Optional[ClaudeExtractor] = None,
    force: Optional[str] = None,
    **extract_kwargs: Any,
) -> ParseResult:
    """
    Parse a shipment's packing slip and (optionally) its shipping advice into one
    ParseResult, ready for `matcher.build_proposed_changes`.
    """
    result = parse_packing_slip(xlsx_path, extractor=extractor, force=force, **extract_kwargs)

    if pdf_path is not None:
        ship_info, ship_parser, ship_warnings = parse_shipping_advice(
            pdf_path, extractor=extractor, force=force
        )
        result.ship_info = ship_info
        result.parser = f"{result.parser} + {ship_parser}"
        result.warnings.extend(ship_warnings)
        if ship_info.get("confidence") in ("medium", "low"):
            result.warnings.append(
                f"shipping advice dates are {ship_info['confidence']}-confidence"
                + (f": {ship_info['note']}" if ship_info.get("note") else "")
                + " -- ETD/ETA may be swapped; a reviewer must confirm the date"
            )
        if not ship_info.get("eta"):
            result.warnings.append(
                "no ETA found in the shipping advice -- the diff engine has no proposed receipt "
                "date to offer the reviewer"
            )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xlsx", help="vendor packing slip (.xlsx), or the first source document with --combine")
    ap.add_argument("pdf", nargs="?", help="shipping advice (.pdf), optional")
    ap.add_argument(
        "--combine",
        nargs="+",
        metavar="FILE",
        help="treat ALL given files (these plus the positional ones) as complementary documents for "
        "ONE shipment, extracted in a single cross-referencing call. Use when no single document "
        "holds the full picture -- e.g. an invoice with no size breakdown plus an inspection "
        "report that has one.",
    )
    ap.add_argument(
        "--focus",
        default="",
        help="with --combine, restrict extraction to one PO/style, e.g. \"PO 1721, style W600001\"",
    )
    ap.add_argument("--force-claude", action="store_true", help="skip deterministic parsers")
    ap.add_argument("--force-deterministic", action="store_true", help="fail rather than use Claude")
    ap.add_argument("--json", dest="json_out", help="write the full result to this path")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    force = "claude" if args.force_claude else "deterministic" if args.force_deterministic else None

    if force != "deterministic" and not credentials_available():
        from claude_extractor import SECRETS_ENV_PATH

        print(
            f"NOTE: no Anthropic credential found. Set ANTHROPIC_API_KEY in {SECRETS_ENV_PATH}\n"
            "      (loaded automatically), or export ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN, or\n"
            "      run `ant auth login`.\n"
            "      The deterministic path still works; the Claude-assisted path will fail if it\n"
            "      is needed.\n",
            file=sys.stderr,
        )

    try:
        if args.combine:
            paths = [args.xlsx] + ([args.pdf] if args.pdf else []) + list(args.combine)
            result = parse_shipment_documents(paths, focus=args.focus)
        else:
            result = parse_vendor_documents(args.xlsx, args.pdf, force=force)
    except ExtractionError as exc:
        print(f"EXTRACTION FAILED: {exc}", file=sys.stderr)
        return 1

    print("=" * 78)
    print(f"PARSED: {Path(args.xlsx).name}")
    print("=" * 78)
    print(f"  parser        : {result.parser}")
    print(f"  vendor        : {result.vendor_name or '(not stated)'}")
    print(f"  lines         : {len(result.lines)}")
    print(f"  low-confidence: {len(result.low_confidence_lines)}")
    print(f"  unparsed      : {len(result.unparsed_regions)}")
    print(f"  needs review  : {'YES' if result.needs_review else 'no'}")
    if result.usage:
        print(f"  token usage   : {result.usage}")
    if result.document_summary:
        print(f"\n  layout: {result.document_summary}")
    if result.ship_info:
        print(f"\n  shipment: {result.ship_info}")

    if result.low_confidence_lines:
        print("\n--- LOW-CONFIDENCE LINES (must be reviewed, never auto-applied) ---")
        for ln in result.low_confidence_lines:
            print(
                f"  [{ln['confidence']}] PO {ln['po_number']} {ln['style_number']} "
                f"{ln['color']}-{ln['size']} qty={ln['quantity']} @ {ln['source_hint']}"
                + (f"\n      {ln['note']}" if ln["note"] else "")
            )

    if result.unparsed_regions:
        print("\n--- UNPARSED REGIONS (data seen but not extracted) ---")
        for region in result.unparsed_regions:
            print(f"  - {region}")

    if result.warnings:
        print("\n--- WARNINGS (these are why review is needed) ---")
        for warning in result.warnings:
            print(f"  - {warning}")

    if result.notes:
        print("\n--- routing notes (informational) ---")
        for note in result.notes:
            print(f"  - {note}")

    print(f"\n{result.review_summary()}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "parser": result.parser,
                    "vendor_name": result.vendor_name,
                    "document_summary": result.document_summary,
                    "lines": result.lines,
                    "ship_info": result.ship_info,
                    "unparsed_regions": result.unparsed_regions,
                    "warnings": result.warnings,
                    "notes": result.notes,
                    "needs_review": result.needs_review,
                    "usage": result.usage,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"\nWrote {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
