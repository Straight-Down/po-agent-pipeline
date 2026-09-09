"""transport-mode recap rows

A packing slip may split one shipment across several TRANSPORT MODE recap rows --
the footwear slip for PO 1624 prints `By Sea` 2,040, `By UPS` 140 and an
`Ordered` row of 2,180 -- and NetSuite holds a separate PO line for each mode.
Four changes, three of them enabling that and one closing a hole it exposes.

**1. `key_recap_label` / `src_recap_label` on `proposed_changes`.** Canonical and
verbatim, the same pairing as the style/colour/size columns. `key_recap_label` is
NOT NULL with `''` for the single-recap documents, because it joins the canonical
key and a nullable key column would enforce different rules on the two engines.

**2. `ux_proposed_changes_canonical_key` widened to include it.** The old index
allowed one row per (PO, style, colour, size) per shipment, which is precisely
what this change has to break: two recap rows for one size are two legitimate
rows. Un-widened, the second one would be rejected by the database instead of
reaching a human.

Keying on the recap label is legitimate where keying on a NetSuite field would
not be, and the distinction is worth stating because it looks like a reversal of
change 5. Change 5 found that a key collision on the NetSuite side cannot be
fixed by improving the key -- there is no per-line transport-mode field, and
`rate` and `leadTime` are identical on both lines in all 73 duplicate groups
surveyed. Here the information is genuinely in the source: **the slip labels its
own rows.** Reading a label the document prints is not the same act as inventing
a distinction NetSuite does not record.

**3. `NEEDS_ASSIGNMENT` state**, seeded into `change_states` with its
transitions. Distinct from `NEEDS_RESOLUTION`: that state picks ONE line for one
shipment row, this one pairs N rows to N lines. Neither is ever resolved
automatically.

**4. `ux_proposed_changes_one_line_per_shipment` -- a guard the schema never had.**
`ux_change_candidates_one_selected` already stops one change selecting two
candidate lines. Nothing stopped the mirror image: two changes selecting the SAME
line, whose second write silently overwrites the first. That was unreachable
while every key produced one row and becomes reachable the moment two rows share
a key, so it goes in with the change that creates the risk. Scoped per shipment,
since a later shipment updating the same line again is normal. The `IS NOT NULL`
predicate is load-bearing, not tidiness: Azure SQL treats NULLs as equal in a
unique index, so without it every not-yet-targeted row would collide there while
passing on SQLite.

Views come down before the batch ALTER and go back after, for the reason 0002 and
0003 record: SQLite's batch mode rebuilds the table and cannot drop one that any
view references.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-09 14:20:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

NEW_STATE = "NEEDS_ASSIGNMENT"
NEW_STATE_DESCRIPTION = (
    "The slip split this size across SEVERAL transport-mode rows (By Sea, By UPS) "
    "and the PO holds several lines for it. Both sides recorded; no pairing made; "
    "a human assigns. Distinct from NEEDS_RESOLUTION, which is picking one line "
    "for ONE shipment row -- this is pairing N rows to N lines."
)
NEW_TRANSITIONS = [
    ("(insert)", NEW_STATE, "several shipment rows and several lines share a key", "SYSTEM"),
    (NEW_STATE, "APPROVED", "human assigned this row to a line", "HUMAN"),
    (NEW_STATE, "DISCARDED", "closed without writing", "HUMAN"),
    (NEW_STATE, "SUPERSEDED", "re-proposed by a later shipment", "SYSTEM"),
]

#: The view as this migration leaves it -- now showing the recap label, so a
#: reviewer looking at two rows for one size can see why there are two.
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
       pc.colour_resolution_method  AS colour_resolution_method,
       pc.colour_resolved_code      AS colour_resolved_code,
       pc.colour_resolved_name      AS colour_resolved_name,
       pc.size_composition_method   AS size_composition_method,
       pc.src_size_axis_primary     AS size_axis_primary_printed,
       pc.src_size_axis_secondary   AS size_axis_secondary_printed
FROM proposed_changes pc
JOIN shipment_pos sp ON sp.id = pc.shipment_po_id
"""

#: Unchanged by this migration; dropped and recreated only because SQLite will
#: not let the table be rebuilt underneath it.
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

#: The 0003 definition, for the downgrade.
VIEW_REVIEW_LINES_0003 = """
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


def upgrade() -> None:
    bind = op.get_bind()
    for view in ("v_review_lines", "v_calibration"):
        bind.execute(sa.text(f"DROP VIEW IF EXISTS {view}"))

    with op.batch_alter_table('proposed_changes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('key_recap_label', sa.String(length=40),
                                      nullable=False, server_default=""))
        batch_op.add_column(sa.Column('src_recap_label', sa.String(length=60), nullable=True))
        batch_op.drop_index('ux_proposed_changes_canonical_key')
        batch_op.create_index(
            'ux_proposed_changes_canonical_key',
            ['shipment_po_id', 'key_style', 'key_color', 'key_size', 'key_recap_label'],
            unique=True,
            sqlite_where=sa.text("key_size <> ''"),
            mssql_where="key_size <> ''",
        )
        batch_op.create_index(
            'ux_proposed_changes_one_line_per_shipment',
            ['shipment_id', 'ns_line_id'],
            unique=True,
            sqlite_where=sa.text("ns_line_id IS NOT NULL"),
            mssql_where="ns_line_id IS NOT NULL",
        )

    # The state machine is data, so a new state is an INSERT rather than a code
    # change -- see schema.assert_transition, which reads these tables at runtime.
    #
    # CONDITIONAL, and not for neatness. Migration 0001 seeds `change_states` by
    # importing the LIVE `schema.CHANGE_STATES`, so the rows it writes change as
    # schema.py evolves: on a database built fresh today 0001 already inserts
    # NEEDS_ASSIGNMENT, while a database that stopped at 0003 does not have it.
    # An unconditional INSERT works on exactly one of those two paths and raises
    # an IntegrityError on the other. (That 0001 does not freeze its seed the way
    # it freezes the view definitions is a wart worth knowing about; every future
    # state-adding migration has to be written this way.)
    bind.execute(
        sa.text("INSERT INTO change_states (state, is_terminal, description) "
                "SELECT :s, 0, :d WHERE NOT EXISTS "
                "(SELECT 1 FROM change_states WHERE state = :s)"),
        {"s": NEW_STATE, "d": NEW_STATE_DESCRIPTION},
    )
    for frm, to, trigger, actor in NEW_TRANSITIONS:
        bind.execute(
            sa.text("INSERT INTO change_state_transitions "
                    "(from_state, to_state, trigger, actor_kind) "
                    "SELECT :f, :t, :g, :a WHERE NOT EXISTS "
                    "(SELECT 1 FROM change_state_transitions "
                    " WHERE from_state = :f AND to_state = :t)"),
            {"f": frm, "t": to, "g": trigger, "a": actor},
        )

    bind.execute(sa.text(VIEW_REVIEW_LINES))
    bind.execute(sa.text(VIEW_CALIBRATION))


def downgrade() -> None:
    bind = op.get_bind()
    for view in ("v_review_lines", "v_calibration"):
        bind.execute(sa.text(f"DROP VIEW IF EXISTS {view}"))

    # Transitions first -- `from_state`/`to_state` reference `change_states`.
    for frm, to, _trigger, _actor in NEW_TRANSITIONS:
        bind.execute(
            sa.text("DELETE FROM change_state_transitions "
                    "WHERE from_state = :f AND to_state = :t"),
            {"f": frm, "t": to},
        )
    bind.execute(sa.text("DELETE FROM change_states WHERE state = :s"), {"s": NEW_STATE})

    with op.batch_alter_table('proposed_changes', schema=None) as batch_op:
        batch_op.drop_index('ux_proposed_changes_one_line_per_shipment')
        batch_op.drop_index('ux_proposed_changes_canonical_key')
        batch_op.create_index(
            'ux_proposed_changes_canonical_key',
            ['shipment_po_id', 'key_style', 'key_color', 'key_size'],
            unique=True,
            sqlite_where=sa.text("key_size <> ''"),
            mssql_where="key_size <> ''",
        )
        batch_op.drop_column('src_recap_label')
        batch_op.drop_column('key_recap_label')

    bind.execute(sa.text(VIEW_REVIEW_LINES_0003))
    bind.execute(sa.text(VIEW_CALIBRATION))
