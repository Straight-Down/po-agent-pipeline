"""
How many OPEN PO lines already carry a receipt this tool has no record of?

That number is the ceiling on the `PRE_EXISTING_RECEIPT` wave: each such line
asks for a one-time confirmation the first time a packing slip touches it,
because the units already received were put there by something other than this
tool and accumulating from a zero base would drop them. It retires itself -- the
confirmation becomes history, so the next slip on that line is an ordinary
`ACCUMULATED`.

Read-only throughout: three SuiteQL SELECTs and a bounded set of PO reads.

    .venv\\Scripts\\python scripts\\estimate_pre_existing_receipts.py

## Why this is two stages rather than one SuiteQL count

**Because the obvious one-query answer is wrong by a factor of ~56, and it looks
entirely reasonable.** `transactionLine` in SuiteQL exposes `isclosed` but no
`isopen`, so the natural filter is `isclosed = 'F'` -- and on this account that
returns **23,981** lines carrying receipts. The measured figure is **426**
(sandbox, 2026-09-16).

`isClosed` is NOT the complement of `isOpen`, which this project already knew and
recorded (RUNBOOK section 6): on a Fully Billed PO every line has `isClosed = F`
*and* `isOpen = F`, because nobody ticks the per-line Closed box on a PO that
simply finished. So `isclosed = 'F'` counts every settled line ever received
against. Those lines are irrelevant here for a reason worth stating: the matcher
targets only open lines (`_resolve_target_line` filters on `is_open`), so a
settled line never reaches `_accumulated_quantity` at all and cannot produce this
flag however many units it holds.

So: **SuiteQL narrows, REST confirms.** Stage 1 finds candidate POs cheaply.
Stage 2 reads each candidate through the same `POLine.is_open` the matcher
branches on, so the count is measured against the field that actually decides,
not against a proxy for it.

## What the sample would have said

A first attempt sampled the 25 most recent POs and found **0** open lines with a
receipt -- true, and useless. Selecting the most recent POs selects for POs that
have not been received against yet, the same sampling trap as the duplicate-key
survey (RUNBOOK section 8). A second attempt narrowed to lines that are not fully
received (`quantity > quantityshiprecv`), which found 6 POs and 41 lines -- also
an undercount, because a line can be fully received and still open while its PO
waits on billing. The net below excludes settled POs by STATUS instead, which is
the condition that actually corresponds to `isOpen`, and finds **426**. Three
nets, three answers: 0, 41, 426. Which line you draw is the entire result.

## Caveats that belong beside the number

- **It is a SANDBOX figure** (`NS_ACCOUNT_ID`). Production receipt state can
  diverge; re-run there before cutover. "If production disagreed, would anything
  say so?" is no -- hence the account printed beside the count.
- **It is a ceiling, not a forecast.** A line only asks once a slip actually
  touches it, so the wave is this number intersected with what ships in the
  coming weeks: smaller, and spread out.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from netsuite_client import NetSuiteClient, NetSuiteConfig  # noqa: E402

#: NOTE: SuiteQL does NOT support parameter binding -- {"q": "... = ?"} returns
#: 400 INVALID_CONTENT. Every value here is a literal in source, so there is
#: nothing to bind; do not reach for string interpolation instead.
#:
#: Statuses 'G' (Fully Billed) and 'H' (Closed) are excluded because their lines
#: are settled: `isOpen` is False on all of them even though `isClosed` is also
#: False. That exclusion is what takes the candidate set from 992 POs to a few
#: dozen, and it is the whole reason this script is not a one-liner.
QUERY_CANDIDATES = """
SELECT DISTINCT transaction AS po_id
FROM transactionLine
WHERE isclosed = 'F'
  AND quantityshiprecv > 0
  AND transaction IN (
        SELECT id FROM transaction WHERE type = 'PurchOrd' AND status NOT IN ('G','H')
      )
"""

#: The naive figure, computed only so the script can show what it is NOT. Keeping
#: it visible is the point: someone will write this query again in six months.
QUERY_NAIVE = """
SELECT COUNT(*) AS lines, COUNT(DISTINCT transaction) AS pos
FROM transactionLine
WHERE isclosed = 'F'
  AND quantityshiprecv > 0
  AND transaction IN (SELECT id FROM transaction WHERE type = 'PurchOrd')
"""


def main() -> int:
    config = NetSuiteConfig.from_env()
    client = NetSuiteClient(config=config)

    print("=" * 72)
    print("PRE_EXISTING_RECEIPT wave -- ceiling")
    print("=" * 72)
    print(f"account: {config.account_id}"
          f"{'  (SANDBOX -- re-run against production before cutover)' if config.is_sandbox else '  (PRODUCTION)'}")
    print()

    naive = client.suiteql(QUERY_NAIVE)
    naive_lines = int(naive[0]["lines"]) if naive else 0
    naive_pos = int(naive[0]["pos"]) if naive else 0

    candidates = [r["po_id"] for r in client.suiteql(QUERY_CANDIDATES)]
    print(f"stage 1 (SuiteQL): {len(candidates)} candidate POs")
    print("stage 2 (REST):    reading each, counting lines where is_open AND received > 0")
    print()

    lines_seen = open_with_receipt = 0
    pos_affected: set = set()
    units = 0.0
    unreadable: list = []

    for po_id in candidates:
        try:
            lines = client.get_purchase_order_lines_by_internal_id(po_id)
        except Exception as exc:  # noqa: BLE001 -- one unreadable PO must not lose the count
            unreadable.append((po_id, type(exc).__name__))
            continue
        for ln in lines:
            lines_seen += 1
            if ln.is_open and (ln.quantity_received or 0) > 0:
                open_with_receipt += 1
                pos_affected.add(po_id)
                units += float(ln.quantity_received or 0)

    print(f"  candidate PO lines read              {lines_seen:>8,}")
    print(f"  OPEN and already received against    {open_with_receipt:>8,}   <-- the ceiling")
    print(f"  purchase orders involved             {len(pos_affected):>8,}")
    print(f"  units already on those lines         {units:>12,.0f}")
    if unreadable:
        print(f"  POs that could not be read           {len(unreadable):>8,}   {unreadable[:3]}")
    print()
    print(f"  the naive `isclosed = 'F'` count     {naive_lines:>8,} lines / {naive_pos:,} POs")
    if open_with_receipt:
        print(f"  ...which overstates it by            {naive_lines / open_with_receipt:>8,.0f}x")
    print("  isClosed is NOT the complement of isOpen -- see this file's docstring.")
    print()
    print("Each of those lines asks ONCE, then retires: the confirmation becomes")
    print("history and later shipments accumulate normally. The real wave is only")
    print("the part of this that actually ships in the coming weeks.")
    print()
    print("Do NOT pre-fill write_attempts to suppress them. RUNBOOK section 6 item 30:")
    print("writing 'we wrote N' for a quantity this tool did not write fabricates the")
    print("record that every later accumulation on that line is computed from.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
