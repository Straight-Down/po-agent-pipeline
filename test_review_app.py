"""
Review app (Phase 3 v1) tests. Offline, no NetSuite, no network.

Rows are made by the real `ingest_shipment` with the parse stubbed -- the same
way `test_ingest` does it -- so the app is tested against the shapes the
pipeline actually writes, not hand-built ones.

    python -m pytest test_review_app.py -q
"""

from __future__ import annotations

import ast
import re
import datetime as dt
import json
import subprocess
import sys
import tempfile
import traceback
from types import SimpleNamespace
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

import dialect_target as dt_target
import schema as sc
from schema import (audit_log, message_attachments, proposed_changes, shipment_pos,
                    write_attempts)

HERE = Path(__file__).resolve().parent
#: UTC, stored naive -- the database's convention (`ingest._utcnow`).
NOW = dt.datetime(2026, 9, 30, 12, 0, 0, tzinfo=dt.timezone.utc).replace(tzinfo=None)
REVIEWER = "Paula Test"
_results: list[tuple[bool, str, str]] = []

#: Approve now writes to NetSuite, through a client the app cannot pass in. So the
#: write path's default client is one it refuses (no config), here as well as in
#: conftest -- `python test_review_app.py` loads no conftest. A test that wants a
#: write installs a fake with `World.netsuite`; `_World` puts this back after.
NO_LIVE_NETSUITE = SimpleNamespace(config=None)
import write_path as _write_path  # noqa: E402

_write_path._live = NO_LIVE_NETSUITE


def check(ok: bool, name: str, detail: str = "") -> bool:
    _results.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  --  {detail}" if detail else ""))
    return bool(ok)


def section(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 70 - len(title)))


# ---------------------------------------------------------------------------
# Fixture: a real ingest into a file database
# ---------------------------------------------------------------------------


def _build_db(path: Path):
    """A seeded schema in a FILE: the app answers requests on worker threads, and an
    in-memory SQLite database is private to the connection that made it.

    Through `dialect_target`, so `--mssql` moves this file onto SQL Server (where
    `path` is unused: a server database is shared between threads already). It
    built its own SQLite engine until 2026-10-12, and so never left SQLite."""
    return dt_target.fresh_engine(sqlite_file=path)


class World:
    """One ingested shipment covering five POs, each shaped for one behaviour."""

    def __init__(self, tmp: Path):
        import ingest as ing
        from extraction_schema import ParseResult
        from netsuite_client import NetSuiteClient
        from test_ingest import (
            FakeClassification,
            StubExtractor,
            install_stub_parse,
            line,
            msg,
            ns_line,
            restore,
        )

        self.tmp = tmp
        self.engine = _build_db(tmp / "review-test.db")
        self.doc = tmp / "Invoice_Packing.xlsx"
        self.doc.write_bytes(b"the vendor's packing list bytes")
        parsed = ParseResult(lines=[
            # PO 1662: a clean change, a flagged (medium) change, an unchanged line.
            line(po="1662", size="S", qty=9),
            line(po="1662", size="M", qty=50, conf="medium",
                 note="PO carried forward from the row above"),
            line(po="1662", size="L", qty=30),
            # PO 1700: two OPEN lines share the key -> NEEDS_RESOLUTION -> refused.
            line(po="1700", size="S", qty=5),
            # PO 1800: the only matching line is closed -> no action possible.
            line(po="1800", size="S", qty=7),
            # PO 1900: flagged (medium), but its figure ALREADY matches NetSuite.
            line(po="1900", size="S", qty=5, conf="medium",
                 note="style inherited from the row above"),
            # PO 2000: the slip ships less than is ordered -> flagged, nothing proposed
            # (never below ordered, 2026-11-09; the PO 1657 rehearsal).
            line(po="2000", size="S", qty=37),
        ], parser="claude-assisted", vendor_name="Inprotex",
            ship_info={"etd": "2026/6/27 19:40", "eta": "2026/6/27 16:45"})
        # PO 1662 orders BELOW the slip (8 < 9, 45 < 50), so both changes are
        # proposable. It was 12 and 71, the demo's reductions, until 2026-11-09, when a
        # figure below ordered stopped being proposed (RUNBOOK section 4).
        client = NetSuiteClient(mock_data={
            "1662": [ns_line("18", size="S", qty=8), ns_line("19", size="M", qty=45),
                     ns_line("20", size="L", qty=30)],
            "2000": [ns_line("14", size="S", qty=216)],
            "1700": [ns_line("1", size="S", qty=5), ns_line("2", size="S", qty=5)],
            "1800": [ns_line("1", size="S", qty=5, is_open=False)],
            "1900": [ns_line("1", size="S", qty=5)],
        })
        classification = FakeClassification(
            selected=[FakeClassification.Item(self.doc, "packing_list")])
        monkey: dict = {}
        install_stub_parse(monkey, parsed, classification)
        try:
            ing.ingest_shipment(self.engine, [self.doc], message=msg(), client=client,
                                extractor=StubExtractor(), now=NOW)
        finally:
            restore(monkey)

    def po_id(self, key: str) -> str:
        with self.engine.connect() as conn:
            return conn.execute(select(shipment_pos.c.id)
                                .where(shipment_pos.c.po_number_key == key)).scalar_one()

    def lines(self, key: str) -> dict:
        with self.engine.connect() as conn:
            rows = conn.execute(select(proposed_changes)
                                .where(proposed_changes.c.shipment_po_id == self.po_id(key))
                                ).mappings().all()
        return {r["src_size_text"]: dict(r) for r in rows}

    def client(self, unrecorded_writes=None) -> TestClient:
        from review_app.app import create_app
        from review_app.settings import Settings

        settings = Settings(db_url="unused", blob_root=self.tmp, reviewer=REVIEWER,
                            unrecorded_writes=unrecorded_writes)
        self.app = create_app(settings, engine=self.engine, now=lambda: NOW)
        return TestClient(self.app)

    def netsuite(self, key: str, internal_id: str = "8489541", line_overrides=None):
        """A fake NetSuite holding this PO's matched lines as the database last saw
        them, installed as the write path's client. Approve writes to it."""
        import write_path
        from test_write_path import FakeNetSuite, client_for, ns_line as fake_line

        with self.engine.begin() as conn:
            conn.execute(shipment_pos.update().where(shipment_pos.c.id == self.po_id(key))
                         .values(ns_internal_id=internal_id))
        # float: NetSuite sends JSON numbers, and the fake JSON-encodes its replies.
        lines = [fake_line(int(r["ns_line_id"]), float(r["current_quantity"]),
                           **{"isOpen": bool(r["ns_line_is_open"]),
                              **(line_overrides or {}).get(int(r["ns_line_id"]), {})})
                 for r in self.lines(key).values() if r["ns_line_id"]]
        self.fake = FakeNetSuite(lines)
        write_path._live = client_for(self.fake)
        return self.fake

    def form(self, client: TestClient, key: str) -> dict:
        """The hidden fields a real page load would give the browser."""
        import re

        html = client.get(f"/po/{self.po_id(key)}").text
        return {"csrf_token": self.app.state.csrf,
                "fingerprint": re.search(r'name="fingerprint" value="([0-9a-f]+)"', html).group(1)}


class _World:
    """`with _World() as w:` -- the engine is disposed before the directory is removed,
    because Windows will not delete a SQLite file that is still open."""

    def __enter__(self) -> World:
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.w = World(Path(self.td.name))
        return self.w

    def __exit__(self, *exc) -> None:
        import write_path

        write_path._live = NO_LIVE_NETSUITE  # whatever a test installed, never live
        self.w.engine.dispose()
        self.td.cleanup()


# ---------------------------------------------------------------------------


def test_refuses_to_start_without_a_reviewer() -> None:
    section("no PO_AGENT_REVIEWER, no app")
    import os

    from review_app.__main__ import main

    saved = os.environ.pop("PO_AGENT_REVIEWER", None)
    try:
        code = main(["--db", "sqlite://"])
    finally:
        if saved is not None:
            os.environ["PO_AGENT_REVIEWER"] = saved
    check(code == 2, "refuses to start: a verdict must name a real decision-maker, and "
          "there is no login to supply one", f"exit {code}")


def test_queue_and_the_no_action_view() -> None:
    section("queue: a PO closed when processed is out of Paula's view entirely")
    with _World() as w:
        c = w.client()
        main = c.get("/").text
        parked = c.get("/no-action").text
        check("1662" in main and "1700" in main and "1800" not in main,
              "the PO with no open matched line is NOT in the queue")
        pages = [main, c.get(f"/po/{w.po_id('1662')}").text, c.get(f"/po/{w.po_id('1700')}").text]
        check(not any("/no-action" in page for page in pages),
              "and nothing in the interface links to where it went")
        check("1800" in parked and "1662" not in parked,
              "the diagnostic page still lists it, reachable by URL only")
        check(f"Reviewing as {REVIEWER}" in main and f"Reviewing as {REVIEWER}" in parked,
              "every page says whose decision it records")
        from review_app import settings as rs

        check(not hasattr(rs, "SEPARATE_NO_ACTION_ENV")
              and "separate_no_action" not in rs.Settings.__dataclass_fields__,
              "there is no setting that puts closed POs back in the queue")


def test_the_po_page() -> None:
    section("the per-PO page: flagged first, the five figures, reference dates, no pre-fill")
    with _World() as w:
        html = w.client().get(f"/po/{w.po_id('1662')}").text
        check(html.index("1 line to check before approving") < html.index("Other lines"),
              "NEEDS_ATTENTION lines are grouped above the clean ones")
        check("Approve: 2 lines change in NetSuite, 1 stays as it is (1 flagged)" in html,
              "the button counts changes and acceptances separately",
              re.search(r'<button type="submit"[^>]*>([^<]+)<', html).group(1))
        check("The tool had to work out part of this line itself. Check it against the "
              "packing slip before approving." in html
              and "Extractor's note: PO carried forward from the row above" in html,
              "a flagged line says what to do; the extractor's own note is under Details (§5)")
        check("That's for your reference only." in html and "arrives 2026/6/27 16:45" in html,
              "vendor dates shown as reference (§9)")
        check("receipt_date" not in html and 'type="date"' not in html
              and "receipt date" not in html.lower() and 'value="2026' not in html,
              "and there is no receipt date to enter at all (2026-11-02)")
        # PO 1662: S 9 vs 8, M 50 vs 45, L 30 vs 30 -- over on both that differ.
        check("This packing slip ships 89 units against 83 ordered on these lines: more than "
              "ordered on every line that differs." in html
              and "2 over, 0 under, 1 equal" in html,
              "the ordered-versus-shipped summary in words, the counts under Details (§4)")


def test_approve_records_a_verdict_on_every_line() -> None:
    section("approve: APPROVED and a verdict on EVERY covered line, nothing else")
    with _World() as w:
        c = w.client()
        r = c.post(f"/po/{w.po_id('1662')}/approve", data=w.form(c, "1662"),
                   follow_redirects=False)
        check(r.status_code == 303, "approve redirects back to the PO", str(r.status_code))
        lines = w.lines("1662")
        check(all(ln["human_verdict"] == "ACCEPTED" and ln["verdict_by"] == REVIEWER
                  and ln["verdict_at"] is not None for ln in lines.values()),
              "all three lines carry ACCEPTED, the reviewer and a time -- including the "
              "unchanged one (§1)")
        check(lines["S"]["state"] == "APPROVED" and lines["M"]["state"] == "APPROVED",
              "the changed lines, flagged one included, are APPROVED")
        check(float(lines["S"]["approved_quantity"]) == 9.0
              and lines["S"]["quantity_approved_by"] == REVIEWER
              and lines["S"]["quantity_write_status"] == "APPROVED",
              "with the approved quantity and who approved it")
        check(lines["L"]["state"] == "NO_CHANGE" and lines["L"]["quantity_write_status"] == "NONE",
              "the unchanged line keeps NO_CHANGE: accepted, nothing to write")
        with w.engine.connect() as conn:
            events = conn.execute(select(audit_log).where(audit_log.c.event == "REVIEW_APPROVED")
                                  ).mappings().all()
        check(len(events) == 3 and all(e["actor"] == REVIEWER and e["actor_kind"] == "HUMAN"
                                       for e in events), "one HUMAN audit row per line")


def test_the_approve_label_counts_changes_and_acceptances_apart() -> None:
    section("Approve's label: lines that change in NetSuite, counted apart from the rest")
    from review_app import data as rdata

    def line(disposition, proposed=None, current=None, state="PENDING_REVIEW"):
        return rdata.Line({"state": state, "human_verdict": None, "accumulation_basis": None,
                           "proposed_quantity": proposed, "current_quantity": current},
                          disposition)

    def label(*lines):
        return rdata.PoView({"id": "x"}, {}, None, list(lines)).approve_label

    change = line("approve", "9", "12")
    same = line("approve", "5", "5.000")  # approved at what NetSuite holds
    no_change = line("accept_no_change", "30", "30", state="NO_CHANGE")
    flagged = line("approve", "50", "71", state="NEEDS_ATTENTION")
    blocked = line("block")
    cases = [
        ((change,), "Approve: 1 line changes in NetSuite"),
        ((change, flagged), "Approve: 2 lines change in NetSuite (1 flagged)"),
        ((same,), "Approve: 1 line stays as it is"),
        ((same, no_change), "Approve: 2 lines stay as they are"),
        ((change, same), "Approve: 1 line changes in NetSuite, 1 stays as it is"),
        ((change, flagged, same, no_change, blocked),
         "Approve: 2 lines change in NetSuite, 2 stay as they are (1 flagged) — 1 can't be changed"),
        ((change, line("approve", "7", None)), "Approve: 2 lines change in NetSuite"),
    ]
    for lines, expected in cases:
        got = label(*lines)
        check(got == expected, f"label: {expected}", got)


def test_a_flagged_line_that_already_matches_is_still_approved() -> None:
    section("a flagged line whose figure already matches NetSuite is APPROVED, not stranded")
    with _World() as w:
        before = w.lines("1900")["S"]
        check(before["state"] == "NEEDS_ATTENTION"
              and float(before["proposed_quantity"]) == float(before["current_quantity"]) == 5.0,
              "premise: NEEDS_ATTENTION with proposed == current (5 = 5)")
        c = w.client()
        html = c.get(f"/po/{w.po_id('1900')}").text
        check("Approve: 1 line stays as it is (1 flagged)" in html
              and "change in NetSuite" not in html and "changes in NetSuite" not in html,
              "its button does not count it as a change: NetSuite already holds 5",
              re.search(r'<button type="submit"[^>]*>([^<]+)<', html).group(1))
        check("Approving updates NetSuite straight away" not in html
              and "nothing changes and nothing is sent" in " ".join(main_text(html).split()),
              "and the copy says nothing changes and nothing is sent")
        c.post(f"/po/{w.po_id('1900')}/approve", data=w.form(c, "1900"))
        after = w.lines("1900")["S"]
        check(after["state"] == "APPROVED" and after["human_verdict"] == "ACCEPTED"
              and after["quantity_write_status"] == "APPROVED"
              and float(after["approved_quantity"]) == 5.0,
              "APPROVED with its quantity scope -- it used to keep NEEDS_ATTENTION with a "
              "verdict, drop out of the queue, and never be approved",
              f"{after['state']} / {after['quantity_write_status']}")


def test_a_line_already_at_its_figure_is_confirmed_not_written() -> None:
    section("approved figure == NetSuite's: confirmed, nothing sent, never shown as a write")
    import ingest as ing

    with _World() as w:
        ns = w.netsuite("1900")  # NetSuite holds 5.0; the approved figure is 5.000
        c = w.client()
        c.post(f"/po/{w.po_id('1900')}/approve", data=w.form(c, "1900"))
        check("PATCH" not in [m for m, _u, _k in ns.requests],
              "nothing is sent to NetSuite", str([m for m, _u, _k in ns.requests]))
        line = w.lines("1900")["S"]
        with w.engine.connect() as conn:
            outcomes = [r.outcome for r in conn.execute(
                select(write_attempts.c.outcome).where(write_attempts.c.change_id == line["id"]))]
        check(outcomes == ["CONFIRMED"] and line["state"] == "WRITTEN",
              "recorded CONFIRMED, and the line is settled", f"{outcomes} / {line['state']}")
        history = ing._line_history(w.engine, ["1900"])
        check(history[("1900", str(line["ns_line_id"]))].written_quantity == 5.0,
              "the accumulation base counts it, as it does ALREADY_PRESENT")
        groups = sections(c.get(f"/po/{w.po_id('1900')}/result").text)
        confirmed = groups.get("Confirmed: NetSuite already had the approved figure", "")
        check("Written to NetSuite" not in groups and "→" not in confirmed
              and "nothing was sent" in confirmed,
              "never under Written, never '5 → 5': its own group, saying nothing was sent",
              str(list(groups)))


def test_a_reason_not_sent_survives_a_restart() -> None:
    section("refused before NetSuite was contacted: the reason is in the audit, not memory")
    from review_app.app import create_app
    from review_app.settings import Settings

    with _World() as w:
        c = w.client()
        # No `w.netsuite(...)`: the stand-in has no config -> refused before contact.
        c.post(f"/po/{w.po_id('1662')}/approve", data=w.form(c, "1662"))
        with w.engine.connect() as conn:
            events = conn.execute(select(audit_log.c.event)
                                  .where(audit_log.c.change_id == w.lines("1662")["S"]["id"])
                                  .where(audit_log.c.event == "QUANTITY_WRITE_NOT_SENT")).all()
        check(len(events) == 1, "the refusal is recorded as an audit event", str(len(events)))
        # A restart: a new app, a new process's memory, the same database.
        fresh = TestClient(create_app(Settings(db_url="unused", blob_root=Path("."),
                                               reviewer=REVIEWER), engine=w.engine))
        groups = sections(fresh.get(f"/po/{w.po_id('1662')}/result").text)
        waiting = groups.get("Approved, not sent to NetSuite yet", "")
        # This world's PO has no NetSuite record id, so the recorded reason is that
        # database precondition; a missing connection is recorded the same way.
        check("has no NetSuite record id on file, so nothing was sent" in waiting
              and "Approved, but not sent to NetSuite yet." not in waiting,
              "after a restart the page still says WHY it was not sent", waiting[:200])


def test_approve_records_no_receipt_date() -> None:
    section("approve is quantity only: no date recorded, nothing APPROVED without a quantity")
    with _World() as w:
        with w.engine.begin() as conn:  # a line a person already dated in NetSuite
            conn.execute(proposed_changes.update()
                         .where(proposed_changes.c.id == w.lines("1662")["M"]["id"])
                         .values(current_override_flag=True))
        c = w.client()
        html = c.get(f"/po/{w.po_id('1662')}").text
        check("receipt date" not in html.lower() and "won't be applied" not in html,
              "no date field and no override warning, even on a line a person dated")
        # A stale form, or a hand-made request, still posting a date is ignored.
        c.post(f"/po/{w.po_id('1662')}/approve",
               data={**w.form(c, "1662"), "receipt_date": "2026-10-15"})
        lines = w.lines("1662")
        check(all(ln["confirmed_receipt_date"] is None and ln["date_write_status"] == "NONE"
                  and ln["date_approved_by"] is None for ln in lines.values()),
              "no line gets a receipt date, whatever the form posts")
        check(lines["L"]["state"] == "NO_CHANGE" and lines["L"]["human_verdict"] == "ACCEPTED",
              "an unchanged line is accepted and keeps NO_CHANGE, as before the date field")
        with w.engine.connect() as conn:
            stranded = conn.execute(
                select(proposed_changes.c.id).where(proposed_changes.c.state == "APPROVED")
                .where((proposed_changes.c.quantity_write_status != "APPROVED")
                       | proposed_changes.c.approved_quantity.is_(None))).all()
            approved = conn.execute(select(proposed_changes.c.id).where(
                proposed_changes.c.state == "APPROVED")).all()
        check(approved and not stranded,
              "every APPROVED line has a quantity approved for writing: none is approved with "
              "nothing writable", f"{len(approved)} approved, {len(stranded)} stranded")


def test_a_write_with_no_record_is_shown_on_the_queue() -> None:
    section("a write that reached NetSuite with no record: a banner on the landing page")
    with _World() as w:
        check("couldn't record" not in w.client().get("/").text,
              "no file configured: no banner")
        log = w.tmp / "unrecorded-writes.jsonl"
        check("couldn't record" not in w.client(unrecorded_writes=log).get("/").text,
              "a configured file that does not exist yet: no banner")
        log.write_text("\n".join(json.dumps(e) for e in (
            {"at": "2026-11-02T09:00:00", "outcome": "SUCCESS", "reached_netsuite": True,
             "change_id": "c-1", "po": "PO 1662", "line": "18", "new_quantity": 50,
             "sent": {"quantity": 50}, "error": "OperationalError: link failure"},
            # Did not reach NetSuite: logged and filed, but not the alarm.
            {"at": "2026-11-02T09:01:00", "outcome": "REFUSED", "reached_netsuite": False,
             "change_id": "c-2", "po": "PO 1662", "line": "19", "new_quantity": 9,
             "sent": {}, "error": "OperationalError: link failure"},
        )) + "\n", encoding="utf-8")
        html = w.client(unrecorded_writes=log).get("/").text
        main = main_text(html)
        check("1 change reached NetSuite, but the tool couldn't record it:" in main
              and "PO 1662 line 18 (now 50)." in main and "line 19" not in main,
              "the banner names the write that landed, and only that one", main[:200])
        check("Don't approve that PO again until Kiko has checked it in NetSuite." in main,
              "and says what to do")
        check("OperationalError" not in main and "OperationalError" in html,
              "the technical detail sits behind the Details toggle")
        check(not INTERNAL.findall(main), "nothing internal leaks into the banner",
              str(INTERNAL.findall(main)))
        po_page = main_text(w.client(unrecorded_writes=log).get(f"/po/{w.po_id('1662')}").text)
        check("PO 1662 line 18 (now 50)." in po_page,
              "the banner is on every page, a PO page included -- not only the queue")

        # A timed-out write whose FAILED row also failed to record MAY have landed.
        unsure = {"at": "2026-11-02T09:02:00", "outcome": "FAILED",
                  "reached_netsuite": "unknown", "change_id": "c-3", "po": "PO 1700",
                  "line": "4", "new_quantity": 12, "sent": {"quantity": 12},
                  "error": "OperationalError: link failure"}
        log.write_text(log.read_text(encoding="utf-8") + json.dumps(unsure) + "\n",
                       encoding="utf-8")
        main = main_text(w.client(unrecorded_writes=log).get("/").text)
        check("2 changes reached, or may have reached, NetSuite" in main
              and "PO 1700 line 4 (may now be 12)." in main,
              "a write that MAY have landed is alarmed too, worded as such", main[:220])

        # Damaged lines: not JSON, and JSON that is not an object. Neither may take
        # down the page that warns about them.
        log.write_text(log.read_text(encoding="utf-8") + "{not json\n5\n", encoding="utf-8")
        response = w.client(unrecorded_writes=log).get("/")
        check(response.status_code == 200
              and "4 changes reached, or may have reached, NetSuite" in main_text(response.text),
              "an entry that will not parse counts too, and the page still renders",
              str(response.status_code))


def sections(html: str) -> dict:
    """The result page's groups: heading text -> that group's visible text."""
    import html as htmllib

    out = {}
    for chunk in html.split("<h2>")[1:]:
        heading, _, rest = chunk.partition("</h2>")
        out[htmllib.unescape(re.sub(r"\s*\(\d+\)$", "", heading.strip()))] = \
            htmllib.unescape(main_text(rest.split("<h2>")[0]))
    return out


def test_approve_writes_to_netsuite_and_shows_what_changed() -> None:
    section("Approve writes to NetSuite, then shows every line's before and after")
    with _World() as w:
        ns = w.netsuite("1662")
        c = w.client()
        r = c.post(f"/po/{w.po_id('1662')}/approve", data=w.form(c, "1662"),
                   follow_redirects=False)
        check(r.status_code == 303
              and r.headers["location"] == f"/po/{w.po_id('1662')}/result",
              "approving redirects to the PO's result page", r.headers.get("location", ""))
        check([b for b, _k in ns.patches()]
              == [{"item": {"items": [{"line": 18, "quantity": 9}, {"line": 19, "quantity": 50}]}}],
              "one PATCH, with the two lines that change; the unchanged line is not sent",
              json.dumps([b for b, _k in ns.patches()]))
        lines = w.lines("1662")
        check(lines["S"]["state"] == lines["M"]["state"] == "WRITTEN"
              and lines["L"]["state"] == "NO_CHANGE" and lines["L"]["human_verdict"] == "ACCEPTED",
              "the two are WRITTEN, the unchanged one accepted as it was")

        page_one = c.get(f"/po/{w.po_id('1662')}/result").text
        groups = sections(page_one)
        check("line 18: 8 → 9" in groups.get("Written to NetSuite", "")
              and "line 19: 45 → 50" in groups.get("Written to NetSuite", ""),
              "the result lists each written line as previous -> new", str(list(groups)))
        written = groups.get("Written to NetSuite", "")
        check(written.index("line 18") < written.index("line 19"),
              "in NetSuite line order, not the slip's style/colour/size order", written[:120])
        check("line 20" in groups.get("No change needed", ""),
              "and the line that needed nothing, under its own heading")
        main = " ".join(main_text(page_one).split())
        check("edit that line's quantity in NetSuite back to the number before the arrow" in main
              and "no undo button" in main,
              "with the sentence on how to reverse a change")
        check(" ".join(main_text(c.get(f"/po/{w.po_id('1662')}/result").text).split()) == main,
              "a refresh shows the same result: it is read from the database, not the POST")
        check("See what changed, line by line" in c.get(f"/po/{w.po_id('1662')}").text,
              "and the PO page links to it afterwards")
        check(not INTERNAL.findall(main), "nothing internal on the result page",
              str(INTERNAL.findall(main)))


def test_lines_that_cant_be_changed_are_left_out_of_the_write() -> None:
    section("a blocked line no longer disables Approve: it is left out and listed")
    with _World() as w:
        with w.engine.begin() as conn:  # line 19 has closed in NetSuite
            conn.execute(proposed_changes.update()
                         .where(proposed_changes.c.id == w.lines("1662")["M"]["id"])
                         .values(ns_line_is_open=False))
        ns = w.netsuite("1662")
        c = w.client()
        import html as htmllib

        html = c.get(f"/po/{w.po_id('1662')}").text
        main = htmllib.unescape(main_text(html))
        check("Approve: 1 line changes in NetSuite, 1 stays as it is — 1 can't be changed" in main
              and '<button type="submit" disabled>' not in html,
              "the button counts what it approves and what it leaves out, and is enabled",
              re.search(r"Approve [^<]*", html).group(0) if re.search(r"Approve [^<]*", html)
              else "")
        check("(line 19)" in main and "left out" in main,
              "the left-out line is listed by the button")
        c.post(f"/po/{w.po_id('1662')}/approve", data=w.form(c, "1662"))
        check([b for b, _k in ns.patches()]
              == [{"item": {"items": [{"line": 18, "quantity": 9}]}}],
              "only the line that can change is sent", json.dumps([b for b, _k in ns.patches()]))
        m = w.lines("1662")["M"]
        check(m["human_verdict"] is None and m["state"] == "NEEDS_ATTENTION",
              "the left-out line gets no verdict and keeps its state")
        groups = sections(c.get(f"/po/{w.po_id('1662')}/result").text)
        check("line 19" in groups.get("Left as they are: can't be changed here", ""),
              "the result page lists it as left as it is", str(list(groups)))
        check("1662" in main_text(c.get("/").text),
              "and the PO stays in the queue for it, to reject or fix in NetSuite")


def test_the_result_page_keeps_each_outcome_apart() -> None:
    section("result page: written, already present, refused and failed are never merged")
    import write_path
    from test_write_path import (Resp, client_for, insert_attempt, line_400,
                                 po_world)
    from review_app.app import create_app
    from review_app.settings import Settings

    td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    engine, po, changes, ns = po_world(
        [(18, 71, 50), (19, 12, 9), (20, 30, 40), (21, 8, 10), (23, 40, 44)],
        ns_overrides={19: {"isClosed": True, "isOpen": False}, 23: {"quantity": 44}},
        pending_line=None, extra_ns=None, sqlite_file=Path(td.name) / "outcomes-test.db")
    with engine.begin() as conn:  # line 23: an earlier attempt sent 44 and never heard back
        conn.execute(proposed_changes.update().where(proposed_changes.c.id == changes[23])
                     .values(state=sc.STATE_WRITE_FAILED, quantity_write_status="FAILED"))
    insert_attempt(engine, changes[23], 1, "FAILED", {"quantity": 44}, "TRANSIENT", line="23")
    # 20 is named in a 400; the resend gets a 204, but only line 18 actually lands.
    ns.patch_failures = [(line_400(20), False), (Resp(204), {18})]
    write_path._live = client_for(ns)
    try:
        app = create_app(Settings(db_url="unused", blob_root=Path(td.name), reviewer=REVIEWER),
                         engine=engine, now=lambda: NOW)
        c = TestClient(app)
        c.post(f"/po/{po}/write", data={"csrf_token": app.state.csrf})
        groups = sections(c.get(f"/po/{po}/result").text)
    finally:
        write_path._live = NO_LIVE_NETSUITE
        engine.dispose()
        td.cleanup()
    written = groups.get("Written to NetSuite", "")
    present = groups.get("Already showing the new quantity", "")
    refused = groups.get("Refused, nothing written", "")
    failed = groups.get("Not written: something went wrong", "")
    check("line 18: 71 → 50" in written and "line 23" not in written,
          "written: line 18 -- and the already-present line is NOT among them", written[:120])
    check("line 23: 40 → 44" in present and "can't prove" in present,
          "already present: its own group, before -> after, saying it is unproven", present[:160])
    check("line 19" in refused and "closed in NetSuite" in refused
          and "line 20" in refused and "Please enter a value for Amount" in refused,
          "refused: each with its reason", refused[:200])
    check("line 21" in failed and "accepted the write" in failed,
          "failed: with what went wrong", failed[:160])
    check("Send the unwritten lines again" in groups,
          "and the lines not written can be sent again from here")


def test_the_page_says_it_is_working_while_netsuite_is_updated() -> None:
    section("Approve says what it does now, and the page never looks frozen")
    with _World() as w:
        html = w.client().get(f"/po/{w.po_id('1662')}").text
        main = main_text(html)
        check("Nothing changes in NetSuite yet" not in html
              and "Approving updates NetSuite straight away." in main
              and "the quantity the factory says it shipped" in main,
              "the copy says Approve changes NetSuite, and what the numbers are")
        check("no undo button" in main and "previous quantity" in main,
              "and how a change is reversed")
        check("data-writes" in html and 'class="working"' in html
              and "This usually takes about ten seconds" in html
              and "Still waiting for NetSuite" in html
              and "taking much longer than usual" in html,
              "the form shows progress, then says more when NetSuite runs long")
        check("<noscript>" in html, "and says the same without scripts")


def test_unwritten_lines_can_be_sent_again() -> None:
    section("a write that didn't go through can be sent again from the result page")
    import requests.exceptions as rex

    with _World() as w:
        ns = w.netsuite("1662")
        ns.patch_failures = [(rex.ReadTimeout("timed out"), False)] * 3
        c = w.client()
        c.post(f"/po/{w.po_id('1662')}/approve", data=w.form(c, "1662"))
        groups = sections(c.get(f"/po/{w.po_id('1662')}/result").text)
        check("line 18" in groups.get("Not written: something went wrong", "")
              and "Send the unwritten lines again" in groups,
              "the timed-out lines are shown as not written, with a way to send them again")
        r = c.post(f"/po/{w.po_id('1662')}/write", data={"csrf_token": w.app.state.csrf},
                   follow_redirects=False)
        groups = sections(c.get(f"/po/{w.po_id('1662')}/result").text)
        check(r.status_code == 303 and "line 18: 8 → 9" in groups.get("Written to NetSuite", "")
              and "Send the unwritten lines again" not in groups,
              "sending again writes them, without approving again", str(list(groups)))
        r = c.post(f"/po/{w.po_id('1662')}/write", data={"csrf_token": "forged"})
        check(r.status_code == 403, "and the send-again form refuses a forged token")


def test_a_failed_write_stays_in_the_queue_until_dealt_with() -> None:
    section("a write that didn't go through never drops out of sight; refused lines have a way out")
    with _World() as w:
        # Line 19 closed in NetSuite after the slip was processed: the write refuses it.
        ns = w.netsuite("1662", line_overrides={19: {"isOpen": False, "isClosed": True}})
        c = w.client()
        c.post(f"/po/{w.po_id('1662')}/approve", data=w.form(c, "1662"))
        queue = main_text(c.get("/").text)
        check("1662" in queue and "1 line approved but not written to NetSuite" in queue,
              "the PO stays in the queue, saying a line isn't written", queue[:200])
        import html as htmllib

        check("1 approved line hasn't been written to NetSuite yet" in htmllib.unescape(
                  main_text(c.get(f"/po/{w.po_id('1662')}").text)),
              "and the PO page says so, with a link to what to do")
        groups = sections(c.get(f"/po/{w.po_id('1662')}/result").text)
        check("closed in NetSuite" in groups.get("Refused, nothing written", "")
              and "Send the unwritten lines again" not in groups
              and "Leave the unwritten lines as they are" in groups,
              "a refused line is not offered for sending again -- it is offered a way out",
              str(list(groups)))
        r = c.post(f"/po/{w.po_id('1662')}/give-up", data={"csrf_token": w.app.state.csrf},
                   follow_redirects=False)
        m = w.lines("1662")["M"]
        check(r.status_code == 303 and m["state"] == "DISCARDED"
              and ns.lines[19]["quantity"] == 45,
              "leaving it as it is discards the line and sends nothing", m["state"])
        groups = sections(c.get(f"/po/{w.po_id('1662')}/result").text)
        check("line 19" in groups.get("Left as they are: you stopped trying", "")
              and "1662" not in main_text(c.get("/").text),
              "the result says so, and the PO leaves the queue", str(list(groups)))
        with w.engine.connect() as conn:
            events = conn.execute(select(audit_log.c.event, audit_log.c.actor)
                                  .where(audit_log.c.change_id == m["id"])).all()
        check(("WRITE_GIVEN_UP", REVIEWER) in [tuple(e) for e in events],
              "recorded as the reviewer's decision")
        r = c.post(f"/po/{w.po_id('1662')}/give-up", data={"csrf_token": "forged"})
        check(r.status_code == 403, "and the form refuses a forged token")


def test_a_written_line_is_shown_by_the_attempt_that_wrote_it() -> None:
    section("a later attempt refused as 'already shows' never turns a WRITTEN line into a refusal")
    # A second tab, or a double submit with no script, can race the first write:
    # the later call re-reads, finds the new value, and records REFUSED. write_path
    # keeps the line WRITTEN (it is in the accumulation base). The result page used
    # to show the NEWEST attempt, so the written line appeared under "Refused".
    from test_write_path import insert_attempt

    with _World() as w:
        w.netsuite("1662")
        c = w.client()
        c.post(f"/po/{w.po_id('1662')}/approve", data=w.form(c, "1662"))
        s = w.lines("1662")["S"]
        check(s["state"] == "WRITTEN", "premise: line 18 was written", s["state"])
        insert_attempt(w.engine, s["id"], 2, "REFUSED", {}, "OTHER", line="18")
        groups = sections(c.get(f"/po/{w.po_id('1662')}/result").text)
        check("line 18: 8 → 9" in groups.get("Written to NetSuite", "")
              and "line 18" not in groups.get("Refused, nothing written", ""),
              "line 18 stays under written, with the value it was written over",
              str(list(groups)))


def test_a_line_given_up_before_it_was_ever_sent_says_so() -> None:
    section("a line refused before NetSuite was contacted, then left as it is, is not 'not sent yet'")
    # No `w.netsuite(...)`: the stand-in has no config, so the write path refuses
    # every line before contacting NetSuite and records NO attempt row -- the same
    # shape as a production account with production writes switched off. The
    # given-up grouping used to be reached only through an attempt row, so such a
    # line read "Approved, but not sent to NetSuite yet" after she had stopped trying.
    with _World() as w:
        c = w.client()
        c.post(f"/po/{w.po_id('1662')}/approve", data=w.form(c, "1662"))
        with w.engine.connect() as conn:
            attempts = conn.execute(select(write_attempts.c.id)).all()
        check(attempts == [], "premise: nothing reached NetSuite, so no attempt row exists",
              str(len(attempts)))
        c.post(f"/po/{w.po_id('1662')}/give-up", data={"csrf_token": w.app.state.csrf})
        check(all(w.lines("1662")[s]["state"] == "DISCARDED" for s in ("S", "M")),
              "premise: both approved lines were left as they are")
        groups = sections(c.get(f"/po/{w.po_id('1662')}/result").text)
        given_up = groups.get("Left as they are: you stopped trying", "")
        check("line 18" in given_up and "line 19" in given_up
              and "Approved, not sent to NetSuite yet" not in groups,
              "both are listed as left as they are, not as still waiting to be sent",
              str(list(groups)))
        check("Nothing was sent to NetSuite" in given_up,
              "and the page says nothing was sent for them", given_up[:200])
        check("1662" not in main_text(c.get("/").text), "and the PO leaves the queue")


def test_approve_copy_never_tells_her_to_reject_the_whole_po_first() -> None:
    section("with a line left out, the copy says to reject it AFTER approving, not before")
    with _World() as w:
        with w.engine.begin() as conn:
            conn.execute(proposed_changes.update()
                         .where(proposed_changes.c.id == w.lines("1662")["M"]["id"])
                         .values(ns_line_is_open=False))
        html = w.client().get(f"/po/{w.po_id('1662')}").text
        card = " ".join(main_text(html.split("<h2>Your decision</h2>")[1].split("</form>")[0])
                        .split())
        check("reject this PO here to clear it" not in card
              and "after you approve. Then reject it here" in card
              and "Don't reject before approving" in card,
              "the left-out line is to be rejected after approving, and the copy warns why",
              card[:300])
        check("(line 19): no open line in NetSuite to update" in card,
              "each left-out line is listed with its reason in a few words")


def test_reject_needs_a_reason() -> None:
    section("reject: a reason, every line discarded, the reason recorded")
    with _World() as w:
        c = w.client()
        form = w.form(c, "1662")
        r = c.post(f"/po/{w.po_id('1662')}/reject", data={**form, "reason": "   "})
        check(r.status_code == 400 and all(ln["human_verdict"] is None
                                           for ln in w.lines("1662").values()),
              "a blank reason is refused and nothing is recorded", str(r.status_code))
        c.post(f"/po/{w.po_id('1662')}/reject",
               data={**form, "reason": "vendor re-sending corrected slip"})
        lines = w.lines("1662")
        check(all(ln["state"] == "DISCARDED" and ln["human_verdict"] == "REJECTED"
                  and ln["human_verdict_note"] == "vendor re-sending corrected slip"
                  for ln in lines.values()), "every line DISCARDED + REJECTED with the reason")


def test_forged_and_stale_forms_are_refused() -> None:
    section("a POST without the page's token, or from a stale page, records nothing")
    with _World() as w:
        c = w.client()
        form = w.form(c, "1662")
        r = c.post(f"/po/{w.po_id('1662')}/approve", data={**form, "csrf_token": "forged"})
        check(r.status_code == 403 and w.lines("1662")["S"]["state"] == "PENDING_REVIEW",
              "a forged token (another site posting to 127.0.0.1) is refused", str(r.status_code))
        r = c.post(f"/po/{w.po_id('1662')}/approve", data={**form, "fingerprint": "0" * 64})
        check(r.status_code == 409 and w.lines("1662")["S"]["state"] == "PENDING_REVIEW",
              "a page older than the data is refused -- reload and decide again")


def test_lines_needing_a_choice_are_left_out_not_refused() -> None:
    section("NEEDS_RESOLUTION / NEEDS_ASSIGNMENT: the page loads, the line is left out")
    # Was (until 2026-11-02): the whole PO page refused to load.
    with _World() as w:
        c = w.client()
        states = {ln["state"] for ln in w.lines("1700").values()}
        check(states == {"NEEDS_RESOLUTION"}, "premise: PO 1700 is NEEDS_RESOLUTION", str(states))
        r = c.get(f"/po/{w.po_id('1700')}")
        main = main_text(r.text)
        check(r.status_code == 200 and "Can't be changed here" in main
              and "could go to more than one line on this PO" in main,
              "the page loads, the line is shown as can't be changed, and says why", main[:160])
        check("disabled" in r.text and "nothing on this PO that can be approved" in main,
              "with nothing else on the PO, Approve has nothing to do")
        form = w.form(c, "1700")
        r = c.post(f"/po/{w.po_id('1700')}/reject", data={**form, "reason": "picked by hand"},
                   follow_redirects=False)
        check(r.status_code == 303 and w.lines("1700")["S"]["human_verdict"] == "REJECTED",
              "and it can be rejected from the page")
        with w.engine.begin() as conn:
            conn.execute(proposed_changes.update()
                         .where(proposed_changes.c.id == w.lines("1700")["S"]["id"])
                         .values(state="NEEDS_ASSIGNMENT", human_verdict=None))
        check(c.get(f"/po/{w.po_id('1700')}").status_code == 200,
              "NEEDS_ASSIGNMENT loads the same way")


def test_a_blocking_line_disables_approve() -> None:
    section("a line with no open target blocks the PO's approval")
    with _World() as w:
        c = w.client()
        html = c.get(f"/po/{w.po_id('1800')}").text
        check("disabled" in html and "match an open line on this PO in NetSuite" in html
              and "be changed here" in html,
              "with every line blocked, Approve is disabled and the page says why, in words")
        r = c.post(f"/po/{w.po_id('1800')}/approve", data=w.form(c, "1800"))
        check(r.status_code == 409 and w.lines("1800")["S"]["human_verdict"] is None,
              "and a POST anyway is refused")


def test_received_and_agreeing_lines_are_kept_from_the_reviewer() -> None:
    section("already received, every figure agreeing: off the review page, on the diagnostic one")
    with _World() as w:
        # PO 1662 line L (30/30) becomes "already received, agrees"; the PO keeps two
        # lines that do need Paula. PO 1900's only line becomes the same, so that PO
        # has nothing left to decide.
        def mark_received_and_agreeing(change_id, qty, keep_flag=False):
            values = dict(state=sc.STATE_NO_CHANGE, accumulation_basis="PRE_EXISTING_RECEIPT",
                          current_quantity_received=qty, proposed_quantity=qty)
            if not keep_flag:
                values["attention_reason"] = None
            with w.engine.begin() as conn:
                conn.execute(proposed_changes.update().where(proposed_changes.c.id == change_id)
                             .values(**values))
        mark_received_and_agreeing(w.lines("1662")["L"]["id"], 30)
        # PO 1900's line is medium confidence: the matcher sets it aside, flag kept.
        check("extraction confidence medium" in (w.lines("1900")["S"]["attention_reason"] or ""),
              "premise: PO 1900's line carries a confidence flag")
        mark_received_and_agreeing(w.lines("1900")["S"]["id"], 5, keep_flag=True)
        c = w.client()

        queue, parked = c.get("/").text, c.get("/no-action").text
        check("1662" in queue and "1900" not in queue,
              "a PO with nothing left to decide leaves the queue; one with other lines stays")
        check("1900" in parked and "Lines already received, with every figure agreeing (2)" in parked
              and "Yes, for its other lines" in parked,
              "the diagnostic page lists both lines, and says which PO is still queued")
        check(parked.count(">Flagged<") == 1 and "work out part of this line" in parked
              and "Tool's reason: extraction confidence medium" in parked,
              "the kept confidence flag is shown there, in words and raw; the unflagged line has none",
              str(parked.count(">Flagged<")))

        html = c.get(f"/po/{w.po_id('1662')}").text
        check(" / L<" not in html and " / S<" in html and " / M<" in html,
              "the agreeing line has no row on the review page; the others do")
        check("Approve: 2 lines change in NetSuite (1 flagged)" in html,
              "and Approve covers only what the reviewer sees",
              re.search(r'<button type="submit"[^>]*>([^<]+)<', html).group(1))
        # Approve writes now, and a PO with an approved line NOT written stays in the
        # queue (2026-11-02). The write has to land for "the rest is decided".
        w.netsuite("1662")
        c.post(f"/po/{w.po_id('1662')}/approve", data=w.form(c, "1662"))
        lines = w.lines("1662")
        check(lines["L"]["human_verdict"] is None and lines["S"]["human_verdict"] == "ACCEPTED",
              "approving records nothing on the hidden line",
              f"L={lines['L']['human_verdict']} S={lines['S']['human_verdict']}")
        check("1662" not in c.get("/").text and "1662" in c.get("/no-action").text,
              "and once the rest is decided, the PO is parked like a closed one")

        empty = c.get(f"/po/{w.po_id('1900')}").text
        check("already received in NetSuite, and the figures agree. Nothing to do." in empty
              and "has been decided" not in empty,
              "a PO with only such lines says so, not that it was decided")


def test_one_shared_block_reason_is_said_once() -> None:
    section("every line blocked for one reason: said once above the table, not per row")
    with _World() as w:
        # A second closed line on PO 1800, blocked exactly like the first.
        s = w.lines("1800")["S"]
        with w.engine.begin() as conn:
            conn.execute(proposed_changes.insert().values(
                {**s, "id": sc.new_id(), "key_size": "m", "src_size_text": "M", "ns_line_id": "2"}))
        c = w.client()
        url = f"/po/{w.po_id('1800')}"
        html = c.get(url).text
        main = main_text(html)
        check(main.count("be changed here") == 1 and "All 2 lines on this PO, each for the same reason" in main,
              "the status is stated once, for the whole PO", str(main.count("be changed here")))
        check(main.count("no longer open") == 1,
              "and the shared reason once, not under each row", str(main.count("no longer open")))
        check(html.count("State: ") == 2 and 'name="fingerprint"' in html and "disabled" in html,
              "each line keeps its Details; the form is untouched")
        check(not INTERNAL.findall(main), "nothing internal leaks into the banner",
              str(INTERNAL.findall(main)))

        # Same reasons, but one line shows a different tag: not the same reason on screen.
        m_id = w.lines("1800")["M"]["id"]
        with w.engine.begin() as conn:
            conn.execute(proposed_changes.update().where(proposed_changes.c.id == m_id)
                         .values(accumulation_basis="DISPUTED"))
        main = main_text(c.get(url).text)
        check(main.count("be changed here") == 2 and "changed in NetSuite since last seen" in main,
              "a tag on one line only keeps it per line, with the tag shown")
        with w.engine.begin() as conn:
            conn.execute(proposed_changes.update().where(proposed_changes.c.id == m_id)
                         .values(accumulation_basis=s["accumulation_basis"]))

        # Same PO, but one line now carries an extra reason: mixed, so per line again.
        with w.engine.begin() as conn:
            conn.execute(proposed_changes.update()
                         .where(proposed_changes.c.id == m_id)
                         .values(attention_reason=s["attention_reason"]
                                 + "; extraction confidence medium: test"))
        main = main_text(c.get(url).text)
        check(main.count("be changed here") == 2 and "for the same reason" not in main,
              "reasons that differ keep the per-line status", str(main.count("be changed here")))
        check(main.count("no longer open") == 2 and "work out part of this line" in main,
              "and each line's own explanation")


def test_source_documents_are_served_only_through_a_proposal() -> None:
    section("GET /source/{change_id}: one proposal's document, nothing else")
    with _World() as w:
        c = w.client()
        change = w.lines("1662")["S"]
        r = c.get(f"/source/{change['id']}")
        check(r.status_code == 200 and r.content == w.doc.read_bytes()
              and r.headers["content-type"] == "application/octet-stream",
              "the cited document's bytes, as a download")
        check(c.get(f"/source/{change['source_sha256']}").status_code == 404,
              "a SHA-256 is not an address: there is no way to ask for a hash directly")
        check(c.get("/source/..%2F..%2Fetc%2Fpasswd").status_code == 404,
              "a traversal-shaped id is just an unknown change")

        # The vendor's filename is attacker-controlled and reaches a header.
        evil = '..\\..\\x/Set-Cookie: a=1\r\nX-Evil: 1\r\n\r\n<b>pack"ing;.xlsx'
        with w.engine.begin() as conn:
            conn.execute(message_attachments.update()
                         .where(message_attachments.c.content_sha256 == change["source_sha256"])
                         .values(filename=evil))
        r = c.get(f"/source/{change['id']}")
        header = r.headers["content-disposition"]
        check(r.status_code == 200 and "\r" not in header and "\n" not in header
              and "/" not in header.split("filename*=")[0] and "\\" not in header
              and "x-evil" not in {k.lower() for k in r.headers},
              "path separators and CRLF are stripped before Content-Disposition -- no "
              "injected header", header)
        from review_app.data import safe_filename

        check(safe_filename(evil) == "Set-Cookie: a=1X-Evil: 1<b>packing.xlsx"
              and safe_filename("\r\n") == "document",
              "the sanitiser keeps a safe basename, and never returns an empty name",
              safe_filename(evil))

        with w.engine.begin() as conn:
            conn.execute(sc.attachments.update()
                         .where(sc.attachments.c.content_sha256 == change["source_sha256"])
                         .values(stored_uri=str(HERE / "CLAUDE.md")))
        check(c.get(f"/source/{change['id']}").status_code == 403,
              "a stored path outside the blob root is refused")
        with w.engine.begin() as conn:
            conn.execute(sc.attachments.update()
                         .where(sc.attachments.c.content_sha256 == change["source_sha256"])
                         .values(stored_uri=str(w.doc)))
        w.doc.write_bytes(b"tampered")
        check(c.get(f"/source/{change['id']}").status_code == 409,
              "bytes that no longer match the recorded hash are refused")


INTERNAL = re.compile(r"NEEDS_|PENDING_REVIEW|NO_CHANGE|PRE_EXISTING|DISPUTED|ACCUMULATED|"
                      r"FIRST_SHIPMENT|MANUAL_ENTRY|\bR\d+\b|![A-Z]|\d%|\bsheet\b|sha256")


def main_text(html: str) -> str:
    """What Paula reads without opening anything: Details blocks, styles and tags
    removed (so hidden inputs and attribute values do not count)."""
    html = re.sub(r"<details.*?</details>", " ", html, flags=re.S)
    html = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


def test_no_internal_detail_outside_the_details_toggle() -> None:
    section("the rule: nothing internal in the main text of any page Paula sees")
    with _World() as w:
        c = w.client()
        pages = {
            "queue": c.get("/").text,
            "PO 1662": c.get(f"/po/{w.po_id('1662')}").text,
            "PO 1700 (refused)": c.get(f"/po/{w.po_id('1700')}").text,
            "PO 1800 (blocked)": c.get(f"/po/{w.po_id('1800')}").text,
            "PO 1900": c.get(f"/po/{w.po_id('1900')}").text,
            "PO 2000 (below ordered)": c.get(f"/po/{w.po_id('2000')}").text,
            "stale form": c.post(f"/po/{w.po_id('1662')}/approve",
                                 data={"csrf_token": w.app.state.csrf, "fingerprint": "0"}).text,
            "expired form": c.post(f"/po/{w.po_id('1662')}/approve", data={}).text,
            "no document": c.get("/source/nope").text,
        }
        leaks = {name: sorted(set(INTERNAL.findall(main_text(html))))
                 for name, html in pages.items()}
        leaks = {k: v for k, v in leaks.items() if v}
        check(not leaks, "no state name, row reference, sheet, percentage or hash outside "
              "Details on any page", str(leaks or "none"))
        check("<details" in pages["PO 1662"] and "State: NEEDS_ATTENTION" in pages["PO 1662"],
              "and the internal detail is still there, behind the toggle")


def test_a_slip_below_ordered_is_shown_with_its_figures_and_left_out() -> None:
    section("below ordered (2026-11-09): the figures shown, nothing approvable, reject clears it")
    import html as htmllib

    with _World() as w:
        row = w.lines("2000")["S"]
        check(row["state"] == "NEEDS_ATTENTION" and row["proposed_quantity"] is None
              and float(row["current_quantity"]) == 216.0,
              "premise: 216 ordered, slip 37, nothing proposed",
              f"{row['state']}/{row['proposed_quantity']}/{row['current_quantity']}")
        c = w.client()
        page = htmllib.unescape(main_text(c.get(f"/po/{w.po_id('2000')}").text))
        check("216 ordered, 37 on this slip" in page
              and "This slip lists 37 of the 216 units still to receive on PO 2000 (216 "
                  "ordered, none received yet)." in page
              and "If more is coming, nothing needs changing." in page
              and "in NetSuite, change line 14 from 216 to 37." in page,
              "the reviewer reads both figures, the PO's coverage and the figure to enter, "
              "in the main text")
        check("The packing slip gives no quantity" not in page,
              "and is never told the slip gave no quantity")
        # The under-only summary branch, which PO 1662 exercised until 2026-11-09.
        check("This packing slip ships 37 units against 216 ordered on these lines: less "
              "than ordered on every line that differs." in page,
              "the summary says less than ordered, in words", page[:0])
        from review_app import data as rdata

        with w.engine.connect() as conn:
            view = rdata.load_po(conn, w.po_id("2000"))
        check(not view.can_approve and [ln.disposition for ln in view.lines] == ["block"]
              and view.lines[0].exclusion_short.startswith("this slip ships less than is ordered"),
              "there is nothing to approve: the line is left out, as below ordered",
              f"{view.can_approve}/{[ln.disposition for ln in view.lines]}")
        r = c.post(f"/po/{w.po_id('2000')}/reject",
                   data={**w.form(c, "2000"), "reason": "more is coming", "confirmed": "yes"},
                   follow_redirects=False)
        after = w.lines("2000")["S"]
        check(r.status_code == 303 and after["human_verdict"] == "REJECTED",
              "Reject records the decision on the line", f"{r.status_code}/{after['human_verdict']}")
        check("2000" not in main_text(c.get("/").text),
              "and the PO leaves the queue, with nothing sent anywhere")


def test_the_wording_map_tracks_what_the_matcher_writes() -> None:
    section("plain wording: every phrase is one the matcher really writes; nothing dropped")
    from types import SimpleNamespace

    from review_app import wording

    source = (HERE / "matcher.py").read_text(encoding="utf-8").lower()
    missing = [key for key, phrase, _plain in wording.REASON_CATEGORIES
               if phrase.lower() not in source]
    check(not missing, "every phrase the wording keys on still appears in matcher.py -- a "
          "reworded reason would otherwise fall to the generic message unnoticed",
          str(missing or "none"))
    samples = {
        "2 NetSuite line(s) match this style/colour/size (line 3, 41) but none is open":
            "no longer open",
        "extraction confidence medium: PO carried forward": "work out part of this line",
        "NetSuite line 3 already shows 62 received, from before this tool tracked it, but "
        "this slip says 60": "already shows goods received",
        "NetSuite line 5 holds 44, but ... Someone changed the line outside this tool":
            "has been changed since",
    }
    for reason, expected in samples.items():
        got = wording.reasons(reason)
        check(len(got) == 1 and expected in got[0], f"{reason[:45]!r} -> one right sentence",
              str(got))
    check(wording.reasons("something the matcher says next year") == [wording.UNRECOGNISED],
          "an unrecognised reason is never dropped: it says 'needs a look'")

    # Given the row, the already-received sentence states the figures -- and never a sum.
    received = "NetSuite line 5 already shows 52 received, from before this tool tracked it"
    both = wording.reasons(received, {"current_quantity": "44.000",
                                      "current_quantity_received": "52.000",
                                      "src_quantity_text": "50"})
    check(len(both) == 1 and "44 ordered but 52 received" in both[0]
          and "This packing slip says 50, and NetSuite has 52 received" in both[0],
          "with figures to hand, the sentence states them plainly", str(both))
    check(not any(s in both[0] for s in ("102", "96", "94")),
          "and adds none of them together", both[0])

    def blocked(**row):
        return SimpleNamespace(row={"state": "NEEDS_ATTENTION", "ns_line_id": "3",
                                    "ns_line_is_open": True, "accumulation_basis": None, **row})

    pre = wording.block_explanations([blocked(accumulation_basis="PRE_EXISTING_RECEIPT")] * 2)
    check(pre == [(wording.BLOCK_MESSAGES["pre_existing"], 2)]
          and pre[0][0].startswith("NetSuite already shows goods received on these lines")
          and "doesn't add a packing slip to earlier receipts" in pre[0][0],
          "a PRE_EXISTING_RECEIPT PO explains the disabled Approve without offering a total")
    check(wording.block_explanations([blocked(ns_line_is_open=False)])[0][0]
          == wording.BLOCK_MESSAGES["not_updatable"],
          "a line with no open NetSuite line gets the not-updatable message")

    # Never below ordered (2026-11-09): the matcher's own sentence is shown whole --
    # it is written for the reviewer -- and only that reason, not its neighbours.
    below = ("This packing slip ships less than is ordered on NetSuite line 14: 216 ordered, "
             "37 on this slip. The tool never proposes a quantity below what is ordered, so it "
             "hasn't proposed one. This slip lists 1,049 of the 4,400 units ordered on PO 1657."
             " If more is coming, nothing needs changing. If this is the last shipment, the PO "
             "needs reducing — do that in NetSuite.")
    got = wording.reasons(below + "; extraction confidence medium: PO carried forward")
    check(len(got) == 2 and got[0] == below and "work out part of this line" in got[1],
          "the below-ordered reason is shown verbatim, and the next reason separately",
          str(got)[:160])
    # Every summary_sentence branch, by its counts (PO 1662 stopped covering "less
    # than" and nothing covered "mixed" once the world's figures moved, 2026-11-09).
    def summ(over, under, equal, matched=3):
        return SimpleNamespace(shipped_total=89, matched_lines=matched, ordered_on_matched=83,
                               shipped_on_matched=89, over=over, under=under, equal=equal)
    for counts, expected in (((2, 0, 1), "more than ordered on every line that differs"),
                             ((0, 2, 1), "less than ordered on every line that differs"),
                             ((0, 0, 3), "exactly the ordered quantity on every line"),
                             ((1, 1, 1), "more than ordered on some lines and less on others")):
        check(expected in wording.summary_sentence(summ(*counts)),
              f"summary sentence: {expected}")
    check("None of its lines matched" in wording.summary_sentence(summ(0, 0, 0, matched=0)),
          "summary sentence: nothing matched")

    kind = wording.block_kind(blocked(accumulation_basis="FIRST_SHIPMENT", attention_reason=below))
    check(kind == "below_ordered" and "never lowers an ordered quantity"
          in wording.BLOCK_MESSAGES[kind],
          "and the disabled Approve explains it as below ordered, not as 'no quantity'", kind)

    many = wording.rows_lost_message({"lines_extracted": 44, "removed": [
        {"reason": "NOT SHIPMENT DATA (no carton backing)"}] * 16})
    one = wording.rows_lost_message({"lines_extracted": 4, "removed": [
        {"reason": "NOT SHIPMENT DATA (no carton backing)"}]})
    check(many.startswith("16 of the 44 lines on this packing slip were not counted as "
                          "shipped") and many.endswith("unless you think that's wrong."),
          "the escalation reads as information, generated from the counts", many[:60])
    check(one.startswith("1 of the 4 lines on this packing slip was not counted")
          and "It may belong" in one, "and is grammatical for a single line", one[:60])


#: The ONE thing the review app may take from the write side (RUNBOOK section 11,
#: Phase 4 decision 2): this function, imported under this exact name, called
#: with this signature, and never handed a client. NARROWED from "nothing at all"
#: on 2026-11-02, not removed. Widening it is a decision, made here on purpose.
#: Moved DELIBERATELY the same day from the per-line `write_approved_quantity`
#: (now an internal fallback the app may NOT import) to the per-PO batch
#: (RUNBOOK section 11 decision 2): one PO in, a result per line out.
PERMITTED_MODULE, PERMITTED_FUNCTION = "write_path", "write_po_batch"
PERMITTED_SIGNATURE = (
    ("engine", "POSITIONAL_OR_KEYWORD", "<none>"),
    ("shipment_po_id", "POSITIONAL_OR_KEYWORD", "<none>"),
    ("client", "KEYWORD_ONLY", None),
    ("now", "KEYWORD_ONLY", None),
)
#: The keywords the app may pass. `client` is for tests: an app that builds its
#: own client has reached past the one function.
PERMITTED_KEYWORDS = {"now"}
WRITE_SIDE = ("netsuite_client", "matcher", "ingest", PERMITTED_MODULE)


def _write_side_reach(name: str, source: str) -> list[str]:
    """Every way `source` reaches the write side beyond the one permitted function."""
    found = []
    for node in ast.walk(ast.parse(source)):
        where = f"{name}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Import):
            found += [f"{where}: import {a.name}" for a in node.names
                      if a.name.split(".")[0] in WRITE_SIDE]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in WRITE_SIDE:
            for a in node.names:
                if not (node.module == PERMITTED_MODULE and node.level == 0
                        and a.name == PERMITTED_FUNCTION and a.asname is None):
                    found.append(f"{where}: from {node.module} import {a.name}"
                                 + (f" as {a.asname}" if a.asname else ""))
        elif isinstance(node, ast.Call):
            func = node.func
            called = (func.id if isinstance(func, ast.Name)
                      else func.attr if isinstance(func, ast.Attribute) else "")
            if called in ("import_module", "__import__"):
                found.append(f"{where}: dynamic import")
            if called == PERMITTED_FUNCTION:
                extra = [k.arg or "**" for k in node.keywords if k.arg not in PERMITTED_KEYWORDS]
                if extra or len(node.args) > 2 or any(isinstance(a, ast.Starred)
                                                      for a in node.args):
                    found.append(f"{where}: {PERMITTED_FUNCTION} called with "
                                 f"{extra or 'extra positional arguments'}")
    return found


def test_review_app_never_imports_netsuite_client() -> None:
    section("the review app reaches NetSuite through ONE function, and nothing else")
    package = HERE / "review_app"
    reach = []
    for path in package.rglob("*.py"):
        reach += _write_side_reach(path.name, path.read_text(encoding="utf-8"))
    check(not reach, f"review_app takes nothing from netsuite_client, matcher or ingest, and "
          f"only {PERMITTED_FUNCTION} from {PERMITTED_MODULE}", str(reach or "none"))

    # The scanner must be able to fail, on every widening it exists to catch.
    permitted = (f"from {PERMITTED_MODULE} import {PERMITTED_FUNCTION}\n"
                 f"{PERMITTED_FUNCTION}(engine, shipment_po_id, now=now)\n")
    widenings = {
        "the per-line helper (the fallback, not an entry point)":
            f"from {PERMITTED_MODULE} import write_approved_quantity",
        "the client module": "import netsuite_client",
        "a name from the client": "from netsuite_client import NetSuiteClient",
        "the matcher": "import matcher",
        "ingest": "from ingest import ingest_shipment",
        "the whole write module": f"import {PERMITTED_MODULE}",
        "a second name": f"from {PERMITTED_MODULE} import {PERMITTED_FUNCTION}, _live_client",
        "an alias": f"from {PERMITTED_MODULE} import {PERMITTED_FUNCTION} as w",
        "a client passed in": permitted.replace("now=now", "client=c"),
        "keywords splatted": permitted.replace("now=now", "**kw"),
        "a dynamic import": "importlib.import_module('netsuite_client')",
    }
    missed = [label for label, src in widenings.items() if not _write_side_reach("x", src)]
    check(not _write_side_reach("x", permitted) and not missed,
          "the scanner passes the permitted call and catches each widening",
          f"missed: {missed}" if missed else f"{len(widenings)} widenings caught")

    import inspect

    import write_path

    params = tuple((p.name, p.kind.name,
                    "<none>" if p.default is inspect.Parameter.empty else p.default)
                   for p in inspect.signature(getattr(write_path, PERMITTED_FUNCTION))
                   .parameters.values())
    check(params == PERMITTED_SIGNATURE,
          f"{PERMITTED_FUNCTION} has exactly the pinned signature: no way to pass fields",
          str(params))
    own = [r for r in _write_side_reach("write_path.py",
                                        (HERE / "write_path.py").read_text(encoding="utf-8"))
           if "matcher" in r or "ingest" in r]
    check(not own, f"{PERMITTED_MODULE} imports neither matcher nor ingest, so the "
          "permitted import cannot carry them in", str(own or "none"))

    probe = (
        "import sys\n"
        "import review_app.app, review_app.data, review_app.__main__\n"
        "def loaded(): return sorted(m for m in ('netsuite_client','matcher','ingest',"
        "'write_path') if m in sys.modules)\n"
        "app = loaded()\n"
        "import write_path\n"
        "print(app)\n"
        "print(loaded())\n")
    out = subprocess.run([sys.executable, "-c", probe], cwd=HERE, capture_output=True,
                         text=True, timeout=120)
    app_loaded = (out.stdout.splitlines() or [""])[0]
    check(out.returncode == 0 and "matcher" not in out.stdout and "ingest" not in out.stdout,
          "a fresh interpreter loading the whole app, and then the write function as the "
          "wired app will, never loads matcher or ingest", (out.stdout.strip()
                                                            or out.stderr.strip())[-200:])
    check(("netsuite_client" in app_loaded) == ("write_path" in app_loaded),
          "and the client is loaded by the app only by way of the write function",
          app_loaded)


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            _results.append((False, f"{fn.__name__} crashed", ""))
    passed = sum(1 for ok, _n, _d in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    print(json.dumps({"failed": [n for ok, n, _d in _results if not ok]}) if passed != len(_results) else "")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
