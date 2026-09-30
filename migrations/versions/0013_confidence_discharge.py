"""proposed_changes.confidence_discharge: why a medium-confidence line is not flagged

A nullable Unicode(2000) column holding, in plain words, the proof that EVERY field
of a medium/low-confidence line was established from the documents' own
arithmetic (`carton_backing.prove_attribution`): the unique carton run behind the
quantity, the inseam label above it, and the proven group that names only its
colour, style and PO. When set, the matcher did not raise the confidence flag;
the review page shows this under the line's Details. NULL when nothing was
discharged. The extractor's note is never the source, and neither is two
documents agreeing (RUNBOOK section 8 lesson 32).

**The view dance.** Both views read `proposed_changes`, and SQLite's batch mode
rebuilds the table, so both come down first and go back up unchanged after --
in upgrade AND downgrade. Enforced by
`test_migrations_touching_a_viewed_table_do_the_view_dance`.

No index and no constraint is added.

Revision ID: 0013
Revises: 0012
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None

TABLE = 'proposed_changes'
COLUMN = 'confidence_discharge'

#: Both views exactly as 0012 left them (unchanged since 0009). Frozen literals,
#: per the freeze rule -- never imported from schema.py.
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


def _drop_views(bind):
    for view in ("v_review_lines", "v_calibration"):
        bind.execute(sa.text(f"DROP VIEW IF EXISTS {view}"))


def _create_views(bind):
    bind.execute(sa.text(VIEW_REVIEW_LINES))
    bind.execute(sa.text(VIEW_CALIBRATION))


def upgrade() -> None:
    bind = op.get_bind()
    _drop_views(bind)
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.add_column(sa.Column(COLUMN, sa.Unicode(2000), nullable=True))
    _create_views(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _drop_views(bind)
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.drop_column(COLUMN)
    _create_views(bind)
