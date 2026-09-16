"""quantity accumulation

Two columns on `proposed_changes` recording HOW a proposed quantity was arrived
at, now that a second shipment's quantity **adds to** the first rather than
replacing it.

**Paula's ruling, 2026-09-16:** *"The vendor's packing slip only shows the new
shipment's quantities."* The engine shipped with replace semantics, which on a
PO line receiving a first shipment of 128 and a second of 100 would write 100 and
silently lose 28 units. Slips accumulate.

**Why a column rather than arithmetic after the fact.** `proposed_quantity` is
now a TOTAL, and a total does not say what it is a total of. `228` on a row whose
slip said `100` is unreadable without the base, and the difference between "first
shipment of 228" and "128 already written plus 100 more" is exactly what a
reviewer is being asked to approve. Same reasoning as the colour columns in 0002
and the size axes in 0003: when the result cannot be reconstructed from the
inputs on the row, the derivation is written down.

**Why the base is not NetSuite's current quantity — the part that will look like
an omission to the next reader.** `quantity` is one of the four fields this tool
writes. Using it as the base for the next write would let the tool's own past
output become the input to its next decision, so any error compounds silently and
every run confirms the previous one. RUNBOOK section 8 lesson 13: *would this
field have this value if the tool had never run?* For a line this tool has
written, no. The base therefore comes from `proposed_changes` joined to a
successful `write_attempts` row -- this tool's own audit trail, which NetSuite
cannot contaminate. NetSuite's value is used as a CONSISTENCY CHECK instead: if
it disagrees with what we believe we wrote, someone edited the line outside the
tool, and the row is marked `DISPUTED` with nothing proposed. The tool does not
reconcile; both numbers go to Paula. See `matcher._accumulated_quantity`.

`accumulation_basis` is NULL on a line that matched no NetSuite line -- there is
no base because there is no line.

Also refreshes `v_review_lines` so the basis and base sit beside the proposed
quantity, where the question "228 of what?" is actually asked. The same view
dance as 0002 and 0003 applies and for the same reason: **every view over the
table comes down BEFORE the batch ALTER**, not just the one being changed,
because SQLite's batch mode rebuilds the table and cannot drop a table that any
view references. The downgrade restores the pre-0007 definition verbatim rather
than importing the live one, which would make it a no-op.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-16 11:20:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

#: The view as this migration leaves it. Spelled out here rather than imported
#: from `schema.py`, so the migration keeps describing what it actually did even
#: after the metadata moves on.
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

#: `v_calibration` is unchanged by this migration; it is dropped and recreated
#: only because SQLite will not let the table be rebuilt underneath it.
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

#: The definition in force before this migration, for the downgrade. 0004 last
#: CHANGED it (adding `recap_label_printed`); 0005 left it alone entirely and
#: 0006 dropped and recreated it byte-identically, because altering `shipments`
#: forced every dependent view down. Verified equal to both rather than assumed.
VIEW_REVIEW_LINES_0004 = """
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
       pc.colour_resolution_method  AS colour_resolution_method,
       pc.colour_resolved_code      AS colour_resolved_code,
       pc.colour_resolved_name      AS colour_resolved_name,
       pc.size_composition_method   AS size_composition_method,
       pc.src_size_axis_primary     AS size_axis_primary_printed,
       pc.src_size_axis_secondary   AS size_axis_secondary_printed
FROM proposed_changes pc
JOIN shipment_pos sp ON sp.id = pc.shipment_po_id
"""


def upgrade() -> None:
    # Drop EVERY dependent view first -- SQLite's batch rebuild cannot drop a
    # table a view references. See the module docstring.
    bind = op.get_bind()
    for view in ("v_review_lines", "v_calibration"):
        bind.execute(sa.text(f"DROP VIEW IF EXISTS {view}"))

    with op.batch_alter_table('proposed_changes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('accumulation_basis', sa.String(length=16), nullable=True))
        batch_op.add_column(
            sa.Column('accumulation_base_quantity', sa.Numeric(precision=12, scale=3), nullable=True)
        )
        batch_op.create_check_constraint(
            batch_op.f('ck_proposed_changes_accumulation_basis'),
            "accumulation_basis IS NULL OR accumulation_basis IN "
            "('FIRST_SHIPMENT','ACCUMULATED','DISPUTED')",
        )
        # An accumulation that cannot say what it added to is not auditable.
        batch_op.create_check_constraint(
            batch_op.f('ck_proposed_changes_accumulation_needs_base'),
            "accumulation_basis <> 'ACCUMULATED' OR accumulation_base_quantity IS NOT NULL",
        )

    # Rebuilt with the basis and base beside the proposed quantity, so "228 of
    # what?" is answerable from the review view without a join.
    bind.execute(sa.text(VIEW_REVIEW_LINES))
    bind.execute(sa.text(VIEW_CALIBRATION))


def downgrade() -> None:
    bind = op.get_bind()
    for view in ("v_review_lines", "v_calibration"):
        bind.execute(sa.text(f"DROP VIEW IF EXISTS {view}"))

    with op.batch_alter_table('proposed_changes', schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f('ck_proposed_changes_accumulation_needs_base'), type_='check'
        )
        batch_op.drop_constraint(
            batch_op.f('ck_proposed_changes_accumulation_basis'), type_='check'
        )
        batch_op.drop_column('accumulation_base_quantity')
        batch_op.drop_column('accumulation_basis')

    bind.execute(sa.text(VIEW_REVIEW_LINES_0004))
    bind.execute(sa.text(VIEW_CALIBRATION))
