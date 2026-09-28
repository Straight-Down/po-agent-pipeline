"""
Carton backing: is a quantity row a count of what shipped, or something else?

**The rule (Paula, 2026-09-23).** A quantity row counts as a shipment quantity
only if cartons in the same document back it. PO 1624's footwear slip printed
three quantity rows per colour -- one backed by ten carton rows, one equal to the
order, and one for goods going later by a different carrier -- and the extractor
emitted two of them as this shipment. The later goods had no carton on this slip,
which is exactly how you can tell they were not in it.

**Nothing here reads a label.** "By Sea", "By UPS", "Ordered Qty", "ACTUAL",
"sub total": the next vendor will say something else. The test is arithmetic on
the grid:

  - A **carton row** is a row whose own non-size cells show carton arithmetic:
    some per-carton quantity x some carton count = some total, and that total is
    the row's size quantities (x the carton count, where the vendor prints the
    per-carton figure under each size). Footwear prints `8 x 3 = 24` with 24
    under the size; Tainan prints `32 x 2 = 64` with 32 under the size; the
    511/533 sheet prints `40 x 2 = 80` with 80 under it. A summary row -- a
    recap, an order row, a sub-total -- carries a total but no per-carton x count
    pair, so it is never a carton row.
  - A quantity row is **BACKED** when a contiguous run of carton rows above it
    sums to it exactly, size by size. Sizes are matched by their header LABEL,
    not by column: Tainan's carton grid has its waists at I..O and its recap at
    K..Q.

**Three outcomes, and the difference between the last two is the point:**

  BACKED        a run of carton rows sums to it. Shipment data.
  UNBACKED      the sheet HAS carton rows, and none of their runs sums to this
                row. Not shipment data: removed from the shipment, loudly.
  UNVERIFIABLE  there is nothing to check against -- a PDF, a sheet with no
                carton rows (a rollup), a hint that names no row. KEPT, and
                flagged for a human (Paula, 2026-09-23: "keep and flag"). A rule
                that cannot be checked must not silently discard a shipment.

**What it does not do.** It does not decide between two backed rows. Tainan's
`REV` sheet was packed to its own plan, so its ORDER x 1.08 rows ARE carton
backed; this check passes them. That question is the ACT-vs-REV ruling, not
this one. And it does not consult NetSuite: whether a row "equals the order" is
not asked separately, because an unbacked order row is already UNBACKED and a
BACKED one is a shipment that went out exactly to order (Paula, 2026-09-23).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence, Union

logger = logging.getLogger(__name__)

BACKED = "BACKED"
UNBACKED = "UNBACKED"
UNVERIFIABLE = "UNVERIFIABLE"

#: Cell-arithmetic tolerance. Quantities are counts; this only absorbs the float
#: noise of `8.0 * 3.0` read back through a spreadsheet.
_EPS = 1e-6

_HINT = re.compile(r"^\s*(?P<sheet>.+?)!R(?P<row>\d+)\s*$")


@dataclass(frozen=True)
class RowBacking:
    """The verdict for one (sheet, row)."""

    status: str
    sheet: str
    row: Optional[int]
    reason: str
    #: The carton rows whose sum is this row -- the evidence, for a BACKED row.
    carton_rows: tuple[int, ...] = ()


@dataclass
class BackingResult:
    """Lines split by verdict, plus what a reviewer must be told."""

    kept: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _num(text: Any) -> Optional[float]:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _header_candidates(grid: Any) -> list[tuple[int, dict[int, str], bool]]:
    """Every size-header candidate: `(1-based row, {column: label}, all_numeric)`."""
    from attachment_classifier import (
        _classify_size_cell,
        _size_vocabulary,
        size_header_rows,
    )

    vocabulary = _size_vocabulary()
    out = []
    for row, columns in size_header_rows(grid, vocabulary):
        labels, numeric = {}, True
        for column in columns:
            label, is_number = _classify_size_cell(grid.rows[row - 1][column], vocabulary)
            if label is not None:
                labels[column] = label
                numeric = numeric and is_number
        out.append((row, labels, numeric))
    return out


class _Sheet:
    """One grid, with its size headers resolved and its carton rows found."""

    def __init__(self, grid: Any):
        self.grid = grid
        # HEADERS, resolved top-down. `size_header_rows` is permissive by design
        # and proposes "headers" on data rows whose numbers happen to be ascending
        # valid sizes: Inprotex's mixed carton #4 (`4 | 7 | 9 | 10`), 511/533's
        # carton rows 36-37 (`4 | 9.5 | 38`). Accepting one re-labels the rows
        # beneath it and breaks every sum there -- it once made a hand-verified
        # Inprotex recap row read UNBACKED. So an ALL-NUMERIC candidate that is
        # itself a carton row under the header above it is data, not a header.
        # A letter header (`S M L`) identifies itself and is always accepted; a
        # genuine numeric header (`8 ... 14`, `30 ... 42`) has no carton
        # arithmetic in its row and is accepted however few sizes it carries --
        # a label-count floor here rejected real narrow headers and let the rows
        # under them borrow a different style's labels.
        self.headers: list[tuple[int, dict[int, str]]] = []
        self.header_rows: set[int] = set()
        for row, labels, numeric in _header_candidates(grid):
            if numeric and self.headers and self._carton_contribution(row) is not None:
                continue
            self.headers.append((row, labels))
            self.header_rows.add(row)
        #: (row, {label: contributed quantity}) in sheet order.
        self.cartons: list[tuple[int, dict[str, float]]] = []
        #: The subset that shows FULL carton arithmetic (per-carton x count =
        #: total). Only these may vouch for themselves; see `_carton_contribution`.
        self.strong: set[int] = set()
        candidates: list[tuple[int, dict[str, float]]] = []
        for row in range(1, grid.n_rows + 1):
            found = self._carton_contribution(row)
            if found:
                contribution, strong = found
                candidates.append((row, contribution))
                if strong:
                    self.strong.add(row)
        #: Weak candidates refused as carton rows, with why. See `_admit_weak`.
        self.rejected_weak: dict[int, str] = {}
        admitted = self._admit_weak(candidates)
        self.cartons = [(r, c) for r, c in candidates if r in admitted]

    def _admit_weak(self, candidates: list[tuple[int, dict[str, float]]]) -> set[int]:
        """
        Which candidate rows are carton rows: every strong row, and a weak row only
        if BOTH of these hold (Kiko, 2026-09-28):

          (a) it is not a SUMMARY: not equal, size by size, to the sum of two or
              more carton rows directly above it, counted through any summary
              already refused in between (so a total-of-subtotals is a summary
              too); and
          (b) every other member of its sheet-contiguous run of carton rows is
              strong -- on a sheet whose LAYOUT is carton arithmetic, i.e. where
              strong rows are the majority of candidates. On a sheet where they
              are the exception there is nothing that could verify it, and (b)
              cannot apply.

        **Why (a) exists.** The weak test is "some cell equals the size sum, and
        some OTHER cell is a positive integer" -- and that integer is never shown
        to be a carton count. It admitted 511/533's sub-totals (R35, R53), every
        Legendz sub-total (R11, R14, R18) and footwear's recap rows, which is the
        exact thing the module docstring says never happens: a summary row
        counted as a carton, free to back a later row that merely equals it.

        **Why (b) needs (a) first.** 511/533's sub-total R35 sits directly
        against DCHE's first carton, so without (a) the run around the R39 typo
        contains a weak non-member and R39 -- the row the weak test exists for --
        is refused, dropping five real lines.

        **Why the majority test, not "no strong row".** Inprotex prints one row
        per carton and no per-carton x count at all, so its carton rows are weak
        by construction; applying (b) there drops all 77 hand-verified lines. And
        it is not free of strong rows: R197 and R220 read `5 | ... 50 | 10`, which
        is C/NO. 5 x N.W. 10 = 50 -- full carton arithmetic by coincidence. Two
        such accidents among 205 candidates must not switch the rule on for the
        whole sheet. The cost, stated: on a sheet like that, a stray weak row is
        held back by (a) alone.
        """
        contribution = dict(candidates)
        admitted: set[int] = set()
        for row, sizes in candidates:  # top-down, so a summary never feeds a later one
            if row in self.strong:
                admitted.add(row)
                continue
            # The walk passes THROUGH a summary already refused, adding nothing for
            # it, rather than stopping there (Kiko, 2026-09-28). Stopping let a
            # total-of-subtotals through as a carton: the 1720/1721 carton PDF
            # prints `PO TOTAL` directly under its colour subtotal, and PO 1720's
            # PO TOTAL alone then "backed" the M650022 recap -- a total vouching
            # for a total. Measured before applying: 0 of 924 stored workbook
            # verdicts moved, and no workbook's carton set changed.
            accumulated: dict[str, float] = {}
            counted = 0
            above = row - 1
            while above in admitted or above in self.rejected_weak:
                if above in admitted:
                    for label, value in contribution[above].items():
                        accumulated[label] = accumulated.get(label, 0.0) + value
                    counted += 1
                    if counted >= 2 and _same(accumulated, sizes):
                        self.rejected_weak[row] = (
                            f"row {row} is the sum of the {counted} carton rows in "
                            f"{above}-{row - 1}: a summary, not a carton")
                        break
                above -= 1
            else:
                admitted.add(row)
        if 2 * len(self.strong) > len(candidates):
            # Judged against a SNAPSHOT: discarding while scanning made the answer
            # depend on order -- of two weak rows in one run, the first was refused
            # for its weak neighbour and the second then admitted, the neighbour
            # having vanished from the run it was measured against.
            runs_of = frozenset(admitted)
            refused = {}
            for row in sorted(runs_of - self.strong):
                low = high = row
                while low - 1 in runs_of:
                    low -= 1
                while high + 1 in runs_of:
                    high += 1
                others = [r for r in range(low, high + 1) if r != row]
                if not others or any(r not in self.strong for r in others):
                    refused[row] = (
                        f"row {row} shows no full carton arithmetic and is not inside a run "
                        f"of verified carton rows (run {low}-{high})")
            self.rejected_weak.update(refused)
            admitted -= set(refused)
        return admitted

    def _labels_for(self, row: int) -> dict[int, str]:
        governing = [labels for r, labels in self.headers if r < row]
        return governing[-1] if governing else {}

    def sizes(self, row: int) -> dict[str, float]:
        """Non-zero size quantities on a row, keyed by size label."""
        if row in self.header_rows or not 1 <= row <= self.grid.n_rows:
            return {}
        cells = self.grid.rows[row - 1]
        out: dict[str, float] = {}
        for column, label in self._labels_for(row).items():
            if column < len(cells):
                value = _num(cells[column])
                if value:
                    out[label] = out.get(label, 0.0) + value
        return out

    def _carton_contribution(self, row: int) -> Optional[tuple[dict[str, float], bool]]:
        """
        `(contribution, strong)` if this row is a carton row, else None.

        The multiplier is the carton count when the vendor prints a PER-CARTON
        figure under each size (the printed total is sizes x count), and 1 when
        it prints the range's total (the printed total is the sizes' sum).

        **Strong** = full carton arithmetic: a per-carton figure x a carton count
        = the total. **Weak** = a total matching the sizes and a carton count, but
        no per-carton figure that multiplies out. Weak exists because vendors make
        typos: 511/533 row 39 packs 20 pieces in 1 carton and prints 11 as the
        per-carton figure. Strict-only called that carton row a non-carton, left
        its sub-total unbacked, and would have dropped five real lines.

        A weak row may help back a total but may NOT vouch for itself: a later-
        shipment row that happens to print a carton count and a total would
        otherwise certify itself as shipped.
        """
        sizes = self.sizes(row)
        if not sizes or not all(v.is_integer() and v > 0 for v in sizes.values()):
            return None
        size_columns = set(self._labels_for(row))
        cells = self.grid.rows[row - 1]
        others = [(i, v) for i, c in enumerate(cells)
                  if i not in size_columns and (v := _num(c)) is not None]
        total = sum(sizes.values())
        weak: Optional[dict[str, float]] = None
        for ti, t in others:
            for ni, n in others:
                if ni == ti or n < 1 or not float(n).is_integer():
                    continue
                if abs(t - total * n) <= _EPS and n > 1:
                    multiplier = int(n)
                elif abs(t - total) <= _EPS:
                    multiplier = 1
                else:
                    continue
                contribution = {label: v * multiplier for label, v in sizes.items()}
                # ...and a per-carton figure p with p x n = t, in a third cell.
                if any(pi not in (ti, ni) and abs(p * n - t) <= _EPS for pi, p in others):
                    return contribution, True
                if multiplier == 1 and weak is None:
                    weak = contribution
        return (weak, False) if weak is not None else None

    def backing(self, row: int) -> RowBacking:
        name = self.grid.name
        target = self.sizes(row)
        if not target:
            return RowBacking(UNVERIFIABLE, name, row,
                              f"row {row} of sheet '{name}' holds no size quantities to check")
        if not self.cartons:
            return RowBacking(UNVERIFIABLE, name, row,
                              f"sheet '{name}' has no carton rows to check row {row} against")
        if row in self.strong:
            return RowBacking(BACKED, name, row,
                              f"row {row} is itself a carton row", (row,))
        above = [(r, c) for r, c in self.cartons if r < row]
        # Nearest run first: two identical colour blocks (PO 1624's DKF and MLT)
        # must each be backed by their OWN cartons, not the first match found.
        for end in range(len(above) - 1, -1, -1):
            accumulated: dict[str, float] = {}
            for start in range(end, -1, -1):
                for label, value in above[start][1].items():
                    accumulated[label] = accumulated.get(label, 0.0) + value
                if _same(accumulated, target):
                    rows = tuple(r for r, _c in above[start:end + 1])
                    return RowBacking(
                        BACKED, name, row,
                        f"row {row} = sum of carton rows {rows[0]}-{rows[-1]} "
                        f"({len(rows)} row(s))", rows)
        return RowBacking(
            UNBACKED, name, row,
            f"no run of the {len(self.cartons)} carton row(s) on sheet '{name}' sums to "
            f"row {row} ({_show(target)}) -- these quantities are not counted in any "
            "carton of this document")


def _same(a: dict[str, float], b: dict[str, float]) -> bool:
    return set(a) == set(b) and all(abs(a[k] - b[k]) <= _EPS for k in a)


def _show(sizes: dict[str, float]) -> str:
    return ", ".join(f"{k}={v:g}" for k, v in sizes.items())


def _parse_hints(hint: str) -> list[tuple[str, int]]:
    """
    The (sheet, row) pairs a hint cites -- ALL of them, or none.

    A part that does not parse makes the whole hint unusable (returns []), so
    the line is UNVERIFIABLE rather than judged on part of its evidence.
    """
    out = []
    for part in str(hint or "").split(","):
        match = _HINT.match(part)
        if not match:
            return []
        out.append((match.group("sheet").strip(), int(match.group("row"))))
    return out


def _quantity_problem(sheets: dict, cited: list[tuple[str, int]], size: Any,
                      quantity: float) -> str:
    """
    Why the cited row(s) do not hold this line's figure, or "" if they do.

    Checked under the line's OWN size where the header names it -- a figure that
    merely appears somewhere on the row can belong to a different size. A size
    the header does not print as one label (a composed `36-32`, whose inseam sits
    in a block label) falls back to "anywhere on the row". Several cited rows
    pass if the figure is any one of them or their sum.
    """
    import size_vocabulary as sv
    from canonical import canonical

    # The SAME normalisation header cells get (`_classify_size_cell`): numeric
    # formatting first, then canonical. Plain canonical left `9.0` unequal to a
    # header's `9`, fell through to "anywhere on the row", and certified size
    # 10's figure as size 9's.
    numeric = sv.numeric_label(str(size or "").strip())
    wanted = canonical(numeric if numeric is not None else size)
    per_row: list[Optional[float]] = []
    anywhere: list[float] = []
    for sheet_name, row in cited:
        sheet = sheets.get(sheet_name)
        held = sheet.sizes(row) if sheet else {}
        anywhere.extend(held.values())
        labels = set(sheet._labels_for(row).values()) if sheet else set()
        per_row.append(held.get(wanted, 0.0) if wanted in labels else None)
    where = ", ".join(f"'{s}' row {r}" for s, r in cited)
    if all(v is not None for v in per_row):
        candidates = list(per_row) + [sum(per_row)]
        if not any(abs(v - quantity) <= _EPS for v in candidates):
            return (f"{where} does not hold this line's quantity {quantity:g} under size "
                    f"{size!r} (it holds {' + '.join(f'{v:g}' for v in per_row)}); cannot "
                    "tell which row it came from")
        return ""
    if not any(abs(v - quantity) <= _EPS for v in anywhere):
        return (f"{where} does not hold this line's quantity {quantity:g}; cannot tell "
                "which row it came from")
    return ""


def _size_label(text: Any) -> Optional[str]:
    """A size as a header cell would label it -- the key `_Sheet.sizes` uses."""
    from attachment_classifier import _classify_size_cell, _size_vocabulary

    return _classify_size_cell(str(text or "").strip(), _size_vocabulary())[0]


def pdf_carton_grid(path: Union[str, Path]) -> Any:
    """
    A PDF's text layer as ONE `SheetGrid`, columns anchored on its size header.

    A PDF has no cells, so the columns are rebuilt from word positions: a line
    carrying three or more LETTER size labels (`XS S M L XL`) is a header, and
    every word below it is placed in the column of the header word whose centre
    is nearest. Letter labels only: a carton row reading `17 10 2 4 16 1` is
    full of valid numeric sizes and, taken for a header, re-anchored every row
    after it. The anchors carry across pages, because a carton list continues
    onto page 2 without repeating its header. Rows before any header are kept
    as one text cell so row numbers stay in reading order.

    Measured on the 1720/1721 carton-detail PDF: every figure is centred under
    its size header and the headers sit ~29pt apart, so nearest-centre placement
    is unambiguous there. A layout where it is not simply yields no carton rows,
    and nothing is backed from it.
    """
    from claude_extractor import SheetGrid, open_pdf

    from attachment_classifier import _classify_size_cell, _size_vocabulary

    vocabulary = _size_vocabulary()
    rows: list[list[str]] = []
    anchors: Optional[list[float]] = None
    with open_pdf(path) as pdf:
        for page in pdf.pages:
            lines: list[list[Any]] = []
            for word in sorted(page.extract_words(), key=lambda w: (w["top"], w["x0"])):
                for line in lines:
                    if abs(line[0] - word["top"]) <= 2.5:
                        line[1].append(word)
                        break
                else:
                    lines.append([word["top"], [word]])
            for _top, words in lines:
                words.sort(key=lambda w: w["x0"])
                letters = [w for w in words
                           if (lambda c: c[0] is not None and not c[1])(
                               _classify_size_cell(w["text"], vocabulary))]
                if len(letters) >= 3:
                    anchors = [(w["x0"] + w["x1"]) / 2 for w in words]
                if anchors is None:
                    rows.append([" ".join(w["text"] for w in words)])
                    continue
                cells = [""] * len(anchors)
                for w in words:
                    centre = (w["x0"] + w["x1"]) / 2
                    i = min(range(len(anchors)), key=lambda k: abs(anchors[k] - centre))
                    cells[i] = f"{cells[i]} {w['text']}".strip()
                rows.append(cells)
    width = max((len(r) for r in rows), default=0)
    return SheetGrid(name="PDF", rows=[r + [""] * (width - len(r)) for r in rows],
                     first_col=1)


def _evidence_sheets(path: Union[str, Path]) -> list[_Sheet]:
    """Every grid of another attachment that could hold carton rows. Visible only."""
    from claude_extractor import (
        FORMAT_PDF,
        WORKBOOK_FORMATS,
        read_workbook_grids,
        sniff_format,
        visible_grids,
    )

    fmt = sniff_format(Path(path))
    try:
        if fmt in WORKBOOK_FORMATS:
            grids, _notes = visible_grids(read_workbook_grids(path))
            return [_Sheet(g) for g in grids if not g.is_empty]
        if fmt == FORMAT_PDF:
            return [_Sheet(pdf_carton_grid(path))]
    except Exception as exc:  # noqa: BLE001 -- unreadable evidence is no evidence
        logger.warning("carton backing: could not read evidence %s: %s", Path(path).name, exc)
    return []


def _runs_summing_to(sheet: _Sheet, target: dict[str, float]) -> list[tuple[int, ...]]:
    """
    Every contiguous run of `sheet`'s carton rows whose sizes sum to `target`,
    WITHIN ONE TABLE -- the rows under a single size header.

    Header rows are not cartons, so the carton list steps straight over them: on
    the 1720/1721 carton PDF, PO 1720's last carton and PO 1721's first are
    neighbours in the list. A run allowed to cross that would add one PO's
    cartons to another's, so a run stops at a header.
    """
    out = []
    tables: dict[int, list[tuple[int, dict[str, float]]]] = {}
    for row, contribution in sheet.cartons:
        governing = max((h for h, _labels in sheet.headers if h < row), default=0)
        tables.setdefault(governing, []).append((row, contribution))
    for cartons in tables.values():
        for start in range(len(cartons)):
            accumulated: dict[str, float] = {}
            for end in range(start, len(cartons)):
                for label, value in cartons[end][1].items():
                    accumulated[label] = accumulated.get(label, 0.0) + value
                if _same(accumulated, target):
                    out.append(tuple(r for r, _c in cartons[start:end + 1]))
    return out


def _back_across_attachments(kept: list[dict], evidence: Sequence[tuple[Any, str]],
                             primary: Union[str, Path], notes: list[str]) -> None:
    """
    Upgrade UNVERIFIABLE lines to BACKED where ANOTHER attachment's cartons sum
    to their recap row (Kiko, 2026-09-28).

    A recap row is the group of lines sharing PO, style, colour and recap label;
    its target is their size-by-size quantities. It is BACKED when a contiguous
    run of carton rows in some other attachment of the shipment sums to exactly
    that target. The 1720/1721 covering PDF prints recaps and no cartons; the
    carton-detail PDF in the same email prints the cartons.

    **Only ever upgrades.** A target no other attachment backs stays
    UNVERIFIABLE -- another document not containing a recap's cartons says
    nothing about it (the same email carried Inprotex's and Legendz's workbooks
    for other POs), so it is never grounds to drop a line. A line already
    BACKED or UNBACKED by its own document is not touched. An attachment with
    the primary's own bytes is not independent evidence and is skipped.
    """
    from attachment_classifier import _sha256_file
    from canonical import canonical
    from netsuite_client import po_number_key

    pending = [ln for ln in kept if ln["carton_backing"]["status"] == UNVERIFIABLE]
    if not pending or not evidence:
        return
    try:
        primary_sha = _sha256_file(Path(primary))
    except OSError:
        primary_sha = None
    sources = []
    for path, name in evidence:
        try:
            if primary_sha is not None and _sha256_file(Path(path)) == primary_sha:
                continue
        except OSError:
            continue
        sheets = _evidence_sheets(path)
        if any(s.cartons for s in sheets):
            sources.append((name, sheets))
    if not sources:
        return

    groups: dict[tuple, list[dict]] = {}
    for line in pending:
        # `canonical`, as every other grouping in the project: `NEW  INDIGO` and
        # `NEW INDIGO` are one recap, and splitting it would leave both halves
        # unbackable.
        key = (po_number_key(str(line.get("po_number") or "")),
               *(canonical(line.get(k)) for k in ("style_number", "color", "recap_label")))
        groups.setdefault(key, []).append(line)
    for key, members in groups.items():
        target: dict[str, float] = {}
        for line in members:
            label, quantity = _size_label(line.get("size")), _num(line.get("quantity"))
            if label is None or quantity is None:
                target = {}
                break
            target[label] = target.get(label, 0.0) + quantity
        if not target:
            continue
        found = [(name, sheet, run) for name, sheets in sources for sheet in sheets
                 for run in _runs_summing_to(sheet, target)]
        if not found:
            continue
        cited = "; ".join(
            f"'{name}'" + (f" sheet '{sheet.grid.name}'" if sheet.grid.name != "PDF" else "")
            + f" carton rows {run[0]}-{run[-1]} ({len(run)} row(s))"
            for name, sheet, run in found)
        reason = (f"backed by another attachment of this shipment: {cited} sum exactly "
                  f"to this recap ({_show(target)})")
        # `carton_rows` / `carton_source` name the first match; `carton_runs` holds
        # EVERY match, so the stored fields never say less than the reason does.
        # These rows belong to `carton_source`, not to the line's own document.
        for line in members:
            line["carton_backing"] = {
                "status": BACKED, "reason": reason,
                "carton_rows": list(found[0][2]), "carton_source": found[0][0],
                "carton_runs": [{"source": name, "sheet": sheet.grid.name,
                                 "rows": list(run)} for name, sheet, run in found]}
        notes.append(f"carton backing across attachments, {'/'.join(key[:3])}: {reason}")


def check_lines(lines: Sequence[dict], path: Union[str, Path],
                evidence: Sequence[tuple[Any, str]] = ()) -> BackingResult:
    """
    Split extracted lines into kept and dropped by carton backing.

    Each KEPT line gains `line["carton_backing"] = {"status", "reason",
    "carton_rows"}`; the matcher turns UNVERIFIABLE into a review flag. DROPPED
    lines are UNBACKED and leave the shipment -- named, row by row, in
    `warnings`, so the removal is on the shipment's record rather than silent.

    `evidence` is `[(path, display_name), ...]`: the shipment's OTHER usable
    attachments. A line its own document cannot verify may be backed by their
    cartons -- see `_back_across_attachments`. Never pass an excluded attachment
    (an inspection report is not a data source, Paula 2026-08-11).
    """
    from claude_extractor import WORKBOOK_FORMATS, read_workbook_grids, sniff_format

    result = BackingResult()
    sheets: dict[str, _Sheet] = {}
    is_workbook = False
    try:
        is_workbook = sniff_format(Path(path)) in WORKBOOK_FORMATS
        if is_workbook:
            sheets = {g.name: _Sheet(g) for g in read_workbook_grids(path)}
    except Exception as exc:  # noqa: BLE001 -- unreadable means unverifiable, not dropped
        logger.warning("carton backing: could not read %s: %s", Path(path).name, exc)
        is_workbook = False

    verdicts: dict[tuple[str, int], RowBacking] = {}

    def verdict(sheet: str, row: int) -> RowBacking:
        if (sheet, row) not in verdicts:
            grid = sheets.get(sheet)
            verdicts[(sheet, row)] = grid.backing(row) if grid else RowBacking(
                UNVERIFIABLE, sheet, row,
                ("no worksheet named '%s' in this workbook" % sheet) if is_workbook
                else "the source document is not a workbook, so there is no grid of "
                     "cartons to check against")
        return verdicts[(sheet, row)]

    dropped_by_row: dict[tuple[str, int], list[dict]] = {}
    for line in lines:
        cited = _parse_hints(line.get("source_hint"))
        if not cited:
            status, reason, rows = (UNVERIFIABLE,
                                    f"source hint {line.get('source_hint')!r} names no sheet row",
                                    ())
        else:
            found = [verdict(sheet, row) for sheet, row in cited]
            statuses = {v.status for v in found}
            if statuses == {BACKED}:
                status = BACKED
            elif statuses == {UNBACKED}:
                status = UNBACKED
            else:
                status = UNVERIFIABLE
            reason = "; ".join(v.reason for v in found)
            rows = tuple(r for v in found for r in v.carton_rows)
            # The hinted row(s) must actually hold this line's figure, or the hint
            # points somewhere else and the verdict is about the wrong row.
            quantity = _num(line.get("quantity"))
            if status != UNVERIFIABLE and quantity is not None:
                problem = _quantity_problem(sheets, cited, line.get("size"), quantity)
                if problem:
                    status, reason, rows = UNVERIFIABLE, problem, ()
        if status == UNBACKED:
            result.dropped.append(line)
            for sheet, row in cited:
                dropped_by_row.setdefault((sheet, row), []).append(line)
            continue
        line["carton_backing"] = {"status": status, "reason": reason,
                                  "carton_rows": list(rows)}
        result.kept.append(line)

    _back_across_attachments(result.kept, evidence, path, result.notes)

    for (sheet, row), dropped in dropped_by_row.items():
        keys = ", ".join(f"{d.get('style_number')}/{d.get('color')}/{d.get('size')}="
                         f"{d.get('quantity')}" for d in dropped)
        result.warnings.append(
            f"NOT SHIPMENT DATA -- sheet '{sheet}' row {row}: "
            f"{verdicts[(sheet, row)].reason}. {len(dropped)} line(s) removed from this "
            f"shipment: {keys}. Rule (Paula, 2026-09-23): a quantity row counts only if "
            "cartons in the same document back it.")
    counts = {s: sum(1 for ln in result.kept if ln['carton_backing']['status'] == s)
              for s in (BACKED, UNVERIFIABLE)}
    result.notes.append(
        f"carton backing: {counts[BACKED]} line(s) backed, {counts[UNVERIFIABLE]} "
        f"unverifiable (kept, flagged), {len(result.dropped)} unbacked (removed)")
    for (sheet, row), v in sorted(verdicts.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        result.notes.append(f"carton backing, sheet '{sheet}' row {row}: {v.status} -- {v.reason}")
    return result
