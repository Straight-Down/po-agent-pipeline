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

**Version control — the `.git` layout is deliberately unusual.** The working tree stays here in the OneDrive-synced `PO Agent` folder, but **`.git` is a FILE, not a directory**, containing a single line:

```
gitdir: C:/dev/po-agent.git
```

It was created with `git init --separate-git-dir "C:\dev\po-agent.git"` so the git database (objects, refs, index) lives **outside** OneDrive. OneDrive syncing git internals mid-write is a known corruption risk. Everything works normally — `git status`, `git log`, `git push` all behave as usual from inside this folder — but **it looks broken if you don't know**: tools that check for a `.git` *directory* will report "not a repository", and deleting that one small file orphans the history (recoverable: recreate the file with the same line).

Remote is `https://github.com/Straight-Down/po-agent-pipeline.git`, **private** — it must stay private, because the tracked vendor corpus contains real third-party commercial data (supplier unit prices, a named inspector, customer contact details). `.gitattributes` marks pdf/xlsx/png/docx as binary; without it Git's heuristic classified a generated PDF as text and would have rewritten its bytes on checkout, silently invalidating the validation corpus.

## 6. Known limitations and open risks (as of 2026-08-12)

Ranked by how much they matter. Items struck through are resolved, with the resolution recorded in place rather than deleted — the reasoning trail is the point.

1. ~~**A real vendor document with actual banking details (account number, SWIFT code) is sitting in the project folder as a test fixture.**~~ **FIXED 2026-08-11.** The real file was moved to `%USERPROFILE%\.po-agent\vendor-documents-private\` (outside OneDrive, ACL-locked to Kiko only, same treatment as the private key and API key) and is no longer referenced by any test. Tests now use `fixtures/SD Vendor Payment Request SAMPLE (synthetic).pdf` — identical "Request for Payment" structure and field layout, entirely invented bank, account number, SWIFT and recipient. Regenerate with `python make_test_fixtures.py`. The real filename no longer appears in any `.py` file either.
2. ~~**The matcher doesn't check whether a NetSuite PO line is already closed before proposing a change to it.**~~ **FIXED 2026-08-11.** `matcher.build_proposed_changes` now checks `line.closed`: a vendor line matching a closed NetSuite line becomes `NEEDS_ATTENTION` with reason *"PO line is closed in NetSuite; vendor data references it but no automatic change proposed"*, never a `PENDING_REVIEW` quantity change. The write path refuses independently — `ProposedChange.to_netsuite_fields()` raises `LineClosed` — so a closed line can't be written even if something upstream tried. Other lines in the same shipment are unaffected.
3. ~~**No retry/backoff for network timeouts or NetSuite 5xx errors.**~~ **FIXED 2026-08-11.** `NetSuiteClient._request` retries up to 3 attempts with exponential backoff (0.5s, 1.0s) for connection errors, timeouts and 5xx responses, then raises `NetSuiteTransientError` carrying the attempt count and last status — so the audit log can tell "NetSuite was down" from "the request was wrong". **4xx is never retried**, deliberately: NetSuite returns permission denials as 400s, so retrying client errors would delay and risk masking exactly the failures that most need to surface immediately. SSL errors also propagate rather than retrying, being a config problem rather than a blip.
4. ~~**Size matching has only ever been exercised against letter sizes**~~ **ANSWERED 2026-08-12: YES, non-letter sizes exist — and they are the majority.** `customlist_psgss_product_size` was enumerated in full on 2026-08-12, once `Setup > Custom Lists` (View) was added: **46 active values, ids 1–48** (35 and 36 absent), none inactive. **39 of the 46 are not letter sizes.**

   | Group | Count | Values |
   |---|---|---|
   | Letter | 7 | `XS` `S` `M` `L` `XL` `2X` `3X` |
   | Waist–inseam | 16 | `30-32` `32-32` `34-32` `36-32` `38-32` `40-32` `42-32` `44-32` · `30-34` `32-34` `34-34` `36-34` `38-34` `40-34` `42-34` `44-34` |
   | Shoe (incl. half sizes) | 11 | `6` `7` `8` `9` `9.5` `10` `10.5` `11` `12` `13` `14` |
   | Waist only | 8 | `30` `32` `34` `36` `38` `40` `42` `44` |
   | Numeric women's | 3 | `0` `2` `4` |
   | Special | 1 | `ALL` (abbreviation `A`) |

   **Three consequences to design around:**
   - **Sizes are not always integers** — `9.5` and `10.5` exist, so nothing may assume `int`.
   - **`ALL`'s abbreviation (`A`) differs from its name.** Every other value has abbreviation == name, so this is the one value where comparing the wrong field is invisible until it happens. Whichever field the matcher reads must be a deliberate choice.
   - **`32` is ambiguous** between waist-only and the `32-32`/`32-34` family, and which one it is depends on the garment. That is a garment-context problem, not a string problem — no normalizer can resolve it. `NEEDS_ATTENTION` is the correct deliberate output here, not a guess.

   **`SIZE_ALIASES` deliberately NOT extended.** No vendor document in the corpus contains a single non-letter size, so any mapping would be inference rather than evidence — a vendor might print `32x34`, `32/34`, `3232` or `32-34` for the same NetSuite value. Current behaviour on all 39 is `NEEDS_ATTENTION`: the fail-safe, working as designed. (The canonical-form normalizer does already fold en-dash/em-dash to ASCII hyphen, so `32–34` and `32-34` will key alike whenever such a sample does arrive.)

   **Targeted ask for Paula:** *which vendors ship numerically-sized styles — pants/bottoms or footwear?* That is a far more answerable question than "send more packing slips", and one real slip for such a style turns this from inference into evidence.

   **Also recorded:** Inprotex uses `2XL`, `XXL` **and** `XXXL` in the *same file* for what NetSuite stores as `2X`/`3X` — three conventions from one vendor in one document. `SIZE_ALIASES` already covers all three.
5. ~~**No handling yet for a corrupt or password-protected vendor file.**~~ **FIXED 2026-08-11.** All `openpyxl.load_workbook` / `pdfplumber.open` calls now go through `claude_extractor.open_workbook` / `open_pdf`, which raise `DocumentUnreadable` with a specific reason (truncated/corrupt zip, encrypted/password-protected, empty, wrong format, OS permission). Batch behaviour is the point: `build_source_documents` and attachment triage **flag the individual bad attachment and continue processing the rest** rather than aborting the shipment, and triage reports "could not open: &lt;reason&gt;" as its own condition, distinct from "has no size data". Two new fixtures cover it (`fixtures/corrupt_truncated.xlsx`, `fixtures/encrypted_password_protected.pdf`).
6. **This entire system currently only runs from Kiko's laptop, using his own NetSuite employee record.** Not solved by this runbook — solved by the pending Azure migration (Key Vault, Function App) and, if audit clarity becomes important later, a dedicated NetSuite service account (currently a deliberate cost trade-off, documented in the architecture doc §6).
7. **Vendor coverage for v1 is still undefined.** Three vendors are validated; the actual number Straight Down needs covered before this can fully replace Paula's manual process is unknown. Needs a vendor list from Paula.
8. ~~**The NetSuite least-privilege role can't do PO-number search/lookup**~~ **RESOLVED 2026-08-12.** Root cause: the `PO Update Automation (M2M)` role was missing **`Reports > SuiteAnalytics Workbook`**. Added at **Edit** level and `GET /purchaseOrder?limit=1` returns `200`. **Confirmed sole cause by bisect:** `Lists > Subsidiaries` and `Lists > Accounts` were added in the same batch, then removed, and the call still returns 200 without them.

   **The diagnostic signature, worth recognising on sight:** this one permission gates **all** of
   - record collection `GET` (e.g. `/purchaseOrder?limit=1`),
   - `?q=` filtering on a collection,
   - `/query/v1/suiteql`,

   while **single-record `GET`/`PATCH` by internal id is not gated at all** and keeps working throughout. If you ever see by-id reads and writes succeeding while every list/search/query call returns `400 USER_ERROR "Your current role does not have permission to perform this action"`, check this permission first.

   **Custom list reads need a separate permission: `Setup > Custom Lists` (View).** Three details that cost time to find:
   - It is on the **Setup** subtab, not Lists, despite governing what are called *lists*.
   - It is **not** `Custom Record Entries` — that governs custom *records*, a different thing.
   - Custom lists have **no per-list role restriction**, so this grants read access to every custom list in the account, not just the size list. Worth knowing before granting it in production.

   Unlike the collection failures, this one names itself in the error: `403 INSUFFICIENT_PERMISSION`, *"You need the 'Custom Lists' permission"*.

   **OPEN — least-privilege check before production:** `SuiteAnalytics Workbook` is on at **Edit**. **Whether `View` suffices is untested.** The pipeline only reads, so Edit is broader than needed. Test View and downgrade if it works — before the Phase 4 production cutover, not after.
9. **NEW 2026-08-12 — tranId format transformation (Phase 2 blocker).** Resolving a PO now works, but **not from the number vendors actually print.** Vendors print the bare number; **NetSuite stores the tranId as `PO0001662`.**

   | Query | Result |
   |---|---|
   | `?q=tranId IS "PO0001662"` | `200`, `totalResults=1`, id `8489541` |
   | `?q=tranId IS PO0001662` | `200`, `totalResults=1`, id `8489541` |
   | `?q=tranId IS "1662"` | `200`, `totalResults=0` — executes fine, matches nothing |

   Quoting is optional and **neither form is preferred** — both were confirmed equivalent. The dangerous case is the third: a **successful** response with zero results, which a naive caller reads as "PO not found" rather than "you asked the wrong question".

   **Renderings observed across the eight real documents** — every one carries the bare number, and **not one uses NetSuite's stored form:**

   | Where | Renderings seen |
   |---|---|
   | Document bodies | `PO#1662` · `PO NO : 1720` · `PO NO  :1720` (inconsistent spacing *within one document*) · `PO NO. : 1721` · bare `1720` in a table cell under a `PO NO` header |
   | Filenames | `PO#1721` · `PO1721` · `#1720, 1721` · `^N1720^J 1721` |

   Since all renderings carry the bare number, the transformation is **extract digits → zero-pad → prefix**.

   **BLOCKED on, in order:**
   1. Read **Setup > Company > Auto-Generated Numbers**, Purchase Order row — the actual **Prefix**, **Minimum Digits**, **Allow Override**, and **Use Subsidiary / Use Location** settings.
   2. Validate the derived rule against **several hundred real tranIds**, not one.
   3. Confirm with Paula or Brandon.

   **Do NOT hardcode `"PO"` + 7 digits from the single observed sample.**

   **Robustness requirement:** if **Allow Override** is enabled, tranIds are a *convention, not a guarantee* — someone can type an arbitrary one. The resolver must treat a non-conforming tranId as a **defined outcome** (flag for human resolution), never a crash or a silent wrong match.

   **Extraction risk worth naming now:** the bare-`nnnn` case cannot be found with a page-wide regex. Four-digit numbers also appear in these same documents as carton counts, quantities and style-number fragments. Recognising `1720` as a PO requires the **column-header context** (`PO NO`) — i.e. it is an extraction problem, not a post-processing one.
10. **Paula's mailbox access is decided (direct Graph API) but not yet implemented.**
11. **NetSuite's M2M certificate expires 2028-08-03** — calendar reminder only, no automated alert. Low urgency given the lead time, but worth a real alert once this is hosted on Azure rather than relying on memory.

## 7. Design constraints discovered by testing

These are not open questions — they are settled constraints that later phases must respect. Each was found by measurement, not design review.

**Row identity must be the canonical key, never a digest of the whole row.** Verbatim display text varies between runs *by design*: the extractor may render a colour `NEW INDIGO` on one run and `NEW  INDIGO` on the next, and the pipeline deliberately preserves what was printed rather than rewriting it. In one five-run comparison, 6 of 25 rows differed in displayed text while keys, quantities, counts and row order were identical. A row-digest idempotency check on `proposed_changes` would therefore see spurious changes on every re-parse. Key on the canonical form (`canonical.py`).

**The confidence signal is currently INERT for triage.** `needs_review` came back `True` on all four real documents — including the ones that were subsequently hand-verified as flawless. A flag that always fires carries zero information. Two things follow: Phase 3 must **not** build its review queue on `needs_review` as-is, and the signal is **uncalibrated rather than proven safe** — zero extraction errors were observed across 20/20 hand-verified line items, so the false-negative rate is unmeasured, not zero. Calibrating it needs a corpus with known-bad documents.

**A shipment is not 1:1 with a PO, and the fan-out is larger than assumed.** One Inprotex sheet interleaves **six** POs (1640, 1645, 1650, 1662, 1667, 1704); the Symmetry set spans two (1720, 1721). Consequences for Phase 2/3:
- the `shipments` / `proposed_changes` schema must model one email spanning many POs,
- the Phase 3 **approval unit** must be defined deliberately — per PO, per shipment, or per line — rather than falling out of the implementation,
- **write-back needs partial-failure semantics**: one approval can mean six PO writes, and the fifth can fail. What the audit log records, and what Paula sees, when three succeeded and one didn't, has to be decided before the write path is wired.

**Not yet exercised against a real document:** the merge-note path (`color printed as 'X' and 'Y' in the source`) is covered by unit tests, but in every live run so far each individual run rendered a value consistently — the variation was *between* runs. So that code path has never fired on real input.

## 8. Lessons learned — debugging NetSuite permissions

Written up because the permission investigation cost far more cycles than it should have.

### 1. A permission can read as "tested and eliminated" while never having been applied

In NetSuite's role permissions editor, **selecting a permission and a level does nothing until you click `Add` to push the row into the sublist, and then `Save`.** A selection left sitting in the dropdown looks correct on screen and is silently discarded.

**`SuiteAnalytics Workbook` sat at `None` through five probe cycles that all believed it was on.** Every probe returned a byte-identical error, which was reported as "this permission is not the cause" — and was wrong, because the permission had never been applied. That produced a **false elimination** and sent the investigation after `Find Transaction`, `Perform Search`, and the `Web Services Only Role` checkbox.

Second-order damage worth noting: **those earlier eliminations are themselves not sound.** `Web Services Only Role` was verified in the UI, so that one holds. `Find Transaction` and `Perform Search` were not — they may have been silently discarded the same way. Once one save is found to have failed silently, every earlier elimination in that run becomes suspect.

**First move for any future NetSuite permission problem:**

> **Setup > Users/Roles > Show Role Differences** — Base Role = `Administrator`, Compare To = the target role, check **Only Show Differences**, export CSV.

It shows **actual** state, not intended state, and it found in about 30 seconds what five probe cycles missed. Requires `Bulk Manage Roles` on the admin's own role. Run it *before* probing, and again after any change you are about to draw a conclusion from. The REST API cannot substitute: a role cannot read its own permission list.

### 2. Batch candidates when searching; isolate only when confirming

Probing upward one permission at a time from a broken state is O(N) in save-and-verify cycles, and most cycles return zero information. Add several plausible candidates at once to establish *whether* the problem is permissions at all, then bisect to find *which*. That is exactly how the real cause was finally pinned: a batch, then removals proving the others irrelevant.

### 3. Tripwire: two consecutive changes producing byte-identical errors

Identical output across a supposedly-changed system is evidence **the change did not land**, not evidence about the system. That signal appeared repeatedly here and was read as information about NetSuite when it was information about the save workflow. Stop probing and change method.

### 4. Assert on intent, not on incidental behaviour

Three tests in this codebase asserted incidental behaviour and had to be rewritten when *correct* changes landed:

- an **index-based lookup** (`result.lines[2]`) that passed only because row ordering happened to cooperate — deterministic sorting moved the index and exposed that it was never testing what it appeared to test;
- a **`BLACK` / `black` separation** asserting the two must not merge, which protected nothing: the matcher already compared colour case-insensitively, so keeping them apart only ever produced two proposed changes for one NetSuite line.

Neither was a regression; both were tests encoding accidents. Assert on **identity and intent** — "the row with an empty PO number", "the write contains only quantity" — not on position or incidental state. A test that breaks when a correct change lands is a cost, not a safety net.

## 9. How to recover when something breaks

| Symptom | Likely cause | What to do |
|---|---|---|
| NetSuite auth fails (`invalid_client`, `invalid_grant`) | Certificate/role mapping issue, or a sandbox refresh wiped the Integration record | See `NETSUITE-M2M-SETUP.md` troubleshooting table — sandbox refreshes wipe Integration records and cert mappings; redo Steps 1-5, the keypair itself stays valid |
| NetSuite write returns 400 with `USER_ERROR` text | Permission problem, not a generic bad request — NetSuite doesn't reliably use 403 for these | Check the role's permissions; don't assume it's a malformed request |
| Anthropic API calls fail or stop working | Key revoked/expired, or spend limit hit | Check console.anthropic.com under the Straight Down org, Settings -> API Keys and -> Billing |
| A vendor's file produces low-confidence or no extraction | New/unfamiliar layout, or a corrupted/encrypted file | Check the flagged reason in the output — the system is designed to flag rather than guess, so a flag here is expected behavior, not necessarily a bug |
| Nothing has run in a while and you're not sure if it's broken or just quiet | No monitoring exists yet (this is flagged in the build plan as a hard requirement before production) | Currently must be checked manually — this is exactly the gap Phase 4's monitoring work is meant to close |

## 10. Access and ownership inventory

| What | Where | Who has it |
|---|---|---|
| NetSuite sandbox admin access | NetSuite `1321665-sb2` | Kiko, plus whoever has admin in that account |
| NetSuite "PO Update Automation (M2M)" role | Attached to Kiko's own employee record | Kiko only — see §6 item 6 |
| Anthropic Console org | console.anthropic.com | Whoever set up the Straight Down org — confirm who has admin/billing access |
| Azure subscription | Not yet used by this project | Brandon (assumed) — confirm before Phase 2 infrastructure work starts |
| This project's code and docs | `PO Agent` OneDrive folder | Anyone with folder access |
| Private key / API key | Kiko's local machine only, outside the synced folder | Kiko only — this is the single point of failure described in §6 item 6 |

## 11. What's left to build

See `PO-Update-Automation-Build-Plan.md` for full phase detail. Short version: Phase 1 (NetSuite proof-of-concept) is done. The parsing/matching layer under Phase 1's extended scope is done and validated against 3 real vendors. Phase 2 (email intake, database, Azure infrastructure) has not started. Phase 3 (review/approval UI, write-back wiring) has not started. Phase 4 (production cutover, monitoring) has not started.
