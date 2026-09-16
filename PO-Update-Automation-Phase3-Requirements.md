# Phase 3 Requirements — consolidated index

**What this is:** every requirement the review/approval step has to satisfy, gathered from the five
places they accumulated. **It is an index, not a fork.** Each entry cites where the requirement
actually lives and says as little as possible beyond that — the originals stay authoritative, and
where they disagree with this file, they win.

**Why it exists:** the requirements are unusually complete and were spread across
`PO-Update-Automation-Build-Plan.md`, `RUNBOOK.md`, `PO-Update-Automation-Schema-Rationale.md`,
`PO-Update-Automation-Architecture.md` and several docstrings. Six substantive rules exist only
*outside* the build plan, and two of those are decisions that have to be made **before** the screen
is designed rather than discovered while building it.

**Status vocabulary, used on every entry:**

| | |
|---|---|
| **BUILT AND TESTED** | enforced in code today, with a test naming it |
| **DECIDED BUT UNBUILT** | the decision is recorded and settled; nothing implements it |
| **OPEN** | not decided. Someone must choose before the screen can be designed |
| **PROPOSED, PENDING PAULA** | a recommendation made in this document, not yet her decision |

---

## 0. Where each requirement is recorded

The column that matters is the second-to-last: **six of these the build plan does not know about.**

| # | Requirement | Recorded in | In the plan? | Status |
|---|---|---|---|---|
| 1 | Verdict on every line, incl. accepted-unchanged | Build plan item 2 · `Schema-Rationale.md` §8 · `schema.py` `VIEW_CALIBRATION` · `ingest.py` | **yes** | DECIDED BUT UNBUILT |
| 2 | `ns_line_is_open` read from two places | Build plan item 2 · `RUNBOOK.md` §7 · `matcher.POLine` | **yes** | BUILT AND TESTED |
| 3 | Assignment groups presented as groups | Build plan item 2 · `Architecture.md` §6.3 · `matcher._assignment_payload` | **yes** | DECIDED BUT UNBUILT |
| 4 | Ordered-vs-shipped document summary | `RUNBOOK.md` §7 | no | DECIDED BUT UNBUILT |
| 5 | Show the note, not the confidence level | `RUNBOOK.md` §7 · `Architecture.md` §6.2 | no | DECIDED BUT UNBUILT |
| 6 | Approval unit chosen deliberately | `RUNBOOK.md` §7 · `Schema-Rationale.md` §1 · `Architecture.md` §6.2 | no | **OPEN** → PROPOSED |
| 7 | Partial-failure semantics | `RUNBOOK.md` §7 · `Schema-Rationale.md` §6 · `Architecture.md` §6.2 | no | split — see §7 |
| 8 | The five figures, and that nothing gates on them | `Schema-Rationale.md` §10 · `schema.py` · `matcher._line_balance` | no | BUILT AND TESTED |
| 9 | Vendor dates are information, never a proposal | `matcher.reference_dates_label` · `Architecture.md` §6.1, §6.3 | no | BUILT AND TESTED |
| 10 | Date field optional, forever | Build plan item 1 · `Schema-Rationale.md` §5 · `matcher.to_netsuite_fields` | **yes** | BUILT AND TESTED |
| 11 | Override flag only when a date is written | Build plan item 1 · `Architecture.md` §6.3 · `matcher.to_netsuite_fields` | **yes** | BUILT AND TESTED |

---

## 1. A verdict on EVERY line shown, including accepted-unchanged

**DECIDED BUT UNBUILT.** The columns exist and are empty by design.

> *"This only works if the review UI records a verdict on EVERY line it shows, including lines
> accepted unchanged."* — `PO-Update-Automation-Schema-Rationale.md` §8

Repeated as an acceptance criterion in build plan Phase 3 item 2, in `schema.py`'s
`VIEW_CALIBRATION` docstring, and twice in `ingest.py` — which leaves `human_verdict` NULL and
reports it as unpopulated *by design*, not as a gap.

**Why it is easy to get wrong:** the natural implementation writes a row only when Paula changes
something. Verdicts recorded only on disagreement give the calibration corpus **no negatives** — the
base rate becomes unknowable and `needs_review` can never be calibrated. Invisible until someone
tries to calibrate months later and finds half the data missing.

**Columns:** `proposed_changes.human_verdict` / `verdict_by` / `verdict_at`, surfaced by
`v_calibration`. `test_schema.py` already asserts the accepted-unchanged case as *"a NEGATIVE for
calibration"*.

---

## 2. Open state comes from two places, depending on outcome

**BUILT AND TESTED** on the producing side; the screen has to honour it.

> *"`ns_line_is_open` and `ns_item_internal_id` describe the MATCHED line, so when change 5 chooses
> no target they are NULL and the per-line open state lives on `candidate_lines` instead."*
> — build plan Phase 3 item 2, and `RUNBOOK.md` §7

Deliberate, not an oversight: a copy on the row would be a second source of truth for something
change 5 explicitly refused to decide.

**The trap it guards:** `isClosed` is **not** the complement of `isOpen`. A Fully Billed line has
both False — `matcher.POLine` says so, and `test_parsing.py` pins it. A screen rendering "open" as
`not closed` will mislabel exactly the lines a reviewer most needs to understand.

---

## 3. An assignment group is presented AS A GROUP

**DECIDED BUT UNBUILT.** The payload the screen needs is built and tested; the screen is not.

> *"assigning one member constrains the others: give `By Sea` line 5 and line 5 is no longer
> available to `By UPS`."* — build plan Phase 3 item 2

`Architecture.md` §6.3 carries the table distinguishing this from `NEEDS_RESOLUTION`:

| | `NEEDS_RESOLUTION` | `NEEDS_ASSIGNMENT` |
|---|---|---|
| Shape | one extracted line, several NetSuite lines | several extracted lines, several NetSuite lines |
| The human | **selects** one candidate | **assigns** each row; picking one constrains the rest |

Its own reason for not collapsing them into one state with a count: *"a selection is independent per
row, whereas an assignment is a constraint satisfaction problem across a group… A review screen that
treats the two the same will offer the second row a choice that is already taken."*

**What the screen needs:** the group's rows together with recap label and quantity; the shared
candidate lines shown once rather than repeated; remaining choices narrowing as each assignment is
made. **Grouping key:** `(shipment_po_id, key_style, key_color, key_size)` — the canonical key
*without* `key_recap_label`, which is exactly `matcher._sibling_key`.

`matcher._assignment_payload` already sets `auto_assignable: False` *"so nobody later mistakes an
even count for permission to pair by position"*. `ux_proposed_changes_one_line_per_shipment` will
reject a double-assignment, so a per-row dropdown UI offers choices the database cannot save.

---

## 4. An ordered-vs-shipped summary at the document level

**DECIDED BUT UNBUILT.** Build plan: silent.

> *"A uniform overship is a document-level fact, and per-line review hides it. […] the approval unit
> needs a document-level summary alongside the lines — ordered versus shipped for the whole PO, and
> whether the variance is one-sided."* — `RUNBOOK.md` §7

**The evidence:** Tainan ships **865 against an ordered 800 — +8.125%, every single size over**, and
the sheet states it itself at `ACT!R63`. *"A reviewer who sees '+8% on every size' asks the vendor a
question; a reviewer clicking through 28 rows of +1 approves them."*

Overshipment is expected, not an error — Paula: *"there are always extra units that we accept"*
(`Architecture.md` §6.1), and an over-ship produces a plain `PENDING_REVIEW` with no attention flag.
So nothing should *gate* on this figure — see §8. It is a display requirement.

---

## 5. Show the extractor's NOTE, not its confidence level

**DECIDED BUT UNBUILT.** The plan quotes the measurement but draws only the calibration conclusion,
not the display rule.

> *"the note is a first-class field on the review row, not a tooltip on a badge, and queue ordering
> must not be built on the level alone."* — `RUNBOOK.md` §7

**Measured, both sides:**

| Signal | Fires on |
|---|---|
| `needs_review` (the level) | **19 of 29** correctly matched lines |
| `derived_quantity_notes` | **6 times on the entire corpus**, all on Tainan's `REV` |

The flags are honest — the extractor really did infer those values and said so — but at two thirds
of correct work they are unselective, and a reviewer learns to ignore them within about a week.
*"A reviewer reading the note learns something specific and actionable; a reviewer reading 'medium'
learns nothing."*

`Architecture.md` §6.2 states the same constraint and adds the part that stops this becoming an
argument for ignoring the flag entirely: it is **"uncalibrated rather than proven safe"** — zero
errors across 20/20 hand-verified lines means the false-negative rate is *unmeasured*, not zero.
Don't build the queue on it; don't conclude it is safe to drop either. §1 is what eventually
calibrates it.

---

## 6. The approval unit — PROPOSED: PER PO, PENDING PAULA

**Recorded as OPEN in three places.** Proposed here; hers to decide.

> *"the Phase 3 approval unit must be defined deliberately — per PO, per shipment, or per line —
> rather than falling out of the implementation."* — `RUNBOOK.md` §7
>
> *"All three are expressible against this shape; none is forced by it. That choice should be made
> deliberately rather than falling out of the table design, which is exactly why the design does not
> make it."* — `Schema-Rationale.md` §1
>
> *"the Phase 3 **approval unit** (per PO, per shipment, or per line) must be chosen deliberately"*
> — `Architecture.md` §6.2

### Proposal: **per PO**

**Per line is unworkable at the observed volume.** The live run of 2026-09-14 produced **118
proposed changes** from one morning's mail; per-line approval is 118 separate acts.

**Per shipment is actively unsafe.** One Inprotex sheet interleaves **six POs** (1640, 1645, 1650,
1662, 1667, 1704) and the Symmetry set spans two — so one shipment-level click fires writes across
unrelated orders. `RUNBOOK.md` §7 and `Architecture.md` §6.2 both record the fan-out; the schema
already models it (`shipment_pos` sits between `shipments` and `proposed_changes`).

**Per PO matches three things that are already per PO:**

1. **The write unit.** A NetSuite write targets lines within one purchase order.
2. **The failure unit.** `shipment_pos.resolution_status` is per PO — *"PO #4 failing to resolve
   does not stall the other five"* (`Schema-Rationale.md` §1). Approval inherits that isolation.
3. **The summary's own scope.** §4's ordered-versus-shipped figure is *"for the whole PO"*.

**Already expressible:** `proposed_changes.shipment_po_id` is the grouping key. No schema change.

**Note for whoever designs the email flow:** a per-PO *approve button* is not sufficient on its own
for any PO carrying a date — §9 and §10 require an input, not a decision. `Architecture.md` §4.1:
*"An email-link flow needs a lightweight form for this… not a bare Approve button."*

---

## 7. Partial-failure semantics — half decided, half PROPOSED

This splits cleanly, and the split is the point.

### The mechanics: **DECIDED, and already in the schema.** Not open.

`Schema-Rationale.md` §6 — *"Partial failure is per line, and recovery re-approves nothing"* —
settles it:

- Write status is per line **and** per scope; `write_attempts` is append-only with
  `(change_id, scope, attempt_no)` unique.
- Recovery is a query on `quantity_write_status = 'FAILED' OR date_write_status = 'FAILED'`.
- *"The lines that succeeded keep `WRITTEN` **and their original approval timestamps** — nothing is
  re-approved."*
- `error_kind` exists because **NetSuite returns permission denials as HTTP 400**, so a
  status-code retry rule would retry a permission failure forever and never surface it. `TRANSIENT`
  is retryable; `PERMISSION` and `LINE_CLOSED` never are.

### What is genuinely OPEN: what the human sees

> *"one approval can mean six PO writes, and the fifth can fail. **What the audit log records, and
> what Paula sees**, when three succeeded and one didn't, has to be decided before the write path is
> wired."* — `RUNBOOK.md` §7, and near-identically `Architecture.md` §6.2

### PROPOSED, PENDING PAULA

**Approval is per PO; state stays per line.** A partly-succeeded approval is simply rows in
`WRITTEN` and rows in `WRITE_FAILED`, each with its own `write_attempts` row.

**No rollback.** NetSuite line writes are not transactional, and undoing successes means *more*
writes that can themselves fail — turning one partial failure into two. Retry is **per failed
line**, and only for `TRANSIENT`; `PERMISSION` and `LINE_CLOSED` go to a human, not to a retry
button.

**What Paula sees:** *"6 of 8 updated, 2 failed, why, retry those two."*

### Checked against the state machine: **already expressible. No transition needs adding.**

`schema.CHANGE_STATE_TRANSITIONS` already holds every edge this requires:

| From | To | Trigger | Actor |
|---|---|---|---|
| `APPROVED` | `WRITTEN` | every approved scope written | SYSTEM |
| `APPROVED` | `WRITE_FAILED` | at least one approved scope failed | SYSTEM |
| `WRITE_FAILED` | `APPROVED` | re-queued for retry | SYSTEM |
| `WRITE_FAILED` | `WRITTEN` | retry succeeded | SYSTEM |
| `WRITE_FAILED` | `DISCARDED` | given up on | HUMAN |

Three things confirm the model was anticipated at the schema level:

- **State is per `proposed_changes` row**, so "some succeeded, some failed" needs no group state —
  it is the ordinary reading of N rows.
- **`WRITE_FAILED` is documented as *"Retryable without re-approval"***, which is exactly
  per-failed-line retry.
- **`write_attempts` is unique on `(change_id, scope, attempt_no)`** — per line, per scope, numbered
  attempts, with `outcome`, `http_status`, `error_kind`, `error_detail`. The "why" Paula sees has a
  column already.

**And no rollback edge exists, which matches the proposal rather than contradicting it.** The only
transition out of `WRITTEN` is `WRITTEN → APPROVED`, triggered by *"date supplied after the quantity
was written"* — an additive follow-up, not an undo. Building rollback would mean **adding** a
transition. The proposal is not to.

---

## 8. The five figures — and that NOTHING gates on them

**BUILT AND TESTED** on the producing side. The screen must display them and must *not* gate on
them.

> Change 6 attaches `line_balance` to every proposed change *"so the review screen can say 'ordered
> 300, received 0, this slip 128' and a partial delivery is self-evident."*
> — `Schema-Rationale.md` §10

**The five:** `ns_line_id`, `current_quantity`, `current_quantity_received`, `proposed_quantity`,
and `outstanding` — **derived** by `v_review_lines`, never stored, because a stored copy would be a
second source of truth for simple arithmetic.

**The warning that matters for whoever builds the screen:** a version that refused to propose
anything unless the slip equalled outstanding **was built and cancelled**. A final short-ship and a
partial delivery are indistinguishable from quantities alone. `matcher._line_balance` is documented
as *"Display context, never a gate."* Do not reintroduce the gate in the UI layer.

Related, and the reason a short row count is normal rather than alarming: a PO line absent from a
slip is *"routine batch shipping, not cancellation"* (`Architecture.md` §6.1). No record is created
for it at all — `unmatched_netsuite_lines()` lists them for visibility only, and the screen should
present them that way, not as a flag.

---

## 9. Vendor dates are information, NEVER a proposal

**BUILT AND TESTED** as a label; the screen must honour it.

`matcher.reference_dates_label` exists for one purpose — *"How the review UI should present the
vendor's dates: as information, explicitly not as a proposal."* Asserted in `test_parsing.py`:
*"review UI label says reference, not proposal."*

Paula, in `Architecture.md` §6.1: *"I will determine what date to put into NetSuite, we don't use
the port arrival date or anything that the vendor advises. It's a receiving date and includes
buffers."* §6.3 makes it a permanent scope boundary: **the tool never derives a date from a vendor
document.**

**The obvious wrong implementation:** pre-filling the optional date field with the vendor's ETA.
§10 makes the field optional; this says what must not be put in it. The plan states the first and
not the second.

The evidence it is not a technicality: an **18-day** port-to-warehouse gap on Inprotex PO 1662, and
Legendz stating both `ETA 2026/8/16` and `Deliver to warehouse by 2026/8/24` — **8 days** — in the
same sentence (`Architecture.md` §7).

---

## 10. The date field is OPTIONAL — forever, not pending

**BUILT AND TESTED.**

> *"Quantity is knowable from the packing slip the moment it arrives; the receipt date often is not,
> because it waits on the forwarder."* — build plan Phase 3 item 1, and `Schema-Rationale.md` §5

**The part a screen will get wrong:** a line sitting quantity-`WRITTEN` with
`date_write_status = 'NONE'` indefinitely is *"a legal resting place, not a pending task"*
(`Schema-Rationale.md` §5). Do not render it as an outstanding to-do queue.

Two constraints already enforce the human's part:

- `ck_proposed_changes_date_needs_human` — **a date cannot exist in this database without a human's
  name on it.**
- `ck_proposed_changes_date_scope_needs_date` — a date scope cannot be approved with no date in it.

`matcher.to_netsuite_fields(include_dates=False)` is the quantity-only path; asking for dates
without a confirmed one raises `DateNotConfirmed` rather than quietly omitting them.

---

## 11. The override flag is set ONLY when a date is actually written

**BUILT AND TESTED**, structurally.

> *"Setting the override flag on a quantity-only approval would assert an override with nothing
> behind it."* — build plan Phase 3 item 1

In `matcher.to_netsuite_fields`, `custcol_override_expected_receipt = True` sits **inside** the
`if include_dates:` branch, so a quantity-only approval cannot assert an override. Enforced by
structure rather than by remembering.

Its converse, from `Architecture.md` §6.3: **when a date IS written, all three fields go together
with the same value** — `expectedReceiptDate`, `custcol_override_expected_receipt = true`,
`custcol_sd_updatedreceiptdate`. *"Writing only the override pair leaves the effective scheduling
date stale."*

---

## What is NOT a Phase 3 requirement

Recorded so nobody adds it back:

- **Nothing gates on `line_balance`** (§8), on the derived-quantity notes, or on the overship
  figure. `claude_extractor.derived_quantity_notes` is documented as notes, **not gates**.
- **`NEEDS_ASSIGNMENT` is never resolved automatically** — not even when the counts match and
  exactly one pairing is arithmetically possible. *"'Arithmetically possible' is not evidence about
  which line is which transport mode"* (`Architecture.md` §6.3).
- **The tool never creates a PO line.** A vendor line with no NetSuite counterpart becomes
  `NEEDS_ATTENTION`; there is no insert path and no state for one.
- **`custcol_sd_fg_excluderepspark` is never read, written, or displayed** — excluded even from the
  `NEEDS_RESOLUTION` candidate payload, where it is the obvious thing to show (`Architecture.md`
  §6.3).
- **Confirmation email cannot go out through the Graph client.** It is read-only and AST-enforced,
  with `/sendMail` on its forbidden-path list (`graph_client.py`,
  `test_poller.test_graph_surface_is_read_only`). Build plan item 4 needs a separate transport, and
  the plan does not mention this.

---

## Open items blocking the screen's design

Everything above is either built or decided. These are not:

| | Where | Status |
|---|---|---|
| **Approval unit** (§6) | `RUNBOOK.md` §7 · `Schema-Rationale.md` §1 · `Architecture.md` §6.2 | PROPOSED **per PO** — needs Paula |
| **What Paula sees on partial failure** (§7) | `RUNBOOK.md` §7 · `Architecture.md` §6.2 | PROPOSED — needs Paula. Mechanics already decided; no schema change |

Two more, both recorded outside the Phase 3 material and both worth settling in the same
conversation:

- **Multi-batch: replace or accumulate?** `Architecture.md` §6.1 — over-shipment is resolved
  (replace), but *"whether a PO shipping in two genuinely separate batches, weeks apart, has the
  second batch's quantity replace the current NetSuite value or accumulate"* is not. **The code
  currently replaces.** Flagged there as *"worth a quick confirmation before Phase 3"*; surfaced
  here because it decides the proposed quantity, which is the number the screen asks her to approve.
- **Who else may approve when Paula is out.** `Architecture.md` §7, still open. It shapes the auth
  model, so it is cheaper to answer before the screen than after.
