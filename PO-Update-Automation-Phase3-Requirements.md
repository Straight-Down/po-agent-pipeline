# Phase 3 Requirements — consolidated index

**What this is:** every requirement the review/approval step has to satisfy, gathered from the five
places they accumulated. **It is an index, not a fork.** Each entry cites where the requirement
actually lives and says as little as possible beyond that — the originals stay authoritative, and
where they disagree with this file, they win.

**Why it exists:** the requirements are unusually complete and were spread across
`PO-Update-Automation-Build-Plan.md`, `RUNBOOK.md`, `PO-Update-Automation-Schema-Rationale.md`,
`PO-Update-Automation-Architecture.md` and several docstrings. Eight substantive rules exist only
*outside* the build plan, and four of those were decisions that had to be made **before** the screen
was designed rather than discovered while building it. **Paula ruled on all four on 2026-09-16**;
one of them, multi-batch accumulation, turned out to be a live data-loss bug rather than a design
question and is now built.

**Status vocabulary, used on every entry:**

| | |
|---|---|
| **BUILT AND TESTED** | enforced in code today, with a test naming it |
| **DECIDED BUT UNBUILT** | the decision is recorded and settled; nothing implements it |
| **OPEN** | not decided. Someone must choose before the screen can be designed |
| **CONFIRMED** | Paula ruled on it. A decision, not a recommendation |
| **PROPOSED, PENDING PAULA** | a recommendation made in this document, not yet her decision |

**Paula ruled on four of these on 2026-09-16.** Nothing in this document is now
PROPOSED, and the screen is no longer blocked on a decision. Entries 6, 7, 12 and 13 carry her
wording.

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
| 6 | Approval unit chosen deliberately | `RUNBOOK.md` §7 · `Schema-Rationale.md` §1 · `Architecture.md` §6.2 | no | **CONFIRMED** 2026-09-16 |
| 7 | Partial-failure semantics | `RUNBOOK.md` §7 · `Schema-Rationale.md` §6 · `Architecture.md` §6.2 | no | **CONFIRMED** 2026-09-16 |
| 8 | The five figures, and that nothing gates on them | `Schema-Rationale.md` §10 · `schema.py` · `matcher._line_balance` | no | BUILT AND TESTED |
| 9 | Vendor dates are information, never a proposal | `matcher.reference_dates_label` · `Architecture.md` §6.1, §6.3 | no | BUILT AND TESTED |
| 10 | Date field optional, forever | Build plan item 1 · `Schema-Rationale.md` §5 · `matcher.to_netsuite_fields` | **yes** | BUILT AND TESTED |
| 11 | Override flag only when a date is written | Build plan item 1 · `Architecture.md` §6.3 · `matcher.to_netsuite_fields` | **yes** | BUILT AND TESTED |
| 12 | Multi-batch quantities ACCUMULATE | `Architecture.md` §6.1 · `matcher._accumulated_quantity` · migration 0007 | no | **BUILT AND TESTED** |
| 13 | Paula is the only approver | `Architecture.md` §7 | no | **CONFIRMED** 2026-09-16 |

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

## 6. The approval unit — PER PO

**CONFIRMED by Paula, 2026-09-16: "per PO".** Recorded as OPEN in three places before that; the reasoning below is what was put to her and what she agreed with.

> *"the Phase 3 approval unit must be defined deliberately — per PO, per shipment, or per line —
> rather than falling out of the implementation."* — `RUNBOOK.md` §7
>
> *"All three are expressible against this shape; none is forced by it. That choice should be made
> deliberately rather than falling out of the table design, which is exactly why the design does not
> make it."* — `Schema-Rationale.md` §1
>
> *"the Phase 3 **approval unit** (per PO, per shipment, or per line) must be chosen deliberately"*
> — `Architecture.md` §6.2

### The ruling, and why

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

**Already expressible:** `proposed_changes.shipment_po_id` is the grouping key. No schema change was needed and none was made.

**Note for whoever designs the email flow:** a per-PO *approve button* is not sufficient on its own
for any PO carrying a date — §9 and §10 require an input, not a decision. `Architecture.md` §4.1:
*"An email-link flow needs a lightweight form for this… not a bare Approve button."*

---

## 7. Partial-failure semantics — successes stand, failures retry individually

**CONFIRMED by Paula, 2026-09-16:** *successes stand, failures are flagged with the reason and retried individually. No rollback, no all-or-nothing.* This matched the mechanics `Schema-Rationale.md` §6 had already decided, so nothing in the schema moved.

The question split cleanly into a half that was already settled and a half that was genuinely open, and the split is worth keeping visible.

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

### What she ruled

**Approval is per PO; state stays per line.** A partly-succeeded approval is simply rows in
`WRITTEN` and rows in `WRITE_FAILED`, each with its own `write_attempts` row.

**No rollback.** NetSuite line writes are not transactional, and undoing successes means *more*
writes that can themselves fail — turning one partial failure into two. Retry is **per failed
line**, and only for `TRANSIENT`; `PERMISSION` and `LINE_CLOSED` go to a human, not to a retry
button.

**What Paula sees:** *"6 of 8 updated, 2 failed, why, retry those two."* Her phrasing: failures are *"flagged with the reason"* — `write_attempts.error_kind` and `error_detail` are where that reason already lives.

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

## 12. A second shipment ADDS to the first — it does not replace it

**BUILT AND TESTED**, 2026-09-16. This one changed behaviour rather than describing it.

**CONFIRMED by Paula, 2026-09-16:** *"The vendor's packing slip only shows the new shipment's
quantities."*

It had been open since 2026-08-10 in `Architecture.md` §6.1, where it was filed as low urgency —
*"whether a PO shipping in two genuinely separate batches, weeks apart, has the second batch's
quantity replace the current NetSuite value or accumulate. The code currently replaces."*

**It was not low urgency.** Replace semantics against a line that had already been written 128 and
then received a slip for 100 would write 100 and **silently lose 28 units**, with nothing anywhere
saying so. Accumulation is now the behaviour: base plus this slip.

### The constraint that shapes the implementation

**The arithmetic base is this tool's own record, never NetSuite's current quantity.** That is the
whole design, and it looks like an omission to anyone who does not know the rule behind it — the
obvious implementation is `line.quantity + slip` and it is wrong.

`quantity` is one of the four fields in `WRITABLE_LINE_FIELDS`. Reading it back as the base lets the
tool's own past output become the input to its next decision, so an error compounds instead of
correcting and every run confirms the last one. RUNBOOK §8 lesson 13 gives the test: **would this
field have this value if the tool had never run?** For a line this tool has written, no.

So the base comes from `proposed_changes` in `WRITTEN` state joined to a **successful**
`write_attempts` row — an audit trail NetSuite cannot contaminate. A proposal that was rejected,
discarded, still pending, or whose write failed contributes nothing, because none of them moved the
line.

### NetSuite's value is a consistency check, and disagreement is a full stop

| Situation | What happens |
|---|---|
| Line matches what our record says we wrote | `ACCUMULATED` — propose base + this slip |
| Line differs from what we wrote | `DISPUTED` — **nothing proposed**, both numbers to Paula |
| No history, nothing received | `FIRST_SHIPMENT` — base zero, propose the slip |
| No history, but goods already received | `PRE_EXISTING_RECEIPT` — confirm the total once |
| Seen before, never written, line has moved since | `DISPUTED` — edited outside the tool |

**The tool does not reconcile.** Picking one source as authoritative is exactly the judgement that
belongs to Paula, so a disputed line carries both figures, what we believe we wrote, and which
change wrote it.

**Residual gap, named rather than hidden:** a line this tool has never seen, with nothing received,
whose quantity was edited by hand, is indistinguishable from an untouched line. NetSuite carries no
separate "originally ordered" figure — `quantity` is both the ordered value and the field we
overwrite — so there is nothing to detect it with. Every line the tool has seen once is covered from
then on.

### Where it lives

`matcher._accumulated_quantity` (the arithmetic and the reasoning), `ingest._line_history` (the two
queries that supply the uncontaminated base), and **migration 0007**, which adds
`accumulation_basis` and `accumulation_base_quantity` to `proposed_changes` and surfaces both on
`v_review_lines`.

**Why those columns exist:** `proposed_quantity` is now a TOTAL, and a total does not say what it is
a total of. `228` on a row whose slip printed `100` is unreadable without the base, and the
difference between "first shipment of 228" and "128 already written plus 100 more" is precisely what
the reviewer is being asked to approve. Same reasoning as the colour columns in 0002 and the size
axes in 0003.

### The day-one case is separate from the alarming one

`PRE_EXISTING_RECEIPT` is **not** a dispute. Nothing contradicts anything: the line simply has no
history and already carries receipts from before this tool existed. It asks *"this line had N units
received before the tool started tracking it; confirm the total"*, and it **retires itself** — the
confirmation becomes history, so the next slip on that line is an ordinary `ACCUMULATED`.

`DISPUTED` is reserved for a genuine contradiction and should never be routine. **They cannot share
a label:** `PRE_EXISTING_RECEIPT` is guaranteed to arrive in a batch on first contact, and a
predictable wave wearing the alarm word teaches a reviewer to skim the alarm word. The case she
would skim past is the one that means someone changed something behind the tool. This project has
already watched that happen once — §5 above is the same failure in the confidence flag.

**The wave is 426 open PO lines across 33 POs** (sandbox, 2026-09-16,
`scripts/estimate_pre_existing_receipts.py`). A ceiling, not a forecast: only the part of it that
actually ships asks anything, and each asks once. Re-run against production before cutover.

**Do not backfill `write_attempts` to suppress it.** Writing "this tool wrote N" for a quantity it
did not write fabricates the record that every later accumulation on that line is computed from —
one invented base is wrong forever, compounds with each shipment, and is indistinguishable
afterwards from a real write. 426 one-time questions is the cheaper price.

**For the screen:** these two need visibly different treatment. A `PRE_EXISTING_RECEIPT` row is a
confirmation prompt; a `DISPUTED` row is an alarm. They share a state (`NEEDS_ATTENTION`) and are
told apart only by `accumulation_basis`.

**For the screen:** show the basis. An `ACCUMULATED` row is the one case where the proposed quantity
is not a number printed on any document, and a reviewer who does not know that will read `228`
against a slip saying `100` as an extraction error.

---

## 13. Paula is the only approver — no delegation, no backup

**CONFIRMED by Paula, 2026-09-16: Paula only. No delegation, no backup. Work queues until she
returns.**

Open since the architecture doc was written — `Architecture.md` §7: *"Who else, besides Paula,
should be able to approve changes (e.g., backup approver when she's out)?"*

**This is her explicit choice, not an omission.** It is recorded that way deliberately: an absent
answer invites a future reader to add a backup approver as an obvious convenience, and a recorded
decision does not.

**The operational consequence, stated plainly: the pipeline has a single approver and no path around
her.** While she is away nothing is written to NetSuite, however long the queue grows and however
routine the change. That is by design — it follows from the working agreement that human review
precedes every NetSuite write, permanently — but it is a real single point of failure on a business
process, and it should be a known one rather than a discovered one.

**For the screen:** the auth model is one person. No approver roles, no delegation UI, no
escalation path. `quantity_approved_by` and `date_approved_by` are still recorded per row, because
"who approved this" must be answerable from the audit trail even when the answer is always the same
name — and because this ruling can change.

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

## Nothing is blocking the screen's design

Every requirement above is built, decided, or ruled on. **Paula's four rulings of 2026-09-16 closed
the last of them**, including the two this document had raised as proposals:

| | Was | Now |
|---|---|---|
| Approval unit (§6) | OPEN in three documents | **per PO** |
| Partial failure (§7) | mechanics decided, presentation open | **successes stand, failures retry individually** |
| Multi-batch (§12) | OPEN since 2026-08-10, code silently wrong | **accumulate** — built, migration 0007 |
| Approver (§13) | OPEN since the architecture doc | **Paula only**, no delegation |

**What is left is work, not decisions.** Entries 1, 3, 4 and 5 are DECIDED BUT UNBUILT — they
describe a screen nobody has written yet. Entry 12 is the only one of the four rulings that needed
code, and it has it.

**One thing to carry into the build rather than rediscover:** §13 means the queue has no drain while
Paula is away, and §12 means a line can now sit in `DISPUTED` indefinitely with nothing proposed.
Both are correct behaviour and both look like the system being stuck. The screen should say which it
is.
