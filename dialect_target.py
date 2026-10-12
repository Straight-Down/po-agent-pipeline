"""
Which database the test suites build against.

SQLite in memory by default -- the fast loop, and what every run has used until
now. Set `PO_AGENT_TEST_DB_URL` (or pass pytest's `--mssql`) to point the same
suites at a real SQL Server instead.

    set PO_AGENT_TEST_DB_URL=mssql+pyodbc://po_agent_test_runner:<pw>@sql-po-agent-prod.database.windows.net/sqldb-po-agent-test?driver=ODBC+Driver+18+for+SQL+Server

The target must be named `*-test` and the login must NOT be `dbo` there (a
contained user of the test database only); both are refused otherwise.
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


class NotADisposableDatabase(Exception):
    """The harness was pointed at a database it is not allowed to destroy."""


#: Every database this harness may be pointed at must be named to end in this.
#:
#: NOT a convention -- a refusal, with no override and no escape hatch. The
#: harness calls `_drop_everything()` on its target once per test, roughly 85
#: times a run: it is a destructive tool by design, and the only thing standing
#: between it and a database that matters is the name it was handed.
#:
#: This exists because of a near-miss on 2026-09-23. `PO_AGENT_TEST_DB_URL` was
#: set to `sqldb-po-agent` -- the production database, explicitly ring-fenced in
#: the same breath -- while the instruction said `sqldb-po-agent-test`. The two
#: names differ by five characters. Nothing in the code could tell them apart,
#: because a URL is just a string and the harness had no opinion about which one
#: it deserved.
#:
#: If the harness is ever genuinely needed against something else, rename the
#: database. That is deliberately more work than editing a variable.
TEST_DATABASE_SUFFIX = "-test"


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


def _require_disposable(url) -> None:
    """
    Refuse a target this harness is not allowed to destroy.

    In-memory SQLite is exempt: there is nothing there to lose. Everything else
    -- a server database or a SQLite FILE -- must be named to end in
    `TEST_DATABASE_SUFFIX`, checked on the name alone with any extension
    stripped, so `po_agent-test.db` passes and `po_agent.db` does not.
    """
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    name = parsed.database
    if not name:
        return  # sqlite:// in memory -- nothing to destroy

    stem = name.rsplit(".", 1)[0] if name.rsplit(".", 1)[-1] in ("db", "sqlite", "sqlite3") else name
    if stem.endswith(TEST_DATABASE_SUFFIX):
        return

    raise NotADisposableDatabase(
        f"{TARGET_ENV} names the database {name!r}, which does not end in "
        f"{TEST_DATABASE_SUFFIX!r}.\n"
        "This harness DROPS EVERY TABLE on its target once per test -- about 85 times "
        "a run -- so it will only point at a database whose name says it is disposable. "
        "There is no override: if you genuinely need it here, rename the database.\n"
        f"(The variable is {TARGET_ENV}. Alembic reads a DIFFERENT one, PO_AGENT_DB_URL, "
        "precisely so that migrating a real database stays a separate, deliberate act.)"
    )


#: Connection ESTABLISHMENT is tried this many times in all, then it fails.
CONNECT_ATTEMPTS = 3
#: Seconds waited before the 2nd and 3rd attempts.
CONNECT_BACKOFF_SECONDS = (2, 5)
#: ODBC SQLSTATEs that mean "the connection could not be opened": link failure
#: and login timeout. Deliberately not 28000 (login REFUSED) or anything else --
#: a wrong password retried three times is three failed logins against a server
#: that locks accounts, and an error that is not about the link is a finding.
_CONNECTION_SQLSTATES = frozenset({"08001", "08S01", "HYT00", "HYT01"})

#: How many times this process retried opening a connection. Printed at the end
#: of every server run (`retry_summary`, conftest) -- a retry nobody is told
#: about turns a degrading link into a green run.
connect_retries = 0


def _is_connection_failure(exc: BaseException) -> bool:
    """True for a DBAPI error whose SQLSTATE says the connection never opened."""
    state = exc.args[0] if exc.args else None
    return isinstance(state, str) and state in _CONNECTION_SQLSTATES


def _establish(open_connection, sleep=None):
    """
    Open one DBAPI connection, retrying a failed OPEN a bounded number of times.

    **Only ever wraps the open itself.** A connection that does not exist yet
    cannot be inside a transaction, so retrying here can never replay a
    statement or paper over half-applied work. A statement that fails on an
    established connection is not seen by this function at all and still fails
    its test: that is a real event on a real link, and the suite should say so.
    """
    global connect_retries
    import time

    sleep = sleep or time.sleep
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            return open_connection()
        except Exception as exc:  # noqa: BLE001 -- re-raised unless it is the link
            if attempt == CONNECT_ATTEMPTS or not _is_connection_failure(exc):
                raise
            connect_retries += 1
            sleep(CONNECT_BACKOFF_SECONDS[attempt - 1])
    raise AssertionError("unreachable")  # pragma: no cover


def _retry_connection_establishment(engine) -> None:
    """Route every new DBAPI connection this engine's pool opens through `_establish`."""
    from sqlalchemy import event

    @event.listens_for(engine, "do_connect")
    def _do_connect(dialect, _conn_rec, cargs, cparams):
        return _establish(lambda: dialect.loaded_dbapi.connect(*cargs, **cparams))


def retry_summary() -> str:
    """One line for the end of a server run. Says zero out loud when it is zero."""
    if connect_retries == 0:
        return "database link: 0 connection retries"
    return (
        f"database link: {connect_retries} connection attempt(s) FAILED and were retried "
        f"(up to {CONNECT_ATTEMPTS} tries each). The run's result stands, but the link "
        "is degrading -- a count that grows between runs is the thing to act on."
    )


def connect(sqlite_file=None):
    """
    An engine on the configured target, with the connection actually tested.

    `sqlite_file` is for a caller that needs the SQLite database in a FILE (the
    review app answers on worker threads, and an in-memory SQLite database is
    private to one connection). It is ignored on a server target, which is the
    point: the caller keeps its file on SQLite and still moves with `--mssql`.
    The file must be named `*-test.db`, like every other target.

    `create_engine` is lazy about the *server* -- it succeeds against one that is
    not running -- but eager about the *driver*, which it imports immediately. So
    both failures are caught here and both are re-raised as `TargetUnreachable`
    with something actionable, rather than as a bare `ModuleNotFoundError` that
    reads like a broken harness.

    A configured-but-unreachable target raises. It never degrades to SQLite; see
    the module docstring for why that matters more here than anywhere else.
    """
    url = target_url()
    if is_sqlite() and sqlite_file is not None:
        from pathlib import Path

        url = f"sqlite:///{Path(sqlite_file).as_posix()}"
    # Checked BEFORE any connection: refusing after connecting would already have
    # proved the credentials work against something that must not be touched.
    _require_disposable(url)
    if is_sqlite():
        return sc.connect(url)

    try:
        engine = sc.connect(url)
        # Before the reachability test below, so the first connection of a run
        # gets the same bounded retry as every later one.
        _retry_connection_establishment(engine)
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
    _refuse_server_admin(engine)
    return engine


class RunningAsServerAdmin(Exception):
    """The harness is connected as `dbo` -- an identity that owns more than the test DB."""


def _refuse_server_admin(engine) -> None:
    """
    Refuse to run the harness as `dbo` (2026-10-01).

    `dbo` in the test database means the connection is the database owner by
    virtue of a SERVER-level identity -- on Azure SQL the server admin, which is
    `dbo` in EVERY database on the server, production's included. The test and
    production databases share a server, so a destructive suite connected that
    way is one mistyped name away from dropping production's tables. The fix is a
    contained user that exists only in the test database (RUNBOOK section 8
    lesson 25); this catches that fix being undone later -- the admin credential
    pasted back into `.env` because it was to hand.

    Called in two places, deliberately: at the end of `connect()` (so a run fails
    at startup), and at the top of `_drop_everything` (so NO teardown runs as dbo,
    whatever built the engine -- the migration tests build theirs with
    `sc.connect(url)` and never pass through `connect()`). On Azure SQL the
    Microsoft Entra admin is also mapped to `dbo`, per Microsoft's documentation --
    not verified against this server. Names the database user and the login it
    found; never the password.
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        user, login = conn.execute(text("SELECT USER_NAME(), SUSER_SNAME()")).one()
    if str(user).lower() == "dbo":
        engine.dispose()
        raise RunningAsServerAdmin(
            f"Refusing to run the test harness: {TARGET_ENV} connects as login {login!r}, "
            f"which is database user {user!r} -- the owner through a server-level identity, "
            "which on Azure SQL is dbo in EVERY database on the server, production's too. "
            "Connect as a contained user of the test database instead (CREATE USER ... WITH "
            "PASSWORD, in the test database only, added to db_owner there). See RUNBOOK "
            "section 8 lesson 25."
        )


def _drop_everything(engine) -> None:
    """
    Tear the schema down on a persistent server. Views first.

    `alembic_version` goes too. It is not in `sc.metadata`, so `drop_all` leaves
    it -- and a stale revision row is worse than no schema: Alembic reads it,
    concludes the database is already at head, and builds nothing, so the next
    thing to run finds `Invalid object name 'change_states'`. "Tear the schema
    down" has to mean the bookkeeping as well, or the database is left claiming
    to be something it is not.
    """
    from sqlalchemy import text

    # THE destructive operation checks the identity itself: callers reach it with
    # engines from `connect()` AND straight from `sc.connect(url)` (the migration
    # tests), so a check only in `connect()` left those paths unguarded.
    if engine.dialect.name != "sqlite":
        _refuse_server_admin(engine)
    with engine.begin() as conn:
        for view, _ddl in sc.VIEWS:
            conn.execute(text(f"DROP VIEW IF EXISTS {view}"))
    sc.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


class SupersededEngine(Exception):
    """An engine was used after a later `fresh_engine()` tore its schema down."""


#: The engine `fresh_engine()` last handed out on a SERVER target. None on SQLite.
_current_server_engine = None


def _retire(engine) -> None:
    """
    Make every later use of `engine` raise `SupersededEngine` (2026-10-12).

    In-memory SQLite gives each `fresh_engine()` call its own database, so a test
    may hold two at once. A server target is ONE database: the second call drops
    every table the first engine was looking at, and the first engine goes on
    working -- against the second's schema, silently. Two tests copied "run 1's
    rows" from an engine whose rows the next line had just dropped; they copied
    nothing, and the failure read as "a stored verdict is not found on SQL
    Server", a product defect that did not exist. Raising here names the real
    cause at the line that has it.
    """
    from sqlalchemy import event

    def refuse(_conn):
        raise SupersededEngine(
            "This engine came from an earlier fresh_engine() call. On a server target "
            "every call rebuilds the SAME database, so the rows it held were dropped by "
            "the later call. Read what you need from it BEFORE asking for the next one."
        )

    event.listen(engine, "engine_connect", refuse)
    engine.dispose()


def fresh_engine(sqlite_file=None):
    """
    A built, seeded schema with both views -- on whichever target is configured.

    The suites' own `fresh_db()` delegates here, so one environment variable
    moves every DB-backed test onto SQL Server without touching a test.
    `sqlite_file`: see `connect`.

    **On a server, each call invalidates the engine the previous call returned**
    (`_retire`): there is one database, and this call is about to empty it.
    """
    global _current_server_engine
    from sqlalchemy import text

    engine = connect(sqlite_file)
    if not is_sqlite():
        if _current_server_engine is not None:
            _retire(_current_server_engine)
        _current_server_engine = engine
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
