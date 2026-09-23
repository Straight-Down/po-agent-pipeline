"""
Alembic environment for the PO update pipeline.

Two things here are load-bearing rather than boilerplate:

- **`render_as_batch=True`.** SQLite cannot `ALTER TABLE ... DROP COLUMN` or drop a
  constraint, so any future migration that changes a column would be impossible
  against the development database. Batch mode does the create-copy-swap rebuild
  instead. It only works on named constraints, which is why `schema.py` sets a
  naming convention.
- **`PO_AGENT_DB_URL` overrides `alembic.ini`.** Azure SQL is reached through the
  environment, so a connection string carrying a password never lands in a file
  inside the OneDrive-synced project folder. It is a DIFFERENT variable from the
  test harness's `PO_AGENT_TEST_DB_URL`, and never falls back to it.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schema import metadata  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ONE variable, and deliberately NOT the harness's one.
#
# `PO_AGENT_DB_URL` names a database to MIGRATE. `PO_AGENT_TEST_DB_URL` names one
# the test harness may DROP EVERY TABLE on, ~25 times a run. Those are opposite
# safety requirements, so the safe default for one is the unsafe default for the
# other, and a single variable serving both is set correctly for at most one of
# them. This file never reads the harness variable and never falls back to it.
#
# Not hypothetical: on 2026-09-23 `PO_AGENT_TEST_DB_URL` was found pointing at the
# production database while the written instruction said the test one. See RUNBOOK
# section 8.
# REMOVED 2026-09-23: a guard that refused when PO_AGENT_DB_URL equalled
# PO_AGENT_TEST_DB_URL. Recorded because removing a guard is usually the wrong
# instinct and the next reader deserves the reasoning rather than a silent gap.
#
# It was added on the theory that setting both variables to one database
# re-created the single-variable hazard the split removed. It did not.
# `dialect_target` already refuses any harness target whose name does not end in
# `-test`, so if the two variables are equal then BOTH name a disposable
# database -- which is the ordinary way to migrate the test database and was
# never the hazard. The real protection is that reaching a database that matters
# requires setting a SEPARATE variable deliberately, and that is structural in
# the split itself, not in an equality check.
#
# What it actually did was block the legitimate workflow while protecting
# nothing, which is how a guard teaches people to route around it.
if os.environ.get("PO_AGENT_DB_URL"):
    config.set_main_option("sqlalchemy.url", os.environ["PO_AGENT_DB_URL"])

target_metadata = metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it -- for reviewing what will happen."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
