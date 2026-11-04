"""write_attempts: an ALREADY_PRESENT outcome

Widens `ck_write_attempts_outcome` to admit a fourth value.

ALREADY_PRESENT means: a retry's re-read found the line already holding
exactly the value an EARLIER attempt of the same proposal sent, where that
attempt's own result was never confirmed (a timeout, a dropped link). "The line
holds the intended value; the tool cannot prove it made the change." It is not
SUCCESS -- nothing in this attempt proves the tool made the change -- and not
FAILED, because the line holds the intended value. It settles the line:
`write_path` takes it to WRITTEN, and `ingest._line_history` counts it as the
accumulation base, so the next slip on the line is not flagged DISPUTED.

Before this, such a line was refused as "already shows" on every retry and
stayed WRITE_FAILED for good (RUNBOOK section 11, decision 2, the known dead
end; decided by Kiko 2026-11-02).

**Widening a CHECK constraint only.** No view reads `write_attempts`, so no
view dance. **The downgrade refuses a database holding ALREADY_PRESENT rows**,
by re-creating the narrower constraint over them -- the same choice as 0008 and
0014: those rows have no honest representation in 0014's vocabulary.

Revision ID: 0015
Revises: 0014
"""
from __future__ import annotations

from alembic import op


revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None

TABLE = 'write_attempts'
OUTCOME_CK = 'ck_write_attempts_outcome'

#: Frozen literals, per the freeze rule -- never imported from schema.py.
OUTCOME_0014 = "outcome IN ('SUCCESS','FAILED','REFUSED')"
OUTCOME_0015 = "outcome IN ('SUCCESS','FAILED','REFUSED','ALREADY_PRESENT')"


def upgrade() -> None:
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f(OUTCOME_CK), type_='check')
        batch_op.create_check_constraint(batch_op.f(OUTCOME_CK), OUTCOME_0015)


def downgrade() -> None:
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f(OUTCOME_CK), type_='check')
        batch_op.create_check_constraint(batch_op.f(OUTCOME_CK), OUTCOME_0014)
