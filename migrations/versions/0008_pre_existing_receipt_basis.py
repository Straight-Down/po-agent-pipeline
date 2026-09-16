"""split PRE_EXISTING_RECEIPT out of DISPUTED

Widens `ck_proposed_changes_accumulation_basis` to admit a fourth value.

0007 gave `accumulation_basis` three values and folded two very different
situations into `DISPUTED`:

- our record and NetSuite **contradict** each other -- we wrote 128 and the line
  holds 150, or the line moved since we last looked. Someone changed something
  behind the tool's back.
- the line simply has **no history** and already carries receipts from before
  this tool existed. Nothing contradicts anything; there is just nothing to add
  to.

**The second is not a dispute, and it is guaranteed to arrive in a batch.** Every
in-flight PO line that was partly received before 0007 shipped hits it on first
contact. Folding it into `DISPUTED` would mean the first live run produces a wave
of rows labelled with the word that is supposed to mean "someone changed
something" -- and a reviewer who sees that word on a hundred unremarkable lines
learns within a week to skim past it. The case she would then skim past is the
alarming one.

That is the same failure the extraction-confidence flag already demonstrated on
this project (RUNBOOK section 7): `needs_review` fired on 19 of 29 correctly
matched lines, and a signal that fires on expected conditions carries no
information however honest each individual firing is. The fix there was to
separate the selective signal from the unselective one, and it is the fix here.

So `PRE_EXISTING_RECEIPT` gets its own value and its own wording -- *"this line
had N units received before the tool started tracking it; confirm the total"*
rather than a report of a disagreement. It **retires itself**: the confirmation
is written, the line then has history, and every later slip on it is an ordinary
`ACCUMULATED`. `DISPUTED` keeps its alarm and should never become routine.

**Widening a CHECK constraint only.** No column is added, no data is rewritten,
and no existing row changes meaning: nothing has run against a database at this
revision yet, so there are no stored `DISPUTED` rows that ought to have been
`PRE_EXISTING_RECEIPT`. Were there any, they would have to be re-derived from the
source documents rather than relabelled in place, because which of the two a row
belongs to is not recoverable from the row.

The same view dance as 0002/0003/0007 applies -- SQLite's batch mode rebuilds the
table and cannot drop a table any view references, so **every** dependent view
comes down first, not just the one being changed. Neither view's text changes
here; both are recreated exactly as 0007 left them.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-16 14:05:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None

#: Both views exactly as 0007 left them. Spelled out rather than imported from
#: `schema.py`, so this migration keeps describing what it actually did even after
#: the metadata moves on.
VIEW_REVIEW_LINES = """
CREATE VIEW v_review_lines AS
SELECT pc.id                        AS change_id,
       pc.shipment_id               AS shipment_id,
       sp.po_number_printed         AS po_number_printed,
       sp.ns_tranid                 AS ns_tranid,
       pc.src_style_text            AS style_printed,
       pc.src_color_text            AS color_printed,
       pc.src_size_text             AS size_printed,
       pc.src_recap_label           AS recap_label_printed,
       pc.state                     AS state,
       pc.ns_line_id                AS ns_line_id,
       pc.current_quantity          AS current_quantity,
       pc.current_quantity_received AS current_quantity_received,
       pc.proposed_quantity         AS proposed_quantity,
       pc.current_quantity - COALESCE(pc.current_quantity_received, 0) AS outstanding,
       pc.accumulation_basis        AS accumulation_basis,
       pc.accumulation_base_quantity AS accumulation_base_quantity,
       pc.colour_resolution_method  AS colour_resolution_method,
       pc.colour_resolved_code      AS colour_resolved_code,
       pc.colour_resolved_name      AS colour_resolved_name,
       pc.size_composition_method   AS size_composition_method,
       pc.src_size_axis_primary     AS size_axis_primary_printed,
       pc.src_size_axis_secondary   AS size_axis_secondary_printed
FROM proposed_changes pc
JOIN shipment_pos sp ON sp.id = pc.shipment_po_id
"""

VIEW_CALIBRATION = """
CREATE VIEW v_calibration AS
SELECT pc.id                    AS change_id,
       s.parser                 AS parser,
       s.extractor_model        AS extractor_model,
       s.extractor_prompt_version AS extractor_prompt_version,
       pc.extraction_confidence AS extraction_confidence,
       pc.needs_review          AS needs_review,
       s.doc_needs_review       AS doc_needs_review,
       pc.state                 AS state,
       pc.human_verdict         AS human_verdict,
       pc.proposed_quantity     AS proposed_quantity,
       pc.approved_quantity     AS approved_quantity,
       CASE WHEN pc.approved_quantity IS NOT NULL
                 AND pc.approved_quantity <> pc.proposed_quantity
            THEN 1 ELSE 0 END   AS quantity_was_corrected,
       pc.verdict_at            AS verdict_at
FROM proposed_changes pc
JOIN shipments s ON s.id = pc.shipment_id
"""

BASIS_CK = 'ck_proposed_changes_accumulation_basis'

#: 0007's three values, for the downgrade. A database holding any
#: PRE_EXISTING_RECEIPT row cannot go back through this without violating the
#: narrower constraint -- which is correct: those rows have no honest
#: representation under 0007's vocabulary, and silently rewriting them to
#: DISPUTED would assert a contradiction that never happened.
BASIS_0007 = (
    "accumulation_basis IS NULL OR accumulation_basis IN "
    "('FIRST_SHIPMENT','ACCUMULATED','DISPUTED')"
)

BASIS_0008 = (
    "accumulation_basis IS NULL OR accumulation_basis IN "
    "('FIRST_SHIPMENT','ACCUMULATED','PRE_EXISTING_RECEIPT','DISPUTED')"
)


def upgrade() -> None:
    bind = op.get_bind()
    for view in ("v_review_lines", "v_calibration"):
        bind.execute(sa.text(f"DROP VIEW IF EXISTS {view}"))

    with op.batch_alter_table('proposed_changes', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f(BASIS_CK), type_='check')
        batch_op.create_check_constraint(batch_op.f(BASIS_CK), BASIS_0008)

    bind.execute(sa.text(VIEW_REVIEW_LINES))
    bind.execute(sa.text(VIEW_CALIBRATION))


def downgrade() -> None:
    bind = op.get_bind()
    for view in ("v_review_lines", "v_calibration"):
        bind.execute(sa.text(f"DROP VIEW IF EXISTS {view}"))

    with op.batch_alter_table('proposed_changes', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f(BASIS_CK), type_='check')
        batch_op.create_check_constraint(batch_op.f(BASIS_CK), BASIS_0007)

    bind.execute(sa.text(VIEW_REVIEW_LINES))
    bind.execute(sa.text(VIEW_CALIBRATION))
