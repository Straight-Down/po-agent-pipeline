"""stored classification verdict

The attachment classifier's content verdict, persisted on the attachment row so
it is asked for ONCE per set of bytes.

**Why.** A content verdict is a model call, and a model call is not a function.
Until this migration every ingest classified its attachments twice -- once in
`ingest_shipment` (which decided what was RECORDED) and once inside
`parse_shipment_email` (which decided what was PARSED). On 2026-09-23 those two
calls disagreed about PO 1624's clearance workbook: the parse took 44 lines from
it while `shipment_sources` recorded it EXCLUDED with `lines_proposed: false`, and
every one of the 44 proposals carried a NULL `source_sha256`. The same code, run
on 2026-09-14, had excluded the same bytes outright.

So: one call per ingest, and the verdict stored under the content hash. A later
ingest of the same bytes reuses it instead of re-rolling the gate.

**Four columns, all nullable, no backfill.**

  has_size_breakdown      the gate reads doc_type AND this; storing only doc_type
                          would make a reused verdict unable to reproduce the
                          decision it records
  classifier_rationale    the model's sentence verbatim
  classifier_model        which model produced it
  classifier_prompt_hash  SHA-256 of the prompt and response schema

NULL across the four means "no model verdict exists", and existing rows SHOULD
read that way: their doc_type was written by whichever of the two calls ran
last, which is exactly the value this migration exists to stop trusting.
Backfilling them would promote those verdicts to reusable ones.

**No view dance.** Neither view reads `attachments` -- the same reason 0005
could alter this table without one. No index and no constraint is added.

Revision ID: 0011
Revises: 0010
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None

COLUMNS = ("has_size_breakdown", "classifier_rationale", "classifier_model",
           "classifier_prompt_hash")


def upgrade() -> None:
    with op.batch_alter_table("attachments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("has_size_breakdown", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("classifier_rationale", sa.Unicode(1000), nullable=True))
        batch_op.add_column(sa.Column("classifier_model", sa.Unicode(64), nullable=True))
        batch_op.add_column(sa.Column("classifier_prompt_hash", sa.Unicode(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("attachments", schema=None) as batch_op:
        for column in reversed(COLUMNS):
            batch_op.drop_column(column)
