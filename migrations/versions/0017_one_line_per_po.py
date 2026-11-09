"""proposed_changes: one change per NetSuite line PER PO, not per shipment

Replaces `ux_proposed_changes_one_line_per_shipment` (shipment_id, ns_line_id),
created by 0004 on 2026-09-09, with `ux_proposed_changes_one_line_per_po`
(shipment_po_id, ns_line_id). Same `ns_line_id IS NOT NULL` filter.

**The defect.** `ns_line_id` is NetSuite's sublist line NUMBER, and line numbers
restart at 1 on every PO. Keyed on the shipment, two POs in one email that each
had a matched line 18 were "two changes targeting one line" -- and the database
refused the second, so the whole email failed to ingest. One vendor email
covering several POs is the normal case: the Inprotex SD-219 sheet interleaves
six. Found 2026-11-04 in the Sandbox 2 rehearsal (`msg-inprotex-001`:
"UNIQUE constraint failed: proposed_changes.shipment_id, proposed_changes.ns_line_id").

**The fix.** `shipment_pos` is one row per PO per shipment (`uq_shipment_pos_key`),
so `shipment_po_id` is "this PO in this shipment" and the guard 0004 meant to
build -- two changes in one shipment must not select the same NetSuite line --
is exactly (shipment_po_id, ns_line_id). Nothing it caught before is let through.

**No view dance, deliberately.** Plain `op.drop_index` / `op.create_index`, not
`batch_alter_table`: an index change does not rebuild the table on SQLite, so the
views over `proposed_changes` are never invalidated.

**The downgrade refuses a database it cannot represent.** Re-creating the
per-shipment index over a shipment that now legitimately holds the same line
number on two POs fails with a uniqueness error -- the same choice as 0008, 0014,
0015 and 0016: those rows have no honest representation under the old guard.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-09 12:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None

TABLE = 'proposed_changes'
OLD = 'ux_proposed_changes_one_line_per_shipment'
NEW = 'ux_proposed_changes_one_line_per_po'
#: Frozen literal, per the freeze rule -- never imported from schema.py.
PREDICATE = "ns_line_id IS NOT NULL"


def _create(name: str, columns: list[str]) -> None:
    # Both dialect kwargs, always: SQLAlchemy silently ignores a kwarg for another
    # dialect, and a filtered index that loses its filter becomes a FULL unique
    # index -- the 0004-0007 defect, RUNBOOK section 8 lesson 22.
    op.create_index(name, TABLE, columns, unique=True,
                    sqlite_where=sa.text(PREDICATE), mssql_where=sa.text(PREDICATE))


def upgrade() -> None:
    op.drop_index(OLD, table_name=TABLE)
    _create(NEW, ['shipment_po_id', 'ns_line_id'])


def downgrade() -> None:
    op.drop_index(NEW, table_name=TABLE)
    _create(OLD, ['shipment_id', 'ns_line_id'])
