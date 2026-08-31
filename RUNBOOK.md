# PO Update Automation — Handoff Runbook

**Purpose:** so Brandon (or anyone else) can understand, operate, and fix this system without Kiko in the room — per Beth's discovery follow-up (2026-08-05), condition of moving past sandbox testing.

**Audience:** assumes general technical competence, no prior context on this specific project. Where more depth exists elsewhere, this doc points to it rather than repeating it — `PO-Update-Automation-Architecture.md` is the full design rationale; this doc is the "how do I actually run/fix/hand this off" companion.

**Last updated:** 2026-08-31

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

    **Scoping is what makes it safe**, and the measurement says so. Globally the item data has collisions, but only just: one name maps to two codes (`'Navy / Silver'` → `NAV` and `NVSL`) and five codes have items that disagree on spelling (`FUS`: Fuchsia/Fucshia, `MLK`: MilkShake/Milkshake, `CHC`: Charcoal/Charcoal Heather, `NAV`: Navy/Navy / Silver, `NIN`: NIN/New Indigo). **Per PO there is not one collision across all 133 open POs** — median 3 colours per PO, max 20. `BLACK` versus `Blackcurrant` is the shape that would defeat a fuzzy matcher and is trivially unambiguous when the only colours in the room are BLK, COC and NIN. If two colours on one PO ever do collide, the change flags with both candidates and picks neither.

    **Correction to an earlier figure, which should not be quoted:** a previous probe reported "51 codes carry multiple descriptions" and a value holding `'Black'`, `'INDe'` and `'Indigo'`. That came from joining the colour list to `item.custitem_psgss_product_color`, which is **empty on child matrix items** — an invalid join. The correct pairing takes the code from the PO line (`custcol_product_color`) and the name from the item, and gives the much cleaner figures above.

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

## 7. Design constraints discovered by testing

These are not open questions — they are settled constraints that later phases must respect. Each was found by measurement, not design review.

**Row identity must be the canonical key, never a digest of the whole row.** Verbatim display text varies between runs *by design*: the extractor may render a colour `NEW INDIGO` on one run and `NEW  INDIGO` on the next, and the pipeline deliberately preserves what was printed rather than rewriting it. In one five-run comparison, 6 of 25 rows differed in displayed text while keys, quantities, counts and row order were identical. A row-digest idempotency check on `proposed_changes` would therefore see spurious changes on every re-parse. Key on the canonical form (`canonical.py`).

**The confidence signal is currently INERT for triage.** `needs_review` came back `True` on all four real documents — including the ones that were subsequently hand-verified as flawless. A flag that always fires carries zero information. Two things follow: Phase 3 must **not** build its review queue on `needs_review` as-is, and the signal is **uncalibrated rather than proven safe** — zero extraction errors were observed across 20/20 hand-verified line items, so the false-negative rate is unmeasured, not zero. Calibrating it needs a corpus with known-bad documents.

**A shipment is not 1:1 with a PO, and the fan-out is larger than assumed.** One Inprotex sheet interleaves **six** POs (1640, 1645, 1650, 1662, 1667, 1704); the Symmetry set spans two (1720, 1721). Consequences for Phase 2/3:
- the `shipments` / `proposed_changes` schema must model one email spanning many POs,
- the Phase 3 **approval unit** must be defined deliberately — per PO, per shipment, or per line — rather than falling out of the implementation,
- **write-back needs partial-failure semantics**: one approval can mean six PO writes, and the fifth can fail. What the audit log records, and what Paula sees, when three succeeded and one didn't, has to be decided before the write path is wired.

**`transactionLine.isclosed` is NOT the complement of `isOpen`, and reading it as such produces confidently wrong numbers.** The per-line Closed checkbox is effectively unused in this account — nobody ticks it — so SuiteQL `isclosed = 'F'` reports the lines of a **Fully Billed** PO as fully open. That produced a stated count of **~1,024 "open POs"** which was simply wrong, and it was wrong in the most dangerous direction: a plausible number, quoted with confidence, that no one would think to question. **Use PO status for the business meaning of "open", and REST's per-line `isOpen` as the usable flag** — `isOpen` was present on all 367 lines of the 25 most recent sandbox POs. `POLine` now models both fields separately, with the trap named on the field itself, because a line can be **neither** open nor closed.

**SuiteQL SILENTLY IGNORES a SQL `OFFSET` clause.** `... ORDER BY id OFFSET 1000 ROWS FETCH FIRST 1000 ROWS ONLY` returns the **first** page every time — `FETCH FIRST` is honoured, `OFFSET` is not. A paging loop written that way never advances and never terminates: the first attempt at this ran for ten minutes, cheerfully fetching page one at offsets up to 505,000. Nothing errors, so there is no failure to notice.

**Page with the endpoint's own query parameters instead** — `POST /query/v1/suiteql?limit=1000&offset=1000` — which works correctly and returns `totalResults` and `hasMore` to loop on. Keyset pagination (`WHERE id > :last ORDER BY id FETCH FIRST 1000`) also works and is the safer habit for a long-running job, since it cannot drift if rows are inserted mid-scan. Both were verified: 1,659 POs in two pages either way.

**Also worth knowing:** a `LIKE 'PO%'` scan over the whole `transaction` table (no type filter) does not return in ten minutes. Filter by `type` first.

**SuiteQL is not dependable for aggregates. Keep queries narrow.** `GROUP BY status` and `GROUP BY (tranid, id, item) HAVING COUNT(*) > 1` returned **HTTP 500 with three distinct error ids** across repeated attempts, while per-status `COUNT(*)` and a narrower `GROUP BY t.tranid` succeeded instantly. So the failure is about query shape, not load or permissions. **Phase 2 must not lean on aggregate SuiteQL**; compute aggregates client-side from narrow reads. One thing that did work exactly as designed: the change-3 transient-retry handler retried the 500s with backoff and then **reported the failure**, rather than silently returning an empty result set — demonstrated on a real fault rather than a simulated one, which is the harder test to arrange.

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
