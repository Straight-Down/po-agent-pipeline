"""size composition provenance

Three columns on `proposed_changes` recording HOW a line's size was arrived at
when it was **composed from two axes** rather than printed in one place: the
method, and each source axis verbatim.

**Why this layout needs recording at all.** Tainan's packing list puts the waist
across the column headers (`30`, `32`, … `42`) and the inseam in a row-block
label several rows above (`INS 32`, then `INS 34` further down). NetSuite keys
those lines on the pair — `32-34` — which appears in neither cell. The same
column therefore means a different size depending on which block a row sits in,
and reading either axis alone collapses two real sizes into one: before this,
every Tainan line came out keyed on the waist only, with the two inseams summed,
and none of the 29 lines matched any of the PO's 28.

**Why persist rather than re-derive.** The pairing is the whole inference, and it
is not recoverable from the result. `32-34` on its own does not say which column
header and which block label produced it, whether the block label was read or
assumed, or that the vendor never wrote `32-34` anywhere. Same reasoning as the
colour columns in 0002: a size determines which product a quantity is written
against, so "why is this line 32-34" has to be answerable from the row.

`size_composition_method` also records the REJECTED case. A composed size is only
accepted if it exists in `customlist_psgss_product_size` (enforced in
`extraction_schema.enforce_size_composition`, not left to the model); when it
does not, the line is flagged and both axes are still written down, because a
composition that failed is exactly the case a human needs the source cells for.

NULL on single-axis documents, which is most of them — four of the five vendors
in the corpus print their sizes in full and compose nothing.

Also refreshes `v_review_lines` so a reviewer sees the two source axes beside the
size. The same view dance as 0002 applies and for the same reason: **every view
over the table comes down BEFORE the batch ALTER**, not just the one being
changed, because SQLite's batch mode rebuilds the table and cannot drop a table
that any view references. The downgrade restores 0002's definition verbatim
rather than importing the live one, which would make it a no-op.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-02 18:40:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
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

#: The 0002 definition, for the downgrade.
VIEW_REVIEW_LINES_0002 = """
CREATE VIEW v_review_lines AS
SELECT pc.id                        AS change_id,
       pc.shipment_id               AS shipment_id,
       sp.po_number_printed         AS po_number_printed,
       sp.ns_tranid                 AS ns_tranid,
       pc.src_style_text            AS style_printed,
       pc.src_color_text            AS color_printed,
       pc.src_size_text             AS size_printed,
       pc.state                     AS state,
       pc.ns_line_id                AS ns_line_id,
       pc.current_quantity          AS current_quantity,
       pc.current_quantity_received AS current_quantity_received,
       pc.proposed_quantity         AS proposed_quantity,
       pc.current_quantity - COALESCE(pc.current_quantity_received, 0) AS outstanding,
       pc.colour_resolution_method  AS colour_resolution_method,
       pc.colour_resolved_code      AS colour_resolved_code,
       pc.colour_resolved_name      AS colour_resolved_name
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
        batch_op.add_column(sa.Column('size_composition_method', sa.String(length=24), nullable=True))
        batch_op.add_column(sa.Column('src_size_axis_primary', sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column('src_size_axis_secondary', sa.String(length=60), nullable=True))
        batch_op.create_check_constraint(
            batch_op.f('ck_proposed_changes_size_composition_method'),
            "size_composition_method IS NULL OR size_composition_method IN "
            "('COMPOSED','COMPOSITION_REJECTED')",
        )
        batch_op.create_check_constraint(
            batch_op.f('ck_proposed_changes_composition_needs_both_axes'),
            "size_composition_method IS NULL OR "
            "(src_size_axis_primary IS NOT NULL AND src_size_axis_secondary IS NOT NULL)",
        )

    # Rebuilt with the two source axes, so a reviewer can see at a glance that
    # `32-34` came from a `30`-style column header and an `INS 34` block label.
    bind.execute(sa.text(VIEW_REVIEW_LINES))
    bind.execute(sa.text(VIEW_CALIBRATION))


def downgrade() -> None:
    bind = op.get_bind()
    for view in ("v_review_lines", "v_calibration"):
        bind.execute(sa.text(f"DROP VIEW IF EXISTS {view}"))

    with op.batch_alter_table('proposed_changes', schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f('ck_proposed_changes_composition_needs_both_axes'), type_='check'
        )
        batch_op.drop_constraint(
            batch_op.f('ck_proposed_changes_size_composition_method'), type_='check'
        )
        batch_op.drop_column('src_size_axis_secondary')
        batch_op.drop_column('src_size_axis_primary')
        batch_op.drop_column('size_composition_method')

    bind.execute(sa.text(VIEW_REVIEW_LINES_0002))
    bind.execute(sa.text(VIEW_CALIBRATION))
