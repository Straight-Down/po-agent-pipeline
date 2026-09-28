"""stored per-sheet classification verdicts

A workbook's per-SHEET content verdicts, persisted on its attachment row so each
sheet is asked about once per (bytes, prompt, model) -- the per-sheet
counterpart of 0011.

**Why.** Since 2026-09-23 the file gate admits a workbook on per-size content
alone, whatever its type, so the per-sheet check is the only thing keeping a
workbook's COMMERCIAL INVOICE sheet out of the quantities. It is a model call,
and until this migration it was made afresh on every ingest: the same bytes
could put an invoice sheet into the proposals on one run and not the next.

**One nullable column, JSON, no backfill.** A list of
{sheet, doc_type, has_size_breakdown, rationale, model, prompt_hash}. The key is
(content_sha256, sheet, prompt_hash, model): the row supplies the first part and
each entry the rest. A JSON column rather than a table because the key is looked
up only from inside one attachment's ingest, and a table would add a composite
key -- a CONSTRAINT, with the dual-dialect obligation that carries -- for no
query anyone runs. NULL means nothing on record; existing rows read that way,
and there is nothing to backfill them from (per-sheet verdicts were never kept).

**No view dance.** Neither view reads `attachments` (see 0011). No index and no
constraint is added.

Revision ID: 0012
Revises: 0011
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mssql


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("attachments", schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            "sheet_verdicts_json",
            sa.UnicodeText().with_variant(mssql.NVARCHAR(None), "mssql"),
            nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("attachments", schema=None) as batch_op:
        batch_op.drop_column("sheet_verdicts_json")
