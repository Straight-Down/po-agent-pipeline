# PO Update Automation — Handoff Runbook

**Purpose:** so Brandon (or anyone else) can understand, operate, and fix this system without Kiko in the room — per Beth's discovery follow-up (2026-08-05), condition of moving past sandbox testing.

**Audience:** assumes general technical competence, no prior context on this specific project. Where more depth exists elsewhere, this doc points to it rather than repeating it — `PO-Update-Automation-Architecture.md` is the full design rationale; this doc is the "how do I actually run/fix/hand this off" companion.

**Last updated:** 2026-09-01

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

1. **Intake** (not yet built) will read new emails via Microsoft Graph API from a **new shared mailbox** (`shipments@`), app-only `Mail.Read` scoped to that one mailbox via RBAC for Applications. **This reverses the 2026-08-10 "direct access to Paula's inbox" decision** — `Mail.Read` is mailbox-level, not folder-level, so that route would have exposed her whole inbox. See §6 item 11.
2. **Attachment triage** (`attachment_classifier.py`) looks at every attachment in the email and decides what it is — packing list, invoice, payment request, inspection report, shipping schedule. **Filenames are not trusted for this** — one real vendor's invoice was literally named "...PACKING LIST.pdf." Classification looks at document content instead (does this sheet/page actually break quantities out by size). Only the packing list gets parsed for shipment data; everything else is set aside.
3. **Parsing** (`document_parsers.py`, `claude_extractor.py`, `parse_packing_slip.py`) extracts PO number, style, color, size, and quantity from the packing list.
   - If the file is a known, previously-validated format (currently just Inprotex), a fast deterministic parser handles it for free.
   - Otherwise, the Anthropic API reads the document's actual structure (not just an image) and returns the same structured fields. This is the primary path for essentially all vendors, since every vendor's layout is different.
   - Anything the extractor isn't confident about gets flagged for manual review rather than guessed.
4. **Matching** (`matcher.py`) takes those parsed lines and looks up the real NetSuite PO. It matches to the exact PO line using NetSuite's own custom fields (`custcol_sd_tmpl_style`, `custcol_product_color.refName`, `custcol_product_size.refName`), not by parsing the item's display name. Size labels get normalized first (e.g., vendor's "XXL" -> NetSuite's "2X") via `SIZE_ALIASES`. **Colour** is matched by code first; if the vendor printed a name instead (`NEW INDIGO` against NetSuite's `NIN`), it is resolved through the long-form name on the child item — **scoped to the colours on that PO only**, never a global table, and flagged rather than guessed if two colours on one PO could both be meant (§6 item 12).
5. A PO line that doesn't appear in a given shipment's packing list is left alone entirely — no record, no flag. Paula confirmed POs routinely ship in batches, so this is the normal case, not an error.

6. **Persistence** (`ingest.py`, built 2026-08-26) writes the whole shipment into the database in one transaction: the intake event, its source documents and their roles, one row per PO, one `proposed_changes` row per extracted line in whatever state the matcher assigned, candidate rows where a key matched several open lines, and an `audit_log` entry. Re-ingesting the same document is a no-op — content dedup is checked before the extractor runs, so a re-forward costs nothing. Schema and reasoning: `PO-Update-Automation-Schema-Rationale.md`.

## 4. How the diff/approval logic works

`matcher.py`'s `ProposedChange` represents one line's proposed update. The rules baked into it come directly from Paula, not from assumptions:

- **Quantity**: the packing list's shipped quantity **replaces** the PO line's current quantity. Shipping more than was ordered is normal and accepted — it does not get flagged as unusual.
- **Receipt dates are never computed or proposed by this system.** Paula determines the actual receipt date herself, using her own knowledge of customs/trucking buffers — she explicitly does not use the vendor's stated arrival date. Enforced structurally: `ProposedChange` has no `proposed_expected_receipt_date` field at all. The vendor's ETD/ETA are still shown as labeled reference information, but `to_netsuite_fields(include_dates=True)` will raise `DateNotConfirmed` until a human calls `confirm_receipt_date()`. Quantity-only writes are unaffected by this and work normally.
- **Inspection reports (QC documents) are never a data source**, even on the rare occasion one contains data the packing list lacks. This is enforced in code — `parse_shipment_documents` raises `ExtractionError` if handed an inspection report.
- **A vendor's packing list that can't be resolved to individual size-level lines results in a manual-entry flag**, not a guess (no proportional splitting, no inference from another document).

**Not yet confirmed:** if a single PO ships in two genuinely separate batches weeks apart (not just multiple styles on one PO), does the second batch's quantity replace what's in NetSuite, or add to it? The code currently replaces. Low urgency, worth asking Paula before this goes further.

**One vendor line can match several NetSuite lines**, because `(PO, style, colour, size)` is not unique per PO line. One open line among them is targeted normally; several open lines produce `NEEDS_RESOLUTION` with every candidate's figures attached and **no** automatic choice. Full evidence and reasoning in §6 item 10 — including why NetSuite-side duplicates must never be summed while extraction-side duplicates must.

**Dates are written as all three fields together**, same value: `expectedReceiptDate`, `custcol_override_expected_receipt = true`, `custcol_sd_updatedreceiptdate`. Tested 2026-08-12 — NetSuite does **not** derive `expectedReceiptDate` from the override pair (architecture doc §6), so omitting it would leave the field NetSuite actually schedules against stale.

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

## 6. Known limitations and open risks (as of 2026-08-31)

Ranked by how much they matter. Items struck through are resolved, with the resolution recorded in place rather than deleted — the reasoning trail is the point.

1. ~~**A real vendor document with actual banking details (account number, SWIFT code) is sitting in the project folder as a test fixture.**~~ **FIXED 2026-08-11.** The real file was moved to `%USERPROFILE%\.po-agent\vendor-documents-private\` (outside OneDrive, ACL-locked to Kiko only, same treatment as the private key and API key) and is no longer referenced by any test. Tests now use `fixtures/SD Vendor Payment Request SAMPLE (synthetic).pdf` — identical "Request for Payment" structure and field layout, entirely invented bank, account number, SWIFT and recipient. Regenerate with `python make_test_fixtures.py`. The real filename no longer appears in any `.py` file either.

   **SECOND INSTANCE, 2026-09-02 — caught before any commit, and it establishes the pattern.** Paula sent two new vendor document sets (six files). Four carried third-party data and were moved to the same private directory, read-only, before being staged: the Tainan **commercial invoice** (a US retailer as consignee and buyer, a MID code, the factory's name and address, the vendor's own bank, account number and SWIFT, and a separate Mexico consignee on a third tab), the footwear **Clearance Invoice** and **Payment invoice** (the freight agent's bank, account number and SWIFT), and the **ocean bill of lading** (forwarder and consignee addresses). **The set carries TWO distinct banks, not one** — the vendor's own on the Tainan invoice, the freight agent's on both footwear invoices. Confirmed absent from history, index and stashes first — `git log --all --name-only` returned nothing for any of the six.

   **The pattern, which is the reusable part: it is the document SET that leaks, not the packing list.** A vendor sends the packing list with an invoice, a payment request and a bill of lading attached. The packing list is the only file the pipeline needs, and in every case so far it has been clean; the attachments beside it carry bank accounts, SWIFT codes, MID codes and *other customers'* names. So the default has to be: **admit the packing list, exclude the rest, and check per file rather than per set.**

   **A workbook is not safe just because one of its sheets is a packing list.** The footwear `Clearance Invoice.xlsx` carried the bank details on its `COMMERCIAL INVOICE` tab while its three `PO-1624 …` tabs — the only size-level source for that PO — were clean. It was split rather than kept or discarded whole: the original moved out, and `FW26 footwear PO-1624 packing sheets (invoice sheet removed).xlsx` stays in the repo with the invoice tab deleted and the size grid verified intact. Scan sheet by sheet, not file by file.

   **`.gitignore` now nets the invoice-shaped members of a set** (`/*COMMERCIAL INVOICE*.xls[x]`, `/*Clearance Invoice*.xls[x]`, `/*Payment invoice*.xls[x]`, `/*HBL*.pdf`, `/*BILL OF LADING*.pdf`), verified with `git check-ignore -v` in both directions — each moved file would now be ignored, and the four files we keep are not. That is a net for the next arrival, not a remedy for these; the remedy was moving them before they were ever added.

   **THE GRAPH PRIVATE KEY GETS THE SAME TREATMENT, 2026-09-09.** `C:\dev\po-agent-secrets\po-agent-graph.key` — outside git, outside OneDrive, inheritance stripped and granted to the current user only:

   ```powershell
   icacls C:\dev\po-agent-secrets /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F"
   ```

   Recorded here so both private keys in this project have **one documented standard**, rather than one being protected and the other being an exception nobody wrote down. The public certificate (`po-agent-graph.cer`) sits beside it for convenience and is **not** secret — it was uploaded to Entra, so Microsoft holds it and anyone who can read the app registration can fetch it. `*.key`, `*.cer`, `*.crt`, `*.der`, `*.pem` and `*.pfx` are all gitignored as a second line, but the first line is the key not being in the tree at all. Full detail in `GRAPH-SETUP.md`.

   **Do not verify that ACL from Git Bash** — `ls -l` misreports it, see §7.

   **Already in history, NOT acted on:** `SD #1720, 1721 INVOICE, PACKING LIST.pdf` is committed and pushed, and it carries a MID code, consignee details and an L/C issuing bank field. It is a Symmetry customs invoice that happens also to be a packing list, which is why it came in. **A history rewrite has not been attempted** — the repo is pushed and shared, so that is Kiko's call, not a cleanup to perform quietly. The mitigation meanwhile is the one already in place: the remote is private. Flagged here so the decision is visible rather than forgotten.
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

   **Targeted ask for Paula — ANSWERED 2026-08-24, and the answer rules out an approach.** Paula: **size scale is a property of the STYLE, not of the vendor** — the same vendor ships numerically-sized pants and letter-sized jackets, sometimes on the same PO. So size interpretation must **resolve per line**, against the style's own size run (`custcol_sd_tmpl_size_run`), and **cannot** be driven by a per-vendor profile. Any design that keys size handling off "which vendor sent this" is wrong before it is written. She is sending packing-slip examples for numerically-sized styles; until one arrives the 39 non-letter values stay at `NEEDS_ATTENTION`.

   **A NetSuite data gap found while asking, for whoever maintains the size list:** the women's numeric group in `customlist_psgss_product_size` holds only **`0`, `2`, `4`**, while Paula described the scale as "2, 4, 6, etc". If vendors ship 6 and up, **those values do not exist in the list yet** — so the item records could not be created for them, let alone matched. That is a NetSuite data problem upstream of this pipeline, not something the matcher can normalize around.

   **`ALL` — which field REST returns, read live 2026-08-24 (PO0001649 / A320001 / WHT):** `custcol_product_size` comes back as `{"id": "45", "refName": "ALL"}`. **REST's `refName` is the list value's NAME, not its abbreviation.** The list record itself holds `name = "ALL"`, `abbreviation = "A"`, so the two really are different and REST hands over the name. `matcher.py` compares `refName`, so it compares against `"ALL"` — the deliberate choice §6 called for, now made and recorded rather than inherited by accident. **The open half:** nothing yet says which form a *vendor* prints. If one prints `A`, or `OS`, or `ONE SIZE`, it will not match, and the fix is a `SIZE_ALIASES` entry rather than switching which NetSuite field is read — switching would break every other value, where name and abbreviation are identical. No vendor sample in the corpus contains a size-`ALL` line, so this is unmeasured, not safe.

   **Also recorded:** Inprotex uses `2XL`, `XXL` **and** `XXXL` in the *same file* for what NetSuite stores as `2X`/`3X` — three conventions from one vendor in one document. `SIZE_ALIASES` already covers all three.
5. ~~**No handling yet for a corrupt or password-protected vendor file.**~~ **FIXED 2026-08-11.** All `openpyxl.load_workbook` / `pdfplumber.open` calls now go through `claude_extractor.open_workbook` / `open_pdf`, which raise `DocumentUnreadable` with a specific reason (truncated/corrupt zip, encrypted/password-protected, empty, wrong format, OS permission). Batch behaviour is the point: `build_source_documents` and attachment triage **flag the individual bad attachment and continue processing the rest** rather than aborting the shipment, and triage reports "could not open: &lt;reason&gt;" as its own condition, distinct from "has no size data". Two new fixtures cover it (`fixtures/corrupt_truncated.xlsx`, `fixtures/encrypted_password_protected.pdf`).
6. **This entire system currently only runs from Kiko's laptop, using his own NetSuite employee record.** Not solved by this runbook — solved by the pending Azure migration (Key Vault, Function App) and, if audit clarity becomes important later, a dedicated NetSuite service account (currently a deliberate cost trade-off, documented in the architecture doc §6).
7. **Vendor coverage for v1 is still undefined.** Three vendors are validated; the actual number Straight Down needs covered before this can fully replace Paula's manual process is unknown. Needs a vendor list from Paula.
8. ~~**The NetSuite least-privilege role can't do PO-number search/lookup**~~ **RESOLVED 2026-08-12.** Root cause: the `PO Update Automation (M2M)` role was missing **`Reports > SuiteAnalytics Workbook`**. Added at **Edit** level — which is the **only** level this permission offers, not a choice (see the closed sub-item below) — and `GET /purchaseOrder?limit=1` returns `200`. **Confirmed sole cause by bisect:** `Lists > Subsidiaries` and `Lists > Accounts` were added in the same batch, then removed, and the call still returns 200 without them.

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

   ~~**OPEN — least-privilege check before production:** whether `View` suffices is untested.~~ **RESOLVED 2026-09-09 — there is nothing to narrow. `Reports > SuiteAnalytics Workbook` offers ONLY `Edit` in the role editor's level dropdown; `View` is not an available option for this permission.** So `Edit` is the **minimum NetSuite permits**, not a least-privilege compromise anyone accepted. Oracle's own documentation corroborates: it says *"set the access to Edit"* rather than describing a range of levels.

   **Do not re-open this.** The instinct is right — the pipeline only reads, so `Edit` looks broader than needed — and that is exactly why it invites a second investigation. The permission has no narrower level to grant. A planned retest was cancelled for this reason rather than run.
9. ~~**NEW 2026-08-12 — tranId format transformation (Phase 2 blocker).**~~ **RESOLVED 2026-08-31 (change 8).** The rule is now in the pipeline and validated against every PO in the account. Vendors print the bare number; **NetSuite stores the tranId as `PO0001662`.**

   **The rule, from Setup > Company > Auto-Generated Numbers (Purchase Order row):** Prefix `PO`, Minimum Digits `7`, Current Number `1777`. So `tranId = "PO" + str(number).zfill(7)`.

   **Validated against the data, not the checkbox.** Allow Override and Use Subsidiary / Use Location were never captured — and a checkbox says what is *permitted*, not what *happened*. So every PO tranId in the account was queried:

   | Measure | Result |
   |---|---|
   | POs examined | **1,659** (all of them) |
   | match `^PO\d{7}$` exactly | **1,659 — 100.00%** |
   | non-conforming | **0** |
   | distinct shapes | **1** (`PO#######`) |
   | date span | 2021-06-16 → 2026-07-31 |
   | duplicate numbers | none |
   | `"PO" + zfill(7)` round-trip failures | **0** |

   **The finding:** override is off or unused, and there is no subsidiary or location variation in practice. Not one legacy or hand-keyed value exists, across five years and every status (959 Fully Billed, 562 Closed, 89 Pending Receipt, and the rest) — so this is not a case of the convention holding only for recent records. 112 numbers are missing from the sequence, which is ordinary deletion, not a second format.

   **Implemented as** `netsuite_client.po_tranid` (`1662`, `PO#1662`, `PO NO : 1720` and `PO0001662` all resolve; idempotent), applied inside `resolve_po_internal_id`. It used to live only in a report script while the pipeline default was an untransformed lookup that always failed — that seam is gone.

   **Non-conforming input is a defined outcome, never a second guess.** A reference with no digits, or naming two POs (`#1720, 1721`), is refused **before any request** — splitting that is the extractor's job, since each line carries its own `po_number`, and picking one would attach a whole shipment to the wrong order. A derived tranId that does not exist gives `resolution_status = 'NOT_FOUND'` with **both** the printed value and the attempted tranId recorded; there is no fuzzy fallback, because the rule reproduces all 1,659 existing tranIds and a miss therefore means the PO is absent, not that the format is wrong.

   **The extraction boundary still stands** (`assert_po_reference` states it): a bare four-digit number cannot be found with a page-wide regex, because carton counts, quantities and style fragments look identical. Recognising `1720` as a PO needs the column-header context (`PO NO`) — that is an extraction problem, and nothing in the resolver can recover from being handed a carton count.

   **Historical detail, kept because it is the reason the transformation had to be exact:**

   | Query | Result |
   |---|---|
   | `?q=tranId IS "PO0001662"` | `200`, `totalResults=1`, id `8489541` |
   | `?q=tranId IS PO0001662` | `200`, `totalResults=1`, id `8489541` |
   | `?q=tranId IS "1662"` | `200`, `totalResults=0` — executes fine, matches nothing |

   Quoting is optional and **neither form is preferred** — both were confirmed equivalent, and `last_lookup_strategy` still records which one answered. The dangerous case is the third: a **successful** response with zero results, which a naive caller reads as "PO not found" rather than "you asked the wrong question".

   **Renderings observed across the eight real documents** — every one carries the bare number, and **not one uses NetSuite's stored form:**

   | Where | Renderings seen |
   |---|---|
   | Document bodies | `PO#1662` · `PO NO : 1720` · `PO NO  :1720` (inconsistent spacing *within one document*) · `PO NO. : 1721` · bare `1720` in a table cell under a `PO NO` header |
   | Filenames | `PO#1721` · `PO1721` · `#1720, 1721` · `^N1720^J 1721` |

   Since all renderings carry the bare number, the transformation is **extract digits → zero-pad → prefix**.

   ~~**BLOCKED on:** read the numbering setup; validate against several hundred real tranIds; confirm with Paula or Brandon.~~ **All three done** — the setup was read, the rule was validated against 1,659 tranIds rather than several hundred, and the data made the confirmation unnecessary.

   **The robustness requirement still holds even at 100% conformance.** Allow Override was never observed in the *setup*, only in the *data*: if it is enabled, tranIds remain a convention rather than a guarantee, and someone could type an arbitrary one tomorrow. So `TRANID_PATTERN` stays in the code as the check, a non-conforming value stays a flagged outcome, and this is worth re-running against production before cutover (Phase 4) — the two accounts have already been shown to differ elsewhere.

   **Extraction risk worth naming now:** the bare-`nnnn` case cannot be found with a page-wide regex. Four-digit numbers also appear in these same documents as carton counts, quantities and style-number fragments. Recognising `1720` as a PO requires the **column-header context** (`PO NO`) — i.e. it is an extraction problem, not a post-processing one.
10. **NEW 2026-08-24 — `(PO, style, colour, size)` is NOT unique per NetSuite PO line.** The matcher's key can resolve to several lines. This was found by measurement, not review, and it is the assumption the whole matching design rested on.

    **The evidence:**

    | Measure | Value |
    |---|---|
    | POs carrying duplicate-key lines | **64 of 1,659 (3.9%)**, 451 surplus lines |
    | Pending Receipt POs with duplicates | **0 of 89** |
    | Partially Received POs with duplicates | 4 of 17 (24%) |

    The **0 of 89** is the well-powered result and the important one: **these lines are created during receiving, not at PO entry.** Treat the 24% as directional only (n=17). The consequence is that this pipeline meets them **disproportionately** — a second packing slip landing against a partially-received PO is precisely the case the tool exists for, so its exposure is much higher than the 3.9% headline.

    **On the live population** — the 5 POs a packing slip could still act on — there were **25 duplicate groups: 24 with exactly one open line, 1 with two.** Every live group had at least one open line.

    **No single field discriminates the pair.** Across 435 pairs, two rough families: **date-driven (233)** and **non-date (202)**, the latter differing on `rate`, `description` or the RepSpark flag. That `rate` differs in **21%** matters: those pairs are separate commercial transactions at different prices, not a split of one order. And `custcol_sd_fg_excluderepspark` differs in only **25.5%** — it was **not** the discriminator, and it differs because a human sets it case by case. So "what is the second line?" has more than one answer, and no rule can be derived from the field data alone.

    **The resolution rule as built (change 5, commit `40168d2`):** gather **all** matching lines → filter to `isOpen` → **exactly one** open line: target it → **zero**: flagged, no write → **two or more**: `NEEDS_RESOLUTION`, carrying every candidate's quantity, received, billed, dates and rate so a human decides with the facts in front of them. `LineAmbiguous` makes the refusal structural — `to_netsuite_fields()` cannot build a write for an unresolved change.

    **No tiebreaker, ever.** `quantityReceived` looks like it would resolve the one live ambiguous case (50 units received 0 versus 200 units received 100) and it probably would — but that is **n=1**, and a wrong automatic pick **fails silently**: the wrong line is updated and the right one goes stale with nothing to notice. Surface the receipt figures for the human; never branch on them.

    **The two problems that share a symptom and need opposite fixes** — this is the distinction to hold on to:

    | | Extraction-side duplicates | NetSuite-side duplicates |
    |---|---|---|
    | What it is | one key across several carton rows in one document | one key, several PO lines |
    | Correct handling | **SUM them** (change 2) | **NEVER sum them** (change 5) |
    | Where | `extraction_schema.aggregate_lines` | `matcher._resolve_target_line` |

    Both look like "the same style/colour/size twice". Applying either fix to the other's case is a silent data error: summing PO lines would write 250 where the answer is 50 or 200; refusing to sum carton rows would write one carton's quantity as the whole shipment. A test asserts both halves together so neither drifts into the other.

    **OPEN — needs Paula:** what *is* the second line on `PO0001649` (`A320001`/`WHT`/`ALL`, 50 received 0 versus 200 received 100, both open, both due 2026-07-01)? Her answer is the only way to learn whether a rule exists. Who creates these lines and why is **out of scope for this tool** either way — it never creates PO lines.
11. **Paula's mailbox access — DECISION CHANGED 2026-08-24. It is a new shared mailbox, not her inbox.** The earlier decision ("direct Graph API access to Paula's own mailbox", 2026-08-10) is **superseded**, and for a reason worth keeping: **Graph's `Mail.Read` application permission is mailbox-level, not folder-level.** There is no way to grant an app-only application access to one folder of a person's mailbox — granting it against Paula's account would expose her entire inbox to the service. Her inbox was therefore **explicitly rejected as the target**.

    **What to build instead:** a new shared mailbox (`shipments@`), with app-only `Mail.Read` scoped to just that mailbox via **RBAC for Applications** — the mechanism that makes "this app, this one mailbox" expressible at all. Vendors are redirected there, or Paula forwards into it.

    **Two different admin roles are involved, which is what makes this a scheduling problem rather than a task:** the app registration and tenant-wide admin consent need **Entra ID** admin rights (Application Administrator or Global Administrator); creating the shared mailbox and the RBAC-for-Applications scope needs **Exchange** admin rights. Confirm both exist, with names attached, before Phase 2 starts — missing admin access has already blocked this project three times (Integration record, then role permissions, twice).
12. ~~**NEW 2026-08-26 — vendors print colour NAMES; NetSuite stores 3-letter colour CODES. This blocks matching for some vendors entirely.**~~ **RESOLVED 2026-08-26 (change 7).** Kept in full because the correction matters: the original entry said no long-form colour existed *anywhere in the account*. That was too broad. It exists nowhere in the **sandbox colour list** — but it does exist on the **child item record**, and production's colour list `Name` column has it too.

    **The measurement.** 33 real extracted lines from the Legendz xlsx and the Symmetry pair. **4 matched a NetSuite line; 29 did not.** The split is entirely along this axis:

    | Vendor prints | Example | NetSuite has | Matched |
    |---|---|---|---|
    | a colour **code** | Legendz: `MLT`, `DKF` | `MLT`, `DKF` | **yes** — 4 of 4 |
    | a colour **name** | Symmetry: `NEW INDIGO`, `BLACK`, `COCONUT` | `NIN`, `BLK`/`BLC`, `COC` | **no** — 0 of 25 |

    **Where the long name is NOT.** `customlist_psgss_product_color` holds **589 values in which `name` is identical to `abbreviation`** — both the 3-letter code. Verified two ways on the same object: REST `customlist_psgss_product_color/334` returns `name='NIN'`, `abbreviation='NIN'`, and SuiteQL filtered on `id = 334` (no name predicate) agrees. The control is the size list, where the identical REST call returns `name='ALL'`, `abbreviation='A'` — so both fields *are* exposed and the colour data really is code-in-Name. The record type has 13 fields and none of the others holds a name either; `custrecordproduct_color_standard` ("Color Standard") is populated on 21 of 589 values and holds Pantone/Coloro references (`MLT → 'Coloro 122-40-14'`).

    **Where it IS: `custitem_psgss_product_color_desc` on the child item.** `NIN → 'New Indigo'`, `BLK → 'Black'`, `BLC → 'Blackcurrant'`, `COC → 'Coconut'`, `MLT → 'Moonlight'`, `DKF → 'Dark Forest'`. `custitemcolorfamily` is a coarser grouping (Blue, Purple, Neutral) and is not the name.

    **Coverage on the population that matters** — items on **open** POs, since those are the only ones a packing slip can touch (138 open POs of 1,659; 3,677 lines; 2,393 distinct items):

    | Measure | Value |
    |---|---|
    | distinct items with a colour name | **2,390 of 2,393** |
    | the three without | poly mailers and zipper bags — packaging, no colour by nature |
    | distinct colour codes on open POs | 114, **every one of which has a name** |

    **Production and sandbox provably disagree, in both directions.** A UI export of the colour list from another account state has 620 rows with curated long names (`Name='New Indigo'`, `Abbreviation='NIN'`); sandbox has 589 with codes, and for `abbreviation='B'` sandbox's `Name` is `'BLB'` where the export says `'Blue Sky'`. It is the same list — `custrecordproduct_color_standard` matches row for row. But sandbox also holds values the export lacks (id 576 `TAYLOR WHITNEY CABERNET`/`TWC`). **The item field was chosen because it is verified in the account we test against**; building on production's list would mean colour tests that pass in production and fail in sandbox.

    **The resolution rule (change 7):** canonical CODE match first — a code-printing vendor costs no item read at all — then canonical NAME match **scoped to the colours on that PO**, then flag. Never a global map, never fuzzy.

    **Why scoped — and NOT because of collision risk.** That was the original argument and the data does not support it. Measured on items appearing on open POs: exactly **one** name maps to two codes (`'Navy / Silver'` → `NAV` and `NVSL`), five codes have items that disagree on spelling (`FUS`: Fuchsia/Fucshia, `MLK`: MilkShake/Milkshake, `CHC`: Charcoal/Charcoal Heather, `NAV`, `NIN`), and **per PO there is not a single collision across all 133 open POs** (median 3 colours per PO, max 20). A global map would have collided **once**. That is not a reason to build anything.

    The reasons that survive are about maintenance, and they are sufficient on their own:

    - **No seeded table** — nothing to populate by hand across 589 colour values, and no chance of seeding one wrong.
    - **No refresh story.** A cached global map goes stale the moment a colour is added, and colour values *are* still being added (the newest in sandbox was created 2026-06-03). A per-PO lookup is built from live data every time.
    - **No drift** between what a map claims and what the PO actually holds, because the lookup is derived from the PO's own lines.
    - **Coverage is not a differentiator either way:** all 114 codes on open POs have a name, across 2,390 of 2,393 items.

    If two colours on one PO ever *do* collide, the change flags with both candidates and picks neither — that path exists and is tested, it just has never fired on real data.

    **Retracted figure, which should not be requoted:** an earlier probe reported "51 codes carry multiple descriptions" and a value holding `'Black'`, `'INDe'` and `'Indigo'`. That came from joining the colour list to `item.custitem_psgss_product_color`, which is **empty on child matrix items** — an invalid join. The correct pairing takes the code from the PO line (`custcol_product_color`) and the name from the item.

    **Provenance is now persisted** (migration 0002): the method, the printed value looked up, the resolved code, the long name that supplied it, and the item whose record said so. Not reconstructable later — the item read is not stored, and a PO's colour set changes as lines are added or received — so "why did NEW INDIGO become NIN" is a query rather than an archaeology exercise.

    **What NOT to do, still:** fuzzy-match by initials or substring. `BLK`/`BLC`, `COO`/`COC` and `HER`/`H` are all live values, and a wrong colour writes a quantity against the wrong product. Three tests assert those pairs never cross-match.

    **Result on the real corpus:** 29 of 33 lines now match, up from 4. The remaining 4 are the `DFK` lines — see item 13.
13. **NEW 2026-08-26 — the Legendz slip prints both `DKF` and `DFK`, and the extractor read both correctly.** Not an extraction error. The two forms are two different cells on two different rows, transcribed faithfully:

    | Cell | Verbatim | Style on the same row |
    |---|---|---|
    | `D6` | `'DFK'` | `C6 = 'PO#1657，M630018'` |
    | `D15` | `'DKF'` | `C15 = 'PO#1657，M680009'` |

    NetSuite settles which is right: `PO0001657` carries `M630018` in **DKF and MLT**, and `M680009` in **DKF and MLT**. So row 6's `DFK` is a transposition of `DKF`, and `DFK` is not among the 589 colour values at all. A vendor typo, worth raising with Legendz — four lines (148, 205, 188 and 32 units) cannot be matched because of it.

    **The extractor still has zero observed errors** across every real document run so far. Worth stating plainly, because "we extracted both forms" looked at first like the first extraction failure and would have contaminated the calibration corpus if recorded as one.

    **The permanent lesson:** a 3-letter code carries **no redundancy** — `DFK` is exactly as plausible-looking as `DKF`, and nothing in the string reveals the transposition. So a printed code must be **validated against the colour list**, never trusted because it looks like a code. The current behaviour does this correctly by construction: an unknown code matches no line and flags.
14. **NetSuite's M2M certificate expires 2028-08-03** — calendar reminder only, no automated alert. Low urgency given the lead time, but worth a real alert once this is hosted on Azure rather than relying on memory.
15. ~~**NEW 2026-09-02 — a filename could EXCLUDE an attachment, and did: three packing-list sheets were thrown away unopened.**~~ **FIXED 2026-09-02.** `FW26 ... PO-1624 USA -  Clearance Invoice.xlsx` matched the `invoice` filename rule, which was marked unambiguous, so triage never opened it (`method: filename`). Inside were four sheets: `COMMERCIAL INVOICE`, then `PO-1624 20138` / `20139` / `20140`, each headed `Packing list` at H8 with a full per-size grid. That workbook is the only size-level source for PO 1624.

    **This was a regression in judgment, not a missing feature.** The classifier had scored 8/8 earlier *precisely because it opened* a file named `SD #1720, 1721 INVOICE, PACKING LIST.pdf` and correctly called it a commercial invoice. Content inspection is the part that demonstrably works; letting the signal known to be unreliable in both directions short-circuit it inverted the design.

    **The rule now, stated so it cannot drift back:** a filename hint may **prioritise or deprioritise**; it may **never exclude**. Only content, or a file that will not open, excludes. Concretely, in `classify_attachments` every readable attachment goes to the content check, and the name's remaining jobs are to seed a prior (overridden by whatever content says) and to order the survivors in `ClassificationResult.primary` — where a packing-named file wins over an invoice-named one, but the invoice-named one is still selected and still available as a cross-check. `AttachmentClassification.filename_hint` keeps the name's claim as the audit trail for the disagreement.

    **The one exception stands:** the inspection-report ban still short-circuits before any content read. That is keyed on document **type** and is Paula's ruling (2026-08-11), not a filename heuristic — and it is exactly why it does not generalise to the word "invoice".

    Cost of the fix: one preview per readable attachment instead of per suspicious one, in the same single API call. A few thousand tokens against discarding a vendor's only size-level source.
16. ~~**NEW 2026-09-02 — the size-header detector recognised only LETTER sizes, so every numerically-sized sheet looked sizeless.**~~ **FIXED 2026-09-02.** `_find_size_header_row` compared cells against a hand-written set of `XS`…`4XL`. It therefore could not see the footwear sheet's `K28='8'`…`Q28='14'` (strings) or Tainan's `I7=30.0`…`O7=42.0` (floats). Both sheets classified as "packing list but no per-size quantities" and were excluded. **This was the real numeric-size blocker** — canonicalising the token set earlier fixed full-width `２Ｘ` but did nothing for numbers, and item 4 above had already established that **39 of the account's 46 sizes are not letter sizes**.

    A bigger hand-written set would have been the same bug again, so the vocabulary is now **read from the live `customlist_psgss_product_size`** and cached in a generated snapshot: `size_vocabulary.py`, `netsuite_size_list.json` (46 values, `python size_vocabulary.py --refresh` to rewrite, `--check` to report drift without writing). Recognised forms: bare integer strings (`'8'`), integers arriving as floats (`30.0` → the label `30`, **not** `'30.0'`), decimals (`9.5`, `10.5`), and waist-inseam pairs (`32-34`) matched whole against the list rather than parsed.

    **Bare numbers need one test more than letters**, because a row of quantities is also a row of bare numbers. Two things separate them, both measured against the real sheets: **list validity** (Tainan's net-weight row `0.39`…`0.48` yields zero valid labels; the footwear carton row yields two, under the threshold of three) and **monotonicity** (a size scale is printed ascending; Tainan's quantity row `30 | 30 | 3 | 90 | 6` has three valid labels but does not ascend). Monotonicity is required only when *every* hit is a bare number — a row containing `S` or `32-34` has already identified itself, and a descending letter row is still a size row.

    Residual, accepted knowingly: an adversarial row of ascending, all-list-valid quantities would still match. A hit only adds a preview region for the classifier to read, so a false positive costs a few hundred tokens while a false negative sends a whole shipment to manual entry. **`matcher.SIZE_ALIASES` was NOT touched** — every size on both new slips already exists in the NetSuite list, so there is nothing to alias.
17. ~~**NEW 2026-09-02 — legacy `.xls` was unreadable, blocking a vendor entirely on file format.**~~ **FIXED 2026-09-02.** Tainan's packing list begins `d0cf11e0a1b11ae1` — an OLE2/BIFF compound document, not OOXML. openpyxl cannot open one, so it raised `DocumentUnreadable` and triage excluded the file. It is the **only** size-level source for PO 1725.

    `xlrd>=2.0` now reads BIFF (recorded in `requirements.txt`), and **routing is by magic bytes, not by extension** (`claude_extractor.sniff_format`) so a renamed or mis-saved attachment still lands on a reader that can read it. Both readers produce identical `SheetGrid`s, so nothing downstream knows which ran. Two conversions earn that parity: BIFF dates are floats against a workbook epoch (an unconverted cell renders `46244` where the OOXML path renders a real date), and BIFF numbers are all floats (an integral `30.0` renders `30`, which is what the size list holds).

    **A trap worth knowing:** openpyxl validates the **extension** before looking at the file, so a genuine OOXML workbook saved as `.xls` is refused with `InvalidFileException` on the name alone. Signature routing does not hold end-to-end unless the bytes are handed to it directly — `open_workbook` does that for any suffix outside `.xlsx/.xlsm/.xltx/.xltm`. Found by a test, not by review.

    `open_workbook` itself stays openpyxl-only on purpose: its callers use the real openpyxl object, and imitating that API over xlrd is a lot of surface for one vendor. `read_workbook_grids` is the format-agnostic entry point, and it is what the classifier and the Claude extractor use.
18. ~~**NEW 2026-09-02 — `shipment_pos.po_number_key` was not canonical, so the same document ingested differently run to run.**~~ **FIXED 2026-09-02.** One extraction of the footwear workbook returned `'1624'` for two of its three sheets and `'PO0001624'` for the third, and which sheet got which **varied between runs**. `po_tranid` normalises both for lookup, so PO *resolution* never noticed — but the raw strings were also dict keys and were written to the database, so one PO became **two `shipment_pos` rows**, its lines split across two parents, and the PO was read from NetSuite twice. Same class as the `NEW  INDIGO` double-space nondeterminism (§7, "Row identity must be the canonical key").

    `netsuite_client.po_number_key` is now the single grouping and storage key (`'1624'`), derived **through `po_tranid`** so there is one digit-extraction rule rather than two that can drift. Applied in `ingest` (grouping, colour lookups, the DB write) and in `matcher.build_proposed_changes` (so the matcher's `colour_lookups` keys line up with what ingest stores). A reference `po_tranid` refuses — no digits, or more than one distinct number — falls back to the canonical form of the text: deterministic, without inventing a PO number, and resolution still reports `NOT_FOUND`.

    **A second, quieter instance of the same bug in the same row**, found by the test rather than by inspection: `po_number_printed` took the *first* matching line's rendering, which is also whichever came first that run. It now stores **every distinct rendering, sorted** (`1624 / PO0001624`) — deterministic, and more truthful, since the column exists so a reviewer sees what the vendor wrote and the vendor wrote two things.
19. ~~**NEW 2026-09-02 — the size can be split across TWO axes, and reading either alone collapses two real sizes into one.**~~ **FIXED 2026-09-02.** Tainan's sheet `ACT` puts the **waist** across the column headers (`I7=30.0` … `O7=42.0`, floats) and the **inseam** in a row-block label several rows above the rows it governs (`H11='INS 32'`, then `H24='INS 34'`). The same column therefore means a different size in each block: column J beneath `INS 32` is `32-32`, and beneath `INS 34` it is `32-34`. NetSuite's PO 1725 keys its 28 lines on exactly those pairs, so **neither axis identifies a size on its own** and the pair appears in no single cell.

    Before this, every line came out keyed on the waist alone with the two inseams **summed** — `22` where NetSuite holds `16` and `4` — and none of the 29 extracted lines matched any of the PO's 28.

    **This was never a parser failure.** The extractor read the structure correctly and said so unprompted: *"Each colour is shipped in two inseam lengths (INS 32 and INS 34 carton sections, with a separate recap block for each), but the printed size labels are…"*, and it warned that the lines would look like duplicates. What it lacked was permission to combine the axes and knowledge of the target spelling. So the fix is a prompt change plus a constraint, not a new parser:

    - **`PACKING_SYSTEM_PROMPT` rule 8** (and rule 9 on the multi-doc prompt) describes the layout and asks for the combination, one line per combination.
    - **The account's 46 size values are shown to the model** as a second, cached system block (`claude_extractor.size_vocabulary_block`), so `30-32` versus `30/32` is a lookup rather than an invention. Deliberately a separate block from the pinned prompt text: a `size_vocabulary.py --refresh` would otherwise change `prompt_fingerprint()` and look like a forgotten `PROMPT_VERSION` bump.
    - **The guarantee is in code, not in the prompt.** `extraction_schema.enforce_size_composition` accepts a composed size **only** if it exists in `customlist_psgss_product_size`. "This is a real size in this ERP" is a checkable claim, and anything checkable should not rest on a generation.

    **What a rejection does, and does not do.** It flags: confidence forced to `low`, both printed axes preserved, and the size left **exactly as emitted** — not blanked, not rewritten. **No separator is ever guessed.** A `30/32` against a list holding `30-32` flags rather than being "corrected", because a separator rule inferred from one document would be applied silently to the next, and the account's own spelling is the only authority on how a size is written.

    **The gate is the SECOND axis, and that is what keeps it inert elsewhere.** A row counts as composed only when it declares `size_axis_secondary`. Four of the five corpus vendors compose nothing and are untouched — which matters more than it sounds: Inprotex prints `XXL`, which is **not** in the account's list at all (NetSuite spells it `2X`), so validating single-axis sizes here would break a vendor that has been correct for months. `matcher.SIZE_ALIASES` still owns vendor-label-to-NetSuite mapping and was not touched.

    Provenance is persisted (migration 0003: `size_composition_method`, `src_size_axis_primary`, `src_size_axis_secondary`, with a check constraint that a composition must carry **both** axes). Same reasoning as the colour columns: `32-34` on its own does not say which column header and which block label produced it, or that the vendor never wrote `32-34` anywhere.

    **Result, live against sandbox 2026-09-02.** 56 lines, **56 of 56 composed and all 56 accepted** — zero rejections — producing exactly the 14 real pairs. **All 28 lines of the authoritative `ACT` sheet matched their NetSuite line 1:1 and correctly** (`30-32`→line 1, `30-34`→line 8, `32-32`→line 2, `32-34`→line 9), up from **0 of 29**. Colour resolved alongside (`NIN`→New Indigo, `SLV`→Silver) on 2 item reads.

    Worth recording how the model handled a wrinkle nobody had specified: **the recap blocks are not labelled with their inseam.** `INS 32` sits at `H11` and `INS 34` at `H24`, governing the *carton* sections; the recap tables at rows 46–49 and 52–55 carry no inseam label at all. The model attributed each recap to an inseam by matching its `ACTUAL` row against the carton rows, and said so: *"Recap block at rows 46-49 is unlabelled for inseam; identified as INS 32 because its ACTUAL row matches the INS 32 carton rows exactly"* — then marked those lines `medium`, which is the correct call.

    **So the 28 matched lines are still `NEEDS_ATTENTION`, and the reason is `extraction confidence medium` — nothing to do with size.** That is the already-documented §7 finding ("the confidence signal is INERT for triage") firing again, this time for a defensible cause. Sizes are solved; the flag is the known uncalibrated signal, and these 28 lines are good calibration rows: a correct match, a stated reason, and a human verdict still to record.
20. ~~**NEW 2026-09-02 — the Tainan file's two sheets, `ACT` and `REV`, are two versions of ONE shipment.**~~ **RESOLVED 2026-09-02, by comparing the numbers rather than the structure.** They are not two versions of one document; they are two **different kinds** of document. `ACT` is the packing **RECORD**. `REV` is an 8%-target **PLAN**.

    **The deciding evidence is that `REV`'s "ACTUAL" figures are calculated.** `REV` carries fractional rows the `ACT` sheet does not have at all:

    ```
    REV R45  INS 32   17.28  91.80  90.72  71.28  28.08  20.52  4.32   = 324
    REV R51  INS 34    4.32  14.04  22.68  38.88  16.20   9.72  2.16   = 108
    ```

    Those are **`ORDER x 1.08`, with the arithmetic left on the sheet**, and `REV`'s ACTUAL row is that row **rounded** — 7 of 7 cells on both NEW INDIGO blocks (5/7 and 6/7 on SILVER, the deviations adjusting the totals down). `ACT`'s ACTUAL matches the same uplift on only 2/7, 3/7, 2/7 and 4/7 — no better than chance for small integers — and reconciles instead with its own irregular carton detail. **An "actual" equal to order x 1.08 is a plan; an "actual" that is not derivable from the order is a count.**

    | ACTUAL vs `round(ORDER x 1.08)` | NIN INS32 | NIN INS34 | SLV INS32 | SLV INS34 |
    |---|---|---|---|---|
    | **REV** | **7/7** | **7/7** | 5/7 | 6/7 |
    | **ACT** | 2/7 | 3/7 | 2/7 | 4/7 |

    **What the two sheets agree and disagree on.** Every one of the 28 `ORDER` cells is **identical** across both sheets, totalling 800 on each — and both reproduce PO 1725's 28 NetSuite lines exactly (NIN `30-32`=16, `32-32`=85 … SLV `42-34`=2). The `ACTUAL` figures differ in **18 of 28 cells**: `ACT` 865 pcs in 34 cartons (+8.125%), `REV` 860 in 33 (+7.5%), **net −5 pieces**. So whatever `REV` revised, it was not the order.

    **RECENCY IS MOOT, and that is why this is resolved rather than waiting on Paula.** You do not post a plan, whenever it was written. A newer plan does not override an actual count — the two sheets are different kinds of document, not two drafts of one, so "which is newer" is the wrong question. (For the record, nothing in the file answers it anyway: both headers read `2026-08-10` on all four pages, there is no version cell, and the only `REV` text is the sheet's own label at `D40`/`D114`. The filename's `Correction on Aug.10 from Aug.07` refers to *file* versions.)

    Supporting detail, all consistent with a plan: `REV` also holds a **blank `KETERANGAN` production block** (CUTTING / SEWING / WASHING / PACKING, no values), **intended pack ratios** (`30-32=34`, `34-36=32`, `38-40=30`), and two real data-entry errors (`R17`: `GW 1.15` / `NW 0.00` for 27 pcs; `R85`: carton range reads `'18-'` where `ACT` reads `'19-20'`). Its lines also came back **`high`** confidence against `ACT`'s **`medium`** — not a vote for `REV`, but the shape of confidently extracting the wrong document.

    **Behaviour is now deliberate rather than accidental.** `ACT` won because its style matched PO 1725 and `REV`'s `50144-2` did not — the right outcome by coincidence, with nothing recording it. `matcher.describe_sheet_selection` now states the choice on the shipment (*"this document contained 2 sheets with different style codes; ACT (50144) matched PO 1725 on 28 of 28 line(s); REV (50144-2) did not"*), it lands in the audit detail, and a test pins it so the non-matching sheet cannot later be made to win silently. Note the headline extraction total (1725 units) is still the sum of both sheets and means nothing on its own — read the per-style split.

21. **Two retracted findings from the ACT/REV work, named by value so they cannot be requoted** (same reason as the retracted "51 codes" figure in item 12).

    **RETRACTED: "the carton rows sum to 202 against a recap of 324, so they do not reconcile."** Wrong, and the reconciliation is exact. The carton grid's waist cells hold quantity **per carton**; the carton count is in column **T** and the extended total in column **U** (`U = S x T`). Row 13 reads `A='2-3'` (two cartons), `J=32` (32 per carton), `S=32`, `T=2`, `U=64` — 64 pieces, not 32. Summing the waist cells directly is what produced 202. Corrected, `ACT`'s NIN INS 32 cartons total **324**, matching its recap to the unit, and all eight carton sections across both sheets reconcile exactly with their recap blocks. **Do not quote 202.**

    This also upgrades the inseam attribution from inference to measurement: recap **block 1 = INS 32** and **block 2 = INS 34**, confirmed on all four blocks of both sheets by extended carton totals (NIN 324 / 110, SLV 323 / 108 on `ACT`; 324 / 108 and 322 / 106 on `REV`). Earlier notes describing that attribution as "inferred from position" are superseded.

    **RETRACTED: "the recap blocks are not labelled with their inseam."** True of `ACT` only. `REV` **does** label them — `H45='INS 32'`, `H51='INS 34'`, and again at `H119`/`H125`. `ACT` carries `INS` labels only on its carton sections (`H11`, `H24`, `H83`, `H97`). The original claim was made while looking at `ACT` and generalised without checking `REV`. The practical consequence is the opposite of a problem: it is why the extractor had to derive `ACT`'s attribution and honestly marked those lines `medium` (see §7), and it is one more asymmetry consistent with `REV` being the planning worksheet.

22. **NEW 2026-09-02 — the empty-size line is a vendor data-entry error, not a composition or a MIXED-carton problem.** It comes from `REV!R85`, where `CTN NO` reads `'18-'` — a truncated carton range, with cartons apparently skipped — and the extractor emitted a quantity-0, low-confidence placeholder rather than dropping the anomaly. **Unrelated to the `MIXED` rows** (`H19`, `H28`, `H29` on `ACT`), which are ordinary multi-size cartons that spread their quantities across several waist columns and read correctly.

    Behaviour is right as-is and is now pinned by a test: a blank size is not a size, so it is never used as a merge key, never matched, and stays flagged. Worth noting only because it is on the `REV` sheet, alongside that sheet's other data-entry errors, which is one more asymmetry consistent with item 20's conclusion that `ACT` is the packing record and `REV` the plan.

23. ~~**NEW 2026-09-09 — a slip with several transport-mode recap rows lost all but one of them.**~~ **FIXED 2026-09-09 (change 8, transport modes).** *Label collision worth knowing: item 9 also records a "change 8" — the tranId transformation of 2026-08-31. Two unrelated changes carry that number, so cite them by subject rather than by number.* The footwear slip for PO 1624 splits each size across a `By Sea` row and a `By UPS` row, and NetSuite holds a separate PO line for each. The tool took `By Sea` only — **not by decision**: the extractor picked one row and nothing downstream looked for others, so the UPS portion vanished silently.

    Paula's ruling: **propose both, she assigns.** Implemented as extraction emitting one line per size per recap row tagged with that row's label verbatim (`recap_label`), the label joining the extraction-side canonical key, aggregation grouping on it, and a new `NEEDS_ASSIGNMENT` state that surfaces both sides and pairs nothing.

    **THE ASSIGNMENT IS PERMANENTLY MANUAL, and the reason is measured, not assumed** (73 duplicate-key groups across six POs — see item 24 for the full survey). `_assignment_payload`'s docstring carries it so it cannot be re-litigated from memory:
    - **No per-line transport-mode column exists** — 49 line fields, none of them mode, carrier, incoterm, freight or vessel.
    - **`rate` and `leadTime` are identical on both lines in all 73 groups.** If mode were modelled per line, those two are exactly what would differ; their agreement is the strongest available evidence that it is not.
    - The header's `shipMethod` is per-PO, empty on 3 of 6 surveyed, and reads `BOAT` on the very PO carrying a UPS portion.
    - **Quantity equality is not a discriminator.** It appeared to work on PO0001624 only because the receipts were already posted there, so both lines already reflected the shipment and one side matched exactly. On a PO awaiting the update — the only kind this tool acts on — both lines carry ordered quantities and match neither row.

    **NEVER key the pairing on `custcol_override_expected_receipt` or `custcol_sd_updatedreceiptdate`.** They differ in 31 of 73 groups, so they look like signal. They are an **echo**: this tool writes both fields, so pairing on them would let its own past writes decide its future pairings, and the correlation would strengthen with every run whether or not it was ever right. The most convincing-looking candidate here is the most dangerous one. (`custcol_sd_fg_excluderepspark` differs in 51 of 73 — the most of any custom column — and is out of scope entirely.)

    **Adding the label to the key is not a reversal of item 10's finding.** Item 10 established that a key collision on the **NetSuite** side cannot be fixed by improving the key, because the information does not exist there. Here it does: **the slip labels its own rows.** Reading a label the document prints is not the same act as inventing a distinction NetSuite does not record. Opposite situations, opposite answers.

    **Migration 0004** widens `ux_proposed_changes_canonical_key` to include `key_recap_label` — un-widened it *forbade the second row*, rejecting in the database what should reach a human — and adds a guard the schema never had: **`ux_proposed_changes_one_line_per_shipment`**. That is the mirror image of `ux_change_candidates_one_selected`, which stops one change selecting two lines while nothing stopped **two changes selecting one line**, whose second write silently overwrites the first. Unreachable while every key produced one row; reachable the moment two rows share a key; so it ships with the change that creates the risk. Scoped per shipment, because a later shipment updating the same line is normal.

    **Result, live 2026-09-09.** Footwear went from 28 proposals to **44** — **16 assignment groups covering 32 changes, plus 12 unambiguous singles** — and the arithmetic reconciles exactly against the sheets: 6 dual-row sizes on `20138` (size 14 has `By UPS` 0), 5 × 2 colours on `20139`, and `20140`'s 7 sizes are unlabelled singles. All four single-recap vendors are unchanged and report **zero** recap labels and **zero** assignment cases: **Inprotex 6,387 units / 77 lines, 77 of 77 targeted** (all six POs resolved this run, against 53 of 77 previously when two of them hit sandbox read timeouts), Legendz 1,049 / 8, Symmetry 1,669 / 25, and Tainan 1,725 / 56 with 28 targeted.
24. **NEW 2026-09-09 — the duplicate-line survey, and what it rules out.** Kept in full because it is the evidence base for item 23's "permanently manual", and because every field in it *looks* like a discriminator until measured. 73 duplicate-key groups across PO0001624 (16), PO0001620 (4), PO0001514 (1), PO0001649 (1), PO0001555 (41) and PO0001366 (10).

    **Identical on both lines in all 73 groups**, so carrying no signal whatsoever: `rate`, `leadTime`, `units`, `item`, `itemType`, `matrixType`, `isClosed`, `isBillable`, `matchBillToReceipt`, and every `custcol_ava_*`, `custcol_scm_*`, `custcol_sd_tmpl_*` and `custcol_product_*`.

    | Field | Differs | Why it is not a discriminator |
    |---|---|---|
    | `isOpen` | 16/73 | **All 16 are PO1624.** Elsewhere 0/4, 0/1, 0/1, **0/41**, 0/10. Means closure state |
    | `quantityBilled` | 73/73 | Tracks quantity. "Exactly one line unbilled" is 16/16 on 1624 and **0** on the other four |
    | `expectedReceiptDate` | 30/73 | **Identical on all 41 PO1555 groups**; where it differs nothing labels which date is sea |
    | `custcol_override_expected_receipt` | 31/73 | An echo of this tool's own writes — see item 23 |
    | `custcol_sd_updatedreceiptdate` | 31/32 | Same |
    | `custcol_sd_fg_excluderepspark` | 51/73 | Out of scope by standing instruction; a RepSpark flag, not a mode |
    | `description` | 21/73 | **Identical within every PO1624 group** (it is the item description) |
    | `line` number | 73/73 | The higher line number carries the smaller quantity on 1624/1620/1555/1366 — but that is a *quantity* observation, silent about mode, and it inverts the moment an air shipment is the larger one |

    **And most duplicate-key pairs are not transport splits at all.** On PO1555 — the largest set at 41 groups — both lines share one date, both are fully received, both fully billed, neither has an override. Same on 1620, 1514 and 1366. Only PO1624 shows the asymmetric one-open/one-closed shape. So a rule of "two recap rows means update both lines" must not be generalised into "a duplicate key means a transport split"; the two are different phenomena that happen to coincide on one PO. Worth asking Paula what these pairs mean on the other POs before anything keys on them.

25. **NEW 2026-09-09 — the footwear workbook's ACTUAL labelled figures, because a wrong set has been circulating in this project's briefs.** Read off the sheets cell by cell, not from any prior note:

    | Row | Sheet `20138` | Sheet `20139` (twice — two colour blocks) | Total |
    |---|---|---|---|
    | labelled `By Sea` | 256 | 592 + 592 | **1,440** |
    | labelled `By UPS` | 44 | 8 + 8 | **60** |
    | **unlabelled** recap (sheet `20140` R39) | — | — | **600** |

    `1,440 + 600 = 2,040`, and **2,040 is the document grand total** — printed on sheet `20140` at R41 beside `255` cartons. The extractor reproduces every one of these figures exactly.

    **The previously-circulated "By Sea 2,040 / By UPS 140 / Ordered 2,180" is WRONG.** Named here so nobody re-derives it from an old note:
    - **2,040 is the grand total, not the By Sea subtotal.** It is the three labelled `By Sea` rows *plus* sheet `20140`'s unlabelled 600.
    - **140 appears nowhere in the workbook.** It is `2,180 − 2,040`, subtracted from a figure that was itself mis-read. The labelled `By UPS` rows sum to **60**.
    - **2,180 appears nowhere either.** There is no document-wide `Ordered` row; sheet `20138` alone prints `Ordered Qty`, at 300.

    The error was self-reinforcing, which is why it lasted: `1,440 + 600 = 2,040` makes the wrong reading arithmetically satisfying, and the subtraction then manufactures a plausible companion figure out of nothing.

    **Sheet `20140`'s 600-unit recap row carries NO transport-mode label, and it stays unlabelled.** Its `recap_label` is empty, its 7 lines take the ordinary single-row path, and they account for 7 of the 12 non-assignment changes. **Do not attribute it to a mode.** The temptation is real — 1,440 + 600 reconciling to the grand total makes "so the 600 must be sea" feel obvious — but the sheet does not say so, and extraction rule 2b exists precisely to stop the tool inventing a label the document withholds. If that 600 needs attributing, Paula attributes it.

26. **NEW 2026-09-09 — the Graph credential chain is live, and one thread is still open with IT.** Recorded because it is the state Phase 2's intake job assumes, and because the *way* each part was established differs.

    | | State | How |
    |---|---|---|
    | Certificate on the app registration | **working** | `scripts/probe_graph_auth.py` mints a token with it — observed, not reported |
    | `Mail.Read` granted **and** admin-consented | **working** | the target mailbox read returns `200`; an unconsented application permission returns `403` on the call itself, so the read proves both |
    | Mailbox scoping (RBAC for Applications) | **OBSERVED** | a second, populated mailbox in the same tenant returns `403 ErrorAccessDenied` at step (d). Previously only asserted by IT |
    | A second certificate alongside ours | **UNKNOWN — open** | see below |

    **The open thread: ask IT to list the thumbprints currently on the registration.** This tool cannot see them. Listing an app registration's own credentials needs `Application.Read.All`, which this app does not have and should not be given for reading mail. IT did state in the morning that ours was the only certificate — but that was *before* the upload that actually put ours there, so it describes a registration state that no longer exists. Any thumbprint other than `E05CF5DB8EBFC7CAF259FD5EA6678B966353F016` is an **unclaimed public key**: nobody here holds its private half, so it cannot serve this pipeline, and its presence means Entra would accept an assertion signed by whoever does.

    **The certificate took two attempts to land, and the failure mode is worth knowing.** The first probe run got `AADSTS700027` (*the key was not found*) despite that morning's confirmation that the thumbprint was correct — it was correct, and it had not been uploaded. Re-verifying the local pair immediately afterwards is what isolated the fault to the Entra side rather than leaving it ambiguous. `GRAPH-SETUP.md` carries the timeline; the general lesson is §8 lesson 11.
27. **NEW 2026-09-14 — the write-back test re-ran under the current SEVEN-permission role and passed.** Recorded because the previous pass was a claim about a role that had stopped existing.

    **What was wrong with the old evidence.** `test_phase1_writeback.py` passed on **2026-08-04** against a **five**-permission role. The role then gained `Reports > SuiteAnalytics Workbook` on 2026-08-12 and `Setup > Custom Lists` after it, and `netsuite_client.py` changed across seven commits. Nothing re-ran the test. The sentence "the write path works under the least-privilege role" stayed in the build plan and quietly changed meaning underneath itself.

    **The re-run, 2026-09-14, sandbox `1321665-sb2`, PO internal id 8489541 line 18** (`M120246 : M120246-Waterman Polo-TID-3X`, style/colour/size confirmed before writing):

    | Field | Before | Written | Read back | Reverted to |
    |---|---|---|---|---|
    | `quantity` | 2 | 99 | 99 | 2 |
    | `expectedReceiptDate` | 2026-07-15 | 2026-06-27 | 2026-06-27 | 2026-07-15 |
    | `custcol_override_expected_receipt` | False | True | True | False |
    | `custcol_sd_updatedreceiptdate` | None | 2026-06-27 | 2026-06-27 | None |

    All four in **one** PATCH — `HTTP 204` — then verified individually, reverted, and **each revert verified too**. Exit 0. Auth was M2M/JWT with no browser, token in ~4s.

    **It also rules out the quiet failure, which is the reason the test reads back field by field.** NetSuite can accept a PATCH with `204` and **silently discard** a field the role is not permitted to write — no error, no indication, the old value simply still there. A test that checked only the status code would pass on a role that wrote nothing. Both custom fields, the two most likely to carry field-level access restrictions independent of record-level Edit, came back changed.

    **The rule this produced, now written into `NETSUITE-M2M-SETUP.md` and the build plan's Phase 4 role procedure: ANY change to the role's permission set — add, remove, or re-level — re-runs this test, and the result gets dated here.** The gap existed because nothing connected a permission change to the test that had proven the write; they were related only in someone's memory. Linking them in both documents is what stops it drifting again. It costs under a minute, it reverts what it writes, and it verifies the revert.

    Note what this does **not** cover: the test targets an internal id directly, so it passes on a role with no collection access at all. That is why a five-permission role would pass here and still fail on every real vendor document — see §6 item 2 and the `400 USER_ERROR` signature in the setup doc.


## 7. Design constraints discovered by testing

These are not open questions — they are settled constraints that later phases must respect. Each was found by measurement, not design review.

**Row identity must be the canonical key, never a digest of the whole row.** Verbatim display text varies between runs *by design*: the extractor may render a colour `NEW INDIGO` on one run and `NEW  INDIGO` on the next, and the pipeline deliberately preserves what was printed rather than rewriting it. In one five-run comparison, 6 of 25 rows differed in displayed text while keys, quantities, counts and row order were identical. A row-digest idempotency check on `proposed_changes` would therefore see spurious changes on every re-parse. Key on the canonical form (`canonical.py`).

**The confidence NOTE carries real information even where the LEVEL does not. Surface the note; do not lean on the level.** First hard evidence on 2026-09-02, and it separates two things that had been treated as one.

On Tainan's `ACT` sheet the extractor returned `medium` on all 28 lines with this reason: *"Recap block at rows 46-49 is unlabelled for inseam; identified as INS 32 because its ACTUAL row matches the INS 32 carton rows exactly."* Every part of that was independently verified afterwards. The recap blocks really do lack an inseam label on `ACT` — `H45`/`H51` carry `INS 32`/`INS 34` on `REV` and are **absent** on `ACT` — so the difficulty was real, not imagined. The stated method was the correct method. And the answer was right: the carton rows reconcile to 324 and 110 exactly, matching recap blocks 1 and 2. **A true statement, correct reasoning, right answer, and an honest downgrade for a genuine ambiguity.**

Set that against the level, which flagged 19 of 29 correctly matched lines in the same corpus. The level is unselective and stays that way. But **a reviewer reading the note learns something specific and actionable; a reviewer reading "medium" learns nothing.** So as a Phase 3 review-UI requirement: **the note is a first-class field on the review row, not a tooltip on a badge**, and queue ordering must not be built on the level alone. This is also the first datum for calibrating them separately — note quality and level quality are different measurements, and only the level is known to be uninformative.

**The confidence signal is INERT for triage, and now measurably so.** After change 7 resolved colour matching, the real corpus produces 29 matched lines of 33 — and **`needs_review` fires on 19 of those 29 correctly matched lines.** The flags are *honest*: the extractor really did infer the PO, style or colour from a block recap row or carry it forward from the row above, and it said so. They are simply **unselective**, and a reviewer learns within about a week that a flag firing on two thirds of correct work means nothing.

Those 19 are also the first calibration rows with a **target line attached**, which is what makes them usable: the tool's claim, the line it matched, and space for the human verdict. See the Phase 3 acceptance criterion in the build plan — without a verdict recorded on *every* line shown, including the accepted-unchanged ones, this corpus has no negatives and the flag can never be calibrated.

The original observation, kept because the shape of the problem has not changed: `needs_review` came back `True` on all four real documents — including the ones that were subsequently hand-verified as flawless. A flag that always fires carries zero information. Two things follow: Phase 3 must **not** build its review queue on `needs_review` as-is, and the signal is **uncalibrated rather than proven safe** — zero extraction errors were observed across 20/20 hand-verified line items, so the false-negative rate is unmeasured, not zero. Calibrating it needs a corpus with known-bad documents.

**A shipment is not 1:1 with a PO, and the fan-out is larger than assumed.** One Inprotex sheet interleaves **six** POs (1640, 1645, 1650, 1662, 1667, 1704); the Symmetry set spans two (1720, 1721). Consequences for Phase 2/3:
- the `shipments` / `proposed_changes` schema must model one email spanning many POs,
- the Phase 3 **approval unit** must be defined deliberately — per PO, per shipment, or per line — rather than falling out of the implementation,
- **write-back needs partial-failure semantics**: one approval can mean six PO writes, and the fifth can fail. What the audit log records, and what Paula sees, when three succeeded and one didn't, has to be decided before the write path is wired.

**A fractional quantity in a shipped column is not a count — and a recap row that is a clean multiple of an order row is a plan wearing a count's clothes.** This is the part of the ACT/REV forensics (§6 item 20) that outlives the file. You cannot ship 17.28 pairs of trousers, so a fractional value in a size column is *derived*; and a tally of cartons does not come out as a constant multiple of the order, so a row that does is a target. Either signal identifies a plan on sight, with none of the cell-by-cell comparison it actually took to establish.

Both are implemented as **extraction NOTES, not gates** (`claude_extractor.derived_quantity_notes`). A derived quantity is not wrong — a vendor is entitled to plan an 8% overship — it is just not evidence of what shipped, and the distinction vanishes the moment the figure is rounded to an integer and put in a column headed `ACTUAL`. Nothing branches on the notes; they go to a reviewer.

**Making them selective was the whole job, and it took three measured passes** — worth recording, because the naive version of each is useless in a way that only shows up against real documents:

- Unrestricted, the fractional signal fired **47 times on one clean footwear document**. Every hit was a weight or CBM row. The rule's own wording carries the fix — *"in a shipped column"* — so both signals are confined to the size grid (`attachment_classifier.quantity_columns`).
- Confined but uncoupled, it still fired **24 times on Inprotex**, whose size grid legitimately carries a per-unit weight row per block. So a fractional row must now **round to** a neighbouring whole-number row — which is what made `REV`'s row 45 meaningful in the first place.
- The multiple check reported a weight row against a quantity row as **"x 0.00198"**, so the factor must be a plausible over/under-ship and the base row whole numbers. It also has to look **both directions**: `REV` puts its uplift row *above* the order row it derives from, so a forward-only scan never saw it.

Final behaviour across the whole corpus: **6 notes, all 6 on `REV`**, naming both the rounding coupling and the `x 1.08` factor; **zero** on `ACT`, Inprotex, Legendz and Footwear. Compare that with the `needs_review` flag above, which fires on everything and therefore says nothing — a note is only worth adding if it stays quiet on correct documents.

**A uniform overship is a document-level fact, and per-line review hides it.** Tainan's slip ships **865 against an ordered 800 — +8.125%, with every single size over** (the sheet states this itself at `ACT!R63`). While the sizes did not match, this was invisible: all 29 lines flagged on size, so the overship never surfaced. Now that they match it surfaces correctly as a `PENDING_REVIEW` quantity change per line, per ruling 6 — but that is 28 individually plausible small increases (`16 → 17`, `85 → 91`, `84 → 90`), each of which reads as an unremarkable rounding when seen alone. The pattern only exists in aggregate.

This is a Phase 3 review-screen requirement, not a diff-engine one: **the approval unit needs a document-level summary alongside the lines** — ordered versus shipped for the whole PO, and whether the variance is one-sided. A reviewer who sees "+8% on every size" asks the vendor a question; a reviewer clicking through 28 rows of `+1` approves them. Nothing in the pipeline should *gate* on this (a uniform overship can be entirely legitimate), but it must be *shown*. The five-vendor run script reports it explicitly for this reason.

**A cheap signal may rank; only an authoritative one may reject.** Two of the four defects found on 2026-09-02 were the same shape: a cheap proxy for a fact was allowed to make a *terminal* decision about that fact. A filename was allowed to exclude an attachment from ever being opened, and a file extension was allowed to decide which reader ran. Both proxies are usually right, which is why both survived review — and both were wrong on the first genuinely new vendor, in the direction that discards data silently. The rule that follows applies well beyond these two: rank on the cheap signal if it helps, but route rejection through the authoritative one — content for what a document is, magic bytes for what a file is. A proxy that can only reorder work is safe; a proxy that can end it is not.

**When the model already understands the document, the fix is permission plus a target — not a parser.** The two-axis size layout (§6 item 19) looked like the hardest parsing problem in the corpus and turned out not to be a parsing problem at all: the extractor had described the structure correctly, in its own words, in a warning nobody had acted on. What it lacked was permission to combine the two axes and knowledge of how this account spells the result. So the fix was three things — a prompt rule, the account's real vocabulary shown to the model, and a code-side membership check — and no new parsing logic whatsoever.

The generalisable part is the division of labour. **Reading an unfamiliar layout is a judgment call and belongs to the model; "the value I produced is a real value in this ERP" is a checkable fact and belongs to code.** Wherever the model is asked to produce a value from a controlled vocabulary, show it the vocabulary *and* validate membership afterwards. Showing it alone is not enough (nothing stops an invention), and validating alone is not enough (a model that has never seen `30-32` will invent `30/32` and every line will flag). Read the extractor's warnings before writing a parser: it flags what it could not do, and that list is a work queue.

**A hand-maintained vocabulary of someone else's data is a latent bug with a delay on it.** The size-header detector's hardcoded letter-size set was not wrong when written; it became wrong when Straight Down's footwear and bottoms lines made numeric sizes the majority of the account's 46 values. Nothing failed loudly — the sheets simply read as sizeless. The same trap exists anywhere the pipeline compares vendor text against "our" vocabulary, and the answer is the one now used for sizes: read the list from NetSuite, snapshot it for offline use, and provide a drift check (`python size_vocabulary.py --check`). Note that `matcher.SIZE_ALIASES` is a different thing and is legitimately hand-maintained: it maps *vendor spellings onto* NetSuite labels, so it is a translation table, not a copy of NetSuite's data.

### SuiteQL is unreliable in specific ways — prefer REST paging and per-row queries

Four separate instances, one habit. Each was found the hard way, and **every one of
them fails without raising an error** — which is why they belong together rather
than scattered as curiosities: the shared lesson is that a SuiteQL result looking
plausible is not evidence that it is complete or correct.

**1. `OFFSET` is SILENTLY IGNORED.** `FETCH FIRST` is honoured; `OFFSET` is not, so
`... ORDER BY id OFFSET 1000 ROWS FETCH FIRST 1000 ROWS ONLY` returns the **first**
page every time. A paging loop written that way never advances and never
terminates. Ours was still cheerfully fetching page one at **offset 505,000** when
a ten-minute timeout killed it. Nothing errored at any point.

**Page with the endpoint's own query parameters instead** —
`POST /query/v1/suiteql?limit=1000&offset=1000` — which pages correctly and returns
`totalResults` and `hasMore` to loop on. Keyset pagination
(`WHERE id > :last ORDER BY id FETCH FIRST 1000`) also works and is the safer habit
for a long-running job, since it cannot drift if rows are inserted mid-scan. Both
verified: 1,659 POs in two pages either way.

**2. `totalResults` is capped and wrong at small limits.** Verbatim, because it is
the clearest instance we have:

```
GET /purchaseOrder?limit=1  ->  totalResults=1000, hasMore=true
GET /purchaseOrder?limit=5  ->  totalResults=1659, hasMore=true
```

A caller trusting `totalResults` at `limit=1` concludes there are exactly 1,000 POs.
There are 1,659. **Follow `hasMore` to completion; never treat `totalResults` as a
count.**

**3. `GROUP BY` returns 500s.** `GROUP BY status` and
`GROUP BY (tranid, id, item) HAVING COUNT(*) > 1` returned **HTTP 500 with three
distinct error ids** across repeated attempts, while per-status `COUNT(*)` and a
narrower `GROUP BY t.tranid` succeeded instantly. The failure is about query shape,
not load or permissions. **Compute aggregates client-side from narrow reads.** One
thing that worked exactly as designed: the change-3 transient-retry handler retried
the 500s with backoff and then **reported** the failure rather than silently
returning an empty result set — demonstrated on a real fault rather than a
simulated one.

**4. `transactionLine.isclosed` is NOT the complement of `isOpen`.** The per-line
Closed checkbox is effectively unused in this account — nobody ticks it — so
`isclosed = 'F'` reports the lines of a **Fully Billed** PO as fully open. That
produced a stated count of **~1,024 "open POs"** which was simply wrong, and wrong
in the most dangerous direction: a plausible number, quoted with confidence, that
nobody would think to question. **Use PO status for the business meaning of "open",
and REST's per-line `isOpen` as the usable flag** — present on all 367 lines of the
25 most recent sandbox POs. `POLine` models both fields separately, with the trap
named on the field itself, because a line can be **neither** open nor closed.

**Also worth knowing:** a `LIKE 'PO%'` scan over the whole `transaction` table with
no `type` filter does not return in ten minutes. Filter by `type` first.

### Paging: the pipeline is clean today, and Phase 2 is where that changes

A silent-truncation audit (2026-08-31) checked every NetSuite read in the pipeline
against the two traps above.

| Call site | Shape | Verdict |
|---|---|---|
| `_lookup_via_record_query` | `?q=... &limit=5`, reads `items` | **Safe by design** — it wants exactly one match and raises on more than one, so ignoring `hasMore` is correct here |
| `get_purchase_order_record` | `expandSubResources=true` → `record["item"]["items"]` | **Now guarded** — see below |
| `_fetch_sublist_the_long_way` | `/purchaseOrder/{id}/item` collection | Same guard; fallback path, has never fired live |
| `verify_connection` | `?limit=1` | Diagnostic only, does not consume `items` |

**No SQL `OFFSET` appears anywhere in the pipeline** — the only uses were in
throwaway probe scripts.

**The structural finding, which is forward-looking rather than historical: no
pipeline code paginates a collection to completion, because none needs to yet.**
The single collection read is the one-PO lookup. Every bulk enumeration so far has
lived in a probe script that was deleted afterwards. **Phase 2's polling job is
where this first becomes real, and any enumeration it does MUST follow `hasMore` to
completion** — a mailbox listing, a "which POs are open" sweep, a backlog replay.
Getting that wrong processes a subset and reports success.

**The PO sublist is the one place a real PO could truncate, and it is now guarded.**
The sublist reports `totalResults` but **no `hasMore` and no `offset`**, so counting
is the only available signal. `_assert_sublist_complete` compares `len(items)`
against `totalResults` on every PO read and raises `SublistTruncated` on a
mismatch. Fatal rather than a warning, deliberately: a truncated read stages
proposals for the lines it saw and stays silent about the rest, Paula approves what
she is shown, and the missing lines never appear anywhere — no error, no flag, no
row. A partial PO looks complete, which is what makes it worse than a failed read.
Zero false positives on current data: the largest PO in the account is **380 lines**
(`PO0001497`) and **no PO has 1,000 or more**, so the guard raises on nothing today
and exists for the PO that eventually does.

### `recap_label` is in the EXTRACTION key and NOT in the NetSuite-side key

Two keys, deliberately asymmetric, and the asymmetry is the design rather than an oversight:

- **Extraction side** — `(PO, style, colour, size, recap_label)`. Used by `aggregate_lines`, stored as `proposed_changes.key_recap_label`, and enforced by `ux_proposed_changes_canonical_key`. Two recap rows for one size are two rows.
- **NetSuite side** — `(PO, style, colour, size)`, unchanged. `matcher._find_matching_lines` does **not** filter candidates by recap label, and `_sibling_key` deliberately drops it so sibling rows can see each other.

**Why they differ: the slip labels its rows and NetSuite does not.** A PO line carries no transport-mode field at all — 49 line fields, none of them mode, carrier, incoterm or freight, and `rate` and `leadTime` identical on both lines in all 73 duplicate groups (§6 item 24). On the extraction side the distinction is *read from the document*; on the NetSuite side there is nothing to read.

**This is not a reversal of §6 item 10** ("you cannot fix this by improving the key"). That finding was about the NetSuite side specifically and still holds: adding a column there would mean inventing a distinction the record does not make. Adding a label the vendor printed to the extraction-side key is reading the source. Opposite situations, opposite answers — and the mistake to avoid is applying either conclusion to the other side.

The consequence worth holding onto: **an assignment case exists precisely because the extraction key splits while the NetSuite key does not.** N rows meet N lines and neither key resolves which goes with which, which is why the outcome is `NEEDS_ASSIGNMENT` and a human — not a cleverer key.

### `ls -l` lies about Windows ACLs, so never audit file permissions from Git Bash

`C:\dev\po-agent-secrets\po-agent-graph.key` is locked to a single user — inheritance stripped, `icacls ... /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F"`. From this project's Git Bash shell it reports as:

```
-rw-r--r-- 1 kiko.barroso 1049089 1704 Sep 9 11:17 po-agent-graph.key
```

**World-readable, apparently.** It is not. MSYS synthesises POSIX permission bits for a security model that has no POSIX bits in it, and when it cannot express a Windows ACL it emits a plausible default rather than an error. The private key was correctly protected the entire time the `044` group and other bits were being displayed.

This is not academic: **it produced a false finding in this very repo.** A permissions audit on 2026-09-09 reported the Graph key as world-readable and recommended tightening an ACL that was already correct, and it took the person who ran `icacls` to say so. The report was wrong in the *safe* direction, which is the more insidious case — a false alarm costs a cycle, but the same tool would equally happily render a genuinely open file as `-rw-r--r--` and hide a real problem.

**Check Windows permissions with a Windows tool:**

```powershell
icacls C:\dev\po-agent-secrets
```

Same class of mistake as §8 lessons 11 and 12 (a confirmation is only as good as the question it answered; do not gate a verdict on evidence you cannot read): the output *looked* like evidence, so nobody asked what produced it. The general rule — **when a tool translates between two models, its output describes the translation, not the thing.** MSYS on ACLs, `git status` on case-only renames, and `stat` on a network share are all the same shape.

### The layout renderer can double a space INSIDE a value, and that is not a bug to fix

`render_pdf_page_layout` places every word at a character column derived from its x-position, which is what preserves column alignment on a numeric table (see the size-column reasoning in its docstring). A consequence: when one logical cell holds two words, the x-gap between them maps to **two** character columns. The Symmetry packing list prints `NEW INDIGO` with a single space; the model is shown:

```
   1720       1      M650022     NEW  INDIGO            22
```

**The model resolves this one way per document, self-consistently, and both readings are defensible** — the doubled space is either part of the value or a layout artefact, and nothing in the rendered text settles it. Measured over 10 runs of the Symmetry pair: 9 returned `NEW INDIGO`, 1 returned `NEW  INDIGO`, and in that run **all six** of that colour's lines carried it while the other document in the same run carried none.

**`canonical()` absorbs it completely, so nothing downstream can distinguish the runs.** `extraction_schema.aggregate_lines` keys on `canonical(color)` and merges the two spellings into one line; `matcher._sibling_key` and `_find_matching_lines` canonicalise both operands; `proposed_changes.key_color` stores the canonical form with the verbatim text preserved beside it in `src_color_text`. Change 4 exists for exactly this class of input — its worked example is literally `NEW  INDIGO` against NetSuite's `NEW INDIGO`. The two runs produce byte-identical database rows and byte-identical NetSuite proposals.

**The renderer was deliberately NOT changed, and the reason is that the gap IS the signal.** Collapsing runs of spaces would require distinguishing an intra-cell gap from an inter-cell one, and the renderer does no column detection — it has no notion of where a cell starts or ends, only where words are. A blanket collapse would flatten the column structure this function exists to preserve, misfiring across every vendor to solve a problem one vendor has and the pipeline already handles. Same shape as the rule in §8 lesson 12 about proxies: fix the thing that actually decides identity, not the thing that happens to be easy to edit.

**What this cost, and where it surfaced:** only the *tests*, which keyed on raw strings — see §8 lesson 17. The pipeline was never affected.

### Migrations freeze their data as literals and import no application code

**The rule, now enforced by a test rather than by discipline:** a migration writes only literals spelled out in the migration itself. `schema.py` is the *runtime* declaration and the thing tests compare against; it is never a source a migration reads. Any edit to it that changes seeded data requires a paired migration carrying its own frozen literal.

**Migration 0001 violated this for three imports** — `CHANGE_STATES`, `CHANGE_STATE_TRANSITIONS` and `VIEWS` — and wrote whatever those held at the moment it ran. **FIXED 2026-09-09**: replaced with literals recovered from git history at 0001's own commit (`11b2817`) — 11 states, 28 transitions, both view definitions as they stood — generated programmatically and verified to round-trip against that commit exactly rather than transcribed.

**The drift that had already happened was self-correcting, and that was luck, not design.** By 2026-09-09 `schema.py` held 12 states and 32 transitions against 0001's historical 11 and 28. A database at 0003 converged on upgrade because 0004's inserts are conditional — but **0004 was written to make change 8 work, not to repair 0001**. Nothing had been designed to reconcile the two, and the next state-adding migration written without that conditional would have diverged silently.

**No reconciliation migration was added**, deliberately: there is nothing to reconcile (both paths reach 12/32 today), and a reconciler that read `schema.py` to learn the intended set would be the identical time-dependence bug at a higher revision number. If one is ever needed it carries a frozen literal like anything else.

**Two things worth knowing if you ever freeze a literal from history:**
- **Do not wrap long strings across adjacent literals.** The first generator did, and dropped a space at each join (`'may still be'` `'wanted on the line.'` → `bewanted`), which would have silently changed the seeded descriptions. The frozen block is deliberately unwrapped however long the lines run.
- **Check view coverage before freezing, or the freeze can create the divergence.** Freezing 0001 to historical DDL would leave a fresh build at head with a stale view *unless* a later migration recreates it from its own literal. Here both views are recreated by 0002, 0003 **and** 0004, and 0004's literals are whitespace-identical to `schema.py`'s current ones — so head produces today's views on every path. Verified rather than assumed, including that 0002's `VIEW_REVIEW_LINES_0001` downgrade literal matches the real historical DDL, so upgrade-to-0001 and downgrade-to-0001 agree.

`test_schema.test_migration_seed_matches_schema` migrates an empty database to head and compares every row and both view definitions against `schema.py`, **in both directions**; `test_migrations_import_no_application_code` parses each migration's AST and rejects any project import. The second is the one that stops the pattern returning.

One historical note kept because it explains why 0004 looks the way it does: while 0001 still imported live, a state-adding migration could not simply `INSERT` — that succeeded on a fresh build (where 0001 had already seeded the new state) and raised `IntegrityError` on a database upgraded from an older revision. 0004 therefore uses `INSERT ... SELECT ... WHERE NOT EXISTS`, which is now belt-and-braces rather than load-bearing. Found by running the migration, not by reading it: the failure appears only on one of the two paths.

### Migrations: autogenerate cannot see views, and SQLite will not alter under one

This will recur on every future migration, so it is worth knowing before writing
one rather than after.

**Alembic's autogenerate does not track views** — they are not in the metadata — so
any view change has to be hand-written into the migration.

**And on SQLite, EVERY view over a table must be dropped BEFORE a batch `ALTER`,
not just the view being changed.** Batch mode rebuilds the table (create tmp, copy,
drop original, rename), and dropping a table that any view references fails
outright: `error in view v_calibration: no such table: main.proposed_changes`. In
migration 0002, `v_calibration` had to come down and go back unchanged purely
because `proposed_changes` was being altered underneath it. Cost two failed
round-trips to find, because the first failure names the *view*, which is not the
thing being changed.

Two habits that follow: drop every dependent view first and recreate them after,
and spell the previous definition out **in the migration** for the downgrade rather
than importing the live one from `schema.py` — importing it would silently
reinstate the new shape and make the downgrade a no-op.

**Two values for one thing, deliberately: `PROMPT_VERSION` and `prompt_fingerprint()`.**
The version is a human-readable string (`2026-08-31.1`) stored on
`shipments.extractor_prompt_version`, because a calibration query sliced by prompt
revision has to be legible to whoever reads it. The fingerprint is a 16-character
hash of all four prompt texts, pinned by a test — so editing a prompt without
bumping the version fails loudly, with both values shown.

The hash is **not** used as the stored value: an opaque string in a database column
tells a reader nothing. That is the same separation already in force between
verbatim vendor text and canonical keys — display and identity are different jobs,
and collapsing them loses whichever one you did not choose.

**`ns_line_is_open` and `ns_item_internal_id` describe the MATCHED line**, so when
change 5 selects no target they stay NULL and the per-line open state is on
`candidate_lines` instead. The review screen therefore reads open state from two
places depending on outcome: the row when a line was chosen, the candidate payload
when none was. Deliberate rather than denormalised — a copy on the row would be a
second source of truth for something change 5 explicitly refused to decide — but
whoever builds that screen needs to know it up front.

**Not yet exercised against a real document:** the merge-note path (`color printed as 'X' and 'Y' in the source`) is covered by unit tests, but in every live run so far each individual run rendered a value consistently — the variation was *between* runs. So that code path has never fired on real input.

## 8. Lessons learned — debugging and validating against NetSuite

Written up because the permission investigation cost far more cycles than it should have. Items 5 and 6 are about *analysis* rather than permissions, and were added after two conclusions turned out to be artifacts of how the data was chosen.

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

### 5. Choose the denominator that matches the decision

The first duplicate-key analysis sampled POs by **surplus line count** — the worst offenders — which felt like going where the signal was. It was not. Selecting for the most surplus lines selects for POs that accumulated the most amendments, which selects for the **longest-lived**, which selects for **closed**. The resulting headline — "93% of duplicate groups have no open line" — was therefore close to **tautological**, and the entire practical conclusion rested on the single live PO that happened to fall into the sample.

Re-running against **currently-open POs** — the population a packing slip can actually act on — inverted the picture: 24 of 25 groups had exactly one open line, and **every** live group had at least one. Same data, opposite operational reading.

The lesson is not "sample randomly". It is: **the denominator has to be the population the decision applies to.** The decision here was "what should the matcher do when it meets one of these", so the denominator is POs the matcher can still meet.

### 6. One validation target cannot prove a general case, however thoroughly it passes

The whole parsing/matching phase was validated against **PO 8489541 (PO0001662)**. That PO has **41 lines, 41 distinct keys, one delivery date, and every override flag `False`.** It is a clean single-delivery PO — which means it **could not have surfaced the duplicate-key problem** no matter how carefully it was tested. The 77/77 and 20/20 results against it were real, and they said nothing whatsoever about the general case.

Worth being precise about the failure: it was not insufficient rigour, it was **rigour aimed at one specimen**. A structural property of the corpus (does any PO have duplicate keys? do dates vary within a PO?) has to be checked **across** the population — one cheap aggregate query would have found this at the start of the phase rather than at the end of it. Before trusting a validation corpus again, ask what shapes it **structurally cannot contain**.

### 7. A change that requires mass-rewriting correct assertions is probably wrong

A guard was proposed for change 6: compare each slip line's quantity against the
matched PO line's outstanding quantity (`quantity - quantity_received`), and refuse
to propose anything — quantity or date — unless they were equal. The reasoning was
sound in isolation: a NetSuite line holds one quantity and one date, so a line
whose quantity arrives in two batches cannot be represented, and writing a date on
it over-promises stock to RepSpark.

Running it against the suite before committing produced **22 failed checks across
7 test functions, 5 of them crashing mid-run** — and **not one was a real
regression.** Every fixture in the suite used a slip quantity that differed from
the line's ordered quantity, because that difference was the thing being tested.
The guard turned all of them into flags.

That number was the finding. Twenty-two assertions of correct behaviour cannot all
be wrong at once, so the change was. Two errors surfaced by looking at *why* each
one failed, neither visible from the rule itself:

- **It contradicted a settled ruling.** Over-shipment flags under the guard, while
  Paula's ruling 6 (*"there are always extra units that we accept"*) says it is
  normal and must not be flagged. Some of those failing tests asserted that ruling
  by name.
- **It removed the tool's main job, arithmetically.** With `quantity_received = 0`,
  outstanding equals ordered, so "slip equals outstanding" means "nothing to update
  but the date". The tool could only ever have proposed a quantity change on a line
  that already had receipts against it. `demo_matcher.py`, built on the real PO 1662
  case, went from 2 proposals to 0.

And the premise did not hold either: the guard existed to stop a date being written
before Paula saw the slip, but nothing is written without her approval, so there was
no race to prevent. She knows a line split is coming because she arranges the air
shipment herself.

**The habit worth keeping:** when a change makes many tests fail, read the failures
before fixing them. If they were all testing correct behaviour, the blast radius is
telling you about the change, not about the tests. The temptation is to see 22 red
lines as 22 chores. Here it was one piece of evidence, and rewriting the fixtures
would have destroyed it — the tests would have gone green around a rule that had
quietly disabled the feature. What shipped instead was the same numbers with no
gate: `line_balance` on every change, so the review screen shows "ordered 300,
received 0, this slip 128" and the human who can judge it does.

### 8. Validate a configured rule against the whole population, not the setting

The tranId transformation was blocked for weeks on reading three checkboxes in
Setup > Company > Auto-Generated Numbers: Prefix, Minimum Digits, **Allow
Override**, and **Use Subsidiary / Use Location**. Two of those were never captured.

Querying the data settled it in one pass and settled it better: **1,659 of 1,659 PO
tranIds match `^PO\d{7}$`** — one distinct shape, spanning 2021-06-16 to 2026-07-31
and every status, zero non-conforming, zero duplicate numbers, and `"PO" + zfill(7)`
reproduces every one exactly. The 112 gaps in the sequence are ordinary deletions,
not a second format.

**A checkbox says what is permitted; the data says what happened.** The second is
what the code has to handle. And the data answered a question the checkboxes could
not: even if Allow Override *is* enabled, nobody has used it in five years — which
is a stronger statement than "the setting is off", and it came free.

The habit: when a rule comes from configuration, spend the one query to check it
against the whole real population before building on it. It converts a config
reading into a measured fact, it is usually cheap, and it occasionally tells you the
configuration is lying. Keep the pattern check in the code regardless — `TRANID_PATTERN`
stays, because 100% conformance today is not a guarantee about tomorrow, and
re-running the survey against production is a Phase 4 item precisely because
sandbox conformance is not production conformance.

### 9. A new vendor is the only real test of a generalisation

Three vendors were validated, hand-checked, and passing — 8/8 on attachment triage,
77/77 on Inprotex, 100% tranId conformance across 1,659 POs. Then two new vendors
arrived and produced **four** defects before the matcher was reached at all: a
filename that excluded a file unopened, a size detector blind to numbers, a file
format that would not open, and a database key that was not canonical.

None of these was subtle in hindsight, and none was findable from the first three
vendors, because all three happened to be apparel-only, letter-sized, `.xlsx`/PDF,
and single-rendering. The passing tests were not wrong; they were **describing a
narrower population than the one being claimed** — which is the same point as
lesson 6 above, arriving with a bill attached.

Two working consequences:

- **Run a new vendor end to end before writing any code for it.** The diagnostic
  pass on these two files (classify → extract → check sizes against NetSuite →
  hand-verify five lines per document) cost one session and found all four defects
  in one go, including the two that had nothing to do with the vendor's layout.
- **Expect the defects to be upstream of the interesting part.** The layout
  question everyone anticipated (Tainan's two-axis waist/inseam grid) is still
  open and correctly scoped separately. The four things that actually blocked
  these vendors were all in triage, file reading, and key derivation — the parts
  already considered settled.

### 10. A runtime-authoritative table seeded from live code makes behaviour depend on WHEN a database was built

Migration 0001 seeded `change_states` and `change_state_transitions` by importing `schema.py` at migration time. `schema.assert_transition` then reads `change_state_transitions` **at runtime** to decide whether a state change is legal. Put those two together and **a database's behaviour is a function of the date it was migrated, not of its revision** — two databases both reporting `0004` could disagree about what the state machine permits.

**The symptom is that there is no symptom.** `alembic current` reports the same revision on both. `alembic check` compares the metadata to the *table shapes* and is silent about row contents. Nothing in the schema, the migration history, or any drift check distinguishes a correct database from a stale one. The divergence surfaces only as a transition being refused on one machine and allowed on another, arbitrarily far from the cause.

Three properties make a bug this shape, and it is worth recognising the combination rather than the instance:

1. **data written by a migration** (so it is fixed at build time),
2. **sourced from code that keeps changing** (so what gets fixed varies),
3. **read at runtime to make a decision** (so the variation changes behaviour).

Drop any one and it is merely untidy. A migration that imports a *column list* is caught by `alembic check`. A seeded table nobody reads at runtime is dead weight. This had all three.

**The fix is the test, not the freeze.** Freezing 0001's literals stops the drift already in flight; what stops the next one is `test_migration_seed_matches_schema`, which migrates an empty database to head and compares every row against `schema.py` **in both directions** — the reverse direction being the one that catches a *deletion*, which is the case that makes two databases genuinely disagree. Plus `test_migrations_import_no_application_code`, which rejects the pattern at the source rather than detecting its consequences later.

**Generalisation for the Azure move:** anything a migration writes and the application later reads must be a literal in the migration. Seed rows, enum tables, default configuration, lookup values. If it is worth writing once at build time, it is worth pinning to that build.

### 11. A confirmation is only as good as the question it answered

**Three instances now, and in all three the report was honest and the state was wrong.** That is what makes this worth a lesson rather than a grumble: nobody was careless or misleading. Each confirmation answered exactly the question it was asked, and the question was narrower than what the reader took from it.

- **"Is this the right thumbprint?"** IT confirmed on 2026-09-09 that `E05CF5DB…F016` was the certificate for this application, and it was. Hours later the auth probe got `AADSTS700027` — *the key was not found* — because the certificate **was not yet on the registration**. The confirmation established that a string matched. It could not establish that an upload had happened, because that was never the question. (`GRAPH-SETUP.md`, *Certificates on the registration*, carries the timeline.)
- **`SuiteAnalytics Workbook`.** The permission read as set in the role editor across five probe cycles. It had never been *committed* to the role. Every cycle honestly reported what the screen showed; the screen showed intent, not saved state. (Lesson 1.)
- **The footwear totals.** "By Sea 2,040 / By UPS 140 / Ordered 2,180" travelled through briefs into a change specification, read as verified because it had been stated confidently and repeatedly. The workbook says 1,440 / 60 / an unlabelled 600 (§6 item 25).

**Ask for an observation, not an assurance.** "Read X and tell me what it says" rather than "is X correct?" — the first has a wrong answer that shows up, the second returns the answerer's belief. And prefer a probe to either: **`scripts/probe_graph_auth.py` found the missing certificate on its first run**, which is the whole argument for building it standalone *ahead* of the pipeline instead of discovering the same fault inside one. Inside the polling job it would have been one candidate cause among several; standalone it was the only thing that could have failed.

Corollary from the footwear case: **when the code and the spec disagree, check the source before assuming the code is wrong.** The implementation had reproduced the document faithfully; the specification had not.

### 11a. And re-derive, rather than re-quote

The mechanic behind all three: a figure or an elimination repeated from an earlier report **inherits that report's confidence without inheriting any of its evidence**. So when one matters, re-derive it from the source — and say in the report which of the two you did. The tells are cheap to watch for: a figure nobody can point at a cell for, a difference that is suspiciously round, an elimination whose only evidence is another elimination, a confirmation whose subject is a string rather than a state.

### 12. Do not gate a verdict on evidence you cannot read

The auth probe's step (b) checked the token's `roles` claim for `Mail.Read`. **Microsoft Graph access tokens are opaque to the client** — they carry a `nonce` in the JWT header marking Graph's protected format, and the payload a client can decode holds 29 claims with no `roles` and no `scp` at all. The claim is absent **by design**, on a healthy system, always.

So the probe reported a **fully working credential chain as `NOT CLEAN`**: token minted, target mailbox read, second mailbox correctly refused — and a red verdict, because one input to it could never be true.

**A check that cannot pass when the system is healthy is worse than no check.** It gets ignored, which is the best case. Then it gets *trusted* on the day it happens to matter, and by then nobody remembers it was always red. A missing check leaves a known gap; a permanently failing one manufactures a false gap and hides the real state behind it.

**The behaviour was already proving what the proxy was asking.** Step (c)'s `200` establishes that `Mail.Read` was both granted *and* admin-consented, because an unconsented application permission returns `403` on the call itself. The claim inspection added nothing even in principle — it was a worse instrument aimed at a question already answered one step later.

The general form: **prefer a check that exercises the behaviour over one that inspects a proxy for it.** Read the mailbox rather than reading the token that authorises reading the mailbox; write the row and read it back rather than asserting the column exists. A proxy can be unreadable, stale, or renamed by a vendor without notice — and when it disagrees with the behaviour, the behaviour is what is true.

Related: this is the same shape as §7's note that `ls -l` lies about Windows ACLs. In both cases the instrument sat one translation away from the thing being measured, and its output described the translation.

### 13. A signal the tool itself writes is an echo, not evidence

The sharpest trap found so far, and it is invisible unless you ask where a field's value comes from. When the tool needed to pair two shipment rows with two PO lines (§6 item 23), `custcol_override_expected_receipt` and `custcol_sd_updatedreceiptdate` differed within 31 of 73 duplicate groups — the second-best correlation of any field, and semantically plausible: an already-updated line looks like the settled one.

**But this tool writes both fields.** Pairing on them would mean the tool's own past writes decided its future pairings, and the correlation would *strengthen with every run* regardless of whether the first pairing was ever right. A validation set drawn from live data would confirm it beautifully. That is the failure mode: a self-fulfilling discriminator looks better the longer it runs.

The check is one question, and it generalises to anything learned from live data: **would this field have this value if the tool had never run?** If not, it is not evidence about the world; it is a record of what the tool already did. Fields this tool writes — the four in `WRITABLE_LINE_FIELDS` — are permanently disqualified as matching or pairing inputs, and the docstring says so at the point of temptation rather than here.

Corollary worth keeping: **the most convincing-looking candidate deserves the most suspicion**, because plausibility is exactly what stops anyone checking provenance.

### 14. Describe removed sensitive data by CATEGORY, never by value

**The hygiene commit is the likeliest place for the data to survive, because you are writing about exactly what you took out.** This is not a hypothetical: the 2026-09-02 commit that moved four third-party files out of the working tree **transcribed all four categories verbatim** into its own commit message *and* into the RUNBOOK entry recording the move — a retailer's name, a MID code, a bank account number and a SWIFT code. The tree was clean and the permanent record was not. Caught only because a later audit grepped the unpushed commits rather than trusting the earlier "moved it out" report; fixed by rewriting all seven unpushed commits before anything was pushed.

The rule, and it costs nothing: write **"a US retailer as consignee"**, **"a MID code"**, **"the freight agent's bank, account number and SWIFT"**. Every operational point survives — which file, which category, which party role, why it matters — and no identifier does. The entry exists to establish a *pattern*; the digits were never the point.

Two corollaries, both learned the same day:

- **Grep the messages, not just the trees.** `git log --name-only` says nothing about content, and a commit message is as permanent as a blob and is not covered by `.gitignore`, a file move, or a cell-level scan of the working tree. The audit that found this ran `git log origin/main..HEAD --format=%B | grep -i` over every unpushed commit.
- **A scan for third-party data must cover PARTY NAMES as well as ACCOUNT IDENTIFIERS.** The same audit initially reported the fixtures clean because its needles were numbers, SWIFT codes and MID codes. A second pass for company names found the freight forwarder's name sitting in a `Shipped Per` header cell on all three footwear packing sheets — `B22`, well outside the bank block anyone would think to check. An invoice hides its identifiers in an obvious place and its names in ordinary fields.

The unpushed window is the whole of the cheap-fix opportunity, so **audit before pushing, not after**. Rewriting seven local commits took minutes; the same content on a shared remote is a coordination problem plus a disclosure question.

### 15. Provenance stays true; a prediction goes stale

A sweep for outdated cross-references found ~35 mentions of "Phase N" across the tracked Python. **Five needed changing. The other ~30 were correct and had to be left alone.** The distinction is not how old the line is or how confident it sounds — it is what kind of claim it makes:

- **Provenance** — *which phase built this, and why it exists.* `"Persist a parsed shipment into the database (Phase 2, item 4)"`; `"There is no Graph client yet (Phase 2 item 2)"`; `"For Phase 1 this is the specific finding we're hunting"`. These stay true as the project moves, because they describe a fact about the past that later work does not alter. They are also the useful ones: they tell a reader *why* a thing is shaped as it is.
- **Prediction** — *what happens next.* `"Next: Prompt 2 in Claude-Code-Kickoff-Prompts.md (the parsing layer)"` — printed on every pass of the write-back test, pointing at a parsing layer built a month earlier. `"Resolve with your NetSuite admin before Phase 2; do not widen the role unilaterally"` — advice for a problem solved in August, which would send a reader to re-derive a known answer. `"has not been run against a single real vendor document"` — false since the day it was written into a green run.

**A prediction has an expiry date it does not carry.** Nothing in the line says when it stops being true, and nothing fails when it does — the write-back test kept passing while printing a stale next step, and the parsing suite printed its false warning on a green run for weeks.

**This is what makes the sweep possible without being mechanical.** A blanket find-and-replace on "Phase" would have touched all 35 and damaged the 30 that were right; reading all 35 closely is affordable exactly once. The grammatical tell does the filtering: *past tense about why this exists* → keep; *imperative or future tense about what to do next* → check it, and delete it if the thing it points at has happened. A line that names a phase as the **reason for a decision** is documentation. A line that names a phase as a **destination** is a to-do that nobody scheduled.

Corollary, and the reason this is a lesson rather than a tidy-up: **the stale ones concentrate in output**, not in comments. Banners, closing lines, and "next step" hints are written once at the end of a task, when the next step is vividly in mind and least likely to stay true. Comments explaining a design decision get re-read whenever the code is touched; a print statement at the bottom of a passing test is read by nobody who is in a position to notice it is wrong. Same shape as §8 lesson 12 — a check that cannot pass on a healthy system — and the two were found in the same sweep.

### 16. Ask of every sandbox-derived fact: if production disagreed, would anything say so?

Three facts in this pipeline were learned from sandbox, baked into matching, and are re-checked nowhere at runtime:

| Fact | Learned from | Where it decides something |
|---|---|---|
| tranId is `'PO' + zfill(7)` | 1,659 sandbox POs, 100% conforming | every PO-number-to-internal-id lookup |
| Colour resolves via `custitem_psgss_product_color_desc` | 2,390 of 2,393 sandbox items populated | every colour match |
| The size vocabulary | `netsuite_size_list.json`, 46 sandbox values, one day in September | what counts as a size header at all, and whether a composed `30-32` is accepted |

**All three fail the same way, and it is the worst available way: silently, as a non-match rather than an error.** That is not a coincidence — it follows from where they sit. Each is an input to *matching*, and matching's failure mode is producing **fewer rows**, never raising. A size absent from the vocabulary is indistinguishable from a cell that was never a size. A colour that does not resolve looks exactly like a line the vendor did not ship. Nothing in the output says "I could not read this"; the tool simply proposes less and reports success.

**The test is one question: *if production disagreed with this, would anything say so?*** If the answer is no, the fact belongs on the Phase 4 re-verification list. It is deliberately not "is this fact likely to be wrong" — likelihood is exactly what nobody can assess about an account they have not read, and the colour list has already been shown to differ between the two accounts in **both** directions.

**Recognising the fourth is the point of naming the shape.** Three separate warnings read as three pieces of trivia; one named shape is something a reader can apply to a fact discovered next month. The pipeline learns something new about this account's data every time a vendor arrives, and each new thing learned from sandbox joins this list by default until someone checks it against production.

Related: this is the same failure geometry as §8 lesson 12, approached from the other end. There, a check was red on a healthy system and so got ignored. Here, the system is green while silently doing less work. **Both are cases where the absence of an error was read as evidence, and in neither case was it.**

### 17. A test that compares raw strings where production compares canonical forms is testing a different system

It is not a stricter test or a looser one. **It is an assertion about a system that merely shares code with the one shipping**, and it will disagree with reality in both directions: failing on differences the pipeline erases, and — the half nobody notices — passing on differences it would not.

**How it presented.** `test_live_symmetry` asserted `detail == rollup` on a dict keyed by raw `(po, style, colour, size)`. It failed about once in ten runs, reporting **twelve quantity disagreements**. Every quantity was identical. Both documents totalled 1,669 in all ten runs, 25 keys each. The entire difference was `NEW INDIGO` versus `NEW  INDIGO` — a dict comparison renders one respelled colour as six missing keys and six extra ones, and the failure output looks exactly like a numeric catastrophe (§7 has the whitespace mechanism).

**Nine passing runs made it look like noise**, which is the dangerous part. A flake that fires 1-in-10 gets re-run rather than read; the second run is green and everyone moves on. It was on a direct path to §8 lesson 12 — a check that fails on a healthy system gets ignored, and then gets trusted on the day it matters. The only reason it was caught is that ten deliberate runs were cheaper than the uncertainty.

**The fix is to key on what production keys on**, and it is *stricter*, not looser: `po_number_key` for the PO and `canonical` for style, colour, size and recap label — the key in `extraction_schema.aggregate_lines`. The raw renderings stay under test **separately**, as a set that must all collapse to the expected canonical forms, so a genuine colour change, truncation or dropped word still fails while whitespace does not. Same split the PO number already used for the same reason: its printed form varies between runs (`1624` / `PO0001624`) while its identity does not.

**Deliberately not `matcher._size_key`.** That resolves `SIZE_ALIASES` first, which is a vendor-to-NetSuite mapping rather than an identity function — an extractor emitting `XXL` where the sheet prints `2XL` would key alike and pass. An extraction test mirrors the extraction side of the pipeline; a matcher test mirrors the matcher's. **Pick the layer you are testing and use that layer's identity, not the most forgiving one available.**

**The latent exposure is the real lesson.** Three other fixtures were keyed the same way and escaped only by accident: Legendz's and footwear's colours (`DFK`, `MLT`, `PAT`, `WHT`) are single tokens with no internal gap, and Tainan's `NEW INDIGO` arrives from `.xls` **cell values**, which never pass through the PDF layout renderer. Not one of them was safe by design. Any future PDF vendor with a two-word colour walks straight into it — so all four were converted, including the three that could not currently fail.

The general question, worth asking of any equality assertion: **what does the system consider these two things to be?** If the answer is "the same", and the test says otherwise, the test is wrong even while it is red.

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
