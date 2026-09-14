"""mailbox intake

What the poller needs that the schema did not have. Four changes, and each is
forced by a decision recorded elsewhere rather than by a preference.

**1. `poll_state` -- the watermark.** A position (`last_received_at`), not a Graph
delta token: an opaque server-owned cursor cannot be reasoned about, replayed, or
nudged backwards by an operator who needs to re-read a week. `last_polled_at` is
separate on purpose -- a poll that finds nothing still advances it, which is the
only way to tell "the job is alive and the mailbox is quiet" from "the job
stopped running".

**2. `messages.folder_id`.** `Mail.Read` is mailbox-level and the mailbox is
dedicated to this pipeline, so nothing is filtered by folder today. The folder is
RECORDED anyway, because a folder rule added later should be writable against
stored rows rather than by re-reading the mailbox. It is Graph's opaque
`parentFolderId`, not a display name -- resolving the name needs a fifth Graph
call and the client interface deliberately has four.

**3. `messages.poll_error` and `messages.extracted_at` / `extraction_error`.**
Two independent axes that look like one. `poll_error` is intake: one unreadable
message must not stop a poll, so the failure is recorded against the row and the
loop continues. `extracted_at` is the ingest->extraction seam: NULL means
"landed, not yet parsed", which IS the work queue. Keeping them apart is what
lets a parser change be re-run over everything already stored without touching
Graph.

`attachment_count` joins them: what the mailbox said it carried, before anything
was fetched. It makes a zero-attachment message a recorded fact rather than an
absence, and a partial fetch detectable.

**4. `'UNCLASSIFIED'` added to the `attachments.doc_type` CHECK.** The poller
stores bytes and classifies nothing, and neither existing value says that.
`'OTHER'` means classified and none of the above; `'UNREADABLE'` means opened and
failed. Without a third, "never looked at" is indistinguishable from "looked at,
uninteresting" -- and the second is a conclusion the poller has no standing to
record.

No view comes down here. `v_review_lines` and `v_calibration` are defined over
`proposed_changes`, `shipment_pos` and `shipments`; neither reads `messages` or
`attachments`, so the SQLite batch rebuild has nothing to trip over. That is
checked rather than assumed -- see `test_schema.test_view_coverage`.

Revision ID: 0005
Revises: 0004
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

#: Frozen as a literal, like every other migration in this project: the values
#: the CHECK must hold AFTER this migration. Importing `schema.py` would make the
#: constraint depend on when the database was built rather than on which
#: migration it stopped at (RUNBOOK section 7).
DOC_TYPES_AFTER = (
    "PACKING_LIST", "COMMERCIAL_INVOICE", "SHIPPING_ADVICE", "SHIPPING_SCHEDULE",
    "PAYMENT_REQUEST", "INSPECTION_REPORT", "OTHER", "UNREADABLE", "UNCLASSIFIED",
)
DOC_TYPES_BEFORE = DOC_TYPES_AFTER[:-1]


def _doc_type_check(values) -> str:
    return "doc_type IN (" + ",".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.create_table(
        "poll_state",
        sa.Column("mailbox", sa.String(length=320), nullable=False),
        sa.Column("last_received_at", sa.DateTime(), nullable=False),
        sa.Column("last_polled_at", sa.DateTime(), nullable=False),
        sa.Column("messages_seen", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("mailbox"),
    )

    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("folder_id", sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column("attachment_count", sa.Integer(), nullable=False,
                                      server_default="0"))
        batch_op.add_column(sa.Column("poll_error", sa.String(length=1000), nullable=True))
        batch_op.add_column(sa.Column("extracted_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("extraction_error", sa.String(length=1000),
                                      nullable=True))

    # SQLite cannot ALTER a CHECK in place; batch mode rebuilds the table, which
    # is the only way to widen it. The constraint is named, so it can be dropped
    # by name on both engines rather than by whatever the backend called it.
    with op.batch_alter_table("attachments", schema=None) as batch_op:
        batch_op.drop_constraint("doc_type", type_="check")
        batch_op.create_check_constraint("doc_type", _doc_type_check(DOC_TYPES_AFTER))


def downgrade() -> None:
    # Anything the poller stored but nothing classified would violate the
    # narrowed CHECK. Move it to 'OTHER' first: lossy, and the alternative is a
    # downgrade that fails on any database the poller has actually run against.
    op.get_bind().execute(
        sa.text("UPDATE attachments SET doc_type = 'OTHER' WHERE doc_type = 'UNCLASSIFIED'")
    )
    with op.batch_alter_table("attachments", schema=None) as batch_op:
        batch_op.drop_constraint("doc_type", type_="check")
        batch_op.create_check_constraint("doc_type", _doc_type_check(DOC_TYPES_BEFORE))

    with op.batch_alter_table("messages", schema=None) as batch_op:
        for column in ("extraction_error", "extracted_at", "poll_error",
                       "attachment_count", "folder_id"):
            batch_op.drop_column(column)

    op.drop_table("poll_state")
