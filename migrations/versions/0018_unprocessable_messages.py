"""messages: a permanent, recorded intake failure -- the unprocessable message

Adds `messages.unprocessable_reason` (Unicode 1000) and
`messages.unprocessable_resolved_at` (DateTime), both nullable.

**The defect.** An attachment that Graph has no file bytes for -- an item
attachment (a forwarded email attached as an .eml/.msg) or a reference attachment
(a cloud link) -- failed the message's intake with `poll_error`. A `poll_error`
message is retried on every poll and holds the watermark just before it, so this
one failed forever and the watermark never moved past it again. Nothing about the
attachment could change between polls.

**The fix.** Such a message is recorded as unprocessable, with its reason: never
retried, never extracted, none of its attachments stored, and shown in the review
app until a person clears it. Clearing sets `unprocessable_resolved_at` and writes
an audit event naming who; the reason itself is kept. `poll_error` keeps its
meaning: a failure that may succeed on the next poll.

**No view dance.** No view reads `messages`, so `batch_alter_table` rebuilding it
on SQLite invalidates nothing -- the same shape as 0011 on `attachments`.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-18 12:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None

TABLE = 'messages'
#: Frozen literals, per the freeze rule -- never imported from schema.py.
COLUMNS = ('unprocessable_reason', 'unprocessable_resolved_at')


def upgrade() -> None:
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.add_column(sa.Column('unprocessable_reason', sa.Unicode(1000), nullable=True))
        batch_op.add_column(sa.Column('unprocessable_resolved_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    # Dropping the columns forgets which messages were unprocessable. A downgraded
    # database would then treat those rows as complete (no `poll_error`) and never
    # look at them again, which is the silent skip this migration exists to end.
    # So the downgrade refuses while any such row exists, as 0008 and 0014-0017
    # refuse rows the old schema cannot represent honestly.
    bind = op.get_bind()
    held = bind.execute(sa.text(
        "SELECT COUNT(*) FROM messages WHERE unprocessable_reason IS NOT NULL"
    )).scalar()
    if held:
        raise RuntimeError(
            f"refusing to downgrade 0018: {held} message(s) are recorded as unprocessable, "
            "and the 0017 schema has no way to say so. Resolve what they are first; the "
            "rows cannot be represented honestly without these columns."
        )
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        for column in reversed(COLUMNS):
            batch_op.drop_column(column)
