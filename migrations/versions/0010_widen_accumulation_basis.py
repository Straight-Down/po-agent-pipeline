"""widen accumulation_basis to hold its own longest permitted value

`proposed_changes.accumulation_basis` was declared 16 characters wide while its
own CHECK constraint permits `PRE_EXISTING_RECEIPT`, which is 20.

**It went unnoticed because SQLite does not enforce a VARCHAR length.** It
stores whatever it is given, so every test wrote the full 20 characters and read
them back intact. SQL Server enforces the declaration and refuses the row
outright:

    String or binary data would be truncated in table 'proposed_changes',
    column 'accumulation_basis'. Truncated value: 'PRE_EXISTING_REC'.  (2628)

So the column was valid on the development engine and broken on the deployment
one, from the moment 0008 added the fourth basis value.

**Swept for the class rather than fixed as a case.** Every column whose
permitted values are enumerated by a CHECK constraint was compared against the
longest value that CHECK allows: 17 such columns, and this was the only one too
short. The seeded state-machine values were checked the same way against
`change_states` and `change_state_transitions` -- all fit. `size_composition_method`
is the closest other call at 24 declared against `COMPOSITION_REJECTED` at 20.

32 rather than 20: the next basis value should not need a migration, and the
column is not in any index, so the width costs nothing.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-23 14:05:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None

TABLE = 'proposed_changes'
COLUMN = 'accumulation_basis'
CHECK = 'ck_proposed_changes_accumulation_basis'
PREDICATE = (
    "accumulation_basis IS NULL OR accumulation_basis IN "
    "('FIRST_SHIPMENT','ACCUMULATED','PRE_EXISTING_RECEIPT','DISPUTED')"
)


#: Both views exactly as 0009 left them (0009 recreated them unchanged from
#: 0008). Frozen literals, per the freeze rule -- never imported from schema.py.
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


def _alter(bind, width: int) -> None:
    """
    Retype the column, dropping the CHECK that names it first.

    SQL Server refuses ALTER COLUMN on a column a CHECK constraint references
    (type changes get none of the length-change exceptions). The column is in no
    index and no key, so nothing else has to come down -- which is why this is a
    handful of statements rather than 0009's full teardown.
    """
    bind.execute(sa.text(f"ALTER TABLE [{TABLE}] DROP CONSTRAINT [{CHECK}]"))
    bind.execute(sa.text(
        f"ALTER TABLE [{TABLE}] ALTER COLUMN [{COLUMN}] NVARCHAR({width}) NULL"))
    bind.execute(sa.text(
        f"ALTER TABLE [{TABLE}] ADD CONSTRAINT [{CHECK}] CHECK ({PREDICATE})"))


def _sqlite_alter(width: int) -> None:
    """
    SQLite has to do this one too -- unlike 0009.

    The distinction is worth stating because the two migrations look alike and
    are not: 0009 changed VARCHAR to NVARCHAR, which SQLite cannot represent at
    all, so it was genuinely a no-op there and `alembic check` could not see it
    (RUNBOOK section 8 lesson 24). A LENGTH is different -- SQLite records
    `VARCHAR(16)` and reflects it back -- so skipping this one leaves the
    metadata saying 32 while the database says 16, and the drift check DOES
    catch that. It caught it, which is how this branch came to exist.
    """
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.alter_column(COLUMN, type_=sa.Unicode(width), existing_nullable=True)


def upgrade() -> None:
    bind = op.get_bind()
    # EVERY view over the table comes down first. On SQLite batch mode rebuilds
    # `proposed_changes`, and a view referencing a table being replaced fails
    # with "error in view v_review_lines: no such table". The same dance as
    # 0002/0003/0007/0008 -- omitted here at first, and caught by exactly that
    # error.
    _drop_views(bind)
    if bind.dialect.name == "sqlite":
        _sqlite_alter(32)
    else:
        _alter(bind, 32)
    _create_views(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _drop_views(bind)
    if bind.dialect.name == "sqlite":
        _sqlite_alter(16)
        _create_views(bind)
        return
    # Deliberately narrows back to the broken width: a downgrade restores the
    # previous schema, warts included. Any row already holding
    # PRE_EXISTING_RECEIPT will block it, which is the correct outcome -- the
    # data genuinely does not fit what 0009's schema declared.
    _alter(bind, 16)
    _create_views(bind)
