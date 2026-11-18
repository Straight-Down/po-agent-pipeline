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
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence, Union

from sqlalchemy import select

import graph_client as gc
from schema import attachments, audit_log, message_attachments, messages, poll_state

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

#: Where a poll starts when no watermark exists AND no explicit `since` is given.
#:
#: Effectively "the whole mailbox". That is the right library default -- a first
#: run against a mailbox with history should read it rather than silently skip
#: everything older than today -- but it is a poor thing to discover by running
#: it. **The CLI therefore refuses a cold start without an explicit choice**: see
#: `main`, which requires `--since` or `--from-beginning` when no watermark
#: exists. The refusal is only on the first run; every later poll has a watermark
#: and needs no flag.
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
    #: Envelope detail, populated for every outcome so a dry run can show what
    #: WOULD be stored without fetching a single byte.
    subject: str = ""
    from_address: str = ""
    attachment_preview: list = field(default_factory=list)
    stored: bool = False
    skipped_existing: bool = False
    attachments_new: int = 0
    attachments_linked: int = 0
    error: str = ""
    #: Set when this poll RECORDED the message as unprocessable: an attachment
    #: Graph has no file bytes for. Not an `error` -- it will not be retried and
    #: does not hold the watermark -- and not `stored` either, because nothing
    #: of it was. It is surfaced instead (`messages.unprocessable_reason`).
    unprocessable: str = ""


#: Who the poller's audit events name. A system actor, like `write_path.ACTOR`.
ACTOR = "poller"
MESSAGE_UNPROCESSABLE = "MESSAGE_UNPROCESSABLE"
MESSAGE_UNPROCESSABLE_RESOLVED = "MESSAGE_UNPROCESSABLE_RESOLVED"


class _Unprocessable(Exception):
    """Raised inside a message's transaction so that NOTHING of it is stored."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class PollReport:
    """What one poll did, in enough detail to explain itself without the DB."""

    mailbox: str = ""
    client_kind: str = ""
    window_from: Optional[dt.datetime] = None
    watermark_before: Optional[dt.datetime] = None
    watermark_after: Optional[dt.datetime] = None
    watermark_advanced: bool = False
    dry_run: bool = False
    #: True when `max_messages` cut the batch short. Distinct from "that was all
    #: there was" -- without it, a capped run and an empty mailbox look alike.
    truncated: bool = False
    available: int = 0
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

    @property
    def unprocessable(self) -> list:
        """Messages THIS poll recorded as unprocessable. Never retried, so a later
        poll skips them as already had; the review app keeps showing them."""
        return [o for o in self.outcomes if o.unprocessable]

    def summary(self) -> str:
        if self.dry_run:
            atts = sum(len(o.attachment_preview) for o in self.outcomes)
            return (f"DRY RUN: {self.seen} message(s) would be considered, "
                    f"{atts} attachment(s), NOTHING stored, watermark untouched "
                    f"at {self.watermark_before}")
        cut = f" (capped at {self.seen} of {self.available})" if self.truncated else ""
        return (f"{self.seen} seen, {self.stored} stored, {self.skipped} already had, "
                f"{len(self.failed)} failed, {len(self.unprocessable)} unprocessable{cut} "
                f"| watermark "
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
    since: Optional[dt.datetime] = None,
    max_messages: Optional[int] = None,
    dry_run: bool = False,
) -> PollReport:
    """
    One pass. Reads from the watermark minus the overlap, stores, then advances.

    Every message is handled in its OWN transaction, so one bad message cannot
    roll back the ones that worked, and its failure is recorded against its own
    row rather than raised.

    `since` OVERRIDES the computed window entirely, watermark included -- it is
    how a first run is made small and chosen, and how an operator re-reads a
    known period. Overriding is safe because dedup makes a re-read free; that is
    the same property the overlap relies on.

    `max_messages` caps ONE run. The list arrives oldest-first, so a cap takes
    the oldest N and the watermark advances only across them; the next run
    continues from there. A capped run is flagged `truncated` in the report,
    because "I stopped early" and "that was everything" must not look alike.

    `dry_run` reads and reports and writes NOTHING -- no rows, no bytes, no
    watermark. It fetches attachment METADATA only, never content, so it is
    cheap in exactly the way the auth probe is cheap: find out what is true
    before acting on it.
    """
    store = store or BlobStore()
    now = now or _utcnow()
    report = PollReport(mailbox=mailbox, client_kind=getattr(client, "kind", "?"),
                        dry_run=dry_run)

    watermark = read_watermark(engine, mailbox)
    report.watermark_before = watermark
    if since is not None:
        window_from = _aware(since)
    elif watermark is not None:
        window_from = _aware(watermark) - overlap
    else:
        window_from = EPOCH
    report.window_from = window_from

    envelopes = list(client.list_messages(window_from))
    report.available = len(envelopes)
    if max_messages is not None and len(envelopes) > max_messages:
        envelopes = envelopes[:max_messages]
        report.truncated = True

    for envelope in envelopes:
        if dry_run:
            report.outcomes.append(_preview_message(client, envelope))
            continue
        outcome = _handle_message(engine, client, store, mailbox, envelope, now)
        report.outcomes.append(outcome)

    if dry_run:
        # Nothing is written, and that INCLUDES `last_polled_at`. A dry run must
        # not be able to masquerade as a poll in the monitoring signal.
        report.watermark_after = watermark
        logger.info("poll: %s", report.summary())
        return report

    _advance_watermark(engine, mailbox, report, now)
    logger.info("poll: %s", report.summary())
    return report


def _preview_message(client: gc.GraphClient, envelope: dict) -> MessageOutcome:
    """
    What WOULD be stored, from metadata alone.

    `list_attachments` returns name, contentType and size without transferring
    content, so a preview of a hundred messages costs a hundred metadata calls
    and zero bytes of attachment traffic. A failure here is recorded like any
    other -- a dry run that raised on one bad message would be useless for
    exactly the mailbox you most want to look at before touching.
    """
    sender = (envelope.get("from") or {}).get("emailAddress") or {}
    outcome = MessageOutcome(
        graph_message_id=str(envelope.get("id") or ""),
        received_at=_naive(gc._parse_graph_time(envelope["receivedDateTime"])),
        subject=(envelope.get("subject") or "")[:200],
        from_address=(sender.get("address") or ""),
    )
    try:
        outcome.attachment_preview = [
            {"name": str(a.get("name") or ""), "size": int(a.get("size") or 0),
             "content_type": str(a.get("contentType") or "")}
            for a in client.list_attachments(outcome.graph_message_id)
        ]
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"{type(exc).__name__}: {exc}"[:1000]
    return outcome


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
            # That includes an UNPROCESSABLE row (`unprocessable_reason` set,
            # `poll_error` NULL): recorded once, never retried, and shown in the
            # review app instead. A row WITH `poll_error` is a previous failure
            # being retried, which is why the marker is cleared and the
            # attachments re-fetched below.
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
                try:
                    new_content, linked = _store_attachment(
                        conn, client, store, message_id, graph_id, meta, now
                    )
                except gc.AttachmentNotFetchable as exc:
                    # Raised INSIDE the transaction, so every row this message
                    # wrote is rolled back: none of its attachments is stored,
                    # and extraction can never see half of it.
                    raise _Unprocessable(_unprocessable_reason(meta, declared, exc)) from exc
                outcome.attachments_new += int(new_content)
                outcome.attachments_linked += int(linked)
            outcome.stored = True
    except _Unprocessable as exc:
        # PERMANENT: the same attachment will never have file bytes, so a retry
        # would fail on every poll and hold the watermark behind it for good.
        # Recorded instead, and surfaced. If even the recording fails, fall back
        # to an ordinary failure: retried, watermark held. Never a silent skip.
        # The counts were for attachments the rollback just undid.
        outcome.attachments_new = outcome.attachments_linked = 0
        if _record_unprocessable(engine, mailbox, graph_id, envelope, received, now,
                                 exc.reason, declared):
            outcome.unprocessable = exc.reason
            logger.error("poll: message %s is UNPROCESSABLE and will not be retried: %s",
                         graph_id, exc.reason)
        else:
            outcome.error = f"unprocessable, but recording that failed: {exc.reason}"[:1000]
            logger.error("poll: message %s failed: %s", graph_id, outcome.error)
            _record_failure(engine, mailbox, graph_id, envelope, received, now, outcome.error)
    except Exception as exc:  # noqa: BLE001 -- a failure here is data, not a crash
        outcome.error = f"{type(exc).__name__}: {exc}"[:1000]
        logger.warning("poll: message %s failed: %s", graph_id, outcome.error)
        _record_failure(engine, mailbox, graph_id, envelope, received, now, outcome.error)
    return outcome


def _unprocessable_reason(meta: dict, declared: Sequence[dict],
                          exc: gc.AttachmentNotFetchable) -> str:
    """What a person reads about an unprocessable message: which attachment, what
    it is, and that nothing of the message was taken in."""
    name = str(meta.get("name") or "an attachment")
    kind = {
        "#microsoft.graph.itemAttachment": "a forwarded email attached as an item (.eml/.msg)",
        "#microsoft.graph.referenceAttachment": "a link to a file stored elsewhere",
    }.get(exc.odata_type or "", "an attachment Graph has no file bytes for")
    return (f"attachment {name!r} is {kind}, so it cannot be read as a file. "
            f"None of this message's {len(declared)} attachment(s) were taken in. "
            f"Graph: {exc}")[:1000]


def _record_unprocessable(engine, mailbox, graph_id, envelope, received, now,
                          reason: str, declared: Sequence[dict]) -> bool:
    """
    Record the message as unprocessable, in its own transaction, with an audit
    event. Returns False if it could not be recorded, so the caller can fall back
    to an ordinary, retried failure rather than move the watermark past a message
    nothing records.
    """
    try:
        with engine.begin() as conn:
            found = conn.execute(
                messages.select().with_only_columns(messages.c.id,
                                                    messages.c.unprocessable_reason)
                .where(messages.c.graph_message_id == graph_id)
            ).one_or_none()
            if found is not None and found.unprocessable_reason is not None:
                # Already recorded (a concurrent poll, or a retry after a failed
                # recording). Write nothing: a second audit event would double the
                # trail, and resetting `unprocessable_resolved_at` would put a
                # message a person already cleared back on the banner.
                return True
            existing = found.id if found is not None else None
            sender = (envelope.get("from") or {}).get("emailAddress") or {}
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
                "unprocessable_reason": reason,
                "unprocessable_resolved_at": None,
            }
            message_id = existing or str(uuid.uuid4())
            if existing:
                conn.execute(messages.update().where(messages.c.id == existing).values(**row))
            else:
                conn.execute(messages.insert(), {"id": message_id, **row})
            conn.execute(audit_log.insert(), {
                "id": str(uuid.uuid4()),
                "occurred_at": now,
                "workflow": "PACKING_SLIP",
                "actor": ACTOR,
                "actor_kind": "SYSTEM",
                "event": MESSAGE_UNPROCESSABLE,
                "message_id": message_id,
                "detail_json": json.dumps({"graph_message_id": graph_id, "reason": reason,
                                           "attachments": [str(m.get("name") or "")
                                                           for m in declared]}),
            })
        return True
    except Exception:  # noqa: BLE001 -- the caller falls back to a retried failure
        # A concurrent poll may have inserted the row first (the unique constraint
        # on graph_message_id). If what is there now records it, it IS recorded.
        try:
            with engine.connect() as conn:
                recorded = conn.execute(
                    messages.select().with_only_columns(messages.c.unprocessable_reason)
                    .where(messages.c.graph_message_id == graph_id)
                ).scalar()
            if recorded is not None:
                return True
        except Exception:  # noqa: BLE001
            pass
        logger.exception("poll: could not record message %s as unprocessable", graph_id)
        return False


def unprocessable_messages(engine, *, include_resolved: bool = False) -> list[dict]:
    """Messages recorded as unprocessable, oldest first. Unresolved only, unless asked."""
    query = (select(messages.c.id, messages.c.graph_message_id, messages.c.subject,
                    messages.c.from_address, messages.c.received_at,
                    messages.c.attachment_count, messages.c.unprocessable_reason,
                    messages.c.unprocessable_resolved_at)
             .where(messages.c.unprocessable_reason.is_not(None))
             .order_by(messages.c.received_at, messages.c.id))
    if not include_resolved:
        query = query.where(messages.c.unprocessable_resolved_at.is_(None))
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(query)]


class ResolveRefused(RuntimeError):
    """A resolve request that would not be honest to record."""


def resolve_unprocessable(engine, message_ref: str, *, by: str, note: str,
                          now: Optional[dt.datetime] = None) -> dict:
    """
    Take one unprocessable message off the review app's banner, deliberately.

    The record stays: `unprocessable_reason` is kept, `unprocessable_resolved_at`
    is set, and an audit event names who cleared it and why. `message_ref` is the
    row id or the Graph message id. Refuses a blank name or note, a message that
    is not unprocessable, and one already cleared.
    """
    by, note = (by or "").strip(), (note or "").strip()
    if not by or not note:
        raise ResolveRefused("both --by (who) and --note (what was done) are required")
    now = now or _utcnow()
    with engine.begin() as conn:
        row = conn.execute(
            messages.select().where(
                (messages.c.id == message_ref) | (messages.c.graph_message_id == message_ref))
        ).one_or_none()
        if row is None:
            raise ResolveRefused(f"no message {message_ref!r}")
        if row.unprocessable_reason is None:
            raise ResolveRefused(f"message {message_ref!r} is not recorded as unprocessable")
        if row.unprocessable_resolved_at is not None:
            raise ResolveRefused(f"message {message_ref!r} was already cleared at "
                                 f"{row.unprocessable_resolved_at}")
        conn.execute(messages.update().where(messages.c.id == row.id)
                     .values(unprocessable_resolved_at=now))
        conn.execute(audit_log.insert(), {
            "id": str(uuid.uuid4()),
            "occurred_at": now,
            "workflow": "PACKING_SLIP",
            "actor": by[:320],
            "actor_kind": "HUMAN",
            "event": MESSAGE_UNPROCESSABLE_RESOLVED,
            "message_id": row.id,
            "detail_json": json.dumps({"graph_message_id": row.graph_message_id,
                                       "note": note, "reason": row.unprocessable_reason}),
        })
    return {"id": row.id, "graph_message_id": row.graph_message_id, "resolved_at": now}


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
            found = conn.execute(
                messages.select().with_only_columns(messages.c.id,
                                                    messages.c.unprocessable_reason)
                .where(messages.c.graph_message_id == graph_id)
            ).one_or_none()
            if found is not None and found.unprocessable_reason is not None:
                # Recorded as unprocessable (by a concurrent poll): leave it. A
                # poll_error here would make the next poll retry it and re-record it.
                return
            if found is not None:
                conn.execute(messages.update()
                             .where(messages.c.id == found.id)
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


def parse_since(value: str) -> dt.datetime:
    """`--since` as a date or an ISO timestamp, always interpreted as UTC."""
    text = value.strip().replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _print_preview(report: PollReport) -> None:
    """The cheap look: what is there, from metadata only."""
    print()
    print("=" * 78)
    print(f"DRY RUN -- {report.mailbox} [{report.client_kind}]")
    print("=" * 78)
    print(f"  window from : {report.window_from}")
    print(f"  watermark   : {report.watermark_before} (UNCHANGED)")
    if report.truncated:
        print(f"  capped      : showing {report.seen} of {report.available} available")
    print()
    total_bytes = 0
    for outcome in report.outcomes:
        print(f"  {outcome.received_at}  {outcome.from_address or '(no sender)'}")
        print(f"      {outcome.subject or '(no subject)'}")
        if outcome.error:
            print(f"      !! {outcome.error}")
        for att in outcome.attachment_preview:
            total_bytes += att["size"]
            print(f"      - {att['name'][:64]:66} {att['size']:>9,} B  {att['content_type']}")
        if not outcome.attachment_preview and not outcome.error:
            print("      (no attachments)")
    attachment_count = sum(len(o.attachment_preview) for o in report.outcomes)
    print()
    print(f"  {report.seen} message(s), {attachment_count} attachment(s), "
          f"{total_bytes:,} bytes would be fetched")
    print("  NOTHING was stored and the watermark was not advanced.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Poll once. Extraction is a SEPARATE command -- see extract_pending.py."""
    import argparse

    import config as config_module
    from sqlalchemy import create_engine

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="sqlite:///po_agent.db")
    ap.add_argument("--store", default=None, help="attachment store root")
    ap.add_argument("--overlap-minutes", type=int,
                    default=int(POLL_OVERLAP.total_seconds() // 60))
    ap.add_argument("--since", type=parse_since, default=None,
                    help="start here instead of the watermark: a date (2026-09-01) "
                         "or an ISO timestamp. Overrides the watermark; safe, because "
                         "re-reading is deduped.")
    ap.add_argument("--from-beginning", action="store_true",
                    help="cold start over the ENTIRE mailbox history. Required "
                         "explicitly, so a first run is never an accident.")
    ap.add_argument("--max-messages", type=int, default=None,
                    help="process at most N messages this run, oldest first. The "
                         "next run continues from where this one stopped.")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what WOULD be stored and store nothing. Fetches "
                         "attachment metadata only, never content.")
    ap.add_argument("--list-unprocessable", action="store_true",
                    help="list messages recorded as unprocessable and not yet cleared, "
                         "then stop. Reads no mail.")
    ap.add_argument("--resolve-unprocessable", metavar="MESSAGE", default=None,
                    help="clear one unprocessable message from the review app's banner "
                         "(row id or Graph message id), then stop. Needs --by and --note. "
                         "The record and its reason are kept. Reads no mail.")
    ap.add_argument("--by", default="", help="who is clearing it (with --resolve-unprocessable)")
    ap.add_argument("--note", default="", help="what was done about it (with --resolve-unprocessable)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Housekeeping on the record, not a poll: no Graph configuration needed.
    if args.list_unprocessable or args.resolve_unprocessable:
        engine = create_engine(args.db)
        if args.resolve_unprocessable:
            try:
                done = resolve_unprocessable(engine, args.resolve_unprocessable,
                                             by=args.by, note=args.note)
            except ResolveRefused as exc:
                print(f"REFUSED: {exc}")
                return 2
            print(f"Cleared {done['graph_message_id']} at {done['resolved_at']}. "
                  "Its reason stays on record.")
            return 0
        held = unprocessable_messages(engine)
        print(f"{len(held)} unprocessable message(s) not yet cleared.")
        for row in held:
            print(f"  {row['id']}  {row['received_at']}  {row['from_address'] or '(no sender)'}")
            print(f"      {row['subject'] or '(no subject)'}")
            print(f"      {row['unprocessable_reason']}")
        return 0

    cfg = config_module.GraphConfig.from_env()
    client = gc.build_graph_client(cfg)
    mailbox = cfg.mailbox or "mock@local"
    engine = create_engine(args.db)

    # THE COLD START IS A CHOICE, NOT A DEFAULT.
    #
    # With no watermark and no `--since`, the window opens at EPOCH and the run
    # pulls the entire mailbox. That is the correct library behaviour -- skipping
    # history silently would be worse -- but it is a poor thing to find out by
    # doing it, and this mailbox is being seeded with forwarded historical slips.
    # So the first run has to say which it wants. Every later run has a watermark
    # and needs no flag.
    # `--from-beginning` SETS THE WINDOW. It used to only satisfy the guard
    # below, so with a watermark present it read from the watermark and the flag
    # was a silent no-op -- it hid a message on a live dry run. A flag that does
    # nothing under some conditions is worse than one that does not exist,
    # because it is believed (RUNBOOK section 8 lesson 20).
    #
    # `--since` wins if both are given: it is the more specific instruction, and
    # refusing the combination would fail a command whose intent is unambiguous.
    if args.from_beginning and args.since is None:
        args.since = EPOCH

    if (read_watermark(engine, mailbox) is None
            and args.since is None and not args.from_beginning):
        print("No watermark for this mailbox: this would be a COLD START and would "
              "read the entire mailbox history.")
        print("Choose one, deliberately:")
        print("  --since 2026-09-01     start from a date you pick")
        print("  --from-beginning       read everything")
        print("  --dry-run              see what is there first, storing nothing")
        print("\nAdd --max-messages N to cap the first run whichever you choose.")
        return 2

    report = poll_once(
        engine, client, mailbox, BlobStore(args.store),
        overlap=dt.timedelta(minutes=args.overlap_minutes),
        since=args.since, max_messages=args.max_messages, dry_run=args.dry_run,
    )

    if args.dry_run:
        _print_preview(report)
        return 0

    print(f"[{report.client_kind}] {report.summary()}")
    for outcome in report.failed:
        print(f"  FAILED {outcome.graph_message_id}: {outcome.error}")
    for outcome in report.unprocessable:
        print(f"  UNPROCESSABLE {outcome.graph_message_id}: {outcome.unprocessable}")
    if report.unprocessable:
        print("  Unprocessable messages are never retried. The review app shows them until "
              "cleared with --resolve-unprocessable.")
    if report.truncated:
        print(f"  {report.available - report.seen} message(s) left for the next run.")
    print("\nNothing has been extracted. Run extract_pending.py to parse what landed.")
    return 1 if (report.failed or report.unprocessable) else 0


if __name__ == "__main__":
    raise SystemExit(main())
