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
  inside the OneDrive-synced project folder.
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
