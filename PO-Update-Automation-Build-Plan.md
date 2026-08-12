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
4. Build the Claude-assisted extraction path (Anthropic API) as the primary parser for vendor packing slips — required now that every vendor's layout is confirmed different, not optional. Keep `parse_packing_slip.py` as the fast/free path specifically for Inprotex, with unit tests against the sample files we already have. Route any low-confidence extraction to the review step flagged as such, rather than guessing.
5. ~~Add a size-code normalization step to matcher.py~~ **Already implemented** (`SIZE_ALIASES`).
6. ~~Step 8 "Web Services Only Role" experiment~~ **Done, confirmed 2026-08-04:** checked the box, re-ran the test, still a clean pass. Keep it checked going forward — see architecture doc §6.

**Exit criteria:** ~~can programmatically read a sandbox PO and update one item line's quantity and dates via script under the real M2M-authenticated "PO Update" role specifically~~ **MET.**

**New finding carried into Phase 2:** the "PO Update" role can read individual PO records by internal id but **cannot perform collection-level GET/search queries** (`GET /purchaseOrder?limit=1` and similar all return `400 USER_ERROR`, despite View-level List permissions). This blocks resolving a human PO number ("1662") to its NetSuite internal id ("8489541") — needed by the matcher from Phase 2 onward, since vendor packing slips only carry PO numbers. Needs a NetSuite admin conversation about which permission grants collection/search access; see architecture doc §6.

**Before Phase 3 locks in the diff engine's behavior**, get Paula's input on the three business-logic questions in architecture doc §6.1 (vendor-date-to-NetSuite-field mapping and transit buffer, re-shipment/split-shipment semantics, and handling of lines absent from a given packing slip). Conservative defaults are documented there for each, but they're inferred, not confirmed — don't let the diff engine silently ship on inferred behavior for these.

## Phase 2 — Email intake + data layer (~3–5 days)

1. Register an Azure AD app (Graph API, `Mail.Read` app-only permission) against the relevant mailbox.
2. Build the intake job as polling (confirmed sufficient at 10–20 emails/week — no webhook needed), pulling new messages + attachments from a designated folder every 15–30 minutes.
3. Stand up the database (SQLite to start) with the `shipments` / `proposed_changes` / `audit_log` tables from the architecture doc.
4. Wire parsing output into the database as `PENDING_REVIEW` records — no NetSuite write yet, no UI yet. Verify with real historical emails.
5. **Blocked on the collection/search permission finding above** until resolved with NetSuite admin: PO-number-to-internal-id resolution (`resolve_po_internal_id()` in `netsuite_client.py`) needs collection/query access the current role doesn't have. Don't work around this by widening the role without checking first — get the specific minimal permission from the admin conversation instead.

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
- **New risk surfaced by that same test:** the "PO Update" role can't do collection-level REST queries (search/list), only direct-by-id reads. Since PO-number-to-internal-id resolution needs exactly that, this blocks Phase 2's matcher until a NetSuite admin identifies the right permission. Same category of risk as the original permission-scoping problem — worth getting ahead of rather than discovering again mid-Phase-2.
- **Vendor format diversity — now the single biggest unproven assumption in the design (2026-08-04).** The Claude-assisted extractor (`claude_extractor.py`) is built, has 101 passing tests, and is well-engineered (structured Pydantic output, confidence/needs_review flags, chunking instead of truncation) — but every one of those tests runs against synthetic fixtures or mocks. **It has never been run against a single real vendor document.** The whole design rests on it generalizing to layouts nobody has seen yet; right now that's an assumption, not a result. Get 2-3 real, non-Inprotex vendor packing slips from Paula before Phase 2 locks in the matching/staging behavior around this extractor's output — this is no longer a "nice to have early," it's the binding constraint on whether the primary parsing path actually works.
- **NetSuite account permissions:** creating a new Integration record + custom role with M2M grant requires NetSuite admin access — same kind of access that was needed (and initially missing) this session. Confirm this is available before Phase 1 starts, or it becomes the same blocker again in a new form. **Now a recurring pattern, not a one-off:** this session has already hit three separate permission gaps (AI Connector Service scope, OAuth Access Tokens login permission, collection/search access) — budget for a real conversation with the NetSuite admin covering all of them at once rather than discovering each one serially.
- **Unconfirmed business logic driving the diff engine:** the exact date-mapping/transit-buffer rule, split-shipment semantics, and handling of lines absent from a packing slip are all still inferred defaults, not confirmed with Paula (architecture doc §6.1). Getting any of these wrong means the pipeline proposes plausible-looking but incorrect changes — exactly the kind of error the review step exists to catch, but worth resolving properly rather than leaning on review to catch a systematic mistake every time.
- **Silent failures:** an unattended pipeline that quietly stops working is worse than a manual process — Phase 4's monitoring/alerting isn't optional polish, it's what makes "unattended" safe to trust.
