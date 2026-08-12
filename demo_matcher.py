"""
Demo/test harness for matcher.py, using the real Inprotex sample files plus a
mocked NetSuite "current state" built from the actual PO 1662 / M120246 / TID
line values Paula showed us at the start of this project (Expected Receipt Date
7/6/2026, Override: Yes, S=12, M=71).

This proves the diff engine surfaces exactly the kind of mismatch this project
exists to catch: NetSuite currently says S=12/M=71, but the real shipment (per
the packing slip) is S=9/M=50.

It also demonstrates Paula's rulings of 2026-08-11 in action:
  - The engine proposes a new QUANTITY and nothing else. It does not propose a
    receipt date; the vendor's ETA travels through as labelled reference only.
  - Asking for a date write before a human confirms one raises DateNotConfirmed.
  - NetSuite lines absent from this packing slip produce no records at all.

Run: python demo_matcher.py
"""

from datetime import date
from pathlib import Path

from matcher import (
    STATUS_NEEDS_ATTENTION,
    STATUS_NO_CHANGE,
    STATUS_PENDING_REVIEW,
    DateNotConfirmed,
    build_proposed_changes,
    unmatched_netsuite_lines,
)
from netsuite_client import NetSuiteClient, POLine
from parse_packing_slip import parse_packing_sheet, parse_shipping_advice_pdf

HERE = Path(__file__).resolve().parent
XLSX = HERE / "0626建躍空運成衣 (SD-219國外)Invoice_Packing.xlsx"
PDF = HERE / "Shipping Advice 6128990769 建躍.pdf"

for path in (XLSX, PDF):
    if not path.exists():
        raise SystemExit(f"Sample file missing: {path.name}")

vendor_lines = parse_packing_sheet(str(XLSX))
ship_info = parse_shipping_advice_pdf(str(PDF))

# Mocked "current NetSuite state" -- only PO 1662 / M120246 has real known values
# (from Paula's screenshot); every other PO/style is intentionally left out to
# demonstrate the NEEDS_ATTENTION path for lines with no NetSuite counterpart.
mock_ns_state = {
    "1662": [
        POLine(
            line_id="101",
            item="M120246 : M120246-Waterman Polo-TID-S",
            style_number="M120246",
            vendor_name=None,
            color="TID",
            size="S",
            quantity=12,
            units="Ea",
            expected_receipt_date=date(2026, 7, 6),
            override_expected_receipt=True,
            updated_receipt_date=date(2026, 7, 6),
        ),
        POLine(
            line_id="102",
            item="M120246 : M120246-Waterman Polo-TID-M",
            style_number="M120246",
            vendor_name=None,
            color="TID",
            size="M",
            quantity=71,
            units="Ea",
            expected_receipt_date=date(2026, 7, 6),
            override_expected_receipt=True,
            updated_receipt_date=date(2026, 7, 6),
        ),
        # A colour this shipment says nothing about -- normal for a batched PO,
        # and the case Paula ruled must be a silent no-op rather than a flag.
        POLine(
            line_id="103",
            item="M120246 : M120246-Waterman Polo-NVY-M",
            style_number="M120246",
            vendor_name=None,
            color="NVY",
            size="M",
            quantity=40,
            units="Ea",
            expected_receipt_date=date(2026, 7, 6),
            override_expected_receipt=True,
            updated_receipt_date=date(2026, 7, 6),
        ),
    ]
}

client = NetSuiteClient(mock_data=mock_ns_state)
changes = build_proposed_changes(
    vendor_lines, client, eta=ship_info.get("eta"), etd=ship_info.get("etd")
)

pending = [c for c in changes if c.status == STATUS_PENDING_REVIEW]
needs_attention = [c for c in changes if c.status == STATUS_NEEDS_ATTENTION]
no_change = [c for c in changes if c.status == STATUS_NO_CHANGE]

print(f"Total vendor lines parsed: {len(changes)}")
print(f"  PENDING_REVIEW (real, matched change):  {len(pending)}")
print(f"  NEEDS_ATTENTION (no NetSuite match yet): {len(needs_attention)}")
print(f"  NO_CHANGE:                               {len(no_change)}")
print()
print("--- Matched changes (this is the real signal) ---")
for c in pending:
    print(
        f"PO {c.po_number} {c.style_number} {c.color}-{c.size}: "
        f"qty {c.current_quantity} -> {c.proposed_quantity}  "
        f"[quantity only; no date proposed]"
    )
    print(f"    NetSuite currently expects receipt: {c.current_expected_receipt_date}")
    print(f"    {c.reference_dates_label}")

print()
print("--- Receipt dates are Paula's to set (ruling 2026-08-11) ---")
if pending:
    sample = pending[0]
    print(f"  quantity-only write : {sample.to_netsuite_fields(include_dates=False)}")
    try:
        sample.to_netsuite_fields(include_dates=True)
    except DateNotConfirmed as exc:
        print(f"  date write refused  : {type(exc).__name__}")
        print(f"                        {str(exc).splitlines()[0][:96]}")
    sample.confirm_receipt_date("2026-07-20")
    print(f"  after Paula confirms: {sample.to_netsuite_fields(include_dates=True)}")

print()
print("--- PO lines not in this shipment (normal: POs ship in batches) ---")
leftover = unmatched_netsuite_lines(vendor_lines, client.get_purchase_order("1662"))
for line in leftover:
    print(f"  PO 1662 {line.style_number} {line.color}-{line.size} qty={line.quantity} — no change proposed")
print(f"  ({len(leftover)} line(s) left alone, producing no records and no flags)")
