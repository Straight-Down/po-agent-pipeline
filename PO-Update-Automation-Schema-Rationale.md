# PO Update Automation — Database Schema Rationale

**What this is:** why the schema in `schema.py` / `migrations/versions/0001_initial_schema.py` is shaped the way it is. The table definitions carry the *what*; this carries the *why*, and the why is the part that gets lost. Every section below is a constraint discovered by testing against real data or by Paula's direct answer — not a preference, and each would be painful to retrofit.

**Last updated:** 2026-08-26 · **Phase:** 2, item 3 (schema only — no polling job, no Graph client, no review UI)

---

## 0. Terms, before anything else

**A shipment** is *a batch of PO line updates arising from one real-world shipment event, evidenced either by a vendor document or by Paula's instruction.*

Both workflows live in one `shipments` table, separated by `origin`. So a row with `origin = 'PAULA_DIRECTED'` legitimately has **no message, no attachment, and often no vendor name** — that is the shape of the record, not missing data, and `ck_shipments_provenance` enforces it rather than leaving it to be inferred. The name still fits: when Paula pulls a size forward by air freight, that is a shipment; she arranged it herself instead of reading about it in an email.

**A change** is one line of one PO, in one shipment. It is the unit of approval, the unit of write status, and the unit the audit log answers questions about.

---

## 1. A shipment is not 1:1 with a PO

**The fact.** One Inprotex packing sheet interleaves **six** POs (1640, 1645, 1650, 1662, 1667, 1704). The Symmetry set spans two. This is normal, not exceptional.

**How the schema satisfies it.** Three levels, each with its own identity:

| Level | Table | Grain |
|---|---|---|
| The intake event | `shipments` | one email, or one instruction |
| The PO fan-out | `shipment_pos` | one PO within that event |
| The line | `proposed_changes` | one canonical key within that PO |

`shipment_pos` carries its own `resolution_status`, so PO #4 failing to resolve from a printed number to a NetSuite internal id does not stall the other five — it is one row in `NOT_FOUND` beside five in `RESOLVED`. `proposed_changes` hangs off `shipment_po_id`, so a line always knows both its email and its PO without either owning the other.

**What this makes possible later.** Phase 3 has to choose an approval unit — per PO, per shipment, or per line. All three are expressible against this shape; none is forced by it. That choice should be made deliberately rather than falling out of the table design, which is exactly why the design does not make it.

---

## 2. Row identity is the canonical key, never a digest of the row

**The fact.** Verbatim display text varies between extraction runs *by design*. The extractor may render a colour `NEW INDIGO` on one run and `NEW  INDIGO` on the next, and the pipeline deliberately preserves what was printed rather than rewriting it. In a five-run comparison, 6 of 25 rows differed in displayed text while keys, quantities, counts and order were identical.

**How the schema satisfies it.** `key_style` / `key_color` / `key_size` hold the canonical form (`canonical.py`: NFKC → dash folding → zero-width folding → whitespace collapse → strip → casefold). The unique index `ux_proposed_changes_canonical_key` is on that triple per PO. **Nothing anywhere hashes a row.** A re-parse that renders the colour differently collides with the existing row instead of creating a phantom second one.

**Two details that look arbitrary and are not:**

- **`''`, never `NULL`, in a key column.** Azure SQL treats NULLs as **equal** in a unique index; SQLite treats them as **distinct**. A nullable key column would therefore enforce a different rule on each engine — the worst kind of portability bug, since the development database would be the permissive one. The sentinel removes the question.
- **Sizeless rows are exempt from uniqueness** (`WHERE key_size <> ''`). Extraction-side aggregation deliberately never collapses a row with no size, so several can legitimately coexist on one PO.

**Collation, while we are here.** SQLite's default is case-*sensitive* (`BINARY`); Azure SQL's is case-*insensitive*. That difference would normally threaten this index — but canonical form is casefolded, so every key column is lowercase by construction and the two engines agree. This is a reason the canonical form is stored in its own columns rather than computed at query time. **Verbatim columns must never be used for comparison**; if that rule is ever broken, collation bites immediately.

---

## 3. Two dedup axes, because one has a hole

**The facts.** Graph's `Mail.Read` **cannot mark a message read or move it** — the mailbox is immutable to this application, so it cannot be used as state. And because vendor mail will initially be **forwarded by Paula**, a re-forward arrives with a **new message id carrying the same attachment**.

**How the schema satisfies it.**

| Axis | Mechanism | Catches |
|---|---|---|
| Message | `uq_messages_graph_message_id` | redelivery of the same message |
| Content | `attachments` keyed by `content_sha256` + `ux_shipments_primary_attachment` | a re-forward, a re-send, the same file under a new name |

`attachments` is keyed **by content**, not by `(message, filename)`. That is the whole mechanism: the same PDF arriving on a new message is *one* attachment row with *two* `message_attachments` rows, and the filtered unique index on `shipments.primary_attachment_sha` refuses to build a second live shipment from content already parsed.

`source_set_hash` covers the multi-document case — Symmetry sent a rollup **and** a carton-by-carton detail for one shipment, which the pipeline cross-checks against each other — so an identical re-forward of the whole attachment set is recognised before a primary is even chosen. And when Paula forwards only *part* of an email (just the packing list from six attachments), the set hash differs but the primary-attachment index still catches it. Each axis alone has a hole; together they do not.

**Deliberate escape hatch.** A re-parse under a new extractor version is legitimate. Setting `superseded_by_shipment_id` on the old shipment frees the filtered index, so the escape is explicit and leaves a trail rather than requiring a constraint to be dropped.

---

## 4. Unresolved is a state, not a null foreign key

**The fact.** `(PO, style, colour, size)` is **not unique per NetSuite PO line** — 64 of 1,659 POs carry duplicate-key lines, created during *receiving* rather than at PO entry, so this pipeline meets them disproportionately. One extracted line can match several open lines, and no field discriminates them (RUNBOOK §6 item 10).

**How the schema satisfies it.** `STATE_NEEDS_RESOLUTION`, rows in `change_candidates`, and `ns_line_id IS NULL`. The load-bearing part is `ck_proposed_changes_target_required`:

```sql
state NOT IN ('APPROVED','WRITTEN','WRITE_FAILED') OR ns_line_id IS NOT NULL
```

An unresolved change **cannot** be approved or written. The "null FK plus a comment asking people to be careful" design is not merely discouraged, it is an integrity error. `ux_change_candidates_one_selected` allows at most one chosen candidate.

`change_candidates` carries quantity, received, billed, dates, rate and `is_open` per candidate — everything a human needs to choose. It carries **no** `custcol_sd_fg_excluderepspark` column, here least of all: it is the obvious thing to show, and it failed as a discriminator at 25.5%. See §11.

**No tiebreaker exists in the schema either.** There is no "preferred candidate" column, no ranking. `quantityReceived` looks like it would settle the one live ambiguous case, but that is n=1 and a wrong automatic pick fails silently.

---

## 5. Quantity and date approve separately, and the date is optional forever

**The fact.** Paula supplies the receipt date herself, from the freight forwarder, with her own buffers applied. Quantity is knowable from the packing slip the moment it arrives; the date often is not, because it waits on someone else.

**How the schema satisfies it.** Two independent approval column-groups on `proposed_changes` — `approved_quantity` / `quantity_approved_by` / `quantity_approved_at` / `quantity_write_status`, and `confirmed_receipt_date` / `date_approved_by` / `date_approved_at` / `date_write_status`. A line can be quantity-`WRITTEN` with `date_write_status = 'NONE'` **indefinitely**; that is a legal resting place, not a pending task.

When the forwarder's date arrives weeks later, the change moves `WRITTEN → APPROVED` (see §12) and only the date scope is written, as its own `write_attempts` row.

**Two guards worth naming:**

- `ck_proposed_changes_date_needs_human` — `confirmed_receipt_date IS NULL OR date_approved_by IS NOT NULL`. **A date cannot exist in this database without a human's name on it.** This is §10's scope boundary as a constraint rather than a convention.
- `ck_proposed_changes_date_scope_needs_date` — a date scope cannot be approved with no date in it.

---

## 6. Partial failure is per line, and recovery re-approves nothing

**The fact.** One approval can fan out to writes across six POs, and the fourth can fail — closed line, permission, conflict, NetSuite down.

**How the schema satisfies it.** Write status is per line **and** per scope, and `write_attempts` is append-only with `(change_id, scope, attempt_no)` unique. Recovery is a query:

```sql
SELECT id FROM proposed_changes
WHERE quantity_write_status = 'FAILED' OR date_write_status = 'FAILED'
```

The lines that succeeded keep `WRITTEN` **and their original approval timestamps** — nothing is re-approved, which is the requirement. Both attempts survive as history, so "it failed and then worked" is legible rather than overwritten.

`error_kind` exists because the HTTP status cannot decide retry policy on its own: **NetSuite returns permission denials as HTTP 400**, so a status-code rule would retry a permission failure and never surface it. `TRANSIENT` is retryable; `PERMISSION` and `LINE_CLOSED` never are.

`payload_json` stores exactly what was sent. For a date write that is all three fields with the same value — see §10.

---

## 7. Two workflows, one set of tables, both answerable in six months

**The facts.** Two distinct workflows share these tables:

- **Packing-slip driven** — a vendor document arrives, the tool proposes, Paula approves.
- **Paula-directed** — she pulls a size in early by air freight. No packing slip, no email. She names a PO line and a date; the tool executes. **No proposal step at all.**

**How the schema satisfies it.** `shipments.origin` records which, `ck_shipments_provenance` enforces the shape of each, and `audit_log` carries `workflow` + `actor` + `actor_kind` on **every** row rather than leaving it to be inferred later from whether a message id happens to be present. A Paula-directed change is inserted straight into `APPROVED` by a `HUMAN` actor — a legal initial transition, not a shortcut.

"Why did this line change?" is one query — `SELECT * FROM audit_log WHERE change_id = ? ORDER BY occurred_at` — and it returns the ordered story for either workflow, with `write_attempts.payload_json` showing exactly what was asserted to NetSuite.

**`audit_log` is append-only.** Nothing in the application may `UPDATE` or `DELETE` it. That is a discipline the schema cannot enforce on its own; the answer is a database role without those grants on that table, not a trigger — **now a Phase 4 cutover item in the build plan**, because a constraint recorded only in a rationale doc is a constraint nobody implements.

---

## 8. Calibration data, captured from day one

**The problem.** `needs_review` currently fires on **100% of documents**, including the ones later hand-verified as flawless. A flag that always fires carries no information, so Phase 3 must not build its review queue on it as-is. It is **uncalibrated, not proven safe**: zero extraction errors were observed across 20/20 hand-verified line items, so the false-negative rate is unmeasured rather than zero. The only way out is a corpus pairing the tool's claim with what turned out to be true.

**How the schema satisfies it.** Per line: `extraction_confidence`, `needs_review`, `extraction_note`. Per document: `doc_needs_review`, `parser`, `extractor_model`, `extractor_prompt_version` — so calibration can be sliced by which parser and which prompt produced the claim. Against that: `human_verdict` (`ACCEPTED` / `CORRECTED` / `REJECTED` / `CANDIDATE_PICKED`), `verdict_by`, `verdict_at`, and `approved_quantity` beside `proposed_quantity`.

`v_calibration` joins the two halves and derives `quantity_was_corrected`. **A corrected quantity is a proven extraction error**, and it is derivable without asking anyone to fill in an extra field — the strongest signal in the set comes free.

> **This only works if the review UI records a verdict on EVERY line it shows, including lines accepted unchanged.** If it writes rows only on disagreement, the corpus has no negatives, the base rate is unknowable, and `needs_review` can never be calibrated. That is an acceptance criterion on the Phase 3 review step, recorded in the build plan where whoever builds it will actually read it — not only here.

---

## 9. Verbatim vendor text, preserved

**The fact.** Change 4 preserves what the vendor printed on purpose: for audit, and for showing Paula what the document actually said rather than what the pipeline made of it.

**How the schema satisfies it.** `src_style_text`, `src_color_text`, `src_size_text`, `src_quantity_text` and `source_hint` (`PACKING!R42`, `P2!R17`) on every change, plus `source_sha256` → `attachments` → `message_attachments` → `messages`. "Show me what the vendor printed, in which file, on which row, from which email" is a join.

**The rule that goes with it:** verbatim columns are for display and audit, never for comparison. Comparison uses the canonical key columns — see §2 on collation for what happens if that slips.

---

## 10. The five review figures, and why nothing gates on them

**The fact.** Change 6 (commit `6045b78`) attaches a `line_balance` payload to every proposed change so the review screen can say *"ordered 300, received 0, this slip 128"* and a partial delivery is self-evident to the person who can judge it.

**How the schema satisfies it.** `ns_line_id`, `current_quantity`, **`current_quantity_received`**, `proposed_quantity` on `proposed_changes`, with `outstanding` **derived** by `v_review_lines` as `current_quantity - COALESCE(current_quantity_received, 0)`. Stored arithmetic would be a second source of truth for a subtraction.

`current_quantity_received` is on `proposed_changes` and not only on `change_candidates` for a specific reason: **a single-match change has no candidate rows at all.** On the normal path — which is 96% of lines — the figure would have had nowhere to persist.

**Nothing gates on any of them, and that was tested rather than assumed.** A version that refused to propose anything unless the slip equalled outstanding was built and cancelled: a final short-ship and a partial delivery are the same document, so no arithmetic on these numbers separates them; and with `quantity_received = 0`, "slip equals outstanding" means "nothing to update but the date", which would have left the tool unable to propose a quantity change on any unreceived line. RUNBOOK §8 item 7 has the full account. **There is no `PARTIAL_LINE` state and no `OVER_SHIPMENT` state.** Ruling 6 stands: over-shipment is a plain `PENDING_REVIEW` with no flag.

---

## 11. Scope boundaries the schema enforces

Four boundaries, each asserted by a test in `test_schema.py` rather than only written down here.

- **The tool never creates PO lines.** There is no new-line state, no table for lines to be created, and no create path in the client. The only line reference is `ns_line_id`, always to something NetSuite already has. A vendor line with no counterpart is a flag. *Who* creates a second duplicate-key line, and why, is explicitly out of this tool's scope.
- **Dates never come from a vendor document.** `vendor_etd` / `vendor_eta` live on `shipments`, deliberately away from the line, and there is no `proposed_*_date` column anywhere for a derived date to occupy. `ck_proposed_changes_date_needs_human` makes the boundary structural.
- **A date write is always the triple**, same value: `expectedReceiptDate` + `custcol_override_expected_receipt = true` + `custcol_sd_updatedreceiptdate`. Tested 2026-08-12 in sandbox: NetSuite does **not** derive `expectedReceiptDate` from the override pair, so writing only the pair leaves the field NetSuite actually schedules against stale. `write_attempts.payload_json` records what was sent, so this is auditable after the fact.
- **`custcol_sd_fg_excluderepspark` appears nowhere** — not read, not written, not stored, not displayed. Paula manages it by hand. A test introspects every column in every table and asserts no name matches `%repspark%`.

---

## 12. The state machine

Kept as **data** — `change_states` and `change_state_transitions`, seeded by migration 0001 — so the legal set ships with the schema and cannot drift from the code that reads it. **The runtime guard reads the table** (`schema.assert_transition`, called on every state write including the initial insert), which is what stops the seeding from being decoration: `CHANGE_STATE_TRANSITIONS` in `schema.py` is the *authoring* source that seeds the table, the table is the *runtime authority*, and a test asserts the two still agree. One authority, one drift check — rather than a constant and a table each quietly believing itself in charge. `proposed_changes.state` has a foreign key to `change_states`, which makes a misspelled status an integrity error instead of a row nobody notices.

### States

| State | Terminal | Meaning |
|---|---|---|
| `PENDING_REVIEW` | no | Matched one open line, quantity differs, waiting on a human. |
| `NO_CHANGE` | no | Matched, quantity already correct. **Not** terminal — a receipt date may still be wanted. |
| `NEEDS_ATTENTION` | no | Cannot propose: no match, no open line, closed line, missing PO or quantity, low confidence. |
| `NEEDS_RESOLUTION` | no | Several open lines match. Candidates recorded, no target, a human picks. |
| `MANUAL_ENTRY_REQUIRED` | no | No acceptable size-level source document. Paula keys it herself. |
| `APPROVED` | no | A human approved at least one scope. A target line is guaranteed present. |
| `WRITTEN` | **no** | See below. |
| `WRITE_FAILED` | no | At least one approved scope failed. Retryable without re-approval. |
| `DISCARDED` | **yes** | A human closed it without writing. |
| `SUPERSEDED` | **yes** | A later shipment re-proposed the same key against the same line before this one was written. |

### `WRITTEN` does not mean "done"

**It means: every scope that has been APPROVED has been written.** Nothing more. A line sits in `WRITTEN` with its quantity applied and no date at all, indefinitely, and that is correct and expected — the receipt date may arrive weeks later or never. **The per-scope columns hold the truth**; the state name suggests a finality it does not have, which is why this paragraph exists. When a date is approved afterwards, the change moves `WRITTEN → APPROVED` and back again. That backwards edge is the one that makes `WRITTEN` non-terminal, and it is deliberate.

### Legal transitions

| From | To | Actor | Trigger |
|---|---|---|---|
| *(insert)* | `PENDING_REVIEW` | SYSTEM | matched, quantity differs |
| *(insert)* | `NO_CHANGE` | SYSTEM | matched, quantity already correct |
| *(insert)* | `NEEDS_ATTENTION` | SYSTEM | cannot propose |
| *(insert)* | `NEEDS_RESOLUTION` | SYSTEM | several open lines match |
| *(insert)* | `MANUAL_ENTRY_REQUIRED` | SYSTEM | no acceptable source document |
| *(insert)* | `APPROVED` | HUMAN | Paula-directed instruction, no proposal step |
| `PENDING_REVIEW` | `APPROVED` | HUMAN | approved |
| `PENDING_REVIEW` | `NEEDS_ATTENTION` | SYSTEM | re-check before write found a problem |
| `NEEDS_ATTENTION` | `APPROVED` | HUMAN | human named the target line |
| `NEEDS_RESOLUTION` | `APPROVED` | HUMAN | human selected a candidate line |
| `NO_CHANGE` | `APPROVED` | HUMAN | quantity fine, a receipt date is still wanted |
| `MANUAL_ENTRY_REQUIRED` | `DISCARDED` | HUMAN | keyed into NetSuite by hand |
| any non-terminal | `DISCARDED` | HUMAN | closed without writing |
| any non-terminal | `SUPERSEDED` | SYSTEM | re-proposed by a later shipment |
| `APPROVED` | `WRITTEN` | SYSTEM | every approved scope written |
| `APPROVED` | `WRITE_FAILED` | SYSTEM | at least one approved scope failed |
| `WRITE_FAILED` | `WRITTEN` | SYSTEM | retry succeeded |
| `WRITE_FAILED` | `APPROVED` | SYSTEM | re-queued for retry |
| `WRITTEN` | `APPROVED` | HUMAN | **date supplied after the quantity was written** |

Anything absent from `change_state_transitions` is illegal by definition — that is the point of keeping it as data. `(insert)` is a sentinel string rather than NULL, because NULL in a primary key is not portable and a sentinel is greppable.

### `SUPERSEDED` and the assumption inside it

`SUPERSEDED` fires when a later shipment re-proposes the same canonical key against the same PO line before the earlier change was written. **That timing assumes replace semantics** — the newer document is authoritative — which is confirmed for over-shipment (*"there are always extra units that we accept"*).

The case this assumption used to worry about was a PO shipping in two genuinely separate batches weeks apart, where the second quantity might need to *add* rather than replace. **That case is now out of the tool's scope:** Paula has confirmed line splits are entirely her manual workflow — she adds the extra PO lines and sets the dates herself, because she arranges the air shipment and knows before the packing slip arrives. So the two changes the tool would see are against different lines, not the same one.

Nothing is lost either way: both rows persist, and `SUPERSEDED` is a state change rather than a delete.

---

## 13. Retention: where the bytes live, and the purge story

**What accumulates.** `attachments.stored_uri` points at real vendor documents. These contain **supplier unit prices, MID codes, bank details on payment requests, named inspectors, and customer contact details** — third-party commercial data, some of it personal. The `attachments` row itself holds only the hash, size, classification and a pointer; **the bytes are never stored in the database.**

**Where they live.** Development: a local directory outside the OneDrive-synced project folder, alongside the private key and API key (`%USERPROFILE%\.po-agent\`). Production (Phase 4): Azure Blob Storage, private container, same subscription as the Function App, with the connection string in Key Vault rather than in `.env`.

**The purge story: there is none in v1, and that is a decision rather than an oversight.** Documents are kept indefinitely, because the calibration corpus (§8) depends on being able to go back to a source document and check what the extractor got wrong, and that value grows over the first year. **Revisit at Phase 4**, before production cutover, with three specific questions:

1. **A retention period.** Straight Down may already have one for vendor commercial documents; if so this inherits it rather than inventing one.
2. **Whether payment requests should be stored at all.** They carry bank details and are already banned as a data source; there may be no reason to keep the bytes once classified.
3. **Whether a purge must cascade.** Deleting bytes while keeping the `attachments` row is probably right — the hash and classification stay useful for dedup — but `source_sha256` on `proposed_changes` means an audit trail can outlive the document it points at, which needs to be a deliberate answer rather than a surprise.

Until then: the store is private, it is not in OneDrive, and nothing has been deleted.

---

## 14. Migrations

**Alembic over SQLAlchemy Core metadata.** Not the ORM — `schema.py` is table definitions, and the application talks to them through Core or plain SQL.

**Why not numbered `.sql` files.** Three reasons, in order of how much they would cost:

1. **SQLite cannot `ALTER TABLE ... DROP COLUMN` or drop a constraint.** The first "rename a column" becomes a hand-written create-copy-swap table rebuild, and then a second one for the other dialect. `render_as_batch=True` does that rebuild automatically. It only works on **named** constraints, which is why `schema.py` sets a naming convention.
2. **One definition, two dialects.** `NVARCHAR(n)` / `DECIMAL` / `BIT` / `DATETIME2` on Azure SQL, `TEXT` / `NUMERIC` / `INTEGER` on SQLite, from the same source — rather than two `.sql` files that drift.
3. **`alembic check`** reports drift between the metadata and the migrations, in CI, before it becomes a mystery.

**How to work with it.**

```bash
alembic upgrade head        # apply
alembic downgrade base      # unapply (tested)
alembic check               # metadata vs migrations drift
alembic revision --autogenerate -m "what changed"
```

**Do not edit a migration that has been applied anywhere.** Change `schema.py` and generate a new revision.

**The state machine is seeded by the migration** as a data migration, not at application startup, so a fresh database is immediately consistent and the legal transitions cannot disagree with the code.

**Connection string.** `PO_AGENT_DB_URL` overrides `alembic.ini`, so an Azure SQL string carrying a password never lands in a file inside the OneDrive-synced folder. The `alembic.ini` default is the local SQLite file, which is gitignored.

### Portability rules the schema follows

| Rule | Why |
|---|---|
| Application-generated UUID text keys | identity columns and sequences differ between engines; 36 chars costs nothing at 10–20 shipments/week |
| Application-generated UTC timestamps | no `datetime('now')` / `GETDATE()` |
| Explicit length on every string column | Azure SQL cannot index `NVARCHAR(MAX)`, and caps an index key at 1700 bytes |
| `Numeric`, never `Float`, for quantities | binary floating point is wrong for figures a human reconciles against a printed document |
| No NULLs in key columns | NULL equality in unique indexes differs between the engines (§2) |
| Every constraint named | batch-mode ALTER can only reproduce constraints it can name |
| No `INSERT OR REPLACE` / `ON CONFLICT` | Azure SQL has neither; use select-then-insert behind a repository method |
| `PRAGMA foreign_keys=ON` per connection | SQLite ignores foreign keys otherwise — and it fails **silently**, the worst way for a constraint to be absent. Registered on the Engine class in `schema.py` so no caller can forget it. |

**Two places the dialects genuinely differ**, both handled and both worth verifying against a real Azure SQL instance before Phase 4 rather than assuming: **filtered unique indexes** (same syntax, but the NULL-equality difference makes the `IS NOT NULL` predicates load-bearing) and **CHECK constraint** expression support.

---

## 15. Tests

`test_schema.py` — **79 checks.** These pin the constraints above, not the schema's shape: a column rename should not break them; losing a dedup axis, a state guarantee, or per-line write status should.

| Test | Pins |
|---|---|
| `test_migration_round_trip` | upgrade → downgrade → upgrade; every table and both views built; the state machine seeded; **`alembic check` finds no drift** — which is what lets the other tests build from the metadata and still prove something about the migration |
| `test_message_id_dedup` | axis 1 |
| `test_content_hash_dedup` | axis 2: one attachment row, two message links, no second live shipment — plus the deliberate re-parse escape hatch |
| `test_subset_reforward` | a forward carrying only some attachments is still caught |
| `test_unresolved_multi_candidate` | `APPROVED` without a target is refused; one selected candidate only |
| `test_partial_write_failure_is_per_line` | one line fails, others stay `WRITTEN`, retry re-approves nothing, both attempts survive |
| `test_quantity_without_date` | `WRITTEN` with no date; the date written weeks later as its own attempt; a date with no approver refused |
| `test_canonical_key_identity` | same key + different printed text collides; sizeless rows coexist |
| `test_five_review_figures` | all five figures; `outstanding` derived; a single-match change has no candidate rows to have held `current_quantity_received` |
| `test_calibration_pairing` | the accepted-unchanged **negative** case, and a correction detected without a form field |
| `test_two_workflows_are_distinguishable` | proposal+approval versus one human instruction; both shipment shapes enforced |
| `test_state_machine_is_data` | `WRITE_FAILED` not `FAILED`; illegal edges absent; only two terminal states; a misspelled state refused |
| `test_scope_boundaries_in_the_schema` | no repspark column anywhere; no new-line state; no vendor date on a line; no `PARTIAL_LINE` / `OVER_SHIPMENT` |
| `test_foreign_keys_are_enforced` | the PRAGMA is actually on, and an orphan row is refused |

---

## 15a. What the first real ingest measured

The schema's first contact with real data (2026-08-26, `ingest.py` against the Legendz xlsx and both Symmetry PDFs) is worth recording, because a schema's claims are cheap until rows land in it.

| | Legendz | Symmetry (pair) |
|---|---|---|
| `shipments` | 1 | 1 |
| `shipment_sources` | 1 PRIMARY | 1 PRIMARY + 1 CROSS_CHECK |
| `shipment_pos` | 1 (`1657` → `PO0001657`) | 2 (`1720`, `1721`) |
| `proposed_changes` | 8 | 25 |
| `change_candidates` | 0 | 0 |
| units | 1,049 | 1,669 |

**Idempotency held on real documents.** Re-ingesting both shipments and then re-forwarding each under a new message id left `proposed_changes` at 33 rows with **identical ids and 0 new ids minted**, and the extractor was not called again. `messages`, `message_attachments` and `audit_log` did grow — correctly: a duplicate arriving is a fact worth recording, and the skip is audited.

**Every line came back `NEEDS_ATTENTION`**, for two distinct and legitimate reasons: 29 because the vendor printed a colour *name* against NetSuite's colour *codes* (RUNBOOK §6 item 12, a new blocker this run found), and 4 because the extractor reported `medium` confidence with a specific note — *"Quantity read from the block subtotal row 14; PO, style and colour inferred from the block header in row 12."* Those 4 are the first real rows in the calibration corpus: the tool's claim recorded, awaiting a human verdict.

**Columns with no producer, reported rather than defaulted:** `ns_item_internal_id` and `ns_line_is_open` (33/33 NULL — both exist on `POLine` but `matcher.ProposedChange` does not surface them), `extractor_prompt_version` (no such constant), `agreement_json` (the cross-check result is a warning string, not structured data). The human and approval columns are 33/33 NULL by design — that is the review step's job.

## 16. What this deliberately does not include

Phase 2 item 3 was the schema; item 4 (`ingest.py`) is the writer that fills it. Still not built, and not implied by anything above: the polling job, the Graph client, the review UI, the write-back worker. The tables are ready for all four; none of them is snuck in.

One thing worth stating because its absence looks like an omission: **there is no queue table.** `proposed_changes.state` plus its index is the queue. At 10–20 shipments a week, a separate queue would be a second source of truth about the same rows.
