"""
The NetSuite quantity write (`write_path.write_approved_quantity`). Offline.

NetSuite is a fake HTTP session behind the REAL `NetSuiteClient`, so what these
tests inspect is the request the client actually put on the wire -- the PATCH
body, its headers, the order of GET and PATCH -- not a mock's idea of a call.
The database goes through `dialect_target`, so `--mssql` runs every test here
on SQL Server.

    python -m pytest test_write_path.py -q
    python -m pytest test_write_path.py -q --mssql
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import traceback
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import config
import dialect_target as dt_target
import schema as sc
import write_path as wp
from types import SimpleNamespace

import netsuite_client as nc
from netsuite_client import NetSuiteClient, NetSuiteConfig
from schema import audit_log, proposed_changes, write_attempts
from test_schema import mk_change, mk_message, mk_po, mk_shipment

#: UTC, stored naive -- the database's convention.
NOW = dt.datetime(2026, 11, 2, 9, 0, 0, tzinfo=dt.timezone.utc).replace(tzinfo=None)
REVIEWER = "Paula Test"
PO_ID = "8489541"
_results: list[tuple[bool, str, str]] = []

#: Never the real file: these tests break recording on purpose (conftest does the
#: same for pytest; this covers `python test_write_path.py`).
UNRECORDED_FILE = Path(tempfile.mkdtemp(prefix="po-agent-test-")) / "unrecorded-writes.jsonl"
os.environ[config.UNRECORDED_WRITES_ENV] = str(UNRECORDED_FILE)


def guarded(fn, *args, expect_unrecorded=False, **kwargs):
    """
    Call a write function, and RE-RAISE any recording failure it swallowed.

    The write path catches a failure to write `write_attempts` so its caller still
    gets a per-line result. That hid a dropped SQL Server connection on 2026-11-02:
    the test saw a changed result, failed as an assertion, and the harness -- which
    re-runs only a test that raised a connection-class error -- never saw the link
    error. Raised here, in the test's own call phase, the harness classifies it.
    A test that breaks recording on purpose says so with `expect_unrecorded=True`.
    """
    before = wp.unrecorded_seq()
    result = fn(*args, **kwargs)
    new = [f for f in wp.unrecorded_failures if f.seq > before]
    if new and not expect_unrecorded:
        raise new[0].error
    return result


def check(ok: bool, name: str, detail: str = "") -> bool:
    _results.append((bool(ok), name, detail))
    line = f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  --  {detail}" if detail else "")
    # The result messages carry an arrow; a cp1252 console cannot print it.
    print(line.encode("ascii", "backslashreplace").decode("ascii"))
    return bool(ok)


def section(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 70 - len(title)))


# ---------------------------------------------------------------------------
# a fake NetSuite, behind the real client
# ---------------------------------------------------------------------------

FAKE_CONFIG = NetSuiteConfig(
    account_id="1321665-sb2",
    client_id="fake_consumer_key_for_offline_tests",
    certificate_id="FAKE_CERT_ID_123",
    private_key_path=Path("never-read.pem"),  # the token is preset; no assertion is built
)

PERMISSION_DENIED = {"o:errorDetails": [{
    "o:errorCode": "USER_ERROR",
    "detail": "Your current role does not have permission to perform this action."}]}


class Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def ns_line(no, quantity, **kw):
    """A purchaseOrder item line shaped like the live sandbox's."""
    return {
        "line": no,
        "item": {"id": "41231", "refName": "M120246 : M120246-Waterman Polo-TID-M"},
        "quantity": quantity,
        "units": {"id": "1", "refName": "Ea"},
        "expectedReceiptDate": "2026-07-15",
        "custcol_override_expected_receipt": False,
        "custcol_sd_updatedreceiptdate": None,
        "custcol_sd_fg_excluderepspark": True,
        "custcol_sd_tmpl_style": "M120246",
        "custcol_product_color": {"id": "12", "refName": "TID"},
        "custcol_product_size": {"id": "4", "refName": "M"},
        "isClosed": False,
        "isOpen": True,
        "quantityReceived": 0,
        **kw,
    }


class FakeNetSuite:
    """One purchaseOrder that answers GET and PATCH like the REST API, logging each request.

    `get_failures` / `patch_failures` are replayed first. A patch failure is
    `(response, landed)`: `landed=True` applies the PATCH and THEN fails, which is
    what a timeout after the request reached NetSuite looks like from here. The
    response may be an exception (raised after applying), and `landed` may be a
    set of line numbers, applying only those entries of a batch."""

    def __init__(self, lines):
        self.lines = {int(ln["line"]): dict(ln) for ln in lines}
        self.requests: list[tuple[str, str, dict]] = []
        self.get_failures: list = []
        #: Replayed only for reads AFTER a PATCH -- a closing read that fails while
        #: the opening read succeeded.
        self.get_failures_after_patch: list = []
        self.patch_failures: list = []
        self.patched = False

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if method == "GET":
            if self.get_failures:
                return self.get_failures.pop(0)
            if self.patched and self.get_failures_after_patch:
                return self.get_failures_after_patch.pop(0)
            items = [dict(v) for _, v in sorted(self.lines.items())]
            return Resp(200, {"id": PO_ID, "tranId": "PO0001662",
                              "entity": {"id": "7", "refName": "Inprotex"},
                              "item": {"items": items, "totalResults": len(items)}})
        if method == "PATCH":
            self.patched = True
            failure = self.patch_failures.pop(0) if self.patch_failures else None
            landed = True if failure is None else failure[1]
            for sent in kwargs["json"]["item"]["items"]:
                if landed is True or (isinstance(landed, (set, frozenset))
                                      and sent["line"] in landed):
                    self.lines[sent["line"]].update(
                        {k: v for k, v in sent.items() if k != "line"})
            if failure is None:
                return Resp(204)
            if isinstance(failure[0], BaseException):
                raise failure[0]
            return failure[0]
        raise AssertionError(f"unexpected {method} {url}")

    def methods(self):
        return [m for m, _u, _k in self.requests]

    def patches(self):
        """(body, idempotency key) of every PATCH attempt, in order."""
        return [(k["json"], k["headers"]["X-NetSuite-Idempotency-Key"])
                for m, _u, k in self.requests if m == "PATCH"]


def client_for(ns: FakeNetSuite) -> NetSuiteClient:
    client = NetSuiteClient(config=FAKE_CONFIG)
    client._session = ns
    client._access_token = "fake-token"
    client._token_expires_at = 9_999_999_999
    client.TRANSIENT_BACKOFF_SECONDS = 0
    return client


# ---------------------------------------------------------------------------
# a database with one approved line
# ---------------------------------------------------------------------------


def world(*, ns_lines=None, base=71, approved=50, **change_kw):
    """One APPROVED change for PO 1662 line 18, and a NetSuite holding `ns_lines`.

    On a server every `fresh_engine()` empties the one database, so a test must
    finish with one world before building the next."""
    engine = dt_target.fresh_engine()
    values = dict(state=sc.STATE_APPROVED, ns_line_id="18", current_quantity=base,
                  proposed_quantity=approved, approved_quantity=approved,
                  quantity_approved_by=REVIEWER, quantity_approved_at=NOW,
                  quantity_write_status="APPROVED", human_verdict="ACCEPTED",
                  verdict_by=REVIEWER, verdict_at=NOW, accumulation_basis="FIRST_SHIPMENT")
    values.update(change_kw)
    with engine.begin() as conn:
        ship = mk_shipment(conn, message_id=mk_message(conn, "msg-write"), primary_sha=None)
        po = mk_po(conn, ship, "PO NO : 1662", "1662")
        change = mk_change(conn, ship, po, size="m", **values)
    ns = FakeNetSuite(ns_lines if ns_lines is not None
                      else [ns_line(18, 71), ns_line(19, 5)])
    return engine, change, ns


def write(engine, change, ns, **kw):
    return guarded(wp.write_approved_quantity, engine, change, client=client_for(ns),
                   now=NOW, **kw)


def attempts(engine, change):
    with engine.connect() as conn:
        return conn.execute(select(write_attempts).where(write_attempts.c.change_id == change)
                            .order_by(write_attempts.c.attempt_no)).all()


def change_row(engine, change):
    with engine.connect() as conn:
        return conn.execute(select(proposed_changes)
                            .where(proposed_changes.c.id == change)).one()


def audits(engine, change):
    with engine.connect() as conn:
        return conn.execute(select(audit_log).where(audit_log.c.change_id == change)
                            .order_by(audit_log.c.occurred_at)).all()


def num(value):
    return None if value is None else Decimal(value)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_a_write_sends_quantity_and_nothing_else() -> None:
    section("an approved line: re-read, then one PATCH carrying quantity alone")
    engine, change, ns = world()
    result = write(engine, change, ns)

    check(result.status == wp.WRITTEN, "the line is written", result.message)
    check(result.message == "PO 1662 line 18: quantity 71 → 50.",
          "and the result reads 'previous -> new', naming the PO and line", result.message)
    check(result.previous_quantity == 71 and result.new_quantity == 50,
          "previous and new are carried for display",
          f"{result.previous_quantity} -> {result.new_quantity}")

    check(ns.methods() == ["GET", "PATCH"],
          "NetSuite is re-read immediately before the PATCH", str(ns.methods()))
    _m, url, kwargs = ns.requests[1]
    check(url.endswith(f"/record/v1/purchaseOrder/{PO_ID}"), "the PATCH targets the PO", url)
    check(kwargs["json"] == {"item": {"items": [{"line": 18, "quantity": 50}]}},
          "the body on the wire is the line number and quantity, nothing else",
          json.dumps(kwargs["json"]))
    check("replace" not in (kwargs.get("params") or {}) and "replace" not in url,
          "no `replace` parameter (it would wipe every other line)")
    check(ns.lines[18]["expectedReceiptDate"] == "2026-07-15"
          and ns.lines[18]["custcol_override_expected_receipt"] is False
          and ns.lines[18]["custcol_sd_updatedreceiptdate"] is None
          and ns.lines[18]["custcol_sd_fg_excluderepspark"] is True,
          "the line's date fields and excluderepspark are as they were")
    check(ns.lines[18]["quantity"] == 50 and ns.lines[19]["quantity"] == 5,
          "NetSuite now holds 50 on line 18, and line 19 is untouched")

    rows = attempts(engine, change)
    check(len(rows) == 1, "one write_attempts row", str(len(rows)))
    a = rows[0]
    check(a.outcome == "SUCCESS" and a.attempt_no == 1 and a.scope == "QUANTITY",
          "recorded as attempt 1, SUCCESS, QUANTITY scope", f"{a.outcome} #{a.attempt_no}")
    check(num(a.previous_quantity) == 71, "with the re-read value as previous_quantity",
          str(a.previous_quantity))
    check(json.loads(a.payload_json) == {"quantity": 50} and a.http_status == 204,
          "with exactly what was sent, and the 204", f"{a.payload_json} / {a.http_status}")
    check(kwargs["headers"]["X-NetSuite-Idempotency-Key"] == a.idempotency_key,
          "the idempotency key on the wire is the one recorded")
    row = change_row(engine, change)
    check(row.state == sc.STATE_WRITTEN and row.quantity_write_status == "WRITTEN",
          "the change is WRITTEN", f"{row.state}/{row.quantity_write_status}")
    log = audits(engine, change)
    check([(e.event, e.from_state, e.to_state, e.actor_kind) for e in log]
          == [("QUANTITY_WRITE_SUCCESS", sc.STATE_APPROVED, sc.STATE_WRITTEN, "SYSTEM")],
          "and one audit row says so", str([(e.event, e.to_state) for e in log]))

    import ingest as ing  # the reader of what this write records
    history = ing._line_history(engine, ["1662"]).get(("1662", "18"))
    check(history is not None and history.written_quantity == 50,
          "the accumulation base now reads 50 written: the record is what ingest expects",
          str(history and history.written_quantity))


def test_a_date_approved_with_it_is_not_sent() -> None:
    section("a row approved with a date before 2026-11-02: quantity alone, and it ends WRITTEN")
    # The review app no longer records a date; only an older row can carry one.
    engine, change, ns = world(confirmed_receipt_date=dt.date(2026, 12, 1),
                               date_approved_by=REVIEWER, date_approved_at=NOW,
                               date_write_status="APPROVED")
    result = write(engine, change, ns)
    sent = ns.requests[1][2]["json"]["item"]["items"][0]
    check(result.status == wp.WRITTEN and set(sent) == {"line", "quantity"},
          "the PATCH still carries only line and quantity", str(sorted(sent)))
    check(ns.lines[18]["expectedReceiptDate"] == "2026-07-15",
          "expectedReceiptDate in NetSuite is unchanged")
    check("receipt date recorded with it was not written" in result.message,
          "the result says the date was not written", result.message)
    row = change_row(engine, change)
    check(row.quantity_write_status == "WRITTEN" and row.state == sc.STATE_WRITTEN,
          "the change is WRITTEN: never left APPROVED with nothing a write could send",
          f"{row.state} q={row.quantity_write_status} d={row.date_write_status}")
    check(row.date_write_status == "APPROVED" and str(row.confirmed_receipt_date) == "2026-12-01",
          "and its recorded date is left as it was, unused")

    again = write(engine, change, ns)
    check(again.status == wp.REFUSED and "already written" in again.message
          and ns.methods() == ["GET", "PATCH"] and len(attempts(engine, change)) == 1,
          "a second call refuses without contacting NetSuite or recording", again.message)


def test_closed_and_not_open_lines_are_refused() -> None:
    section("a line closed or no longer open: refused, nothing sent, reason in plain words")
    for label, flags, words in (
            ("closed", {"isClosed": True, "isOpen": False}, "is closed in NetSuite"),
            ("not open", {"isClosed": False, "isOpen": False}, "is no longer open")):
        engine, change, ns = world(ns_lines=[ns_line(18, 71, **flags)])
        result = write(engine, change, ns)
        check(result.status == wp.REFUSED and result.error_kind == "LINE_CLOSED",
              f"{label}: REFUSED, LINE_CLOSED", f"{result.status}/{result.error_kind}")
        check(result.message.startswith("PO 1662 line 18 ") and words in result.message
              and "nothing was written" in result.message,
              f"{label}: the reason names the PO and line", result.message)
        check(ns.methods() == ["GET"] and ns.lines[18]["quantity"] == 71,
              f"{label}: no PATCH was sent", str(ns.methods()))
        a = attempts(engine, change)
        check(len(a) == 1 and a[0].outcome == "REFUSED" and a[0].payload_json == "{}"
              and num(a[0].previous_quantity) == 71 and a[0].error_detail == result.message,
              f"{label}: recorded REFUSED, nothing sent, previous and reason kept",
              f"{a[0].outcome if a else None} {a[0].payload_json if a else ''}")
        row = change_row(engine, change)
        check(row.state == sc.STATE_WRITE_FAILED and row.quantity_write_status == "FAILED",
              f"{label}: the change is WRITE_FAILED", f"{row.state}/{row.quantity_write_status}")


def test_a_line_gone_from_the_po_is_refused() -> None:
    section("the line no longer exists on the PO")
    engine, change, ns = world(ns_lines=[ns_line(19, 5)])
    result = write(engine, change, ns)
    check(result.status == wp.REFUSED and "no longer has a line 18" in result.message
          and result.previous_quantity is None and ns.methods() == ["GET"],
          "refused, no previous value, nothing sent", result.message)


def test_an_edited_line_is_not_overwritten() -> None:
    section("quantity no longer what the proposal was based on: someone else's edit")
    engine, change, ns = world(ns_lines=[ns_line(18, 60)], base=71)
    result = write(engine, change, ns)
    check(result.status == wp.REFUSED and result.error_kind == "CONFLICT",
          "REFUSED, CONFLICT", f"{result.status}/{result.error_kind}")
    check("now shows 60" in result.message and "when it showed 71" in result.message,
          "the reason states both figures", result.message)
    check(ns.methods() == ["GET"] and ns.lines[18]["quantity"] == 60,
          "nothing sent; NetSuite keeps the other person's 60")
    a = attempts(engine, change)[0]
    check(num(a.previous_quantity) == 60,
          "previous_quantity is what NetSuite holds now, not the stored 71",
          str(a.previous_quantity))

    engine, change, ns = world(ns_lines=[ns_line(18, 50)], base=71, approved=50)
    result = write(engine, change, ns)
    check(result.status == wp.REFUSED and "already shows 50" in result.message
          and ns.methods() == ["GET"],
          "a line already at the new value is refused with its own explanation",
          result.message)

    engine, change, ns = world(base=None)
    result = write(engine, change, ns)
    check(result.status == wp.REFUSED and "no record of what" in result.message
          and ns.methods() == ["GET"],
          "with no recorded base, the edit check cannot run, so nothing is written",
          result.message)


def test_failures_are_returned_and_recorded() -> None:
    section("NetSuite errors: returned, never raised, recorded with the error")
    engine, change, ns = world()
    ns.patch_failures = [(Resp(400, PERMISSION_DENIED), False)]
    result = write(engine, change, ns)
    check(result.status == wp.FAILED and result.error_kind == "PERMISSION"
          and "doesn't have permission" in result.message,
          "a permission refusal on the PATCH comes back as FAILED/PERMISSION", result.message)
    a = attempts(engine, change)[0]
    check(a.outcome == "FAILED" and json.loads(a.payload_json) == {"quantity": 50}
          and num(a.previous_quantity) == 71 and "PermissionError" in (a.error_detail or ""),
          "recorded with what was sent, the previous value and the error",
          f"{a.outcome} {a.payload_json}")
    check(change_row(engine, change).state == sc.STATE_WRITE_FAILED, "the change is WRITE_FAILED")

    engine, change, ns = world()
    ns.get_failures = [Resp(503, {"title": "Service Unavailable"})] * 3
    result = write(engine, change, ns)
    check(result.status == wp.FAILED and result.error_kind == "TRANSIENT"
          and result.message.startswith("Couldn't read PO 1662 line 18")
          and "PATCH" not in ns.methods(),
          "NetSuite down on the re-read: FAILED/TRANSIENT, and no PATCH", result.message)
    a = attempts(engine, change)[0]
    check(a.previous_quantity is None and a.payload_json == "{}" and a.http_status == 503,
          "recorded with no previous value and nothing sent", f"{a.http_status}")

    engine, change, ns = world()
    ns.patch_failures = [(Resp(500, {"title": "Internal Error"}), False)] * 3
    result = write(engine, change, ns)
    check(result.status == wp.FAILED and "may or may not have been applied" in result.message,
          "a failed PATCH says it may have landed and that retrying is safe", result.message)


def test_a_failed_write_is_retried_per_line() -> None:
    section("retry: a failure is retried without re-approval; a write that landed is caught")
    engine, change, ns = world()
    ns.patch_failures = [(Resp(503, {}), False)] * 3
    write(engine, change, ns)
    # A later clock for the retry, as in life. With one frozen NOW the two audit
    # rows tie on occurred_at, and SQL Server returns ties in no promised order.
    later = NOW + dt.timedelta(minutes=5)
    result = guarded(wp.write_approved_quantity, engine, change, client=client_for(ns),
                     now=later)
    check(result.status == wp.WRITTEN and result.attempt_no == 2,
          "the retry writes, as attempt 2", f"{result.status} #{result.attempt_no}")
    check([(a.attempt_no, a.outcome) for a in attempts(engine, change)]
          == [(1, "FAILED"), (2, "SUCCESS")], "both attempts kept, in order")
    row = change_row(engine, change)
    check(row.state == sc.STATE_WRITTEN and row.quantity_approved_at == NOW,
          "WRITE_FAILED -> WRITTEN, and nothing was re-approved", row.state)
    check([e.event for e in audits(engine, change)]
          == ["QUANTITY_WRITE_FAILED", "QUANTITY_WRITE_SUCCESS"], "each attempt audited")

    # A timeout after the PATCH reached NetSuite: it applied, and we heard a failure.
    engine, change, ns = world()
    ns.patch_failures = [(Resp(503, {}), True)] * 3
    first = write(engine, change, ns)
    patches = ns.methods().count("PATCH")
    second = write(engine, change, ns)
    check(first.status == wp.FAILED and ns.lines[18]["quantity"] == 50,
          "the first call reports FAILED although NetSuite now holds 50")
    # Was: refused as "already shows", WRITE_FAILED forever, the next slip DISPUTED.
    # Since 2026-11-02 (Kiko): ALREADY_PRESENT -- settled, never called SUCCESS.
    check(second.status == wp.ALREADY_PRESENT and ns.methods().count("PATCH") == patches
          and "(attempt 1) sent without NetSuite confirming it" in second.message
          and "can't prove that attempt made the change" in second.message,
          "the retry finds the value attempt 1 sent: ALREADY_PRESENT, no second PATCH, and "
          "the result says it is unproven", second.message)
    rows = attempts(engine, change)
    check([(a.attempt_no, a.outcome) for a in rows] == [(1, "FAILED"), (2, "ALREADY_PRESENT")]
          and rows[1].payload_json == "{}" and num(rows[1].previous_quantity) == 50,
          "recorded as its own outcome, nothing sent, with the value found",
          str([(a.attempt_no, a.outcome) for a in rows]))
    # A set, not "the last row": both calls share the frozen NOW, and SQL Server
    # returns rows tied on occurred_at in no promised order.
    events = sorted(e.event for e in audits(engine, change))
    check(events == ["QUANTITY_WRITE_ALREADY_PRESENT", "QUANTITY_WRITE_FAILED"],
          "the audit log names it ALREADY_PRESENT -- and no SUCCESS appears", str(events))
    row = change_row(engine, change)
    check(row.state == sc.STATE_WRITTEN and row.quantity_write_status == "WRITTEN",
          "the line is unblocked: WRITTEN", f"{row.state}/{row.quantity_write_status}")
    import ingest as ing
    import matcher as mt
    history = ing._line_history(engine, ["1662"]).get(("1662", "18"))
    check(history is not None and history.written_quantity == 50,
          "the accumulation base counts it as settled at 50")
    from netsuite_client import NetSuiteClient as _C
    line_now = _C(config=FAKE_CONFIG)._map_line(ns.lines[18], PO_ID, None)
    proposed, accumulation, problem = mt._accumulated_quantity(line_now, 20, history)
    check(problem is None and accumulation.get("basis") == "ACCUMULATED" and proposed == 70,
          "and the next slip (20 more) accumulates onto it -- not DISPUTED",
          f"{accumulation.get('basis')} -> {proposed} {problem or ''}")


def insert_attempt(engine, change, no, outcome, payload, error_kind=None, http_status=None,
                   line="18"):
    """An earlier attempt on record, as the write path would have left it."""
    with engine.begin() as conn:
        conn.execute(write_attempts.insert(), {
            "id": sc.new_id(), "change_id": change, "scope": "QUANTITY", "attempt_no": no,
            "ns_internal_id": PO_ID, "ns_line_id": line, "payload_json": json.dumps(payload),
            "idempotency_key": f"earlier-{no}", "outcome": outcome, "error_kind": error_kind,
            "http_status": http_status, "attempted_at": NOW - dt.timedelta(hours=1)})


def test_already_present_is_guarded_narrowly() -> None:
    section("ALREADY_PRESENT only on an earlier, unconfirmed attempt of THIS proposal, "
            "for THIS value")

    def run(prepare, ns_line_kw=None, state=sc.STATE_WRITE_FAILED, status="FAILED"):
        engine, change, ns = world(ns_lines=[ns_line(18, 50, **(ns_line_kw or {}))],
                                   state=state, quantity_write_status=status)
        prepare(engine, change)
        return write(engine, change, ns), ns

    cases = [
        ("no earlier attempt at all (someone typed 50 by hand)", lambda e, c: None, None),
        ("an earlier attempt that sent a different value (49)",
         lambda e, c: insert_attempt(e, c, 1, "FAILED", {"quantity": 49}, "TRANSIENT"), None),
        ("an earlier attempt NetSuite refused with a 400 (nothing applied)",
         lambda e, c: insert_attempt(e, c, 1, "REFUSED", {"quantity": 50}, "VALIDATION", 400),
         None),
        ("an earlier attempt stopped at the batch cap (VALIDATION)",
         lambda e, c: insert_attempt(e, c, 1, "FAILED", {"quantity": 50}, "VALIDATION"), None),
        ("an earlier attempt refused for permission",
         lambda e, c: insert_attempt(e, c, 1, "FAILED", {"quantity": 50}, "PERMISSION"), None),
        ("an earlier attempt of this proposal aimed at a different line",
         lambda e, c: insert_attempt(e, c, 1, "FAILED", {"quantity": 50}, "TRANSIENT",
                                     line="99"), None),
        ("an earlier attempt that sent nothing (the re-read failed)",
         lambda e, c: insert_attempt(e, c, 1, "FAILED", {}, "TRANSIENT"), None),
        ("a matching attempt, but the line is closed now",
         lambda e, c: insert_attempt(e, c, 1, "FAILED", {"quantity": 50}, "TRANSIENT"),
         {"isClosed": True, "isOpen": False}),
    ]
    for label, prepare, ns_kw in cases:
        result, ns = run(prepare, ns_kw)
        check(result.status == wp.REFUSED and "PATCH" not in ns.methods(),
              f"refused, not settled: {label}", result.message[:90])

    # Another proposal's attempt on the same NetSuite line does not count for this one.
    engine, change, ns = world(ns_lines=[ns_line(18, 50)], state=sc.STATE_WRITE_FAILED,
                               quantity_write_status="FAILED")
    with engine.begin() as conn:
        other_ship = mk_shipment(conn, message_id=mk_message(conn, "msg-other"), primary_sha=None)
        other_po = mk_po(conn, other_ship, "PO NO : 1662", "1662")
        other = mk_change(conn, other_ship, other_po, size="m", ns_line_id="18",
                          state=sc.STATE_WRITE_FAILED, current_quantity=71,
                          proposed_quantity=50, approved_quantity=50,
                          quantity_approved_by=REVIEWER, quantity_approved_at=NOW,
                          quantity_write_status="FAILED")
    insert_attempt(engine, other, 1, "FAILED", {"quantity": 50}, "TRANSIENT")
    result = write(engine, change, ns)
    check(result.status == wp.REFUSED,
          "refused, not settled: a matching attempt from ANOTHER proposal on the same line",
          result.message[:90])

    # The positive case, set up by hand: the guard's conditions exactly.
    result, ns = run(lambda e, c: insert_attempt(e, c, 1, "FAILED", {"quantity": 50},
                                                 "TRANSIENT"))
    check(result.status == wp.ALREADY_PRESENT and "PATCH" not in ns.methods(),
          "settled: an earlier TRANSIENT attempt of this proposal sent exactly 50",
          result.message[:90])

    # Kept deliberately (Kiko, 2026-11-04): NetSuite answered 204 and the closing
    # re-read disagreed. NetSuite said it accepted the write -- stronger evidence it
    # landed than a timeout, not weaker.
    result, ns = run(lambda e, c: insert_attempt(e, c, 1, "FAILED", {"quantity": 50},
                                                 "OTHER", 204))
    check(result.status == wp.ALREADY_PRESENT,
          "settled: an earlier 204 whose closing re-read disagreed, for exactly 50",
          result.message[:90])


def test_a_line_already_at_the_approved_figure_is_confirmed_not_sent() -> None:
    section("approved figure == what NetSuite holds: CONFIRMED, nothing sent, a settled base")
    import ingest as ing

    # Per line. NetSuite holds 50.0; the approved figure is Decimal 50 from the
    # database: compared numerically, never as text.
    engine, change, ns = world(ns_lines=[ns_line(18, 50.0)], base=50, approved=50)
    result = write(engine, change, ns)
    row = attempts(engine, change)[0]
    check(result.status == wp.CONFIRMED and "PATCH" not in ns.methods(),
          "confirmed, and nothing is sent", result.message)
    check(row.outcome == "CONFIRMED" and json.loads(row.payload_json) == {}
          and change_row(engine, change).state == sc.STATE_WRITTEN,
          "recorded CONFIRMED with nothing sent; the line is settled")
    history = ing._line_history(engine, ["1662"]).get(("1662", "18"))
    check(history is not None and history.written_quantity == 50,
          "and the accumulation base counts it, like ALREADY_PRESENT")

    # The batch: the confirmed line is left out of the PATCH; the rest is sent.
    engine, po, changes, ns = po_world([(18, 71, 71), (19, 12, 9)], pending_line=None,
                                       extra_ns=None)
    lines = by_line(batch(engine, po, ns))
    check(lines["18"].status == wp.CONFIRMED and lines["19"].status == wp.WRITTEN
          and [b for b, _k in ns.patches()] == [{"item": {"items": [{"line": 19, "quantity": 9}]}}],
          "line 18 confirmed and left out; line 19 sent and written",
          json.dumps([b for b, _k in ns.patches()]))

    # Every line already right: no PATCH, and so no closing read either.
    engine, po, changes, ns = po_world([(18, 71, 71)], pending_line=None, extra_ns=None)
    result = batch(engine, po, ns)
    check(ns.methods() == ["GET"] and result.batch_requests == 0
          and by_line(result)["18"].status == wp.CONFIRMED,
          "nothing to send: one read, no PATCH", str(ns.methods()))


def test_a_401_the_refresh_did_not_cure_is_never_evidence() -> None:
    section("401, then the token refresh fails: PERMISSION, no closing read, never ALREADY_PRESENT")

    def refresh_fails(client):
        def authenticate(force=False):
            if force:
                raise nc.NetSuiteAuthError("token endpoint refused")
            return "fake-token"
        client.authenticate = authenticate
        return client

    engine, po, changes, ns = po_world([(18, 71, 50)], pending_line=None, extra_ns=None)
    ns.patch_failures = [(Resp(401, {"title": "Unauthorized"}), False)]
    line = by_line(batch(engine, po, ns, client=refresh_fails(client_for(ns))))["18"]
    row = attempts(engine, changes[18])[0]
    check(line.status == wp.FAILED and row.error_kind == "PERMISSION"
          and "didn't accept the tool's login" in line.message,
          "FAILED as PERMISSION, saying the login was refused", f"{row.error_kind}: {line.message}")
    check(ns.methods() == ["GET", "PATCH"],
          "no closing re-read: NetSuite applied nothing", str(ns.methods()))
    ns.lines[18]["quantity"] = 50  # someone types the approved figure by hand
    retry = by_line(batch(engine, po, ns))["18"]
    check(retry.status == wp.REFUSED and "already shows 50" in retry.message,
          "a retry is refused, never settled as ALREADY_PRESENT", retry.message[:120])

    # But NOT when an earlier try of the same request timed out after sending: that
    # try may have landed. Batch: the closing re-read decides, and finds it landed.
    import requests.exceptions as rex

    def expires_after_first_patch(client, ns, *, once=False):
        # The token lapses once a PATCH has gone out -- not before: the opening
        # re-read authenticates too, and a token that fails THERE means the PATCH
        # was never sent, which is the not-sent case, not this one. `once`: a blip
        # at the token endpoint, so the closing re-read can authenticate again.
        failed = {"n": 0}

        def authenticate(force=False):
            if (force or ns.patched) and not (once and failed["n"]):
                failed["n"] += 1
                raise nc.NetSuiteAuthError("token expired and could not be refreshed")
            return "fake-token"
        client.authenticate = authenticate
        return client

    engine, po, changes, ns = po_world([(18, 71, 50)], pending_line=None, extra_ns=None)
    ns.patch_failures = [(rex.ReadTimeout("read timed out"), True)]  # it landed
    client = expires_after_first_patch(client_for(ns), ns, once=True)
    line = by_line(batch(engine, po, ns, client=client))["18"]
    check(line.status == wp.WRITTEN and ns.methods()[-1] == "GET",
          "a timeout that landed, then a failed refresh: re-read, and WRITTEN", line.message)

    # Per line: recorded TRANSIENT (may have landed), so a retry settles it.
    engine, change, ns = world()
    ns.patch_failures = [(rex.ReadTimeout("read timed out"), True)]
    result = guarded(wp.write_approved_quantity, engine, change,
                     client=expires_after_first_patch(client_for(ns), ns), now=NOW)
    check(result.status == wp.FAILED and result.error_kind == "TRANSIENT"
          and "nothing was changed" not in result.message,
          "per line: FAILED as TRANSIENT, never 'nothing was changed'",
          f"{result.error_kind}: {result.message}")
    retry = write(engine, change, ns)
    check(retry.status == wp.ALREADY_PRESENT,
          "and the retry settles it as ALREADY_PRESENT, not stranded", retry.message[:120])

    # A 401 the refresh DID cure, then 401 again: already a permission refusal.
    engine, po, changes, ns = po_world([(18, 71, 50)], pending_line=None, extra_ns=None)
    ns.patch_failures = [(Resp(401, {"title": "Unauthorized"}), False)] * 2
    client = client_for(ns)
    client.authenticate = lambda force=False: "fake-token"
    batch(engine, po, ns, client=client)
    check(attempts(engine, changes[18])[0].error_kind == "PERMISSION",
          "a 401 surviving a successful refresh is PERMISSION too")


def test_a_refusal_before_contact_is_recorded_in_the_audit() -> None:
    section("refused before NetSuite was contacted: no attempt row, the reason in the audit")
    engine, change, ns = world()
    result = guarded(wp.write_approved_quantity, engine, change,
                     client=SimpleNamespace(config=None), now=NOW)
    events = [(e.event, json.loads(e.detail_json)["message"]) for e in audits(engine, change)]
    check(result.status == wp.REFUSED and attempts(engine, change) == []
          and change_row(engine, change).state == sc.STATE_APPROVED,
          "refused, no write_attempts row, the line left APPROVED (still sendable)")
    check(events == [(wp.NOT_SENT_EVENT, result.message)],
          "one QUANTITY_WRITE_NOT_SENT event carrying the reason", str(events))


def test_a_request_that_never_left_is_not_evidence() -> None:
    section("a write the client proved never left: nothing recorded as sent, nothing credited")
    import requests.exceptions as rex

    # The batch. The value appears on the line anyway (someone typing it) while
    # every try fails to connect: the closing re-read used to credit it as SUCCESS.
    engine, po, changes, ns = po_world([(18, 71, 50)], pending_line=None)
    ns.patch_failures = [(rex.ConnectTimeout("connect timed out"), True)] * 3
    result = batch(engine, po, ns)
    line = by_line(result)["18"]
    row = attempts(engine, changes[18])[0]
    check(line.status == wp.FAILED and line.message.startswith(
              "Nothing was sent to PO 1662 line 18: NetSuite couldn't be reached."),
          "FAILED, saying nothing was sent", line.message)
    check(row.outcome == "FAILED" and json.loads(row.payload_json) == {},
          "its row records nothing sent", row.payload_json)
    check(ns.methods() == ["GET", "PATCH", "PATCH", "PATCH"] and result.batch_requests == 0,
          "and no closing re-read: a value found then is not this tool's", str(ns.methods()))

    # The retry finds 50 on the line: refused as "already shows", NOT settled.
    retry = by_line(batch(engine, po, ns))["18"]
    check(retry.status == wp.REFUSED and "already shows 50" in retry.message,
          "a retry is refused, not ALREADY_PRESENT: a local failure is not evidence",
          retry.message[:120])

    # The per-line helper, the same way.
    engine, change, ns = world()
    ns.patch_failures = [(rex.ConnectTimeout("connect timed out"), False)] * 3
    result = write(engine, change, ns)
    check(result.status == wp.FAILED and "Nothing was sent to PO 1662 line 18" in result.message
          and json.loads(attempts(engine, change)[0].payload_json) == {},
          "per line: FAILED with nothing recorded as sent", result.message)


def test_the_batch_settles_an_already_present_line_without_sending_it() -> None:
    section("batch: a line already holding its proven value is settled, not resent")
    engine, po, changes, ns = po_world(ns_overrides={18: {"quantity": 50}})
    with engine.begin() as conn:
        conn.execute(proposed_changes.update().where(proposed_changes.c.id == changes[18])
                     .values(state=sc.STATE_WRITE_FAILED, quantity_write_status="FAILED"))
    insert_attempt(engine, changes[18], 1, "FAILED", {"quantity": 50}, "TRANSIENT")
    lines = by_line(batch(engine, po, ns))
    check(lines["18"].status == wp.ALREADY_PRESENT
          and ns.patches()[0][0] == {"item": {"items": [{"line": 19, "quantity": 9},
                                                        {"line": 20, "quantity": 40}]}},
          "line 18 settled as ALREADY_PRESENT and left out of the PATCH; 19 and 20 sent",
          json.dumps(ns.patches()[0][0]))
    check(lines["19"].status == lines["20"].status == wp.WRITTEN
          and attempts(engine, changes[18])[-1].outcome == "ALREADY_PRESENT",
          "the others written; line 18's row says ALREADY_PRESENT")


def test_nothing_is_sent_for_a_line_not_approved() -> None:
    section("database preconditions: refused before NetSuite is contacted")
    engine, change, ns = world(state=sc.STATE_PENDING_REVIEW, approved_quantity=None,
                               quantity_approved_by=None, quantity_approved_at=None,
                               quantity_write_status="NONE", human_verdict=None,
                               verdict_by=None, verdict_at=None)
    result = write(engine, change, ns)
    check(result.status == wp.REFUSED and "isn't approved for writing" in result.message
          and result.attempt_no is None, "an unapproved line is refused", result.message)
    check(ns.requests == [] and attempts(engine, change) == [],
          "NetSuite was not contacted and nothing was recorded")
    try:
        wp.write_approved_quantity(engine, "no-such-change", client=client_for(ns), now=NOW)
        check(False, "an unknown change id raises", "returned")
    except LookupError:
        check(True, "an unknown change id raises: that is a caller bug, not a refusal")


def test_the_quantity_only_guard_can_fail() -> None:
    section("the quantity-only guard refuses a payload carrying anything else")
    # Literal names, not the module's constants: renaming a constant must not
    # quietly change what is banned.
    banned = {"expectedReceiptDate", "custcol_override_expected_receipt",
              "custcol_sd_updatedreceiptdate", "custcol_sd_fg_excluderepspark"}
    check(wp.NEVER_SENT == banned and wp.SENT_FIELDS == {"quantity"},
          "quantity is the only field sent; the three date fields and excluderepspark are banned")
    for field in sorted(banned):
        try:
            wp._assert_quantity_only({"quantity": 50, field: "x"})
            check(False, f"{field} alongside quantity is refused", "accepted")
        except wp.WritePathViolation as exc:
            check(field in str(exc), f"{field} alongside quantity is refused")

    engine, change, ns = world()
    original = wp._quantity_payload
    wp._quantity_payload = lambda q: {"quantity": q, "expectedReceiptDate": "2026-12-01"}
    try:
        write(engine, change, ns)
        check(False, "a widened payload raises before sending", "returned")
    except wp.WritePathViolation:
        check("PATCH" not in ns.methods() and ns.lines[18]["quantity"] == 71,
              "a widened payload raises before anything is sent", str(ns.methods()))
    finally:
        wp._quantity_payload = original


class Interleaved(NetSuiteClient):
    """A real client that runs ANOTHER write of the same change at a chosen moment:
    inside its own re-read (`during="read"`) or straight after its PATCH lands
    (`during="patch"`). Deterministic stand-in for two tabs or a double click."""

    def __init__(self, ns, other, during):
        super().__init__(config=FAKE_CONFIG)
        self._session, self._access_token = ns, "fake-token"
        self._token_expires_at, self.TRANSIENT_BACKOFF_SECONDS = 9_999_999_999, 0
        self.other, self.during, self.other_result = other, during, None

    def get_purchase_order_lines_by_internal_id(self, internal_id, po_number=None):
        if self.during == "read" and self.other_result is None:
            self.other_result = self.other()
        return super().get_purchase_order_lines_by_internal_id(internal_id, po_number)

    def update_po_line(self, *args, **kwargs):
        sent = super().update_po_line(*args, **kwargs)
        if self.during == "patch" and self.other_result is None:
            self.other_result = self.other()
        return sent


def test_two_calls_on_one_line_never_lose_the_write() -> None:
    section("two calls racing on one change: the SUCCESS stands, whichever records first")
    import ingest as ing

    # B loads, then A writes completely before B re-reads; B is refused and records last.
    engine, change, ns = world()
    b_client = Interleaved(ns, lambda: write(engine, change, ns), during="read")
    b = guarded(wp.write_approved_quantity, engine, change, client=b_client, now=NOW)
    a = b_client.other_result
    row = change_row(engine, change)
    check(a.status == wp.WRITTEN and b.status == wp.REFUSED,
          "A writes; B, re-reading after A landed, is refused", f"{a.status} / {b.status}")
    check(row.state == sc.STATE_WRITTEN and row.quantity_write_status == "WRITTEN",
          "B's refusal does not overwrite A's WRITTEN", f"{row.state}/{row.quantity_write_status}")
    check([(x.attempt_no, x.outcome) for x in attempts(engine, change)]
          == [(1, "SUCCESS"), (2, "REFUSED")], "both attempts are kept as history")
    history = ing._line_history(engine, ["1662"]).get(("1662", "18"))
    check(history is not None and history.written_quantity == 50,
          "and the accumulation base still knows 50 was written",
          str(history and history.written_quantity))

    # A PATCHes; B runs completely (refused, recorded) before A records its SUCCESS.
    engine, change, ns = world()
    a_client = Interleaved(ns, lambda: write(engine, change, ns), during="patch")
    a = guarded(wp.write_approved_quantity, engine, change, client=a_client, now=NOW)
    b = a_client.other_result
    row = change_row(engine, change)
    check(b.status == wp.REFUSED and a.status == wp.WRITTEN,
          "B, between A's PATCH and A's record, is refused; A then records WRITTEN",
          f"{a.status} / {b.status}")
    check(row.state == sc.STATE_WRITTEN and row.quantity_write_status == "WRITTEN",
          "A's SUCCESS lands over B's earlier WRITE_FAILED", f"{row.state}")
    history = ing._line_history(engine, ["1662"]).get(("1662", "18"))
    check(history is not None and history.written_quantity == 50,
          "and the accumulation base knows 50 was written",
          str(history and history.written_quantity))
    check(ns.methods().count("PATCH") == 1, "one PATCH in all", str(ns.methods()))


def test_production_is_refused_unless_switched_on_for_that_account() -> None:
    section("sandbox first: a production account is refused before anything is sent")
    production = NetSuiteConfig(account_id="1321665", client_id="c", certificate_id="k",
                                private_key_path=Path("never-read.pem"))
    saved = os.environ.pop(wp.PRODUCTION_WRITES_ENV, None)
    try:
        engine, change, ns = world()
        client = client_for(ns)
        client.config = production
        result = guarded(wp.write_approved_quantity, engine, change, client=client, now=NOW)
        check(result.status == wp.REFUSED and "production" in result.message
              and ns.requests == [] and attempts(engine, change) == [],
              "refused, NetSuite not contacted, nothing recorded", result.message)

        os.environ[wp.PRODUCTION_WRITES_ENV] = "1321665-sb2"
        result = guarded(wp.write_approved_quantity, engine, change, client=client, now=NOW)
        check(result.status == wp.REFUSED and ns.requests == [],
              "the switch naming a DIFFERENT account does not open it")

        os.environ[wp.PRODUCTION_WRITES_ENV] = "1321665"
        result = guarded(wp.write_approved_quantity, engine, change, client=client, now=NOW)
        check(result.status == wp.WRITTEN,
              "the switch naming this production account does", result.message)

        mock = NetSuiteClient(mock_data={})
        engine, change, ns = world()
        result = guarded(wp.write_approved_quantity, engine, change, client=mock, now=NOW)
        check(result.status == wp.REFUSED and "no live NetSuite connection" in result.message,
              "a mock client is refused rather than tried", result.message)
    finally:
        os.environ.pop(wp.PRODUCTION_WRITES_ENV, None)
        if saved is not None:
            os.environ[wp.PRODUCTION_WRITES_ENV] = saved


def test_unexpected_errors_are_still_returned() -> None:
    section("an error outside NetSuite's types, or a failed record, still comes back as a result")
    engine, change, ns = world()
    real = ns.request

    def broken_patch(method, url, **kwargs):
        if method == "PATCH":
            raise ValueError("not a NetSuite error")
        return real(method, url, **kwargs)

    ns.request = broken_patch
    result = write(engine, change, ns)
    check(result.status == wp.FAILED and result.error_kind == "OTHER",
          "a non-NetSuite exception on the PATCH is FAILED/OTHER, not raised", result.message)
    check(attempts(engine, change)[0].outcome == "FAILED", "and it is recorded")

    engine, change, ns = world()
    original = wp._Attempt._insert

    def no_database(self, *args, **kwargs):
        raise RuntimeError("database went away")

    import logging

    class Collect(logging.Handler):
        def __init__(self):
            super().__init__(logging.ERROR)
            self.records = []

        def emit(self, record):
            self.records.append(record)

    collected = Collect()
    logging.getLogger("write_path").addHandler(collected)
    before_file = config.read_unrecorded_writes(UNRECORDED_FILE)
    before_seq = wp.unrecorded_seq()
    wp._Attempt._insert = no_database
    try:
        result = write(engine, change, ns, expect_unrecorded=True)
    finally:
        wp._Attempt._insert = original
        logging.getLogger("write_path").removeHandler(collected)
    check(result.status == wp.WRITTEN_NOT_RECORDED and ns.lines[18]["quantity"] == 50
          and "Don't try again" in result.message and result.attempt_no is None,
          "a write that landed but could not be recorded says exactly that", result.message)

    # ...and it is SURFACED, not only returned (Kiko, 2026-11-02).
    check(any(r.levelno >= logging.CRITICAL and "WRITE REACHED NETSUITE WITH NO RECORD"
              in r.getMessage() and r.exc_info for r in collected.records),
          "logged at CRITICAL, saying the write reached NetSuite with no record, with the "
          "traceback", str([r.getMessage()[:60] for r in collected.records]))
    entries = config.read_unrecorded_writes(UNRECORDED_FILE)[len(before_file):]
    check(len(entries) == 1 and entries[0]["reached_netsuite"] is True
          and entries[0]["change_id"] == change and entries[0]["po"] == "PO 1662"
          and entries[0]["line"] == "18" and entries[0]["sent"] == {"quantity": 50}
          and "database went away" in entries[0]["error"],
          "appended to the unrecorded-writes file with what was sent and why",
          json.dumps(entries)[:200])
    new = [f for f in wp.unrecorded_failures if f.seq > before_seq]
    check(len(new) == 1 and isinstance(new[0].error, RuntimeError)
          and new[0].outcome == "SUCCESS",
          "and kept in-process with the original exception, for the harness")

    # A write that MAY have landed (timed out, sent) whose FAILED row also cannot be
    # recorded: filed as "unknown" and alarmed at CRITICAL like a landed one. A
    # refusal (nothing sent) whose row cannot be recorded: filed as not reached.
    for label, prepare, want in (
            ("timed out after sending", lambda ns: ns.patch_failures.extend(
                [(Resp(503, {}), False)] * 3), "unknown"),
            ("refused by the re-read", lambda ns: ns.lines[18].update(
                isClosed=True, isOpen=False), False)):
        engine, change, ns = world()
        prepare(ns)
        collected = Collect()
        logging.getLogger("write_path").addHandler(collected)
        before = len(config.read_unrecorded_writes(UNRECORDED_FILE))
        wp._Attempt._insert = no_database
        try:
            write(engine, change, ns, expect_unrecorded=True)
        finally:
            wp._Attempt._insert = original
            logging.getLogger("write_path").removeHandler(collected)
        entry = config.read_unrecorded_writes(UNRECORDED_FILE)[before]
        level = max((r.levelno for r in collected.records), default=0)
        check(entry["reached_netsuite"] == want
              and level == (logging.CRITICAL if want else logging.ERROR),
              f"{label}: filed as reached_netsuite={want!r}, logged at "
              f"{logging.getLevelName(logging.CRITICAL if want else logging.ERROR)}",
              f"{entry['reached_netsuite']!r} / {logging.getLevelName(level)}")

    # Unexpected, the same failure is RAISED to the test -- so a dropped link inside
    # recording is a link error the harness can re-run, not a silent assertion.
    engine, change, ns = world()
    wp._Attempt._insert = no_database
    try:
        write(engine, change, ns)
        check(False, "an unexpected recording failure is re-raised by the guard", "returned")
    except RuntimeError as exc:
        check("database went away" in str(exc),
              "an unexpected recording failure is re-raised by the guard, unchanged")
    finally:
        wp._Attempt._insert = original

    # The case that hid the 2026-11-02 SQL Server failure: the link drops while the
    # attempt is being recorded. Re-raised, it is connection-class, so the harness
    # re-runs the test instead of reporting a changed result as an assertion.
    from sqlalchemy.exc import OperationalError

    class Dbapi(Exception):
        pass

    def link_drops(self, *args, **kwargs):
        raise OperationalError("INSERT INTO write_attempts", (),
                               Dbapi("08S01", "Communication link failure"))

    engine, change, ns = world()
    wp._Attempt._insert = link_drops
    try:
        write(engine, change, ns)
        check(False, "a link drop during recording reaches the test", "returned")
    except OperationalError as exc:
        check(dt_target.is_connection_class(exc),
              "a link drop during recording reaches the test as connection-class, so the "
              "harness re-runs it")
    finally:
        wp._Attempt._insert = original

    engine, change, ns = world(state=sc.STATE_WRITTEN)
    result = write(engine, change, ns)
    check(result.status == wp.REFUSED and ns.requests == [],
          "a WRITTEN row carrying an approved scope is refused, not failed into an "
          "illegal transition", result.message)


# ---------------------------------------------------------------------------
# the batch: one PATCH per PO
# ---------------------------------------------------------------------------


def line_400(line, detail="Error while accessing a resource. Please enter a value for Amount."):
    """A 400 naming one entry, shaped exactly as sandbox returned it on 2026-11-02."""
    return Resp(400, {"title": "Bad Request", "status": 400, "o:errorDetails": [{
        "detail": detail, "o:errorPath": f"item.items[?(@.line=={line})]",
        "o:errorCode": "USER_ERROR"}]})


#: Lines 18-20 approved (NetSuite 71/12/30 -> 50/9/40), 21 pending review (not
#: approved), and NetSuite line 22 that no change touches.
STANDARD = [(18, 71, 50), (19, 12, 9), (20, 30, 40)]


def po_world(spec=STANDARD, *, ns_overrides=None, pending_line=21, extra_ns=22,
             sqlite_file=None):
    """One PO with several approved changes. `spec` is (line, base, approved);
    NetSuite holds `base` on each unless `ns_overrides` says otherwise.
    `sqlite_file`: for a caller that reads it from another thread (the review app)."""
    engine = dt_target.fresh_engine(sqlite_file=sqlite_file)
    common = dict(state=sc.STATE_APPROVED, quantity_approved_by=REVIEWER,
                  quantity_approved_at=NOW, quantity_write_status="APPROVED",
                  human_verdict="ACCEPTED", verdict_by=REVIEWER, verdict_at=NOW,
                  accumulation_basis="FIRST_SHIPMENT")
    changes = {}
    with engine.begin() as conn:
        ship = mk_shipment(conn, message_id=mk_message(conn, "msg-batch"), primary_sha=None)
        po = mk_po(conn, ship, "PO NO : 1662", "1662")
        for i, (line, base, approved) in enumerate(spec):
            changes[line] = mk_change(conn, ship, po, size=f"s{i}", ns_line_id=str(line),
                                      current_quantity=base, proposed_quantity=approved,
                                      approved_quantity=approved, **common)
        if pending_line:
            changes[pending_line] = mk_change(conn, ship, po, size="pending",
                                              ns_line_id=str(pending_line),
                                              current_quantity=5, proposed_quantity=6)
    lines = []
    for line, base, _a in spec:
        over = dict((ns_overrides or {}).get(line, {}))
        lines.append(ns_line(line, over.pop("quantity", base), **over))
    if pending_line:
        lines.append(ns_line(pending_line, 5))
    if extra_ns:
        lines.append(ns_line(extra_ns, 7))
    return engine, po, changes, FakeNetSuite(lines)


def batch(engine, po, ns, client=None):
    return guarded(wp.write_po_batch, engine, po, client=client or client_for(ns), now=NOW)


def by_line(result):
    return {r.line: r for r in result.lines}


def test_a_clean_batch_is_one_patch_between_two_reads() -> None:
    section("batch: re-read, ONE PATCH with every approved line, re-read; a row per line")
    engine, po, changes, ns = po_world()
    result = batch(engine, po, ns)
    lines = by_line(result)

    check(ns.methods() == ["GET", "PATCH", "GET"],
          "one read, one PATCH, one closing read -- whatever the line count", str(ns.methods()))
    body, key = ns.patches()[0]
    check(body == {"item": {"items": [{"line": 18, "quantity": 50}, {"line": 19, "quantity": 9},
                                      {"line": 20, "quantity": 40}]}},
          "the PATCH carries the three approved lines' quantities and nothing else",
          json.dumps(body))
    check(set(lines) == {"18", "19", "20"} and all(r.status == wp.WRITTEN for r in lines.values()),
          "all three written; the pending line is not part of the write",
          str({k: r.status for k, r in lines.items()}))
    check(result.batch_requests == 1 and not result.fell_back_to_per_line,
          "one batch request, no fallback")
    check(lines["18"].message == "PO 1662 line 18: quantity 71 → 50.",
          "each line reads previous -> new", lines["18"].message)

    rows = {line: attempts(engine, cid) for line, cid in changes.items()}
    check(all(len(rows[line]) == 1 for line in (18, 19, 20)) and rows[21] == [],
          "write_attempts: one row per LINE, none for the line not approved")
    check({rows[line][0].idempotency_key for line in (18, 19, 20)} == {key},
          "every row of the batch carries the request's idempotency key")
    check([num(rows[line][0].previous_quantity) for line in (18, 19, 20)] == [71, 12, 30]
          and [json.loads(rows[line][0].payload_json) for line in (18, 19, 20)]
          == [{"quantity": 50}, {"quantity": 9}, {"quantity": 40}],
          "each row has its own previous value and exactly what was sent for it")
    check(all(change_row(engine, changes[line]).state == sc.STATE_WRITTEN for line in (18, 19, 20))
          and change_row(engine, changes[21]).state == sc.STATE_PENDING_REVIEW,
          "the three are WRITTEN; the pending line is untouched")
    check(ns.lines[21]["quantity"] == 5 and ns.lines[22]["quantity"] == 7,
          "NetSuite lines outside the batch are untouched")
    import ingest as ing
    history = ing._line_history(engine, ["1662"])
    check({ln: history[("1662", ln)].written_quantity for ln in ("18", "19", "20")}
          == {"18": 50, "19": 9, "20": 40},
          "the accumulation base knows all three writes")


def test_lines_the_reread_refuses_are_left_out_of_the_batch() -> None:
    section("batch: closed and edited lines are refused in the per-line words, the rest sent")
    engine, po, changes, ns = po_world(ns_overrides={
        19: {"isClosed": True, "isOpen": False}, 20: {"quantity": 31}})
    result = batch(engine, po, ns)
    lines = by_line(result)
    check(lines["19"].status == wp.REFUSED and lines["19"].message
          == "PO 1662 line 19 is closed in NetSuite, so nothing was written.",
          "a closed line: refused, same sentence as the per-line write", lines["19"].message)
    check(lines["20"].status == wp.REFUSED and "now shows 31" in lines["20"].message
          and "when it showed 30" in lines["20"].message,
          "an edited line: refused with both figures", lines["20"].message)
    check([b for b, _k in ns.patches()] == [{"item": {"items": [{"line": 18, "quantity": 50}]}}]
          and lines["18"].status == wp.WRITTEN,
          "only the remaining line is sent, and written")
    check(attempts(engine, changes[19])[0].outcome == "REFUSED"
          and attempts(engine, changes[19])[0].payload_json == "{}",
          "the refusal is recorded, with nothing sent")


def test_a_bad_line_is_dropped_and_the_batch_resent() -> None:
    section("batch: a 400 naming a line -> that line refused, the rest resent and written")
    engine, po, changes, ns = po_world()
    ns.patch_failures = [(line_400(19), False)]
    result = batch(engine, po, ns)
    lines = by_line(result)
    (first, first_key), (second, second_key) = ns.patches()
    check([e["line"] for e in second["item"]["items"]] == [18, 20] and first_key != second_key,
          "the resend carries the other two lines, under a new key",
          str([e["line"] for e in second["item"]["items"]]))
    check(lines["19"].status == wp.REFUSED and lines["19"].error_kind == "VALIDATION"
          and "Please enter a value for Amount." in lines["19"].message
          and "The rest of the PO was sent without it." in lines["19"].message,
          "the named line is refused, quoting NetSuite", lines["19"].message)
    row19 = attempts(engine, changes[19])[0]
    check(row19.outcome == "REFUSED" and row19.http_status == 400
          and json.loads(row19.payload_json) == {"quantity": 9}
          and row19.idempotency_key == first_key,
          "recorded REFUSED with the 400, what was sent for it, and the first request's key")
    check(lines["18"].status == lines["20"].status == wp.WRITTEN
          and attempts(engine, changes[18])[0].idempotency_key == second_key,
          "the other two are written, recorded under the resend's key")
    check(ns.lines[19]["quantity"] == 12 and result.batch_requests == 2,
          "line 19 untouched in NetSuite; two batch requests")
    check(change_row(engine, changes[19]).state == sc.STATE_WRITE_FAILED,
          "the refused line is WRITE_FAILED, retryable")


def test_the_batch_stops_after_three_requests() -> None:
    section("batch: three 400s each naming a line -> stop and report, no fourth request")
    engine, po, changes, ns = po_world(STANDARD + [(23, 8, 10)])
    ns.patch_failures = [(line_400(18), False), (line_400(19), False), (line_400(20), False)]
    result = batch(engine, po, ns)
    lines = by_line(result)
    check(ns.methods().count("PATCH") == wp.MAX_BATCH_REQUESTS == result.batch_requests == 3,
          "exactly three batch requests", str(ns.methods()))
    check(all(lines[ln].status == wp.REFUSED for ln in ("18", "19", "20")),
          "each named line is refused")
    check(lines["23"].status == wp.FAILED and "3 times" in lines["23"].message
          and "so the tool stopped" in lines["23"].message,
          "the line never named is reported as stopped, not written", lines["23"].message)
    check(all(ns.lines[ln]["quantity"] == base for ln, base, _a in STANDARD + [(23, 8, 10)])
          and ns.methods() == ["GET", "PATCH", "PATCH", "PATCH"],
          "nothing changed in NetSuite, and no closing read for a batch that applied nothing",
          str(ns.methods()))


def test_a_400_naming_no_line_falls_back_to_per_line_writes() -> None:
    section("batch: a 400 naming no line -> each line written on its own")
    engine, po, changes, ns = po_world()
    ns.patch_failures = [(Resp(400, {"o:errorDetails": [
        {"detail": "Invalid content.", "o:errorCode": "INVALID_CONTENT"}]}), False)]
    result = batch(engine, po, ns)
    lines = by_line(result)
    check(result.fell_back_to_per_line and result.batch_requests == 1,
          "one batch request, then the fallback")
    single = [b for b, _k in ns.patches()[1:]]
    check(single == [{"item": {"items": [{"line": ln, "quantity": q}]}}
                     for ln, q in ((18, 50), (19, 9), (20, 40))],
          "then one single-line PATCH per line, each after its own re-read",
          str(ns.methods()))
    check(all(r.status == wp.WRITTEN for r in lines.values())
          and all(len(attempts(engine, changes[ln])) == 1 for ln in (18, 19, 20)),
          "all written; one attempt row each -- the failed batch applied nothing and "
          "recorded nothing")


def test_a_timeout_that_landed_is_recorded_as_written() -> None:
    section("batch: the PATCH times out but landed -> the closing read decides: WRITTEN")
    import requests.exceptions as rex

    engine, po, changes, ns = po_world()
    ns.patch_failures = [(rex.ReadTimeout("read timed out"), True)] * 3
    result = batch(engine, po, ns)
    lines = by_line(result)
    keys = {k for _b, k in ns.patches()}
    check(len(ns.patches()) == 3 and len(keys) == 1,
          "the client retried twice, reusing the idempotency key", f"{len(ns.patches())} tries")
    check(all(r.status == wp.WRITTEN for r in lines.values())
          and "didn't confirm the write, but the PO now shows it" in lines["18"].message,
          "every line that landed is WRITTEN, saying the confirmation never came",
          lines["18"].message)
    check(attempts(engine, changes[18])[0].http_status is None,
          "no HTTP status recorded: none was received")
    import ingest as ing
    check(ing._line_history(engine, ["1662"])[("1662", "18")].written_quantity == 50,
          "and the accumulation base counts it")

    engine, po, changes, ns = po_world()
    ns.patch_failures = [(rex.ReadTimeout("read timed out"), False)] * 3
    lines = by_line(batch(engine, po, ns))
    check(all(r.status == wp.FAILED and r.error_kind == "TRANSIENT" for r in lines.values())
          and "still shows 71" not in lines["18"].message
          and "now shows 71, not 50" in lines["18"].message
          and "Trying again is safe." in lines["18"].message,
          "a timeout that did NOT land: FAILED, with what the line shows", lines["18"].message)


def test_the_closing_read_disagreeing_with_what_was_sent() -> None:
    section("batch: 204, but the closing read shows a line unchanged -> that line FAILED")
    engine, po, changes, ns = po_world()
    ns.patch_failures = [(Resp(204), {18, 20})]  # 204 for all; line 19 silently not applied
    result = batch(engine, po, ns)
    lines = by_line(result)
    check(lines["18"].status == lines["20"].status == wp.WRITTEN,
          "the lines that landed are written")
    check(lines["19"].status == wp.FAILED and lines["19"].error_kind == "OTHER"
          and "accepted the write to PO 1662 line 19, but the line now shows 12, not 9"
          in lines["19"].message,
          "the one that did not is FAILED, the 204 notwithstanding", lines["19"].message)
    row19 = attempts(engine, changes[19])[0]
    check(row19.outcome == "FAILED" and row19.http_status == 204,
          "recorded FAILED, with the 204 that was received")
    import ingest as ing
    history = ing._line_history(engine, ["1662"])
    check(history[("1662", "19")].written_quantity is None
          and history[("1662", "18")].written_quantity == 50,
          "no write is claimed for line 19 in the accumulation base")


def test_a_400_naming_several_lines_drops_them_all_at_once() -> None:
    section("batch: one 400 naming two lines -> both dropped in one step, one resend")
    engine, po, changes, ns = po_world()
    two = Resp(400, {"o:errorDetails": [
        {"detail": "Bad 18.", "o:errorPath": "item.items[?(@.line==18)]", "o:errorCode": "X"},
        {"detail": "Bad 20.", "o:errorPath": "item.items[?(@.line==20)]", "o:errorCode": "X"}]})
    ns.patch_failures = [(two, False)]
    result = batch(engine, po, ns)
    lines = by_line(result)
    check(result.batch_requests == 2
          and ns.patches()[1][0] == {"item": {"items": [{"line": 19, "quantity": 9}]}},
          "both named lines dropped together; the resend carries only line 19")
    check(lines["18"].status == lines["20"].status == wp.REFUSED
          and "Bad 18." in lines["18"].message and "Bad 20." in lines["20"].message,
          "each refused line quotes NetSuite's sentence about IT",
          f"{lines['18'].message} | {lines['20'].message}")
    check(lines["19"].status == wp.WRITTEN, "and the remaining line is written")

    engine, po, changes, ns = po_world([(18, 71, 50)], pending_line=None)
    ns.patch_failures = [(line_400(18), False)]
    result = batch(engine, po, ns)
    check(ns.methods() == ["GET", "PATCH"] and by_line(result)["18"].status == wp.REFUSED,
          "a batch emptied by drops makes no closing read", str(ns.methods()))


def test_the_closing_read_is_retried_before_giving_up() -> None:
    section("batch: the closing read fails once -> tried again; fails throughout -> FAILED")
    down = [Resp(503, {"title": "Service Unavailable"})] * 3  # one failed client call
    engine, po, changes, ns = po_world()
    ns.get_failures_after_patch = list(down)
    lines = by_line(batch(engine, po, ns))
    check(all(r.status == wp.WRITTEN for r in lines.values()),
          "a closing read that fails once is tried again, and the writes are confirmed")

    engine, po, changes, ns = po_world()
    ns.get_failures_after_patch = list(down) * wp.CLOSING_READ_ATTEMPTS
    lines = by_line(batch(engine, po, ns))
    check(all(r.status == wp.FAILED and r.error_kind == "TRANSIENT" for r in lines.values())
          and "couldn't re-read the PO to see whether it landed" in lines["18"].message
          and "Check the line in NetSuite before trying again." in lines["18"].message,
          "a closing read that never answers: FAILED, not inferred from the 204, and the "
          "message says to check first", lines["18"].message)
    check(ns.lines[18]["quantity"] == 50,
          "(the write did land -- the known dead end the RUNBOOK names)")


def test_batch_refusals_before_netsuite() -> None:
    section("batch: permission, production and an empty PO")
    engine, po, changes, ns = po_world()
    ns.patch_failures = [(Resp(400, PERMISSION_DENIED), False)]
    lines = by_line(batch(engine, po, ns))
    check(all(r.status == wp.FAILED and r.error_kind == "PERMISSION" for r in lines.values())
          and ns.methods().count("PATCH") == 1,
          "a permission refusal fails every line, with no resend and no fallback")

    engine, po, changes, ns = po_world()
    client = client_for(ns)
    client.config = NetSuiteConfig(account_id="1321665", client_id="c", certificate_id="k",
                                   private_key_path=Path("never-read.pem"))
    result = batch(engine, po, ns, client=client)
    check(all(r.status == wp.REFUSED and "production" in r.message for r in result.lines)
          and ns.requests == [] and all(attempts(engine, c) == [] for c in changes.values()),
          "production is refused before any request, nothing recorded")

    engine, po, changes, ns = po_world([], pending_line=21)
    result = batch(engine, po, ns)
    check(result.lines == () and ns.requests == [],
          "a PO with nothing approved writes nothing and contacts no one")


def test_the_outcome_vocabulary_is_enforced() -> None:
    section("write_attempts.outcome: SUCCESS, FAILED, REFUSED and nothing else")
    engine, change, _ns = world()
    row = {"change_id": change, "scope": "QUANTITY", "ns_internal_id": PO_ID,
           "ns_line_id": "18", "payload_json": "{}", "idempotency_key": "k",
           "attempted_at": NOW}
    with engine.begin() as conn:
        conn.execute(write_attempts.insert(), {**row, "id": sc.new_id(), "attempt_no": 1,
                                               "outcome": "REFUSED", "previous_quantity": 7})
    check(True, "REFUSED is accepted, with a previous_quantity")
    try:
        with engine.begin() as conn:
            conn.execute(write_attempts.insert(), {**row, "id": sc.new_id(), "attempt_no": 2,
                                                   "outcome": "SKIPPED"})
        check(False, "an outcome outside the vocabulary is rejected", "accepted")
    except IntegrityError:
        check(True, "an outcome outside the vocabulary is rejected by the database")


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
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
