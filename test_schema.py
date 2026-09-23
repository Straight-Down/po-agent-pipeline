"""
Schema tests -- the constraints that would be expensive to discover later.

These pin the discovered facts the schema exists to satisfy, not the schema's
shape. A column rename should not break them; losing a dedup axis, a state
guarantee, or per-line write status should.

    python test_schema.py

The migration round-trip test drives real Alembic against a temporary SQLite
file, and also asserts that `schema.py` and migration 0001 have not drifted
apart. Everything else builds the tables from the metadata directly, for speed.
"""

from __future__ import annotations

import datetime as dt
import itertools
import tempfile
import traceback
from pathlib import Path

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

import dialect_target as dt_target
import schema as sc
from schema import (
    attachments,
    audit_log,
    change_candidates,
    change_state_transitions,
    change_states,
    message_attachments,
    messages,
    proposed_changes,
    shipment_pos,
    shipment_sources,
    shipments,
    write_attempts,
)

HERE = Path(__file__).resolve().parent
NOW = dt.datetime(2026, 8, 26, 9, 0, 0)
_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    _results.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  --  {detail}" if detail else ""))
    return bool(ok)


def expect_integrity(fn, name: str) -> None:
    try:
        fn()
    except IntegrityError as exc:
        check(True, name, str(exc.orig).splitlines()[0][:90])
        return
    except Exception as exc:  # noqa: BLE001
        check(False, name, f"raised {type(exc).__name__} instead of IntegrityError: {exc}")
        return
    check(False, name, "no error raised -- the constraint is not enforced")


def section(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 70 - len(title)))


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def fresh_db():
    """
    A schema built from the metadata, with the state machine and views in place.

    Delegates to `dialect_target`, so `PO_AGENT_TEST_DB_URL` (or pytest's
    `--mssql`) moves every test in this file onto a real SQL Server without
    changing a line here. Default is unchanged: SQLite in memory.
    """
    return dt_target.fresh_engine()

def mk_message(conn, graph_id="AAMkAGI1", **kw):
    row = {
        "id": sc.new_id(),
        "graph_message_id": graph_id,
        "mailbox": "shipments@straightdown.com",
        "subject": "SD-219 shipment",
        "from_address": "exports@inprotex.example",
        "received_at": NOW,
        "ingested_at": NOW,
        **kw,
    }
    conn.execute(messages.insert(), row)
    return row["id"]


def mk_attachment(conn, sha, doc_type="PACKING_LIST", **kw):
    row = {
        "content_sha256": sha,
        "byte_size": 4096,
        "doc_type": doc_type,
        "banned_as_data_source": False,
        "first_seen_at": NOW,
        **kw,
    }
    conn.execute(attachments.insert(), row)
    return sha


def mk_shipment(conn, origin="VENDOR_EMAIL", message_id=None, primary_sha=None, **kw):
    row = {
        "id": sc.new_id(),
        "origin": origin,
        "message_id": message_id,
        "primary_attachment_sha": primary_sha,
        "vendor_name": "Inprotex" if origin == "VENDOR_EMAIL" else None,
        "doc_needs_review": False,
        "needs_manual_entry": False,
        "created_by": "system" if origin == "VENDOR_EMAIL" else "paula@straightdown.com",
        "created_at": NOW,
        **kw,
    }
    conn.execute(shipments.insert(), row)
    return row["id"]


def mk_po(conn, shipment_id, printed="PO#1662", key="1662", **kw):
    row = {
        "id": sc.new_id(),
        "shipment_id": shipment_id,
        "po_number_printed": printed,
        "po_number_key": key,
        "ns_tranid": f"PO{int(key):07d}",
        "ns_internal_id": "8489541",
        "resolution_status": "RESOLVED",
        "resolved_at": NOW,
        **kw,
    }
    conn.execute(shipment_pos.insert(), row)
    return row["id"]


#: Distinct NetSuite line per fixture row. A real PO line carries exactly one
#: style/colour/size, so two different canonical keys can never target the same
#: line -- and `ux_proposed_changes_one_line_per_shipment` now enforces that.
#: The old fixture hardcoded line "18" on every row, which modelled something
#: NetSuite cannot represent; tests that specifically want a collision pass
#: `ns_line_id=` explicitly.
_fixture_line_no = itertools.count(100)


def mk_change(conn, shipment_id, po_id, size="s", state=sc.STATE_PENDING_REVIEW, **kw):
    row = {
        "id": sc.new_id(),
        "shipment_id": shipment_id,
        "shipment_po_id": po_id,
        "state": state,
        "key_style": "m120246",
        "key_color": "tid",
        "key_size": size,
        "src_style_text": "M120246",
        "src_color_text": "TID",
        "src_size_text": size.upper(),
        "src_quantity_text": "9",
        "source_hint": "PACKING!R42",
        "ns_line_id": str(next(_fixture_line_no)),
        "current_quantity": 12,
        "current_quantity_received": 0,
        "proposed_quantity": 9,
        "extraction_confidence": "high",
        "needs_review": False,
        "quantity_write_status": "NONE",
        "date_write_status": "NONE",
        "created_at": NOW,
        "updated_at": NOW,
        **kw,
    }
    conn.execute(proposed_changes.insert(), row)
    return row["id"]


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_migration_seed_matches_schema() -> None:
    """
    Migrate an empty database to head, then compare CONTENT against schema.py.

    This is the actual fix for migration 0001 having imported `schema.CHANGE_STATES`,
    `CHANGE_STATE_TRANSITIONS` and `VIEWS` live -- freezing those literals stops the
    drift that had already happened, and this stops the next one.

    Three properties, each chosen because the obvious weaker version misses a real
    failure:

    - **Content, not counts.** A flipped `is_terminal` or an edited description leaves
      the row count intact. Every column of every row is compared.
    - **Set equality BOTH ways.** A row in the database and absent from `schema.py`
      is the case that matters most: **removing** a transition from `schema.py`
      without a migration leaves it live in every existing database while a fresh one
      never gets it, and since `assert_transition` reads the table at runtime, the two
      genuinely disagree about what is legal. A one-directional check misses a
      deletion entirely.
    - **Normalised view DDL.** Compared with whitespace collapsed, so reformatting a
      view definition does not fail the test while a changed column list does.
    """
    section("migrating an empty database to head reproduces schema.py exactly")
    import re
    import tempfile as _tf

    from alembic import command
    from alembic.config import Config

    def norm(sql: str) -> str:
        return re.sub(r"\s+", " ", (sql or "").strip()).lower()

    with _tf.TemporaryDirectory() as td:
        db = Path(td) / "seed.db"
        cfg = Config(str(HERE / "alembic.ini"))
        cfg.set_main_option("script_location", str(HERE / "migrations"))
        seed_url = dt_target.migration_url(db)
        cfg.set_main_option("sqlalchemy.url", seed_url)
        command.upgrade(cfg, "head")

        engine = sc.connect(seed_url)
        with engine.connect() as conn:
            in_db_states = {
                (r[0], bool(r[1]), r[2])
                for r in conn.execute(text(
                    "SELECT state, is_terminal, description FROM change_states"))
            }
            in_db_trans = {
                (r[0], r[1], r[2], r[3])
                # Core, not raw SQL: `trigger` is reserved in BOTH dialects and
                # SQL Server rejects it unquoted. Letting the dialect quote it is
                # the entire point -- RUNBOOK section 8.
                for r in conn.execute(select(
                    change_state_transitions.c.from_state,
                    change_state_transitions.c.to_state,
                    change_state_transitions.c.trigger,
                    change_state_transitions.c.actor_kind))
            }
            # Each engine keeps view DDL in its own catalog, and neither name is
            # portable: `sqlite_master` on SQLite, `sys.sql_modules` on SQL
            # Server. The COMPARISON is the same either way -- normalised text
            # against schema.VIEWS -- so only the lookup branches.
            if dt_target.is_sqlite():
                view_sql = conn.execute(text(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'view'"))
            else:
                view_sql = conn.execute(text(
                    "SELECT v.name, m.definition FROM sys.views v "
                    "JOIN sys.sql_modules m ON m.object_id = v.object_id "
                    "WHERE v.is_ms_shipped = 0"))
            in_db_views = {r[0]: norm(r[1]) for r in view_sql}
        engine.dispose()

    want_states = {(s, bool(t), d) for s, t, d in sc.CHANGE_STATES}
    want_trans = {(f, t, g, a) for f, t, g, a in sc.CHANGE_STATE_TRANSITIONS}
    want_views = {n: norm(d) for n, d in sc.VIEWS}

    # -- change_states, every column, both directions -------------------------
    check(in_db_states == want_states,
          "change_states matches schema.py EXACTLY -- name, is_terminal and description",
          f"{len(in_db_states)} in db, {len(want_states)} declared")
    missing = want_states - in_db_states
    extra = in_db_states - want_states
    check(not missing, "no state declared in schema.py is missing from the database",
          str(sorted(s[0] for s in missing)) if missing else "none")
    check(not extra, "and no state in the database is absent from schema.py (a DELETION "
          "from schema.py with no migration would show up here)",
          str(sorted(s[0] for s in extra)) if extra else "none")

    # -- change_state_transitions, including the trigger STRING ---------------
    check(in_db_trans == want_trans,
          "change_state_transitions matches EXACTLY -- including trigger and actor_kind",
          f"{len(in_db_trans)} in db, {len(want_trans)} declared")
    t_missing = want_trans - in_db_trans
    t_extra = in_db_trans - want_trans
    check(not t_missing, "no declared transition is missing from the database",
          str(sorted((t[0], t[1]) for t in t_missing)) if t_missing else "none")
    check(not t_extra,
          "and no transition in the database is absent from schema.py -- the case that "
          "makes two databases at head disagree about legality",
          str(sorted((t[0], t[1]) for t in t_extra)) if t_extra else "none")
    # A mutated trigger keeps the (from, to) pair, so compare that dimension too.
    db_trigs = {(t[0], t[1]): (t[2], t[3]) for t in in_db_trans}
    want_trigs = {(t[0], t[1]): (t[2], t[3]) for t in want_trans}
    changed = [k for k in db_trigs.keys() & want_trigs.keys() if db_trigs[k] != want_trigs[k]]
    check(not changed, "and no trigger or actor_kind differs on a shared (from, to) pair",
          str(changed) if changed else "none")

    # -- views ----------------------------------------------------------------
    check(set(in_db_views) == set(want_views), "both views exist and no others",
          str(sorted(in_db_views)))
    for name in sorted(want_views):
        check(in_db_views.get(name) == want_views[name],
              f"{name} DDL matches schema.py (whitespace-normalised)",
              "differs" if in_db_views.get(name) != want_views[name] else "identical")


def test_migrations_import_no_application_code() -> None:
    """
    No migration may import project code. Source-level, so it cannot be evaded.

    Prevents the pattern returning rather than catching its consequences later: a
    migration that imports `schema` writes whatever that module holds when it runs,
    which makes the data depend on the calendar instead of the revision. Checked by
    reading the source rather than by importing, so a migration that would fail at
    import time is still audited.
    """
    section("migrations import no application code")
    import ast

    versions = sorted((HERE / "migrations" / "versions").glob("*.py"))
    check(len(versions) >= 4, "found the migration modules", f"{len(versions)} file(s)")

    # Everything importable from this project, so the check names the offender
    # rather than just failing.
    project_modules = {p.stem for p in HERE.glob("*.py")} - {"conftest"}

    for path in versions:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level:
                imported.add(f"(relative import level {node.level})")
        offenders = sorted(
            m for m in imported
            if m in project_modules or m.startswith("(relative")
        )
        check(not offenders, f"{path.name} imports no application code",
              f"imports {offenders}" if offenders else f"only {sorted(imported)}")


def test_migration_round_trip() -> None:
    section("migration: upgrade -> downgrade -> upgrade, and no drift")
    from alembic import command
    from alembic.config import Config
    from alembic.util.exc import AutogenerateDiffsDetected

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "roundtrip.db"
        config = Config(str(HERE / "alembic.ini"))
        config.set_main_option("script_location", str(HERE / "migrations"))
        # On a server this runs the migrations against the PRODUCTION dialect --
        # batch mode, the view drop/recreate dance and every constraint rename
        # live only in migrations and never in metadata.create_all.
        url = dt_target.migration_url(db)
        config.set_main_option("sqlalchemy.url", url)

        command.upgrade(config, "head")
        check(db.exists() or not dt_target.is_sqlite(),
              "upgrade head builds the database", dt_target.dialect_name())

        engine = sc.connect(url)
        names = set(inspect(engine).get_table_names())
        expected = {t.name for t in sc.metadata.sorted_tables}
        check(expected <= names, "every table in the metadata exists",
              str(sorted(expected - names)) if expected - names else "all present")
        check({"v_review_lines", "v_calibration"} <= set(inspect(engine).get_view_names()),
              "both views created", str(sorted(inspect(engine).get_view_names())))

        with engine.connect() as conn:
            seeded = conn.execute(select(change_states)).all()
            edges = conn.execute(select(change_state_transitions)).all()
        check(len(seeded) == len(sc.CHANGE_STATES),
              "the state machine ships seeded WITH the schema", f"{len(seeded)} states")
        check(len(edges) == len(sc.CHANGE_STATE_TRANSITIONS),
              "and so do its legal transitions", f"{len(edges)} edges")
        engine.dispose()

        # Drift check: the migration and schema.py must describe the same schema,
        # or the functional tests below (built from the metadata) prove nothing
        # about what the migration actually creates.
        try:
            command.check(config)
            check(True, "alembic check: no drift between schema.py and migration 0001")
        except AutogenerateDiffsDetected as exc:
            check(False, "alembic check: no drift", str(exc)[:120])

        command.downgrade(config, "base")
        engine = sc.connect(url)
        left = set(inspect(engine).get_table_names()) - {"alembic_version"}
        views_left = set(inspect(engine).get_view_names())
        engine.dispose()
        check(not left, "downgrade base removes every table", str(sorted(left)))
        check(not views_left, "and both views", str(sorted(views_left)))

        command.upgrade(config, "head")
        engine = sc.connect(url)
        check(expected <= set(inspect(engine).get_table_names()),
              "and it rebuilds cleanly -- the round trip is safe to rely on")
        engine.dispose()


def test_message_id_dedup() -> None:
    section("dedup axis 1: the Graph message id")
    engine = fresh_db()
    with engine.begin() as conn:
        mk_message(conn, "AAMkAGI1-first")
    # Mail.Read cannot mark a message read or move it, so redelivery is normal and
    # the database is the only place that can remember having seen one.
    with engine.begin() as conn:
        expect_integrity(
            lambda: mk_message(conn, "AAMkAGI1-first"),
            "the same graph_message_id cannot be ingested twice",
        )
    with engine.begin() as conn:
        mk_message(conn, "AAMkAGI1-second")
    with engine.connect() as conn:
        count = len(conn.execute(select(messages.c.id)).all())
    check(count == 2, "a genuinely different message still gets in", f"{count} messages")


def test_content_hash_dedup() -> None:
    section("dedup axis 2: attachment content (the re-forward case)")
    engine = fresh_db()
    sha = "a" * 64
    with engine.begin() as conn:
        first_msg = mk_message(conn, "msg-original")
        mk_attachment(conn, sha)
        conn.execute(message_attachments.insert(), {
            "id": sc.new_id(), "message_id": first_msg, "content_sha256": sha,
            "filename": "Invoice_Packing.xlsx"})
        shipment = mk_shipment(conn, message_id=first_msg, primary_sha=sha)
        conn.execute(shipment_sources.insert(), {
            "id": sc.new_id(), "shipment_id": shipment, "content_sha256": sha,
            "role": "PRIMARY"})

    # Paula forwards the vendor's mail: NEW message id, SAME bytes. The attachment
    # is keyed by content, so this is one attachment seen twice -- which is the
    # whole reason the table is not keyed by (message, filename).
    with engine.begin() as conn:
        second_msg = mk_message(conn, "msg-forwarded", forwarded_by="paula@straightdown.com")
        conn.execute(message_attachments.insert(), {
            "id": sc.new_id(), "message_id": second_msg, "content_sha256": sha,
            "filename": "FW Invoice_Packing.xlsx"})
    with engine.connect() as conn:
        blobs = len(conn.execute(select(attachments.c.content_sha256)).all())
        links = len(conn.execute(select(message_attachments.c.id)).all())
    check(blobs == 1, "one attachment row for the content", f"{blobs} attachment(s)")
    check(links == 2, "two message links to it", f"{links} link(s)")

    # And the content cannot become a second live shipment.
    with engine.begin() as conn:
        expect_integrity(
            lambda: mk_shipment(conn, message_id=second_msg, primary_sha=sha),
            "the same parsed content cannot start a second live shipment",
        )

    # A deliberate re-parse is still possible: supersede the first, then insert.
    with engine.begin() as conn:
        replacement = sc.new_id()
        # Order matters: the self-referencing foreign key means the replacement must
        # exist before the old row can point at it. Marking the old one superseded is
        # also what frees the filtered unique index.
        conn.execute(shipments.update()
                     .where(shipments.c.id == shipment)
                     .values(primary_attachment_sha=None))
        conn.execute(shipments.insert(), {
            "id": replacement, "origin": "VENDOR_EMAIL", "message_id": second_msg,
            "primary_attachment_sha": sha, "vendor_name": "Inprotex",
            "doc_needs_review": False, "needs_manual_entry": False,
            "created_by": "system", "created_at": NOW})
        conn.execute(shipments.update()
                     .where(shipments.c.id == shipment)
                     .values(superseded_by_shipment_id=replacement,
                             primary_attachment_sha=sha))
    with engine.connect() as conn:
        live = conn.execute(
            select(shipments.c.id).where(shipments.c.superseded_by_shipment_id.is_(None))
        ).all()
    check(len(live) == 1, "superseding the first makes room for a deliberate re-parse",
          f"{len(live)} live shipment")


def test_subset_reforward() -> None:
    section("dedup axis 2, harder: a forward carrying only SOME of the attachments")
    engine = fresh_db()
    packing, invoice = "b" * 64, "c" * 64
    with engine.begin() as conn:
        msg = mk_message(conn, "msg-six-attachments")
        mk_attachment(conn, packing)
        mk_attachment(conn, invoice, doc_type="COMMERCIAL_INVOICE")
        shipment = mk_shipment(conn, message_id=msg, primary_sha=packing,
                               source_set_hash="hash-of-both")
        for sha, role in ((packing, "PRIMARY"), (invoice, "EXCLUDED")):
            conn.execute(shipment_sources.insert(), {
                "id": sc.new_id(), "shipment_id": shipment, "content_sha256": sha,
                "role": role, "exclusion_reason": None if role == "PRIMARY" else
                "classified commercial_invoice, not a packing list"})

    # She forwards just the packing list. Different attachment SET, so
    # source_set_hash does not match -- the primary-attachment index still catches
    # it. Two axes because either one alone has a hole.
    with engine.begin() as conn:
        fwd = mk_message(conn, "msg-partial-forward", forwarded_by="paula@straightdown.com")
        expect_integrity(
            lambda: mk_shipment(conn, message_id=fwd, primary_sha=packing,
                                source_set_hash="hash-of-packing-only"),
            "a subset forward is still recognised as already ingested",
        )


def test_unresolved_multi_candidate() -> None:
    section("an unresolved multi-match is a STATE, not a null foreign key")
    engine = fresh_db()
    with engine.begin() as conn:
        ship = mk_shipment(conn, message_id=mk_message(conn, "msg-dup"), primary_sha=None)
        po = mk_po(conn, ship, "PO0001649", "1649")
        change = mk_change(conn, ship, po, size="all",
                           state=sc.STATE_NEEDS_RESOLUTION, ns_line_id=None,
                           current_quantity=None, current_quantity_received=None,
                           proposed_quantity=58,
                           attention_reason="2 open NetSuite lines match")
        for line_id, qty, recv, billed, open_ in (("1", 50, 0, 0, True),
                                                  ("2", 200, 100, 100, True)):
            conn.execute(change_candidates.insert(), {
                "id": sc.new_id(), "change_id": change, "ns_line_id": line_id,
                "quantity": qty, "quantity_received": recv, "quantity_billed": billed,
                "expected_receipt_date": dt.date(2026, 7, 1), "rate": 12.5,
                "is_open": open_, "selected": False})

    with engine.connect() as conn:
        cands = conn.execute(select(change_candidates.c.ns_line_id)).all()
    check(len(cands) == 2, "both candidate lines are recorded", f"{len(cands)} candidates")

    # The refusal is structural: no target line, no approval. This is the check
    # that stops "null FK plus a comment" from being the design.
    with engine.begin() as conn:
        expect_integrity(
            lambda: conn.execute(proposed_changes.update()
                                 .where(proposed_changes.c.id == change)
                                 .values(state=sc.STATE_APPROVED,
                                         approved_quantity=58,
                                         quantity_approved_by="paula@straightdown.com",
                                         quantity_approved_at=NOW,
                                         quantity_write_status="APPROVED")),
            "APPROVED without a target line is rejected by the database",
        )

    # A human picks one, and only one.
    with engine.begin() as conn:
        conn.execute(change_candidates.update()
                     .where(change_candidates.c.change_id == change)
                     .where(change_candidates.c.ns_line_id == "2")
                     .values(selected=True))
    with engine.begin() as conn:
        expect_integrity(
            lambda: conn.execute(change_candidates.update()
                                 .where(change_candidates.c.change_id == change)
                                 .where(change_candidates.c.ns_line_id == "1")
                                 .values(selected=True)),
            "two selected candidates on one change is impossible",
        )
    with engine.begin() as conn:
        conn.execute(proposed_changes.update()
                     .where(proposed_changes.c.id == change)
                     .values(state=sc.STATE_APPROVED, ns_line_id="2",
                             human_verdict="CANDIDATE_PICKED",
                             verdict_by="paula@straightdown.com", verdict_at=NOW,
                             approved_quantity=58,
                             quantity_approved_by="paula@straightdown.com",
                             quantity_approved_at=NOW, quantity_write_status="APPROVED"))
    with engine.connect() as conn:
        row = conn.execute(select(proposed_changes.c.state, proposed_changes.c.ns_line_id)
                           .where(proposed_changes.c.id == change)).one()
    check(row.state == sc.STATE_APPROVED and row.ns_line_id == "2",
          "once a line is chosen, the same approval succeeds", f"{row.state}/{row.ns_line_id}")


def test_partial_write_failure_is_per_line() -> None:
    section("partial failure: per line and per scope, recoverable without re-approval")
    engine = fresh_db()
    with engine.begin() as conn:
        ship = mk_shipment(conn, message_id=mk_message(conn, "msg-six-pos"), primary_sha=None)
        # One shipment, two POs -- the fan-out that makes partial failure possible.
        po_a = mk_po(conn, ship, "PO#1662", "1662")
        po_b = mk_po(conn, ship, "PO#1667", "1667")
        ids = {
            "a_s": mk_change(conn, ship, po_a, size="s", state=sc.STATE_APPROVED,
                             approved_quantity=9, quantity_approved_by="paula@sd.com",
                             quantity_approved_at=NOW, quantity_write_status="APPROVED"),
            "a_m": mk_change(conn, ship, po_a, size="m", state=sc.STATE_APPROVED,
                             approved_quantity=50, quantity_approved_by="paula@sd.com",
                             quantity_approved_at=NOW, quantity_write_status="APPROVED"),
            "b_l": mk_change(conn, ship, po_b, size="l", state=sc.STATE_APPROVED,
                             approved_quantity=20, quantity_approved_by="paula@sd.com",
                             quantity_approved_at=NOW, quantity_write_status="APPROVED"),
        }

    def attempt(conn, change_id, no, outcome, error_kind=None):
        conn.execute(write_attempts.insert(), {
            "id": sc.new_id(), "change_id": change_id, "scope": "QUANTITY",
            "attempt_no": no, "ns_internal_id": "8489541", "ns_line_id": "18",
            "payload_json": '{"quantity": 9}', "idempotency_key": sc.new_id(),
            "outcome": outcome, "http_status": 204 if outcome == "SUCCESS" else 400,
            "error_kind": error_kind, "attempted_at": NOW})

    with engine.begin() as conn:
        attempt(conn, ids["a_s"], 1, "SUCCESS")
        attempt(conn, ids["a_m"], 1, "SUCCESS")
        attempt(conn, ids["b_l"], 1, "FAILED", "TRANSIENT")
        for key, status, state in (("a_s", "WRITTEN", sc.STATE_WRITTEN),
                                   ("a_m", "WRITTEN", sc.STATE_WRITTEN),
                                   ("b_l", "FAILED", sc.STATE_WRITE_FAILED)):
            conn.execute(proposed_changes.update()
                         .where(proposed_changes.c.id == ids[key])
                         .values(quantity_write_status=status, state=state))

    with engine.connect() as conn:
        rows = {r.id: r for r in conn.execute(select(
            proposed_changes.c.id, proposed_changes.c.state,
            proposed_changes.c.quantity_write_status,
            proposed_changes.c.quantity_approved_at)).all()}
    check(rows[ids["a_s"]].state == sc.STATE_WRITTEN
          and rows[ids["b_l"]].state == sc.STATE_WRITE_FAILED,
          "one line failing leaves the others WRITTEN -- status is per line",
          f"{rows[ids['a_s']].state} / {rows[ids['b_l']].state}")

    # Recovery: find the failures, retry only those. Nothing is re-approved.
    with engine.connect() as conn:
        retryable = conn.execute(select(proposed_changes.c.id).where(
            proposed_changes.c.quantity_write_status == "FAILED")).scalars().all()
    check(retryable == [ids["b_l"]], "the retry query finds exactly the failed line",
          f"{len(retryable)} to retry")

    with engine.begin() as conn:
        attempt(conn, ids["b_l"], 2, "SUCCESS")
        conn.execute(proposed_changes.update()
                     .where(proposed_changes.c.id == ids["b_l"])
                     .values(quantity_write_status="WRITTEN", state=sc.STATE_WRITTEN))
    with engine.connect() as conn:
        after = {r.id: r for r in conn.execute(select(
            proposed_changes.c.id, proposed_changes.c.state,
            proposed_changes.c.quantity_approved_at)).all()}
        history = conn.execute(select(write_attempts.c.attempt_no, write_attempts.c.outcome)
                               .where(write_attempts.c.change_id == ids["b_l"])
                               .order_by(write_attempts.c.attempt_no)).all()
    check(all(r.state == sc.STATE_WRITTEN for r in after.values()),
          "after the retry every line is WRITTEN")
    check(after[ids["a_s"]].quantity_approved_at == rows[ids["a_s"]].quantity_approved_at,
          "and the lines that succeeded were never re-approved")
    check([(h.attempt_no, h.outcome) for h in history] == [(1, "FAILED"), (2, "SUCCESS")],
          "both attempts survive as history -- the log is append-only",
          str([(h.attempt_no, h.outcome) for h in history]))
    with engine.begin() as conn:
        expect_integrity(
            lambda: attempt(conn, ids["b_l"], 2, "SUCCESS"),
            "and an attempt number cannot be reused",
        )


def test_quantity_without_date() -> None:
    section("quantity and date approve separately, and the date is optional forever")
    engine = fresh_db()
    with engine.begin() as conn:
        ship = mk_shipment(conn, message_id=mk_message(conn, "msg-qty-only"), primary_sha=None)
        po = mk_po(conn, ship, "PO#1662", "1662")
        change = mk_change(conn, ship, po, state=sc.STATE_APPROVED,
                           approved_quantity=9, quantity_approved_by="paula@sd.com",
                           quantity_approved_at=NOW, quantity_write_status="APPROVED")

    # Quantity is knowable from the slip on arrival; the receipt date waits on the
    # freight forwarder and may never be supplied for this line at all.
    with engine.begin() as conn:
        conn.execute(write_attempts.insert(), {
            "id": sc.new_id(), "change_id": change, "scope": "QUANTITY", "attempt_no": 1,
            "ns_internal_id": "8489541", "ns_line_id": "18",
            "payload_json": '{"quantity": 9}', "idempotency_key": sc.new_id(),
            "outcome": "SUCCESS", "http_status": 204, "attempted_at": NOW})
        conn.execute(proposed_changes.update()
                     .where(proposed_changes.c.id == change)
                     .values(quantity_write_status="WRITTEN", state=sc.STATE_WRITTEN))
    with engine.connect() as conn:
        row = conn.execute(select(proposed_changes).where(
            proposed_changes.c.id == change)).one()
    check(row.state == sc.STATE_WRITTEN and row.date_write_status == "NONE"
          and row.confirmed_receipt_date is None,
          "WRITTEN with the quantity applied and NO date -- a legal resting place",
          f"{row.state}, date_write_status={row.date_write_status}")

    # WRITTEN is not terminal, and this is the transition that proves it. Asked of
    # the DATABASE, which is the runtime authority -- see schema.legal_transitions.
    with engine.connect() as conn:
        check((sc.STATE_WRITTEN, sc.STATE_APPROVED) in sc.legal_transitions(conn),
              "WRITTEN -> APPROVED is legal, for a date supplied later")
    with engine.connect() as conn:
        terminal = conn.execute(select(change_states.c.is_terminal).where(
            change_states.c.state == sc.STATE_WRITTEN)).scalar()
    check(not terminal, "and WRITTEN is not marked terminal")

    with engine.begin() as conn:
        conn.execute(proposed_changes.update()
                     .where(proposed_changes.c.id == change)
                     .values(state=sc.STATE_APPROVED,
                             confirmed_receipt_date=dt.date(2026, 9, 10),
                             date_approved_by="paula@straightdown.com",
                             date_approved_at=NOW, date_write_status="APPROVED"))
        conn.execute(write_attempts.insert(), {
            "id": sc.new_id(), "change_id": change, "scope": "DATE", "attempt_no": 1,
            "ns_internal_id": "8489541", "ns_line_id": "18",
            # All three fields, same value: NetSuite does not derive
            # expectedReceiptDate from the override pair (tested 2026-08-12).
            "payload_json": '{"expectedReceiptDate": "2026-09-10", '
                            '"custcol_sd_updatedreceiptdate": "2026-09-10", '
                            '"custcol_override_expected_receipt": true}',
            "idempotency_key": sc.new_id(), "outcome": "SUCCESS", "http_status": 204,
            "attempted_at": NOW})
        conn.execute(proposed_changes.update()
                     .where(proposed_changes.c.id == change)
                     .values(date_write_status="WRITTEN", state=sc.STATE_WRITTEN))
    with engine.connect() as conn:
        scopes = conn.execute(select(write_attempts.c.scope)
                              .where(write_attempts.c.change_id == change)).scalars().all()
        final = conn.execute(select(proposed_changes.c.quantity_write_status,
                                    proposed_changes.c.date_write_status)
                             .where(proposed_changes.c.id == change)).one()
    check(sorted(scopes) == ["DATE", "QUANTITY"],
          "the date is written as its own attempt, weeks later", str(sorted(scopes)))
    check(final.quantity_write_status == "WRITTEN" and final.date_write_status == "WRITTEN",
          "both scopes now WRITTEN, each on its own timeline")

    # And a date can never exist without a human's name against it.
    with engine.begin() as conn:
        po2 = mk_po(conn, ship, "PO#1667", "1667")
        expect_integrity(
            lambda: mk_change(conn, ship, po2, confirmed_receipt_date=dt.date(2026, 9, 10),
                              date_approved_by=None),
            "a receipt date with no approver is rejected -- dates never come from a document",
        )


def test_canonical_key_identity() -> None:
    section("row identity is the canonical key, never a digest of the row")
    engine = fresh_db()
    with engine.begin() as conn:
        ship = mk_shipment(conn, message_id=mk_message(conn, "msg-keys"), primary_sha=None)
        po = mk_po(conn, ship, "PO#1662", "1662")
        mk_change(conn, ship, po, size="s")

    # Verbatim text varies between extraction runs by design; the key does not. A
    # re-parse that renders the colour differently must collide with the existing
    # row rather than create a phantom second one.
    with engine.begin() as conn:
        expect_integrity(
            lambda: mk_change(conn, ship, po, size="s", src_color_text="T I D",
                              src_quantity_text="9 pcs"),
            "same canonical key, different printed text -> rejected as a duplicate",
        )
    with engine.begin() as conn:
        mk_change(conn, ship, po, size="m")
    with engine.connect() as conn:
        sizes = conn.execute(select(proposed_changes.c.key_size)).scalars().all()
    check(sorted(sizes) == ["m", "s"], "a different size is a different row", str(sorted(sizes)))

    # Sizeless rows are exempt: extraction deliberately never collapses them, so
    # several can coexist. This is why key_size stores '' and not NULL -- NULLs
    # compare as equal in a unique index on Azure SQL and as distinct on SQLite,
    # so a nullable key column would enforce two different rules.
    with engine.begin() as conn:
        mk_change(conn, ship, po, size="", src_size_text="")
        mk_change(conn, ship, po, size="", src_size_text="", source_hint="PACKING!R71")
    with engine.connect() as conn:
        sizeless = conn.execute(select(proposed_changes.c.id).where(
            proposed_changes.c.key_size == "")).all()
    check(len(sizeless) == 2, "two sizeless rows coexist on one PO", f"{len(sizeless)} rows")


def test_five_review_figures() -> None:
    section("the five figures the review screen needs, outstanding derived")
    engine = fresh_db()
    with engine.begin() as conn:
        ship = mk_shipment(conn, message_id=mk_message(conn, "msg-figures"), primary_sha=None)
        po = mk_po(conn, ship, "PO#1721", "1721")
        # "ordered 300, received 0, this slip 128" -- a partial delivery is
        # self-evident from the numbers. Nothing in the schema gates on them; the
        # gating version of this was built and cancelled (commit 6045b78).
        mk_change(conn, ship, po, size="m", ns_line_id="4",
                  current_quantity=300, current_quantity_received=0, proposed_quantity=128)
        mk_change(conn, ship, po, size="l", ns_line_id="5",
                  current_quantity=300, current_quantity_received=128, proposed_quantity=172)

    with engine.connect() as conn:
        rows = {r.size_printed: r for r in conn.execute(text(
            "SELECT size_printed, ns_line_id, current_quantity, "
            "current_quantity_received, proposed_quantity, outstanding "
            "FROM v_review_lines")).all()}
    m, l = rows["M"], rows["L"]
    check(float(m.outstanding) == 300.0,
          "300 ordered, 0 received -> 300 outstanding", str(m.outstanding))
    check(float(l.outstanding) == 172.0,
          "300 ordered, 128 received -> 172 outstanding", str(l.outstanding))
    check(all(getattr(m, f) is not None for f in
              ("ns_line_id", "current_quantity", "current_quantity_received",
               "proposed_quantity", "outstanding")),
          "all five figures reach the review screen from one row")
    check("current_quantity_received" in {c["name"] for c in
                                          inspect(engine).get_columns("proposed_changes")},
          "current_quantity_received persists on EVERY change, not just multi-match ones")

    # A single-match change has no candidate rows at all, which is exactly why the
    # figure cannot live only on change_candidates.
    with engine.connect() as conn:
        cands = conn.execute(select(change_candidates.c.id)).all()
    check(not cands, "and this shipment has no candidate rows to have held it")


def test_calibration_pairing() -> None:
    section("calibration: the tool's claim beside what turned out to be true")
    engine = fresh_db()
    with engine.begin() as conn:
        ship = mk_shipment(conn, message_id=mk_message(conn, "msg-calib"), primary_sha=None,
                           parser="claude", extractor_model="claude-opus-5",
                           extractor_prompt_version="v3", doc_needs_review=True)
        po = mk_po(conn, ship, "PO#1662", "1662")
        # Accepted as proposed: a NEGATIVE for calibration, and the one a review UI
        # is most likely to omit. Without it there is nothing to calibrate against.
        mk_change(conn, ship, po, size="s", needs_review=True,
                  human_verdict="ACCEPTED", verdict_by="paula@sd.com", verdict_at=NOW,
                  approved_quantity=9, quantity_approved_by="paula@sd.com",
                  quantity_approved_at=NOW, quantity_write_status="APPROVED",
                  state=sc.STATE_APPROVED)
        # Corrected: a proven extraction error, derivable without a form field.
        mk_change(conn, ship, po, size="m", needs_review=True, proposed_quantity=50,
                  human_verdict="CORRECTED", verdict_by="paula@sd.com", verdict_at=NOW,
                  approved_quantity=52, quantity_approved_by="paula@sd.com",
                  quantity_approved_at=NOW, quantity_write_status="APPROVED",
                  state=sc.STATE_APPROVED)

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT human_verdict, needs_review, quantity_was_corrected, parser, "
            "extractor_prompt_version FROM v_calibration ORDER BY human_verdict")).all()
    check(len(rows) == 2, "both verdicts land in the calibration view", f"{len(rows)} rows")
    accepted, corrected = rows
    check(accepted.human_verdict == "ACCEPTED" and accepted.quantity_was_corrected == 0,
          "an accepted line records as not corrected -- the negative case")
    check(corrected.human_verdict == "CORRECTED" and corrected.quantity_was_corrected == 1,
          "a changed quantity is detected without anyone filling in a field")
    check(all(r.parser == "claude" and r.extractor_prompt_version == "v3" for r in rows),
          "and each is attributable to a parser and prompt version")


def test_two_workflows_are_distinguishable() -> None:
    section("two workflows, one set of tables, six months later still answerable")
    engine = fresh_db()
    with engine.begin() as conn:
        # Workflow 1: vendor document arrives, tool proposes, Paula approves.
        msg = mk_message(conn, "msg-packing-slip")
        sha = mk_attachment(conn, "d" * 64)
        slip_ship = mk_shipment(conn, message_id=msg, primary_sha=sha)
        slip_po = mk_po(conn, slip_ship, "PO#1662", "1662")
        slip_change = mk_change(conn, slip_ship, slip_po)

        # Workflow 2: Paula pulls a size forward by air. No email, no document, no
        # proposal step -- she names the line and the date and the tool executes.
        direct_ship = mk_shipment(conn, origin="PAULA_DIRECTED")
        direct_po = mk_po(conn, direct_ship, "PO#1721", "1721")
        direct_change = mk_change(
            conn, direct_ship, direct_po, size="xl", state=sc.STATE_APPROVED,
            src_quantity_text="", proposed_quantity=40, approved_quantity=40,
            quantity_approved_by="paula@straightdown.com", quantity_approved_at=NOW,
            quantity_write_status="APPROVED",
            confirmed_receipt_date=dt.date(2026, 9, 2),
            date_approved_by="paula@straightdown.com", date_approved_at=NOW,
            date_write_status="APPROVED")

        for change, workflow, actor, kind, event, frm, to in (
            (slip_change, "PACKING_SLIP", "system", "SYSTEM", "PROPOSED",
             sc.STATE_INSERT, sc.STATE_PENDING_REVIEW),
            (slip_change, "PACKING_SLIP", "paula@straightdown.com", "HUMAN", "APPROVED",
             sc.STATE_PENDING_REVIEW, sc.STATE_APPROVED),
            (direct_change, "PAULA_DIRECTED", "paula@straightdown.com", "HUMAN",
             "INSTRUCTED", sc.STATE_INSERT, sc.STATE_APPROVED),
        ):
            conn.execute(audit_log.insert(), {
                "id": sc.new_id(), "occurred_at": NOW, "workflow": workflow,
                "actor": actor, "actor_kind": kind, "event": event,
                "change_id": change, "from_state": frm, "to_state": to})

    with engine.connect() as conn:
        slip_story = conn.execute(select(audit_log.c.workflow, audit_log.c.event,
                                         audit_log.c.actor_kind)
                                  .where(audit_log.c.change_id == slip_change)
                                  .order_by(audit_log.c.event)).all()
        direct_story = conn.execute(select(audit_log.c.workflow, audit_log.c.event,
                                           audit_log.c.actor_kind)
                                    .where(audit_log.c.change_id == direct_change)).all()
    check([(r.workflow, r.event) for r in slip_story]
          == [("PACKING_SLIP", "APPROVED"), ("PACKING_SLIP", "PROPOSED")],
          "the packing-slip line has a proposal AND an approval",
          str([r.event for r in slip_story]))
    check([(r.workflow, r.event, r.actor_kind) for r in direct_story]
          == [("PAULA_DIRECTED", "INSTRUCTED", "HUMAN")],
          "the Paula-directed line has one human instruction and no proposal",
          str([r.event for r in direct_story]))

    # The shape of a Paula-directed shipment is enforced, not merely intended: a
    # future reader seeing vendor_name NULL is looking at the design, not a bug.
    with engine.begin() as conn:
        expect_integrity(
            lambda: mk_shipment(conn, origin="PAULA_DIRECTED",
                                message_id=mk_message(conn, "msg-wrong-shape")),
            "a PAULA_DIRECTED shipment cannot carry an email",
        )
    with engine.begin() as conn:
        expect_integrity(
            lambda: mk_shipment(conn, origin="VENDOR_EMAIL", message_id=None),
            "and a VENDOR_EMAIL shipment cannot lack one",
        )


def test_state_machine_is_data() -> None:
    section("the state machine is data, and illegal transitions are absent from it")
    engine = fresh_db()
    with engine.connect() as conn:
        states = set(conn.execute(select(change_states.c.state)).scalars().all())
        edges = {(r.from_state, r.to_state) for r in
                 conn.execute(select(change_state_transitions)).all()}

    check("WRITE_FAILED" in states, "WRITE_FAILED is the name, not FAILED")
    check("FAILED" not in states, "and bare FAILED is not a state at all")
    for state in (sc.STATE_PENDING_REVIEW, sc.STATE_NEEDS_ATTENTION,
                  sc.STATE_NEEDS_RESOLUTION, sc.STATE_APPROVED, sc.STATE_WRITTEN,
                  sc.STATE_MANUAL_ENTRY_REQUIRED):
        check(state in states, f"{state} exists")

    check((sc.STATE_WRITTEN, sc.STATE_APPROVED) in edges,
          "WRITTEN -> APPROVED is legal (a date supplied later)")
    check((sc.STATE_NEEDS_RESOLUTION, sc.STATE_APPROVED) in edges,
          "NEEDS_RESOLUTION -> APPROVED is legal (a human picked a candidate)")
    for illegal in ((sc.STATE_WRITTEN, sc.STATE_PENDING_REVIEW),
                    (sc.STATE_DISCARDED, sc.STATE_APPROVED),
                    (sc.STATE_SUPERSEDED, sc.STATE_APPROVED),
                    (sc.STATE_INSERT, sc.STATE_WRITTEN),
                    (sc.STATE_NEEDS_RESOLUTION, sc.STATE_WRITTEN)):
        check(illegal not in edges, f"{illegal[0]} -> {illegal[1]} is NOT legal")
        with engine.connect() as conn:
            try:
                sc.assert_transition(conn, *illegal)
                check(False, "and the guard refuses it", "no exception raised")
            except sc.IllegalTransition:
                check(True, "and the guard refuses it")

    # The table is the runtime authority; CHANGE_STATE_TRANSITIONS is the authoring
    # source that seeds it. They must agree, or the seeding is decoration -- this is
    # the drift check that earns the table its keep.
    with engine.connect() as conn:
        from_db = sc.legal_transitions(conn)
    from_const = {(f, t) for f, t, _g, _a in sc.CHANGE_STATE_TRANSITIONS}
    check(from_db == from_const,
          "the seeded table and the authoring constant agree exactly",
          str(sorted(from_db ^ from_const)) if from_db != from_const else "no drift")

    with engine.connect() as conn:
        terminal = set(conn.execute(select(change_states.c.state).where(
            # `.is_(True)` renders `IS 1`, which SQLite accepts and SQL Server
            # rejects -- there, IS pairs only with NULL. `== True` renders
            # `= 1` on both. (`.is_(None)` stays correct everywhere.)
            change_states.c.is_terminal == True)).scalars().all())  # noqa: E712
    check(terminal == {sc.STATE_DISCARDED, sc.STATE_SUPERSEDED},
          "only DISCARDED and SUPERSEDED are terminal", str(sorted(terminal)))

    # A status typo is an integrity error rather than a row nobody notices.
    with engine.begin() as conn:
        ship = mk_shipment(conn, message_id=mk_message(conn, "msg-typo"), primary_sha=None)
        po = mk_po(conn, ship, "PO#1662", "1662")
        expect_integrity(
            lambda: mk_change(conn, ship, po, state="PENDNIG_REVIEW"),
            "a misspelled state is rejected by the foreign key",
        )


def test_colour_provenance_columns() -> None:
    section("migration 0002: colour resolution provenance")
    engine = fresh_db()
    with engine.begin() as conn:
        ship = mk_shipment(conn, message_id=mk_message(conn, "msg-colour"), primary_sha=None)
        po = mk_po(conn, ship, "PO#1720", "1720")

    # A NAME resolution has to carry its evidence. This is the check that makes the
    # column set provenance rather than an assertion.
    with engine.begin() as conn:
        expect_integrity(
            lambda: mk_change(conn, ship, po, size="m",
                              colour_resolution_method="NAME",
                              colour_printed_key="new indigo",
                              colour_resolved_code="nin",
                              colour_resolved_name=None,
                              colour_name_source_item_id=None),
            "a NAME resolution without the name and its source item is rejected",
        )
    with engine.begin() as conn:
        change = mk_change(conn, ship, po, size="m",
                           colour_resolution_method="NAME",
                           colour_printed_key="new indigo",
                           colour_resolved_code="nin",
                           colour_resolved_name="New Indigo",
                           colour_name_source_item_id="51669")
    with engine.connect() as conn:
        row = conn.execute(select(proposed_changes).where(
            proposed_changes.c.id == change)).one()
    check(row.colour_resolution_method == "NAME"
          and row.colour_resolved_code == "nin"
          and row.colour_resolved_name == "New Indigo"
          and row.colour_name_source_item_id == "51669",
          "a complete NAME resolution persists",
          f"{row.colour_printed_key} -> {row.colour_resolved_code}")

    # CODE needs no attribution: the printed value WAS the code.
    with engine.begin() as conn:
        mk_change(conn, ship, po, size="l", colour_resolution_method="CODE",
                  colour_printed_key="mlt", colour_resolved_code="mlt")
    with engine.connect() as conn:
        row = conn.execute(select(proposed_changes).where(
            proposed_changes.c.key_size == "l")).one()
    check(row.colour_resolution_method == "CODE" and row.colour_resolved_name is None,
          "a CODE resolution persists without a name source")

    for method in ("AMBIGUOUS", "UNRESOLVED"):
        with engine.begin() as conn:
            mk_change(conn, ship, po, size=method.lower()[:2],
                      colour_resolution_method=method, colour_printed_key="puce")
        check(True, f"{method} is an accepted method")
    with engine.begin() as conn:
        expect_integrity(
            lambda: mk_change(conn, ship, po, size="xl",
                              colour_resolution_method="GUESSED"),
            "an unknown resolution method is rejected",
        )

    # The review screen reads it from the view, which is where the question is asked.
    with engine.connect() as conn:
        cols = conn.execute(text("SELECT * FROM v_review_lines")).keys()
    for column in ("colour_resolution_method", "colour_resolved_code",
                   "colour_resolved_name"):
        check(column in cols, f"v_review_lines exposes {column}")
    with engine.connect() as conn:
        answer = conn.execute(text(
            "SELECT color_printed, colour_resolved_code, colour_resolved_name "
            "FROM v_review_lines WHERE colour_resolution_method = 'NAME'")).one()
    check(answer.colour_resolved_name == "New Indigo",
          "so 'why did NEW INDIGO become NIN' is one query",
          f"{answer.color_printed} -> {answer.colour_resolved_code} "
          f"({answer.colour_resolved_name})")


def test_scope_boundaries_in_the_schema() -> None:
    section("scope boundaries the schema itself enforces")
    engine = fresh_db()
    inspector = inspect(engine)

    # The tool never touches custcol_sd_fg_excluderepspark -- not read, not
    # written, not stored. Asserted by introspection so it cannot creep in.
    offenders = [
        f"{table}.{col['name']}"
        for table in inspector.get_table_names()
        for col in inspector.get_columns(table)
        if "repspark" in col["name"].lower()
    ]
    check(not offenders, "no column anywhere mentions repspark", str(offenders))

    # No table or state represents creating a PO line. The only line reference is
    # ns_line_id, always to something NetSuite already has.
    check("NEW_LINE" not in {s for s, _t, _d in sc.CHANGE_STATES},
          "there is no new-line state")
    check(not any("create" in t or "new_line" in t for t in inspector.get_table_names()),
          "and no table for lines the tool would create",
          str(inspector.get_table_names()))

    # Vendor dates live on the shipment, deliberately away from the line, and there
    # is no proposed-date column for a derived value to occupy.
    change_cols = {c["name"] for c in inspector.get_columns("proposed_changes")}
    check("vendor_eta" not in change_cols and "vendor_etd" not in change_cols,
          "a vendor date cannot be stored on a line")
    check(not [c for c in change_cols if c.startswith("proposed_") and c.endswith("date")],
          "and there is no proposed_*_date column at all",
          str(sorted(c for c in change_cols if "date" in c)))

    # No PARTIAL_LINE / OVER_SHIPMENT: that gating design was cancelled, and
    # over-shipment remains a plain PENDING_REVIEW.
    for absent in ("PARTIAL_LINE", "OVER_SHIPMENT"):
        check(absent not in {s for s, _t, _d in sc.CHANGE_STATES},
              f"{absent} is not a state -- the cancelled gate stays cancelled")


def test_foreign_keys_are_enforced() -> None:
    section("foreign keys are actually enforced on whichever engine this is")
    engine = fresh_db()

    # HOW they are enforced is dialect-specific; THAT they are enforced is not,
    # and it is the only part worth asserting the same way on both. SQLite has
    # them off by default and needs a per-connection PRAGMA (schema.py registers
    # one); SQL Server has no such switch and no such pragma -- issuing it there
    # gets "Could not find stored procedure 'PRAGMA'".
    if dt_target.is_sqlite():
        with engine.begin() as conn:
            enabled = conn.execute(text("PRAGMA foreign_keys")).scalar()
        check(enabled == 1, "PRAGMA foreign_keys is ON for this connection", str(enabled))
    else:
        with engine.begin() as conn:
            n = conn.execute(text(
                "SELECT COUNT(*) FROM sys.foreign_keys WHERE is_disabled = 0")).scalar()
        check(n == 19, "all 19 foreign keys exist and none is disabled", str(n))
    with engine.begin() as conn:
        expect_integrity(
            lambda: conn.execute(shipment_pos.insert(), {
                "id": sc.new_id(), "shipment_id": "does-not-exist",
                "po_number_printed": "PO#1", "po_number_key": "1",
                "resolution_status": "UNRESOLVED"}),
            "so an orphan row is refused rather than silently accepted",
        )



def test_partial_indexes_refuse_unsupported_dialects() -> None:
    section("a filtered index must never quietly become a FULL unique index")
    from sqlalchemy.dialects import mssql, mysql, oracle, postgresql, sqlite
    from sqlalchemy.schema import CreateIndex

    indexes = {i.name: i for t in sc.metadata.tables.values() for i in t.indexes}
    partial = {n: i for n, i in indexes.items()
               if (i.info or {}).get("partial_predicate")}
    plain = {n: i for n, i in indexes.items()
             if not (i.info or {}).get("partial_predicate")}

    check(len(partial) == 4, "four filtered indexes carry a predicate",
          str(sorted(partial)))
    check(len(plain) >= 1, "and the plain indexes are marked differently",
          str(len(plain)))

    # The two supported dialects must still emit the WHERE. Without this half the
    # guard could "pass" by refusing everything everywhere.
    for label, dialect in (("sqlite", sqlite.dialect()), ("mssql", mssql.dialect())):
        emitted = []
        for name, index in partial.items():
            sql = str(CreateIndex(index).compile(dialect=dialect))
            if "WHERE" in sql.upper():
                emitted.append(name)
        check(len(emitted) == len(partial),
              f"{label}: every filtered index still compiles WITH its WHERE",
              f"{len(emitted)}/{len(partial)}")

    # And the unsupported ones must RAISE rather than silently widen. postgresql
    # is the one that matters -- it is the reflexive default, and it is exactly
    # where the predicate used to vanish.
    for label, dialect in (("postgresql", postgresql.dialect()),
                           ("mysql", mysql.dialect()),
                           ("oracle", oracle.dialect())):
        refused = []
        widened = []
        for name, index in partial.items():
            try:
                sql = str(CreateIndex(index).compile(dialect=dialect))
            except sc.PartialIndexUnsupported:
                refused.append(name)
            else:
                if "WHERE" not in sql.upper():
                    widened.append(name)
        check(not widened,
              f"{label}: NO filtered index compiles to a full unique index",
              str(widened or "none"))
        check(len(refused) == len(partial),
              f"{label}: all {len(partial)} raise PartialIndexUnsupported instead",
              f"{len(refused)}/{len(partial)}")

    # The error has to be actionable: which index, which dialect, and what was
    # about to be lost. A bare "unsupported" would send someone hunting.
    try:
        CreateIndex(partial["ux_change_candidates_one_selected"]).compile(
            dialect=postgresql.dialect())
    except sc.PartialIndexUnsupported as exc:
        text_ = str(exc)
        check("ux_change_candidates_one_selected" in text_,
              "the error names the index", text_[:80])
        check("postgresql" in text_, "and the dialect that could not spell it")
        check("selected = 1" in text_,
              "and quotes the predicate that would have been dropped")
        check("PARTIAL_INDEX_DIALECTS" in text_,
              "and says what to change to support the dialect properly")
    else:
        check(False, "compiling that index on postgresql raised at all")

    # The guard is registered for EVERY dialect but must only inspect filtered
    # indexes -- an ordinary index has to keep compiling anywhere.
    unaffected = []
    for name, index in plain.items():
        try:
            str(CreateIndex(index).compile(dialect=postgresql.dialect()))
            unaffected.append(name)
        except Exception:  # noqa: BLE001 -- any failure here is the finding
            pass
    check(len(unaffected) == len(plain),
          "ordinary indexes are untouched by the guard on an unsupported dialect",
          f"{len(unaffected)}/{len(plain)}")

    # WHY THIS TEST EXISTS, stated where it will be read: SQLAlchemy accepts
    # dialect-prefixed kwargs it does not recognise and ignores the ones that do
    # not apply. `sqlite_where` and `mssql_where` are therefore invisible to
    # postgresql -- it emitted a FULL unique index, with no error and nothing
    # failing. `ux_change_candidates_one_selected` without its predicate is
    # UNIQUE(change_id), which permits ONE candidate row per change, when the
    # entire purpose of change_candidates is to hold several for a human to
    # choose between.
    check(True, "(the silent-widening failure is now unrepresentable)")


def test_migrations_touching_a_viewed_table_do_the_view_dance() -> None:
    """
    A migration that alters a table any view depends on must drop those views
    first and recreate them after.

    ## Why this is a TEST and not a helper

    The obvious fix is a shared `view_dance()` helper. It is the wrong one here,
    and for a reason this repo already enforces:
    `test_migrations_import_no_application_code` forbids a migration importing
    project code, because a migration's behaviour must be fixed at its revision
    rather than following whatever the imported module holds when it runs. A
    shared helper is precisely that -- every migration's behaviour would depend
    on code that keeps changing. And a helper you forget to call is exactly as
    broken as the DDL you forget to write; it lowers the cost of remembering
    without removing the need to.

    So the structural fix is a check that fails when the dance is missing. This is
    the THIRD time the omission has bitten (0002 established the pattern, and it
    was left out of 0010 by an author who knew about it and had written the note).
    **When documentation has failed repeatedly at the same point, the answer is
    not a louder note.**

    Static, not runtime: the round-trip test already catches this by blowing up
    with "error in view v_review_lines: no such table", but only after the
    migration exists and only for the paths a run happens to take. This reads the
    source, so it catches the omission in every migration including downgrade-only
    paths.
    """
    section("a migration altering a viewed table must drop and recreate the views")
    import ast
    import pathlib as _pl
    import re as _re

    here = _pl.Path(__file__).resolve().parent

    # Which tables the views depend on -- read from the view SQL, not hardcoded,
    # so a view gaining a JOIN extends this automatically.
    viewed = set()
    for _name, ddl in sc.VIEWS:
        for m in _re.finditer(r"\b(?:FROM|JOIN)\s+(\w+)", ddl, _re.I):
            viewed.add(m.group(1).lower())
    check(viewed >= {"proposed_changes", "shipment_pos", "shipments"},
          "the tables the views depend on, derived from the view SQL",
          str(sorted(viewed)))

    offenders = []
    checked = 0
    for f in sorted((here / "migrations" / "versions").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src)

        # Does it alter a viewed table? Either via batch_alter_table(...) or a
        # raw ALTER TABLE naming one.
        touched = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                nm = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if nm == "batch_alter_table" and node.args:
                    a = node.args[0]
                    if isinstance(a, ast.Constant) and str(a.value).lower() in viewed:
                        touched.add(str(a.value).lower())
        for m in _re.finditer(r"ALTER\s+TABLE\s+\[?(\w+)\]?", src, _re.I):
            if m.group(1).lower() in viewed:
                touched.add(m.group(1).lower())
        if not touched:
            continue

        checked += 1
        drops = bool(_re.search(r"DROP\s+VIEW", src, _re.I))
        creates = bool(_re.search(r"CREATE\s+VIEW", src, _re.I))
        if not (drops and creates):
            offenders.append(
                f"{f.name}: alters {sorted(touched)} but "
                f"{'never DROPs a view' if not drops else 'never re-CREATEs one'}")

    check(checked >= 5,
          "several migrations do alter a viewed table -- so this can actually fail",
          f"{checked} migration(s) alter one")
    check(not offenders,
          "every one of them drops the views and puts them back",
          str(offenders) if offenders else "all clean")

    # The check must be able to fail, or it is decoration. Run the same logic over
    # a synthetic migration that alters a viewed table and forgets the dance.
    bad = ("def upgrade():\n"
           "    with op.batch_alter_table('proposed_changes') as b:\n"
           "        b.alter_column('key_size')\n")
    bad_tree = ast.parse(bad)
    bad_touched = {
        str(n.args[0].value).lower()
        for n in ast.walk(bad_tree)
        if isinstance(n, ast.Call)
        and (n.func.attr if isinstance(n.func, ast.Attribute) else "") == "batch_alter_table"
        and n.args and isinstance(n.args[0], ast.Constant)
        and str(n.args[0].value).lower() in viewed
    }
    would_flag = bool(bad_touched) and not _re.search(r"DROP\s+VIEW", bad, _re.I)
    check(would_flag,
          "and the check REJECTS a migration that omits it -- verified on a "
          "synthetic one, not assumed")

def main() -> int:
    print("=" * 78)
    print("SCHEMA TESTS -- migration 0001")
    print("=" * 78)
    print()
    print("Pins the discovered constraints from the rationale doc. A column rename")
    print("should not break these; losing a dedup axis or a state guarantee should.")

    REGISTERED = (
        test_migration_round_trip,
        test_message_id_dedup,
        test_content_hash_dedup,
        test_subset_reforward,
        test_unresolved_multi_candidate,
        test_partial_write_failure_is_per_line,
        test_quantity_without_date,
        test_canonical_key_identity,
        test_five_review_figures,
        test_calibration_pairing,
        test_colour_provenance_columns,
        test_two_workflows_are_distinguishable,
        test_state_machine_is_data,
        test_migration_seed_matches_schema,
        test_migrations_import_no_application_code,
        test_scope_boundaries_in_the_schema,
        test_foreign_keys_are_enforced,
        test_partial_indexes_refuse_unsupported_dialects,
        test_migrations_touching_a_viewed_table_do_the_view_dance,
    )

    # A test registered twice runs twice and its checks are counted twice. That
    # is how this suite reported 115 for 105 distinct checks until a pytest run,
    # which collects each function once, disagreed with the script (RUNBOOK
    # section 8 lessons 18 and 19). Cheap to assert, so the class cannot recur.
    dupes = sorted({f.__name__ for f in REGISTERED if REGISTERED.count(f) > 1})
    check(not dupes, "no test is registered more than once", str(dupes or "none"))

    for fn in REGISTERED:
        try:
            fn()
        except Exception:  # noqa: BLE001
            print()
            traceback.print_exc()
            _results.append((False, f"{fn.__name__} crashed", ""))

    passed = sum(1 for ok, _n, _d in _results if ok)
    total = len(_results)
    print()
    print("=" * 78)
    print(f"{passed}/{total} checks passed")
    print("=" * 78)
    for ok, name, _detail in _results:
        if not ok:
            print(f"  FAILED: {name}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
