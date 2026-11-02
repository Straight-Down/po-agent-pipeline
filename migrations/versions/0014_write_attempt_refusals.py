"""write_attempts: a REFUSED outcome, and the quantity read just before writing

Two changes, both for the quantity write path (`write_path.py`):

- **`previous_quantity`**, nullable Numeric(12,3): what the write path re-read
  from NetSuite immediately before deciding. It is the "previous" in the result
  page's "previous -> new", and the value a reversal edits back to (RUNBOOK
  section 11, Phase 4 decision 3). Not the proposal-time `current_quantity`,
  which can be days stale by the time the line is approved. NULL when the
  re-read itself failed, because then nothing was read.
- **`outcome` admits `REFUSED`** alongside SUCCESS and FAILED. A refusal means
  the re-read found the line closed, not open, gone, or edited since the
  proposal, and NOTHING was sent. Folding it into FAILED would make "how many
  writes failed" count lines where no write was attempted, and FAILED cannot
  promise nothing reached NetSuite (a timeout after the PATCH left is FAILED too).

**No view reads `write_attempts`**, so no view dance: both views select from
`proposed_changes`, `shipment_pos` and `shipments` only.

**The downgrade refuses a database holding REFUSED rows**, by re-creating the
narrower constraint over them -- the same choice as 0008. Those rows have no
honest representation under 0013's vocabulary: rewriting them to FAILED would
claim an attempt was made that never was.

Revision ID: 0014
Revises: 0013
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None

TABLE = 'write_attempts'
COLUMN = 'previous_quantity'
OUTCOME_CK = 'ck_write_attempts_outcome'

#: Frozen literals, per the freeze rule -- never imported from schema.py.
OUTCOME_0013 = "outcome IN ('SUCCESS','FAILED')"
OUTCOME_0014 = "outcome IN ('SUCCESS','FAILED','REFUSED')"


def upgrade() -> None:
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.add_column(sa.Column(COLUMN, sa.Numeric(12, 3), nullable=True))
        batch_op.drop_constraint(batch_op.f(OUTCOME_CK), type_='check')
        batch_op.create_check_constraint(batch_op.f(OUTCOME_CK), OUTCOME_0014)


def downgrade() -> None:
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f(OUTCOME_CK), type_='check')
        batch_op.create_check_constraint(batch_op.f(OUTCOME_CK), OUTCOME_0013)
        batch_op.drop_column(COLUMN)
