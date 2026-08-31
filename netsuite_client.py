"""
NetSuite REST client, authenticated via OAuth 2.0 Client Credentials
(Machine-to-Machine) using a signed JWT assertion -- no browser login anywhere.

See PO-Update-Automation-Architecture.md section 6 for why this grant type and
not the interactive Authorization Code Grant that Cowork's connector uses.

Confirmed live against NetSuite sandbox (PO 8489541 / PO# 1662, 2026-08-04) --
these are measured facts, not assumptions:
  - Standard REST Record API supports direct writes to `quantity`,
    `expectedReceiptDate`, `custcol_override_expected_receipt`, and
    `custcol_sd_updatedreceiptdate` on the item sublist. No RESTlet needed.
    Target a line via its `line` number inside `item.items[]`.
  - Style-color-size is one child Item record per SKU (matrixType: "CHILD"),
    not a matrix item with variants.
  - The exact-match style key is `custcol_sd_tmpl_style` (plain string, e.g.
    "M120246"). `custcol_cmo_parentitem.refName` is an equally-exact alternate.
    Both beat substring-parsing the Item display name.
  - Color/size are reference fields: `custcol_product_color.refName` (e.g.
    "TID") and `custcol_product_size.refName` ("S", "2X", "3X" -- NetSuite's
    canonical labels; see matcher.py SIZE_ALIASES).

*** NOT YET CONFIRMED -- the open validation step this module exists to close ***
The live write test above ran under the CFO role (a temporary workaround to
unblock Cowork's connector, see CLAUDE.md blocker #1). It proves the REST API
*mechanically* supports these sublist writes. It does NOT prove the
least-privilege "PO Update" role (Purchase Order: Edit, Items: View, Vendors:
View, REST Web Services: Full) can do the same -- NetSuite custom fields can
carry field-level access restrictions independent of record-level Edit
permission. `test_phase1_writeback.py` closes that gap, and deliberately
distinguishes a hard permission error from the nastier failure mode where
NetSuite accepts the PATCH (204) but silently discards a restricted field.

Mock mode is preserved: NetSuiteClient(mock_data={...}) still works exactly as
the old stub did, so demo_matcher.py keeps running without live credentials.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Field names -- confirmed live, see module docstring
# ---------------------------------------------------------------------------

NS_QUANTITY = "quantity"
NS_EXPECTED_RECEIPT_DATE = "expectedReceiptDate"
NS_OVERRIDE_EXPECTED_RECEIPT = "custcol_override_expected_receipt"
NS_UPDATED_RECEIPT_DATE = "custcol_sd_updatedreceiptdate"

NS_STYLE = "custcol_sd_tmpl_style"
NS_PARENT_ITEM = "custcol_cmo_parentitem"
NS_COLOR = "custcol_product_color"
NS_SIZE = "custcol_product_size"

#: The child item field holding the long-form colour name ("New Indigo" for code
#: "NIN"). The colour LIST does not carry it: in this account
#: `customlist_psgss_product_color/334` returns name='NIN', abbreviation='NIN',
#: and the identical REST call against the size list returns name='ALL',
#: abbreviation='A' -- so both fields are exposed and the colour data really is
#: code-in-Name. A UI export from another account state does have curated long
#: names in that column, but building against it would mean colour tests that pass
#: in production and fail in sandbox. This field is verified in the account we test
#: against: populated on 2,390 of the 2,393 distinct items on open POs (the other
#: three are poly mailers -- packaging, with no colour by nature).
ITEM_COLOUR_NAME_FIELD = "custitem_psgss_product_color_desc"

#: PO transaction numbering, from Setup > Company > Auto-Generated Numbers
#: (Purchase Order row): Prefix "PO", Minimum Digits 7, Current Number 1777.
#: So a printed 1662 is stored as PO0001662.
#:
#: **Validated against the data, not trusted from the setup screen**, because Allow
#: Override and Use Subsidiary / Use Location were not captured -- and a checkbox
#: says what is permitted, not what happened. All **1,659 of 1,659** PO tranIds in
#: the account match `^PO\d{7}$`: one single shape, spanning 2021-06-16 to
#: 2026-07-31, no duplicate numbers, and `"PO" + zfill(7)` reproduces every one of
#: them exactly. So override is off or unused, and there is no subsidiary or
#: location variation in practice. Re-run the same check against production before
#: cutover -- the two accounts have already been shown to differ elsewhere.
TRANID_PREFIX = "PO"
TRANID_MIN_DIGITS = 7
TRANID_PATTERN = re.compile(r"^PO\d{7}$")

#: The four writable target fields, in the order the docs list them.
WRITABLE_LINE_FIELDS = (
    NS_QUANTITY,
    NS_EXPECTED_RECEIPT_DATE,
    NS_OVERRIDE_EXPECTED_RECEIPT,
    NS_UPDATED_RECEIPT_DATE,
)

#: Accept the pipeline's snake_case names as well as raw NetSuite field names,
#: so callers (matcher.py, the future approval handler) don't have to know
#: NetSuite's spelling. Unknown keys are rejected loudly rather than dropped.
_FIELD_ALIASES = {
    "quantity": NS_QUANTITY,
    "expected_receipt_date": NS_EXPECTED_RECEIPT_DATE,
    "override_expected_receipt": NS_OVERRIDE_EXPECTED_RECEIPT,
    "updated_receipt_date": NS_UPDATED_RECEIPT_DATE,
}

_DATE_FIELDS = {NS_EXPECTED_RECEIPT_DATE, NS_UPDATED_RECEIPT_DATE}
_BOOL_FIELDS = {NS_OVERRIDE_EXPECTED_RECEIPT}
_NUMBER_FIELDS = {NS_QUANTITY}


# ---------------------------------------------------------------------------
# Errors -- typed so callers can tell "no permission" from "bad request" from
# "NetSuite is down". Phase 3's write-back needs that distinction to decide
# whether to surface a config problem or a transient retry.
# ---------------------------------------------------------------------------


class NetSuiteError(Exception):
    """Base for every NetSuite failure."""


class NetSuiteConfigError(NetSuiteError):
    """Missing/contradictory local configuration -- never reached NetSuite."""


class NetSuiteAuthError(NetSuiteError):
    """Token acquisition failed (bad cert/key/client id, expired certificate)."""


class NetSuitePermissionError(NetSuiteError):
    """
    NetSuite authenticated us but refused the operation (403 / INSUFFICIENT_PERMISSION).

    For Phase 1 this is the specific finding we're hunting: it would mean the
    "PO Update" role can't do what the CFO role could.
    """


class PONumberUnresolvable(NetSuiteError):
    """
    A printed PO reference could not be turned into a usable tranId, or the tranId
    it produced does not exist.

    A **defined outcome**, not a bug: the caller records it against the shipment's
    PO row (`resolution_status = 'NOT_FOUND'`) and flags. Carries `printed` and
    `attempted` so a reviewer sees both what the vendor wrote and what was looked
    up -- without those two strings side by side, "PO not found" is unactionable.

    Never followed by a second format guess or a fuzzy search. If the derived
    tranId is absent, the answer is that a human looks, not that the tool tries
    another shape.
    """

    def __init__(self, message: str, printed: str = "", attempted: str = ""):
        super().__init__(message)
        self.printed = printed
        self.attempted = attempted


class SublistTruncated(NetSuiteError):
    """
    A PO's item sublist came back shorter than NetSuite says it is.

    The failure this prevents is silent and expensive: a 1,200-line PO yields the
    first 1,000 lines, proposals are staged for those, Paula approves them, and the
    remaining 200 never appear anywhere -- no error, no flag, no row. Nothing
    downstream could notice, because a short list is indistinguishable from a short
    PO.

    So a mismatch is fatal to the read rather than a warning. A partial PO is worse
    than no PO: no PO is visibly missing, while a partial one looks complete.
    """


class NetSuiteTransientError(NetSuiteError):
    """
    NetSuite was unreachable or failing server-side, and retries were exhausted.

    Deliberately distinguishable from every other failure so callers and the audit
    log can tell "NetSuite was genuinely down" apart from "the request was wrong".
    A transient failure is worth retrying later; a 4xx never is.
    """

    def __init__(self, message: str, attempts: int = 0, last_status: Optional[int] = None):
        super().__init__(message)
        self.attempts = attempts
        self.last_status = last_status


class NetSuiteAPIError(NetSuiteError):
    """Any other non-2xx response."""

    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class POLine:
    """
    One PO item-sublist line.

    Field names/order are unchanged from the original stub so existing callers
    (matcher.py, demo_matcher.py) keep working -- new fields are appended with
    defaults only.
    """

    line_id: str  # NetSuite sublist `line` number as a string -- targets the update
    item: str  # e.g. "M120246 : M120246-Waterman Polo-TID-S" (display name -- cross-check/logging only)
    style_number: str  # exact match key from custcol_sd_tmpl_style / custcol_cmo_parentitem.refName
    vendor_name: Optional[str]
    color: str  # custcol_product_color.refName
    size: str  # custcol_product_size.refName -- NetSuite canonical labels (2X/3X, not XXL/XXXL)
    quantity: int
    units: str
    expected_receipt_date: Optional[dt.date]
    override_expected_receipt: bool
    updated_receipt_date: Optional[dt.date]
    closed: bool = False

    # --- appended in the live implementation ---
    po_internal_id: Optional[str] = None  # NetSuite record id (e.g. "8489541")
    item_internal_id: Optional[str] = None  # Item record id, for future use
    raw: dict = field(default_factory=dict)  # untouched REST payload, for debugging

    #: NetSuite's per-line `isOpen` — whether the line can still be updated.
    #: **This is NOT the complement of `closed`.** A line can be neither open nor
    #: closed: on a Fully Billed PO every line has isClosed=False (nobody ticks the
    #: per-line Closed box) AND isOpen=False (nothing is outstanding). Use
    #: `is_open` to decide whether a line can still receive an update, and `closed`
    #: only for the deliberate-close check. Reading `isClosed` as "not open"
    #: already produced one wrong conclusion on this data.
    #:
    #: Defaults True so a hand-built line (mocks, demos) is a live line unless it
    #: says otherwise. The live path never reaches the default — `_map_line` always
    #: sets it, and does so conservatively: a missing `isOpen` reads as False, so
    #: an unexpected payload flags for review rather than writing blind. NetSuite
    #: returned it on all 367 lines of the 25 most recent sandbox POs.
    is_open: bool = True

    #: Receipt/billing progress and price. Carried so a human resolving an
    #: ambiguous match can see which line is actually being received against.
    quantity_received: Optional[float] = None
    quantity_billed: Optional[float] = None
    rate: Optional[float] = None

    @property
    def line_number(self) -> int:
        """The sublist line as the integer NetSuite's API actually wants."""
        return int(self.line_id)


@dataclass
class NetSuiteConfig:
    """
    Everything needed to authenticate. All of it comes from the NetSuite UI
    setup in NETSUITE-M2M-SETUP.md; none of it is guessable.
    """

    account_id: str  # e.g. "1321665-sb2"
    client_id: str  # Integration record's Consumer Key / Client ID
    certificate_id: str  # from the OAuth 2.0 Client Credentials (M2M) Setup page -> JWT `kid`
    private_key_path: Path  # the key generate_m2m_keypair.py made (NOT the certificate)
    algorithm: str = "PS256"  # NetSuite accepts PS256/RS256 for RSA
    private_key_passphrase: Optional[str] = None
    timeout: int = 60

    @property
    def is_sandbox(self) -> bool:
        """Sandbox account ids carry an -sbN / _SBN suffix."""
        normalized = self.account_id.lower().replace("_", "-")
        return "-sb" in normalized

    @property
    def host(self) -> str:
        """
        NetSuite's REST host. Account ids are lowercased and underscores become
        hyphens for DNS (e.g. 1321665_SB2 -> 1321665-sb2).
        """
        return f"{self.account_id.lower().replace('_', '-')}.suitetalk.api.netsuite.com"

    @property
    def token_url(self) -> str:
        return f"https://{self.host}/services/rest/auth/oauth2/v1/token"

    @property
    def record_base(self) -> str:
        return f"https://{self.host}/services/rest/record/v1"

    @property
    def suiteql_url(self) -> str:
        """
        SuiteQL endpoint. NOT used by `resolve_po_internal_id` -- the SuiteQL
        fallback was removed once the `?q=` path was confirmed working. Kept
        because SuiteQL is available to this role (the same SuiteAnalytics
        Workbook permission gates it) and may be useful for future reporting reads.

        If you do use it: NetSuite's SuiteQL REST endpoint does NOT support
        parameter binding -- sending {"q": "... = ?", "params": [...]} returns
        400 INVALID_CONTENT. That mistake is what made the old fallback fail in a
        misleading way. Do not work around it with string interpolation.
        """
        return f"https://{self.host}/services/rest/query/v1/suiteql"

    @classmethod
    def from_env(cls, dotenv_path: Union[str, Path, None] = ".env") -> "NetSuiteConfig":
        """
        Load from environment / .env. Raises NetSuiteConfigError listing every
        missing variable at once rather than failing one at a time.
        """
        try:
            from dotenv import load_dotenv

            if dotenv_path and Path(dotenv_path).exists():
                load_dotenv(dotenv_path)
        except ImportError:  # dotenv is convenience, not a requirement
            pass

        required = {
            "NS_ACCOUNT_ID": os.environ.get("NS_ACCOUNT_ID"),
            "NS_CLIENT_ID": os.environ.get("NS_CLIENT_ID"),
            "NS_CERTIFICATE_ID": os.environ.get("NS_CERTIFICATE_ID"),
            "NS_PRIVATE_KEY_PATH": os.environ.get("NS_PRIVATE_KEY_PATH"),
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise NetSuiteConfigError(
                "Missing required configuration: "
                + ", ".join(missing)
                + "\n\nCopy .env.example to .env and fill in the values NetSuite gave you "
                "(see NETSUITE-M2M-SETUP.md)."
            )

        key_path = Path(os.path.expandvars(os.path.expanduser(required["NS_PRIVATE_KEY_PATH"])))
        if not key_path.exists():
            raise NetSuiteConfigError(
                f"Private key not found at {key_path}.\n"
                "Run generate_m2m_keypair.py, or point NS_PRIVATE_KEY_PATH at the existing key."
            )

        return cls(
            account_id=required["NS_ACCOUNT_ID"],
            client_id=required["NS_CLIENT_ID"],
            certificate_id=required["NS_CERTIFICATE_ID"],
            private_key_path=key_path,
            algorithm=os.environ.get("NS_JWT_ALGORITHM", "PS256"),
            private_key_passphrase=os.environ.get("NS_PRIVATE_KEY_PASSPHRASE") or None,
            timeout=int(os.environ.get("NS_HTTP_TIMEOUT", "60")),
        )


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def _to_iso_date(value: Any, field_name: str) -> Optional[str]:
    """NetSuite wants 'YYYY-MM-DD'. Validate strictly -- a malformed date that
    reaches NetSuite comes back as an opaque error far from its cause."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip()).isoformat()
        except ValueError as exc:
            raise NetSuiteError(f"{field_name}: {value!r} is not an ISO date (YYYY-MM-DD)") from exc
    raise NetSuiteError(f"{field_name}: expected a date or ISO date string, got {type(value).__name__}")


def _parse_ns_date(value: Any) -> Optional[dt.date]:
    """Read NetSuite's date back. It returns 'YYYY-MM-DD', but tolerate a
    datetime string rather than crashing the whole PO read on one odd line."""
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    for candidate in (text, text.split("T")[0], text.split(" ")[0]):
        try:
            return dt.date.fromisoformat(candidate)
        except ValueError:
            continue
    logger.warning("Could not parse NetSuite date %r; treating as empty", value)
    return None


def _ref_name(value: Any) -> str:
    """Pull refName off a NetSuite reference field ({id, refName}) safely."""
    if isinstance(value, dict):
        return str(value.get("refName") or value.get("id") or "").strip()
    return str(value or "").strip()


def _ref_id(value: Any) -> Optional[str]:
    if isinstance(value, dict) and value.get("id") is not None:
        return str(value["id"])
    return None


def _to_number(value: Any) -> Optional[float]:
    """NetSuite returns these as numbers or numeric strings; None stays None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "t", "yes", "y", "1"}
    return bool(value)


def _assert_sublist_complete(sublist: Any, internal_id: str) -> None:
    """
    Refuse a PO whose item sublist is shorter than NetSuite says it is.

    The sublist reports `totalResults` but **no `hasMore` and no `offset`**, so
    counting is the only truncation signal available. Verified on this account:
    the largest PO has 380 lines (`PO0001497`) and none has 1,000 or more, so this
    raises on nothing today -- it exists for the PO that eventually does.

    Why fatal rather than a warning: a truncated read produces proposals for the
    lines it saw and silence for the rest. Paula approves what she is shown, the
    missing lines never surface, and no error is ever emitted. A partial PO looks
    complete, which is precisely what makes it worse than a failed read.

    A missing or non-numeric `totalResults` is NOT treated as a mismatch -- absent
    metadata is not evidence of truncation, and guessing would turn a guard into a
    source of false failures.
    """
    if not isinstance(sublist, dict):
        return
    total = sublist.get("totalResults")
    if not isinstance(total, int):
        return
    got = len(sublist.get("items") or [])
    if got < total:
        raise SublistTruncated(
            f"PO internal id {internal_id}: NetSuite reports {total} item lines but the "
            f"read returned {got}. Refusing the record rather than staging proposals for "
            f"{got} of {total} lines -- the missing ones would never appear anywhere, with "
            "no error and no flag. The sublist exposes no hasMore/offset, so pagination "
            "has to be added here (see RUNBOOK section 7) before a PO this size can be "
            "processed."
        )


def po_tranid(printed: str) -> str:
    """
    Turn what a vendor printed into NetSuite's tranId: `1662` -> `PO0001662`.

    Handles every rendering seen across the real corpus -- `PO#1662`,
    `PO NO : 1720`, `PO NO. : 1721`, a bare `1720` in a table cell -- by taking the
    digits and applying the account's numbering rule (`TRANID_PREFIX`,
    `TRANID_MIN_DIGITS`). A value that is already a tranId passes through unchanged,
    so this is idempotent and safe to apply twice.

    **Why this cannot be a fuzzy or multi-format search.** Querying the bare number
    returns HTTP 200 with `totalResults=0` -- it executes correctly and matches
    nothing, which a naive caller reads as "PO not found" rather than "you asked the
    wrong question". So the transformation has to be right the first time, and a
    miss has to be reported rather than retried in another shape.

    Raises `PONumberUnresolvable` when the input carries no digits, or carries more
    than one distinct number. The second case matters: a filename like
    `#1720, 1721` names two POs, and silently taking the first would attach a whole
    shipment to the wrong order. Splitting that is the extractor's job -- each
    extracted line carries its own `po_number` -- so this refuses rather than
    guessing.

    **This only ever receives a value the extractor identified as a PO reference.**
    That boundary is load-bearing: a bare four-digit number cannot be found by a
    page-wide regex, because carton counts, quantities and style-number fragments
    look identical, and recognising `1720` as a PO needs the column-header context
    (`PO NO`). Nothing here can recover from being handed a carton count, so it does
    not try to; `assert_po_reference` states the contract.
    """
    text = str(printed or "").strip()
    if TRANID_PATTERN.match(text):
        return text

    groups = re.findall(r"\d+", text)
    if not groups:
        raise PONumberUnresolvable(
            f"PO reference {printed!r} contains no digits, so no tranId can be derived. "
            "Expected something like '1662', 'PO#1662' or 'PO0001662'.",
            printed=text,
        )
    distinct = {str(int(g)) for g in groups}
    if len(distinct) > 1:
        raise PONumberUnresolvable(
            f"PO reference {printed!r} contains {len(distinct)} different numbers "
            f"({', '.join(sorted(distinct))}). That names more than one PO, and picking "
            "one would attach the shipment to the wrong order. Split it upstream: each "
            "extracted line carries its own po_number.",
            printed=text,
        )

    return f"{TRANID_PREFIX}{int(groups[0]):0{TRANID_MIN_DIGITS}d}"


def assert_po_reference(printed: str) -> str:
    """
    The contract on `po_tranid`'s input: a value the EXTRACTOR called a PO reference.

    Returns it unchanged. Exists to make the boundary explicit and greppable rather
    than implied by a comment. Resolving a PO number is a lookup problem; *recognising*
    a bare `1720` as one -- against carton counts and quantities that look identical --
    is an extraction problem, solved with column-header context this layer does not
    have. If this ever fires, the fix is upstream.
    """
    text = str(printed or "").strip()
    if not text:
        raise PONumberUnresolvable(
            "an empty PO reference reached the resolver. The extractor should never emit "
            "a line with no po_number, and the matcher already flags one that does.",
            printed="",
        )
    return text


def normalize_line_fields(fields: dict) -> dict:
    """
    Translate a caller's field dict into a NetSuite sublist patch body.

    Accepts snake_case pipeline names or raw NetSuite names; coerces dates,
    booleans and numbers; rejects anything unrecognized. Rejecting rather than
    ignoring is deliberate -- a typo'd field name that silently no-ops would
    look like a successful write that changed nothing, which is exactly the
    failure mode this project can least afford.
    """
    if not fields:
        raise NetSuiteError("No fields supplied -- refusing to send an empty update.")

    out: dict[str, Any] = {}
    for key, value in fields.items():
        ns_key = _FIELD_ALIASES.get(key, key)
        if ns_key not in WRITABLE_LINE_FIELDS:
            raise NetSuiteError(
                f"Unsupported field {key!r} (resolved to {ns_key!r}). "
                f"Phase 1 supports exactly: {', '.join(WRITABLE_LINE_FIELDS)} "
                f"(or their aliases: {', '.join(_FIELD_ALIASES)}). "
                "Widening this set means re-validating role permissions -- see architecture doc section 6."
            )
        if ns_key in _DATE_FIELDS:
            out[ns_key] = _to_iso_date(value, ns_key)
        elif ns_key in _BOOL_FIELDS:
            out[ns_key] = _to_bool(value)
        elif ns_key in _NUMBER_FIELDS:
            if value is None:
                raise NetSuiteError(f"{ns_key}: refusing to write None as a quantity.")
            out[ns_key] = int(value) if float(value).is_integer() else float(value)
        else:  # unreachable given the guard above; kept explicit
            out[ns_key] = value
    return out


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class NetSuiteClient:
    """
    Live NetSuite REST client (M2M/JWT), with the old stub's mock mode intact.

    Live:  NetSuiteClient(config=NetSuiteConfig.from_env())
    Mock:  NetSuiteClient(mock_data={"1662": [POLine(...), ...]})
    """

    #: Leeway before a token's stated expiry at which we proactively re-mint.
    TOKEN_REFRESH_SKEW_SECONDS = 120

    #: Total attempts for a transient failure (timeout / connection error / 5xx).
    #: Small on purpose: this pipeline is not latency-critical, but a long retry
    #: loop would delay a human noticing a genuine outage.
    MAX_TRANSIENT_ATTEMPTS = 3

    #: Base backoff; doubles per attempt (0.5s, then 1.0s with 3 attempts).
    TRANSIENT_BACKOFF_SECONDS = 0.5

    def __init__(
        self,
        account_id: Optional[str] = None,
        mock_data: Optional[dict] = None,
        config: Optional[NetSuiteConfig] = None,
    ):
        self.config = config
        self.account_id = config.account_id if config else (account_id or "1321665-sb2")
        self._mock_data = mock_data or {}
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._po_id_cache: dict[str, str] = {}
        self._session = None
        #: Which PO-lookup strategy actually worked. We can't test the `q=`
        #: quoting rules without live credentials, so record the answer from the
        #: first real run instead of guessing in a comment.
        self.last_lookup_strategy: Optional[str] = None
        #: The tranId the last resolve actually queried, for showing beside the
        #: vendor's printed value.
        self.last_resolved_tranid: Optional[str] = None

    # -- mode ---------------------------------------------------------------

    @property
    def is_mock(self) -> bool:
        return self.config is None

    def _require_live(self, operation: str) -> NetSuiteConfig:
        if self.config is None:
            raise NetSuiteConfigError(
                f"{operation} requires live credentials, but this client is in mock mode. "
                "Construct it with config=NetSuiteConfig.from_env()."
            )
        return self.config

    # -- HTTP plumbing ------------------------------------------------------

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _build_client_assertion(self, config: NetSuiteConfig) -> str:
        """
        Build the signed JWT that stands in for an interactive login.

        NetSuite verifies this against the certificate uploaded in its UI; the
        `kid` header tells it which certificate to use. `aud` must be exactly
        the token endpoint URL or NetSuite rejects the assertion.
        """
        import jwt
        from cryptography.hazmat.primitives import serialization

        key_bytes = config.private_key_path.read_bytes()
        password = config.private_key_passphrase.encode() if config.private_key_passphrase else None
        try:
            private_key = serialization.load_pem_private_key(key_bytes, password=password)
        except (TypeError, ValueError) as exc:
            hint = (
                " The key looks encrypted -- set NS_PRIVATE_KEY_PASSPHRASE."
                if password is None
                else " Check NS_PRIVATE_KEY_PASSPHRASE."
            )
            raise NetSuiteConfigError(f"Could not load private key {config.private_key_path}: {exc}.{hint}") from exc

        now = int(time.time())
        payload = {
            "iss": config.client_id,
            "scope": ["rest_webservices"],
            "aud": config.token_url,
            "iat": now,
            "exp": now + 3000,  # NetSuite caps assertion lifetime at 60 minutes
        }
        headers = {"typ": "JWT", "alg": config.algorithm, "kid": config.certificate_id}
        return jwt.encode(payload, private_key, algorithm=config.algorithm, headers=headers)

    def authenticate(self, force: bool = False) -> str:
        """
        Get a bearer token, minting a new one only when needed.

        No browser, no refresh token, no re-authorization -- which is the whole
        reason for choosing this grant type (architecture doc section 6).
        """
        config = self._require_live("authenticate()")
        if not force and self._access_token and time.time() < self._token_expires_at - self.TOKEN_REFRESH_SKEW_SECONDS:
            return self._access_token

        assertion = self._build_client_assertion(config)
        logger.info("Requesting NetSuite access token (M2M/JWT, alg=%s)", config.algorithm)

        try:
            response = self.session.post(
                config.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                    "client_assertion": assertion,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=config.timeout,
            )
        except Exception as exc:  # network-level
            raise NetSuiteAuthError(f"Could not reach NetSuite token endpoint {config.token_url}: {exc}") from exc

        if response.status_code != 200:
            raise NetSuiteAuthError(_explain_token_failure(response, config))

        body = response.json()
        token = body.get("access_token")
        if not token:
            raise NetSuiteAuthError(f"Token endpoint returned 200 but no access_token: {body}")

        self._access_token = token
        self._token_expires_at = time.time() + int(body.get("expires_in", 3600))
        logger.info("Got access token, valid ~%ss", body.get("expires_in", "?"))
        return token

    def _request(self, method: str, url: str, *, retry_auth: bool = True, **kwargs):
        """
        Authenticated request, with one token refresh on 401 and a bounded retry
        for transient failures.

        Retry policy, deliberately narrow:
          - **Retried:** connection errors and timeouts (NetSuite unreachable), and
            5xx responses (NetSuite failing server-side). Both are conditions where
            the identical request may well succeed a moment later.
          - **Never retried:** any 4xx. A permission problem or a malformed request
            will fail identically every time, and looping on it would turn a clear,
            immediate error into a slow one — and risk masking it. This matters
            especially here because NetSuite returns permission denials as 400s
            (see `_raise_for_response`), so a naive "retry all errors" would retry
            exactly the failures that most need to surface at once.

        After the attempt budget is spent, raises `NetSuiteTransientError` so the
        audit log can distinguish an outage from a bad request.
        """
        config = self._require_live(f"{method} {url}")
        # Keep the caller's own headers so they survive a retry -- losing them
        # would silently drop `Prefer: transient` on SuiteQL or the idempotency
        # key on a PATCH, exactly when a retry makes those matter most.
        extra_headers = kwargs.pop("headers", {}) or {}

        last_error: Optional[str] = None
        last_status: Optional[int] = None

        for attempt in range(1, self.MAX_TRANSIENT_ATTEMPTS + 1):
            headers = {
                "Authorization": f"Bearer {self.authenticate()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                **extra_headers,
            }
            try:
                response = self.session.request(
                    method, url, headers=headers, timeout=config.timeout, **kwargs
                )
            except Exception as exc:  # network-level: timeout, connection reset, DNS
                if not _is_transient_exception(exc):
                    raise
                last_error = f"{type(exc).__name__}: {exc}"
                last_status = None
            else:
                if response.status_code == 401 and retry_auth:
                    logger.info("Got 401; re-minting token and retrying once")
                    self.authenticate(force=True)
                    return self._request(method, url, retry_auth=False, headers=extra_headers, **kwargs)

                # 4xx: fail fast, no retry. This is the important half of the policy.
                if 400 <= response.status_code < 500:
                    _raise_for_response(response, method, url)

                if response.status_code >= 500:
                    summary, _payload = _extract_error_details(response)
                    last_error = f"HTTP {response.status_code}: {summary[:200]}"
                    last_status = response.status_code
                else:
                    return response

            if attempt < self.MAX_TRANSIENT_ATTEMPTS:
                delay = self.TRANSIENT_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Transient NetSuite failure on %s %s (attempt %d/%d): %s — retrying in %.1fs",
                    method, url, attempt, self.MAX_TRANSIENT_ATTEMPTS, last_error, delay,
                )
                time.sleep(delay)

        raise NetSuiteTransientError(
            f"{method} {url} failed after {self.MAX_TRANSIENT_ATTEMPTS} attempts. "
            f"Last failure: {last_error}. NetSuite appears to be unreachable or failing "
            "server-side — this is not a problem with the request itself, and is worth "
            "retrying later.",
            attempts=self.MAX_TRANSIENT_ATTEMPTS,
            last_status=last_status,
        )

    # -- reads --------------------------------------------------------------

    def resolve_po_internal_id(self, po_number: str) -> str:
        """
        Map a printed PO reference OR a tranId to its internal record id.

        `1662`, `PO#1662` and `PO0001662` all resolve to `"8489541"`: the input goes
        through `po_tranid` first, which is idempotent for an already-correct tranId.
        That transformation used to sit outside the pipeline -- the default was an
        untransformed lookup that always failed against live data, with the padding
        applied only inside a report script. It is now the real behaviour.

        `last_resolved_tranid` records what was actually queried, so a caller can show
        the vendor's printed value beside the derived tranId.

        Both `q=` quoting forms are confirmed working and equivalent (each returns
        totalResults=1 for PO0001662), so quoting is optional. The quoted form is
        tried first and whichever succeeds is recorded in `last_lookup_strategy`.

        Requires `Reports > SuiteAnalytics Workbook` on the role: that permission
        gates every record COLLECTION endpoint, `?q=` filtering included. Without
        it this returns 400 USER_ERROR while single-record GET/PATCH by internal
        id keeps working -- that asymmetry is the diagnostic signature.
        """
        printed = assert_po_reference(po_number)
        tranid = po_tranid(printed)
        self.last_resolved_tranid = tranid

        if tranid in self._po_id_cache:
            return self._po_id_cache[tranid]

        config = self._require_live("resolve_po_internal_id()")
        attempts = [
            ("record q= (quoted)", lambda: self._lookup_via_record_query(f'tranId IS "{tranid}"')),
            ("record q= (unquoted)", lambda: self._lookup_via_record_query(f"tranId IS {tranid}")),
        ]

        errors = []
        for label, attempt in attempts:
            try:
                internal_id = attempt()
            except NetSuitePermissionError:
                raise  # a permission problem is a finding, not something to route around
            except NetSuiteError as exc:
                logger.debug("PO lookup strategy %s failed: %s", label, exc)
                errors.append(f"{label}: {exc}")
                continue
            if internal_id:
                self.last_lookup_strategy = label
                self._po_id_cache[tranid] = internal_id
                logger.info(
                    "Resolved PO %s (as tranId %s) -> internal id %s (via %s)",
                    printed, tranid, internal_id, label,
                )
                return internal_id
            errors.append(f"{label}: no match")

        # A defined outcome. Both strings are in the message because either alone is
        # unactionable: the printed value says what the vendor wrote, the tranId says
        # what was actually asked for. No second format is attempted.
        raise PONumberUnresolvable(
            f"PO {printed!r} was looked up as tranId {tranid!r} and does not exist in "
            f"account {config.account_id}.\n"
            + "\n".join(f"  - {e}" for e in errors)
            + f"\n\nThe tranId came from the account's numbering rule ({TRANID_PREFIX} + "
            f"{TRANID_MIN_DIGITS} digits), which reproduces all 1,659 existing PO tranIds "
            "exactly. A miss therefore means the PO does not exist, not that the format is "
            "wrong -- so no other shape is tried. A human resolves it.",
            printed=printed,
            attempted=tranid,
        )

    def _lookup_via_record_query(self, q: str) -> Optional[str]:
        response = self._request("GET", f"{self._require_live('lookup').record_base}/purchaseOrder", params={"q": q, "limit": 5})
        items = response.json().get("items", [])
        if not items:
            return None
        if len(items) > 1:
            raise NetSuiteError(f"PO lookup {q!r} matched {len(items)} records; refusing to guess which one.")
        return str(items[0]["id"])

    def get_purchase_order_record(self, internal_id: str) -> dict:
        """Fetch the raw PO record with its item sublist expanded."""
        base = self._require_live("get_purchase_order_record()").record_base
        record = self._request("GET", f"{base}/purchaseOrder/{internal_id}", params={"expandSubResources": "true"}).json()

        # Some configurations return the sublist as a link rather than inline.
        if not isinstance(record.get("item"), dict) or "items" not in record.get("item", {}):
            logger.info("Item sublist not inlined; fetching /purchaseOrder/%s/item separately", internal_id)
            record["item"] = self._fetch_sublist_the_long_way(internal_id)

        _assert_sublist_complete(record.get("item"), internal_id)
        return record

    def _fetch_sublist_the_long_way(self, internal_id: str) -> dict:
        base = self._require_live("sublist fetch").record_base
        collection = self._request("GET", f"{base}/purchaseOrder/{internal_id}/item").json()
        lines = []
        for stub in collection.get("items", []):
            line_no = stub.get("line")
            if line_no is None:  # fall back to parsing the self link
                link = next((l.get("href", "") for l in stub.get("links", []) if l.get("rel") == "self"), "")
                line_no = link.rstrip("/").rsplit("/", 1)[-1] or None
            if line_no is None:
                logger.warning("Sublist entry without a line number on PO %s: %s", internal_id, stub)
                continue
            lines.append(self._request("GET", f"{base}/purchaseOrder/{internal_id}/item/{line_no}").json())
        # Same check on the fallback path: it reads a collection too, and the
        # collection reports its own total.
        _assert_sublist_complete({**collection, "items": lines}, internal_id)
        return {"items": lines}

    def get_purchase_order(self, po_number: str) -> list[POLine]:
        """
        Return the PO's item lines as POLine objects.

        Mock mode returns injected data (unchanged stub behavior). Live mode
        resolves the PO number to an internal id, reads the record, and maps the
        item sublist.
        """
        if self.is_mock:
            return self._mock_data.get(po_number, [])

        internal_id = self.resolve_po_internal_id(po_number)
        return self.get_purchase_order_lines_by_internal_id(internal_id, po_number=po_number)

    def get_purchase_order_lines_by_internal_id(
        self, internal_id: str, po_number: Optional[str] = None
    ) -> list[POLine]:
        """Read lines when the internal id is already known (as in the Phase 1 test)."""
        record = self.get_purchase_order_record(internal_id)
        vendor_name = _ref_name(record.get("entity")) or None
        raw_lines = record.get("item", {}).get("items", [])
        logger.info(
            "PO %s (internal id %s, tranId %s): %d item lines",
            po_number or "?",
            internal_id,
            record.get("tranId"),
            len(raw_lines),
        )
        return [self._map_line(raw, internal_id, vendor_name) for raw in raw_lines]

    def _map_line(self, raw: dict, po_internal_id: str, vendor_name: Optional[str]) -> POLine:
        # Style: prefer the dedicated exact-match field, then the parent-item
        # reference, and only then fall back to splitting the display name.
        item_display = _ref_name(raw.get("item"))
        style = str(raw.get(NS_STYLE) or "").strip()
        if not style:
            style = _ref_name(raw.get(NS_PARENT_ITEM))
        if not style and item_display:
            style = item_display.split(":")[0].strip()

        units = raw.get("units")
        return POLine(
            line_id=str(raw.get("line")),
            item=item_display,
            style_number=style,
            vendor_name=vendor_name,
            color=_ref_name(raw.get(NS_COLOR)),
            size=_ref_name(raw.get(NS_SIZE)),
            quantity=int(float(raw.get(NS_QUANTITY) or 0)),
            units=_ref_name(units) if isinstance(units, dict) else str(units or ""),
            expected_receipt_date=_parse_ns_date(raw.get(NS_EXPECTED_RECEIPT_DATE)),
            override_expected_receipt=_to_bool(raw.get(NS_OVERRIDE_EXPECTED_RECEIPT)),
            updated_receipt_date=_parse_ns_date(raw.get(NS_UPDATED_RECEIPT_DATE)),
            closed=_to_bool(raw.get("isClosed")),
            is_open=_to_bool(raw.get("isOpen")),
            quantity_received=_to_number(raw.get("quantityReceived")),
            quantity_billed=_to_number(raw.get("quantityBilled")),
            rate=_to_number(raw.get("rate")),
            po_internal_id=po_internal_id,
            item_internal_id=_ref_id(raw.get("item")),
            raw=raw,
        )

    def get_po_line(self, po_number_or_id: str, line_number: Union[int, str], by_internal_id: bool = False) -> POLine:
        """Read a single line. Convenience for the write/verify/revert test."""
        if by_internal_id:
            lines = self.get_purchase_order_lines_by_internal_id(str(po_number_or_id))
        else:
            lines = self.get_purchase_order(str(po_number_or_id))
        wanted = str(line_number)
        for line in lines:
            if line.line_id == wanted:
                return line
        raise NetSuiteError(
            f"Line {line_number} not found on PO {po_number_or_id}. "
            f"Available lines: {', '.join(l.line_id for l in lines) or '(none)'}"
        )

    # -- writes -------------------------------------------------------------

    def update_po_line(
        self,
        po_number: str,
        line_id: Union[int, str],
        fields: dict,
        by_internal_id: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """
        PATCH one item-sublist line.

        Confirmed working shape (live sandbox test, architecture doc section 6):
        target the line by its `line` number inside `item.items[]` and send all
        four fields in a single call.

        Deliberately does NOT send the `replace` query parameter. `replace=item`
        would replace the entire sublist rather than merging this one line --
        i.e. it would wipe every other line on the PO. There is no code path
        here that sets it.

        Returns a result dict (not the updated record -- NetSuite answers a
        successful PATCH with 204 No Content). Always read the line back to
        confirm; see the silent-discard caveat in the module docstring.
        """
        if self.is_mock:
            raise NetSuiteConfigError(
                "update_po_line() is a live-only operation; this client is in mock mode. "
                "Mock mode exists for matcher/diff testing (demo_matcher.py), which must never write."
            )

        config = self._require_live("update_po_line()")
        body_fields = normalize_line_fields(fields)
        internal_id = str(po_number) if by_internal_id else self.resolve_po_internal_id(str(po_number))
        line_number = int(line_id)

        payload = {"item": {"items": [{"line": line_number, **body_fields}]}}
        logger.info("PATCH purchaseOrder/%s line %s: %s", internal_id, line_number, body_fields)

        response = self._request(
            "PATCH",
            f"{config.record_base}/purchaseOrder/{internal_id}",
            json=payload,
            headers={"X-NetSuite-Idempotency-Key": idempotency_key or str(uuid.uuid4())},
        )

        return {
            "ok": True,
            "status_code": response.status_code,
            "po_number": None if by_internal_id else po_number,
            "po_internal_id": internal_id,
            "line": line_number,
            "sent": body_fields,
        }

    def get_item_colour_name(
        self, item_internal_id: Union[int, str], cache: Optional[dict] = None
    ) -> Optional[str]:
        """
        The long-form colour name on one child item, or None.

        `cache` is a caller-owned dict keyed by item internal id, so one shipment's
        processing reads each item once. It is deliberately NOT persisted: a table
        would need a refresh story, and this is static reference data that costs one
        cheap GET when it is actually needed. Vendors who print colour codes never
        trigger a read at all.

        Returns None when the field is empty, when the item cannot be read, or in
        mock mode. None means "no name available" and the caller must flag rather
        than guess -- see `matcher.build_colour_lookup`.
        """
        key = str(item_internal_id)
        if cache is not None and key in cache:
            return cache[key]

        value: Optional[str] = None
        if not self.is_mock:
            config = self._require_live("get_item_colour_name()")
            # Child SKUs are inventory items here, but the record type is not
            # guaranteed, so fall through rather than failing the whole shipment.
            for record_type in ("inventoryItem", "assemblyItem", "nonInventorySaleItem"):
                try:
                    record = self._request(
                        "GET", f"{config.record_base}/{record_type}/{key}"
                    ).json()
                except NetSuiteError:
                    continue
                raw = record.get(ITEM_COLOUR_NAME_FIELD)
                value = str(raw).strip() or None if raw is not None else None
                break
            else:
                logger.warning("item %s: could not be read for a colour name", key)

        if cache is not None:
            cache[key] = value
        return value

    # -- diagnostics --------------------------------------------------------

    def verify_connection(self) -> dict:
        """
        Prove auth works end-to-end without touching a specific record: mint a
        token, then make the cheapest authenticated read available.

        The probe is the purchaseOrder metadata catalog. An earlier version used
        a `GET /purchaseOrder?limit=1` collection listing, which the "PO Update"
        role is refused (see `probe_collection_listing`) -- that made the smoke
        test fail for a reason unrelated to what Phase 1 actually needs. The
        metadata catalog returns no business data and exercises exactly the
        permission the write path depends on (Setup > REST Web Services).

        Collection-listing capability is reported alongside, as information
        rather than a pass/fail gate, because Phase 1's write path targets an
        internal id directly and does not need it -- but Phase 2 onward does.
        """
        config = self._require_live("verify_connection()")
        started = time.time()
        self.authenticate(force=True)
        response = self._request("GET", f"{config.record_base}/metadata-catalog/purchaseOrder")
        listing_ok, listing_detail = self.probe_collection_listing()
        return {
            "account_id": config.account_id,
            "host": config.host,
            "is_sandbox": config.is_sandbox,
            "algorithm": config.algorithm,
            "token_expires_in": max(0, int(self._token_expires_at - time.time())),
            "probe_status": response.status_code,
            "collection_listing_ok": listing_ok,
            "collection_listing_detail": listing_detail,
            "elapsed_seconds": round(time.time() - started, 2),
        }

    def probe_collection_listing(self) -> tuple[bool, str]:
        """
        Can this role list/search the purchaseOrder collection?

        This is what `resolve_po_internal_id` needs in order to turn a tranId
        ("PO0001662") into an internal id ("8489541").

        RESOLVED 2026-08-11: collection access requires `Reports > SuiteAnalytics
        Workbook` on the role -- confirmed by bisect as the sole cause. Until it
        was added this returned 400 USER_ERROR while single-record GET/PATCH by
        internal id worked fine. Retained as a live check because that exact
        asymmetry is the signature to look for if it regresses (a sandbox refresh
        rebuilding the role would do it).

        Non-fatal by design: returns a verdict instead of raising, so callers
        can report it as a finding rather than dying on it.
        """
        config = self._require_live("probe_collection_listing()")
        try:
            response = self._request("GET", f"{config.record_base}/purchaseOrder", params={"limit": 1})
        except NetSuitePermissionError as exc:
            return False, f"refused: {str(exc).splitlines()[0]}"
        except NetSuiteError as exc:
            return False, f"failed: {str(exc).splitlines()[0]}"
        return True, f"HTTP {response.status_code}"


# ---------------------------------------------------------------------------
# Error translation -- NetSuite's errors are RFC 7807 problem+json and its
# permission messages are easy to mistake for generic 400s if not unpacked.
# ---------------------------------------------------------------------------


def _extract_error_details(response) -> tuple[str, Any]:
    try:
        payload = response.json()
    except Exception:
        return (response.text or "").strip()[:1000], None

    details: Iterable[dict] = payload.get("o:errorDetails") or []
    messages = []
    for detail in details:
        code = detail.get("o:errorCode") or detail.get("errorCode") or ""
        text = detail.get("detail") or detail.get("message") or ""
        path = detail.get("o:errorPath") or ""
        messages.append(" ".join(p for p in (f"[{code}]" if code else "", path, text) if p).strip())

    summary = "; ".join(m for m in messages if m) or payload.get("title") or payload.get("detail") or str(payload)[:1000]
    return summary, payload


def _is_transient_exception(exc: BaseException) -> bool:
    """
    Whether a network-level exception is worth retrying.

    Timeouts and connection failures are; anything else (a bad URL, a TLS
    verification failure, a programming error) is not, and must propagate so it
    gets fixed rather than retried.
    """
    try:
        import requests.exceptions as rex

        if isinstance(exc, (rex.Timeout, rex.ConnectionError, rex.ChunkedEncodingError)):
            # An SSLError is a ConnectionError subclass but is a configuration
            # problem, not a blip -- don't mask it behind retries.
            return not isinstance(exc, rex.SSLError)
    except ImportError:  # pragma: no cover
        pass
    return isinstance(exc, (TimeoutError, ConnectionError))


def _raise_for_response(response, method: str, url: str) -> None:
    summary, payload = _extract_error_details(response)
    status = response.status_code
    context = f"{method} {url} -> HTTP {status}: {summary}"

    # NetSuite phrases permission refusals several different ways and does not
    # reliably use 403 for them -- the sandbox returns HTTP 400 + USER_ERROR
    # "Your current role does not have permission to perform this action."
    # (observed 2026-08-04). Match on wording as well as status, or a genuine
    # permission finding gets misreported as a generic bad request.
    permission_markers = (
        "insufficient_permission",
        "insufficient permission",
        "do not have permission",
        "does not have permission",
        "do not have permissions",
        "does not have permissions",
        "do not have privileges",
        "does not have privileges",
        "permission violation",
        "not have access",
    )
    if status in (401, 403) or any(marker.lower() in summary.lower() for marker in permission_markers):
        raise NetSuitePermissionError(
            context
            + "\n\nThis looks like a role/permission problem rather than a bad request.\n"
            "For Phase 1 that is a REAL FINDING, not something to route around: it would mean\n"
            "the least-privilege 'PO Update' role lacks something the CFO role had. Likely\n"
            "candidates are field-level access on custcol_override_expected_receipt /\n"
            "custcol_sd_updatedreceiptdate, or a missing Purchase Order: Edit level.\n"
            "Report it before widening the role -- see architecture doc section 6."
        )
    raise NetSuiteAPIError(context, status_code=status, payload=payload)


def _explain_token_failure(response, config: NetSuiteConfig) -> str:
    summary, _ = _extract_error_details(response)
    base = f"Token request failed (HTTP {response.status_code}): {summary}"
    hints = {
        "invalid_client": (
            "NetSuite could not match the assertion to a certificate/integration. Check, in order:\n"
            f"  - NS_CERTIFICATE_ID ({config.certificate_id!r}) matches the Certificate ID shown on\n"
            "    Setup > Integration > OAuth 2.0 Client Credentials (M2M) Setup\n"
            "  - NS_CLIENT_ID is the Integration record's CONSUMER KEY / CLIENT ID (not the secret)\n"
            "  - the certificate you uploaded is the one matching this private key\n"
            "  - the certificate has not expired, and its mapping row still exists\n"
            "  - the Integration record has 'CLIENT CREDENTIALS' checked and REST WEB SERVICES scoped"
        ),
        "invalid_grant": (
            "The assertion was rejected. Usually the entity/role mapping: confirm the M2M setup row\n"
            "points at the service-account employee AND the 'PO Update' role, and that the role is\n"
            "actually assigned to that employee."
        ),
        "invalid_request": "Malformed assertion -- check NS_JWT_ALGORITHM (PS256 or RS256) and the machine clock.",
        "unsupported_grant_type": "The Integration record does not have Client Credentials enabled.",
    }
    for marker, hint in hints.items():
        if marker in summary.lower().replace(" ", "_") or marker in summary.lower():
            return f"{base}\n\n{hint}"
    return base
