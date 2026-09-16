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
3. ~~**Outlook/M365 connector only has access to Kiko's mailbox, not Paula's**~~
   **RESOLVED 2026-09-09/14, and the answer was neither option listed here.**
   Paula's inbox was explicitly rejected as the target: `Mail.Read` is
   mailbox-level, not folder-level, so pointing an app-only permission at her
   account would expose her whole inbox. A dedicated **`shipments@` shared
   mailbox** was created instead, with app-only `Mail.Read` scoped to it alone.
   Scoping is **observed**, not asserted — `scripts/probe_graph_auth.py` reads
   that mailbox and is refused (`403 ErrorAccessDenied`) on a second one. The
   intake service is built and has run against it (build plan Phase 2).
4. ~~**NEW 2026-08-04 — blocks Phase 2's PO matching:** the "PO Update" role
   cannot perform REST collection/search queries.~~ **RESOLVED 2026-08-12.**
   The missing permission was **`Reports > SuiteAnalytics Workbook`**,
   confirmed by bisect as the sole cause; it gates every collection `GET`,
   every `?q=` filter and all SuiteQL, while by-id `GET`/`PATCH` is ungated.
   `Edit` is the only level the permission offers. The role is now **seven**
   permissions, not the five listed in item 2 above —
   `NETSUITE-M2M-SETUP.md` Step 2 carries the current set and is the file to
   follow when building the production role. PO-number resolution ran end to
   end on live mail on 2026-09-14: 5 of 5, including a zero-padded
   `0001725`.

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

---

<!-- ===================================================================
     Added 2026-09-14. Everything above this line was written 2026-08-04
     and parts of it are now out of date - see "Staleness warning" below.
     =================================================================== -->

## Staleness warning - read before trusting anything above

The sections above describe the project as of **2026-08-04**. Several
statements in them are no longer true, and Claude Code weights this file more
heavily than the code it can read. Known drift as of 2026-09-14:

- The file-table entry for `netsuite_client.py` calls it a "stub" whose
  "method bodies are mocked/`NotImplementedError`". It is not. It is a
  1,293-line client making real REST calls, with zero `NotImplementedError`
  remaining.
- The file table lists only the Phase 0 prototypes. The repo now also contains
  `ingest.py`, `schema.py`, `extraction_schema.py`, `claude_extractor.py`,
  `document_parsers.py`, `attachment_classifier.py`, `size_vocabulary.py`,
  `canonical.py`, `config.py`, alembic migrations 0001-0006, and seven test
  modules — plus, since Phase 2 closed on 2026-09-14, the mailbox intake:
  `graph_client.py` (a four-method Graph interface with a mock and a real
  client), `poller.py` (the polling job and the content-addressed attachment
  store) and `extract_pending.py` (the ingest -> extraction driver, a separate
  command by design).
- "Nothing here has touched production data" should be re-confirmed rather
  than assumed, given how much has been built since.

**Rule: when a change makes a statement in this file untrue, fix the statement
in the same turn.** A wrong line here costs more than a missing one, because
every future session inherits it and trusts it over the source.

## Commands

The project venv is at `.venv` (Windows layout - `.venv\Scripts\`).

```
# runtime deps
.venv\Scripts\python -m pip install -r requirements.txt

# verification tooling (pytest, ruff) - REQUIRED for the hooks to do anything
.venv\Scripts\python -m pip install -r requirements-dev.txt

# full suite
.venv\Scripts\python -m pytest -q

# one module / one test
.venv\Scripts\python -m pytest test_parsing.py -q
.venv\Scripts\python -m pytest test_parsing.py::test_routing -q

# lint one file
.venv\Scripts\python -m ruff check <file>.py

# re-baseline the known-failure ratchet (deliberate, not routine)
.venv\Scripts\python .claude\hooks\verify_stop.py --write-baseline
```

Git note: `.git` is a gitfile pointing at `C:/dev/po-agent.git`, deliberately
outside the OneDrive tree so OneDrive does not corrupt the object store. Git
commands work normally from the project root.

## Definition of done

A change is not finished until all four hold. Do not report success before
then.

1. The file compiles and `ruff check` is clean on what you touched.
2. `pytest -q` introduces no failure that was not already in
   `.claude/known-failures.txt`.
3. The `code-reviewer` subagent has reviewed the diff and its blocking
   findings are resolved.
4. Any statement in `CLAUDE.md`, `RUNBOOK.md` or the architecture doc that
   this change made untrue has been corrected.

The PostToolUse and Stop hooks in `.claude/settings.json` enforce 1 and 2
automatically. 3 and 4 you invoke.

## Test suite status

There is a backlog of pre-existing failures recorded in
`.claude/known-failures.txt`. The Stop hook ignores those and blocks only on
**new** failures, so a long-standing red test cannot trap a session in a loop.

That file is a ratchet. Delete lines from it as you fix them. Never add a line
to it to get a turn to pass - if a change breaks a test, fix the change.

## Gotchas log

Append one line here every time a bug is fixed that a future session could
plausibly reintroduce. Write them as instructions with the failure attached,
not as history - a rule without its failure mode gets rationalised away.

- Choose the Excel reader by **file signature, not extension**. `xlrd>=2.0`
  reads `.xls` only and `openpyxl` reads `.xlsx` only; Tainan's packing list
  is a genuine `.xls` and is that PO's only size-level source.
- A line that cannot be matched returns `NEEDS_ATTENTION`. Never drop, skip,
  or default it - a silent matching miss loses real shipment quantities.
- Match on the exact-match custom fields (`custcol_sd_tmpl_style`,
  `custcol_product_color.refName`, `custcol_product_size.refName` with size
  normalisation). Display-name substring matching was tried and rejected; do
  not reintroduce it.
- The PO Update role can read a PO by internal id but cannot run REST
  collection/search queries. Do not work around a `400 USER_ERROR` by widening
  the role - raise it instead.
- Pass the vendor's FILENAME alongside the bytes. Attachments are stored
  content-addressed, so the path is a SHA-256 and carries no signal; handing it
  to the classifier silently disabled every filename rule, including the
  inspection-report ban. `display_names` exists for this.
- `ClaudeExtractor.last_usage` ACCUMULATES for the life of the instance and is
  never reset. Take a delta across the parse boundary (`usage_delta`); copying
  it wholesale reports the run's running total as one document's cost.
- `NetSuiteClient(cfg)` binds a config to `account_id` and yields a silent MOCK
  client with no data. It raises now, and `config` is keyword-only - use
  `NetSuiteClient(config=cfg)`.
- A flag that only satisfies a guard is a no-op. `--from-beginning` never set
  the window, so with a watermark present it read from the watermark and hid a
  message. Assert a flag's EFFECT, not that it is accepted.
- <add the next one here>
