# Claude Code Kickoff Prompts

Copy/paste these into Claude Code, one at a time, in order. Each one corresponds to a
phase in `PO-Update-Automation-Build-Plan.md` — don't start the next prompt until the
previous phase's "exit criteria" (in that doc) are actually met, not just "looks done."

Open this whole `PO Agent` folder in VS Code / Claude Code so `CLAUDE.md` loads
automatically as project context — these prompts assume it has.

---

## Prompt 0 — Orientation (run this first, always)

```
Read CLAUDE.md, PO-Update-Automation-Architecture.md, and
PO-Update-Automation-Build-Plan.md in this folder in full.

Summarize back to me in a few bullet points:
- The core problem and the human-review guarantee that must never be removed
- The confirmed NetSuite field mapping and write-path (section 6 of the
  architecture doc) so I know you've actually absorbed it, not skimmed it
- The confirmed vendor-diversity and volume findings and how they changed the
  parsing/intake design
- Anything in these docs that's ambiguous or that you'd want confirmed before
  writing code

Don't write any code yet -- I want to confirm you're aligned before Phase 1 starts.
```

---

## Prompt 1 — Phase 1: NetSuite integration (M2M auth + write-path)

Some of this phase is manual NetSuite UI work only I can do (creating the
Integration record and role requires clicking through NetSuite as an admin).
This prompt splits it accordingly.

```
We're starting Phase 1 of the build plan: the NetSuite integration
proof-of-concept, using OAuth 2.0 Client Credentials (Machine-to-Machine)
grant -- not the interactive browser flow, see architecture doc section 6 for
why.

1. Generate the RSA keypair needed for NetSuite's M2M OAuth (JWT bearer
   assertion flow). Explain what to do with the public key/certificate --
   I'll upload it into NetSuite's UI myself when creating the Integration
   record and role (walk me through exactly what to click, referencing the
   permissions already worked out in architecture doc section 6: Purchase
   Order Edit, Items View, Vendors View, REST Web Services Full). Note on
   "Web Services Only Role": the interactive flow needed it unchecked, but
   for M2M it's likely correct to check it (no legitimate reason this
   service-account role should ever support interactive login) -- this
   hasn't been empirically verified against our account yet, so flag it as
   something to confirm during setup, not something to assume.

2. Once I give you back the Account ID, Consumer Key/Client ID, and
   Certificate ID from that NetSuite Integration record, implement a real
   NetSuiteClient class (replacing the stub in netsuite_client.py) that
   authenticates via the M2M/JWT flow and can call NetSuite's REST Record
   API for get and update operations on purchaseOrder records.

3. Write a test script that mirrors the exact read/write/revert test already
   validated manually against sandbox PO 8489541 (PO# 1662, line 18,
   style M120246-TID-3X): read the line, update quantity,
   expectedReceiptDate, custcol_override_expected_receipt, and
   custcol_sd_updatedreceiptdate together, read it back to confirm every
   value changed, then revert to the original values. IMPORTANT: that
   earlier test ran under the CFO role as a temporary Cowork-connector
   workaround, NOT the least-privilege "PO Update" role -- it proves the API
   mechanically supports these writes but not that this specific role has
   permission to. This test must pass under the real M2M-authenticated
   "PO Update" role specifically before this phase counts as done. If it
   fails with a permission error that CFO didn't hit, that's a real finding
   (likely a custom-field-level access restriction) -- tell me, don't work
   around it by widening the role's permissions without checking with me
   first.

Don't move on to matching or email intake yet -- this phase is done when that
test script passes against the sandbox with real M2M credentials under the
actual "PO Update" role, no browser login involved anywhere.
```

---

## Prompt 2 — Phase 1 (cont'd): parsing layer

```
Now build the parsing layer described in architecture doc section 4.1.
Important context: every vendor sends a completely different packing slip
layout (confirmed by the business), so the Claude-assisted extractor is the
PRIMARY parsing path for this project, not a fallback for edge cases.

1. Build the Claude-assisted extractor: given a spreadsheet's raw cell grid
   (not an image -- read it with openpyxl/pandas and pass the structured
   data, not a screenshot) and a target schema (PO#, style, color, size,
   quantity), call the Anthropic API and return structured, validated JSON.
   Any row the model isn't confident about should come back flagged
   low-confidence rather than guessed -- it needs to route to human review
   later, not fail silently.

2. Keep parse_packing_slip.py as-is for Inprotex specifically (it's already
   validated against a real file, 100% match against the vendor's own
   summary email) -- use it as a fast/free path when the file matches that
   known layout, and fall back to the Claude-assisted extractor otherwise.

3. Also generalize the shipping advice PDF parsing (ETA/ETD/HAWB/invoice
   number extraction) the same way -- deterministic regex parsing for known
   formats, Claude-assisted fallback for unknown ones.

4. Write unit tests against the two real sample files already in this folder,
   plus at least one deliberately malformed/edge-case input to confirm
   low-confidence rows get flagged rather than silently misparsed.

I don't have more real vendor files yet beyond the Inprotex sample -- flag
that as a real limitation of your test coverage, don't pretend the
Claude-assisted path is fully proven until it's been run against a second
real vendor file.
```

---

## Prompt 3 — Phase 2: email intake + data layer

```
Build Phase 2 from the build plan: email intake and the persistence layer.

1. Register/configure a Microsoft Graph API app-only integration
   (Mail.Read permission) -- walk me through the Azure AD app registration
   steps I need to do manually, then wire up the polling job. Confirmed
   volume is 10-20 emails/week, so poll every 15-30 minutes -- do not build
   a real-time webhook subscription, it's unneeded complexity at this volume.

2. Important unresolved item: the mailbox that needs to be monitored is
   Paula's, not mine, and a direct Graph API test this session returned a
   403 against her mailbox. Before wiring this up for real, tell me clearly
   that we need either (a) Paula forwarding/rule-routing vendor emails into
   a shared mailbox this app has access to, or (b) explicit delegate access
   granted to her mailbox for this app. Don't silently build against my own
   mailbox as a stand-in for hers.

3. Build the SQLite database with the schema from architecture doc section 5
   (shipments, proposed_changes, audit_log).

4. Wire the parsing output (Phase 1) into the database as PENDING_REVIEW
   proposed_changes rows, matched against real sandbox PO data using the
   logic in matcher.py (already includes the NetSuite size-label
   normalization -- XXL/XXXL vendor labels map to NetSuite's 2X/3X, and
   exact-match keys on custcol_sd_tmpl_style / custcol_product_color.refName
   / custcol_product_size.refName).

5. Three business-logic questions in architecture doc section 6.1 are still
   unconfirmed with Paula (date-to-field mapping/transit buffer, split
   shipment semantics, lines absent from a packing slip). Implement the
   conservative defaults documented there for now (don't auto-write a raw
   port ETA as the receipt date -- surface it as a clearly labeled reference
   value for the reviewer to confirm/adjust; treat absent lines as
   leave-alone-and-flag, never auto-zero), but remind me explicitly that
   these are placeholder behaviors pending Paula's actual answer, not
   finished logic.

This phase is done when a real vendor email results in a correct, persisted
proposed_changes row set, matched against live sandbox PO data -- not mocked
data like the demo_matcher.py test.
```

---

## Prompt 4 — Phase 3: review/approval + write-back

```
Build Phase 3: the human review/approval step and the NetSuite write-back.
This review step is permanent by design, not a training-wheels feature to be
removed later -- that was an explicit decision, don't build a path that
bypasses it.

1. Start with the simplest version: an email digest listing pending
   proposed_changes per shipment, with an approve link/mechanism, rather than
   a full web dashboard. We can upgrade this later if Paula wants richer
   interaction.

2. Wire approval to the NetSuite write-back built in Phase 1 -- update the
   real fields (quantity, expectedReceiptDate,
   custcol_override_expected_receipt, custcol_sd_updatedreceiptdate) on the
   matched line.

3. Handle NetSuite write failures explicitly and visibly (e.g. closed PO,
   permission error) -- surface them clearly in the audit log and back to
   me/Paula, never fail silently.

4. Send a confirmation notification back to Paula (and me) on success or
   failure.

Done when the full loop works end-to-end against sandbox: email in, review
digest out, approval in, sandbox PO actually updated, confirmation out.
```

---

## Prompt 5 — Phase 4: hardening + production cutover

```
Build Phase 4: hardening and the move to production. Do not point anything
at the production NetSuite account until every exit criterion in Phase 1-3
has been met against sandbox -- sandbox-first is a firm rule for this
project, not a suggestion.

1. Run the full pipeline against any real historical vendor emails I can
   provide, to shake out edge cases beyond the one sample file we've tested
   against so far.

2. Set up a new NetSuite Integration record + role in PRODUCTION mirroring
   exactly what we built in sandbox during Phase 1 (same permissions, same
   M2M grant type) -- walk me through the manual NetSuite UI steps for this,
   same as Phase 1.

3. Add basic monitoring/alerting -- e.g. "the intake job hasn't run in X
   hours" or "a NetSuite write failed" -- so a silent failure doesn't sit
   unnoticed. An unattended pipeline that quietly breaks is worse than the
   manual process it's replacing.

4. Write a short handoff doc: how Paula uses the review/approval step, and
   how whoever maintains this after me can debug it.

Done when this has processed a real production shipment successfully, with
Paula actually using the review step herself -- not me testing it on her behalf.
```
