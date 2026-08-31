"""colour resolution provenance

Five columns on `proposed_changes` recording HOW a line's colour was resolved:
the method, the canonical printed value that was looked up, the code it resolved
to, the long-form name that supplied the mapping, and the item whose record that
name came from.

**Why persist rather than re-derive.** The item read is not stored anywhere, and a
PO's colour set changes as lines are added or received. So "why did NEW INDIGO
become NIN" is answerable today and guesswork in six months -- exactly the kind of
non-obvious inference that has to be written down when it is known. A wrong colour
writes a quantity against the wrong product, so this provenance is not decoration.

Also refreshes `v_review_lines`, which autogenerate cannot see because views are
not in the metadata. Two things about that are load-bearing:

- **EVERY view over the table is dropped BEFORE the batch ALTER, not just the one
  being changed.** SQLite's batch mode rebuilds the table (create tmp, copy, drop
  original, rename), and dropping a table any view references fails outright:
  `error in view v_calibration: no such table: main.proposed_changes`. So
  `v_calibration` comes down too and goes back unchanged. Any future migration
  altering a table with dependent views has to do the same dance.
- **The downgrade restores the 0001 definition verbatim**, spelled out below rather
  than imported from `schema.py` -- importing the live one would silently reinstate
  the new shape and make the downgrade a no-op.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-31 10:00:35.194630
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

#: The view as this migration leaves it. Spelled out here rather than imported from
#: `schema.py`, so the migration keeps describing what it actually did even after
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

#: `v_calibration` is unchanged by this migration; it is dropped and recreated only
#: because SQLite will not let the table be rebuilt underneath it.
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

#: The 0001 definition, for the downgrade.
VIEW_REVIEW_LINES_0001 = """
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
       pc.current_quantity - COALESCE(pc.current_quantity_received, 0) AS outstanding
FROM proposed_changes pc
JOIN shipment_pos sp ON sp.id = pc.shipment_po_id
"""


def upgrade() -> None:
    # Drop the dependent view FIRST -- SQLite's batch rebuild cannot drop a table a
    # view references. See the module docstring.
    bind = op.get_bind()
    for view in ("v_review_lines", "v_calibration"):
        bind.execute(sa.text(f"DROP VIEW IF EXISTS {view}"))

    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('proposed_changes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('colour_resolution_method', sa.String(length=12), nullable=True))
        batch_op.add_column(sa.Column('colour_printed_key', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('colour_resolved_code', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('colour_resolved_name', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('colour_name_source_item_id', sa.String(length=40), nullable=True))
        batch_op.create_check_constraint(batch_op.f('ck_proposed_changes_colour_resolution_method'), "colour_resolution_method IS NULL OR colour_resolution_method IN ('CODE','NAME','AMBIGUOUS','UNRESOLVED')")
        batch_op.create_check_constraint(batch_op.f('ck_proposed_changes_name_resolution_needs_provenance'), "colour_resolution_method <> 'NAME' OR (colour_resolved_code IS NOT NULL AND colour_resolved_name IS NOT NULL  AND colour_name_source_item_id IS NOT NULL)")

    # ### end Alembic commands ###

    # And rebuild it, now with the three columns a human reads when asking why a
    # colour resolved the way it did.
    bind.execute(sa.text(VIEW_REVIEW_LINES))
    bind.execute(sa.text(VIEW_CALIBRATION))


def downgrade() -> None:
    bind = op.get_bind()
    for view in ("v_review_lines", "v_calibration"):
        bind.execute(sa.text(f"DROP VIEW IF EXISTS {view}"))

    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('proposed_changes', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('ck_proposed_changes_name_resolution_needs_provenance'), type_='check')
        batch_op.drop_constraint(batch_op.f('ck_proposed_changes_colour_resolution_method'), type_='check')
        batch_op.drop_column('colour_name_source_item_id')
        batch_op.drop_column('colour_resolved_name')
        batch_op.drop_column('colour_resolved_code')
        batch_op.drop_column('colour_printed_key')
        batch_op.drop_column('colour_resolution_method')

    # ### end Alembic commands ###

    bind.execute(sa.text(VIEW_REVIEW_LINES_0001))
    bind.execute(sa.text(VIEW_CALIBRATION))
