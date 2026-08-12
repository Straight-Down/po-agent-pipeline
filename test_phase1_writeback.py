"""
Phase 1 acceptance test -- NetSuite M2M write-back under the "PO Update" role.

This mirrors, exactly, the read/write/verify/revert round-trip that was already
validated manually against sandbox PO 8489541 (PO# 1662, line 18,
M120246-TID-3X) -- with one critical difference: that earlier test ran under the
CFO role via Cowork's connector. This one runs under the real
M2M-authenticated, least-privilege "PO Update" role, with no browser login
anywhere in the flow.

Why that difference is the entire point (architecture doc section 6, final
bullet): the CFO test proved the REST API *mechanically* supports these sublist
writes. It did not prove this role is *permitted* to make them. NetSuite custom
fields can carry field-level access restrictions independent of record-level
Edit permission, so `custcol_override_expected_receipt` and
`custcol_sd_updatedreceiptdate` are the fields most likely to behave
differently here.

Two distinct failure modes are checked, because only one of them is loud:
  1. HARD FAILURE -- NetSuite returns 403 / INSUFFICIENT_PERMISSION. Obvious.
  2. SILENT DISCARD -- NetSuite accepts the PATCH (HTTP 204, no error) but
     quietly drops a field it won't let this role write, leaving the old value
     in place. This is the dangerous one: without reading the record back
     field-by-field it looks like a clean success. Step 3 below catches it.

Neither is worked around here. If either happens the test fails loudly and
tells you what to ask NetSuite support about -- widening the role's permissions
is a decision for Kiko, not for this script.

Safety properties:
  - Refuses to run against a non-sandbox account unless --allow-production is
    passed explicitly (sandbox-first is a firm project rule).
  - Reverts in a finally block, so an assertion failure mid-test still restores
    the original values.
  - Verifies the revert too -- a test that leaves a sandbox record dirty is a
    test that lies to the next person.
  - --dry-run prints the exact payloads and writes nothing.

Usage:
    .venv\\Scripts\\python.exe test_phase1_writeback.py
    .venv\\Scripts\\python.exe test_phase1_writeback.py --dry-run
    .venv\\Scripts\\python.exe test_phase1_writeback.py --po-internal-id 8489541 --line 18

Exit codes:
    0  PASS -- Phase 1 write-path proven under the PO Update role
    1  FAIL -- a check failed (details printed)
    2  CONFIG -- credentials/setup incomplete; never reached NetSuite
    3  PERMISSION FINDING -- authenticated fine, but the role was refused
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import traceback

from netsuite_client import (
    NS_EXPECTED_RECEIPT_DATE,
    NS_OVERRIDE_EXPECTED_RECEIPT,
    NS_QUANTITY,
    NS_UPDATED_RECEIPT_DATE,
    NetSuiteAPIError,
    NetSuiteClient,
    NetSuiteConfig,
    NetSuiteConfigError,
    NetSuiteError,
    NetSuitePermissionError,
    POLine,
)

# The exact record the manual CFO-role test used, so this is a like-for-like
# comparison rather than a new experiment.
DEFAULT_PO_INTERNAL_ID = "8489541"
DEFAULT_PO_NUMBER = "1662"
DEFAULT_LINE = 18
EXPECTED_STYLE = "M120246"
EXPECTED_COLOR = "TID"
EXPECTED_SIZE = "3X"

# Target date from the documented test (the shipment's real port ETA).
TARGET_DATE = dt.date(2026, 6, 27)
TARGET_QUANTITY = 99
FALLBACK_QUANTITY = 98  # if the line already sits at 99, a no-op proves nothing
FALLBACK_DATE = dt.date(2026, 6, 26)


class CheckFailed(Exception):
    """A verification step failed."""


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_PASS = "PASS"
_FAIL = "FAIL"


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def check(ok: bool, description: str, detail: str = "") -> bool:
    print(f"  [{_PASS if ok else _FAIL}] {description}" + (f"  --  {detail}" if detail else ""))
    return ok


def describe_line(line: POLine) -> None:
    print(f"    line              : {line.line_id}")
    print(f"    item              : {line.item}")
    print(f"    style/color/size  : {line.style_number} / {line.color} / {line.size}")
    print(f"    quantity          : {line.quantity}")
    print(f"    expectedReceipt   : {line.expected_receipt_date}")
    print(f"    override flag     : {line.override_expected_receipt}")
    print(f"    updatedReceipt    : {line.updated_receipt_date}")
    print(f"    closed            : {line.closed}")


def snapshot(line: POLine) -> dict:
    """The four target fields, in the pipeline's snake_case vocabulary."""
    return {
        "quantity": line.quantity,
        "expected_receipt_date": line.expected_receipt_date,
        "override_expected_receipt": line.override_expected_receipt,
        "updated_receipt_date": line.updated_receipt_date,
    }


#: snake_case -> the NetSuite field name, for error messages that need to name
#: the actual field a NetSuite admin would look up.
NS_NAMES = {
    "quantity": NS_QUANTITY,
    "expected_receipt_date": NS_EXPECTED_RECEIPT_DATE,
    "override_expected_receipt": NS_OVERRIDE_EXPECTED_RECEIPT,
    "updated_receipt_date": NS_UPDATED_RECEIPT_DATE,
}


def build_target_values(original: dict) -> dict:
    """
    Pick target values that mirror the documented test but are guaranteed to
    differ from what's currently there -- writing a value that's already set
    would pass vacuously and prove nothing about permissions.
    """
    return {
        "quantity": TARGET_QUANTITY if original["quantity"] != TARGET_QUANTITY else FALLBACK_QUANTITY,
        "expected_receipt_date": (
            TARGET_DATE if original["expected_receipt_date"] != TARGET_DATE else FALLBACK_DATE
        ),
        # Flip it, whichever way it currently sits.
        "override_expected_receipt": not original["override_expected_receipt"],
        "updated_receipt_date": (
            TARGET_DATE if original["updated_receipt_date"] != TARGET_DATE else FALLBACK_DATE
        ),
    }


def compare(expected: dict, actual: dict, *, phase: str) -> list[str]:
    """
    Field-by-field comparison. Returns the list of fields that did NOT take.

    This is what catches silent discards -- the write returned 204, so the only
    evidence a field was refused is that its value didn't move.
    """
    discarded = []
    for key, want in expected.items():
        got = actual[key]
        ok = got == want
        check(ok, f"{phase}: {key} ({NS_NAMES[key]})", f"expected {want!r}, read back {got!r}")
        if not ok:
            discarded.append(key)
    return discarded


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    section("PHASE 1 ACCEPTANCE TEST -- NetSuite M2M write-back, 'PO Update' role")
    print()
    print("  Mirrors the manual CFO-role test on the same record, but under the")
    print("  least-privilege role with M2M/JWT auth and no browser login.")

    # -- config -------------------------------------------------------------
    try:
        config = NetSuiteConfig.from_env()
    except NetSuiteConfigError as exc:
        section("RESULT: CONFIG INCOMPLETE")
        print(f"\n{exc}\n")
        print("  Nothing was sent to NetSuite. Finish NETSUITE-M2M-SETUP.md first.\n")
        return 2

    if not config.is_sandbox and not args.allow_production:
        section("RESULT: REFUSED -- NON-SANDBOX ACCOUNT")
        print(f"\n  NS_ACCOUNT_ID is {config.account_id!r}, which does not look like a sandbox account.")
        print("  Sandbox-first is a firm rule for this project (working agreement, CLAUDE.md).")
        print("  This test WRITES to a real PO line. If you genuinely mean to run it against")
        print("  production, pass --allow-production.\n")
        return 2

    print()
    print(f"  account        : {config.account_id}  ({'SANDBOX' if config.is_sandbox else 'PRODUCTION'})")
    print(f"  host           : {config.host}")
    print(f"  client id      : {config.client_id[:12]}...{config.client_id[-4:]}")
    print(f"  certificate id : {config.certificate_id}")
    print(f"  private key    : {config.private_key_path}")
    print(f"  jwt algorithm  : {config.algorithm}")
    print(f"  target         : PO internal id {args.po_internal_id}, line {args.line}")
    print(f"  mode           : {'DRY RUN (no writes)' if args.dry_run else 'LIVE (will write, then revert)'}")

    client = NetSuiteClient(config=config)

    # -- step 1: authenticate ----------------------------------------------
    section("STEP 1 -- Authenticate via M2M/JWT (no browser)")
    try:
        info = client.verify_connection()
    except NetSuitePermissionError as exc:
        section("RESULT: PERMISSION FINDING AT AUTH/PROBE")
        print(f"\n{exc}\n")
        return 3
    except NetSuiteError as exc:
        section("RESULT: AUTHENTICATION FAILED")
        print(f"\n{exc}\n")
        return 1

    print()
    ok = check(True, "access token obtained via signed JWT assertion", f"expires in ~{info['token_expires_in']}s")
    ok &= check(info["probe_status"] < 300, "authenticated read (purchaseOrder metadata catalog)", f"HTTP {info['probe_status']}")
    print()
    print(f"    no interactive login, no refresh token, no browser consent screen ({info['elapsed_seconds']}s)")

    # Reported, deliberately NOT a pass/fail gate: Phase 1's write path targets
    # an internal id directly and never lists the collection. Phase 2's
    # PO-number -> internal-id lookup does, so this needs resolving before then.
    if not info["collection_listing_ok"]:
        print()
        print("  NOTE -- collection listing is REFUSED for this role:")
        print(f"    {info['collection_listing_detail']}")
        print("    Not a Phase 1 blocker (this test targets an internal id directly), but it")
        print("    WILL block Phase 2: resolving a PO number like '1662' to internal id")
        print("    '8489541' needs the collection/search endpoint. Flagged in the summary.")

    if not ok:
        return 1

    # -- step 2: read the line ---------------------------------------------
    section("STEP 2 -- Read the target line and snapshot its current values")
    try:
        line = client.get_po_line(args.po_internal_id, args.line, by_internal_id=True)
    except NetSuitePermissionError as exc:
        section("RESULT: PERMISSION FINDING ON READ")
        print(f"\n{exc}\n")
        print("  The role authenticated but cannot read the PO. Check Transactions >")
        print("  Purchase Order = Edit (Edit implies View) and Lists > Items = View.\n")
        return 3
    except NetSuiteError as exc:
        section("RESULT: READ FAILED")
        print(f"\n{exc}\n")
        return 1

    print()
    describe_line(line)
    print()

    identity_ok = True
    identity_ok &= check(line.style_number == EXPECTED_STYLE, f"style is {EXPECTED_STYLE}", f"got {line.style_number!r}")
    identity_ok &= check(line.color == EXPECTED_COLOR, f"color is {EXPECTED_COLOR}", f"got {line.color!r}")
    identity_ok &= check(line.size == EXPECTED_SIZE, f"size is {EXPECTED_SIZE}", f"got {line.size!r}")
    if not identity_ok:
        section("RESULT: WRONG LINE -- ABORTING BEFORE ANY WRITE")
        print()
        print(f"  Line {args.line} of PO {args.po_internal_id} is not the {EXPECTED_STYLE}-{EXPECTED_COLOR}-{EXPECTED_SIZE}")
        print("  line the documented test used. Sandbox data may have been refreshed or the")
        print("  PO edited since. Nothing was written. Re-point with --line / --po-internal-id")
        print("  after confirming the right line in the NetSuite UI.\n")
        return 1

    if line.closed:
        section("RESULT: LINE IS CLOSED -- ABORTING BEFORE ANY WRITE")
        print("\n  NetSuite rejects edits to closed PO lines. Pick an open line.\n")
        return 1

    original = snapshot(line)
    target = build_target_values(original)

    print()
    print("  Planned change (all four fields in a single PATCH, mirroring the CFO-role test):")
    for key in original:
        print(f"    {key:<28} {original[key]!r}  ->  {target[key]!r}")

    if args.dry_run:
        section("DRY RUN -- payload that WOULD be sent")
        from netsuite_client import normalize_line_fields

        print()
        print(f"  PATCH {config.record_base}/purchaseOrder/{args.po_internal_id}")
        print(f'  {{"item": {{"items": [{{"line": {args.line}, ...}}]}}}} with fields:')
        for k, v in normalize_line_fields(target).items():
            print(f"    {k:<38} = {v!r}")
        print()
        print("  Note: no `replace` query parameter is sent -- that would replace the whole")
        print("  sublist instead of merging this one line.")
        print()
        print("  Nothing written. Re-run without --dry-run for the real test.\n")
        return 0

    # -- steps 3 & 4: write, verify, revert --------------------------------
    write_succeeded = False
    exit_code = 1
    try:
        section("STEP 3 -- Write all four fields in one PATCH, then read back")
        try:
            result = client.update_po_line(
                args.po_internal_id, args.line, target, by_internal_id=True
            )
            write_succeeded = True
            print()
            check(True, "PATCH accepted", f"HTTP {result['status_code']}")
        except NetSuitePermissionError as exc:
            section("RESULT: PERMISSION FINDING ON WRITE  <-- THE THING WE WERE WATCHING FOR")
            print(f"\n{exc}\n")
            print("  This is a genuine finding: the CFO role could make this write and the")
            print("  'PO Update' role cannot. Do NOT widen the role to make it pass.")
            print("  What to take to your NetSuite admin:")
            print("    - does the 'PO Update' role need Transactions > Purchase Order = Edit")
            print("      at a higher LEVEL (not just Edit), or an additional permission?")
            print(f"    - do the custom columns {NS_OVERRIDE_EXPECTED_RECEIPT} and")
            print(f"      {NS_UPDATED_RECEIPT_DATE} have field-level access restrictions")
            print("      (Customization > Lists,Records,&Fields > Transaction Line Fields >")
            print("      [field] > Access tab) that exclude this role?\n")
            return 3

        print()
        readback = client.get_po_line(args.po_internal_id, args.line, by_internal_id=True)
        describe_line(readback)
        print()

        discarded = compare(target, snapshot(readback), phase="verify")

        if discarded:
            section("RESULT: SILENT FIELD DISCARD  <-- THE QUIET FAILURE MODE")
            print()
            print(f"  NetSuite accepted the PATCH (HTTP {result['status_code']}, no error) but these")
            print("  fields did not change:")
            for key in discarded:
                print(f"    - {key}  ({NS_NAMES[key]})")
            print()
            print("  That pattern -- write accepted, value unchanged -- is what a field-level")
            print("  access restriction looks like from the outside. It is strictly worse than a")
            print("  403, because in production it would look like a successful update while")
            print("  quietly doing nothing.")
            print()
            print("  This is a real finding. Do NOT widen the role's permissions to make it")
            print("  pass. Take to your NetSuite admin: Customization > Lists,Records,&Fields >")
            print("  Transaction Line Fields > [field] > Access tab, and check whether the")
            print("  'PO Update' role has Edit access to each custom column above.")
            exit_code = 3
        else:
            print("  All four fields took. Write-path confirmed under this role.")
            exit_code = 0

    except Exception:
        section("UNEXPECTED ERROR DURING WRITE/VERIFY")
        print()
        traceback.print_exc()
        exit_code = 1

    finally:
        if write_succeeded:
            section("STEP 4 -- Revert to the original values (always runs)")
            try:
                client.update_po_line(args.po_internal_id, args.line, original, by_internal_id=True)
                reverted = client.get_po_line(args.po_internal_id, args.line, by_internal_id=True)
                print()
                leftover = compare(original, snapshot(reverted), phase="revert")
                if leftover:
                    print()
                    print("  *** THE SANDBOX RECORD IS LEFT DIRTY. Restore these by hand in the")
                    print("      NetSuite UI before anyone relies on this PO: ***")
                    for key in leftover:
                        print(f"        line {args.line} {NS_NAMES[key]} -> {original[key]!r}")
                    exit_code = 1
                else:
                    print()
                    print("  Record restored to its original state.")
            except Exception:
                print()
                traceback.print_exc()
                print()
                print("  *** REVERT FAILED -- THE SANDBOX RECORD IS LEFT MODIFIED. ***")
                print(f"      Restore line {args.line} of PO {args.po_internal_id} by hand:")
                for key, value in original.items():
                    print(f"        {NS_NAMES[key]} -> {value!r}")
                exit_code = 1

    # -- verdict ------------------------------------------------------------
    if exit_code == 0:
        section("RESULT: PASS -- PHASE 1 WRITE-PATH PROVEN")
        print()
        print("  Confirmed under the least-privilege 'PO Update' role, M2M/JWT auth:")
        print("    - authenticated with no browser login, no refresh token")
        print(f"    - read PO internal id {args.po_internal_id} line {args.line}")
        print("    - wrote quantity, expectedReceiptDate, custcol_override_expected_receipt")
        print("      and custcol_sd_updatedreceiptdate together in a single PATCH")
        print("    - verified every field field-by-field, then reverted and verified the revert")
        print()
        print(f"  PO lookup strategy that worked: {client.last_lookup_strategy or 'n/a (internal id given directly)'}")
        print()
        print("  This closes the open validation step in architecture doc section 6.")
        print()
        if not info["collection_listing_ok"]:
            print("  OPEN FINDING carried forward (does NOT affect this pass):")
            print("    This role cannot list/search record collections, so it cannot yet map a")
            print("    PO NUMBER to an internal id -- needed from Phase 2 on. Resolve with your")
            print("    NetSuite admin before Phase 2; do not widen the role unilaterally.")
            print()
        print("  Next: Prompt 2 in Claude-Code-Kickoff-Prompts.md (the parsing layer).")
        print()
    elif exit_code == 3:
        print()
        print("  Phase 1 is NOT done. Report the finding above before changing any permissions.")
        print()
    else:
        section("RESULT: FAIL")
        print()
        print("  Phase 1 is NOT done. See the failure detail above.")
        print()
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--po-internal-id", default=DEFAULT_PO_INTERNAL_ID, help=f"default {DEFAULT_PO_INTERNAL_ID} (PO# {DEFAULT_PO_NUMBER})")
    parser.add_argument("--line", type=int, default=DEFAULT_LINE, help=f"sublist line number (default {DEFAULT_LINE})")
    parser.add_argument("--dry-run", action="store_true", help="read and plan only; write nothing")
    parser.add_argument("--allow-production", action="store_true", help="required to run against a non-sandbox account")
    parser.add_argument("--verbose", "-v", action="store_true", help="show HTTP/auth debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return run(args)
    except NetSuiteAPIError as exc:
        section("RESULT: NETSUITE API ERROR")
        print(f"\n{exc}\n")
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
