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
import traceback
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import dialect_target as dt_target
import schema as sc
import write_path as wp
from netsuite_client import NetSuiteClient, NetSuiteConfig
from schema import audit_log, proposed_changes, write_attempts
from test_schema import mk_change, mk_message, mk_po, mk_shipment

#: UTC, stored naive -- the database's convention.
NOW = dt.datetime(2026, 11, 2, 9, 0, 0, tzinfo=dt.timezone.utc).replace(tzinfo=None)
REVIEWER = "Paula Test"
PO_ID = "8489541"
_results: list[tuple[bool, str, str]] = []


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
    what a timeout after the request reached NetSuite looks like from here."""

    def __init__(self, lines):
        self.lines = {int(ln["line"]): dict(ln) for ln in lines}
        self.requests: list[tuple[str, str, dict]] = []
        self.get_failures: list = []
        self.patch_failures: list = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if method == "GET":
            if self.get_failures:
                return self.get_failures.pop(0)
            items = [dict(v) for _, v in sorted(self.lines.items())]
            return Resp(200, {"id": PO_ID, "tranId": "PO0001662",
                              "entity": {"id": "7", "refName": "Inprotex"},
                              "item": {"items": items, "totalResults": len(items)}})
        if method == "PATCH":
            failure = self.patch_failures.pop(0) if self.patch_failures else None
            if failure is None or failure[1]:
                for sent in kwargs["json"]["item"]["items"]:
                    self.lines[sent["line"]].update(
                        {k: v for k, v in sent.items() if k != "line"})
            return failure[0] if failure else Resp(204)
        raise AssertionError(f"unexpected {method} {url}")

    def methods(self):
        return [m for m, _u, _k in self.requests]


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


def write(engine, change, ns):
    return wp.write_approved_quantity(engine, change, client=client_for(ns), now=NOW)


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
    section("a receipt date approved alongside: quantity still goes alone")
    engine, change, ns = world(confirmed_receipt_date=dt.date(2026, 12, 1),
                               date_approved_by=REVIEWER, date_approved_at=NOW,
                               date_write_status="APPROVED")
    result = write(engine, change, ns)
    sent = ns.requests[1][2]["json"]["item"]["items"][0]
    check(result.status == wp.WRITTEN and set(sent) == {"line", "quantity"},
          "the PATCH still carries only line and quantity", str(sorted(sent)))
    check(ns.lines[18]["expectedReceiptDate"] == "2026-07-15",
          "expectedReceiptDate in NetSuite is unchanged")
    check("receipt date approved with it was not written" in result.message,
          "the result says the date was not written", result.message)
    row = change_row(engine, change)
    check(row.quantity_write_status == "WRITTEN" and row.date_write_status == "APPROVED"
          and row.state == sc.STATE_APPROVED,
          "quantity WRITTEN, date scope still APPROVED, so the change is not WRITTEN",
          f"{row.state} q={row.quantity_write_status} d={row.date_write_status}")

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
    result = wp.write_approved_quantity(engine, change, client=client_for(ns), now=later)
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
    check(second.status == wp.REFUSED and "already shows 50" in second.message
          and ns.methods().count("PATCH") == patches,
          "the retry sees 50, refuses with that explanation, and sends no second PATCH",
          second.message)
    import ingest as ing
    history = ing._line_history(engine, ["1662"]).get(("1662", "18"))
    check(history is not None and history.written_quantity is None,
          "and no SUCCESS is claimed, so the next slip is flagged rather than summed")


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
    b = wp.write_approved_quantity(engine, change, client=b_client, now=NOW)
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
    a = wp.write_approved_quantity(engine, change, client=a_client, now=NOW)
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
    import os

    production = NetSuiteConfig(account_id="1321665", client_id="c", certificate_id="k",
                                private_key_path=Path("never-read.pem"))
    saved = os.environ.pop(wp.PRODUCTION_WRITES_ENV, None)
    try:
        engine, change, ns = world()
        client = client_for(ns)
        client.config = production
        result = wp.write_approved_quantity(engine, change, client=client, now=NOW)
        check(result.status == wp.REFUSED and "production" in result.message
              and ns.requests == [] and attempts(engine, change) == [],
              "refused, NetSuite not contacted, nothing recorded", result.message)

        os.environ[wp.PRODUCTION_WRITES_ENV] = "1321665-sb2"
        result = wp.write_approved_quantity(engine, change, client=client, now=NOW)
        check(result.status == wp.REFUSED and ns.requests == [],
              "the switch naming a DIFFERENT account does not open it")

        os.environ[wp.PRODUCTION_WRITES_ENV] = "1321665"
        result = wp.write_approved_quantity(engine, change, client=client, now=NOW)
        check(result.status == wp.WRITTEN,
              "the switch naming this production account does", result.message)

        mock = NetSuiteClient(mock_data={})
        engine, change, ns = world()
        result = wp.write_approved_quantity(engine, change, client=mock, now=NOW)
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

    wp._Attempt._insert = no_database
    try:
        result = write(engine, change, ns)
    finally:
        wp._Attempt._insert = original
    check(result.status == wp.WRITTEN_NOT_RECORDED and ns.lines[18]["quantity"] == 50
          and "Don't try again" in result.message and result.attempt_no is None,
          "a write that landed but could not be recorded says exactly that", result.message)

    engine, change, ns = world(state=sc.STATE_WRITTEN)
    result = write(engine, change, ns)
    check(result.status == wp.REFUSED and ns.requests == [],
          "a WRITTEN row carrying an approved scope is refused, not failed into an "
          "illegal transition", result.message)


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
