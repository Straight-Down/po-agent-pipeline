"""
The ingest -> extraction seam: parse what has already landed.

Reads `messages` rows that have not been extracted, gathers the stored paths of
their attachments, and hands them to `ingest_shipment` -- which is unchanged, and
deliberately so. It takes file paths, it is validated by 167 checks, and the
boundary this file provides does not require it to change.

## Why this is a SEPARATE command from the poller

Because a parser change must be re-runnable without re-reading the mailbox, and
the only way to guarantee that is to make re-running possible without Graph in
the picture at all. Chain the two and a parser bug costs a re-download; worse,
the decision about whether re-extraction is worth its token cost gets made
implicitly by whoever runs the poll.

So: two commands. `poller.py` reads mail and stops. This reads rows and stops.
`--all` re-extracts everything already stored, which is the whole point.

## What counts as "not yet extracted"

`messages.extracted_at IS NULL`. It is on the MESSAGE rather than the attachment
because `ingest_shipment` works per email -- one call receives every attachment
of one message, since attachment triage is a decision across the set (which is
the packing list, which is the invoice, which is a banned inspection report).
Per-attachment extraction would have to re-derive that grouping from the
database, badly.

A message whose extraction failed keeps `extracted_at` NULL and records
`extraction_error`, so it is retried by default and can be listed. That mirrors
`poll_error` on the intake side; the two are separate columns because they are
separate failures and conflating them would hide which half is broken.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from sqlalchemy import select

import ingest as ingest_module
from schema import attachments, message_attachments, messages

logger = logging.getLogger(__name__)


@dataclass
class ExtractionReport:
    """What one driver run did."""

    attempted: int = 0
    extracted: int = 0
    skipped_no_attachments: int = 0
    failed: list = field(default_factory=list)
    shipment_ids: list = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.attempted} message(s) attempted, {self.extracted} extracted, "
                f"{self.skipped_no_attachments} had no attachments, "
                f"{len(self.failed)} failed")


def pending_messages(engine, *, include_extracted: bool = False) -> list[dict]:
    """
    Messages awaiting extraction, oldest first, with their stored paths.

    Ordered by `received_at` so a backlog is worked in the order it arrived --
    which matters for a PO receiving two shipments, where the later slip should
    be applied after the earlier one rather than in whatever order a scan
    returned.
    """
    query = (
        select(messages.c.id, messages.c.graph_message_id, messages.c.internet_message_id,
               messages.c.mailbox, messages.c.subject, messages.c.from_address,
               messages.c.sent_at, messages.c.received_at, messages.c.attachment_count)
        .order_by(messages.c.received_at, messages.c.id)
    )
    if not include_extracted:
        query = query.where(messages.c.extracted_at.is_(None))
    # A message that failed INTAKE has not finished landing -- extracting a
    # partial attachment set would produce a shipment missing documents, which
    # is worse than waiting for the next poll to complete it.
    query = query.where(messages.c.poll_error.is_(None))

    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(query)]
        for row in rows:
            row["paths"] = [
                Path(uri) for (uri,) in conn.execute(
                    select(attachments.c.stored_uri)
                    .select_from(
                        message_attachments.join(
                            attachments,
                            message_attachments.c.content_sha256
                            == attachments.c.content_sha256,
                        )
                    )
                    .where(message_attachments.c.message_id == row["id"])
                    .order_by(message_attachments.c.filename)
                ) if uri
            ]
    return rows


def extract_pending(
    engine,
    *,
    include_extracted: bool = False,
    limit: Optional[int] = None,
    client=None,
    extractor=None,
    now: Optional[dt.datetime] = None,
) -> ExtractionReport:
    """Run `ingest_shipment` over everything stored and not yet extracted."""
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    report = ExtractionReport()
    rows = pending_messages(engine, include_extracted=include_extracted)
    if limit is not None:
        rows = rows[:limit]

    for row in rows:
        report.attempted += 1
        if not row["paths"]:
            # Nothing to parse. Marked extracted so it leaves the queue -- a
            # message with no attachments is COMPLETE, not pending, and leaving
            # it NULL would make the backlog grow by one every poll forever.
            _mark(engine, row["id"], extracted_at=now, error=None)
            report.skipped_no_attachments += 1
            continue

        source = ingest_module.SourceMessage(
            graph_message_id=row["graph_message_id"],
            mailbox=row["mailbox"],
            received_at=row["received_at"],
            subject=row["subject"] or "",
            from_address=row["from_address"] or "",
            internet_message_id=row["internet_message_id"],
            sent_at=row["sent_at"],
        )
        try:
            result = ingest_module.ingest_shipment(
                engine, row["paths"], message=source, client=client,
                extractor=extractor, now=now,
            )
        except Exception as exc:  # noqa: BLE001 -- one bad document, not a stopped run
            error = f"{type(exc).__name__}: {exc}"[:1000]
            logger.warning("extract: message %s failed: %s", row["graph_message_id"], error)
            _mark(engine, row["id"], extracted_at=None, error=error)
            report.failed.append((row["graph_message_id"], error))
            continue

        _mark(engine, row["id"], extracted_at=now, error=None)
        report.extracted += 1
        if result.shipment_id:
            report.shipment_ids.append(result.shipment_id)

    logger.info("extract: %s", report.summary())
    return report


def _mark(engine, message_id: str, *, extracted_at, error) -> None:
    with engine.begin() as conn:
        conn.execute(
            messages.update().where(messages.c.id == message_id)
            .values(extracted_at=extracted_at, extraction_error=error)
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Extract what has landed. Reads NO mail -- run poller.py for that."""
    import argparse

    from sqlalchemy import create_engine

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="sqlite:///po_agent.db")
    ap.add_argument("--all", action="store_true",
                    help="re-extract messages already extracted (after a parser change)")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = extract_pending(
        create_engine(args.db), include_extracted=args.all, limit=args.limit,
    )
    print(report.summary())
    for graph_id, error in report.failed:
        print(f"  FAILED {graph_id}: {error}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
