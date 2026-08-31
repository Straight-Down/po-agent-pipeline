"""
PO matcher + diff/staging logic.

Takes normalized vendor line items (the output of the parsing layer) and the
corresponding current NetSuite PO state (via NetSuiteClient), matches them by
(po_number, style, color, size), and produces the `proposed_changes` records
described in PO-Update-Automation-Architecture.md section 5. Nothing here writes
to NetSuite; this only stages what *would* change, for human review.

Matching keys, confirmed against a live NetSuite sandbox record (PO 1662 /
style M120246): style-color-size is one child Item record per SKU (not a matrix
item), color/size are reference fields on the PO line (`custcol_product_color`,
`custcol_product_size`), and NetSuite's canonical size labels are `2X`/`3X` —
NOT `XXL`/`XXXL`, which is what some vendors print for the same sizes.
SIZE_ALIASES below normalizes for that; extend it as new vendors appear.

## Paula's rulings (2026-08-11) — these are decisions, not defaults

**Receipt dates are never proposed by this engine.** It does not compute, infer,
or suggest a value for `expectedReceiptDate` or `custcol_sd_updatedreceiptdate`.
Paula determines the actual receipt date herself; no vendor-stated date is
treated as an answer. The vendor's ETD/ETA travel through as clearly labelled
*reference* fields for her to read, and `confirmed_receipt_date` stays None until
a human types one in. `to_netsuite_fields()` physically cannot emit a date field
before that happens — see `DateNotConfirmed`. This replaces the earlier
behaviour, which proposed the raw port ETA for all three date fields.

**Quantity replaces, and over-shipment is normal.** The packing list's shipped
quantity becomes the line's new quantity, replacing the ordered amount. Shipped
exceeding ordered is standard practice and is NOT flagged, surfaced as an
anomaly, or treated differently in any way.

**A PO line absent from a packing list is the normal case, not an event.** POs
routinely ship in batches, so most shipments cover only some of a PO's styles.
Lines with no vendor data produce no change record at all — a silent no-op, not
a flag. Attention flags are reserved for genuinely unexpected mismatches.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Optional

from canonical import canonical
from netsuite_client import (
    NS_EXPECTED_RECEIPT_DATE,
    NS_OVERRIDE_EXPECTED_RECEIPT,
    NS_QUANTITY,
    NS_UPDATED_RECEIPT_DATE,
    NetSuiteClient,
    POLine,
)

# Vendor size label -> NetSuite canonical size label.
# Confirmed against live NetSuite data: custcol_sd_tmpl_size_run = "S,M,L,XL,2X,3X"
SIZE_ALIASES = {
    "XXL": "2X",
    "XXXL": "3X",
    "2XL": "2X",
    "3XL": "3X",
}

#: Extraction confidences that require a human to look before anything is written.
REVIEW_CONFIDENCES = {"medium", "low"}

STATUS_PENDING_REVIEW = "PENDING_REVIEW"
STATUS_NO_CHANGE = "NO_CHANGE"
STATUS_NEEDS_ATTENTION = "NEEDS_ATTENTION"

#: One extracted line matched SEVERAL open NetSuite lines. The tool does not pick
#: and does not sum -- a human chooses. See `_resolve_target_line`.
STATUS_NEEDS_RESOLUTION = "NEEDS_RESOLUTION"


class LineClosed(Exception):
    """
    Raised when a write is built for a PO line that is closed in NetSuite.

    `netsuite_client` has always read `isClosed` into `POLine.closed`; nothing
    checked it before proposing a change. Now the diff engine flags such lines
    `NEEDS_ATTENTION` and this makes the write path refuse them outright.
    """


class LineAmbiguous(Exception):
    """
    Raised when a write is built for a change whose target line was never chosen.

    `(PO, style, colour, size)` is not unique per NetSuite line, so an extracted
    line can match several open lines. The engine refuses to guess; this makes
    that refusal structural rather than advisory.
    """


class DateNotConfirmed(Exception):
    """
    Raised when a write would include a receipt date no human has confirmed.

    This is the enforcement point for Paula's ruling. It exists so the rule is
    structural rather than a comment someone has to remember: there is no code
    path that emits `expectedReceiptDate` or `custcol_sd_updatedreceiptdate`
    from a vendor-stated date.
    """


#: SIZE_ALIASES keyed by canonical form, so " xxl ", "XXL" and a full-width
#: variant all resolve. Derived rather than hand-maintained, so SIZE_ALIASES stays
#: the single editable source of truth.
_SIZE_ALIASES_CANON = {canonical(k): v for k, v in SIZE_ALIASES.items()}


def _normalize_size(size: str) -> str:
    """
    Vendor size label -> NetSuite's canonical label (e.g. "XXL" -> "2X").

    Returns NetSuite's own casing, because that is what a human reading a review
    row expects to see. Use `_size_key` for comparisons, never this.
    """
    return _SIZE_ALIASES_CANON.get(canonical(size), str(size).strip())


def _size_key(size: str) -> str:
    """
    Comparison key for a size: resolve the vendor alias, then canonicalise.

    Applied to BOTH sides of a match, so "XXL", "2XL", "2x" and full-width or
    dash variants all key alike -- and so do NetSuite's own stored values.
    """
    return canonical(_normalize_size(size))


@dataclass
class ColourLookup:
    """
    One PO's colour vocabulary: long-form name -> the code(s) it means, ON THIS PO.

    Built per PO by `build_colour_lookup`. `by_name` keys and values are canonical
    (change 4); `display` maps a canonical code back to the name as NetSuite spells
    it, for review messages. `missing_names` lists codes whose item carried no
    colour name -- a printed name can never resolve to those, so they flag.
    """

    by_name: dict = field(default_factory=dict)
    display: dict = field(default_factory=dict)
    missing_names: list = field(default_factory=list)
    #: canonical code -> the item internal id whose record supplied its name. Kept
    #: so the persisted provenance can name the source, not just the answer.
    name_source: dict = field(default_factory=dict)


@dataclass
class ProposedChange:
    """
    One staged change to one NetSuite PO line.

    Note what is absent: there is no `proposed_expected_receipt_date` or
    `proposed_updated_receipt_date` field. Their existence is what invited the
    engine to guess a date. The vendor's dates live in `vendor_etd`/`vendor_eta`
    as reference only, and the writable date comes from
    `confirmed_receipt_date`, which only a human sets.
    """

    po_number: str
    style_number: str
    color: str
    size: str
    line_id: Optional[str]

    # Quantity — replace semantics; this is the part that can be plainly approved.
    current_quantity: Optional[int]
    proposed_quantity: Optional[int]

    #: The matched line's item record id, and whether NetSuite still considers the
    #: line open. Both come from `POLine` (change 5) and were previously dropped
    #: here, so they landed NULL in the database. `ns_line_is_open` is what the
    #: review screen needs: `line_closed` is NOT its complement -- a Fully Billed
    #: line is neither open nor closed -- so "can this still be updated?" cannot be
    #: answered from `line_closed` alone.
    ns_item_internal_id: Optional[str] = None
    ns_line_is_open: Optional[bool] = None

    # Current NetSuite date state, for display next to the reference dates.
    current_expected_receipt_date: Optional[str] = None
    current_updated_receipt_date: Optional[str] = None
    current_override_flag: bool = False

    # Vendor-stated dates. REFERENCE ONLY — never written, never proposed.
    vendor_etd: Optional[str] = None
    vendor_eta: Optional[str] = None

    # Set only by a human typing an actual receipt date. None = not yet supplied.
    confirmed_receipt_date: Optional[str] = None

    status: str = STATUS_PENDING_REVIEW
    attention_reason: str = ""
    extraction_confidence: str = "high"
    extraction_note: str = ""

    #: True when the matched NetSuite line is closed. Such a line is never
    #: written to automatically — `to_netsuite_fields()` refuses.
    line_closed: bool = False

    #: The five quantity figures for this line, on EVERY change -- see
    #: `_line_balance`. Display context, never a gate.
    line_balance: dict = field(default_factory=dict)

    #: Set when the colour matched through the item's long-form NAME rather than by
    #: code, e.g. "printed 'NEW INDIGO' resolved to code NIN ('New Indigo')".
    #: The human sentence, for display.
    colour_resolution: str = ""

    #: The same thing structured, for persistence: method, the canonical printed
    #: value that was looked up, the code it resolved to, the long name that
    #: supplied the mapping, and the item whose record that name came from.
    #:
    #: **Persisted because it is not reconstructable later.** The item read is not
    #: stored, and a PO's colour set changes as lines are added or received -- so
    #: re-deriving "why did NEW INDIGO become NIN" from a row six months old is
    #: guesswork. The answer has to be written down when it is known.
    colour_provenance: dict = field(default_factory=dict)

    #: Every NetSuite line whose canonical key matched, when the match was not a
    #: clean 1:1. Populated for NEEDS_RESOLUTION (several open lines) and for the
    #: no-open-line case, so a human has what they need to decide without going
    #: back to NetSuite. Deliberately excludes custcol_sd_fg_excluderepspark:
    #: that field is managed by hand and this tool neither reads, writes nor
    #: displays it.
    candidate_lines: list = field(default_factory=list)

    # -- derived state ------------------------------------------------------

    @property
    def quantity_changed(self) -> bool:
        return (
            self.proposed_quantity is not None
            and self.current_quantity is not None
            and self.proposed_quantity != self.current_quantity
        )

    @property
    def receipt_date_pending(self) -> bool:
        """True while no human has supplied an actual receipt date."""
        return not self.confirmed_receipt_date

    @property
    def reference_dates_label(self) -> str:
        """
        How the review UI should present the vendor's dates: as information,
        explicitly not as a proposal.
        """
        bits = []
        if self.vendor_etd:
            bits.append(f"vendor ETD {self.vendor_etd}")
        if self.vendor_eta:
            bits.append(f"vendor ETA {self.vendor_eta}")
        if not bits:
            return "No vendor date on the shipment documents — enter the receipt date."
        return (
            "Reference only, not a proposed value ("
            + "; ".join(bits)
            + "). Enter the actual receipt date."
        )

    # -- writing ------------------------------------------------------------

    def confirm_receipt_date(self, value: str | dt.date) -> None:
        """Record the receipt date a human typed in. Validates the format."""
        if isinstance(value, dt.date):
            self.confirmed_receipt_date = value.isoformat()
            return
        text = str(value).strip()
        try:
            self.confirmed_receipt_date = dt.date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise ValueError(f"receipt date must be an ISO date (YYYY-MM-DD), got {value!r}") from exc

    def to_netsuite_fields(self, include_dates: bool = True) -> dict:
        """
        The NetSuite field dict for this change's approved write.

        Quantity is included whenever it changed. The three date fields are
        included ONLY if a human has confirmed a receipt date; asking for dates
        without one raises `DateNotConfirmed` rather than quietly omitting them,
        so a caller cannot believe it wrote a date that it didn't.

        Pass include_dates=False for the quantity-only approval path, which is
        the normal case: quantity can be approved on its own without Paula having
        yet decided the receipt date.
        """
        if self.status == STATUS_NEEDS_RESOLUTION:
            raise LineAmbiguous(
                f"PO {self.po_number} {self.style_number} {self.color}-{self.size}: this key "
                f"matches {len(self.candidate_lines)} open NetSuite lines and no target was "
                "chosen. Refusing to build a write. A human picks the line; the engine must "
                "not guess, and must never sum the candidates into one."
            )

        if self.line_closed:
            raise LineClosed(
                f"PO {self.po_number} {self.style_number} {self.color}-{self.size} (line "
                f"{self.line_id}) is closed in NetSuite. Refusing to build a write for it — a "
                "closed line was deliberately finished with, so a vendor document referencing it "
                "is a discrepancy for a human to explain, not a quantity to overwrite."
            )

        fields: dict = {}
        if self.quantity_changed:
            fields[NS_QUANTITY] = self.proposed_quantity

        if include_dates:
            if self.receipt_date_pending:
                raise DateNotConfirmed(
                    f"PO {self.po_number} {self.style_number} {self.color}-{self.size}: refusing to "
                    "write a receipt date that no human confirmed. Paula sets this value; the "
                    f"vendor's dates ({self.reference_dates_label}) are reference only. Either "
                    "call confirm_receipt_date() first or use include_dates=False to write the "
                    "quantity alone."
                )
            fields[NS_EXPECTED_RECEIPT_DATE] = self.confirmed_receipt_date
            fields[NS_UPDATED_RECEIPT_DATE] = self.confirmed_receipt_date
            fields[NS_OVERRIDE_EXPECTED_RECEIPT] = True

        return fields

    def as_dict(self) -> dict:
        return asdict(self)


def _candidate_payload(line: POLine) -> dict:
    """
    What a human needs to choose between candidate lines.

    Deliberately excludes `custcol_sd_fg_excluderepspark` -- that field is managed
    manually by Paula and is outside this tool's scope entirely.
    """
    return {
        "line_id": line.line_id,
        "quantity": line.quantity,
        "quantity_received": line.quantity_received,
        "quantity_billed": line.quantity_billed,
        "expected_receipt_date": _iso_or_none(line.expected_receipt_date),
        "override_expected_receipt": line.override_expected_receipt,
        "updated_receipt_date": _iso_or_none(line.updated_receipt_date),
        "rate": line.rate,
        "is_open": line.is_open,
    }


def _find_matching_lines(
    vendor_line: dict,
    ns_lines: list[POLine],
    colour_lookup: Optional[ColourLookup] = None,
) -> tuple[list[POLine], str, str, dict]:
    """
    ALL NetSuite lines whose canonical key matches -- not the first one.

    Matches on exact style_number (from custcol_sd_tmpl_style /
    custcol_cmo_parentitem.refName) plus exact colour/size refName, with the
    vendor's size label normalized to NetSuite's convention first.

    `(PO, style, colour, size)` is **not unique per NetSuite PO line**: across
    1,659 POs, 64 carry duplicate-key lines. They are created during receiving
    rather than at PO entry (0 of 89 Pending Receipt POs have them, versus 4 of 17
    Partially Received), so this pipeline meets them disproportionately -- a second
    packing slip against a partially-received PO is exactly the case it exists for.

    The previous `_find_matching_line` returned the first match and silently
    ignored the rest, which meant one line was updated and its twin left stale
    with no flag.

    Returns `(lines, colour_resolution, colour_problem, colour_provenance)` -- the
    colour may have been recovered from the item's long-form name
    (`_resolve_colour_codes`), which is worth recording and persisting, and may have
    been ambiguous, which must flag.
    """
    style = canonical(vendor_line.get("style_number"))
    printed_colour = canonical(vendor_line.get("color"))
    size = _size_key(vendor_line.get("size"))
    colours, resolution, problem, provenance = _resolve_colour_codes(
        printed_colour, ns_lines, colour_lookup
    )
    matches = [
        line
        for line in ns_lines
        # BOTH operands are canonicalised. Normalising only the extracted side
        # would relocate the mismatch rather than fix it -- there is no guarantee
        # NetSuite's stored colour is clean either.
        if canonical(line.style_number) == style
        and canonical(line.color) in colours
        and _size_key(line.size) == size
    ]
    return matches, resolution, problem, provenance


def build_colour_lookup(
    client: NetSuiteClient, ns_lines: list[POLine], cache: Optional[dict] = None
) -> ColourLookup:
    """
    Build one PO's name -> code lookup, reading each distinct colour's item once.

    **Scoped to this PO, never a global table, and that is the whole design.**

    Vendors do not agree on how to write a colour. Legendz prints the code
    (`MLT`, `DKF`); Symmetry prints the name (`NEW INDIGO`, `BLACK`, `COCONUT`).
    NetSuite stores only the code on the PO line, and the long name on the child
    item. So a name-printing vendor needs the code recovered from the name -- and
    the safe way to do that is against the handful of colours on the PO in hand.

    **Why scoped, now that the numbers are in.** The original argument was that a
    global map would collide. On the real data it barely would: across items on open
    POs, exactly **one** name maps to two codes (`'Navy / Silver'` -> `NAV` and
    `NVSL`), five codes have items that disagree on spelling (`FUS`, `MLK`, `CHC`,
    `NAV`, `NIN`), and **per PO there is not a single collision across 133 open
    POs**. So collision risk is not the reason, and citing it would overstate the
    case. (An earlier probe's "51 codes carry multiple descriptions" figure came
    from an invalid join and is retracted -- do not requote it.)

    The reasons that survive are about maintenance, and they are enough:

    - **No seeded table**, so nothing to populate by hand for 589 colour values,
      and no chance of seeding one wrong.
    - **No refresh story.** A cached global map goes stale the moment a colour is
      added -- and colour values *are* still being added (the newest in sandbox was
      created 2026-06-03). A per-PO lookup is built from live data every time.
    - **No drift** between what the map says and what the PO actually holds. The
      lookup is derived from the PO's own lines, so it cannot disagree with them.
    - **Coverage is not a concern either way**: all 114 codes on open POs have a
      name, on 2,390 of 2,393 items (the three exceptions are poly mailers, which
      have no colour).

    Same principle as Paula's ruling on sizes: resolve against the vocabulary
    actually in play, never against a vendor profile or a global table.
    """
    lookup = ColourLookup()
    cache = cache if cache is not None else {}
    first_item_for_code: dict[str, POLine] = {}
    for line in ns_lines:
        code = canonical(line.color)
        if code and code not in first_item_for_code:
            first_item_for_code[code] = line

    for code, line in first_item_for_code.items():
        if line.item_internal_id is None:
            lookup.missing_names.append(line.color)
            continue
        name = client.get_item_colour_name(line.item_internal_id, cache=cache)
        if not name:
            # No name to match against. The line is still matchable by CODE; a
            # printed name simply cannot reach it, and will flag.
            lookup.missing_names.append(line.color)
            continue
        lookup.by_name.setdefault(canonical(name), set()).add(code)
        lookup.display[code] = name
        lookup.name_source[code] = str(line.item_internal_id)
    return lookup


def _resolve_colour_codes(
    printed: str, ns_lines: list[POLine], lookup: Optional[ColourLookup]
) -> tuple[set, str, str, dict]:
    """
    Which NetSuite colour code(s) does the printed colour mean, on THIS PO?

    Returns `(codes, resolution_note, problem, provenance)`. `problem` is non-empty
    only when a printed name is ambiguous on this PO -- two colours it could equally
    be. That case is flagged with both candidates and never resolved, following
    change 5: a wrong colour writes a quantity against the wrong product, which is
    exactly the kind of error nobody notices downstream.

    `provenance` is the structured record persisted on the row: method, printed key,
    resolved code, the name that supplied the mapping and the item it came from.

    **Order matters.** Code match first, so a code-printing vendor needs no item
    read at all. Only then the name path.

    **No fuzzy matching, at any point.** `BLK`/`BLC`, `COO`/`COC` and `HER`/`H` are
    all live colour values in this account. Initial-matching or substring-matching
    would produce confident wrong answers on exactly the pairs that matter.
    """
    po_codes = {canonical(line.color) for line in ns_lines}

    def record(method: str, code: Optional[str] = None) -> dict:
        # The provenance row. `name` and `name_source_item_id` are filled ONLY for a
        # NAME resolution: on the code path no name was consulted, so attributing
        # one -- even a correct one the lookup happens to hold -- would misreport
        # how the match was actually made.
        attributed = method == "NAME" and lookup is not None and code is not None
        return {
            "method": method,
            "printed": printed,
            "code": code,
            "name": lookup.display.get(code) if attributed else None,
            "name_source_item_id": lookup.name_source.get(code) if attributed else None,
        }

    if printed in po_codes:
        return {printed}, "", "", record("CODE", printed)

    if lookup is None or not lookup.by_name:
        # No name data (offline, mock client, or nothing populated). Behaviour is
        # then exactly what it was before this change: code comparison only.
        return {printed}, "", "", record("UNRESOLVED")

    candidates = lookup.by_name.get(printed, set()) & po_codes
    if len(candidates) == 1:
        code = next(iter(candidates))
        display = lookup.display.get(code, code)
        return candidates, (
            f"printed colour {printed!r} resolved to code {code.upper()} "
            f"({display!r}) via the item's colour name"
        ), "", record("NAME", code)

    if len(candidates) > 1:
        named = ", ".join(
            f"{c.upper()} ({lookup.display.get(c, c)!r})" for c in sorted(candidates)
        )
        return candidates, "", (
            f"printed colour {printed!r} matches {len(candidates)} colours on this PO "
            f"({named}). Not choosing between them -- a wrong colour would write this "
            "quantity against the wrong product"
        ), record("AMBIGUOUS")

    return {printed}, "", "", record("UNRESOLVED")


def _line_balance(line: Optional[POLine], slip_quantity: Optional[float]) -> dict:
    """
    The quantity figures for one line, so a human can read the situation directly.

    Attached to every change, not only flagged ones. The review screen can then
    say "ordered 300, received 0, this slip 128" and a partial delivery is
    self-evident to the person who can actually judge it. **The numbers are the
    signal; this tool does not interpret them.** `outstanding` is
    `quantity - quantity_received`, with a missing received count treated as zero.

    All five values are None-safe: an unmatched vendor line still gets a payload,
    carrying its slip quantity with the line-side figures None. "Nothing matched,
    and the slip said 128" is itself worth showing.

    **This is deliberately not a gate, and the reasoning is worth keeping** --
    a version that refused to propose anything when the slip fell short of
    outstanding was built and cancelled:

    - **A final short-ship and a partial delivery are the same document.**
      Production came in light, or the balance is following by sea: slip quantity
      below line quantity either way. No arithmetic on these numbers separates
      them, so a rule built on them mislabels one of the two by construction.
    - **It would have removed the tool's main job.** With `quantity_received = 0`,
      outstanding equals ordered, so "slip equals outstanding" means "nothing to
      update but the date" -- the tool could only ever have proposed a quantity
      change on a line that already had receipts. On the real PO 1662 example it
      went from 2 proposals to 0.
    - **The premise was wrong.** The worry was that the tool would set a date
      before Paula saw the slip. Nothing is ever written without her approval, so
      there is no such race. And she knows a line split is coming because she
      arranges the air shipment herself -- the recognition happens before the
      packing slip arrives, not during review.

    So: show her the numbers, and leave the judgement where it already was.
    """
    received = None if line is None else float(line.quantity_received or 0.0)
    return {
        "ns_line_id": line.line_id if line is not None else None,
        "line_quantity": line.quantity if line is not None else None,
        "quantity_received": received,
        "slip_quantity": slip_quantity,
        "outstanding": None if line is None else float(line.quantity) - received,
    }


def _resolve_target_line(
    candidates: list[POLine],
) -> tuple[Optional[POLine], Optional[str], list[POLine]]:
    """
    Decide which of several matching NetSuite lines to update.

    Returns `(target, problem, ambiguous_lines)`. `target` is the line to write to,
    or None when there is nothing safe to write. `problem` is the reason, phrased
    for a reviewer. `ambiguous_lines` is non-empty *only* when a human has to pick
    between open lines — that is what separates NEEDS_RESOLUTION from the ordinary
    NEEDS_ATTENTION cases.

    The rule, and why each branch is what it is:

    - **Filter to `is_open`.** Only an open line can still receive an update.
      Deliberately `is_open`, NOT `not closed`: on a Fully Billed PO every line has
      isClosed=False (nobody ticks the per-line Closed box) *and* isOpen=False, so
      reading the Closed checkbox as "open" reports settled lines as live. That
      mistake has already been made once, on this data.
    - **Exactly one open line** — that is the target, however many closed or
      already-received twins sit beside it. 24 of the 25 duplicate groups on the
      live population land here.
    - **No open line** — no update, flagged. Did not occur on the live population,
      but it has to be a defined outcome rather than an exception. A deliberately
      closed line keeps its own wording and still sets `line_closed`, so the
      structural refusal in `to_netsuite_fields()` stays alive.
    - **Two or more open lines** — NEEDS_RESOLUTION. No pick, no sum.

    **No tiebreaker, deliberately.** `quantity_received` looks like it would settle
    the one live ambiguous case (50 units received 0 versus 200 units received
    100), and it probably would — but that is n=1, and the failure mode of a wrong
    automatic pick is silent: the wrong line is updated and the right one goes
    stale with nothing to notice. Flagging costs a human one decision every few
    weeks. If a rule emerges from the choices Paula actually makes, encode it then,
    with evidence. The receipt figures are surfaced in `candidate_lines` so a
    person can read them; branching on them is a different thing.
    """
    if not candidates:
        return None, None, []

    open_lines = [line for line in candidates if line.is_open]

    if len(open_lines) == 1:
        return open_lines[0], None, []

    if not open_lines:
        if all(line.closed for line in candidates):
            # Same wording as the single-line closed path, which this supersedes
            # once NetSuite reports a closed line as isOpen=False.
            return None, (
                "PO line is closed in NetSuite; vendor data references it but no automatic "
                "change proposed"
            ), []
        ids = ", ".join(str(line.line_id) for line in candidates)
        return None, (
            f"{len(candidates)} NetSuite line(s) match this style/colour/size "
            f"(line {ids}) but none is open, so none can be updated — the PO has "
            "most likely been received, billed or closed already"
        ), []

    ids = ", ".join(str(line.line_id) for line in open_lines)
    return None, (
        f"{len(open_lines)} open NetSuite lines match this style/colour/size "
        f"(line {ids}). The tool does not choose between them and does not sum them "
        "— pick the line this shipment belongs against"
    ), open_lines


def _as_quantity(value) -> Optional[float]:
    """A slip quantity as a number for display arithmetic; None stays None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_or_none(value: Optional[dt.date]) -> Optional[str]:
    return value.isoformat() if value else None


def _parse_eta_to_date(eta_str: Optional[str]) -> Optional[dt.date]:
    """
    '2026/6/27 16:45' -> date(2026, 6, 27).

    Retained only to normalize a vendor date for *display* as reference
    information. Its result is never written to NetSuite and never proposed as a
    receipt date — see the module docstring.
    """
    if not eta_str:
        return None
    date_part = str(eta_str).strip().split(" ")[0]
    try:
        if "/" in date_part:
            y, m, d = (int(x) for x in date_part.split("/"))
            return dt.date(y, m, d)
        return dt.date.fromisoformat(date_part)
    except (ValueError, TypeError):
        return None


def build_proposed_changes(
    vendor_lines: list[dict],
    client: NetSuiteClient,
    eta: Optional[str] = None,
    etd: Optional[str] = None,
    shipment_needs_manual_entry: bool = False,
    colour_lookups: Optional[dict] = None,
) -> list[ProposedChange]:
    """
    Stage the changes a shipment implies, for human review.

    One record per *vendor* line. NetSuite lines with no vendor line produce
    nothing at all (Paula: partial shipments are the normal case).

    `shipment_needs_manual_entry=True` marks every record NEEDS_ATTENTION — used
    when the parsing layer could not resolve the shipment to style/colour/size
    lines from an acceptable source document.

    `colour_lookups` maps a PO number to its `ColourLookup`, letting a vendor's
    printed colour NAME resolve to NetSuite's code (see `build_colour_lookup`).
    Omit it and matching is by code only, which is what a code-printing vendor
    needs and all this did before change 7. The lookups are passed in rather than
    built here so this function stays free of per-item I/O.
    """
    eta_date = _parse_eta_to_date(eta)
    etd_date = _parse_eta_to_date(etd)
    reference_eta = eta_date.isoformat() if eta_date else (str(eta).strip() if eta else None)
    reference_etd = etd_date.isoformat() if etd_date else (str(etd).strip() if etd else None)

    po_numbers = sorted({str(vl.get("po_number") or "").strip() for vl in vendor_lines})
    ns_lines_by_po = {po: client.get_purchase_order(po) for po in po_numbers if po}

    changes: list[ProposedChange] = []
    for vl in vendor_lines:
        po_number = str(vl.get("po_number") or "").strip()
        confidence = str(vl.get("confidence") or "high").lower()
        note = str(vl.get("note") or "")
        ns_lines = ns_lines_by_po.get(po_number, [])
        # ALL matching lines, not just the first — the key is not unique per line.
        candidates, colour_resolution, colour_problem, colour_provenance = (
            _find_matching_lines(vl, ns_lines, (colour_lookups or {}).get(po_number))
        )
        match, resolution_problem, ambiguous_lines = _resolve_target_line(candidates)

        change = ProposedChange(
            po_number=po_number,
            style_number=str(vl.get("style_number") or "").strip(),
            color=str(vl.get("color") or "").strip(),
            size=str(vl.get("size") or "").strip(),
            line_id=match.line_id if match else None,
            ns_item_internal_id=match.item_internal_id if match else None,
            ns_line_is_open=match.is_open if match else None,
            current_quantity=match.quantity if match else None,
            proposed_quantity=vl.get("quantity"),
            current_expected_receipt_date=_iso_or_none(match.expected_receipt_date) if match else None,
            current_updated_receipt_date=_iso_or_none(match.updated_receipt_date) if match else None,
            current_override_flag=match.override_expected_receipt if match else False,
            vendor_etd=reference_etd,
            vendor_eta=reference_eta,
            extraction_confidence=confidence,
            extraction_note=note,
            colour_resolution=colour_resolution,
            colour_provenance=colour_provenance,
            # Display context on every change, flagged or not. Nothing branches on
            # it -- see `_line_balance` for why a gate here was cancelled.
            line_balance=_line_balance(match, _as_quantity(vl.get("quantity"))),
            # Populated only when the match was not a clean 1:1, so a reviewer can
            # decide without opening NetSuite. Never the RepSpark field.
            candidate_lines=(
                [_candidate_payload(line) for line in candidates]
                if resolution_problem
                else []
            ),
        )

        reasons: list[str] = []
        if colour_problem:
            # Two colours on this PO that the printed name could equally mean. Flag
            # with both, never pick -- change 5's rule, applied to colour.
            reasons.append(colour_problem)
        if resolution_problem:
            reasons.append(resolution_problem)
        if match is None and candidates and all(line.closed for line in candidates):
            # Nothing writable and the candidates were deliberately closed. Keep
            # the structural guard alive: to_netsuite_fields() must still refuse,
            # not merely be advised against by a status field.
            change.line_closed = True
        if match is not None and match.closed:
            # NetSuite rejects edits to a closed line anyway, but the point is to
            # never even stage one: a closed line means someone deliberately
            # finished with it, and a vendor document referencing it is a
            # discrepancy for a human to explain, not a quantity to overwrite.
            change.line_closed = True
            reasons.append(
                "PO line is closed in NetSuite; vendor data references it but no automatic "
                "change proposed"
            )
        if shipment_needs_manual_entry:
            reasons.append(
                "shipment could not be resolved to style/colour/size lines from an acceptable "
                "source document — manual entry required"
            )
        if match is None and not candidates:
            # Genuinely unexpected: the vendor shipped something this PO has no
            # line for. NOT the same as a PO line missing from the packing list,
            # which produces no record at all — and not the same as lines matching
            # but none being writable, which `resolution_problem` already covers.
            reasons.append(
                f"no NetSuite line on PO {po_number or '(unknown)'} matches "
                f"{change.style_number}/{change.color}/{change.size} "
                f"(normalized size {_normalize_size(change.size)})"
            )
        if confidence in REVIEW_CONFIDENCES:
            reasons.append(f"extraction confidence {confidence}" + (f": {note}" if note else ""))
        if not po_number:
            reasons.append("vendor line has no PO number")
        if change.proposed_quantity is None:
            reasons.append("vendor line has no quantity")

        if reasons:
            # NEEDS_RESOLUTION is the narrow case: several open lines, a human
            # picks one. Everything else that blocks a write stays NEEDS_ATTENTION.
            change.status = (
                STATUS_NEEDS_RESOLUTION if ambiguous_lines else STATUS_NEEDS_ATTENTION
            )
            change.attention_reason = "; ".join(reasons)
        elif change.quantity_changed:
            change.status = STATUS_PENDING_REVIEW
        else:
            # Quantity already correct. The receipt date may still need Paula's
            # input, but that is not a *change* this engine proposes.
            change.status = STATUS_NO_CHANGE

        changes.append(change)

    return changes


def unmatched_netsuite_lines(
    vendor_lines: list[dict], ns_lines: list[POLine]
) -> list[POLine]:
    """
    NetSuite lines this shipment says nothing about.

    Provided for reporting/visibility only — e.g. showing Paula "this PO has 12
    other lines not in this shipment". These deliberately produce NO change
    records and NO attention flags: POs ship in batches, so this is the normal
    case (Paula, 2026-08-11). Never auto-zero them, and never infer cancellation.
    """
    shipped = {
        (
            canonical(vl.get("style_number")),
            canonical(vl.get("color")),
            _size_key(vl.get("size")),
        )
        for vl in vendor_lines
    }
    return [
        line
        for line in ns_lines
        if (
            canonical(line.style_number),
            canonical(line.color),
            _size_key(line.size),
        )
        not in shipped
    ]
