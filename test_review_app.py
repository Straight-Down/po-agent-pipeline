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
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

import schema as sc
from schema import audit_log, message_attachments, proposed_changes, shipment_pos

HERE = Path(__file__).resolve().parent
#: UTC, stored naive -- the database's convention (`ingest._utcnow`).
NOW = dt.datetime(2026, 9, 30, 12, 0, 0, tzinfo=dt.timezone.utc).replace(tzinfo=None)
REVIEWER = "Paula Test"
_results: list[tuple[bool, str, str]] = []


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
    in-memory SQLite database is private to the connection that made it."""
    engine = create_engine(f"sqlite:///{path}")
    sc.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sc.change_states.insert(), [
            {"state": s, "is_terminal": t, "description": d} for s, t, d in sc.CHANGE_STATES])
        conn.execute(sc.change_state_transitions.insert(), [
            {"from_state": f, "to_state": t, "trigger": g, "actor_kind": a}
            for f, t, g, a in sc.CHANGE_STATE_TRANSITIONS])
        for _name, ddl in sc.VIEWS:
            conn.execute(text(ddl))
    return engine


class World:
    """One ingested shipment covering four POs, each shaped for one behaviour."""

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
        ], parser="claude-assisted", vendor_name="Inprotex",
            ship_info={"etd": "2026/6/27 19:40", "eta": "2026/6/27 16:45"})
        client = NetSuiteClient(mock_data={
            "1662": [ns_line("18", size="S", qty=12), ns_line("19", size="M", qty=71),
                     ns_line("20", size="L", qty=30)],
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

    def client(self) -> TestClient:
        from review_app.app import create_app
        from review_app.settings import Settings

        settings = Settings(db_url="unused", blob_root=self.tmp, reviewer=REVIEWER)
        self.app = create_app(settings, engine=self.engine, now=lambda: NOW)
        return TestClient(self.app)

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
        check("Approve 3 lines (1 flagged)" in html,
              "the button says exactly what it covers", "Approve 3 lines (1 flagged)")
        check("The tool had to work out part of this line itself. Check it against the "
              "packing slip before approving." in html
              and "Extractor's note: PO carried forward from the row above" in html,
              "a flagged line says what to do; the extractor's own note is under Details (§5)")
        check("That's for your reference only." in html and "arrives 2026/6/27 16:45" in html,
              "vendor dates shown as reference (§9)")
        check('name="receipt_date" value=""' in html and 'value="2026' not in html,
              "and never pre-filled into the date field (§9/§10)")
        # PO 1662: S 9 vs 12, M 50 vs 71, L 30 vs 30 -- under on both that differ.
        check("This packing slip ships 89 units against 113 ordered on these lines: less than "
              "ordered on every line that differs." in html
              and "2 under, 1 equal" in html,
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


def test_a_flagged_line_that_already_matches_is_still_approved() -> None:
    section("a flagged line whose figure already matches NetSuite is APPROVED, not stranded")
    with _World() as w:
        before = w.lines("1900")["S"]
        check(before["state"] == "NEEDS_ATTENTION"
              and float(before["proposed_quantity"]) == float(before["current_quantity"]) == 5.0,
              "premise: NEEDS_ATTENTION with proposed == current (5 = 5)")
        c = w.client()
        c.post(f"/po/{w.po_id('1900')}/approve", data=w.form(c, "1900"))
        after = w.lines("1900")["S"]
        check(after["state"] == "APPROVED" and after["human_verdict"] == "ACCEPTED"
              and after["quantity_write_status"] == "APPROVED"
              and float(after["approved_quantity"]) == 5.0,
              "APPROVED with its quantity scope -- it used to keep NEEDS_ATTENTION with a "
              "verdict, drop out of the queue, and never be approved",
              f"{after['state']} / {after['quantity_write_status']}")


def test_the_date_skips_a_human_override() -> None:
    section("the optional date: applied, except where a person already set one")
    with _World() as w:
        with w.engine.begin() as conn:
            conn.execute(proposed_changes.update()
                         .where(proposed_changes.c.id == w.lines("1662")["M"]["id"])
                         .values(current_override_flag=True))
        c = w.client()
        html = c.get(f"/po/{w.po_id('1662')}").text
        check("a date you enter won't be applied to it" in html,
              "the protected line is named on the page before approving")
        c.post(f"/po/{w.po_id('1662')}/approve",
               data={**w.form(c, "1662"), "receipt_date": "2026-10-15"})
        lines = w.lines("1662")
        check(str(lines["S"]["confirmed_receipt_date"]) == "2026-10-15"
              and lines["S"]["date_approved_by"] == REVIEWER
              and lines["S"]["date_write_status"] == "APPROVED",
              "the date lands on an ordinary line, with the reviewer's name on it")
        check(lines["M"]["confirmed_receipt_date"] is None
              and lines["M"]["date_write_status"] == "NONE",
              "and is NOT applied to the line carrying a human override")
        check(lines["L"]["state"] == "APPROVED" and str(lines["L"]["confirmed_receipt_date"])
              == "2026-10-15", "an unchanged line given a date becomes APPROVED for the date")


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


def test_resolution_and_assignment_are_refused_not_trimmed() -> None:
    section("NEEDS_RESOLUTION / NEEDS_ASSIGNMENT: refuse the PO, name the lines")
    with _World() as w:
        c = w.client()
        states = {ln["state"] for ln in w.lines("1700").values()}
        check(states == {"NEEDS_RESOLUTION"}, "premise: PO 1700 is NEEDS_RESOLUTION", str(states))
        r = c.get(f"/po/{w.po_id('1700')}")
        check(r.status_code == 409 and "can't be reviewed here yet" in r.text
              and "NEEDS_RESOLUTION" in r.text and "Let Kiko know" in r.text,
              "the page refuses, says what to do, and names the line under Details")
        r = c.post(f"/po/{w.po_id('1700')}/approve",
                   data={"csrf_token": w.app.state.csrf, "fingerprint": "x"})
        check(r.status_code == 409 and w.lines("1700")["S"]["human_verdict"] is None,
              "and an approve POST for it records nothing")
        with w.engine.begin() as conn:
            conn.execute(proposed_changes.update()
                         .where(proposed_changes.c.id == w.lines("1700")["S"]["id"])
                         .values(state="NEEDS_ASSIGNMENT"))
        check(c.get(f"/po/{w.po_id('1700')}").status_code == 409,
              "NEEDS_ASSIGNMENT is refused the same way")


def test_a_blocking_line_disables_approve() -> None:
    section("a line with no open target blocks the PO's approval")
    with _World() as w:
        c = w.client()
        html = c.get(f"/po/{w.po_id('1800')}").text
        check("disabled" in html and "match an open line on this PO in NetSuite" in html
              and "be approved yet" in html,
              "Approve is disabled and the page says why, in words")
        r = c.post(f"/po/{w.po_id('1800')}/approve", data=w.form(c, "1800"))
        check(r.status_code == 409 and w.lines("1800")["S"]["human_verdict"] is None,
              "and a POST anyway is refused")


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
        with w.engine.begin() as conn:  # make the override message appear too
            conn.execute(proposed_changes.update()
                         .where(proposed_changes.c.id == w.lines("1662")["M"]["id"])
                         .values(current_override_flag=True))
        c = w.client()
        pages = {
            "queue": c.get("/").text,
            "PO 1662": c.get(f"/po/{w.po_id('1662')}").text,
            "PO 1700 (refused)": c.get(f"/po/{w.po_id('1700')}").text,
            "PO 1800 (blocked)": c.get(f"/po/{w.po_id('1800')}").text,
            "PO 1900": c.get(f"/po/{w.po_id('1900')}").text,
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
        "this line had 62 units received before the tool started tracking it":
            "already received in NetSuite",
        "NetSuite line 5 holds 44, but ... Someone changed the line outside this tool":
            "has been changed since",
    }
    for reason, expected in samples.items():
        got = wording.reasons(reason)
        check(len(got) == 1 and expected in got[0], f"{reason[:45]!r} -> one right sentence",
              str(got))
    check(wording.reasons("something the matcher says next year") == [wording.UNRECOGNISED],
          "an unrecognised reason is never dropped: it says 'needs a look'")

    def blocked(**row):
        return SimpleNamespace(row={"state": "NEEDS_ATTENTION", "ns_line_id": "3",
                                    "ns_line_is_open": True, "accumulation_basis": None, **row})

    pre = wording.block_explanations([blocked(accumulation_basis="PRE_EXISTING_RECEIPT")] * 2)
    check(pre == [(wording.BLOCK_MESSAGES["pre_existing"], 2)]
          and pre[0][0].startswith("These lines were already received in NetSuite before this "
                                   "packing slip arrived.")
          and pre[0][0].endswith("There's nothing to approve here yet."),
          "a PRE_EXISTING_RECEIPT PO explains the disabled Approve in the agreed words")
    check(wording.block_explanations([blocked(ns_line_is_open=False)])[0][0]
          == wording.BLOCK_MESSAGES["not_updatable"],
          "a line with no open NetSuite line gets the not-updatable message")

    many = wording.rows_lost_message({"lines_extracted": 44, "removed": [
        {"reason": "NOT SHIPMENT DATA (no carton backing)"}] * 16})
    one = wording.rows_lost_message({"lines_extracted": 4, "removed": [
        {"reason": "NOT SHIPMENT DATA (no carton backing)"}]})
    check(many.startswith("16 of the 44 lines on this packing slip were not counted as "
                          "shipped") and many.endswith("unless you think that's wrong."),
          "the escalation reads as information, generated from the counts", many[:60])
    check(one.startswith("1 of the 4 lines on this packing slip was not counted")
          and "It may belong" in one, "and is grammatical for a single line", one[:60])


def test_review_app_never_imports_netsuite_client() -> None:
    section("the review app cannot reach NetSuite: not imported, directly or transitively")
    package = HERE / "review_app"
    direct = []
    for path in package.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            direct += [f"{path.name}: {n}" for n in names
                       if n.split(".")[0] in ("netsuite_client", "matcher", "ingest")]
    check(not direct, "no module in review_app imports netsuite_client (or matcher/ingest, "
          "which import it)", str(direct or "none"))
    probe = ("import sys; import review_app.app, review_app.data, review_app.__main__; "
             "print(sorted(m for m in ('netsuite_client','matcher','ingest') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", probe], cwd=HERE, capture_output=True,
                         text=True, timeout=120)
    check(out.returncode == 0 and out.stdout.strip() == "[]",
          "and a fresh interpreter importing the whole app never loads it transitively",
          (out.stdout.strip() or out.stderr.strip())[-200:])


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
