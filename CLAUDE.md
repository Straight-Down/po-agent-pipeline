# PO Update Automation — Project Context for Claude Code

This file is the entry point for Claude Code. Everything in this folder came out
of a planning/scoping session in Cowork — read this first, then pull in the
other docs as needed. Nothing here has touched production data; all NetSuite
interaction so far has been against the sandbox, authenticated via a temporary
CFO-role workaround in the Cowork chat — NOT the least-privilege role the
standalone build should actually use (see "Current blockers" below, item 1).

## What this project is

Paula (Straight Down's supply chain manager) enters POs into NetSuite when
they're placed. Vendors later send shipping update emails (packing slip Excel
+ shipping advice PDF) with the real final quantities and dates, once goods
actually ship. Today she manually re-enters that into NetSuite, at the PO
item-line level: **Quantity, Expected Receipt Date, Override Expected Receipt
Date, Updated Receipt Date**.

The goal: parse those vendor documents automatically, match them to the right
NetSuite PO lines, stage the proposed changes, and only write to NetSuite after
a human (Paula) approves. Ideally triggered by her forwarding/receiving the
vendor email, not by manually uploading files somewhere.

**Read `PO-Update-Automation-Architecture.md` in full before writing any code.**
It covers the two architecture options considered (Cowork-native vs. a
standalone Anthropic-API service), why the standalone service is recommended,
the component breakdown, data model, and — critically — a section of NetSuite
OAuth/role gotchas discovered the hard way this session (§6). Skipping that
section will likely mean re-deriving the same NetSuite auth problems.

**Read `PO-Update-Automation-Build-Plan.md`** for the phased build order,
timeline/cost estimates, and known risks. Build in that phase order — Phase 1
(NetSuite POC) confirms the last open piece of the write-path (that the actual
least-privilege "PO Update" role, not just CFO, can perform the sublist edits
already proven to work mechanically — see architecture doc §6) before
anything else depends on it.

## Files in this folder

| File | What it is |
|---|---|
| `CLAUDE.md` | This file |
| `PO-Update-Automation-Architecture.md` | System design, both architecture options, recommendation, data model, NetSuite integration notes |
| `PO-Update-Automation-Build-Plan.md` | Phases, timeline, cost estimate, risks |
| `Claude-Code-Kickoff-Prompts.md` | Ready-to-paste prompts for Claude Code, one per build phase — use these in order, don't skip ahead |
| `parse_packing_slip.py` | **Validated reference implementation** — parses the Inprotex-format packing slip Excel + shipping advice PDF. Every line it extracts was hand-checked against the vendor's own summary email (100% match). Reuse this logic; don't re-derive it from scratch. |
| `netsuite_client.py` | Stub NetSuite client — defines the interface (`get_purchase_order`, `update_po_line`) the rest of the pipeline codes against. Method bodies are mocked/`NotImplementedError` pending M2M NetSuite access — swap in real REST calls here. Docstring has the confirmed field names/types and the CFO-vs-least-privilege-role caveat — read it before implementing. |
| `matcher.py` | Diff/staging logic — matches parsed vendor lines to NetSuite PO lines and computes proposed changes. Matching uses exact-match fields confirmed live against sandbox (`custcol_sd_tmpl_style`, `custcol_product_color.refName`, `custcol_product_size.refName` with size normalization) — no longer a display-name substring heuristic. Still has 3 unresolved business-logic questions (date mapping/buffer, split shipments, absent lines) — see architecture doc §6.1 before finalizing the diff behavior. |
| `demo_matcher.py` | Proves `matcher.py` works: mocks NetSuite's current state using a real example from Paula (PO 1662/M120246/TID showing S=12,M=71 in NetSuite vs. the real shipment's S=9,M=50) and confirms the diff engine catches it. Lines with no mock NetSuite data correctly come back `NEEDS_ATTENTION` instead of being silently dropped — preserve that behavior in the real implementation; a matching miss on live PO data should never fail silently. |
| `0626建躍空運成衣 (SD-219國外)Invoice_Packing.xlsx` | Real sample vendor packing slip |
| `Shipping Advice 6128990769 建躍.pdf` | Real sample shipping advice |
| `Proposed_PO_Updates_SD-219.xlsx` | Example of what the human review step should show — a flat, readable diff table. Not a required format, just a reference for what "good" looks like. |

**Important:** the `.py` files above are prototypes proven against one real
document, written in a chat session, not production code. Treat the *logic*
as validated; feel free to rewrite the *implementation* (error handling,
structure, tests, packaging) to whatever standard you'd normally build to.

## Current blockers (as of 2026-08-04)

1. **RESOLVED 2026-08-04 — kept for history:** the Cowork NetSuite sandbox
   connector was initially blocked because the account's pre-built "Claude
   AI" integration record is scoped to a proprietary "NetSuite AI Connector
   Service" permission that isn't assignable to normal custom roles (full
   diagnosis in architecture doc §6). This was worked around mid-session by
   assigning Kiko's NetSuite user the CFO role for Cowork-chat purposes only.
   **The bigger question this raised — whether the real least-privilege "PO
   Update" role could perform the same write, not just CFO — is now also
   resolved.** Claude Code built the M2M/JWT-authenticated client and ran the
   same write/verify/revert test under the actual "PO Update" role: it
   passed, all four fields, no field-level restrictions. Phase 1's core exit
   criterion is met (see build plan Phase 1 and architecture doc §6).
2. **For the standalone build (recommended path), don't reuse the CFO role,
   the interactive OAuth flow, or the "Claude AI" integration record.**
   Create a *new* NetSuite Integration record using the **OAuth 2.0 Client
   Credentials (Machine-to-Machine) grant**, scoped to `REST WEB SERVICES`,
   tied to a dedicated least-privilege role. This sidesteps the whole
   interactive-browser-login problem — see architecture doc §6 for the exact
   role permissions already worked out (`Transactions > Purchase Order`:
   Edit, `Lists > Items`: View, `Lists > Vendors`: View,
   `Setup > REST Web Services`: Full, `Setup > Log in using OAuth 2.0 Access
   Tokens`: confirmed required just for the role to be selectable in
   NetSuite's M2M setup screen at all). Note "Web Services Only Role" has
   *opposite* recommendations depending on grant type — unchecked for the
   interactive flow, likely checked (but unverified) for M2M — see
   architecture doc §6, don't copy the interactive-flow setting blindly.
3. **Outlook/M365 connector only has access to Kiko's mailbox, not Paula's**
   (confirmed — a direct search against her mailbox returned 403). She's the
   one who receives vendor emails. Settle whether she forwards into a shared
   mailbox or grants delegate/app-only access before building the email
   intake service (Phase 2 of the build plan).
4. **NEW 2026-08-04 — blocks Phase 2's PO matching:** the "PO Update" role
   can read a PO directly by internal id but cannot perform REST
   collection/search queries (`GET /purchaseOrder?limit=1` and similar
   return `400 USER_ERROR` despite View-level List permissions). Vendor
   packing slips carry human PO numbers ("1662"), not internal ids
   ("8489541") — resolving one to the other needs exactly the query access
   this role doesn't have. Needs a NetSuite admin conversation about which
   permission grants collection/search access without over-widening the
   role. See architecture doc §6. Don't work around this by guessing at
   broader permissions without checking first.

## Open technical questions (validate early, don't assume)

- ~~Does NetSuite's standard REST Record API support editing Purchase Order
  item-line sublist fields directly, or is a SuiteScript RESTlet required?~~
  **Resolved 2026-08-04**, live-tested against sandbox PO 8489541 (PO# 1662):
  yes, standard REST API supports it directly, no RESTlet needed. Confirmed
  writable fields: `quantity` (number), `expectedReceiptDate` (ISO date
  string), `custcol_override_expected_receipt` (boolean, custom field),
  `custcol_sd_updatedreceiptdate` (ISO date string, custom field). Update by
  targeting a line's `line` number inside `item.items[]`. See architecture
  doc §6 for the full test detail (write, verify, revert).
- ~~Is style-color-size one NetSuite Item record per SKU, or one Item with
  matrix variants?~~ **Resolved** — confirmed one child Item record per SKU
  (`matrixType: "CHILD"`), with `custcol_product_color` / `custcol_product_size`
  as reference fields (match on `refName`, e.g. `"TID"`, `"S"`) rather than by
  parsing the item display name.
- **New:** NetSuite's canonical size labels are `2X`/`3X`, not `XXL`/`XXXL`
  like Inprotex's packing slip uses. `matcher.py` now normalizes this
  (`SIZE_ALIASES`) — extend that mapping if other vendors use different size
  labels.
- ~~How many vendors send these updates, and how similar are their spreadsheet
  layouts to Inprotex's?~~ **Answered by Paula: every vendor's packing slip is
  a completely different layout.** This means the Claude-assisted extractor
  (architecture doc §4.1) is the *primary* parsing path, not a fallback —
  `parse_packing_slip.py` is a fast/free special case for Inprotex only, and
  should not be treated as a template to replicate per vendor.
- ~~Confirmed shipment volume~~ **Answered by Paula: 10–20 emails/week.**
  Polling every 15–30 minutes is sufficient — do not build a real-time Graph
  webhook subscription for v1, it's unneeded complexity at this volume.
- **Three business-logic questions still need Paula's input before Phase 3**
  (see architecture doc §6.1 for full detail and conservative defaults to use
  in the meantime): (1) which vendor date maps to `expectedReceiptDate` /
  `custcol_sd_updatedreceiptdate`, and whether there's a transit-time buffer
  between a shipment's port ETA and the actual receipt date — real sandbox
  data shows an 18-day gap on one real example, so don't assume raw ETA is
  correct; (2) does a second shipment's quantity replace or add to an
  existing PO line's quantity; (3) does a PO line missing from a given
  packing slip mean "not shipped yet" or "cancelled." None of these are
  safe to guess silently — the diff engine's correctness depends on them.

## Working agreement (from the planning session)

- **Sandbox first, always.** Don't point anything at the production NetSuite
  account until it's proven in sandbox.
- **Human review before every NetSuite write, permanently** — not a
  training-wheels step to be removed later. This was an explicit, deliberate
  decision, not a default to revisit without checking back in.
