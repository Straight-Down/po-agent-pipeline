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

**Over-shipment is normal.** Shipped exceeding ordered is standard practice and
is NOT flagged, surfaced as an anomaly, or treated differently in any way.

**Quantity ACCUMULATES across shipments (Paula, 2026-09-16).** This supersedes
the replace semantics this engine shipped with. Her words: *"The vendor's packing
slip only shows the new shipment's quantities."* So a second slip against a line
this tool has already written adds to what was written, rather than overwriting
it — replacing a first shipment of 128 with a second of 100 would silently lose
28 units. The first slip against an untouched line still proposes the slip's own
figure, because the base is zero.

**The arithmetic base is this tool's own record, never NetSuite's current
quantity.** That is not an optimisation and it is not an oversight; see
`_accumulated_quantity`, which exists mainly to explain it.

**A PO line absent from a packing list is the normal case, not an event.** POs
routinely ship in batches, so most shipments cover only some of a PO's styles.
Lines with no vendor data produce no change record at all — a silent no-op, not
a flag. Attention flags are reserved for genuinely unexpected mismatches.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Optional, Sequence

from canonical import canonical
from netsuite_client import (
    NS_EXPECTED_RECEIPT_DATE,
    NS_OVERRIDE_EXPECTED_RECEIPT,
    NS_QUANTITY,
    NS_UPDATED_RECEIPT_DATE,
    NetSuiteClient,
    POLine,
    po_number_key,
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

#: SEVERAL extracted lines and SEVERAL NetSuite lines share one key -- a slip that
#: split the shipment across transport modes (`By Sea`, `By UPS`) against a PO that
#: holds a line per mode. Distinct from NEEDS_RESOLUTION: that is picking one line
#: out of several for ONE shipment row, this is PAIRING N shipment rows to N lines.
#: A human assigns; see `_assignment_payload`.
STATUS_NEEDS_ASSIGNMENT = "NEEDS_ASSIGNMENT"

#: Quantities are counts, but they round-trip through `Numeric(12, 3)` and back,
#: so compare with a tolerance below the smallest representable difference rather
#: than with `==` on floats.
QUANTITY_TOLERANCE = 0.001

#: `accumulation["basis"]` -- how the proposed quantity was arrived at. Recorded on
#: every matched line, because "why is this 228 when the slip says 100" must be
#: answerable from the row alone.
BASIS_FIRST_SHIPMENT = "FIRST_SHIPMENT"  # nothing written before; base is zero
BASIS_ACCUMULATED = "ACCUMULATED"  # added to what this tool previously wrote
BASIS_DISPUTED = "DISPUTED"  # NetSuite disagrees with our record; nothing proposed


@dataclass(frozen=True)
class LineHistory:
    """
    What THIS TOOL knows about one NetSuite PO line from its own past runs.

    Built by `ingest` from `proposed_changes` and `write_attempts` and handed in,
    the same way `colour_lookups` is: this module stays free of database I/O, and
    a caller with no history (a demo, a test, the first run against a line) simply
    passes nothing.

    Two different facts, and the difference matters:

    - `written_quantity` -- the cumulative total this tool last successfully WROTE
      to the line. None when it has never written one. This is the accumulation
      base.
    - `observed_quantity` -- what NetSuite's `quantity` read the last time this
      tool looked at the line, whether or not it went on to write anything. This
      is not a base; it is the yardstick for "has someone edited this line since
      we last saw it".

    A line proposed but never written contributes an `observed_quantity` and no
    `written_quantity`. That is the point: a rejected or still-pending proposal
    must not move the arithmetic, but it is still evidence of what the line looked
    like at a known moment.
    """

    po_number: str
    line_id: str

    #: The cumulative total last written, and where that claim comes from. The
    #: provenance is carried so a disputed line can tell Paula not just the number
    #: but which approval produced it.
    written_quantity: Optional[float] = None
    written_at: Optional[str] = None
    written_change_id: Optional[str] = None
    write_count: int = 0

    #: NetSuite's quantity as of this tool's most recent look at the line.
    observed_quantity: Optional[float] = None
    observed_at: Optional[str] = None

    @property
    def has_written(self) -> bool:
        return self.written_quantity is not None


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

    #: The CANONICAL key ("1624"), not the vendor's rendering -- one PO written two
    #: ways on one document is one PO here. The verbatim text is kept on
    #: `shipment_pos.po_number_printed`.
    po_number: str
    style_number: str
    color: str
    size: str
    line_id: Optional[str]

    #: Quantity. ACCUMULATE semantics since Paula's ruling of 2026-09-16:
    #: `proposed_quantity` is what the LINE should hold in total, which on a second
    #: shipment is the previously-written total plus this slip -- not the slip's own
    #: figure. `accumulation` below carries the breakdown. None here means the tool
    #: refused to compute one, which is not the same as the slip being silent about
    #: quantity; `accumulation["basis"]` says which.
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

    #: How `proposed_quantity` was arrived at -- see `_accumulated_quantity`.
    #: Carries `basis` (FIRST_SHIPMENT / ACCUMULATED / DISPUTED), the base it added
    #: to, this slip's own figure, and on a DISPUTED line both disagreeing numbers.
    #:
    #: Separate from `line_balance` on purpose. Those five figures are a fixed,
    #: documented set that the review screen and `v_review_lines` are built around;
    #: this is a different question (where did the arithmetic start) and folding it
    #: in would quietly redefine a contract other things depend on.
    accumulation: dict = field(default_factory=dict)

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

    #: How the SIZE was arrived at, when it was composed from two axes rather than
    #: printed in one place: method (COMPOSED / COMPOSITION_REJECTED), the printed
    #: label of each axis verbatim, and the composed result.
    #:
    #: Same reasoning as `colour_provenance`, and the same reason it is persisted:
    #: "why did this line become 32-34" is answerable only from the two cells it
    #: came from, and those are a waist column header and an inseam block label
    #: several rows apart. Nothing downstream can reconstruct that pairing from
    #: the size alone. Empty dict on the single-axis documents, which is most of
    #: them.
    size_composition: dict = field(default_factory=dict)

    #: The transport-mode recap row this line came off, verbatim (`By Sea`,
    #: `By UPS`). Empty on the single-recap documents, which is four of the five
    #: corpus vendors. Part of the extraction-side key -- see
    #: `extraction_schema.aggregate_lines` on why keying on it is legitimate
    #: where keying on a NetSuite field would not be.
    recap_label: str = ""

    #: Populated only for STATUS_NEEDS_ASSIGNMENT: every candidate PO line and
    #: every sibling extracted line, so a human can pair them without opening
    #: NetSuite. See `_assignment_payload`.
    assignment: dict = field(default_factory=dict)

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


def _assignment_payload(candidates: list[POLine], siblings: list[dict]) -> dict:
    """
    Everything a human needs to PAIR N shipment rows with N NetSuite lines.

    Not a selection and not a suggestion. The payload lists both sides and stops;
    the pairing is Paula's, permanently.

    **There is no field that says which PO line is the sea line and which is the
    air line, and this is measured rather than assumed** (73 duplicate-key groups
    across six POs, 2026-09-09):

      - No per-line transport-mode column exists at all -- 49 line fields, none
        of them mode, carrier, incoterm, freight or vessel.
      - `rate` and `leadTime` are **identical on both lines in all 73 groups**.
        If mode were modelled per line, those two are precisely what would
        differ, so their agreement is the strongest available evidence that it
        is not.
      - The header's `shipMethod` is per-PO, empty on 3 of 6 surveyed, and reads
        `BOAT` on the very PO that carries a UPS portion.
      - Quantity equality only appeared to work on PO0001624 because the
        receipts were already posted there, so both lines already reflected the
        shipment and one side happened to match exactly. On a PO awaiting the
        update -- the only kind this tool acts on -- both lines carry ordered
        quantities and match neither recap row.

    **NEVER key the assignment on `custcol_override_expected_receipt` or
    `custcol_sd_updatedreceiptdate`.** They differ within 31 of 73 groups, so they
    look like signal. They are an echo: **this tool writes both fields**, so
    pairing on them would let the tool's own past writes decide its future
    pairings, and the correlation would strengthen with every run regardless of
    whether it was ever right. That is the most dangerous candidate here because
    it is the most convincing-looking one.

    `custcol_sd_fg_excluderepspark` differs in 51 of 73 groups -- the most of any
    custom column -- and is likewise never used: it is a RepSpark feed flag Paula
    maintains by hand, and this tool does not read, write or display it.
    """
    return {
        "candidate_lines": [_candidate_payload(line) for line in candidates],
        "extracted_lines": [
            {
                "recap_label": str(s.get("recap_label") or ""),
                "quantity": _as_quantity(s.get("quantity")),
                "source_hint": str(s.get("source_hint") or ""),
                "confidence": str(s.get("confidence") or ""),
            }
            for s in siblings
        ],
        "candidate_count": len(candidates),
        "extracted_count": len(siblings),
        # Stated so the review screen does not have to infer it, and so nobody
        # later mistakes an even count for permission to pair by position.
        "auto_assignable": False,
    }


def _sibling_key(vl: dict) -> tuple:
    """
    The key a shipment row shares with its OTHER transport-mode rows.

    Deliberately EXCLUDES the recap label: this groups `By Sea 52` with
    `By UPS 5` for one size, which is what makes them visible to each other as an
    assignment case. The label is in the key everywhere else (aggregation, the
    database) precisely so they stay separate rows.
    """
    return (
        po_number_key(vl.get("po_number")),
        canonical(vl.get("style_number")),
        canonical(vl.get("color")),
        _size_key(vl.get("size")),
    )


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


def _accumulated_quantity(
    line: Optional[POLine],
    slip_quantity: Optional[float],
    history: Optional[LineHistory],
) -> tuple[Optional[float], dict, Optional[str]]:
    """
    What the line's quantity should become, given that slips accumulate.

    Returns `(proposed_quantity, accumulation, problem)`. A `problem` means the
    tool refused to compute anything: `proposed_quantity` comes back None and the
    caller flags the line for Paula.

    ## Why the base is not NetSuite's current quantity

    This will look like an omission to anyone reading it fresh, because the
    obvious implementation is one line — `line.quantity + slip` — and it is
    wrong for a reason that is invisible from the call site.

    **`quantity` is a field this tool writes.** It is one of the four in
    `WRITABLE_LINE_FIELDS`. Reading it back as the base for the next write means
    the tool's own past output becomes the input to its next decision, and the
    arithmetic drifts in whatever direction its mistakes already pointed, with
    nothing to arrest it — every run makes the next run more confident in the same
    error. RUNBOOK section 8 lesson 13 states the general test: *would this field
    have this value if the tool had never run?* For `quantity` on a line this tool
    has written, the answer is no. It is not evidence about the world; it is a
    record of what the tool already did.

    That is not a hypothetical worry here. The failure it produces is silent and
    compounding: one missed write, or one manual correction by Paula that the tool
    reads as its own, and every later shipment on that line is off by the same
    amount, forever, with each run confirming the previous one.

    So the base is `history.written_quantity` -- what the tool's own audit trail
    says it last successfully wrote, from `proposed_changes` joined to a
    successful `write_attempts` row. That record exists independently of NetSuite
    and cannot be contaminated by what NetSuite currently holds.

    ## What NetSuite's value IS used for

    A consistency check, which is the honest use of it. If our record says we
    wrote 128 and the line now reads 128, the two agree and the accumulation is
    safe. If the line reads anything else, someone changed it outside this tool
    and **the tool does not guess and does not reconcile** -- it reports both
    numbers and stops. Reconciling would mean picking one source as authoritative,
    which is exactly the judgement that belongs to Paula.

    ## The three ways a line can have no written history

    - **Never seen before, nothing received.** An ordinary first shipment. The
      base is zero and the slip's figure is the proposal, which is what this
      engine did for every line before accumulation existed.
    - **Never seen before, but goods have already been received against it.**
      `quantity_received > 0` with no record of our own means a shipment arrived
      that this tool knows nothing about. Accumulating from zero would propose
      only this slip and lose the earlier units -- the precise loss Paula's ruling
      is about -- so it is flagged instead.
    - **Seen before but never written** (proposed and rejected, or still pending).
      The base is still zero, because an unwritten proposal changed nothing. But
      the observation is kept: if NetSuite's quantity has moved since we looked,
      someone edited the line, and that is flagged on the same footing.

    **Residual gap, named rather than hidden:** a line this tool has never seen,
    with nothing received, whose quantity was edited by hand, is indistinguishable
    from an untouched line. NetSuite carries no separate "originally ordered"
    figure to compare against -- `quantity` is both the ordered value and the
    field we overwrite -- so there is nothing to detect it with. The first
    shipment this tool processes on such a line will propose the slip's figure.
    Every line it has seen once is covered from then on.
    """
    if line is None or slip_quantity is None:
        # Nothing to accumulate against, or nothing to add. Both are already
        # flagged by the caller for their own reasons; the slip's figure travels
        # through unchanged so the reviewer still sees what the document said.
        return slip_quantity, {}, None

    ns_quantity = float(line.quantity)
    slip = float(slip_quantity)
    payload: dict = {
        "slip_quantity": slip,
        "netsuite_quantity": ns_quantity,
    }

    if history is not None and history.has_written:
        expected = float(history.written_quantity)
        payload.update({
            "base_quantity": expected,
            "base_written_at": history.written_at,
            "base_change_id": history.written_change_id,
            "prior_writes": history.write_count,
        })
        if abs(ns_quantity - expected) > QUANTITY_TOLERANCE:
            payload["basis"] = BASIS_DISPUTED
            return None, payload, (
                f"NetSuite line {line.line_id} holds {ns_quantity:g}, but this tool's own record "
                f"says it last wrote {expected:g} (change {history.written_change_id}, "
                f"{history.written_at}). Someone changed the line outside this tool, so the "
                f"shipped {slip:g} on this slip cannot be added to a base the tool cannot "
                "vouch for. No quantity proposed — confirm which figure is right and what "
                "this line should total"
            )
        payload["basis"] = BASIS_ACCUMULATED
        return expected + slip, payload, None

    if history is not None and history.observed_quantity is not None:
        observed = float(history.observed_quantity)
        payload.update({"base_quantity": 0.0, "observed_quantity": observed,
                        "observed_at": history.observed_at, "prior_writes": 0})
        if abs(ns_quantity - observed) > QUANTITY_TOLERANCE:
            payload["basis"] = BASIS_DISPUTED
            return None, payload, (
                f"NetSuite line {line.line_id} holds {ns_quantity:g}, but read {observed:g} when "
                f"this tool last looked at it ({history.observed_at}) and it has never written to "
                "it. Someone changed the line outside this tool. No quantity proposed — the "
                f"shipped {slip:g} on this slip cannot be added to a base the tool cannot vouch for"
            )
        payload["basis"] = BASIS_FIRST_SHIPMENT
        return slip, payload, None

    received = float(line.quantity_received or 0.0)
    payload.update({"base_quantity": 0.0, "prior_writes": 0,
                    "quantity_received": received})
    if received > QUANTITY_TOLERANCE:
        payload["basis"] = BASIS_DISPUTED
        return None, payload, (
            f"NetSuite line {line.line_id} already shows {received:g} received, but this tool has "
            "no record of ever proposing or writing to it — a shipment reached this line that it "
            f"knows nothing about. Proposing the shipped {slip:g} alone would drop the earlier "
            "units. No quantity proposed — confirm what this line should total"
        )
    payload["basis"] = BASIS_FIRST_SHIPMENT
    return slip, payload, None


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
    line_history: Optional[dict] = None,
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

    `line_history` maps `(po_number, line_id)` to a `LineHistory` -- what this tool
    itself previously wrote to that NetSuite line, and when it last looked at it.
    It is what makes a second shipment ADD rather than replace (Paula, 2026-09-16).
    Passed in for the same reason `colour_lookups` is: the history lives in the
    database and this module does no I/O. **Omitting it does not silently fall back
    to replace semantics** -- every line then reads as a first shipment, which is
    the correct answer when there is genuinely no history and the honest one when
    the caller simply did not look. `ingest` always supplies it.
    """
    eta_date = _parse_eta_to_date(eta)
    etd_date = _parse_eta_to_date(etd)
    reference_eta = eta_date.isoformat() if eta_date else (str(eta).strip() if eta else None)
    reference_etd = etd_date.isoformat() if etd_date else (str(etd).strip() if etd else None)

    # Grouped by CANONICAL key, so a document whose extraction rendered the same PO
    # as both `1624` and `PO0001624` reads that PO once and shares one colour
    # vocabulary for it -- and lines up with the key `ingest` stores. See
    # netsuite_client.po_number_key.
    po_numbers = sorted({po_number_key(vl.get("po_number")) for vl in vendor_lines
                         if str(vl.get("po_number") or "").strip()})
    ns_lines_by_po = {po: client.get_purchase_order(po) for po in po_numbers if po}

    # Shipment rows that share a key with each other -- i.e. one size split across
    # several transport-mode recap rows. Built once, keyed WITHOUT the recap label
    # so the siblings can see each other; see `_sibling_key`.
    siblings_by_key: dict[tuple, list[dict]] = {}
    for vl in vendor_lines:
        siblings_by_key.setdefault(_sibling_key(vl), []).append(vl)

    changes: list[ProposedChange] = []
    for vl in vendor_lines:
        po_number = po_number_key(vl.get("po_number"))
        confidence = str(vl.get("confidence") or "high").lower()
        note = str(vl.get("note") or "")
        ns_lines = ns_lines_by_po.get(po_number, [])
        # ALL matching lines, not just the first — the key is not unique per line.
        candidates, colour_resolution, colour_problem, colour_provenance = (
            _find_matching_lines(vl, ns_lines, (colour_lookups or {}).get(po_number))
        )
        siblings = siblings_by_key.get(_sibling_key(vl), [vl])

        # An ASSIGNMENT case: several shipment rows AND several PO lines share one
        # key. Never resolved automatically -- not even when the counts match and
        # exactly one pairing is arithmetically possible, because "arithmetically
        # possible" is not evidence about which line is which mode. See
        # `_assignment_payload` for what was measured and rejected.
        assignment_problem = None
        assignment: dict = {}
        if len(siblings) > 1 and len(candidates) > 1:
            match, resolution_problem, ambiguous_lines = None, None, []
            assignment = _assignment_payload(candidates, siblings)
            labels = ", ".join(
                repr(str(x.get("recap_label") or "(unlabelled)")) for x in siblings
            )
            assignment_problem = (
                f"this slip splits the shipment across {len(siblings)} transport-mode "
                f"row(s) ({labels}) and PO {po_number} holds {len(candidates)} line(s) "
                f"for this style/colour/size. Assign each row to a line -- the tool "
                f"does not pair them, because NetSuite carries no field distinguishing "
                f"a sea line from an air line"
            )
        elif len(siblings) > 1 and len(candidates) == 1:
            # Two shipment rows, one PO line. Nothing to assign, and writing both
            # to that line would double-write it -- which the database now
            # refuses outright (ux_proposed_changes_one_line_per_shipment).
            match, resolution_problem, ambiguous_lines = None, None, []
            assignment = _assignment_payload(candidates, siblings)
            assignment_problem = (
                f"this slip splits the shipment across {len(siblings)} transport-mode "
                f"row(s) but PO {po_number} holds only ONE line for this "
                f"style/colour/size. Both rows cannot be written to one line; a human "
                f"decides what this means"
            )
        else:
            match, resolution_problem, ambiguous_lines = _resolve_target_line(candidates)

        # ACCUMULATE rather than replace (Paula, 2026-09-16). The base is what this
        # tool's own audit trail says it last wrote to the line -- NOT NetSuite's
        # current quantity, which is a field this tool writes and therefore an echo
        # of itself. `_accumulated_quantity` carries the full reasoning, and is
        # also what turns a disagreement between the two into a flag.
        slip_quantity = _as_quantity(vl.get("quantity"))
        history = (line_history or {}).get((po_number, match.line_id)) if match else None
        proposed_quantity, accumulation, accumulation_problem = _accumulated_quantity(
            match, slip_quantity, history
        )
        if not accumulation:
            # No accumulation applied: unmatched line, or a quantity that is not a
            # number. Carry the vendor's value through exactly as before, so a
            # non-numeric quantity still reaches the reviewer as it was printed.
            proposed_quantity = vl.get("quantity")

        change = ProposedChange(
            po_number=po_number,
            style_number=str(vl.get("style_number") or "").strip(),
            color=str(vl.get("color") or "").strip(),
            size=str(vl.get("size") or "").strip(),
            line_id=match.line_id if match else None,
            ns_item_internal_id=match.item_internal_id if match else None,
            ns_line_is_open=match.is_open if match else None,
            current_quantity=match.quantity if match else None,
            proposed_quantity=proposed_quantity,
            accumulation=accumulation,
            current_expected_receipt_date=_iso_or_none(match.expected_receipt_date) if match else None,
            current_updated_receipt_date=_iso_or_none(match.updated_receipt_date) if match else None,
            current_override_flag=match.override_expected_receipt if match else False,
            vendor_etd=reference_etd,
            vendor_eta=reference_eta,
            extraction_confidence=confidence,
            extraction_note=note,
            colour_resolution=colour_resolution,
            colour_provenance=colour_provenance,
            # Carried straight through from the extraction row. The matcher does
            # not compose sizes and does not second-guess one -- the composition
            # was already validated against the account's size list by
            # `extraction_schema.enforce_size_composition`.
            size_composition=dict(vl.get("size_composition") or {}),
            recap_label=str(vl.get("recap_label") or "").strip(),
            assignment=assignment,
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
        if assignment_problem:
            reasons.append(assignment_problem)
        if accumulation_problem:
            # NetSuite's line disagrees with what this tool believes it wrote, or
            # goods reached a line it has no record of. Both numbers go to Paula
            # verbatim: the tool does not pick one and does not reconcile them.
            reasons.append(accumulation_problem)
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
        if change.proposed_quantity is None and not accumulation_problem:
            # Guarded: a refused accumulation also leaves proposed_quantity None,
            # and reporting that as "the vendor line has no quantity" would blame
            # the document for the tool's own refusal. The slip's figure is right
            # there in `accumulation["slip_quantity"]`.
            reasons.append("vendor line has no quantity")

        if reasons:
            # NEEDS_RESOLUTION is the narrow case: several open lines, a human
            # picks one. Everything else that blocks a write stays NEEDS_ATTENTION.
            if assignment:
                # Pairing N rows to N lines. Checked before NEEDS_RESOLUTION,
                # which is the different question of picking one of several for a
                # single row.
                change.status = STATUS_NEEDS_ASSIGNMENT
            elif ambiguous_lines:
                change.status = STATUS_NEEDS_RESOLUTION
            else:
                change.status = STATUS_NEEDS_ATTENTION
            change.attention_reason = "; ".join(reasons)
        elif change.quantity_changed:
            change.status = STATUS_PENDING_REVIEW
        else:
            # Quantity already correct. The receipt date may still need Paula's
            # input, but that is not a *change* this engine proposes.
            change.status = STATUS_NO_CHANGE

        changes.append(change)

    return changes


def source_sheet_summary(
    changes: Sequence[ProposedChange], vendor_lines: Sequence[dict]
) -> list[dict]:
    """
    Per source sheet: which styles it carried, and how many of its lines matched.

    `changes` and `vendor_lines` are parallel -- `build_proposed_changes` emits
    exactly one change per vendor line, in order -- which is the same pairing
    `ingest` already relies on.

    The sheet label comes from `source_hint` (`ACT!R47` -> `ACT`). A line whose
    hint names several rows of one sheet still resolves to that sheet; a line with
    no usable hint is grouped under `''` rather than dropped.
    """
    if len(changes) != len(vendor_lines):
        raise ValueError(
            f"source_sheet_summary needs parallel inputs: {len(changes)} changes vs "
            f"{len(vendor_lines)} vendor lines"
        )

    by_sheet: dict[str, dict] = {}
    for change, line in zip(changes, vendor_lines):
        hint = str(line.get("source_hint") or "")
        sheets = sorted({part.split("!", 1)[0].strip() for part in hint.split(",") if "!" in part})
        label = sheets[0] if len(sheets) == 1 else (", ".join(sheets) if sheets else "")
        entry = by_sheet.setdefault(
            label, {"sheet": label, "styles": set(), "pos": set(), "lines": 0, "matched": 0}
        )
        entry["lines"] += 1
        if change.style_number:
            entry["styles"].add(change.style_number)
        if change.po_number:
            entry["pos"].add(change.po_number)
        if change.line_id:
            entry["matched"] += 1

    return [
        {
            "sheet": e["sheet"],
            "styles": sorted(e["styles"]),
            "po_numbers": sorted(e["pos"]),
            "lines": e["lines"],
            "matched": e["matched"],
        }
        for e in sorted(by_sheet.values(), key=lambda e: e["sheet"])
    ]


def describe_sheet_selection(summary: Sequence[dict]) -> str:
    """
    One sentence recording WHICH sheet supplied the matched lines, and why.

    Exists because the right answer was being reached by accident. Tainan's
    workbook holds two sheets describing the same shipment: `ACT` (style `50144`,
    the packing record) and `REV` (style `50144-2`, an 8%-target plan -- see
    RUNBOOK section 6 item 20). `ACT`'s lines matched PO 1725 and `REV`'s did not,
    which is the correct outcome, but only because the plan happened to carry a
    style code the PO does not have. Nothing recorded the choice, so nothing
    would have noticed if that coincidence stopped holding.

    Same provenance principle as `colour_resolution` and `size_composition`:
    write down the decision and its reason at the moment it is made, rather than
    leaving it implicit in which rows survived. Empty string when there is nothing
    to explain -- one source sheet, or several that all behaved alike.
    """
    if len(summary) < 2:
        return ""
    matched = [e for e in summary if e["matched"]]
    unmatched = [e for e in summary if not e["matched"]]
    if not matched or not unmatched:
        return ""

    styles = {e["sheet"]: "/".join(e["styles"]) or "(no style)" for e in summary}
    pos = sorted({p for e in matched for p in e["po_numbers"]})
    differing = len({tuple(e["styles"]) for e in summary}) > 1
    return (
        f"this document contained {len(summary)} sheets with "
        + ("different style codes" if differing else "the same style code")
        + "; "
        + ", ".join(
            f"{e['sheet']} ({styles[e['sheet']]}) matched PO {'/'.join(pos)}"
            f" on {e['matched']} of {e['lines']} line(s)"
            for e in matched
        )
        + "; "
        + ", ".join(f"{e['sheet']} ({styles[e['sheet']]}) did not" for e in unmatched)
        + ". The matched sheet supplied the shipment quantities."
    )


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
