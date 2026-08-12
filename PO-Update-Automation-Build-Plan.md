# PO Update Automation — Build Plan, Timeline & Cost

Companion to `PO-Update-Automation-Architecture.md`. Read that first for the "what" and "why" — this doc is the "how long, how much, in what order" for handing off to Claude Code.

**Assumptions this plan is built on** (flag/correct these before treating the estimates below as firm):
- **Confirmed by Paula:** shipment volume is 10–20 vendor emails/week. Low enough that polling (not a real-time webhook) is the right call for intake — see architecture doc §4.1.
- **Confirmed by Paula:** every vendor sends a completely different packing slip layout — there is no "mostly one format" shortcut. This means the Claude-assisted extractor needs to be built as the primary parsing path from Phase 1, not a nice-to-have added later, and Inprotex's deterministic parser (already built and validated) is the exception, not the template.
- One approver (Paula) at launch.
- Builder is Kiko, using Claude Code, working solo (not a dev team) — timeline is in elapsed calendar time assuming part-time/interleaved focus, not a full-time contractor's 40hr/week clock.

---

## Phase 0 — Done (this session)

- Parsing logic validated against a real vendor file and cross-checked by hand (100% match on 77 line items).
- NetSuite role/permission/scope requirements diagnosed in detail (see architecture doc §6).
- Decisions locked: sandbox-first, permanent human review step, Outlook-based intake.

No additional time/cost — already sunk and reusable.

## Phase 1 — NetSuite integration proof-of-concept (~3–5 days) — FULLY COMPLETE 2026-08-04

1. ~~Create a new NetSuite Integration record (sandbox) using OAuth 2.0 Client Credentials (M2M) grant, scope = REST Web Services~~ **Done.**
2. ~~Create the dedicated "PO Update" role and generate/assign a certificate for M2M auth~~ **Done** — five permissions total, including "Log in using OAuth 2.0 Access Tokens" (confirmed required, see architecture doc §6).
3. ~~Confirm end-to-end: authenticate via certificate, read a real PO record, update an item line under the actual least-privilege role~~ **Done and passed.** `test_phase1_writeback.py` exited 0 under real M2M auth as the "PO Update" role: read PO 8489541 line 18, wrote all four fields (quantity, expectedReceiptDate, custcol_override_expected_receipt, custcol_sd_updatedreceiptdate) in one call, verified each individually, reverted cleanly. The CFO-vs-least-privilege-role question is closed — no difference, no field-level restriction blocks this role.
4. ~~Build the Claude-assisted extraction path (Anthropic API) as the primary parser for vendor packing slips~~ **COMPLETE 2026-08-12, validated against real vendor documents.** Evidence, not assertion:
   - **20/20 hand-verified line items exact** across four structurally unrelated layouts (Inprotex xlsx, Legendz xlsx, Symmetry rollup PDF, Symmetry carton-detail PDF) — fields compared one by one against the source documents, not against self-reported confidence.
   - **77/77 exact against the deterministic reference** on the Inprotex `PACKING` sheet: 0 missing, 0 extra, 0 quantity disagreements, 6387 units. That reference is itself the parser hand-checked 77/77 against the vendor's own summary email.
   - **Two independent Symmetry documents agreeing to the unit** — a style/colour/size rollup and a carton-by-carton detail, both 25 keys / 1669 units, matching the documents' own printed `G.TOTAL`. The detail required summing carton rows and carrying colour labels across a page break.
   - **`attachment_classifier` 8/8** on the real eight-document corpus, including overriding a filename that said "PACKING LIST" on a document that is actually a commercial invoice — and accepting Inprotex's `Invoice_Packing.xlsx`, which is the same trap in the opposite direction.
   Low-confidence rows route to review flagged rather than guessed, as specified.
5. ~~Add a size-code normalization step to matcher.py~~ **Already implemented** (`SIZE_ALIASES`).
6. ~~Step 8 "Web Services Only Role" experiment~~ **Done, confirmed 2026-08-04:** checked the box, re-ran the test, still a clean pass. Keep it checked going forward — see architecture doc §6.

**Exit criteria:** ~~can programmatically read a sandbox PO and update one item line's quantity and dates via script under the real M2M-authenticated "PO Update" role specifically~~ **MET.**

**Four code changes landed after the extraction validation (2026-08-12), each verified before the next started:**

| # | Change | Acceptance |
|---|---|---|
| 1 | **Per-sheet classification for workbook sheet selection.** A multi-sheet workbook is N documents in a container; `extract_workbook` had been mining every sheet, so Inprotex's four-sheet file produced 42 sizeless lines from its three invoice sheets and a 4× inflated total. Sheets now go through the existing content-based classifier; only packing-list sheets are extracted. | Inprotex `force='claude'` → **77 lines / 6387 units / 0 sizeless**, 3 sheets skipped with logged reasons. Token cost **−34% input, −49% output** (36,221→23,958 / 17,011→8,642). One classification call per workbook, none for single-sheet workbooks. |
| 2 | **Deterministic aggregation by semantic key.** Collapses duplicate (PO, style, colour, size) rows within one document and sums quantities — correct semantics for carton-level documents, and it removes retry nondeterminism. Confidence takes the worst of the merged rows; sizeless rows are never collapsed. | Symmetry detail PDF **stable at 25 lines / 1669 units across five consecutive runs**. |
| 3 | **`NoPackingSheetFound` → `needs_manual_entry`.** An email producing no reviewable artifact is the failure mode this architecture exists to prevent, so a workbook with no packing sheet becomes the manual-entry outcome with **per-sheet diagnostics preserved intact** (every sheet with its predicted type), not an exception nobody sees. | No-packing-sheet workbook returns `needs_manual_entry`, 0 lines, both sheets named with predicted types. |
| 4 | **Canonical-form normalizer** (`canonical.py`): NFKC → dash folding → zero-width handling → whitespace collapse → strip → casefold, applied at *every* keying and matching site, on **both** operands. Replaces ad-hoc `.strip().upper()`. Verbatim source text is preserved; only the comparison key is derived. | Symmetry detail **stable at 25 / 1669 with identical row order across five consecutive runs** — completing idempotency. A colour printed `NEW  INDIGO` now matches NetSuite's `NEW INDIGO`, and a dirty *NetSuite* value matches a clean vendor one. |

Test totals at this point: **394 offline + 94 NetSuite client**, with the eight real vendor documents tracked in the repository as the validation corpus.

~~**New finding carried into Phase 2:** the "PO Update" role cannot perform collection-level GET/search queries.~~ **RESOLVED 2026-08-12** — the role was missing `Reports > SuiteAnalytics Workbook`, confirmed by bisect as the sole cause. Collection `GET`, `?q=` filtering and SuiteQL are all gated by that one permission while by-id `GET`/`PATCH` is not. Architecture doc §6 has the full permission set and the diagnostic signature.

**Before Phase 3 locks in the diff engine's behavior**, get Paula's input on the three business-logic questions in architecture doc §6.1 (vendor-date-to-NetSuite-field mapping and transit buffer, re-shipment/split-shipment semantics, and handling of lines absent from a given packing slip). Conservative defaults are documented there for each, but they're inferred, not confirmed — don't let the diff engine silently ship on inferred behavior for these.

## Phase 2 — Email intake + data layer (~3–5 days)

1. Register an Azure AD app (Graph API, `Mail.Read` app-only permission) against Paula's mailbox. **Confirm admin access BEFORE starting.** App registration plus granting app-only `Mail.Read` requires **Entra ID admin rights** (Application Administrator or Global Administrator) and tenant-wide admin consent — Kiko may not have these. The risk register already names missing admin access as a recurring pattern in this project: it blocked the NetSuite Integration record, then the role permissions, twice. Identify the admin and confirm the access exists as the first task of Phase 2, not mid-debug.
2. Build the intake job as polling (confirmed sufficient at 10–20 emails/week — no webhook needed), pulling new messages + attachments from a designated folder every 15–30 minutes.
3. Stand up the database (SQLite to start) with the `shipments` / `proposed_changes` / `audit_log` tables from the architecture doc.
4. Wire parsing output into the database as `PENDING_REVIEW` records — no NetSuite write yet, no UI yet. Verify with real historical emails.
5. ~~Blocked on the collection/search permission finding~~ **UNBLOCKED 2026-08-12** — `resolve_po_internal_id()` works now that the role has `Reports > SuiteAnalytics Workbook`. Both `?q=` quoting forms confirmed equivalent.
6. **NEW — tranId format transformation.** Resolution works, but not from the number vendors print. Vendors print the bare number (`1662`); NetSuite stores `PO0001662`, and querying the bare number returns `200` with `totalResults=0` — a *successful* empty result that reads as "PO not found". So Phase 2 needs a transformation (extract digits → zero-pad → prefix) in front of the query. **Prerequisites before writing it:** read Setup > Company > Auto-Generated Numbers (Prefix, Minimum Digits, Allow Override, Use Subsidiary/Location), validate against several hundred real tranIds, and confirm with Paula/Brandon. Do not hardcode `"PO"` + 7 digits from one sample. If Allow Override is on, tranIds are a convention not a guarantee, so a non-conforming value must be a defined flagged outcome. Note the extraction side too: a bare four-digit number needs column-header context — carton counts, quantities and style fragments look identical to a page-wide regex. Full detail in RUNBOOK §6.

**Exit criteria:** forwarding/receiving a real vendor email results in a correct, persisted `proposed_changes` row set, matched against sandbox PO data from Phase 1.

## Phase 3 — Review/approval + write-back (~4–6 days)

1. Build the review step — recommend starting with the **email-approval** version (a digest email with an approve link) rather than a full dashboard, to keep v1 scope small; upgrade to a small web UI later if Paula wants richer interaction.
2. Wire the approval action to the NetSuite write-back built in Phase 1.
3. Error handling: what happens if NetSuite rejects a write (e.g., closed PO, permission error) — should surface clearly, not fail silently.
4. Confirmation notification back to Paula (and Kiko) on success/failure.

**Exit criteria:** end-to-end flow works against sandbox — email in, review link out, approval in, NetSuite sandbox PO updated, confirmation out.

## Phase 4 — Hardening & production cutover (~3–5 days)

1. Run against a backlog of real historical vendor emails (if available) to shake out edge cases beyond the one sample we've tested.
2. Move from sandbox to production NetSuite Integration record/role (mirroring Phase 1 setup).
3. Basic monitoring/alerting (e.g., "intake job hasn't run in X hours," "NetSuite write failed") so silent failures don't sit unnoticed.
4. Short handoff doc for Paula (how to use the review/approval step) and for whoever maintains it after Kiko.

**Exit criteria:** running unattended against production for a real shipment, successfully, with Paula using the actual review step (not Kiko testing it).

---

## Timeline roll-up

| Phase | Elapsed estimate |
|---|---|
| 1 — NetSuite POC | 3–5 days |
| 2 — Email intake + data layer | 3–5 days |
| 3 — Review/approval + write-back | 4–6 days |
| 4 — Hardening + cutover | 3–5 days |
| **Total** | **~3–4 weeks** elapsed, part-time |

This assumes Phase 1's remaining validation (confirming the least-privilege "PO Update" role, not just CFO, can perform the same NetSuite write) goes cleanly, and that Paula's answers on the §6.1 business-logic questions (date mapping/buffer, split shipments, absent lines) arrive before Phase 3 needs them. It extends if the least-privilege role turns out to need a RESTlet after all (unlikely, but not yet proven), or if the Claude-assisted extractor needs real tuning against a second/third vendor format before it's trustworthy.

## Cost estimate

This is being self-built with Claude Code rather than contracted out, so "cost" is mostly time plus a few small recurring line items:

| Item | Estimate | Notes |
|---|---|---|
| Build time | ~3–4 weeks part-time (Kiko + Claude Code) | No dollar figure — this is the main cost, paid in time, not fees |
| Hosting | ~$5–20/month | Azure Functions (Consumption plan) + Azure SQL Database (serverless tier) — decided 2026-08-05, see architecture doc §4.2. Pay-per-execution, no idle VM cost; workload is low-volume and not latency-sensitive |
| Anthropic API usage | Low — likely single-digit $/month at current volume | This is now the *primary* extraction path (not a fallback — every vendor's layout differs) plus change summaries, a few short calls per shipment; recheck if volume or vendor-format diversity grows a lot |
| NetSuite | $0 incremental (confirmed workaround applied) | REST Web Services / Integration records are typically included in standard NetSuite accounts. **Confirmed:** a dedicated M2M service-account Employee record would have consumed a paid user license — avoided by attaching the "PO Update" role to Kiko's existing employee instead (see architecture doc §6 for the audit-trail trade-off this accepts) |
| Microsoft Graph API | $0 incremental | Covered under the existing M365 subscription; app registration itself is free within your tenant, assuming admin access to Azure AD/Entra ID |

**Caveat:** hosting and Azure/NetSuite licensing specifics should be confirmed with whoever manages those accounts — the figures above are typical ranges, not quotes.

## Risks

- ~~**Biggest unknown:** whether NetSuite's standard REST API supports item-line sublist edits directly, and whether the least-privilege role specifically can do it.~~ **Fully resolved 2026-08-04** — live-tested under real M2M auth as the actual "PO Update" role, not just CFO. All four target fields write correctly, no field-level restrictions, no RESTlet needed.
- ~~**New risk surfaced by that same test:** the "PO Update" role can't do collection-level REST queries.~~ **RESOLVED 2026-08-12** — missing `Reports > SuiteAnalytics Workbook`. Worth keeping the meta-lesson, though, because it recurred exactly as predicted: this was the *third* time missing admin access or an unidentified permission blocked progress. RUNBOOK §8 records the debugging method that finally settled it (Show Role Differences, which shows actual rather than intended state) — use it first next time.
- ~~**Vendor format diversity — the single biggest unproven assumption in the design.**~~ **LARGELY RESOLVED 2026-08-12.** The extractor has now been validated against **three real vendors** with materially different layouts, hand-verified line by line (see Phase 1 item 4 for the evidence). The claim that "it has never been run against a single real vendor document" is **no longer true** and has been struck. Test count corrected: **394 offline + 94 NetSuite client**, not 101, and the real vendor documents are now part of the suite rather than mocks. **What remains open** is narrower and worth stating precisely: three vendors is not thirty, every new vendor is still a new layout, and **no document in the corpus contains a non-letter size** — so `SIZE_ALIASES` coverage for the 39 numeric/waist-inseam/shoe values in NetSuite is still untested. The targeted ask is now "which vendors ship numerically-sized styles (pants/bottoms/footwear)?" rather than "send more packing slips".
- **NetSuite account permissions:** creating a new Integration record + custom role with M2M grant requires NetSuite admin access — same kind of access that was needed (and initially missing) this session. Confirm this is available before Phase 1 starts, or it becomes the same blocker again in a new form. **Now a recurring pattern, not a one-off:** this session has already hit three separate permission gaps (AI Connector Service scope, OAuth Access Tokens login permission, collection/search access) — budget for a real conversation with the NetSuite admin covering all of them at once rather than discovering each one serially.
- **Unconfirmed business logic driving the diff engine:** the exact date-mapping/transit-buffer rule, split-shipment semantics, and handling of lines absent from a packing slip are all still inferred defaults, not confirmed with Paula (architecture doc §6.1). Getting any of these wrong means the pipeline proposes plausible-looking but incorrect changes — exactly the kind of error the review step exists to catch, but worth resolving properly rather than leaning on review to catch a systematic mistake every time.
- **Silent failures:** an unattended pipeline that quietly stops working is worse than a manual process — Phase 4's monitoring/alerting isn't optional polish, it's what makes "unattended" safe to trust.
