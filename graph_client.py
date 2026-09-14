"""
The ONLY Microsoft Graph surface this project depends on.

Four methods. If a call is not on `GraphClient`, the pipeline cannot make it --
which is the point, and it is enforced twice over: by the abstract base class
here, and by `test_poller.test_graph_surface_is_read_only`, an AST scan in the
style of `test_schema.test_migrations_import_no_application_code`. That test
fails on any HTTP verb but GET and on any Graph path outside these four.

## Why an interface at all, when there is one real implementation

Because there are two, and switching between them must not be a code change. The
mock is not a testing convenience bolted on afterwards -- it is how the whole
intake path is exercised without a mailbox, without credentials and without
tokens, which is what lets this be built and changed while `GRAPH_CLIENT=mock`.
`build_graph_client` reads that switch through `GraphConfig` and nothing here
touches `os.environ`; see `config.load_env_file`, which is the one loader.

## What is deliberately NOT here

- **No folder listing.** Resolving `parentFolderId` to a display name needs
  `/mailFolders/{id}`, a fifth call. The folder id is recorded verbatim instead
  (`messages.folder_id`), so a folder rule can be written later against stored
  rows. The mailbox is dedicated to this pipeline, so nothing is filtered today.
- **No delta tokens.** The watermark is a timestamp with overlap. See
  `schema.poll_state` for why a position beats an opaque server cursor.
- **Nothing that writes.** No send, no delete, no move, no mark-read, no flag.
  `Mail.Read` could not do them anyway -- but the app registration's permission
  is not this module's guarantee to rely on, and a permission can be widened by
  someone who has no idea this code assumed it would not be.

## Shapes

Graph JSON is passed through as dicts with Graph's own key names
(`receivedDateTime`, `internetMessageId`, `parentFolderId`, `contentBytes`),
rather than being mapped into project types here. Translation happens in
`poller`, in one place, where it can be read next to the columns it fills.
"""

from __future__ import annotations

import abc
import base64
import datetime as dt
import logging
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

#: Fields fetched for a message. Explicit rather than defaulted, so adding one is
#: a visible decision and bodies are never pulled by accident -- this pipeline
#: reads attachments, and a mail body is personal correspondence it has no reason
#: to hold.
MESSAGE_FIELDS = (
    "id", "internetMessageId", "subject", "from", "receivedDateTime",
    "sentDateTime", "hasAttachments", "parentFolderId",
)

#: Retry budget for a throttled or unavailable Graph. Bounded: after this many
#: attempts the message is recorded as failed and the poll moves on, because an
#: unbounded retry turns one bad message into a stalled pipeline.
MAX_RETRIES = 4
#: Cap on a server-supplied `Retry-After`. Graph can ask for a long wait; honour
#: it up to a point, then give up and let the next poll try, rather than holding
#: a process for an unbounded time a remote service chose.
MAX_RETRY_AFTER_SECONDS = 120


class GraphError(RuntimeError):
    """A Graph call failed in a way the caller should record, not retry."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class GraphClient(abc.ABC):
    """
    The four calls. Nothing else is reachable.

    `since` is an aware UTC datetime; implementations filter on
    `receivedDateTime >= since` INCLUSIVELY. The inclusive comparison is half of
    the overlap that keeps near-simultaneous deliveries from being lost -- see
    `poller.POLL_OVERLAP` for the other half and the reasoning.
    """

    #: Reported in logs and recorded on the rows this run produces, so a database
    #: can say which client wrote it.
    kind: str = "abstract"

    @abc.abstractmethod
    def list_messages(self, since: dt.datetime) -> Sequence[dict]:
        """Messages received at or after `since`, oldest first."""

    @abc.abstractmethod
    def get_message(self, message_id: str) -> dict:
        """One message's metadata."""

    @abc.abstractmethod
    def list_attachments(self, message_id: str) -> Sequence[dict]:
        """Attachment metadata for one message: id, name, contentType, size."""

    @abc.abstractmethod
    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """One attachment's bytes."""


# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent

#: The real corpus, wrapped in the envelopes these vendors actually send.
#:
#: Every attachment is a REAL file tracked in this repository -- the same five
#: vendors the extractor is validated against -- so a poll followed by the
#: extraction driver reproduces the figures in RUNBOOK section 6 end to end,
#: from "a message arrived" rather than from a path handed in by a test.
#:
#: Two shapes are here on purpose because they are the ones that break things:
#: `msg-inprotex-001` carries TWO attachments (a packing list and its shipping
#: advice, which is how Inprotex sends), and `msg-symmetry-004` carries NONE --
#: a vendor replying to a thread. A poller that assumes at least one attachment
#: silently drops the latter, and one that assumes at most one drops half the
#: former.
#:
#: `msg-legendz-006` is a re-forward: the SAME bytes as `msg-legendz-002` under a
#: different filename, which is how Paula forwards. It must store one attachment
#: row and two `message_attachments` rows.
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_XLS = "application/vnd.ms-excel"
_PDF = "application/pdf"

MOCK_MESSAGES: tuple[dict, ...] = (
    {
        "id": "msg-inprotex-001",
        "internetMessageId": "<SD219.0626.inprotex@mail.example>",
        "subject": "SD-219 / PO#1662 shipment - invoice & packing list",
        "from": {"emailAddress": {"address": "shipping@inprotex.example",
                                  "name": "Inprotex Shipping"}},
        "receivedDateTime": "2026-06-27T02:14:00Z",
        "sentDateTime": "2026-06-27T02:13:41Z",
        "hasAttachments": True,
        "parentFolderId": "AAMkAG-inbox",
        "_attachments": [
            ("0626建躍空運成衣 (SD-219國外)Invoice_Packing.xlsx", _XLSX),
            ("Shipping Advice 6128990769 建躍.pdf", _PDF),
        ],
    },
    {
        "id": "msg-legendz-002",
        "internetMessageId": "<PL0801.legendz@mail.example>",
        "subject": "PO#1657 packing list - 26 ctns",
        "from": {"emailAddress": {"address": "export@legendz.example",
                                  "name": "Legendz Export"}},
        "receivedDateTime": "2026-08-01T09:02:00Z",
        "sentDateTime": "2026-08-01T09:01:12Z",
        "hasAttachments": True,
        "parentFolderId": "AAMkAG-inbox",
        "_attachments": [("Legendz PL0801- 26ctns.xlsx", _XLSX)],
    },
    {
        "id": "msg-symmetry-003",
        "internetMessageId": "<SD1720-1721.symmetry@mail.example>",
        "subject": "SD #1720, 1721 - actual packing lists",
        "from": {"emailAddress": {"address": "docs@symmetry.example",
                                  "name": "Symmetry Documentation"}},
        "receivedDateTime": "2026-08-11T06:45:00Z",
        "sentDateTime": "2026-08-11T06:44:30Z",
        "hasAttachments": True,
        "parentFolderId": "AAMkAG-inbox",
        "_attachments": [
            ("SD Actual Packing Covering ^N1720^J 1721.pdf", _PDF),
            ("SD Actual Packing ^N1720^J 1721.pdf", _PDF),
            ("SD #1720, 1721 INVOICE, PACKING LIST.pdf", _PDF),
        ],
    },
    {
        # Zero attachments. A real shape: the vendor answering a question.
        "id": "msg-symmetry-004",
        "internetMessageId": "<re.SD1720.symmetry@mail.example>",
        "subject": "RE: SD #1720, 1721 - actual packing lists",
        "from": {"emailAddress": {"address": "docs@symmetry.example",
                                  "name": "Symmetry Documentation"}},
        "receivedDateTime": "2026-08-11T08:20:00Z",
        "sentDateTime": "2026-08-11T08:19:55Z",
        "hasAttachments": False,
        "parentFolderId": "AAMkAG-inbox",
        "_attachments": [],
    },
    {
        "id": "msg-tainan-005",
        "internetMessageId": "<50144.PO1725.tainan@mail.example>",
        "subject": "PO 0001725 packing list (correction on Aug.10 from Aug.07)",
        "from": {"emailAddress": {"address": "ekspor@tainan.example",
                                  "name": "PT Tainan Enterprises"}},
        "receivedDateTime": "2026-08-10T04:30:00Z",
        "sentDateTime": "2026-08-10T04:29:18Z",
        "hasAttachments": True,
        "parentFolderId": "AAMkAG-inbox",
        "_attachments": [
            ("50144--- PO 0001725   packing list  ( Correction on Aug.10 from Aug.07 ).xls",
             _XLS),
        ],
    },
    {
        # Re-forward: same bytes as msg-legendz-002, different filename.
        "id": "msg-legendz-006",
        "internetMessageId": "<fwd.PL0801.legendz@mail.example>",
        "subject": "FW: PO#1657 packing list - 26 ctns",
        "from": {"emailAddress": {"address": "paula@straightdown.example",
                                  "name": "Paula"}},
        "receivedDateTime": "2026-08-01T15:40:00Z",
        "sentDateTime": "2026-08-01T15:39:50Z",
        "hasAttachments": True,
        "parentFolderId": "AAMkAG-forwarded",
        "_attachments": [("PL0801 (forwarded).xlsx", _XLSX)],
        "_attachment_sources": ["Legendz PL0801- 26ctns.xlsx"],
    },
    {
        "id": "msg-footwear-007",
        "internetMessageId": "<CMG26A019A.PO1624.footwear@mail.example>",
        "subject": "FW26 PO-1624 footwear packing sheets",
        "from": {"emailAddress": {"address": "shipping@cmg.example",
                                  "name": "CMG Footwear"}},
        "receivedDateTime": "2026-05-17T23:05:00Z",
        "sentDateTime": "2026-05-17T23:04:02Z",
        "hasAttachments": True,
        "parentFolderId": "AAMkAG-inbox",
        "_attachments": [
            ("FW26 footwear PO-1624 packing sheets (invoice sheet removed).xlsx", _XLSX),
        ],
    },
)


def _parse_graph_time(value: str) -> dt.datetime:
    """Graph's ISO-8601 with a trailing `Z`, as an aware UTC datetime."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


class MockGraphClient(GraphClient):
    """
    The five real vendors in realistic envelopes, served from disk.

    Makes no network call and needs no credential, which is what allows the
    intake path to be developed and changed with `GRAPH_CLIENT=mock`.

    A message whose fixture file is missing from the working tree is served with
    its metadata intact and an attachment fetch that RAISES. That is deliberate:
    the corpus is large and someone will eventually run without all of it, and
    the honest behaviour then is a per-message failure the poller records and
    steps over -- exactly the path item 6 of the brief asks for -- rather than a
    silently shorter mailbox.
    """

    kind = "mock"

    def __init__(self, messages: Iterable[dict] = MOCK_MESSAGES,
                 corpus_dir: Optional[Path] = None) -> None:
        self._messages = list(messages)
        self._dir = Path(corpus_dir) if corpus_dir else HERE

    # -- the four -----------------------------------------------------------
    def list_messages(self, since: dt.datetime) -> Sequence[dict]:
        out = [m for m in self._messages
               if _parse_graph_time(m["receivedDateTime"]) >= since]
        out.sort(key=lambda m: (_parse_graph_time(m["receivedDateTime"]), m["id"]))
        return [self._envelope(m) for m in out]

    def get_message(self, message_id: str) -> dict:
        return self._envelope(self._find(message_id))

    def list_attachments(self, message_id: str) -> Sequence[dict]:
        message = self._find(message_id)
        out = []
        for index, (name, content_type) in enumerate(message.get("_attachments", [])):
            path = self._source_path(message, index, name)
            out.append({
                "id": f"{message_id}-att-{index}",
                "name": name,
                "contentType": content_type,
                "size": path.stat().st_size if path.exists() else 0,
            })
        return out

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        message = self._find(message_id)
        attachments = message.get("_attachments", [])
        try:
            index = int(str(attachment_id).rsplit("-", 1)[-1])
            name, _ = attachments[index]
        except (ValueError, IndexError):
            raise GraphError(f"no attachment {attachment_id!r} on {message_id!r}") from None
        path = self._source_path(message, index, name)
        if not path.exists():
            raise GraphError(
                f"fixture missing from the working tree: {path.name!r}. The mock "
                "serves the real vendor corpus; this message cannot be fetched."
            )
        return path.read_bytes()

    # -- internals ----------------------------------------------------------
    def _find(self, message_id: str) -> dict:
        for message in self._messages:
            if message["id"] == message_id:
                return message
        raise GraphError(f"no message {message_id!r} in the mock mailbox")

    def _source_path(self, message: dict, index: int, name: str) -> Path:
        """
        Where the bytes come from.

        `_attachment_sources` lets a message present a file under a DIFFERENT
        name than the one on disk, which is how the re-forward fixture carries
        identical bytes under a new filename.
        """
        sources = message.get("_attachment_sources")
        return self._dir / (sources[index] if sources else name)

    @staticmethod
    def _envelope(message: dict) -> dict:
        """Graph's own shape: the private `_` keys are fixture wiring, not API."""
        return {k: v for k, v in message.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Real
# ---------------------------------------------------------------------------


class RealGraphClient(GraphClient):
    """
    Certificate client-credentials against Entra, modelled on
    `scripts/probe_graph_auth.py` rather than invented a second time.

    The probe is the thing that proved this path works end to end -- it found a
    missing certificate on its first run -- so the token acquisition here is
    deliberately the same MSAL call with the same three credential fields. Two
    implementations of one auth flow is how they drift, and only one of them
    would then be the one that was tested.
    """

    kind = "real"

    def __init__(self, config, session=None) -> None:
        self._cfg = config
        self._session = session
        self._token: Optional[str] = None

    # -- the four -----------------------------------------------------------
    def list_messages(self, since: dt.datetime) -> Sequence[dict]:
        params = {
            "$select": ",".join(MESSAGE_FIELDS),
            "$orderby": "receivedDateTime asc",
            "$top": 50,
            "$filter": f"receivedDateTime ge {_graph_time(since)}",
        }
        out: list[dict] = []
        url = f"{GRAPH_ROOT}/users/{self._cfg.mailbox}/messages"
        while url:
            payload = self._get_json(url, params=params)
            out.extend(payload.get("value", []))
            # Graph carries every parameter in the nextLink itself; re-sending
            # them alongside it is how a paged read silently restarts at page 1.
            url, params = payload.get("@odata.nextLink"), None
        return out

    def get_message(self, message_id: str) -> dict:
        return self._get_json(
            f"{GRAPH_ROOT}/users/{self._cfg.mailbox}/messages/{message_id}",
            params={"$select": ",".join(MESSAGE_FIELDS)},
        )

    def list_attachments(self, message_id: str) -> Sequence[dict]:
        payload = self._get_json(
            f"{GRAPH_ROOT}/users/{self._cfg.mailbox}/messages/{message_id}/attachments",
            params={"$select": "id,name,contentType,size"},
        )
        return payload.get("value", [])

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        payload = self._get_json(
            f"{GRAPH_ROOT}/users/{self._cfg.mailbox}/messages/{message_id}"
            f"/attachments/{attachment_id}"
        )
        content = payload.get("contentBytes")
        if content is None:
            raise GraphError(
                f"attachment {attachment_id!r} carries no contentBytes -- it is "
                f"probably an item attachment (a forwarded mail) rather than a file. "
                f"type={payload.get('@odata.type')!r}"
            )
        return base64.b64decode(content)

    # -- internals ----------------------------------------------------------
    def _acquire_token(self) -> str:
        if self._token:
            return self._token
        import msal

        cfg = self._cfg
        app = msal.ConfidentialClientApplication(
            client_id=cfg.client_id,
            authority=f"https://login.microsoftonline.com/{cfg.tenant_id}",
            client_credential={
                "private_key": cfg.cert_path.read_text(encoding="utf-8"),
                "thumbprint": cfg.cert_thumbprint,
                "public_certificate": cfg.cert_public_path.read_text(encoding="utf-8"),
            },
        )
        result = app.acquire_token_for_client(scopes=[GRAPH_SCOPE])
        token = result.get("access_token")
        if not token:
            # Microsoft's own text, verbatim: AADSTS codes name the thumbprint
            # offered and the app it was offered to, which is what made the
            # missing-certificate failure diagnosable rather than a mystery.
            raise GraphError(
                f"token request failed: {result.get('error')} "
                f"{result.get('error_description')}"
            )
        self._token = token
        return token

    def _get_json(self, url: str, params: Optional[dict] = None) -> dict:
        """
        The ONLY place this module makes an HTTP request, and it only ever GETs.

        Retries 429 and 503 honouring `Retry-After`, bounded by `MAX_RETRIES`;
        anything else is raised for the caller to record against the message.
        """
        import requests

        session = self._session or requests
        for attempt in range(1, MAX_RETRIES + 1):
            response = session.get(
                url,
                headers={"Authorization": f"Bearer {self._acquire_token()}"},
                params=params,
                timeout=60,
            )
            status = response.status_code
            if status == 200:
                return response.json()
            if status in (429, 503) and attempt < MAX_RETRIES:
                delay = _retry_after_seconds(response.headers, attempt)
                logger.warning("Graph %s on %s; retrying in %ss (attempt %d/%d)",
                               status, url, delay, attempt, MAX_RETRIES)
                time.sleep(delay)
                continue
            if status == 401 and attempt < MAX_RETRIES and self._token:
                # The token expired mid-poll. Drop it and let the next attempt
                # mint a fresh one -- distinct from a 401 on the FIRST call,
                # which means the credential is wrong and retrying cannot help.
                self._token = None
                continue
            raise GraphError(
                f"GET {url} returned {status}: {response.text[:400]}", status=status
            )
        raise GraphError(f"GET {url} still failing after {MAX_RETRIES} attempts")


def _retry_after_seconds(headers: Any, attempt: int) -> float:
    """
    Honour a server-supplied `Retry-After`, capped; otherwise back off.

    Capped because Graph can ask for a very long wait and a poller that obeys it
    literally is indistinguishable from one that has hung. Past the cap the call
    fails, the message is recorded, and the next scheduled poll tries again --
    which is the behaviour a 15-minute cadence already provides for free.
    """
    raw = None
    try:
        raw = headers.get("Retry-After")
    except AttributeError:
        pass
    if raw is not None:
        try:
            return min(float(raw), MAX_RETRY_AFTER_SECONDS)
        except (TypeError, ValueError):
            pass
    return min(2.0 ** attempt, MAX_RETRY_AFTER_SECONDS)


def _graph_time(value: dt.datetime) -> str:
    """An aware UTC datetime as Graph's filter literal."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_graph_client(config=None) -> GraphClient:
    """
    The switch, and the only place it is read.

    `GraphConfig.from_env` validates `GRAPH_CLIENT` strictly -- exact match, no
    default, never falling back to `real` -- so a typo raises here rather than
    sending live requests at a real mailbox. Nothing in this module reads
    `os.environ`; the single `.env` loader is `config.load_env_file`.
    """
    if config is None:
        import config as config_module

        config = config_module.GraphConfig.from_env()
    if config.is_mock:
        return MockGraphClient()
    return RealGraphClient(config)
