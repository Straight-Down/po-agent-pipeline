"""
Everything Paula reads, in one place. The rule for every message: it answers
"do I need to do something, and if so what". Internal detail -- row references,
state names, percentages, sheet names, the pipeline's own reason text -- goes in
a collapsed Details toggle, never in the main text.

Messages are GENERATED from stored data (the matcher's reasons, the
SHIPMENT_ROWS_LOST audit event), not hardcoded per PO. A reason this module does
not recognise still produces a line ("needs a look") and its raw text in
Details: an unrecognised reason is never dropped.
"""

from __future__ import annotations

from typing import Optional

#: (category, phrase the matcher writes, what Paula reads). Matched in order on
#: the matcher's `attention_reason`; one sentence per category found.
REASON_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("pre_existing", "units received before the tool started tracking it",
     "This line was already received in NetSuite before this packing slip arrived, so "
     "the tool hasn't proposed a number for it."),
    ("disputed", "Someone changed the line outside this tool",
     "The quantity in NetSuite has been changed since the tool last saw this line, so "
     "it hasn't proposed a number. Check the line in NetSuite."),
    ("closed", "PO line is closed in NetSuite",
     "This line is closed in NetSuite, so it can't be updated."),
    ("none_open", "but none is open",
     "The matching line in NetSuite is no longer open, so it can't be updated."),
    ("not_on_po", "no NetSuite line on PO",
     "This item isn't on the PO in NetSuite, so there is nothing to update for it."),
    ("several_open", "open NetSuite lines match",
     "This item matches more than one open line on the PO."),
    ("transport", "transport-mode",
     "The packing slip splits this item across more than one delivery."),
    ("confidence", "extraction confidence",
     "The tool had to work out part of this line itself. Check it against the packing "
     "slip before approving."),
    ("cartons", "carton backing unverifiable",
     "The tool couldn't confirm this quantity against carton counts. Check it against "
     "the packing slip."),
    # The matcher's ambiguous-colour problem. NOT the bare word "colour": the
    # none-open reason says "style/colour/size" and would match it.
    ("colour", "printed colour",
     "The colour on the packing slip could be more than one colour on the PO. Check "
     "which one it is."),
    ("no_po", "vendor line has no PO number",
     "The packing slip doesn't say which PO this line belongs to."),
    ("no_quantity", "vendor line has no quantity",
     "The packing slip gives no quantity for this line."),
    ("manual", "manual entry required",
     "The tool couldn't read sizes from this shipment's documents. Enter it in NetSuite "
     "by hand."),
)

UNRECOGNISED = "This line needs a look. What the tool noted is under Details."


def reasons(attention_reason: Optional[str]) -> list[str]:
    """Plain sentences for a line's reasons; empty when there are none."""
    text = attention_reason or ""
    if not text.strip():
        return []
    found = [plain for _key, phrase, plain in REASON_CATEGORIES if phrase.lower() in text.lower()]
    return found or [UNRECOGNISED]


# -- why an Approve is unavailable --------------------------------------------

#: Grouped explanations for a disabled Approve, by what blocks it.
BLOCK_MESSAGES = {
    "pre_existing": (
        "These lines were already received in NetSuite before this packing slip arrived. "
        "The tool can't tell whether that receipt already covers this shipment, so it "
        "hasn't proposed a number. There's nothing to approve here yet."),
    "disputed": (
        "The quantity in NetSuite has been changed since the tool last saw these lines, so "
        "it hasn't proposed a number. Check them in NetSuite. There's nothing to approve "
        "here yet."),
    "not_updatable": (
        "These lines don't match an open line on this PO in NetSuite, so there is nothing "
        "the tool can update. If NetSuite needs changing, do it there; reject this PO here "
        "to clear it from your list."),
    "manual": (
        "The tool couldn't read sizes from this shipment's documents. Enter it in NetSuite "
        "by hand, then reject this PO here to clear it from your list."),
    "no_quantity": (
        "The packing slip gives no quantity for these lines, so there is nothing to "
        "approve. Reject this PO here if nothing further is expected."),
}


def block_kind(line) -> str:
    """Which BLOCK_MESSAGES entry a blocking line belongs to."""
    row = line.row
    if row["state"] == "MANUAL_ENTRY_REQUIRED":
        return "manual"
    if row["ns_line_id"] is None or not row["ns_line_is_open"]:
        return "not_updatable"
    basis = row["accumulation_basis"]
    if basis == "PRE_EXISTING_RECEIPT":
        return "pre_existing"
    if basis == "DISPUTED":
        return "disputed"
    return "no_quantity"


def block_explanations(blocking: list) -> list[tuple[str, int]]:
    """[(message, how many lines)] for a disabled Approve, in a fixed order."""
    counts: dict[str, int] = {}
    for line in blocking:
        kind = block_kind(line)
        counts[kind] = counts.get(kind, 0) + 1
    return [(BLOCK_MESSAGES[k], counts[k]) for k in BLOCK_MESSAGES if k in counts]


# -- the escalation bubble ------------------------------------------------------


def lines_word(n: int) -> str:
    return f"{n} line" + ("" if n == 1 else "s")


def rows_lost_message(detail: Optional[dict]) -> Optional[str]:
    """
    The escalation, as information. Generated from the SHIPMENT_ROWS_LOST event.
    Not an error, and not styled as one: nothing is needed unless it looks wrong.
    """
    if not detail:
        return None
    extracted = int(detail.get("lines_extracted") or 0)
    removed = detail.get("removed") or []
    not_backed = sum(1 for r in removed if "NOT SHIPMENT DATA" in str(r.get("reason")))
    no_po = sum(1 for r in removed if r.get("reason") == "no PO number")
    other = len(removed) - not_backed - no_po
    parts = []
    if not_backed:
        one = not_backed == 1
        parts.append(
            f"{not_backed} of the {extracted} lines on this packing slip "
            f"{'was' if one else 'were'} not counted as shipped, because no cartons in the "
            f"document cover {'it' if one else 'them'}. {'It' if one else 'They'} may belong "
            "to a separate shipment.")
    if no_po:
        parts.append(f"{lines_word(no_po)} didn't say which PO {'it' if no_po == 1 else 'they'} "
                     "belonged to, so " + ("it wasn't" if no_po == 1 else "they weren't")
                     + " included.")
    if other:
        parts.append(f"{lines_word(other)} {'was' if other == 1 else 'were'} left out for "
                     "another reason (see Details).")
    if not parts:
        return None
    return " ".join(parts) + " Nothing is needed from you unless you think that's wrong."


# -- the summary ------------------------------------------------------------------


def summary_sentence(summary) -> str:
    """Ordered versus shipped, in words. Figures that grade it go in Details."""
    shipped = _n(summary.shipped_total)
    if not summary.matched_lines:
        return f"This packing slip ships {shipped} units. None of its lines matched the PO in NetSuite."
    ordered, on_matched = _n(summary.ordered_on_matched), _n(summary.shipped_on_matched)
    if summary.over and not summary.under:
        how = "more than ordered on every line that differs"
    elif summary.under and not summary.over:
        how = "less than ordered on every line that differs"
    elif not summary.over and not summary.under:
        how = "exactly the ordered quantity on every line"
    else:
        how = "more than ordered on some lines and less on others"
    return (f"This packing slip ships {on_matched} units against {ordered} ordered on these "
            f"lines: {how}.")


def _n(value) -> str:
    text = format(value, "f")
    text = text.rstrip("0").rstrip(".") if "." in text else text
    try:
        return f"{int(text):,}"
    except ValueError:
        return text
