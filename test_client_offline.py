"""
Offline tests for netsuite_client.py -- everything that can be proven without
live NetSuite credentials.

What this DOES prove:
  - the JWT client assertion is genuinely valid: it verifies against the real
    certificate generate_m2m_keypair.py produced, with the right iss/aud/scope
    and the certificate id in the `kid` header
  - REST payload mapping: a realistic purchaseOrder item line maps to POLine
    correctly, including the style-key fallback chain and refName extraction
  - the update payload shape matches what the live sandbox test confirmed
    (item.items[] targeted by `line`), and never contains a `replace` parameter
  - field validation: aliases resolve, dates/bools coerce, unknown fields are
    REJECTED rather than silently dropped
  - 403 / INSUFFICIENT_PERMISSION responses become NetSuitePermissionError, so
    the Phase 1 test can distinguish a permission finding from a bad request
  - mock mode still behaves exactly as the old stub did, and refuses writes

What this does NOT prove -- and no offline test can:
  - that NetSuite accepts our assertion (needs a real Integration record)
  - that the least-privilege "PO Update" role is permitted to write those four
    fields. That is the whole point of test_phase1_writeback.py and the only
    thing that closes Phase 1.

Run: .venv\\Scripts\\python.exe test_client_offline.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import time
import traceback
from pathlib import Path

import netsuite_client as nc
from netsuite_client import (
    NetSuiteClient,
    NetSuiteConfig,
    NetSuiteError,
    NetSuiteConfigError,
    NetSuitePermissionError,
    POLine,
    normalize_line_fields,
)

CERT_PATH = Path.home() / ".po-agent" / "keys" / "netsuite_m2m_cert.pem"
KEY_PATH = Path.home() / ".po-agent" / "keys" / "netsuite_m2m_private.pem"

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    _results.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  --  {detail}" if detail else ""))


def expect_raises(exc_type, fn, name: str) -> None:
    try:
        fn()
    except exc_type as exc:
        check(True, name, f"raised {exc_type.__name__}: {str(exc).splitlines()[0][:70]}")
    except Exception as exc:  # wrong exception type is a failure
        check(False, name, f"raised {type(exc).__name__} instead of {exc_type.__name__}: {exc}")
    else:
        check(False, name, "did not raise")


def section(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 70 - len(title)))


# ---------------------------------------------------------------------------

FAKE_CONFIG = NetSuiteConfig(
    account_id="1321665-sb2",
    client_id="fake_consumer_key_for_offline_tests",
    certificate_id="FAKE_CERT_ID_123",
    private_key_path=KEY_PATH,
)

# A realistic purchaseOrder item line, shaped like what the live sandbox test
# saw: reference fields as {id, refName}, dates as ISO strings.
SAMPLE_LINE = {
    "line": 18,
    "item": {"id": "41231", "refName": "M120246 : M120246-Waterman Polo-TID-3X"},
    "quantity": 2,
    "units": {"id": "1", "refName": "Ea"},
    "expectedReceiptDate": "2026-07-15",
    "custcol_override_expected_receipt": False,
    "custcol_sd_updatedreceiptdate": None,
    "custcol_sd_tmpl_style": "M120246",
    "custcol_cmo_parentitem": {"id": "40011", "refName": "M120246"},
    "custcol_product_color": {"id": "12", "refName": "TID"},
    "custcol_product_size": {"id": "6", "refName": "3X"},
    "isClosed": False,
    "isOpen": True,
    "quantityReceived": 0,
    "quantityBilled": 0,
    "rate": 18.75,
}


def test_config_derivation() -> None:
    section("config / URL derivation")
    check(FAKE_CONFIG.host == "1321665-sb2.suitetalk.api.netsuite.com", "sandbox host derived", FAKE_CONFIG.host)
    check(FAKE_CONFIG.is_sandbox, "sandbox detected from account id")
    check(
        FAKE_CONFIG.token_url.endswith("/services/rest/auth/oauth2/v1/token"),
        "token endpoint path",
        FAKE_CONFIG.token_url,
    )
    check(FAKE_CONFIG.record_base.endswith("/services/rest/record/v1"), "record API base", FAKE_CONFIG.record_base)

    # Underscore-style account ids must normalize for DNS.
    underscored = NetSuiteConfig("1321665_SB2", "c", "k", KEY_PATH)
    check(underscored.host.startswith("1321665-sb2."), "underscore account id normalized for DNS", underscored.host)
    check(underscored.is_sandbox, "sandbox detected from underscore form")

    prod = NetSuiteConfig("1321665", "c", "k", KEY_PATH)
    check(not prod.is_sandbox, "production account id NOT flagged sandbox", prod.account_id)

    # from_env() with nothing configured must name every missing variable at
    # once. Clear any inherited env vars so this doesn't pass/fail by accident.
    import os

    saved = {k: os.environ.pop(k, None) for k in ("NS_ACCOUNT_ID", "NS_CLIENT_ID", "NS_CERTIFICATE_ID", "NS_PRIVATE_KEY_PATH")}
    try:
        expect_raises(
            NetSuiteConfigError,
            lambda: NetSuiteConfig.from_env(dotenv_path="__no_such_file__.env"),
            "from_env() with no credentials raises NetSuiteConfigError",
        )
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value

    # A configured-but-missing private key must also fail before any HTTP call.
    saved_path = os.environ.pop("NS_PRIVATE_KEY_PATH", None)
    os.environ.update(NS_ACCOUNT_ID="1321665-sb2", NS_CLIENT_ID="x", NS_CERTIFICATE_ID="y")
    os.environ["NS_PRIVATE_KEY_PATH"] = "does_not_exist.pem"
    try:
        expect_raises(
            NetSuiteConfigError,
            lambda: NetSuiteConfig.from_env(dotenv_path="__no_such_file__.env"),
            "from_env() with a missing private key file raises NetSuiteConfigError",
        )
    finally:
        for key in ("NS_ACCOUNT_ID", "NS_CLIENT_ID", "NS_CERTIFICATE_ID"):
            os.environ.pop(key, None)
        os.environ.pop("NS_PRIVATE_KEY_PATH", None)
        if saved_path is not None:
            os.environ["NS_PRIVATE_KEY_PATH"] = saved_path


def test_jwt_assertion() -> None:
    section("JWT client assertion (the thing that replaces a browser login)")
    if not KEY_PATH.exists() or not CERT_PATH.exists():
        check(False, "keypair present", f"run generate_m2m_keypair.py first ({KEY_PATH})")
        return

    import jwt
    from cryptography import x509

    client = NetSuiteClient(config=FAKE_CONFIG)
    assertion = client._build_client_assertion(FAKE_CONFIG)

    headers = jwt.get_unverified_header(assertion)
    check(headers.get("alg") == "PS256", "header alg", str(headers.get("alg")))
    check(headers.get("typ") == "JWT", "header typ", str(headers.get("typ")))
    check(
        headers.get("kid") == FAKE_CONFIG.certificate_id,
        "header kid is the NetSuite certificate id",
        str(headers.get("kid")),
    )

    # The real proof: verify the signature against the certificate that will be
    # uploaded to NetSuite. If this passes, NetSuite can verify it too.
    cert = x509.load_pem_x509_certificate(CERT_PATH.read_bytes())
    claims = jwt.decode(
        assertion,
        cert.public_key(),
        algorithms=["PS256"],
        audience=FAKE_CONFIG.token_url,
        options={"verify_aud": True},
    )
    check(True, "signature verifies against the uploadable certificate")
    check(claims["iss"] == FAKE_CONFIG.client_id, "iss is the client id", claims["iss"])
    check(claims["aud"] == FAKE_CONFIG.token_url, "aud is exactly the token endpoint", claims["aud"])
    check(claims["scope"] == ["rest_webservices"], "scope is rest_webservices", str(claims["scope"]))
    lifetime = claims["exp"] - claims["iat"]
    check(0 < lifetime <= 3600, "assertion lifetime within NetSuite's 60-minute cap", f"{lifetime}s")

    rs256 = NetSuiteConfig(
        FAKE_CONFIG.account_id, FAKE_CONFIG.client_id, FAKE_CONFIG.certificate_id, KEY_PATH, algorithm="RS256"
    )
    alt = NetSuiteClient(config=rs256)._build_client_assertion(rs256)
    jwt.decode(alt, cert.public_key(), algorithms=["RS256"], audience=rs256.token_url)
    check(True, "RS256 fallback algorithm also produces a verifiable assertion")


def test_line_mapping() -> None:
    section("REST payload -> POLine mapping")
    client = NetSuiteClient(config=FAKE_CONFIG)
    line = client._map_line(SAMPLE_LINE, "8489541", "Inprotex")

    check(line.line_id == "18", "line_id from `line`", line.line_id)
    check(line.line_number == 18, "line_number is an int for the API", str(line.line_number))
    check(line.style_number == "M120246", "style from custcol_sd_tmpl_style", line.style_number)
    check(line.color == "TID", "color from custcol_product_color.refName", line.color)
    check(line.size == "3X", "size from custcol_product_size.refName (canonical, not XXXL)", line.size)
    check(line.quantity == 2, "quantity", str(line.quantity))
    check(line.expected_receipt_date == dt.date(2026, 7, 15), "expectedReceiptDate parsed", str(line.expected_receipt_date))
    check(line.override_expected_receipt is False, "override flag parsed")
    check(line.updated_receipt_date is None, "null updatedReceiptDate -> None")
    check(line.units == "Ea", "units refName", line.units)
    check(line.vendor_name == "Inprotex", "vendor from header entity", str(line.vendor_name))
    check(line.item_internal_id == "41231", "item internal id captured", str(line.item_internal_id))
    check(line.po_internal_id == "8489541", "po internal id captured", str(line.po_internal_id))
    check(line.raw is SAMPLE_LINE, "raw payload retained for debugging")

    # Style fallback chain: custcol_sd_tmpl_style -> parent item -> display name.
    no_style = {**SAMPLE_LINE}
    del no_style["custcol_sd_tmpl_style"]
    check(
        client._map_line(no_style, "1", None).style_number == "M120246",
        "style falls back to custcol_cmo_parentitem.refName",
    )

    no_parent = {**no_style}
    del no_parent["custcol_cmo_parentitem"]
    check(
        client._map_line(no_parent, "1", None).style_number == "M120246",
        "style falls back to display-name prefix as last resort",
    )

    # A closed line must be visible so the test can refuse to write to it.
    check(client._map_line({**SAMPLE_LINE, "isClosed": True}, "1", None).closed, "closed line detected")

    # isOpen decides whether a line can still be updated, and is NOT the
    # complement of isClosed: a Fully Billed PO has lines with both False.
    check(line.is_open, "isOpen mapped")
    check(client._map_line({**SAMPLE_LINE, "isOpen": False}, "1", None).is_open is False,
          "a not-open line is visible as such")
    both_false = client._map_line({**SAMPLE_LINE, "isOpen": False, "isClosed": False}, "1", None)
    check(both_false.is_open is False and both_false.closed is False,
          "neither open NOR closed is representable -- reading isClosed as 'not open' "
          "already produced one wrong conclusion")
    check(client._map_line({k: v for k, v in SAMPLE_LINE.items() if k != "isOpen"}, "1", None).is_open is False,
          "a MISSING isOpen reads as not-open, so an odd payload flags rather than writes blind")

    # Receipt/billing figures and price: surfaced so a human can tell duplicate
    # PO lines apart. Never used to choose between them automatically.
    check(line.quantity_received == 0.0 and line.quantity_billed == 0.0,
          "quantityReceived/quantityBilled mapped",
          f"{line.quantity_received}/{line.quantity_billed}")
    check(line.rate == 18.75, "rate mapped", str(line.rate))
    partly = client._map_line({**SAMPLE_LINE, "quantityReceived": "100", "rate": None}, "1", None)
    check(partly.quantity_received == 100.0, "numeric strings coerce", str(partly.quantity_received))
    check(partly.rate is None, "and a null stays None rather than becoming 0.0", str(partly.rate))
    check(client._map_line({**SAMPLE_LINE, "quantityBilled": "n/a"}, "1", None).quantity_billed is None,
          "an unparseable number degrades to None, doesn't crash the read")

    # A malformed date must not take down the whole PO read.
    check(
        client._map_line({**SAMPLE_LINE, "expectedReceiptDate": "not-a-date"}, "1", None).expected_receipt_date is None,
        "unparseable date degrades to None with a warning, doesn't crash the read",
    )


def test_field_normalization() -> None:
    section("field normalization / validation")
    out = normalize_line_fields(
        {
            "quantity": 99,
            "expected_receipt_date": dt.date(2026, 6, 27),
            "override_expected_receipt": True,
            "updated_receipt_date": "2026-06-27",
        }
    )
    check(set(out) == set(nc.WRITABLE_LINE_FIELDS), "snake_case aliases resolve to NetSuite names", ", ".join(out))
    check(out[nc.NS_EXPECTED_RECEIPT_DATE] == "2026-06-27", "date object -> ISO string", out[nc.NS_EXPECTED_RECEIPT_DATE])
    check(out[nc.NS_UPDATED_RECEIPT_DATE] == "2026-06-27", "ISO string passes through validated")
    check(out[nc.NS_OVERRIDE_EXPECTED_RECEIPT] is True, "bool preserved")
    check(out[nc.NS_QUANTITY] == 99 and isinstance(out[nc.NS_QUANTITY], int), "quantity is an int")

    # Raw NetSuite names must work too, for callers that already speak NetSuite.
    raw = normalize_line_fields({nc.NS_QUANTITY: 5, nc.NS_OVERRIDE_EXPECTED_RECEIPT: "true"})
    check(raw[nc.NS_QUANTITY] == 5, "raw NetSuite field names accepted")
    check(raw[nc.NS_OVERRIDE_EXPECTED_RECEIPT] is True, "'true' string coerced to bool")

    check(normalize_line_fields({"updated_receipt_date": None})[nc.NS_UPDATED_RECEIPT_DATE] is None,
          "None clears a date field (matches NetSuite's null)")

    expect_raises(NetSuiteError, lambda: normalize_line_fields({}), "empty field dict rejected")
    expect_raises(
        NetSuiteError,
        lambda: normalize_line_fields({"quantitiy": 5}),
        "typo'd field name REJECTED, not silently dropped",
    )
    expect_raises(
        NetSuiteError,
        lambda: normalize_line_fields({"rate": 12.50}),
        "field outside the validated four rejected",
    )
    expect_raises(
        NetSuiteError,
        lambda: normalize_line_fields({"expected_receipt_date": "06/27/2026"}),
        "non-ISO date rejected before it reaches NetSuite",
    )
    expect_raises(
        NetSuiteError, lambda: normalize_line_fields({"quantity": None}), "None quantity rejected"
    )


def test_update_payload_shape() -> None:
    section("PATCH payload shape (must match the confirmed live test)")
    captured: dict = {}

    class FakeResponse:
        status_code = 204
        text = ""

        def json(self):
            return {}

    client = NetSuiteClient(config=FAKE_CONFIG)

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return FakeResponse()

    client._request = fake_request  # bypass HTTP only; payload construction is real

    result = client.update_po_line(
        "8489541",
        18,
        {
            "quantity": 99,
            "expected_receipt_date": dt.date(2026, 6, 27),
            "override_expected_receipt": True,
            "updated_receipt_date": dt.date(2026, 6, 27),
        },
        by_internal_id=True,
    )

    body = captured["json"]
    check(captured["method"] == "PATCH", "uses PATCH", captured["method"])
    check(captured["url"].endswith("/record/v1/purchaseOrder/8489541"), "targets the PO record", captured["url"])
    check(list(body) == ["item"], "body has a single `item` key", str(list(body)))
    check(len(body["item"]["items"]) == 1, "exactly one sublist line sent")

    sent = body["item"]["items"][0]
    check(sent["line"] == 18 and isinstance(sent["line"], int), "line targeted by integer `line` number", repr(sent["line"]))
    check(
        all(f in sent for f in nc.WRITABLE_LINE_FIELDS),
        "all four confirmed fields in ONE call",
        ", ".join(k for k in sent if k != "line"),
    )

    # The dangerous parameter. `replace=item` would blow away every other line
    # on the PO instead of merging this one.
    params = captured.get("params") or {}
    check("replace" not in params and "replace" not in captured["url"], "NO `replace` parameter sent (would wipe other PO lines)")
    check("X-NetSuite-Idempotency-Key" in captured.get("headers", {}), "idempotency key sent")
    check(result["ok"] and result["line"] == 18, "result summary returned", json.dumps(result["sent"]))

    expect_raises(
        NetSuiteError,
        lambda: client.update_po_line("8489541", 18, {"amount": 1}, by_internal_id=True),
        "unsupported field rejected before any HTTP call",
    )


def test_error_translation() -> None:
    section("error translation (permission finding vs. bad request)")

    class Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    forbidden = Resp(
        403,
        {
            "type": "https://www.netsuite.com/help/errors",
            "title": "Permission Violation",
            "o:errorDetails": [
                {
                    "detail": "You do not have permissions to set a value for element custcol_override_expected_receipt.",
                    "o:errorCode": "INSUFFICIENT_PERMISSION",
                }
            ],
        },
    )
    try:
        nc._raise_for_response(forbidden, "PATCH", "/purchaseOrder/8489541")
        check(False, "403 raises NetSuitePermissionError", "nothing raised")
    except NetSuitePermissionError as exc:
        check(True, "403 raises NetSuitePermissionError")
        check("custcol_override_expected_receipt" in str(exc), "message names the offending custom field")
        check("REAL FINDING" in str(exc), "message says it's a finding, not something to route around")
    except Exception as exc:
        check(False, "403 raises NetSuitePermissionError", f"raised {type(exc).__name__}")

    # A 400 that is really a permission problem in disguise must still classify
    # as one -- NetSuite is not consistent about which status it uses.
    disguised = Resp(400, {"o:errorDetails": [{"detail": "USER_ERROR: You do not have privileges to edit this record."}]})
    expect_raises(
        NetSuitePermissionError,
        lambda: nc._raise_for_response(disguised, "PATCH", "/x"),
        "permission wording inside a 400 still classifies as a permission finding",
    )

    # The exact wording the sandbox actually returned on 2026-08-04. An earlier
    # marker list missed it, so a real permission refusal was misreported as a
    # generic API error (exit 1 instead of exit 3). Regression test.
    observed = Resp(
        400,
        {
            "o:errorDetails": [
                {
                    "detail": "Your current role does not have permission to perform this action.",
                    "o:errorCode": "USER_ERROR",
                }
            ]
        },
    )
    expect_raises(
        NetSuitePermissionError,
        lambda: nc._raise_for_response(observed, "GET", "/purchaseOrder"),
        "real sandbox wording 'current role does not have permission' classifies as a finding",
    )

    bad_request = Resp(400, {"o:errorDetails": [{"detail": "Invalid date value.", "o:errorCode": "INVALID_FLD_VALUE"}]})
    expect_raises(
        nc.NetSuiteAPIError,
        lambda: nc._raise_for_response(bad_request, "PATCH", "/x"),
        "genuine bad request stays a NetSuiteAPIError",
    )

    token_error = Resp(400, {"error": "invalid_client", "error_description": "invalid_client"})
    msg = nc._explain_token_failure(token_error, FAKE_CONFIG)
    check("NS_CERTIFICATE_ID" in msg, "invalid_client explanation points at the certificate id")


def test_transient_retry() -> None:
    section("transient retry: timeouts/5xx retried, 4xx never")
    import requests.exceptions as rex

    class Resp:
        def __init__(self, status, payload=None):
            self.status_code = status
            self._payload = payload or {}
            self.text = json.dumps(self._payload)

        def json(self):
            return self._payload

    class FakeSession:
        """Replays a scripted sequence of outcomes and counts attempts."""

        def __init__(self, outcomes):
            self.outcomes = list(outcomes)
            self.calls = 0

        def request(self, method, url, **kwargs):
            self.calls += 1
            if not self.outcomes:
                raise AssertionError("session called more times than scripted")
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    def client_with(outcomes):
        client = NetSuiteClient(config=FAKE_CONFIG)
        client._access_token = "fake-token"
        client._token_expires_at = time.time() + 3600
        client._session = FakeSession(outcomes)
        client.TRANSIENT_BACKOFF_SECONDS = 0  # keep the suite fast
        return client

    # 1. A timeout that then succeeds.
    client = client_with([rex.ConnectTimeout("timed out"), Resp(200, {"ok": True})])
    resp = client._request("GET", "https://example/x")
    check(resp.status_code == 200, "recovers after a timeout", f"{client._session.calls} attempts")
    check(client._session.calls == 2, "took exactly 2 attempts", str(client._session.calls))

    # 2. A 500 that then succeeds.
    client = client_with([Resp(500, {"title": "Internal Error"}), Resp(200, {})])
    client._request("GET", "https://example/x")
    check(client._session.calls == 2, "retries a 500 and recovers", str(client._session.calls))

    # 3. Persistent failure -> NetSuiteTransientError after the budget, not a generic error.
    client = client_with([Resp(503, {"title": "Service Unavailable"})] * 3)
    try:
        client._request("GET", "https://example/x")
        check(False, "exhausted retries raise NetSuiteTransientError", "nothing raised")
    except nc.NetSuiteTransientError as exc:
        check(True, "exhausted retries raise NetSuiteTransientError")
        check(client._session.calls == 3, "used the full 3-attempt budget", str(client._session.calls))
        check(exc.attempts == 3 and exc.last_status == 503, "carries attempts + last status",
              f"attempts={exc.attempts} status={exc.last_status}")
        check("unreachable or failing server-side" in str(exc),
              "message distinguishes an outage from a bad request")
    except Exception as exc:  # noqa: BLE001
        check(False, "exhausted retries raise NetSuiteTransientError", f"raised {type(exc).__name__}")

    # 4. Persistent timeouts -> same, and NOT a bare requests exception.
    client = client_with([rex.ReadTimeout("t")] * 3)
    expect_raises(
        nc.NetSuiteTransientError,
        lambda: client._request("GET", "https://example/x"),
        "persistent timeouts raise NetSuiteTransientError",
    )

    # 5. THE IMPORTANT HALF: 4xx must fail immediately, with no retry.
    client = client_with([Resp(403, {"o:errorDetails": [{"detail": "Permission Violation"}]})])
    expect_raises(
        NetSuitePermissionError,
        lambda: client._request("GET", "https://example/x"),
        "403 raises a permission error immediately",
    )
    check(client._session.calls == 1, "403 was NOT retried", f"{client._session.calls} attempt(s)")

    # A permission denial disguised as a 400 must also fail fast -- retrying it
    # would be the worst case, since it's the error that most needs to surface.
    client = client_with([
        Resp(400, {"o:errorDetails": [{"detail": "Your current role does not have permission to perform this action."}]})
    ])
    expect_raises(
        NetSuitePermissionError,
        lambda: client._request("GET", "https://example/x"),
        "disguised-400 permission error raises immediately",
    )
    check(client._session.calls == 1, "disguised 400 was NOT retried", f"{client._session.calls} attempt(s)")

    client = client_with([Resp(400, {"o:errorDetails": [{"detail": "Invalid date value."}]})])
    expect_raises(
        nc.NetSuiteAPIError,
        lambda: client._request("GET", "https://example/x"),
        "ordinary 400 raises immediately",
    )
    check(client._session.calls == 1, "ordinary 400 was NOT retried", f"{client._session.calls} attempt(s)")

    # 6. Non-transient network exceptions propagate rather than being retried.
    client = client_with([rex.SSLError("certificate verify failed")])
    expect_raises(
        rex.SSLError,
        lambda: client._request("GET", "https://example/x"),
        "an SSL error propagates (config problem, not a blip)",
    )
    check(client._session.calls == 1, "SSL error was NOT retried", f"{client._session.calls} attempt(s)")

    check(nc._is_transient_exception(rex.ConnectTimeout("x")), "timeout classified transient")
    check(not nc._is_transient_exception(ValueError("x")), "ValueError not classified transient")


def test_mock_mode_unchanged() -> None:
    section("mock mode (demo_matcher.py must keep working)")
    mock_line = POLine(
        line_id="101",
        item="M120246 : M120246-Waterman Polo-TID-S",
        style_number="M120246",
        vendor_name=None,
        color="TID",
        size="S",
        quantity=12,
        units="Ea",
        expected_receipt_date=dt.date(2026, 7, 6),
        override_expected_receipt=True,
        updated_receipt_date=dt.date(2026, 7, 6),
    )
    client = NetSuiteClient(mock_data={"1662": [mock_line]})

    check(client.is_mock, "mock mode when constructed with mock_data")
    check(client.account_id == "1321665-sb2", "legacy default account id preserved", client.account_id)
    check(len(client.get_purchase_order("1662")) == 1, "mock read returns injected lines")
    check(client.get_purchase_order("9999") == [], "unknown PO returns [] (drives NEEDS_ATTENTION path)")
    check(
        POLine(
            line_id="1", item="i", style_number="s", vendor_name=None, color="c", size="z",
            quantity=1, units="Ea", expected_receipt_date=None, override_expected_receipt=False,
            updated_receipt_date=None,
        ).closed is False,
        "POLine constructible with the original positional/keyword signature",
    )

    expect_raises(
        NetSuiteConfigError,
        lambda: client.update_po_line("1662", "101", {"quantity": 5}),
        "mock client REFUSES to write (no accidental writes from demo code)",
    )
    expect_raises(
        NetSuiteConfigError, lambda: client.authenticate(), "mock client refuses to authenticate"
    )

    # matcher.py must still import and run against the mock client.
    from matcher import build_proposed_changes

    changes = build_proposed_changes(
        [{"po_number": "1662", "style_number": "M120246", "color": "TID", "size": "S", "quantity": 9}],
        client,
        eta="2026/6/27 16:45",
    )
    check(len(changes) == 1 and changes[0].status == "PENDING_REVIEW", "matcher.py still produces a diff", changes[0].status)
    check(changes[0].current_quantity == 12 and changes[0].proposed_quantity == 9, "diff values correct", "12 -> 9")

    unmatched = build_proposed_changes(
        [{"po_number": "1662", "style_number": "M120246", "color": "TID", "size": "XXXL", "quantity": 4}],
        client,
        eta="2026/6/27 16:45",
    )
    check(unmatched[0].status == "NEEDS_ATTENTION", "unmatched line still NEEDS_ATTENTION, never dropped")


def main() -> int:
    print("=" * 78)
    print("OFFLINE TESTS -- netsuite_client.py")
    print("=" * 78)
    print()
    print("Proves everything except the live NetSuite round-trip. Phase 1 is NOT")
    print("complete until test_phase1_writeback.py passes with real credentials.")

    for test in (
        test_config_derivation,
        test_jwt_assertion,
        test_line_mapping,
        test_field_normalization,
        test_update_payload_shape,
        test_error_translation,
        test_transient_retry,
        test_mock_mode_unchanged,
    ):
        try:
            test()
        except Exception:
            print()
            traceback.print_exc()
            _results.append((False, f"{test.__name__} crashed", ""))

    passed = sum(1 for ok, _, _ in _results if ok)
    failed = [name for ok, name, _ in _results if not ok]

    print()
    print("=" * 78)
    print(f"{passed}/{len(_results)} checks passed")
    if failed:
        print()
        for name in failed:
            print(f"  FAILED: {name}")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
