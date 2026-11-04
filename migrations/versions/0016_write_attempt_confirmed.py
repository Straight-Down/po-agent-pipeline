"""write_attempts: a CONFIRMED outcome

Widens `ck_write_attempts_outcome` to admit a fifth value.

CONFIRMED means: the write path's re-read found the line, unchanged since the
proposal, already holding exactly the approved figure, so nothing was sent
(Kiko, 2026-11-04). Before this, such a line was sent anyway -- a same-value
PATCH that changed nothing -- recorded SUCCESS, and shown as "5 -> 5" under
"Written". That claimed a write. CONFIRMED is never a write: nothing is sent and
`payload_json` is '{}'. It settles the line like SUCCESS and ALREADY_PRESENT do
-- `write_path` takes it to WRITTEN and `ingest._line_history` counts the
approved figure as the accumulation base -- because the approved figure IS the
line's value.

**Widening a CHECK constraint only.** No view reads `write_attempts`, so no
view dance. **The downgrade refuses a database holding CONFIRMED rows**, by
re-creating the narrower constraint over them -- the same choice as 0008, 0014
and 0015: those rows have no honest representation in 0015's vocabulary.

Revision ID: 0016
Revises: 0015
"""
from __future__ import annotations

from alembic import op


revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None

TABLE = 'write_attempts'
OUTCOME_CK = 'ck_write_attempts_outcome'

#: Frozen literals, per the freeze rule -- never imported from schema.py.
OUTCOME_0015 = "outcome IN ('SUCCESS','FAILED','REFUSED','ALREADY_PRESENT')"
OUTCOME_0016 = "outcome IN ('SUCCESS','FAILED','REFUSED','ALREADY_PRESENT','CONFIRMED')"


def upgrade() -> None:
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f(OUTCOME_CK), type_='check')
        batch_op.create_check_constraint(batch_op.f(OUTCOME_CK), OUTCOME_0016)


def downgrade() -> None:
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f(OUTCOME_CK), type_='check')
        batch_op.create_check_constraint(batch_op.f(OUTCOME_CK), OUTCOME_0015)
