"""
Which database the test suites build against.

SQLite in memory by default -- the fast loop, and what every run has used until
now. Set `PO_AGENT_TEST_DB_URL` (or pass pytest's `--mssql`) to point the same
suites at a real SQL Server instead.

    set PO_AGENT_TEST_DB_URL=mssql+pyodbc://sa:<pw>@localhost:1433/po_agent_test?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
    .venv\\Scripts\\python -m pytest -q
    .venv\\Scripts\\python test_schema.py

## Why this exists at all

Azure SQL is the deployment target; SQLite is the development database. Offline
DDL compilation proves the two agree about *generating* statements and says
nothing about running them -- collation, isolation levels, deadlocks, NULL
ordering, identity of empty string versus NULL, and every implicit conversion
SQLite performs and SQL Server refuses. **A schema change that has not been run
against the production dialect is untested against the thing it will run on.**
That is RUNBOOK section 8 lesson 19: two runners over one codebase disagreed, and
the disagreement was the finding. Here the two runners are two engines.

It is opt-in and NOT a replacement, deliberately, in the same shape as `--live`:
SQLite stays the loop you run on every edit, because a run that needs a server is
a run people stop doing.

## What is different when the target is a server

An in-memory SQLite database is new every time an engine is made, so `fresh_db()`
means "connect". A server is persistent, so the same call has to tear the schema
down first -- views before tables, because a view over a dropped table is an
error, and this schema has two.

## The one thing this module refuses to do

**If a target is configured and cannot be reached, it raises.** It does not fall
back to SQLite and it does not skip. A dual-dialect harness that quietly runs on
SQLite when the server is down reports the same green as a real run and proves
nothing -- the unfailable-check failure (section 8 lesson 18) in a new costume,
and the one that would matter most here because the whole point is to test the
dialect you did NOT get.
"""
from __future__ import annotations

import os

import schema as sc

#: Connection string for the alternate target. Empty/unset means SQLite.
TARGET_ENV = "PO_AGENT_TEST_DB_URL"

SQLITE_MEMORY = "sqlite://"


class TargetUnreachable(Exception):
    """A target was configured explicitly and could not be connected to."""


def target_url() -> str:
    """The configured target, or in-memory SQLite."""
    return (os.environ.get(TARGET_ENV) or "").strip() or SQLITE_MEMORY


def is_sqlite() -> bool:
    return target_url().startswith("sqlite")


def dialect_name() -> str:
    """`sqlite`, `mssql`, ... -- for labelling a run without opening a connection."""
    url = target_url()
    scheme = url.split(":", 1)[0]
    return scheme.split("+", 1)[0]


def describe() -> str:
    """One line naming the target, printed at the top of every suite."""
    if is_sqlite():
        return "database: SQLite in memory (set PO_AGENT_TEST_DB_URL to use SQL Server)"
    return f"database: {dialect_name().upper()} via {TARGET_ENV} -- REAL SERVER"


def _redacted(url: str) -> str:
    """
    A connection string with the credential masked BY SHAPE, never by value.

    This is deliberately structural: it finds the credential by position -- the
    text between the first `:` after `//` and the `@` that ends the userinfo --
    and never compares against, or holds, the secret itself. A masker built from
    a known password goes stale the instant the password rotates, which makes it
    fail OPEN on the new value while carrying the old one into every log line it
    touches. See RUNBOOK section 8 lesson 23.

    **Fails CLOSED on a malformed URL**, which is the case that matters, because a
    malformed URL is exactly what you are looking at when you call this. An
    earlier version returned the string untouched when it found no `@` -- so a
    truncated `scheme://user:password` (no host, no `@`) printed the password in
    full, in an error message, while looking redacted. That shape is not
    hypothetical; it is what a half-written `.env` line produces.
    """
    marker = url.find("//")
    if marker == -1:
        return url
    start = url.find(":", marker + 2)
    if start == -1:
        return url  # no credential segment at all, e.g. sqlite:// or //host/db

    # There must be a real userinfo before that colon, or the colon belongs to
    # something else entirely -- `sqlite:///:memory:` has one, and masking it
    # produced `sqlite:///:***`. A masker that mangles URLs carrying no secret is
    # a masker people route around, which is its own way of failing open.
    userinfo = url[marker + 2:start]
    if not userinfo or "/" in userinfo:
        return url

    # The LAST `@` before the path ends the userinfo. rfind rather than find so an
    # unencoded `@` inside the password cannot leave its tail exposed.
    end = url.rfind("@")
    if end == -1 or end < start:
        if url.find("/", start) != -1:
            # `host:port/path` -- a port, not a password. Nothing to hide.
            return url
        # No `@` and no path: a truncated credential running to the end of the
        # string. Mask to the end rather than give up.
        end = len(url)
    return url[:start + 1] + "***" + url[end:]


def connect():
    """
    An engine on the configured target, with the connection actually tested.

    `create_engine` is lazy about the *server* -- it succeeds against one that is
    not running -- but eager about the *driver*, which it imports immediately. So
    both failures are caught here and both are re-raised as `TargetUnreachable`
    with something actionable, rather than as a bare `ModuleNotFoundError` that
    reads like a broken harness.

    A configured-but-unreachable target raises. It never degrades to SQLite; see
    the module docstring for why that matters more here than anywhere else.
    """
    url = target_url()
    if is_sqlite():
        return sc.connect(url)

    try:
        engine = sc.connect(url)
    except ModuleNotFoundError as exc:
        raise TargetUnreachable(
            f"{TARGET_ENV} is set to {_redacted(url)}, but its database driver is not "
            f"installed: {exc}. Install requirements-dev.txt -- and note that pyodbc "
            "also needs Microsoft's ODBC Driver for SQL Server present on the machine; "
            "the Python package alone is not enough."
        ) from exc
    except Exception as exc:  # noqa: BLE001 -- re-raised with an actionable message
        raise TargetUnreachable(
            f"{TARGET_ENV} is set to {_redacted(url)} but an engine could not be built: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    try:
        with engine.connect():
            pass
    except Exception as exc:  # noqa: BLE001 -- re-raised with the actionable message
        raise TargetUnreachable(
            f"{TARGET_ENV} is set to {_redacted(url)} but the server could not be "
            f"reached: {type(exc).__name__}: {str(exc)[:300]}. "
            "Refusing to fall back to SQLite -- a dual-dialect run that silently runs "
            "on the dialect you were NOT testing reports green and proves nothing. "
            "Start the server, or unset the variable to run on SQLite deliberately."
        ) from exc
    return engine


def _drop_everything(engine) -> None:
    """Tear the schema down on a persistent server. Views first."""
    from sqlalchemy import text

    with engine.begin() as conn:
        for view, _ddl in sc.VIEWS:
            conn.execute(text(f"DROP VIEW IF EXISTS {view}"))
    sc.metadata.drop_all(engine)


def fresh_engine():
    """
    A built, seeded schema with both views -- on whichever target is configured.

    The suites' own `fresh_db()` delegates here, so one environment variable
    moves every DB-backed test onto SQL Server without touching a test.
    """
    from sqlalchemy import text

    engine = connect()
    if not is_sqlite():
        # Persistent server: the previous test's schema is still there.
        _drop_everything(engine)

    sc.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sc.change_states.insert(), [
            {"state": s, "is_terminal": t, "description": d}
            for s, t, d in sc.CHANGE_STATES])
        conn.execute(sc.change_state_transitions.insert(), [
            {"from_state": f, "to_state": t, "trigger": g, "actor_kind": a}
            for f, t, g, a in sc.CHANGE_STATE_TRANSITIONS])
        for _name, ddl in sc.VIEWS:
            conn.execute(text(ddl))
    return engine


def migration_url(sqlite_path) -> str:
    """
    The URL the migration tests should run Alembic against.

    On SQLite that is the caller's throwaway file. On a server it is the target
    itself -- running `alembic upgrade head` against the production dialect is the
    single most valuable thing this harness does, because batch mode, the view
    drop/recreate dance and every CHECK-constraint rename live only in migrations
    and never in `metadata.create_all`.
    """
    if is_sqlite():
        return f"sqlite:///{sqlite_path.as_posix()}"
    return target_url()
