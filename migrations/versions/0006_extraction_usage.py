"""extraction usage

What a run cost, per shipment and per document, because it was being computed
and thrown away.

`ParseResult.usage` existed and `ingest` never stored it, so after the live run
of 2026-09-14 the question "which document cost the most of the $14.45" could
only be answered by counting characters in the source files and reasoning about
it. That is a reconstruction, not a measurement (section 8 lesson 21).

**Per document as well as per shipment**, because the grain matters: a
cross-check that costs a full extraction and is then discarded is invisible in a
per-shipment total. That run parsed three cross-checks in full and proposed none
of their lines; only a per-document figure shows it.

**NULL and zero are different and both are needed.** NULL means nobody recorded
this -- a row written before this migration, or by a path that does not account.
Zero means measured and genuinely free, which the deterministic Inprotex parser
is. Collapsing them would make "free" and "unmeasured" indistinguishable, which
is the same mistake `UNCLASSIFIED` was added to `attachments.doc_type` to avoid
in 0005.

Nullable and no server default, deliberately: existing rows SHOULD read NULL.
Back-filling them with zero would assert that the defective run was free.

**Both views come down and go back, and here that is REQUIRED rather than
cautious.** `v_calibration` selects from `shipments`, which this migration
rebuilds -- SQLite's batch mode drops and recreates the table, and it cannot drop
one a view depends on. 0005 could skip this because it altered `messages` and
`attachments`, which no view reads. The DDL is frozen as a literal below for the
reason every migration here freezes its data: importing `schema.py` would make
what this migration restores depend on when the database was built.

Revision ID: 0006
Revises: 0005
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

#: Frozen literals, like every migration here: the view DDL as it stands after
#: 0005, so this migration does not depend on when the database was built.
VIEWS = {
    "v_review_lines": """
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
""",
    "v_calibration": """
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
""",
}


def upgrade() -> None:
    bind = op.get_bind()
    for name in VIEWS:
        bind.execute(sa.text(f"DROP VIEW IF EXISTS {name}"))

    with op.batch_alter_table("shipments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("extractor_input_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("extractor_output_tokens", sa.Integer(), nullable=True))

    with op.batch_alter_table("shipment_sources", schema=None) as batch_op:
        batch_op.add_column(sa.Column("input_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("output_tokens", sa.Integer(), nullable=True))

    for _name, ddl in VIEWS.items():
        bind.execute(sa.text(ddl))


def downgrade() -> None:
    bind = op.get_bind()
    for name in VIEWS:
        bind.execute(sa.text(f"DROP VIEW IF EXISTS {name}"))

    with op.batch_alter_table("shipment_sources", schema=None) as batch_op:
        batch_op.drop_column("output_tokens")
        batch_op.drop_column("input_tokens")

    with op.batch_alter_table("shipments", schema=None) as batch_op:
        batch_op.drop_column("extractor_output_tokens")
        batch_op.drop_column("extractor_input_tokens")

    for _name, ddl in VIEWS.items():
        bind.execute(sa.text(ddl))
