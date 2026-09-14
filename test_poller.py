"""
Mailbox intake tests: the poller, the blob store, and the extraction seam.

Offline throughout. `MockGraphClient` serves the real vendor corpus in realistic
envelopes, so these exercise the same bytes the extractor is validated against
without a mailbox, a credential or a token.

What each group pins, and why it is here rather than left to review:

  - RE-POLL IDEMPOTENCY. The mailbox is immutable to this app -- `Mail.Read`
    cannot mark a message read or move it -- so "have I seen this?" is only
    answerable from the database. A second poll over the same window must write
    nothing.
  - THE OVERLAP WINDOW. A bare `>` on `receivedDateTime` loses messages
    delivered in the same second. The test proves the window reaches BACK past
    the watermark, and that dedup makes the re-read free.
  - A MID-BATCH FAILURE. One unreadable message must not stop the poll, must be
    recorded against its own row, and must NOT let the watermark advance past
    it -- otherwise the shipment is lost and nothing ever looks again.
  - DUPLICATE ATTACHMENT CONTENT. The same bytes under two filenames: one file
    on disk, one `attachments` row, two `message_attachments` rows.
  - A MESSAGE WITH NO ATTACHMENTS. Stored as a fact, not skipped, and it must
    leave the extraction queue rather than growing a permanent backlog of one.
  - READ-ONLY, ASSERTED. An AST scan over `graph_client.py` in the style of
    `test_schema.test_migrations_import_no_application_code`: no HTTP verb but
    GET, and no Graph path outside the four calls.
"""

from __future__ import annotations

import ast
import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, func, select

import extract_pending as ep
import graph_client as gc
import poller
from schema import attachments, message_attachments, messages, metadata, poll_state  # noqa: F401

HERE = Path(__file__).resolve().parent

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    _results.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  --  {detail}" if detail else ""))
    return bool(ok)


def section(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 72 - len(title)))


def fresh_db():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    return engine


def counts(engine) -> dict:
    with engine.connect() as conn:
        return {
            "messages": conn.execute(select(func.count()).select_from(messages)).scalar(),
            "attachments": conn.execute(
                select(func.count()).select_from(attachments)).scalar(),
            "links": conn.execute(
                select(func.count()).select_from(message_attachments)).scalar(),
        }


MAILBOX = "shipments@example.test"
#: A frozen, NAIVE test clock -- deliberately. Every DateTime column in this
#: schema stores naive UTC (`poller._naive` converts on the way in), so an
#: aware value here would test a shape the database never sees.
NOW = dt.datetime(2026, 9, 14, 12, 0, 0)  # noqa: DTZ001


# ---------------------------------------------------------------------------


def test_mock_envelopes(tmp: Path) -> None:
    section("the mock serves the real corpus in realistic envelopes")
    client = gc.MockGraphClient()
    msgs = client.list_messages(poller.EPOCH)
    check(len(msgs) == 7, "seven messages", str(len(msgs)))

    times = [gc._parse_graph_time(m["receivedDateTime"]) for m in msgs]
    check(times == sorted(times), "returned oldest first, which the watermark relies on")

    by_id = {m["id"]: m for m in msgs}
    two = client.list_attachments("msg-inprotex-001")
    check(len(two) == 2, "a message with TWO attachments exists (packing list + advice)",
          str([a["name"][:28] for a in two]))
    none = client.list_attachments("msg-symmetry-004")
    check(none == [] and by_id["msg-symmetry-004"]["hasAttachments"] is False,
          "and one with NONE -- a vendor replying to a thread")

    check(all(m.get("internetMessageId") for m in msgs),
          "every envelope carries internetMessageId as well as id")
    check(all("parentFolderId" in m for m in msgs), "and the folder it was found in")
    check({m["parentFolderId"] for m in msgs} == {"AAMkAG-inbox", "AAMkAG-forwarded"},
          "two folders, so folder recording is actually exercised")
    check(not any(k.startswith("_") for m in msgs for k in m),
          "fixture wiring keys are not leaked into the Graph shape")

    types = {a["contentType"] for mid in by_id for a in client.list_attachments(mid)}
    check(types == {gc._XLSX, gc._XLS, gc._PDF},
          "content types match what these vendors really send", str(sorted(types)))


def test_blob_store_is_content_addressed(tmp: Path) -> None:
    section("the blob store: filename IS the content hash")
    store = poller.BlobStore(tmp / "blobs")
    sha_a, path_a = store.put(b"vendor bytes", suffix=".xlsx")
    sha_b, path_b = store.put(b"vendor bytes", suffix=".xlsx")
    check(sha_a == sha_b and path_a == path_b, "identical bytes -> identical path")
    check(path_a.name.startswith(sha_a), "the name carries the hash", path_a.name[:24])
    check(path_a.parent.name == sha_a[:2], "sharded one level to keep directories small")

    sha_c, path_c = store.put(b"different bytes", suffix=".pdf")
    check(sha_c != sha_a and path_c != path_a, "different bytes -> different path")
    check(len(list((tmp / "blobs").rglob("*.xlsx"))) == 1,
          "storing the same content twice leaves ONE file")
    check(not list((tmp / "blobs").rglob("*.part")),
          "no partial file is left behind -- writes rename into place")


def test_poll_stores_and_is_idempotent(tmp: Path) -> None:
    section("a poll stores what arrived; a second poll stores nothing")
    engine, store = fresh_db(), poller.BlobStore(tmp / "blobs")
    client = gc.MockGraphClient()

    first = poller.poll_once(engine, client, MAILBOX, store, now=NOW)
    after_first = counts(engine)
    check(first.seen == 7 and first.stored == 7, "all seven stored",
          f"seen={first.seen} stored={first.stored}")
    check(not first.failed, "no failures", str(first.failed))
    check(after_first["messages"] == 7, "seven message rows", str(after_first))

    # Nine attachment SLOTS across the fixtures, but the forward repeats the
    # Legendz bytes, so eight distinct contents and nine joins.
    check(after_first["attachments"] == 8,
          "eight distinct attachment contents", str(after_first["attachments"]))
    check(after_first["links"] == 9,
          "nine message->attachment joins", str(after_first["links"]))

    second = poller.poll_once(engine, client, MAILBOX, store, now=NOW)
    check(counts(engine) == after_first,
          "a SECOND poll over the same mailbox writes nothing new", str(counts(engine)))
    check(second.stored == 0 and second.skipped == second.seen,
          "and reports every message as already held",
          f"stored={second.stored} skipped={second.skipped}/{second.seen}")

    with engine.connect() as conn:
        row = conn.execute(select(messages).where(
            messages.c.graph_message_id == "msg-legendz-006")).one()
    check(row.folder_id == "AAMkAG-forwarded",
          "the folder is RECORDED so a rule can be added later from stored data",
          str(row.folder_id))
    check(row.internet_message_id == "<fwd.PL0801.legendz@mail.example>",
          "internetMessageId is stored alongside the Graph id, not instead of it")
    check(row.attachment_count == 1, "and what the mailbox said it carried")

    with engine.connect() as conn:
        stored_types = set(conn.execute(select(attachments.c.doc_type)).scalars())
    check(stored_types == {poller.UNCLASSIFIED},
          "the poller classifies NOTHING -- every row is UNCLASSIFIED", str(stored_types))


def test_duplicate_content_stores_once_joins_twice(tmp: Path) -> None:
    section("the same bytes under two filenames: one row, two joins")
    engine, store = fresh_db(), poller.BlobStore(tmp / "blobs")
    poller.poll_once(engine, gc.MockGraphClient(), MAILBOX, store, now=NOW)

    legendz = gc.MockGraphClient().get_attachment("msg-legendz-002", "msg-legendz-002-att-0")
    import hashlib
    sha = hashlib.sha256(legendz).hexdigest()

    with engine.connect() as conn:
        rows = conn.execute(select(attachments).where(
            attachments.c.content_sha256 == sha)).all()
        links = conn.execute(select(message_attachments).where(
            message_attachments.c.content_sha256 == sha)).all()
    check(len(rows) == 1, "ONE attachments row for the repeated content", str(len(rows)))
    check(len(links) == 2, "TWO joins -- the original and the forward", str(len(links)))
    check({link.filename for link in links}
          == {"Legendz PL0801- 26ctns.xlsx", "PL0801 (forwarded).xlsx"},
          "each join keeps the filename IT arrived under",
          str(sorted(link.filename for link in links)))
    check(len(list((tmp / "blobs").rglob(f"{sha}*"))) == 1,
          "and one file on disk, because the store is keyed by content")


def test_message_with_no_attachments(tmp: Path) -> None:
    section("a message with zero attachments is a fact, not an absence")
    engine, store = fresh_db(), poller.BlobStore(tmp / "blobs")
    poller.poll_once(engine, gc.MockGraphClient(), MAILBOX, store, now=NOW)

    with engine.connect() as conn:
        row = conn.execute(select(messages).where(
            messages.c.graph_message_id == "msg-symmetry-004")).one()
        links = conn.execute(select(func.count()).select_from(message_attachments)
                             .where(message_attachments.c.message_id == row.id)).scalar()
    check(row is not None, "it is STORED, not skipped")
    check(row.attachment_count == 0 and links == 0, "with zero declared and zero joined")
    check(row.poll_error is None, "and it is not treated as a failure")

    report = ep.extract_pending(engine, extractor=_never_called)
    check(report.skipped_no_attachments == 1,
          "the driver counts it as having nothing to parse",
          str(report.skipped_no_attachments))
    with engine.connect() as conn:
        again = conn.execute(select(messages.c.extracted_at).where(
            messages.c.id == row.id)).scalar()
    check(again is not None,
          "and MARKS it extracted, so the queue does not grow by one forever")


def test_watermark_overlap(tmp: Path) -> None:
    section("the watermark reads BACK, because a bare > loses mail")
    engine, store = fresh_db(), poller.BlobStore(tmp / "blobs")
    client = gc.MockGraphClient()
    report = poller.poll_once(engine, client, MAILBOX, store, now=NOW)

    newest = max(o.received_at for o in report.outcomes)
    check(report.watermark_advanced and report.watermark_after == newest,
          "the watermark lands on the newest message stored", str(report.watermark_after))

    # The window a SECOND poll opens must start before the watermark.
    second = poller.poll_once(engine, client, MAILBOX, store, now=NOW)
    check(second.window_from < poller._aware(newest),
          "the next window starts BEFORE it, by the overlap",
          f"{second.window_from} < {newest}")
    expected = poller._aware(newest) - poller.POLL_OVERLAP
    check(second.window_from == expected,
          "exactly one overlap back, not an arbitrary fudge", str(second.window_from))
    check(second.seen > 0, "so messages already stored ARE re-read", str(second.seen))
    check(second.stored == 0, "and cost nothing, because dedup catches them")

    # The failure mode the overlap exists for: two messages in the same second.
    twins = [
        dict(m, id=f"twin-{i}", internetMessageId=f"<twin{i}@x>",
             receivedDateTime="2026-09-01T10:00:00Z")
        for i, m in enumerate(gc.MOCK_MESSAGES[1:3])
    ]
    engine2, store2 = fresh_db(), poller.BlobStore(tmp / "blobs2")
    twin_client = gc.MockGraphClient(twins)
    r1 = poller.poll_once(engine2, twin_client, MAILBOX, store2, now=NOW)
    check(r1.stored == 2, "both same-second messages stored on the first pass", str(r1.stored))
    r2 = poller.poll_once(engine2, twin_client, MAILBOX, store2, now=NOW)
    check(r2.seen == 2 and r2.stored == 0,
          "and the next poll re-reads BOTH -- a strict > would have dropped one",
          f"seen={r2.seen} stored={r2.stored}")


class _ExplodingClient(gc.MockGraphClient):
    """Fails one message's attachment fetch. Everything else behaves."""

    def __init__(self, bad_message_id: str) -> None:
        super().__init__()
        self.bad = bad_message_id

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        if message_id == self.bad:
            raise gc.GraphError("simulated: attachment fetch failed")
        return super().get_attachment(message_id, attachment_id)


def test_mid_batch_failure_holds_the_watermark(tmp: Path) -> None:
    section("one bad message: recorded, stepped over, and the watermark waits")
    engine, store = fresh_db(), poller.BlobStore(tmp / "blobs")
    # Tainan sits in the middle of the batch by received_at.
    client = _ExplodingClient("msg-tainan-005")
    report = poller.poll_once(engine, client, MAILBOX, store, now=NOW)

    check(report.seen == 7, "the poll saw every message", str(report.seen))
    check(len(report.failed) == 1, "exactly one failed", str([o.graph_message_id for o in report.failed]))
    check(report.stored == 6, "and the other six were stored -- the poll did NOT stop",
          str(report.stored))

    with engine.connect() as conn:
        bad = conn.execute(select(messages).where(
            messages.c.graph_message_id == "msg-tainan-005")).one()
    check(bad.poll_error and "simulated" in bad.poll_error,
          "the failure is recorded against ITS OWN row", (bad.poll_error or "")[:48])

    failed_at = report.failed[0].received_at
    check(report.watermark_after is None or report.watermark_after < failed_at,
          "the watermark did NOT advance past the failure",
          f"watermark={report.watermark_after} failure={failed_at}")

    later = [o for o in report.outcomes if o.received_at > failed_at and o.stored]
    check(later, "messages AFTER the failure were still stored", str(len(later)))
    check(report.watermark_after != max(o.received_at for o in report.outcomes),
          "so the watermark is behind the newest stored message, deliberately")

    # The retry: a healthy client must pick the failure up and clear it.
    retry = poller.poll_once(engine, gc.MockGraphClient(), MAILBOX, store, now=NOW)
    with engine.connect() as conn:
        fixed = conn.execute(select(messages).where(
            messages.c.graph_message_id == "msg-tainan-005")).one()
    check(fixed.poll_error is None, "a later poll retries it and clears the error")
    check(fixed.attachment_count == 1, "and finally stores its attachment")
    check(retry.watermark_advanced, "only then does the watermark move past it")


def test_extraction_seam(tmp: Path) -> None:
    section("the seam: extraction runs over STORED rows, never over Graph")
    engine, store = fresh_db(), poller.BlobStore(tmp / "blobs")
    poller.poll_once(engine, gc.MockGraphClient(), MAILBOX, store, now=NOW)

    pending = ep.pending_messages(engine)
    check(len(pending) == 7, "everything polled is pending extraction", str(len(pending)))
    check(all(p["paths"] or p["attachment_count"] == 0 for p in pending),
          "each carries the STORED paths of its attachments")
    inprotex = next(p for p in pending if p["graph_message_id"] == "msg-inprotex-001")
    check(len(inprotex["paths"]) == 2,
          "a two-attachment message hands ingest_shipment BOTH paths")
    check(all(path.exists() for path in inprotex["paths"]),
          "and the paths point at real files on disk")
    check([p["received_at"] for p in pending] == sorted(p["received_at"] for p in pending),
          "pending work is ordered oldest-first, so a backlog applies in arrival order")

    # The driver marks progress; the poller is not involved.
    calls: list = []

    def fake_ingest(engine_, paths, **kwargs):
        calls.append(list(paths))
        return ingest_result()

    original = ep.ingest_module.ingest_shipment
    try:
        ep.ingest_module.ingest_shipment = fake_ingest
        report = ep.extract_pending(engine)
    finally:
        ep.ingest_module.ingest_shipment = original

    check(report.extracted == 6 and report.skipped_no_attachments == 1,
          "six with documents extracted, one empty message retired",
          report.summary())
    check(len(calls) == 6, "ingest_shipment called once per message, not per attachment",
          str(len(calls)))
    check(not ep.pending_messages(engine), "nothing is left pending afterwards")

    again = ep.extract_pending(engine)
    check(again.attempted == 0, "re-running extracts nothing -- extracted_at is the queue")

    rerun = ep.extract_pending(engine, include_extracted=True, limit=0)
    check(rerun.attempted == 0 and ep.pending_messages(engine, include_extracted=True),
          "--all re-opens the whole set, which is how a parser change is re-run")


def ingest_result():
    from ingest import IngestReport

    return IngestReport(shipment_id="ship-x", created=True)


def _never_called(*args, **kwargs):
    raise AssertionError("the extractor must not run for a message with no attachments")


# ---------------------------------------------------------------------------
# READ-ONLY, ASSERTED
# ---------------------------------------------------------------------------

#: Everything Graph could be asked to do that this project must never do.
FORBIDDEN_VERBS = ("post", "put", "patch", "delete", "options", "head", "request")

#: The four calls, as URL fragments. Any other Graph path in the module is a
#: fifth capability arriving without a decision.
ALLOWED_PATH_FRAGMENTS = ("/users/", "/messages", "/attachments",
                          "login.microsoftonline.com",
                          # The API root and the scope: no path of their own.
                          "graph.microsoft.com/v1.0", "graph.microsoft.com/.default")

#: Graph paths that would be writes, or reads this pipeline has no business
#: making, spelled out so the failure NAMES what was attempted.
FORBIDDEN_FRAGMENTS = (
    "/sendMail", "/move", "/copy", "/forward", "/reply", "/replyAll",
    "/mailFolders", "/subscriptions", "/delta", "/users/me", "/createReply",
)


def _docstring_nodes(tree: ast.AST) -> set:
    """
    The id() of every string node that is a docstring.

    Excluded from the scans below, and the reason is the whole point of this
    test: `graph_client`'s docstring SAYS "No folder listing... needs
    `/mailFolders/{id}`" and "nothing here touches `os.environ`". A text search
    cannot tell a prohibition from a violation, and failing on the sentence that
    documents the rule is a check measuring a proxy instead of the thing
    (RUNBOOK section 8 lesson 12). These scans read code.
    """
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            out.add(id(body[0].value))
    return out


def _code_strings(tree: ast.AST) -> list[str]:
    """Every string literal that is NOT a docstring."""
    skip = _docstring_nodes(tree)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in skip]


def _reads_environ(tree: ast.AST) -> list[str]:
    """`os.environ`, `os.getenv`, `environ[...]` -- as CODE, not as prose."""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
            hits.append(f"line {node.lineno}: .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in ("environ", "getenv"):
            hits.append(f"line {node.lineno}: {node.id}")
    return hits


def test_graph_surface_is_read_only(tmp: Path) -> None:
    section("AST: the Graph client can only GET, and only the four calls")
    source = (HERE / "graph_client.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    verbs: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_node = node.func
        name = (func_node.attr if isinstance(func_node, ast.Attribute)
                else getattr(func_node, "id", ""))
        if name.lower() in FORBIDDEN_VERBS:
            verbs.append(f"line {node.lineno}: {name}()")
    check(not verbs, "no HTTP verb but GET is called anywhere in graph_client.py",
          str(verbs or "none"))

    literals = _code_strings(tree)
    bad = sorted({lit for lit in literals
                  for frag in FORBIDDEN_FRAGMENTS if frag.lower() in lit.lower()})
    check(not bad, "and no write-shaped or out-of-scope Graph path appears", str(bad or "none"))

    urls = [lit for lit in literals if "graph.microsoft.com" in lit or lit.startswith("/")]
    stray = [u for u in urls
             if not any(frag in u for frag in ALLOWED_PATH_FRAGMENTS)
             and u not in ("/", "")]
    check(not stray, "every Graph URL fragment belongs to one of the four calls",
          str(stray or "none"))

    # The interface itself: adding a method must be a deliberate act, so the
    # abstract surface is pinned by count as well as by name.
    surface = {n.name for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and any(isinstance(d, ast.Attribute) and d.attr == "abstractmethod"
                       for d in n.decorator_list)}
    check(surface == {"list_messages", "get_message", "list_attachments", "get_attachment"},
          "the abstract interface is exactly the four methods", str(sorted(surface)))

    check("import requests" in source and source.count("session.get(") == 1,
          "there is exactly ONE place an HTTP request is made")

    # And the poller must not reach around the interface to Graph directly.
    poller_src = (HERE / "poller.py").read_text(encoding="utf-8")
    check("graph.microsoft.com" not in poller_src and "requests" not in poller_src,
          "the poller never touches Graph or HTTP itself -- only the interface")
    check("import config" not in poller_src.split("def main")[0],
          "and the module body does not reach for config -- it is handed a client")

    # THE SCAN MUST BE ABLE TO FAIL. A check whose failing path has never been
    # exercised is not yet a check (RUNBOOK section 8 lesson 18), and a
    # read-only assertion that silently stopped matching would be the worst
    # possible thing to be wrong about. Three violations, one per rule.
    VIOLATION = chr(10).join([
        '"""A docstring naming /sendMail and os.environ, which is FINE."""',
        "import requests",
        "def send(token):",
        "    return requests.post(GRAPH_ROOT + '/users/x/sendMail')",
        "def folders(token):",
        "    return requests.get('https://graph.microsoft.com/v1.0/me/mailFolders')",
        "def leak():",
        "    return os.environ['GRAPH_CLIENT']",
    ])

    vtree = ast.parse(VIOLATION)
    vverbs = [n.func.attr for n in ast.walk(vtree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr.lower() in FORBIDDEN_VERBS]
    check("post" in vverbs, "the verb scan CATCHES a requests.post()", str(vverbs))
    vstrings = _code_strings(vtree)
    vbad = [lit for lit in vstrings
            for frag in FORBIDDEN_FRAGMENTS if frag.lower() in lit.lower()]
    check(len(vbad) >= 2, "the path scan CATCHES /sendMail and /mailFolders in code",
          str(sorted(set(vbad))))
    check(_reads_environ(vtree), "the environment scan CATCHES os.environ in code",
          str(_reads_environ(vtree)))
    check(not [lit for lit in _code_strings(vtree) if "FINE" in lit],
          "while the docstring naming those same terms is correctly IGNORED")
    for module in ("graph_client.py", "extract_pending.py", "poller.py"):
        hits = _reads_environ(ast.parse((HERE / module).read_text(encoding="utf-8")))
        # `poller` calls os.path.expandvars for the store root, which is a
        # filesystem path and not configuration -- `.environ` / `getenv` are
        # what this forbids, and GraphConfig owns every GRAPH_* value.
        check(not hits, f"{module} reads no environment variable directly",
              str(hits or "none"))


def test_client_selection(tmp: Path) -> None:
    section("GRAPH_CLIENT selects the implementation; no code change to switch")
    from types import SimpleNamespace

    mock_cfg = SimpleNamespace(is_mock=True, client_kind="mock")
    real_cfg = SimpleNamespace(
        is_mock=False, client_kind="real", mailbox="m@x", tenant_id="t",
        client_id="c", cert_path=Path("k"), cert_public_path=Path("c"),
        cert_thumbprint="T",
    )
    check(isinstance(gc.build_graph_client(mock_cfg), gc.MockGraphClient),
          "mock config builds the mock client")
    check(isinstance(gc.build_graph_client(real_cfg), gc.RealGraphClient),
          "real config builds the real client")
    check(gc.MockGraphClient.kind == "mock" and gc.RealGraphClient.kind == "real",
          "each reports which it is, so a row can record what wrote it")
    check(issubclass(gc.MockGraphClient, gc.GraphClient)
          and issubclass(gc.RealGraphClient, gc.GraphClient),
          "both implement the same narrow interface")


def test_retry_after(tmp: Path) -> None:
    section("429/503 honour Retry-After, bounded, then give up")
    check(gc._retry_after_seconds({"Retry-After": "7"}, 1) == 7.0,
          "a server-supplied Retry-After is honoured", "7s")
    check(gc._retry_after_seconds({"Retry-After": "9999"}, 1) == gc.MAX_RETRY_AFTER_SECONDS,
          "but capped -- a poller that waits an hour looks identical to a hung one",
          f"{gc.MAX_RETRY_AFTER_SECONDS}s")
    check(gc._retry_after_seconds({}, 3) == 8.0,
          "with exponential backoff when the header is absent", "2^3")
    check(gc._retry_after_seconds({"Retry-After": "not-a-number"}, 2) == 4.0,
          "and a malformed header falls back rather than raising")

    class _Resp:
        def __init__(self, status, headers=None):
            self.status_code, self.headers = status, (headers or {})
            self.text = "throttled"

        def json(self):
            return {"value": []}

    class _Session:
        def __init__(self, statuses):
            self.statuses, self.calls = list(statuses), 0

        def get(self, *a, **k):
            self.calls += 1
            return _Resp(self.statuses.pop(0) if self.statuses else 200)

    from types import SimpleNamespace
    cfg = SimpleNamespace(mailbox="m@x", tenant_id="t", client_id="c",
                          cert_path=Path("k"), cert_public_path=Path("c"),
                          cert_thumbprint="T")

    session = _Session([429, 503, 200])
    client = gc.RealGraphClient(cfg, session=session)
    client._token = "tok"  # skip MSAL; this test is about the retry loop
    original_sleep = gc.time.sleep
    gc.time.sleep = lambda _s: None
    try:
        payload = client._get_json("https://graph.microsoft.com/v1.0/users/m@x/messages")
    finally:
        gc.time.sleep = original_sleep
    check(payload == {"value": []} and session.calls == 3,
          "it retries through 429 then 503 and succeeds", f"{session.calls} calls")

    session = _Session([429] * 10)
    client = gc.RealGraphClient(cfg, session=session)
    client._token = "tok"
    gc.time.sleep = lambda _s: None
    try:
        raised = ""
        try:
            client._get_json("https://graph.microsoft.com/v1.0/users/m@x/messages")
        except gc.GraphError as exc:
            raised = str(exc)
    finally:
        gc.time.sleep = original_sleep
    check(raised and session.calls == gc.MAX_RETRIES,
          "and gives up after a BOUNDED number of attempts rather than forever",
          f"{session.calls} calls, then: {raised[:40]}")


class _CountingClient(gc.MockGraphClient):
    """Counts calls, so a dry run can be PROVEN not to fetch content."""

    def __init__(self):
        super().__init__()
        self.listed = 0
        self.fetched = 0

    def list_attachments(self, message_id):
        self.listed += 1
        return super().list_attachments(message_id)

    def get_attachment(self, message_id, attachment_id):
        self.fetched += 1
        return super().get_attachment(message_id, attachment_id)


class _ExplodingListClient(gc.MockGraphClient):
    """Fails list_attachments for one message."""

    def __init__(self, bad_message_id: str) -> None:
        super().__init__()
        self.bad = bad_message_id

    def list_attachments(self, message_id):
        if message_id == self.bad:
            raise gc.GraphError("simulated: cannot list attachments")
        return super().list_attachments(message_id)


def test_cold_start_is_a_choice(tmp: Path) -> None:
    section("cold start: the first poll is chosen, not whatever is in there")
    engine, store = fresh_db(), poller.BlobStore(tmp / "blobs")
    client = gc.MockGraphClient()

    check(poller.read_watermark(engine, MAILBOX) is None,
          "a fresh database has no watermark")

    # The LIBRARY default is still the whole mailbox: skipping history silently
    # would be the worse error. The CLI is what refuses to do it unasked.
    everything = poller.poll_once(engine, client, MAILBOX, store, now=NOW)
    check(everything.window_from == poller.EPOCH,
          "with no watermark and no since, the window opens at EPOCH",
          str(everything.window_from))
    check(everything.seen == 7, "which is the entire mailbox", str(everything.seen))

    # `since` overrides, and that is how a first run is made small and chosen.
    engine2 = fresh_db()
    since = dt.datetime(2026, 8, 5, tzinfo=dt.timezone.utc)
    picked = poller.poll_once(engine2, client, MAILBOX, poller.BlobStore(tmp / "b2"),
                              now=NOW, since=since)
    check(picked.window_from == since, "since sets the window exactly",
          str(picked.window_from))
    check(picked.seen == 3 and picked.stored == 3,
          "and only messages at or after it are considered", f"seen={picked.seen}")
    check(all(o.received_at >= since.replace(tzinfo=None) for o in picked.outcomes),
          "nothing older leaks in")

    # `since` also overrides an EXISTING watermark, which is how an operator
    # re-reads a known period. Safe, because re-reading is deduped.
    back = poller.poll_once(engine2, client, MAILBOX, poller.BlobStore(tmp / "b2"),
                            now=NOW, since=poller.EPOCH)
    check(back.window_from == poller.EPOCH,
          "since wins over the watermark, not the other way round")
    check(back.stored == 4 and back.skipped == 3,
          "so older messages are picked up and known ones skipped",
          f"stored={back.stored} skipped={back.skipped}")


def test_max_messages_caps_one_run(tmp: Path) -> None:
    section("max_messages: a ceiling for one run, and the rest next time")
    engine, store = fresh_db(), poller.BlobStore(tmp / "blobs")
    client = gc.MockGraphClient()

    first = poller.poll_once(engine, client, MAILBOX, store, now=NOW, max_messages=3)
    check(first.seen == 3 and first.stored == 3, "exactly three processed", str(first.seen))
    check(first.truncated and first.available == 7,
          "the run is FLAGGED truncated -- a cap and an empty mailbox must not look alike",
          f"truncated={first.truncated} available={first.available}")

    order = [o.received_at for o in first.outcomes]
    check(order == sorted(order), "oldest first, so the cap takes the oldest three")
    check(first.watermark_after == max(order),
          "the watermark advances only across what was processed",
          str(first.watermark_after))

    # THE CAP AND THE OVERLAP INTERACT, and the test says how rather than
    # asserting a number that hides it. The cap counts messages CONSIDERED, not
    # messages stored; the next window opens at the watermark minus the overlap,
    # so it re-reads its own boundary message and stores one fewer than the cap.
    # That is the overlap doing its job -- a capped run that skipped its boundary
    # would have exactly the same gap a bare `>` produces.
    second = poller.poll_once(engine, client, MAILBOX, store, now=NOW, max_messages=3)
    check(second.seen == 3, "the next run considers three again", str(second.seen))
    check(second.stored == 2 and second.skipped == 1,
          "storing two, because the overlap re-read the boundary message",
          f"stored={second.stored} skipped={second.skipped}")
    check(second.outcomes[0].received_at == first.watermark_after,
          "and the re-read one is exactly the message the watermark landed on",
          str(second.outcomes[0].received_at))

    third = poller.poll_once(engine, client, MAILBOX, store, now=NOW, max_messages=3)
    check(not third.truncated, "and the final run is not truncated")
    check(counts(engine)["messages"] == 7,
          "three capped runs land the same seven messages as one uncapped run",
          str(counts(engine)))


def test_dry_run_touches_nothing(tmp: Path) -> None:
    section("dry run: the cheap look before any bytes move")
    engine, store = fresh_db(), poller.BlobStore(tmp / "blobs")
    client = _CountingClient()

    report = poller.poll_once(engine, client, MAILBOX, store, now=NOW, dry_run=True)
    check(report.dry_run, "the report says it was a dry run")
    check(report.seen == 7, "it reports every message it would consider", str(report.seen))

    check(counts(engine) == {"messages": 0, "attachments": 0, "links": 0},
          "NOTHING was written to the database", str(counts(engine)))
    check(not (tmp / "blobs").exists() or not list((tmp / "blobs").rglob("*")),
          "no bytes were written to the store")
    check(poller.read_watermark(engine, MAILBOX) is None,
          "and the watermark was NOT created -- a dry run cannot masquerade as a poll")

    check(client.fetched == 0,
          "get_attachment was never called: metadata only", str(client.fetched))
    check(client.listed == 7, "list_attachments was, once per message", str(client.listed))

    preview = {o.graph_message_id: o for o in report.outcomes}
    inprotex = preview["msg-inprotex-001"]
    check(inprotex.from_address == "shipping@inprotex.example",
          "the preview carries the sender", inprotex.from_address)
    check("PO#1662" in inprotex.subject, "and the subject", inprotex.subject[:40])
    check(len(inprotex.attachment_preview) == 2, "and every attachment")
    check(all(a["size"] > 0 for a in inprotex.attachment_preview),
          "with real sizes, so the transfer cost is known before paying it",
          str([a["size"] for a in inprotex.attachment_preview]))
    check(all(a["content_type"] for a in inprotex.attachment_preview),
          "and content types")
    check(preview["msg-symmetry-004"].attachment_preview == [],
          "a message with no attachments previews as empty, not as an error")

    # A dry run must survive a broken message: the mailbox you most want to look
    # at before touching is the one you already suspect.
    bad = poller.poll_once(fresh_db(), _ExplodingListClient("msg-tainan-005"), MAILBOX,
                           store, now=NOW, dry_run=True)
    check(bad.seen == 7 and len(bad.failed) == 1,
          "one unreadable message is reported, the rest still listed",
          f"seen={bad.seen} failed={len(bad.failed)}")

    real = poller.poll_once(engine, client, MAILBOX, store, now=NOW)
    check(real.stored == 7, "a real poll afterwards stores everything the dry run showed",
          str(real.stored))


def test_parse_since(tmp: Path) -> None:
    section("since accepts a date or a timestamp, always UTC")
    check(poller.parse_since("2026-09-01")
          == dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
          "a bare date is midnight UTC")
    check(poller.parse_since("2026-09-01T12:30:00Z")
          == dt.datetime(2026, 9, 1, 12, 30, tzinfo=dt.timezone.utc),
          "a Z-suffixed timestamp parses")
    check(poller.parse_since("2026-09-01T12:30:00+00:00").tzinfo is not None,
          "and an explicit offset stays aware")
    naive = poller.parse_since("2026-09-01T12:30:00")
    check(naive.tzinfo == dt.timezone.utc,
          "a naive value is INTERPRETED as UTC, not as local time -- local would "
          "shift the window by the machine's offset", str(naive))

def test_classifier_sees_vendor_filenames_not_hashes(tmp: Path) -> None:
    section("the classifier gets a NAME, not a SHA-256")
    import attachment_classifier as ac

    # Reproduce the real shape: bytes stored content-addressed, name kept apart.
    store = poller.BlobStore(tmp / "blobs")
    src = HERE / "FA26 7TH W600001 PO1721 FINAL INSPECTION REPORT.pdf"
    if not src.exists():
        check(False, "inspection-report fixture present", src.name)
        return
    sha, path = store.put(src.read_bytes(), suffix=".pdf")
    check(path.stem == sha, "the stored path IS a hash", path.name[:22])

    # WITHOUT the name: the filename layer has nothing to work with.
    blind = ac.classify_attachments([path], use_content_check=False)
    blind_item = (blind.selected + blind.excluded)[0]
    check(blind_item.display_name == path.name,
          "unnamed, the classifier falls back to the hash", blind_item.display_name[:20])
    check(blind_item.filename_hint != ac.DocType.INSPECTION_REPORT,
          "and the inspection-report filename rule CANNOT fire",
          str(blind_item.filename_hint))

    # WITH the name: the rule fires, as it did before the store existed.
    named = ac.classify_attachments([path], use_content_check=False,
                                    display_names={str(path): src.name})
    item = (named.selected + named.excluded)[0]
    check(item.display_name == src.name,
          "given the vendor filename, that is what it classifies on", item.display_name[:40])
    check(not any(c in item.display_name for c in "0123456789abcdef" * 0) and
          item.display_name != path.name,
          "the display name is NOT the hex path name")
    check(len(item.display_name) != 64 and not _looks_like_sha(item.display_name),
          "and is not a bare SHA-256", item.display_name[:40])
    check(item.filename_hint == ac.DocType.INSPECTION_REPORT,
          "so the inspection-report ban fires from the NAME again",
          str(item.filename_hint))

    # AND THE LABEL THE MODEL SEES. This is the half the first fix missed: the
    # filename rules were repaired while the content call still labelled each
    # attachment with `path.name`, so the one call allowed to weigh a filename
    # was shown a SHA-256. It flipped Symmetry's rollup from PACKING_LIST to
    # SHIPPING_ADVICE -- excluding it from packing-list duty AND from the rollup
    # preference, which can only rank documents that were selected.
    source = (HERE / "attachment_classifier.py").read_text(encoding="utf-8")
    call = source[source.index("def _classify_by_content"):] if         "def _classify_by_content" in source else source
    block = call[call.index("attachment(s) to classify"):
                 call.index("attachment(s) to classify") + 1200]
    check("item.display_name" in block and "item.path.name" not in block,
          "the content call labels attachments by display_name, never by path.name",
          "path.name absent" if "item.path.name" not in block else "STILL USES path.name")

    # The end-to-end shape: what extract_pending hands over.
    engine = fresh_db()
    poller.poll_once(engine, gc.MockGraphClient(), MAILBOX, store, now=NOW)
    pending = ep.pending_messages(engine)
    every = {n for row in pending for n in row.get("display_names", {}).values()}
    check(every and not any(_looks_like_sha(Path(n).stem) for n in every),
          "extract_pending passes real vendor filenames for every attachment",
          str(sorted(every)[:1]))
    for row in pending:
        for path_str, name in row.get("display_names", {}).items():
            check(_looks_like_sha(Path(path_str).stem) and not _looks_like_sha(Path(name).stem),
                  "each pair is (hashed path, vendor name)", f"{Path(path_str).name[:12]}.. <- {name[:28]}")
            break
        break


def _looks_like_sha(text: str) -> bool:
    return len(text) == 64 and all(c in "0123456789abcdef" for c in text.lower())


def test_doc_type_is_written_where_it_is_decided(tmp: Path) -> None:
    section("UNCLASSIFIED means never classified -- and stops meaning it on contact")
    from sqlalchemy import select as sa_select

    import ingest as ing

    engine, store = fresh_db(), poller.BlobStore(tmp / "blobs")
    poller.poll_once(engine, gc.MockGraphClient(), MAILBOX, store, now=NOW)

    with engine.connect() as conn:
        types = set(conn.execute(sa_select(attachments.c.doc_type)).scalars())
    check(types == {poller.UNCLASSIFIED},
          "after a poll every row is UNCLASSIFIED -- nothing has looked at them",
          str(types))

    # A verdict arriving for a row that ALREADY EXISTS must be stored. It used to
    # be written only on INSERT, so the poller's row won and seven real verdicts
    # were computed and dropped.
    sha = next(iter(conn.execute(sa_select(attachments.c.content_sha256)).scalars()
                    if False else []), None)
    with engine.connect() as conn:
        sha = conn.execute(sa_select(attachments.c.content_sha256)).scalars().first()
        path = conn.execute(sa_select(attachments.c.stored_uri)
                            .where(attachments.c.content_sha256 == sha)).scalar()

    class _Verdict:
        def __init__(self):
            self.doc_type = SimpleNamespace(value="packing_list")
            self.reason = "content says packing list"
            self.unreadable_reason = None

    with engine.begin() as conn:
        again = ing._upsert_attachment(conn, Path(path), _Verdict(), NOW)
    check(again == sha, "the same content still resolves to the same row")
    with engine.connect() as conn:
        row = conn.execute(attachments.select()
                           .where(attachments.c.content_sha256 == sha)).one()
    check(row.doc_type == "PACKING_LIST",
          "the verdict is WRITTEN over UNCLASSIFIED, not discarded", row.doc_type)
    check(row.doc_type_reason == "content says packing list",
          "with the reason that produced it")

    # And a later no-verdict pass must not erase it.
    with engine.begin() as conn:
        ing._upsert_attachment(conn, Path(path), None, NOW)
    with engine.connect() as conn:
        row = conn.execute(attachments.select()
                           .where(attachments.c.content_sha256 == sha)).one()
    check(row.doc_type == "PACKING_LIST",
          "a pass with no classifier cannot reset it to UNCLASSIFIED", row.doc_type)


def test_ingest_refuses_to_run_without_netsuite(tmp: Path) -> None:
    section("no NetSuite client is a CHOICE, not an accident that returns zero")
    import ingest as ing
    from netsuite_client import NetSuiteClient, NetSuiteConfig

    engine = fresh_db()
    raised = ""
    try:
        ing.ingest_shipment(engine, [], client=None)
    except ValueError as exc:
        raised = str(exc)
    check("no usable NetSuite client" in raised and "none supplied" in raised,
          "client=None is refused, naming what would have happened", raised[:70])

    raised = ""
    try:
        ing.ingest_shipment(engine, [], client=NetSuiteClient(mock_data={}))
    except ValueError as exc:
        raised = str(exc)
    check("mock client with no data" in raised,
          "and so is a mock client carrying nothing -- the ACTUAL live failure",
          raised[:70])

    # A mock WITH data resolves normally and is not refused.
    ok = True
    try:
        ing.ingest_shipment(engine, [], client=NetSuiteClient(mock_data={"1662": []}),
                            allow_no_netsuite=False)
    except ValueError:
        ok = False
    except Exception:  # noqa: BLE001 -- it gets past the guard, which is the point
        pass
    check(ok, "a mock WITH data is a legitimate offline fixture and passes the guard")

    # THE ROOT CAUSE: the constructor took a config in the account_id slot and
    # silently produced a mock client. That is what made the zero plausible.
    raised = ""
    try:
        NetSuiteClient(NetSuiteConfig(account_id="1321665-sb2", client_id="c",
                                      certificate_id="k", private_key_path=Path("k.pem")))
    except TypeError as exc:
        raised = str(exc)
    check("passes the config as `account_id`" in raised,
          "NetSuiteClient(config) positionally is REFUSED, not silently mocked",
          raised[:60])


def test_dropped_documents_are_rows_not_prose(tmp: Path) -> None:
    section("a usable packing list that is not parsed is recorded, not narrated")
    from sqlalchemy import select as sa_select

    from schema import shipment_sources

    engine, store = fresh_db(), poller.BlobStore(tmp / "blobs")
    poller.poll_once(engine, gc.MockGraphClient(), MAILBOX, store, now=NOW)

    # The Symmetry message carries three documents: a rollup, a carton detail and
    # a customs invoice. One becomes the shipment; the others must say so on a row.
    import json as _json

    import ingest as ing
    from netsuite_client import NetSuiteClient

    row = next(r for r in ep.pending_messages(engine)
               if r["graph_message_id"] == "msg-symmetry-003")
    calls = []

    class _Stub:
        def __getattr__(self, name):
            def f(*a, **k):
                calls.append(name)
                raise RuntimeError("stub: no extraction in this test")
            return f

    try:
        ing.ingest_shipment(engine, row["paths"], client=NetSuiteClient(mock_data={"x": []}),
                            extractor=_Stub(), display_names=row["display_names"], now=NOW)
    except Exception:  # noqa: BLE001 -- the stub stops extraction; the rows are the subject
        pass

    with engine.connect() as conn:
        sources = list(conn.execute(sa_select(
            shipment_sources.c.role, shipment_sources.c.exclusion_reason,
            shipment_sources.c.agreement_json)))
    if not sources:
        check(True, "extraction did not reach the source rows in this stubbed run (skipped)")
        return
    check(all(s.agreement_json for s in sources),
          "every source row records what the document WAS", str(len(sources)))
    named = [_json.loads(s.agreement_json).get("display_name", "") for s in sources]
    check(named and not any(_looks_like_sha(Path(n).stem) for n in named),
          "by vendor filename, not by hash", str(named[:2]))
    cross = [s for s in sources if s.role == "CROSS_CHECK"]
    check(all(s.exclusion_reason for s in cross) if cross else True,
          "and a cross-check says why its own lines were NOT proposed",
          (cross[0].exclusion_reason[:60] if cross else "no cross-checks here"))
    check(all(not _json.loads(s.agreement_json)["lines_proposed"] for s in sources
              if s.role != "PRIMARY"),
          "only the PRIMARY document's lines are proposed, and the rows say so")

def _cli_window(engine, store, argv, monkey_client):
    """Run poller.main with a stubbed poll_once and capture the window it chose."""
    seen = {}
    original = poller.poll_once

    def spy(engine_, client, mailbox, store_, **kw):
        seen.update(kw)
        seen["mailbox"] = mailbox
        return original(engine_, client, mailbox, store_, **kw)

    import config as config_module
    orig_from_env = config_module.GraphConfig.from_env
    orig_build = gc.build_graph_client
    try:
        poller.poll_once = spy
        config_module.GraphConfig.from_env = staticmethod(
            lambda *a, **k: SimpleNamespace(is_mock=True, client_kind="mock",
                                            mailbox=MAILBOX))
        gc.build_graph_client = lambda cfg=None: monkey_client
        import sqlalchemy
        real_create = sqlalchemy.create_engine
        sqlalchemy.create_engine = lambda *a, **k: engine
        try:
            poller.main(argv + ["--store", str(store.root)])
        finally:
            sqlalchemy.create_engine = real_create
    finally:
        poller.poll_once = original
        config_module.GraphConfig.from_env = orig_from_env
        gc.build_graph_client = orig_build
    return seen


def test_from_beginning_sets_the_window(tmp: Path) -> None:
    section("--from-beginning SETS the window; it used to only satisfy the guard")
    engine, store = fresh_db(), poller.BlobStore(tmp / "blobs")
    client = gc.MockGraphClient()

    # Establish a watermark, which is the condition under which the flag broke.
    poller.poll_once(engine, client, MAILBOX, store, now=NOW)
    mark = poller.read_watermark(engine, MAILBOX)
    check(mark is not None, "a watermark exists", str(mark))

    beginning = _cli_window(engine, store, ["--dry-run", "--from-beginning"], client)
    explicit = _cli_window(engine, store, ["--dry-run", "--since", "2000-01-01"], client)

    # THE CHECK THAT WOULD HAVE CAUGHT IT. With a watermark present the two must
    # agree; before the fix `--from-beginning` left `since=None` and the window
    # came from the watermark, so it silently read a narrower range and hid a
    # message on a live dry run.
    check(beginning["since"] == explicit["since"] == poller.EPOCH,
          "--from-beginning and --since <epoch> choose the SAME window",
          f"{beginning['since']} vs {explicit['since']}")
    check(beginning["since"] is not None,
          "and it is not None -- None is what made it fall back to the watermark")
    check(poller._aware(mark) > beginning["since"],
          "the window really is older than the watermark it would otherwise use",
          f"{beginning['since']} < {mark}")

    # --since wins when both are given: the more specific instruction.
    both = _cli_window(engine, store,
                       ["--dry-run", "--from-beginning", "--since", "2026-08-05"], client)
    check(both["since"] == dt.datetime(2026, 8, 5, tzinfo=dt.timezone.utc),
          "--since wins over --from-beginning rather than the command failing",
          str(both["since"]))


def test_since_overrides_on_every_path(tmp: Path) -> None:
    section("--since overrides, watermark or not")
    engine, store = fresh_db(), poller.BlobStore(tmp / "blobs")
    client = gc.MockGraphClient()
    since = dt.datetime(2026, 8, 5, tzinfo=dt.timezone.utc)

    cold = poller.poll_once(engine, client, MAILBOX, store, now=NOW, since=since)
    check(cold.window_from == since, "with NO watermark, since sets the window",
          str(cold.window_from))

    warm = poller.poll_once(engine, client, MAILBOX, store, now=NOW, since=since)
    check(warm.window_from == since,
          "with a watermark, since STILL sets the window -- it is not a floor",
          str(warm.window_from))

    back = poller.poll_once(engine, client, MAILBOX, store, now=NOW, since=poller.EPOCH)
    check(back.window_from == poller.EPOCH,
          "and it can reach back BEHIND the watermark, which is the point")
    check(back.seen > warm.seen,
          "seeing strictly more than the watermark window would",
          f"{back.seen} > {warm.seen}")


def test_max_messages_cannot_drag_the_watermark_backwards(tmp: Path) -> None:
    section("--max-messages with an explicit window: the interaction, stated")
    engine, store = fresh_db(), poller.BlobStore(tmp / "blobs")
    client = gc.MockGraphClient()

    poller.poll_once(engine, client, MAILBOX, store, now=NOW)
    ahead = poller.read_watermark(engine, MAILBOX)

    # Re-read from the beginning, capped. The oldest three are re-processed, and
    # the newest of THEM is far behind the current watermark.
    capped = poller.poll_once(engine, client, MAILBOX, store, now=NOW,
                              since=poller.EPOCH, max_messages=3)
    check(capped.seen == 3 and capped.truncated, "three processed, flagged truncated",
          f"seen={capped.seen} truncated={capped.truncated}")
    after = poller.read_watermark(engine, MAILBOX)
    check(after == ahead,
          "the watermark did NOT move backwards to the capped batch's newest",
          f"{ahead} -> {after}")
    check(not capped.watermark_advanced, "and the report says it did not advance")

    # THE CONSEQUENCE, asserted rather than left to be discovered: a capped
    # backfill behind the watermark makes no forward progress. Capping is for
    # sizing a FIRST run; re-reading history is `--since` without a cap.
    second = poller.poll_once(engine, client, MAILBOX, store, now=NOW,
                              since=poller.EPOCH, max_messages=3)
    check([o.graph_message_id for o in second.outcomes]
          == [o.graph_message_id for o in capped.outcomes],
          "a repeat of the same capped command re-reads the SAME three, forever",
          str(len(second.outcomes)))
    check(second.stored == 0, "storing nothing, because dedup catches them")

def test_reforward_does_not_create_a_second_shipment(tmp: Path) -> None:
    section("three forwards of one shipment: does it cost 1x or 3x?")
    from sqlalchemy import func as sa_func
    from sqlalchemy import select as sa_select

    import attachment_classifier as ac
    import document_parsers as dp
    import ingest as ing
    from netsuite_client import NetSuiteClient
    from schema import shipments

    engine, store = fresh_db(), poller.BlobStore(tmp / "blobs")
    poller.poll_once(engine, gc.MockGraphClient(), MAILBOX, store, now=NOW)

    # The mock's re-forward: msg-legendz-006 carries the SAME bytes as
    # msg-legendz-002 under a different filename, which is how Paula forwards.
    rows = {r["graph_message_id"]: r for r in ep.pending_messages(engine)}
    first, forward = rows["msg-legendz-002"], rows["msg-legendz-006"]
    check(len(first["paths"]) == 1 and len(forward["paths"]) == 1,
          "both messages carry one attachment")
    check(first["paths"][0] == forward["paths"][0],
          "and it is the SAME stored file, because the store is content-addressed",
          first["paths"][0].name[:20])

    sha = ing.sha256_file(first["paths"][0])
    check(sha == ing.sha256_file(forward["paths"][0]),
          "identical bytes -- which is the CONDITION the dedup depends on", sha[:16])

    # Canned classification and a counted parser, so this stays offline. The
    # subject is the ORDER of operations in ingest_shipment, not the classifier.
    verdict = ac.AttachmentClassification(
        path=first["paths"][0], doc_type=ac.DocType.PACKING_LIST,
        has_size_breakdown=True, reason="stub", method="stub",
        display_name="Legendz PL0801- 26ctns.xlsx",
    )
    canned = ac.ClassificationResult(selected=[verdict])
    parses = []
    orig_classify = ac.classify_attachments
    orig_parse = dp.parse_shipment_email
    try:
        ac.classify_attachments = lambda paths, **kw: canned
        def counted(paths, **kw):
            parses.append(list(paths))
            raise AssertionError("parse_shipment_email must NOT run for a re-forward")
        dp.parse_shipment_email = counted

        with engine.begin() as conn:
            # A VENDOR_EMAIL shipment needs its message_id: the schema's
            # provenance constraint refuses a primary_attachment_sha without one.
            conn.execute(shipments.insert(), {
                "id": "ship-1", "origin": "VENDOR_EMAIL", "message_id": first["id"],
                "primary_attachment_sha": sha, "parser": "stub",
                "doc_needs_review": False, "needs_manual_entry": False,
                "line_count": 0, "unit_total": 0, "created_by": "test",
                "created_at": NOW,
            })

        report = ing.ingest_shipment(
            engine, forward["paths"], client=NetSuiteClient(mock_data={"1657": []}),
            display_names=forward["display_names"], now=NOW)
    finally:
        ac.classify_attachments = orig_classify
        dp.parse_shipment_email = orig_parse

    check(not parses,
          "the forward NEVER reached the parser -- it costs 1x, not 2x", str(len(parses)))
    check(not report.created and report.shipment_id == "ship-1",
          "it resolved to the EXISTING shipment", str(report.shipment_id))
    check("already ingested" in report.reason,
          "and says so, naming the content", report.reason[:52])

    with engine.connect() as conn:
        n = conn.execute(sa_select(sa_func.count()).select_from(shipments)).scalar()
    check(n == 1, "exactly ONE shipment exists for the two messages", str(n))

    # THE CONDITION, asserted rather than assumed: the short-circuit keys on the
    # PRIMARY document's content hash, and on nothing else. A forward whose bytes
    # were re-encoded in transit hashes differently, finds no match, and becomes
    # its OWN shipment and its own extraction -- so "three forwards cost 1x"
    # holds only while the bytes are identical.
    with engine.connect() as conn:
        found = conn.execute(
            sa_select(shipments.c.id)
            .where(shipments.c.primary_attachment_sha == sha)
            .where(shipments.c.superseded_by_shipment_id.is_(None))).scalar()
        missed = conn.execute(
            sa_select(shipments.c.id)
            .where(shipments.c.primary_attachment_sha == "0" * 64)
            .where(shipments.c.superseded_by_shipment_id.is_(None))).scalar()
    check(found == "ship-1", "the identical hash finds the existing shipment", str(found))
    check(missed is None,
          "a different hash finds NOTHING -- so re-encoded bytes would re-ingest",
          str(missed))
    check(forward["display_names"] != first["display_names"],
          "and the filenames DIFFER, proving the dedup is content, not name",
          str(list(forward["display_names"].values()))[:46])


def main() -> int:
    print("=" * 78)
    print("MAILBOX INTAKE TESTS -- poller, blob store, extraction seam")
    print("=" * 78)
    print()
    print("Offline: the mock serves the real vendor corpus. No mailbox, no token.")

    REGISTERED = (
        test_mock_envelopes,
        test_blob_store_is_content_addressed,
        test_poll_stores_and_is_idempotent,
        test_duplicate_content_stores_once_joins_twice,
        test_message_with_no_attachments,
        test_watermark_overlap,
        test_mid_batch_failure_holds_the_watermark,
        test_extraction_seam,
        test_cold_start_is_a_choice,
        test_max_messages_caps_one_run,
        test_dry_run_touches_nothing,
        test_parse_since,
        test_classifier_sees_vendor_filenames_not_hashes,
        test_doc_type_is_written_where_it_is_decided,
        test_ingest_refuses_to_run_without_netsuite,
        test_dropped_documents_are_rows_not_prose,
        test_from_beginning_sets_the_window,
        test_since_overrides_on_every_path,
        test_max_messages_cannot_drag_the_watermark_backwards,
        test_reforward_does_not_create_a_second_shipment,
        test_graph_surface_is_read_only,
        test_client_selection,
        test_retry_after,
    )

    # A test registered twice runs twice and its checks are counted twice. That
    # is how test_schema reported 115 for 105 distinct checks until a pytest run,
    # which collects each function once, disagreed with the script (RUNBOOK
    # section 8 lessons 18 and 19). Cheap to assert, so the class cannot recur.
    dupes = sorted({f.__name__ for f in REGISTERED if REGISTERED.count(f) > 1})
    check(not dupes, "no test is registered more than once", str(dupes or "none"))

    import tempfile

    for fn in REGISTERED:
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td))

    failed = [name for ok, name, _ in _results if not ok]
    print()
    print("=" * 78)
    print(f"{len(_results) - len(failed)}/{len(_results)} checks passed")
    for name in failed:
        print(f"  FAILED: {name}")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
