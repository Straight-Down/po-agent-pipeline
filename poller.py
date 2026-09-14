"""
Mailbox intake: read the shipments mailbox, store what arrived, stop.

This job writes `messages`, `attachments` and `message_attachments`. It does not
classify, parse, match or propose. That is not tidiness -- it is the boundary
that makes a parser change cheap: the mailbox is read once, the bytes are
durable, and `extract_pending.py` re-runs extraction over stored rows without
touching Graph. A single command that read mail AND extracted would make a
parser bug cost a re-download, and would take the re-extraction cost decision
out of the operator's hands.

## The content-addressed store

Attachment bytes are written to a file named for their SHA-256 -- the same hash
that is already `attachments.content_sha256`, the primary key. That is not a
coincidence to exploit but the reason it works: dedup axis 2 was ALREADY content
identity, so a content-addressed filename makes the store agree with the database
by construction. The same PDF arriving twice under two filenames is one file on
disk, one `attachments` row, and two `message_attachments` rows.

The path goes in `attachments.stored_uri`, which `ingest_shipment` already reads
paths from -- so nothing downstream had to change to accept mail-sourced bytes.

Default location is OUTSIDE this OneDrive-synced folder, for the same reason the
private keys are: vendor documents carry supplier pricing and named contacts, and
a synced folder is shared by whoever the folder is shared with.

## Idempotency, and the two ids that are not interchangeable

`graph_message_id` is the dedup key and is unique in the schema.
`internetMessageId` is stored ALONGSIDE it and is not a substitute: the Graph id
is per-mailbox and changes when a message is moved between folders, while the
internet id is assigned by the sending server and survives the move. Neither
alone is reliable -- the Graph id breaks on a move, the internet id can be
duplicated by a forwarder that reuses it -- so both are recorded, and the one
that dedupes is the one the schema enforces.

## The watermark

`poll_state.last_received_at`, read back with a deliberate OVERLAP rather than a
bare `>`. See `POLL_OVERLAP`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence, Union

import graph_client as gc
from schema import attachments, message_attachments, messages, poll_state

logger = logging.getLogger(__name__)

#: How far BACK from the stored watermark each poll re-reads.
#:
#: A bare `receivedDateTime > watermark` loses mail, silently, in two ordinary
#: situations: two messages delivered inside the same second (the watermark
#: lands on one of them and the other is never seen again), and any clock skew
#: between Graph's stamp and this process. Nothing detects either, because
#: nothing knows the message existed.
#:
#: The overlap costs a handful of already-stored messages per poll, and
#: `graph_message_id` dedup makes re-reading them free -- no parse, no tokens,
#: one indexed SELECT each. Five minutes against a 15-30 minute cadence is
#: generous on purpose: the cost of overlapping is bounded and visible, the cost
#: of missing a shipment is neither.
POLL_OVERLAP = dt.timedelta(minutes=5)

#: Where the first poll starts when no watermark exists. Not "now": a first run
#: against a mailbox with history should read it, not skip it.
EPOCH = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)

#: Bytes live outside the OneDrive-synced project folder, beside the keys.
DEFAULT_STORE = Path(os.path.expandvars(r"%USERPROFILE%\.po-agent\attachments"))

#: What the poller writes for `attachments.doc_type`. It stores bytes and
#: classifies nothing; saying 'OTHER' would be recording a conclusion it has no
#: standing to reach. See migration 0005.
UNCLASSIFIED = "UNCLASSIFIED"


class BlobStore:
    """
    Content-addressed file store: the filename IS the content hash.

    Sharded one level on the first two hex characters, because a single
    directory holding tens of thousands of files is slow to list on Windows and
    unpleasant to work with by hand.

    Writes are idempotent by construction -- the same bytes produce the same
    path -- so a re-poll overwrites nothing and a half-written file from a crash
    is replaced rather than trusted. The write goes to a temporary name and is
    renamed into place, so a reader never sees a partial file under a name that
    claims to be a complete one.
    """

    def __init__(self, root: Union[str, Path, None] = None) -> None:
        self.root = Path(root) if root else DEFAULT_STORE

    def path_for(self, sha: str, suffix: str = "") -> Path:
        return self.root / sha[:2] / f"{sha}{suffix}"

    def put(self, data: bytes, suffix: str = "") -> tuple[str, Path]:
        """Store bytes, return (sha256, path). Existing content is left alone."""
        sha = hashlib.sha256(data).hexdigest()
        path = self.path_for(sha, suffix)
        if path.exists() and path.stat().st_size == len(data):
            return sha, path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return sha, path


@dataclass
class MessageOutcome:
    """What happened to one message. A failure is data, not an exception."""

    graph_message_id: str
    received_at: dt.datetime
    stored: bool = False
    skipped_existing: bool = False
    attachments_new: int = 0
    attachments_linked: int = 0
    error: str = ""


@dataclass
class PollReport:
    """What one poll did, in enough detail to explain itself without the DB."""

    mailbox: str = ""
    client_kind: str = ""
    window_from: Optional[dt.datetime] = None
    watermark_before: Optional[dt.datetime] = None
    watermark_after: Optional[dt.datetime] = None
    watermark_advanced: bool = False
    outcomes: list = field(default_factory=list)

    @property
    def seen(self) -> int:
        return len(self.outcomes)

    @property
    def stored(self) -> int:
        return sum(1 for o in self.outcomes if o.stored)

    @property
    def skipped(self) -> int:
        return sum(1 for o in self.outcomes if o.skipped_existing)

    @property
    def failed(self) -> list:
        return [o for o in self.outcomes if o.error]

    def summary(self) -> str:
        return (f"{self.seen} seen, {self.stored} stored, {self.skipped} already had, "
                f"{len(self.failed)} failed | watermark "
                f"{'->' if self.watermark_advanced else 'held at'} {self.watermark_after}")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _naive(value: dt.datetime) -> dt.datetime:
    """Stored naive-UTC, matching every other DateTime column in this schema."""
    return _aware(value).astimezone(dt.timezone.utc).replace(tzinfo=None)


def read_watermark(engine, mailbox: str) -> Optional[dt.datetime]:
    with engine.connect() as conn:
        row = conn.execute(
            poll_state.select().where(poll_state.c.mailbox == mailbox)
        ).one_or_none()
    return row.last_received_at if row else None


def poll_once(
    engine,
    client: gc.GraphClient,
    mailbox: str,
    store: Optional[BlobStore] = None,
    *,
    overlap: dt.timedelta = POLL_OVERLAP,
    now: Optional[dt.datetime] = None,
) -> PollReport:
    """
    One pass. Reads from the watermark minus the overlap, stores, then advances.

    Every message is handled in its OWN transaction, so one bad message cannot
    roll back the ones that worked, and its failure is recorded against its own
    row rather than raised.
    """
    store = store or BlobStore()
    now = now or _utcnow()
    report = PollReport(mailbox=mailbox, client_kind=getattr(client, "kind", "?"))

    watermark = read_watermark(engine, mailbox)
    report.watermark_before = watermark
    window_from = (_aware(watermark) - overlap) if watermark else EPOCH
    report.window_from = window_from

    for envelope in client.list_messages(window_from):
        outcome = _handle_message(engine, client, store, mailbox, envelope, now)
        report.outcomes.append(outcome)

    _advance_watermark(engine, mailbox, report, now)
    logger.info("poll: %s", report.summary())
    return report


def _handle_message(engine, client, store, mailbox, envelope, now) -> MessageOutcome:
    graph_id = str(envelope.get("id") or "")
    received = _naive(gc._parse_graph_time(envelope["receivedDateTime"]))
    outcome = MessageOutcome(graph_message_id=graph_id, received_at=received)

    try:
        with engine.begin() as conn:
            existing = conn.execute(
                messages.select()
                .with_only_columns(messages.c.id, messages.c.poll_error)
                .where(messages.c.graph_message_id == graph_id)
            ).one_or_none()

            # DEDUP. A row with no `poll_error` is complete -- nothing to redo.
            # A row WITH one is a previous failure being retried, which is why
            # the marker is cleared and the attachments re-fetched below.
            if existing and not existing.poll_error:
                outcome.skipped_existing = True
                return outcome

            message_id = existing.id if existing else str(uuid.uuid4())
            sender = (envelope.get("from") or {}).get("emailAddress") or {}
            declared = client.list_attachments(graph_id)
            row = {
                "graph_message_id": graph_id,
                "internet_message_id": envelope.get("internetMessageId"),
                "mailbox": mailbox,
                "subject": (envelope.get("subject") or "")[:1000],
                "from_address": (sender.get("address") or "")[:320] or None,
                "sent_at": (_naive(gc._parse_graph_time(envelope["sentDateTime"]))
                            if envelope.get("sentDateTime") else None),
                "received_at": received,
                "ingested_at": now,
                "folder_id": envelope.get("parentFolderId"),
                "attachment_count": len(declared),
                "poll_error": None,
            }
            if existing:
                conn.execute(messages.update()
                             .where(messages.c.id == message_id).values(**row))
            else:
                conn.execute(messages.insert(), {"id": message_id, **row})

            for meta in declared:
                new_content, linked = _store_attachment(
                    conn, client, store, message_id, graph_id, meta, now
                )
                outcome.attachments_new += int(new_content)
                outcome.attachments_linked += int(linked)
            outcome.stored = True
    except Exception as exc:  # noqa: BLE001 -- a failure here is data, not a crash
        outcome.error = f"{type(exc).__name__}: {exc}"[:1000]
        logger.warning("poll: message %s failed: %s", graph_id, outcome.error)
        _record_failure(engine, mailbox, graph_id, envelope, received, now, outcome.error)
    return outcome


def _store_attachment(conn, client, store, message_id, graph_id, meta, now):
    """Fetch, store by content, upsert the row, join it to this message."""
    data = client.get_attachment(graph_id, meta["id"])
    name = str(meta.get("name") or "attachment")
    suffix = Path(name).suffix.lower()[:16]
    sha, path = store.put(data, suffix=suffix)

    known = conn.execute(
        attachments.select().with_only_columns(attachments.c.content_sha256)
        .where(attachments.c.content_sha256 == sha)
    ).scalar()
    new_content = known is None
    if new_content:
        conn.execute(attachments.insert(), {
            "content_sha256": sha,
            "byte_size": len(data),
            # The poller classifies NOTHING. See migration 0005.
            "doc_type": UNCLASSIFIED,
            "banned_as_data_source": False,
            "stored_uri": str(path),
            "first_seen_at": now,
        })

    joined = conn.execute(
        message_attachments.select()
        .with_only_columns(message_attachments.c.id)
        .where(message_attachments.c.message_id == message_id)
        .where(message_attachments.c.content_sha256 == sha)
    ).scalar()
    if joined is None:
        conn.execute(message_attachments.insert(), {
            "id": str(uuid.uuid4()),
            "message_id": message_id,
            "content_sha256": sha,
            "filename": name[:500],
        })
    return new_content, joined is None


def _record_failure(engine, mailbox, graph_id, envelope, received, now, error) -> None:
    """
    Keep the failure, in its own transaction.

    The message row is written even when the attachment fetch failed, so the
    failure is visible and the message is re-tried on the next poll rather than
    being invisible until someone notices a shipment never arrived.
    """
    try:
        with engine.begin() as conn:
            exists = conn.execute(
                messages.select().with_only_columns(messages.c.id)
                .where(messages.c.graph_message_id == graph_id)
            ).scalar()
            if exists:
                conn.execute(messages.update()
                             .where(messages.c.id == exists)
                             .values(poll_error=error, ingested_at=now))
            else:
                sender = (envelope.get("from") or {}).get("emailAddress") or {}
                conn.execute(messages.insert(), {
                    "id": str(uuid.uuid4()),
                    "graph_message_id": graph_id,
                    "internet_message_id": envelope.get("internetMessageId"),
                    "mailbox": mailbox,
                    "subject": (envelope.get("subject") or "")[:1000],
                    "from_address": (sender.get("address") or "")[:320] or None,
                    "received_at": received,
                    "ingested_at": now,
                    "folder_id": envelope.get("parentFolderId"),
                    "attachment_count": 0,
                    "poll_error": error,
                })
    except Exception:  # noqa: BLE001
        logger.exception("poll: could not even record the failure for %s", graph_id)


def _advance_watermark(engine, mailbox, report: PollReport, now) -> None:
    """
    Move the watermark to the newest message stored BEFORE the earliest failure.

    Not "the newest stored": advancing past a failed message would retire it
    from the window and the shipment would be lost, visible only as a row with
    `poll_error` that nothing ever looks at again. Not "never advance on any
    failure" either -- one permanently-bad message would then freeze intake for
    everything behind it.

    Stopping at the earliest failure keeps both properties: progress continues
    up to the problem, and the problem is re-read every poll until it is fixed
    or deleted. The overlap plus dedup make that re-reading nearly free.
    """
    failures = [o.received_at for o in report.outcomes if o.error]
    horizon = min(failures) if failures else None
    eligible = [
        o.received_at for o in report.outcomes
        if not o.error and (horizon is None or o.received_at < horizon)
    ]

    before = report.watermark_before
    report.watermark_after = before
    if not eligible:
        _touch_poll_state(engine, mailbox, before, now, report.seen)
        return

    candidate = max(eligible)
    if before is not None and candidate <= before:
        _touch_poll_state(engine, mailbox, before, now, report.seen)
        return

    report.watermark_after = candidate
    report.watermark_advanced = True
    _touch_poll_state(engine, mailbox, candidate, now, report.seen)


def _touch_poll_state(engine, mailbox, watermark, now, seen) -> None:
    """
    Record that a poll ran, even when it found nothing and moved nothing.

    `last_polled_at` advancing while `last_received_at` stands is how "alive and
    the mailbox is quiet" is told apart from "stopped running" -- the silent
    failure the build plan's Phase 4 monitoring item exists to catch.
    """
    with engine.begin() as conn:
        exists = conn.execute(
            poll_state.select().with_only_columns(poll_state.c.mailbox)
            .where(poll_state.c.mailbox == mailbox)
        ).scalar()
        values = {"last_polled_at": now, "messages_seen": seen}
        if watermark is not None:
            values["last_received_at"] = watermark
        if exists:
            conn.execute(poll_state.update()
                         .where(poll_state.c.mailbox == mailbox).values(**values))
        else:
            conn.execute(poll_state.insert(), {
                "mailbox": mailbox,
                "last_received_at": watermark or EPOCH.replace(tzinfo=None),
                **values,
            })


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Poll once. Extraction is a SEPARATE command -- see extract_pending.py."""
    import argparse

    import config as config_module
    from sqlalchemy import create_engine

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="sqlite:///po_agent.db")
    ap.add_argument("--store", default=None, help="attachment store root")
    ap.add_argument("--overlap-minutes", type=int, default=int(POLL_OVERLAP.total_seconds() // 60))
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = config_module.GraphConfig.from_env()
    client = gc.build_graph_client(cfg)
    report = poll_once(
        create_engine(args.db), client, cfg.mailbox or "mock@local",
        BlobStore(args.store), overlap=dt.timedelta(minutes=args.overlap_minutes),
    )
    print(f"[{report.client_kind}] {report.summary()}")
    for outcome in report.failed:
        print(f"  FAILED {outcome.graph_message_id}: {outcome.error}")
    print("\nNothing has been extracted. Run extract_pending.py to parse what landed.")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
