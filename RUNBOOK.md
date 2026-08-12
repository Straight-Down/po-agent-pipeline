# PO Update Automation — Handoff Runbook

**Purpose:** so Brandon (or anyone else) can understand, operate, and fix this system without Kiko in the room — per Beth's discovery follow-up (2026-08-05), condition of moving past sandbox testing.

**Audience:** assumes general technical competence, no prior context on this specific project. Where more depth exists elsewhere, this doc points to it rather than repeating it — `PO-Update-Automation-Architecture.md` is the full design rationale; this doc is the "how do I actually run/fix/hand this off" companion.

**Last updated:** 2026-08-11

---

## 1. What this system does, in one paragraph

Paula (Supply Chain Manager) gets emails from vendors when a shipment goes out, with attached documents showing real final quantities and dates. Today she reads those by hand and re-types the numbers into NetSuite Purchase Orders. This system automates the reading and matching, and stages the proposed NetSuite changes — but it never writes to NetSuite without Paula explicitly approving each change first. That human approval step is permanent by design, not a temporary safeguard.

## 2. Current status (as of 2026-08-11)

| Piece | Status |
|---|---|
| NetSuite auth (M2M/JWT, least-privilege role) | **Built and validated in sandbox.** All four target fields write correctly. Not yet moved to production. |
| Document parsing (3 real vendors: Inprotex, Legendz, Symmetry) | **Built and validated against real vendor files**, live-tested via the Anthropic API. Only 3 vendors confirmed — total vendor count for v1 is still unknown (open item, §6 below). |
| Attachment triage (choosing which email attachment to actually parse) | **Built and validated** against a real 6-attachment vendor email. |
| Matching (vendor line → NetSuite PO line) | **Built**, exact-match on custom fields, size-alias normalization for letter sizes only (see §6, known gap). |
| Business rules (dates, quantity, absent lines) | **Built**, based on Paula's direct answers (2026-08-10/11) — see §4. |
| Email intake (reading Paula's inbox automatically) | **Not built.** Design decided (direct Graph API access to her mailbox), nothing implemented yet. |
| Review/approval step (the actual UI/email Paula interacts with) | **Not built.** |
| NetSuite write-back triggered by approval | **Not built** (the underlying write call is proven from Phase 1, but nothing wires an approval to it yet). |
| Hosting (Azure Functions, database, Key Vault) | **Not provisioned.** Everything currently runs only on Kiko's laptop. |
| This runbook | You're reading it. |

In short: the hard, uncertain part (can this reliably read messy vendor documents and figure out what changed) is proven. The remaining work is wiring it into something that runs unattended.

## 3. How email-to-PO matching works

1. **Intake** (not yet built) will read new emails from Paula's inbox via Microsoft Graph API (direct access, decided 2026-08-10 — no shared mailbox).
2. **Attachment triage** (`attachment_classifier.py`) looks at every attachment in the email and decides what it is — packing list, invoice, payment request, inspection report, shipping schedule. **Filenames are not trusted for this** — one real vendor's invoice was literally named "...PACKING LIST.pdf." Classification looks at document content instead (does this sheet/page actually break quantities out by size). Only the packing list gets parsed for shipment data; everything else is set aside.
3. **Parsing** (`document_parsers.py`, `claude_extractor.py`, `parse_packing_slip.py`) extracts PO number, style, color, size, and quantity from the packing list.
   - If the file is a known, previously-validated format (currently just Inprotex), a fast deterministic parser handles it for free.
   - Otherwise, the Anthropic API reads the document's actual structure (not just an image) and returns the same structured fields. This is the primary path for essentially all vendors, since every vendor's layout is different.
   - Anything the extractor isn't confident about gets flagged for manual review rather than guessed.
4. **Matching** (`matcher.py`) takes those parsed lines and looks up the real NetSuite PO. It matches to the exact PO line using NetSuite's own custom fields (`custcol_sd_tmpl_style`, `custcol_product_color.refName`, `custcol_product_size.refName`), not by parsing the item's display name. Size labels get normalized first (e.g., vendor's "XXL" -> NetSuite's "2X") via `SIZE_ALIASES`.
5. A PO line that doesn't appear in a given shipment's packing list is left alone entirely — no record, no flag. Paula confirmed POs routinely ship in batches, so this is the normal case, not an error.

## 4. How the diff/approval logic works

`matcher.py`'s `ProposedChange` represents one line's proposed update. The rules baked into it come directly from Paula, not from assumptions:

- **Quantity**: the packing list's shipped quantity **replaces** the PO line's current quantity. Shipping more than was ordered is normal and accepted — it does not get flagged as unusual.
- **Receipt dates are never computed or proposed by this system.** Paula determines the actual receipt date herself, using her own knowledge of customs/trucking buffers — she explicitly does not use the vendor's stated arrival date. Enforced structurally: `ProposedChange` has no `proposed_expected_receipt_date` field at all. The vendor's ETD/ETA are still shown as labeled reference information, but `to_netsuite_fields(include_dates=True)` will raise `DateNotConfirmed` until a human calls `confirm_receipt_date()`. Quantity-only writes are unaffected by this and work normally.
- **Inspection reports (QC documents) are never a data source**, even on the rare occasion one contains data the packing list lacks. This is enforced in code — `parse_shipment_documents` raises `ExtractionError` if handed an inspection report.
- **A vendor's packing list that can't be resolved to individual size-level lines results in a manual-entry flag**, not a guess (no proportional splitting, no inference from another document).

**Not yet confirmed:** if a single PO ships in two genuinely separate batches weeks apart (not just multiple styles on one PO), does the second batch's quantity replace what's in NetSuite, or add to it? The code currently replaces. Low urgency, worth asking Paula before this goes further.

## 5. Where everything lives

**Code and docs** (OneDrive-synced, shared, safe to have here): the `PO Agent` project folder. All `.py` files, all `.md` planning docs, sample vendor files used for testing.

**Secrets — deliberately NOT in the synced folder:**
- NetSuite M2M private key + certificate: `C:\Users\kiko.barroso\.po-agent\keys\`
- Anthropic API key: `C:\Users\kiko.barroso\.po-agent\.env`
- NetSuite identifiers (account ID, client ID, cert ID — safe to have alongside code since they're useless without the private key): the project folder's own `.env`

**Why the split:** anything that can authenticate on its own (a private key, an API key) must never sit in the OneDrive-synced folder — it would sync to the cloud and be inherited by anyone the folder is ever shared with. Identifiers that are meaningless without a separate secret are fine in the synced `.env`. If a new secret gets added to this project in the future (a Graph API app secret, a database connection string), it follows the same rule: outside the synced folder, ideally eventually into Azure Key Vault once that's provisioned.

**NetSuite environments:** sandbox is `1321665-sb2`, currently the only one this touches. Production is a separate account with its own Integration record and role, not yet set up (Phase 4 work).

**Database:** doesn't exist yet. Planned as Azure SQL Database (serverless tier), not built.

## 6. Known limitations and open risks (as of 2026-08-11)

Ranked by how much they matter. Some have fixes in progress — check with Claude Code for current status on any marked "fix requested."

1. ~~**A real vendor document with actual banking details (account number, SWIFT code) is sitting in the project folder as a test fixture.**~~ **FIXED 2026-08-11.** The real file was moved to `%USERPROFILE%\.po-agent\vendor-documents-private\` (outside OneDrive, ACL-locked to Kiko only, same treatment as the private key and API key) and is no longer referenced by any test. Tests now use `fixtures/SD Vendor Payment Request SAMPLE (synthetic).pdf` — identical "Request for Payment" structure and field layout, entirely invented bank, account number, SWIFT and recipient. Regenerate with `python make_test_fixtures.py`. The real filename no longer appears in any `.py` file either.
2. ~~**The matcher doesn't check whether a NetSuite PO line is already closed before proposing a change to it.**~~ **FIXED 2026-08-11.** `matcher.build_proposed_changes` now checks `line.closed`: a vendor line matching a closed NetSuite line becomes `NEEDS_ATTENTION` with reason *"PO line is closed in NetSuite; vendor data references it but no automatic change proposed"*, never a `PENDING_REVIEW` quantity change. The write path refuses independently — `ProposedChange.to_netsuite_fields()` raises `LineClosed` — so a closed line can't be written even if something upstream tried. Other lines in the same shipment are unaffected.
3. ~~**No retry/backoff for network timeouts or NetSuite 5xx errors.**~~ **FIXED 2026-08-11.** `NetSuiteClient._request` retries up to 3 attempts with exponential backoff (0.5s, 1.0s) for connection errors, timeouts and 5xx responses, then raises `NetSuiteTransientError` carrying the attempt count and last status — so the audit log can tell "NetSuite was down" from "the request was wrong". **4xx is never retried**, deliberately: NetSuite returns permission denials as 400s, so retrying client errors would delay and risk masking exactly the failures that most need to surface immediately. SSL errors also propagate rather than retrying, being a config problem rather than a blip.
4. **Size matching has only ever been exercised against letter sizes** (S/M/L/XL/2XL/3XL). **INVESTIGATED 2026-08-11 — could not be answered, and the reason is itself a finding.** Every catalog-wide introspection path is denied to the least-privilege "PO Update" role: SuiteQL is refused (the denial arrives disguised as `INVALID_PARAMETER` on a 400, with *"Your current role does not have permission"* buried in the detail), record collection queries are refused (item 8), and the size custom list `customlist_psgss_product_size` cannot be read either. Probing 100 internal ids found exactly **one** readable PO (8489541, the one we were given), whose sizes are `S, M, L, XL, 2X, 3X` with size run `S,M,L,XL,2X,3X` — all letter sizes, but a one-PO sample is not evidence about the catalog. **No speculative numeric-size aliases were added.** What was added is a test proving the fail-safe: an unrecognized size (`32`, `W32L34`, `34x30`, `One Size`, empty) flags `NEEDS_ATTENTION` and never silently mis-matches an existing line. **Action: add "can we enumerate the item catalog / size list?" to the same NetSuite admin conversation as item 8** — the two are the same permission gap, and answering it also answers this.
5. ~~**No handling yet for a corrupt or password-protected vendor file.**~~ **FIXED 2026-08-11.** All `openpyxl.load_workbook` / `pdfplumber.open` calls now go through `claude_extractor.open_workbook` / `open_pdf`, which raise `DocumentUnreadable` with a specific reason (truncated/corrupt zip, encrypted/password-protected, empty, wrong format, OS permission). Batch behaviour is the point: `build_source_documents` and attachment triage **flag the individual bad attachment and continue processing the rest** rather than aborting the shipment, and triage reports "could not open: &lt;reason&gt;" as its own condition, distinct from "has no size data". Two new fixtures cover it (`fixtures/corrupt_truncated.xlsx`, `fixtures/encrypted_password_protected.pdf`).
6. **This entire system currently only runs from Kiko's laptop, using his own NetSuite employee record.** Not solved by this runbook — solved by the pending Azure migration (Key Vault, Function App) and, if audit clarity becomes important later, a dedicated NetSuite service account (currently a deliberate cost trade-off, documented in the architecture doc §6).
7. **Vendor coverage for v1 is still undefined.** Three vendors are validated; the actual number Straight Down needs covered before this can fully replace Paula's manual process is unknown. Needs a vendor list from Paula.
8. **The NetSuite least-privilege role can't do PO-number search/lookup**, only direct-by-ID reads. Blocks resolving a vendor's human PO number to NetSuite's internal ID automatically. Needs a NetSuite admin conversation (in progress, Brandon looped in). **Confirmed wider than first thought (2026-08-11):** the same gap also blocks SuiteQL entirely and blocks reading custom lists, which is what made item 4 unanswerable. Worth raising as one question — "what does this role need to read the item catalog and query transactions by tranId?" — rather than two.

   **Permissions tried and ruled out (2026-08-11), so nobody repeats them:**

   | Added to the role | Result on `GET /purchaseOrder?limit=1` |
   |---|---|
   | `Transactions > Find Transaction` (View) | unchanged — 400 `USER_ERROR` |
   | `Lists > Perform Search` | unchanged — 400 `USER_ERROR` |

   In every case the response is byte-identical: `400`, `o:errorCode: USER_ERROR`, detail *"Your current role does not have permission to perform this action."* A fresh token was forced before each retest, so none of these are stale-token artifacts. Filtered (`?q=tranId IS "1662"`, both quoted and unquoted) and unfiltered (`?limit=1`) queries fail identically and move together — so the block is on the collection endpoint itself, not on result-set breadth or query syntax. **Note the `q=` syntax question is therefore still open:** both quoting forms are rejected before parsing, so the first successful call is what will settle it (`resolve_po_internal_id()` tries both and records the winner in `last_lookup_strategy`).

   **Cheapest untested candidate: "Web Services Only Role" is checked on this role.** It was confirmed safe for authentication and the by-id read/write path (Phase 1 passed with it checked) but was never tested against collection endpoints, because those have never worked for this role and there was no baseline to compare against. It's a checkbox rather than a new permission, so it's one quick test — but if it turns out to be the cause it's a real trade-off (hardening vs. PO-number lookup) and Kiko's call, not an automatic revert.

   **Question phrased for an admin:** *"For a Client Credentials M2M integration with a custom role, what exactly is required for `GET /services/rest/record/v1/purchaseOrder` (collection) to return results instead of USER_ERROR, when `GET /purchaseOrder/{id}` already works?"* Handing over the working/failing pair is usually what makes this diagnosable in one pass.

   **Impact, and why it isn't a hard blocker:** Phase 1 is unaffected — the write path never needed collection access. This blocks Phase 2's PO-number→internal-id resolution. If the permission proves genuinely unavailable, the fallback is for the pipeline to maintain its own PO-number→internal-id map, populated as POs are encountered, since by-id reads work fine. More moving parts, and not worth building before the admin conversation.
9. **Paula's mailbox access is decided (direct Graph API) but not yet implemented.**
10. **NetSuite's M2M certificate expires 2028-08-03** — calendar reminder only, no automated alert. Low urgency given the lead time, but worth a real alert once this is hosted on Azure rather than relying on memory.

## 7. How to recover when something breaks

| Symptom | Likely cause | What to do |
|---|---|---|
| NetSuite auth fails (`invalid_client`, `invalid_grant`) | Certificate/role mapping issue, or a sandbox refresh wiped the Integration record | See `NETSUITE-M2M-SETUP.md` troubleshooting table — sandbox refreshes wipe Integration records and cert mappings; redo Steps 1-5, the keypair itself stays valid |
| NetSuite write returns 400 with `USER_ERROR` text | Permission problem, not a generic bad request — NetSuite doesn't reliably use 403 for these | Check the role's permissions; don't assume it's a malformed request |
| Anthropic API calls fail or stop working | Key revoked/expired, or spend limit hit | Check console.anthropic.com under the Straight Down org, Settings -> API Keys and -> Billing |
| A vendor's file produces low-confidence or no extraction | New/unfamiliar layout, or a corrupted/encrypted file | Check the flagged reason in the output — the system is designed to flag rather than guess, so a flag here is expected behavior, not necessarily a bug |
| Nothing has run in a while and you're not sure if it's broken or just quiet | No monitoring exists yet (this is flagged in the build plan as a hard requirement before production) | Currently must be checked manually — this is exactly the gap Phase 4's monitoring work is meant to close |

## 8. Access and ownership inventory

| What | Where | Who has it |
|---|---|---|
| NetSuite sandbox admin access | NetSuite `1321665-sb2` | Kiko, plus whoever has admin in that account |
| NetSuite "PO Update Automation (M2M)" role | Attached to Kiko's own employee record | Kiko only — see §6 item 6 |
| Anthropic Console org | console.anthropic.com | Whoever set up the Straight Down org — confirm who has admin/billing access |
| Azure subscription | Not yet used by this project | Brandon (assumed) — confirm before Phase 2 infrastructure work starts |
| This project's code and docs | `PO Agent` OneDrive folder | Anyone with folder access |
| Private key / API key | Kiko's local machine only, outside the synced folder | Kiko only — this is the single point of failure described in §6 item 6 |

## 9. What's left to build

See `PO-Update-Automation-Build-Plan.md` for full phase detail. Short version: Phase 1 (NetSuite proof-of-concept) is done. The parsing/matching layer under Phase 1's extended scope is done and validated against 3 real vendors. Phase 2 (email intake, database, Azure infrastructure) has not started. Phase 3 (review/approval UI, write-back wiring) has not started. Phase 4 (production cutover, monitoring) has not started.
