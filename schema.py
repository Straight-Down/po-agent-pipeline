"""
Database schema for the PO update pipeline (Phase 2, item 3).

One SQLAlchemy Core definition, two targets: **SQLite** to start and **Azure SQL
Database (serverless)** later, per the build plan. Nothing here is ORM -- these
are table definitions, and the application talks to them through Core or plain
SQL. Alembic renders the dialect-correct DDL from this metadata; see
`migrations/versions/0001_initial_schema.py` and the rationale doc
(`PO-Update-Automation-Schema-Rationale.md`), which has one section per
constraint this design exists to satisfy.

## Portability rules this file follows, and why each one is here

- **Application-generated UUID text keys**, never `AUTOINCREMENT`/`IDENTITY`.
  Identity columns and sequences differ between the two engines and are the first
  thing to bite on a migration; a 36-char key costs nothing at 10-20 shipments a
  week.
- **Application-generated UTC timestamps**, never `datetime('now')`/`GETDATE()`.
- **Explicit lengths on every string column.** Azure SQL cannot index
  `NVARCHAR(MAX)` and caps an index key at 1700 bytes, so an unbounded column
  quietly becomes un-indexable there. JSON payload columns use `UNBOUNDED_TEXT`
  (`NVARCHAR(MAX)`) precisely because they are never indexed -- note that plain
  `UnicodeText` would render as the DEPRECATED `NTEXT` there, which is why that
  type is declared once below rather than written at each column.
- **`Unicode`, never `String`, for every text column** -- so Azure SQL gets
  `NVARCHAR` rather than `VARCHAR`. Not a preference: under the Latin-1 collation
  a `VARCHAR` column converts CJK to `?` **on write**, so the two real colours
  `黑色` and `紅色` both become `??` and compare EQUAL -- and since the canonical
  keys are uniquely indexed, the second one is rejected as a duplicate. The
  conversion is lossy and irreversible; re-collating afterwards recovers nothing.
  Applied to ALL 108 sized columns AND all 7 unbounded ones, rather than only
  the 39 that can currently receive non-ASCII, because a rule requiring per-column classification is one more thing
  to get wrong later, and this failure mode is silent. Lengths are unchanged and
  still mean CHARACTERS -- which is why this is `NVARCHAR` and not a UTF-8
  collation, where `VARCHAR(n)` means n BYTES and every length would silently
  become a smaller character budget. See RUNBOOK section 6.
- **`Numeric`, never `Float`, for quantities.** Binary floating point is the wrong
  tool for something a human will reconcile against a printed document.
- **No NULLs in key columns.** `''` is the sentinel for "the document did not say"
  -- because Azure SQL treats NULLs as *equal* in a unique index while SQLite
  treats them as distinct, so a nullable key column would enforce a different rule
  on each engine. Where a partial index does span a nullable column, its predicate
  carries an explicit `IS NOT NULL`.
- **Every constraint is named**, via the metadata naming convention below.
  SQLite cannot `ALTER TABLE ... DROP CONSTRAINT`, so Alembic's batch mode
  rebuilds the table -- and it can only reproduce constraints it can name.

`PRAGMA foreign_keys=ON` must be set on every SQLite connection or the foreign
keys in this file are decorative. `connect()` in this module does it for you.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Table,
    Unicode,
    UnicodeText,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects import mssql
from sqlalchemy.engine import Engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateIndex

# Stable, predictable constraint names -- required for Alembic batch mode on
# SQLite, and it keeps autogenerate diffs from churning.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}

#: The unbounded text type for the JSON/free-text columns, spelled so BOTH
#: engines get the right thing.
#:
#: `UnicodeText` alone renders as **NTEXT** on SQL Server, which is deprecated,
#: cannot be indexed, and does not work with most string functions -- Microsoft
#: has said it will be removed. The variant pins the modern `NVARCHAR(MAX)` for
#: mssql while leaving SQLite exactly as it was (`TEXT`).
#:
#: Caught only by compiling it and reading the output. `UnicodeText` is the
#: obvious name, it is Unicode-correct, and it is still the wrong type -- the
#: kind of thing that is invisible until someone looks at the DDL.
UNBOUNDED_TEXT = UnicodeText().with_variant(mssql.NVARCHAR(None), "mssql")

metadata = MetaData(naming_convention=NAMING_CONVENTION)


def new_id() -> str:
    """Primary keys are generated here, not by the database. See the portability note."""
    return str(uuid.uuid4())


#: The dialects `_partial()` knows how to spell a filtered index for. SQLite is
#: the development database; MSSQL is Azure SQL, the deployment target. Adding a
#: dialect here means adding its `<name>_where` kwarg below IN THE SAME EDIT --
#: the guard exists precisely so that cannot be forgotten.
PARTIAL_INDEX_DIALECTS = frozenset({"sqlite", "mssql"})


class PartialIndexUnsupported(Exception):
    """
    Raised when a filtered index would be emitted as a FULL index.

    See `_partial` for why this is an exception rather than a fallback.
    """


def _partial(predicate: str) -> dict:
    """
    A filtered unique index, spelled for both engines.

    Same feature, same syntax, two dialect keywords. This is one of exactly two
    places the dialects genuinely differ (the other being NULL equality in unique
    indexes, which is why several of these predicates say `IS NOT NULL`), so it is
    worth having in one function rather than repeated at each call site.

    ## Why this also carries `info`, and why a guard compiles off it

    **SQLAlchemy accepts dialect-prefixed keyword arguments it does not
    recognise, and silently ignores the ones that do not match the dialect being
    compiled for.** That is the intended behaviour and it is usually what you
    want. Here it is a trap: compile these indexes against PostgreSQL and every
    `WHERE` clause disappears, with no error, no warning, and no failing test.
    The index is still created -- as a FULL unique index, which is a different
    and stricter constraint than the one written here.

    What that costs is not theoretical. Without its predicate,
    `ux_change_candidates_one_selected` becomes `UNIQUE(change_id)` and permits
    one candidate row per change, when the whole point of `change_candidates` is
    to hold SEVERAL for a human to choose between; and
    `ux_proposed_changes_canonical_key` loses `WHERE key_size <> ''` and starts
    rejecting the second sizeless row on a PO, which is real vendor data.

    So `info["partial_predicate"]` marks these indexes, and the
    `CreateIndex` compiler hook below **refuses to emit one on a dialect this
    function has no predicate for**. The fix for a new dialect is to teach
    `_partial` its spelling, not to let the index quietly widen.

    Note the deliberate choice NOT to simply add `postgresql_where`. That would
    fix this instance and leave the shape intact -- the next dialect, or the next
    dialect-specific kwarg anywhere in this file, would fail the same silent way.
    """
    return {
        "sqlite_where": text_clause(predicate),
        "mssql_where": predicate,
        # Read by _refuse_silent_full_index. The predicate is kept as text so the
        # error can quote what WOULD have been lost.
        "info": {"partial_predicate": predicate},
    }


@compiles(CreateIndex)
def _refuse_silent_full_index(element, compiler, **kw):
    """
    Refuse to compile a filtered index on a dialect that would drop the filter.

    Registered for EVERY dialect and delegating to the normal compiler in the
    supported cases, so the check cannot be bypassed by reaching for a dialect
    nobody thought about. An unfiltered index compiles exactly as before; only
    one carrying `info["partial_predicate"]` is inspected.
    """
    index = element.element
    predicate = (index.info or {}).get("partial_predicate")
    dialect = compiler.dialect.name
    if predicate and dialect not in PARTIAL_INDEX_DIALECTS:
        raise PartialIndexUnsupported(
            f"Index {index.name!r} is a FILTERED index (WHERE {predicate}), and the "
            f"{dialect!r} dialect has no predicate spelled for it in schema._partial. "
            f"SQLAlchemy would ignore the dialect kwargs it does not recognise and emit "
            f"a FULL unique index instead -- silently, and enforcing something stricter "
            f"than what was written. Refusing.\n"
            f"Supported dialects: {', '.join(sorted(PARTIAL_INDEX_DIALECTS))}. "
            f"If {dialect!r} is genuinely a target, teach _partial its `<dialect>_where` "
            f"spelling and add it to PARTIAL_INDEX_DIALECTS in the same edit -- and check "
            f"the predicate's own syntax while you are there, since "
            f"`selected = 1` is valid on sqlite and mssql and a type error on a dialect "
            f"with a real boolean."
        )
    return compiler.visit_create_index(element, **kw)


def text_clause(predicate: str):
    from sqlalchemy import text

    return text(predicate)


# ---------------------------------------------------------------------------
# Intake: the two dedup axes
# ---------------------------------------------------------------------------

messages = Table(
    "messages",
    metadata,
    Column("id", Unicode(36), primary_key=True),
    # DEDUP AXIS 1. Graph cannot mark a message read or move it (Mail.Read is
    # read-only), so the mailbox is immutable to this app and cannot hold state.
    # "Have I seen this message?" has to be answerable from here.
    Column("graph_message_id", Unicode(512), nullable=False),
    Column("internet_message_id", Unicode(512)),
    Column("mailbox", Unicode(320), nullable=False),
    Column("subject", Unicode(1000)),
    Column("from_address", Unicode(320)),
    # Set when Paula forwards rather than the vendor mailing the box directly.
    Column("forwarded_by", Unicode(320)),
    Column("sent_at", DateTime),
    Column("received_at", DateTime, nullable=False),
    Column("ingested_at", DateTime, nullable=False),
    # The folder the message was found in, as Graph reports it: an opaque
    # `parentFolderId`, NOT a display name. Resolving the name needs a fifth
    # Graph call (`/mailFolders/{id}`) and the client interface deliberately has
    # only four -- see `graph_client.GraphClient`. Recorded rather than filtered
    # on: the mailbox is dedicated to this pipeline so everything in it is in
    # scope today, and a folder rule added later can be written against stored
    # rows instead of re-reading the mailbox.
    Column("folder_id", Unicode(512)),
    # What the mailbox said it carried, before any of it was fetched. Lets a
    # message with zero attachments be a recorded fact rather than an absence,
    # and lets a partial fetch be detected.
    Column("attachment_count", Integer, nullable=False, default=0),
    # INTAKE failure, per message. One unreadable message must not stop a poll,
    # so the failure is recorded here and the loop continues.
    Column("poll_error", Unicode(1000)),
    # EXTRACTION progress, and deliberately NOT the same axis as poll_error.
    # The poller stores bytes; a separate driver extracts them. `extracted_at`
    # NULL means "landed, not yet parsed" -- which is exactly the work queue,
    # and is why a parser change can be re-run over everything already stored
    # without touching Graph.
    Column("extracted_at", DateTime),
    Column("extraction_error", Unicode(1000)),
    UniqueConstraint("graph_message_id", name="uq_messages_graph_message_id"),
)

attachments = Table(
    "attachments",
    metadata,
    # DEDUP AXIS 2, and the reason this table is keyed by content rather than by
    # (message, filename): a re-forward arrives with a NEW message id carrying the
    # SAME bytes. Keying on content makes that one row, seen twice.
    Column("content_sha256", Unicode(64), primary_key=True),
    Column("byte_size", Integer, nullable=False),
    # The classifier's verdict belongs to the CONTENT. Filenames lie -- one real
    # vendor's customs invoice is named "...PACKING LIST.pdf" -- so the verdict
    # must not be stored per filename.
    Column("doc_type", Unicode(32), nullable=False),
    Column("doc_type_reason", Unicode(1000)),
    # Set when the file could not be opened at all (truncated, encrypted, empty).
    # Distinct from "opened fine, has no size data".
    Column("open_failure_reason", Unicode(500)),
    # Inspection reports. Paula's ruling, permanent, not a tunable.
    Column("banned_as_data_source", Boolean, nullable=False, default=False),
    # Where the bytes live. NOT in the database -- see the retention section of the
    # rationale doc for what is stored, where, and the purge story.
    Column("stored_uri", Unicode(1000)),
    Column("first_seen_at", DateTime, nullable=False),
    CheckConstraint(
        "doc_type IN ('PACKING_LIST','COMMERCIAL_INVOICE','SHIPPING_ADVICE',"
        "'SHIPPING_SCHEDULE','PAYMENT_REQUEST','INSPECTION_REPORT','OTHER','UNREADABLE',"
        # Written by the POLLER, which stores bytes and classifies nothing. Not
        # the same as 'OTHER' (classified, and none of the above) or 'UNREADABLE'
        # (opened and failed). Distinguishing them is what makes "never looked
        # at" queryable instead of indistinguishable from "looked at, uninteresting".
        "'UNCLASSIFIED')",
        name="doc_type",
    ),
)

message_attachments = Table(
    "message_attachments",
    metadata,
    Column("id", Unicode(36), primary_key=True),
    Column("message_id", Unicode(36), ForeignKey("messages.id"), nullable=False),
    Column(
        "content_sha256", Unicode(64), ForeignKey("attachments.content_sha256"), nullable=False
    ),
    # Per-message, not per-content: the same file can arrive under two names.
    Column("filename", Unicode(500), nullable=False),
    UniqueConstraint("message_id", "content_sha256", name="uq_message_attachments_pair"),
)


# ---------------------------------------------------------------------------
# The work item
# ---------------------------------------------------------------------------

shipments = Table(
    "shipments",
    metadata,
    Column("id", Unicode(36), primary_key=True),
    # A shipment is "a batch of PO line updates arising from one real-world
    # shipment event, evidenced either by a vendor document or by Paula's
    # instruction". Both workflows live here; `origin` separates them. A
    # PAULA_DIRECTED row legitimately has no message, no attachment and often no
    # vendor name -- that is the shape, not missing data.
    Column("origin", Unicode(16), nullable=False),
    Column("message_id", Unicode(36), ForeignKey("messages.id")),
    # The attachment whose lines were actually parsed. NULL for PAULA_DIRECTED.
    Column(
        "primary_attachment_sha", Unicode(64), ForeignKey("attachments.content_sha256")
    ),
    # sha of the sorted source shas -- lets a re-forward of the same attachment
    # SET be recognised even before the primary is chosen.
    Column("source_set_hash", Unicode(64)),
    Column("superseded_by_shipment_id", Unicode(36), ForeignKey("shipments.id")),
    Column("vendor_name", Unicode(200)),
    # Vendor-stated dates: REFERENCE ONLY. Never written to NetSuite, never
    # promoted into a confirmed receipt date. They live here, on the shipment,
    # deliberately far from the line where a date could be written.
    Column("vendor_etd", Unicode(40)),
    Column("vendor_eta", Unicode(40)),
    # Calibration slicing: which parser and which model produced these lines.
    Column("parser", Unicode(64)),
    Column("extractor_model", Unicode(64)),
    Column("extractor_prompt_version", Unicode(64)),
    Column("doc_needs_review", Boolean, nullable=False, default=False),
    Column("needs_manual_entry", Boolean, nullable=False, default=False),
    Column("parse_warnings_json", UNBOUNDED_TEXT),
    Column("parse_notes_json", UNBOUNDED_TEXT),
    Column("line_count", Integer),
    Column("unit_total", Numeric(12, 3)),
    # WHAT THIS SHIPMENT COST, totalled across its primary and every cross-check.
    # NULL means "not recorded" -- a row written before this column existed, or by
    # a path that does not account. ZERO means measured and free, which the
    # deterministic Inprotex parser genuinely is. Keeping those distinct is the
    # point: without it, "which document cost the most" is unanswerable after a
    # run and has to be guessed from character counts (section 8 lesson 21).
    Column("extractor_input_tokens", Integer),
    Column("extractor_output_tokens", Integer),
    Column("created_by", Unicode(320), nullable=False),
    Column("created_at", DateTime, nullable=False),
    CheckConstraint("origin IN ('VENDOR_EMAIL','PAULA_DIRECTED')", name="origin"),
    CheckConstraint(
        "(origin = 'VENDOR_EMAIL' AND message_id IS NOT NULL) OR "
        "(origin = 'PAULA_DIRECTED' AND message_id IS NULL "
        " AND primary_attachment_sha IS NULL)",
        name="provenance",
    ),
)

# DEDUP AXIS 2, enforced: the same parsed content cannot produce two live
# shipments. `IS NOT NULL` is load-bearing -- Azure SQL treats NULLs as equal in a
# unique index, so every PAULA_DIRECTED row would collide without it.
Index(
    "ux_shipments_primary_attachment",
    shipments.c.primary_attachment_sha,
    unique=True,
    **_partial("primary_attachment_sha IS NOT NULL AND superseded_by_shipment_id IS NULL"),
)
Index("ix_shipments_source_set_hash", shipments.c.source_set_hash)

shipment_sources = Table(
    "shipment_sources",
    metadata,
    Column("id", Unicode(36), primary_key=True),
    Column("shipment_id", Unicode(36), ForeignKey("shipments.id"), nullable=False),
    Column(
        "content_sha256", Unicode(64), ForeignKey("attachments.content_sha256"), nullable=False
    ),
    # One email can carry several documents covering the same shipment -- a
    # style/colour/size rollup AND a carton-by-carton detail, which the pipeline
    # cross-checks against each other. Roles record which was parsed, which
    # corroborated it, and which was set aside.
    Column("role", Unicode(16), nullable=False),
    Column("exclusion_reason", Unicode(500)),
    Column("agreement_json", UNBOUNDED_TEXT),
    # PER DOCUMENT, which is the grain the question is actually asked at: a
    # cross-check that costs a full extraction and is then discarded is invisible
    # in a per-shipment total. Same NULL/zero distinction as `shipments`.
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    UniqueConstraint("shipment_id", "content_sha256", name="uq_shipment_sources_pair"),
    CheckConstraint("role IN ('PRIMARY','CROSS_CHECK','EXCLUDED')", name="role"),
)

shipment_pos = Table(
    "shipment_pos",
    metadata,
    Column("id", Unicode(36), primary_key=True),
    Column("shipment_id", Unicode(36), ForeignKey("shipments.id"), nullable=False),
    # A shipment is NOT 1:1 with a PO -- one Inprotex sheet interleaves six. This
    # table is the fan-out, and it carries per-PO resolution state so PO #4 failing
    # to resolve does not stall the other five.
    Column("po_number_printed", Unicode(120), nullable=False),  # verbatim "PO NO : 1720"
    Column("po_number_key", Unicode(40), nullable=False),  # canonical digits "1720"
    Column("ns_tranid", Unicode(40)),  # "PO0001720", once resolved
    Column("ns_internal_id", Unicode(40)),
    Column("resolution_status", Unicode(16), nullable=False, default="UNRESOLVED"),
    Column("resolution_strategy", Unicode(64)),
    Column("resolved_at", DateTime),
    UniqueConstraint("shipment_id", "po_number_key", name="uq_shipment_pos_key"),
    CheckConstraint(
        "resolution_status IN ('UNRESOLVED','RESOLVED','NOT_FOUND','AMBIGUOUS')",
        name="resolution_status",
    ),
)


# ---------------------------------------------------------------------------
# The state machine, as data rather than as strings scattered through code
# ---------------------------------------------------------------------------

change_states = Table(
    "change_states",
    metadata,
    Column("state", Unicode(32), primary_key=True),
    Column("is_terminal", Boolean, nullable=False),
    Column("description", Unicode(500), nullable=False),
)

change_state_transitions = Table(
    "change_state_transitions",
    metadata,
    Column("from_state", Unicode(32), ForeignKey("change_states.state"), primary_key=True),
    Column("to_state", Unicode(32), ForeignKey("change_states.state"), primary_key=True),
    Column("trigger", Unicode(64), primary_key=True),
    Column("actor_kind", Unicode(8), nullable=False),
    CheckConstraint("actor_kind IN ('HUMAN','SYSTEM')", name="actor_kind"),
)

#: The initial state is set at insert time, so "from" is this sentinel rather than
#: NULL -- NULL in a primary key is not portable, and a sentinel is greppable.
STATE_INSERT = "(insert)"

STATE_PENDING_REVIEW = "PENDING_REVIEW"
STATE_NEEDS_ATTENTION = "NEEDS_ATTENTION"
STATE_NEEDS_RESOLUTION = "NEEDS_RESOLUTION"
STATE_NEEDS_ASSIGNMENT = "NEEDS_ASSIGNMENT"
STATE_MANUAL_ENTRY_REQUIRED = "MANUAL_ENTRY_REQUIRED"
STATE_NO_CHANGE = "NO_CHANGE"
STATE_APPROVED = "APPROVED"
STATE_WRITTEN = "WRITTEN"
STATE_WRITE_FAILED = "WRITE_FAILED"
STATE_DISCARDED = "DISCARDED"
STATE_SUPERSEDED = "SUPERSEDED"

#: States a write may be built for. Everything else is a refusal.
WRITABLE_STATES = (STATE_APPROVED, STATE_WRITTEN, STATE_WRITE_FAILED)

CHANGE_STATES: tuple[tuple[str, bool, str], ...] = (
    (STATE_INSERT, False, "Sentinel for the initial transition; never stored on a row."),
    (
        STATE_PENDING_REVIEW,
        False,
        "Matched one open line, quantity differs, waiting on a human.",
    ),
    (
        STATE_NO_CHANGE,
        False,
        "Matched, quantity already correct. Not terminal: a receipt date may still "
        "be wanted on the line.",
    ),
    (
        STATE_NEEDS_ATTENTION,
        False,
        "Cannot propose: no matching line, no open line, closed line, missing PO or "
        "quantity, or low extraction confidence.",
    ),
    (
        STATE_NEEDS_RESOLUTION,
        False,
        "Matched SEVERAL open lines. Candidates recorded; no target chosen; a human picks.",
    ),
    (
        STATE_NEEDS_ASSIGNMENT,
        False,
        "The slip split this size across SEVERAL transport-mode rows (By Sea, By UPS) "
        "and the PO holds several lines for it. Both sides recorded; no pairing made; "
        "a human assigns. Distinct from NEEDS_RESOLUTION, which is picking one line "
        "for ONE shipment row -- this is pairing N rows to N lines.",
    ),
    (
        STATE_MANUAL_ENTRY_REQUIRED,
        False,
        "No acceptable size-level source document for the shipment. Paula keys it herself.",
    ),
    (
        STATE_APPROVED,
        False,
        "A human approved at least one scope. A target line is guaranteed present.",
    ),
    (
        STATE_WRITTEN,
        False,
        "Every APPROVED scope has been written -- NOT 'done'. A line sits here "
        "indefinitely with quantity applied and no date. The per-scope columns hold "
        "the truth.",
    ),
    (
        STATE_WRITE_FAILED,
        False,
        "At least one approved scope failed to write. Retryable without re-approval.",
    ),
    (STATE_DISCARDED, True, "A human closed it without writing. Terminal."),
    (
        STATE_SUPERSEDED,
        True,
        "A later shipment re-proposed the same canonical key against the same line "
        "before this one was written. Terminal.",
    ),
)

#: (from, to, trigger, actor_kind). The complete legal set -- anything absent is
#: illegal by definition, which is the point of keeping it as data.
CHANGE_STATE_TRANSITIONS: tuple[tuple[str, str, str, str], ...] = (
    (STATE_INSERT, STATE_PENDING_REVIEW, "matched, quantity differs", "SYSTEM"),
    (STATE_INSERT, STATE_NO_CHANGE, "matched, quantity already correct", "SYSTEM"),
    (STATE_INSERT, STATE_NEEDS_ATTENTION, "cannot propose", "SYSTEM"),
    (STATE_INSERT, STATE_NEEDS_RESOLUTION, "several open lines match", "SYSTEM"),
    (STATE_INSERT, STATE_NEEDS_ASSIGNMENT, "several shipment rows and several lines share a key", "SYSTEM"),
    (STATE_INSERT, STATE_MANUAL_ENTRY_REQUIRED, "no acceptable source document", "SYSTEM"),
    (STATE_INSERT, STATE_APPROVED, "Paula-directed instruction, no proposal step", "HUMAN"),
    (STATE_PENDING_REVIEW, STATE_APPROVED, "approved", "HUMAN"),
    (STATE_PENDING_REVIEW, STATE_NEEDS_ATTENTION, "re-check before write found a problem", "SYSTEM"),
    (STATE_PENDING_REVIEW, STATE_DISCARDED, "closed without writing", "HUMAN"),
    (STATE_PENDING_REVIEW, STATE_SUPERSEDED, "re-proposed by a later shipment", "SYSTEM"),
    (STATE_NEEDS_ATTENTION, STATE_APPROVED, "human named the target line", "HUMAN"),
    (STATE_NEEDS_ATTENTION, STATE_DISCARDED, "closed without writing", "HUMAN"),
    (STATE_NEEDS_ATTENTION, STATE_SUPERSEDED, "re-proposed by a later shipment", "SYSTEM"),
    (STATE_NEEDS_RESOLUTION, STATE_APPROVED, "human selected a candidate line", "HUMAN"),
    (STATE_NEEDS_RESOLUTION, STATE_DISCARDED, "closed without writing", "HUMAN"),
    (STATE_NEEDS_RESOLUTION, STATE_SUPERSEDED, "re-proposed by a later shipment", "SYSTEM"),
    (STATE_NEEDS_ASSIGNMENT, STATE_APPROVED, "human assigned this row to a line", "HUMAN"),
    (STATE_NEEDS_ASSIGNMENT, STATE_DISCARDED, "closed without writing", "HUMAN"),
    (STATE_NEEDS_ASSIGNMENT, STATE_SUPERSEDED, "re-proposed by a later shipment", "SYSTEM"),
    (STATE_NO_CHANGE, STATE_APPROVED, "quantity fine, a receipt date is still wanted", "HUMAN"),
    (STATE_NO_CHANGE, STATE_DISCARDED, "closed without writing", "HUMAN"),
    (STATE_NO_CHANGE, STATE_SUPERSEDED, "re-proposed by a later shipment", "SYSTEM"),
    (STATE_MANUAL_ENTRY_REQUIRED, STATE_DISCARDED, "keyed into NetSuite by hand", "HUMAN"),
    (STATE_MANUAL_ENTRY_REQUIRED, STATE_SUPERSEDED, "re-proposed by a later shipment", "SYSTEM"),
    (STATE_APPROVED, STATE_WRITTEN, "every approved scope written", "SYSTEM"),
    (STATE_APPROVED, STATE_WRITE_FAILED, "at least one approved scope failed", "SYSTEM"),
    (STATE_APPROVED, STATE_DISCARDED, "cancelled before the write", "HUMAN"),
    (STATE_WRITE_FAILED, STATE_WRITTEN, "retry succeeded", "SYSTEM"),
    (STATE_WRITE_FAILED, STATE_APPROVED, "re-queued for retry", "SYSTEM"),
    (STATE_WRITE_FAILED, STATE_DISCARDED, "given up on", "HUMAN"),
    # The one backwards edge, and the reason WRITTEN is not terminal: quantity is
    # knowable from the packing slip immediately, the receipt date waits on the
    # freight forwarder and may arrive weeks later.
    (STATE_WRITTEN, STATE_APPROVED, "date supplied after the quantity was written", "HUMAN"),
)


class IllegalTransition(Exception):
    """
    Raised when a state change is not in `change_state_transitions`.

    Carries both states so the message is actionable without a lookup.
    """


def legal_transitions(conn) -> set[tuple[str, str]]:
    """
    The legal `(from, to)` pairs, **read from the database**.

    This is the runtime authority, deliberately. `CHANGE_STATE_TRANSITIONS` above
    is the *authoring* source: migration 0001 seeds the table from it, and
    `test_schema.py` asserts the two still agree. But the check that actually runs
    reads the table, because otherwise the table would be decoration -- seeded,
    never consulted, free to drift from the code by a migration that touches one
    and not the other. There is one runtime authority and it is the deployed
    schema.

    The result is small (about thirty rows) and read per call rather than cached;
    at this volume the query is not worth a cache invalidation problem.
    """
    rows = conn.execute(
        change_state_transitions.select().with_only_columns(
            change_state_transitions.c.from_state, change_state_transitions.c.to_state
        )
    ).all()
    return {(r.from_state, r.to_state) for r in rows}


def assert_transition(conn, from_state: str, to_state: str) -> None:
    """
    Refuse a state change the seeded table does not allow.

    Called on every write of `proposed_changes.state`, including the initial
    insert (whose `from_state` is the `(insert)` sentinel).
    """
    if (from_state, to_state) not in legal_transitions(conn):
        raise IllegalTransition(
            f"{from_state} -> {to_state} is not a legal transition. The legal set lives in "
            "change_state_transitions, seeded by migration 0001; add a row there (via a new "
            "migration) rather than working around this."
        )


# ---------------------------------------------------------------------------
# The line
# ---------------------------------------------------------------------------

proposed_changes = Table(
    "proposed_changes",
    metadata,
    Column("id", Unicode(36), primary_key=True),
    Column("shipment_id", Unicode(36), ForeignKey("shipments.id"), nullable=False),
    Column("shipment_po_id", Unicode(36), ForeignKey("shipment_pos.id"), nullable=False),
    Column("state", Unicode(32), ForeignKey("change_states.state"), nullable=False),
    # -- the canonical key. Casefolded per canonical.py, NEVER NULL. '' means "the
    # -- document did not state it" (a sizeless row). Row identity is THIS, never a
    # -- digest of the whole row: verbatim display text varies between extraction
    # -- runs by design, so a row hash would see phantom changes on every re-parse.
    Column("key_style", Unicode(64), nullable=False),
    Column("key_color", Unicode(64), nullable=False),
    Column("key_size", Unicode(32), nullable=False),
    # -- verbatim, as the vendor printed it. Preserved for audit and for showing
    # -- Paula what the document actually said. NEVER used for comparison.
    Column("src_style_text", Unicode(200), nullable=False),
    Column("src_color_text", Unicode(200), nullable=False),
    Column("src_size_text", Unicode(100), nullable=False),
    Column("src_quantity_text", Unicode(100)),
    Column("source_hint", Unicode(120)),  # 'PACKING!R42' / 'P2!R17'
    Column("source_sha256", Unicode(64), ForeignKey("attachments.content_sha256")),
    # -- how the colour was resolved (migration 0002). Persisted because it is NOT
    # -- reconstructable later: the item read is not stored, and a PO's colour set
    # -- changes as lines are added or received. "Why did NEW INDIGO become NIN"
    # -- has to be answerable from this row alone.
    # --   CODE       the printed value was already a NetSuite colour code
    # --   NAME       recovered from the item's long-form colour name
    # --   AMBIGUOUS  the name matched two colours on this PO; nothing was chosen
    # --   UNRESOLVED neither path matched
    Column("colour_resolution_method", Unicode(12)),
    Column("colour_printed_key", Unicode(64)),   # canonical value actually looked up
    Column("colour_resolved_code", Unicode(64)),  # the NetSuite code it resolved to
    Column("colour_resolved_name", Unicode(200)),  # the long name that supplied it
    Column("colour_name_source_item_id", Unicode(40)),  # whose item record said so
    # -- how the SIZE was arrived at, when it was composed from two axes rather
    # -- than printed in one place (migration 0003). Tainan's sheet puts the waist
    # -- in a column header and the inseam in a row-block label several rows above,
    # -- so `32-34` exists in neither cell. Persisted for the same reason as the
    # -- colour columns: "why is this line 32-34" is answerable only from the pair
    # -- of cells it came from, and nothing downstream can reconstruct that pairing
    # -- from the size string. NULL on single-axis documents, which is most of them.
    # --   COMPOSED              both axes read, and the result IS a real size
    # --   COMPOSITION_REJECTED  the result is not a value in the account's size
    # --                         list, so it was flagged and never treated as a
    # --                         matchable size
    Column("size_composition_method", Unicode(24)),
    Column("src_size_axis_primary", Unicode(60)),    # verbatim, e.g. '30'
    Column("src_size_axis_secondary", Unicode(60)),  # verbatim, e.g. 'INS 32'
    # -- the transport-mode recap row this shipment row came off (migration 0004).
    # -- `key_recap_label` is canonical and NEVER NULL -- '' means "the slip had a
    # -- single recap row", which is the normal case. It is part of the canonical
    # -- key because the SLIP labels its own rows, so the distinction genuinely
    # -- exists in the source; contrast the NetSuite side, where no field
    # -- distinguishes a sea line from an air line and the key therefore cannot be
    # -- improved (RUNBOOK section 6 item 10).
    Column("key_recap_label", Unicode(40), nullable=False, server_default=""),
    Column("src_recap_label", Unicode(60)),  # verbatim, e.g. 'By Sea'
    # -- the five figures the review screen needs, so a human can read the
    # -- situation directly: "ordered 300, received 0, this slip 128" makes a
    # -- partial delivery self-evident. `outstanding` is derived
    # -- (current_quantity - current_quantity_received), exposed by v_review_lines.
    # -- Nothing gates on any of them; see the rationale doc, constraint 10.
    Column("ns_line_id", Unicode(40)),  # NULL until a target is chosen
    Column("ns_item_internal_id", Unicode(40)),
    Column("current_quantity", Numeric(12, 3)),
    Column("current_quantity_received", Numeric(12, 3)),
    Column("proposed_quantity", Numeric(12, 3)),
    # -- HOW that proposed quantity was arrived at (migration 0007). Slips
    # -- ACCUMULATE: a second shipment's quantity adds to what was written for the
    # -- first, because the vendor's slip shows only the new shipment (Paula,
    # -- 2026-09-16). So `proposed_quantity` is a TOTAL, and on its own it does not
    # -- say what it was a total OF.
    # --   FIRST_SHIPMENT        nothing written before; base 0, proposal = this slip
    # --   ACCUMULATED           base = what this tool last wrote, + this slip
    # --   PRE_EXISTING_RECEIPT  no history AND the line already carries receipts
    # --                         from before this tool existed. NOT a dispute --
    # --                         nothing contradicts anything, there is just nothing
    # --                         to add to. Expected in a batch on first contact,
    # --                         asks for a one-time confirmation, and RETIRES
    # --                         ITSELF: the confirmation becomes history, so the
    # --                         next slip on that line is ACCUMULATED.
    # --   DISPUTED              our record and NetSuite CONTRADICT each other --
    # --                         we wrote one number and the line holds another, or
    # --                         the line moved since we last looked. Someone
    # --                         changed something behind the tool. Kept separate
    # --                         from PRE_EXISTING_RECEIPT so a predictable wave
    # --                         cannot train a reviewer to skim past the word that
    # --                         means alarm.
    # -- Neither of the last two proposes anything; `attention_reason` carries the
    # -- numbers.
    # -- The base is this tool's OWN audit trail, never NetSuite's current
    # -- quantity -- that is a field this tool writes, and feeding it back in would
    # -- let past output decide future output (lesson 13). See
    # -- `matcher._accumulated_quantity`.
    # Unicode(32), not (16). 0008 added PRE_EXISTING_RECEIPT -- 20 characters --
    # to a column declared 16 wide and nothing complained, because SQLite does not
    # enforce a VARCHAR length. SQL Server does: error 2628, "Truncated value:
    # 'PRE_EXISTING_REC'". A declared length that contradicts the column's own
    # CHECK constraint is valid on one engine and broken on the other; see
    # RUNBOOK section 8.
    Column("accumulation_basis", Unicode(32)),
    Column("accumulation_base_quantity", Numeric(12, 3)),
    # -- NetSuite's date state at proposal time, for display beside the reference dates
    Column("current_expected_receipt_date", Date),
    Column("current_updated_receipt_date", Date),
    Column("current_override_flag", Boolean),
    Column("ns_line_is_open", Boolean),
    Column("ns_line_closed", Boolean),
    # -- calibration: the tool's claim...
    Column("extraction_confidence", Unicode(8), nullable=False),
    Column("extraction_note", Unicode(1000)),
    Column("needs_review", Boolean, nullable=False, default=False),
    Column("attention_reason", UNBOUNDED_TEXT),
    # -- ...paired with what turned out to be true. Without both halves,
    # -- needs_review can never be calibrated. See rationale constraint 8.
    Column("human_verdict", Unicode(20)),
    Column("human_verdict_note", Unicode(1000)),
    Column("verdict_by", Unicode(320)),
    Column("verdict_at", DateTime),
    # -- approval, per scope. Quantity and date are independent, and the date is
    # -- optional forever: quantity is knowable from the slip on arrival, the
    # -- receipt date waits on the freight forwarder.
    Column("approved_quantity", Numeric(12, 3)),
    Column("quantity_approved_by", Unicode(320)),
    Column("quantity_approved_at", DateTime),
    Column("quantity_write_status", Unicode(16), nullable=False, default="NONE"),
    Column("confirmed_receipt_date", Date),  # Paula's, never a vendor document's
    Column("date_approved_by", Unicode(320)),
    Column("date_approved_at", DateTime),
    Column("date_write_status", Unicode(16), nullable=False, default="NONE"),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    CheckConstraint(
        "extraction_confidence IN ('high','medium','low')", name="extraction_confidence"
    ),
    CheckConstraint(
        "quantity_write_status IN ('NONE','APPROVED','WRITTEN','FAILED')",
        name="quantity_write_status",
    ),
    CheckConstraint(
        "date_write_status IN ('NONE','APPROVED','WRITTEN','FAILED')",
        name="date_write_status",
    ),
    CheckConstraint(
        "human_verdict IS NULL OR human_verdict IN "
        "('ACCEPTED','CORRECTED','REJECTED','CANDIDATE_PICKED')",
        name="human_verdict",
    ),
    CheckConstraint(
        "colour_resolution_method IS NULL OR colour_resolution_method IN "
        "('CODE','NAME','AMBIGUOUS','UNRESOLVED')",
        name="colour_resolution_method",
    ),
    # A NAME resolution has to say which name and which item said so, or it is not
    # provenance -- just an assertion. CODE needs neither: the printed value WAS
    # the code, and there is nothing to attribute.
    CheckConstraint(
        "colour_resolution_method <> 'NAME' OR "
        "(colour_resolved_code IS NOT NULL AND colour_resolved_name IS NOT NULL "
        " AND colour_name_source_item_id IS NOT NULL)",
        name="name_resolution_needs_provenance",
    ),
    CheckConstraint(
        "size_composition_method IS NULL OR size_composition_method IN "
        "('COMPOSED','COMPOSITION_REJECTED')",
        name="size_composition_method",
    ),
    # Same principle as the colour constraint above: a claim that a size was
    # composed is not provenance unless BOTH source axes are recorded. Half a
    # composition is unauditable -- '32-34' with only the waist kept leaves no way
    # to tell whether the inseam was read or assumed.
    CheckConstraint(
        "size_composition_method IS NULL OR "
        "(src_size_axis_primary IS NOT NULL AND src_size_axis_secondary IS NOT NULL)",
        name="composition_needs_both_axes",
    ),
    # No target line => cannot be approved or written. This is what makes
    # NEEDS_RESOLUTION a real state rather than a null FK with a comment beside it.
    CheckConstraint(
        "state NOT IN ('APPROVED','WRITTEN','WRITE_FAILED') OR ns_line_id IS NOT NULL",
        name="target_required",
    ),
    # A date exists in this database only if a human put it there. The scope
    # boundary "dates never come from a vendor document" as a constraint, not a
    # convention -- vendor_etd/vendor_eta live on `shipments` and there is no
    # column here they could be copied into without tripping this.
    CheckConstraint(
        "confirmed_receipt_date IS NULL OR date_approved_by IS NOT NULL",
        name="date_needs_human",
    ),
    CheckConstraint(
        "date_write_status = 'NONE' OR confirmed_receipt_date IS NOT NULL",
        name="date_scope_needs_date",
    ),
    CheckConstraint(
        "quantity_write_status = 'NONE' OR approved_quantity IS NOT NULL",
        name="quantity_scope_needs_quantity",
    ),
    CheckConstraint(
        "accumulation_basis IS NULL OR accumulation_basis IN "
        "('FIRST_SHIPMENT','ACCUMULATED','PRE_EXISTING_RECEIPT','DISPUTED')",
        name="accumulation_basis",
    ),
    # Same principle as the colour and size provenance constraints: a claim that
    # a quantity was accumulated is not auditable unless the row also says what it
    # was accumulated ONTO. Without the base, `proposed_quantity` is a total whose
    # parts cannot be recovered -- and the parts are the whole question when a
    # reviewer asks why a slip saying 100 proposed 228.
    CheckConstraint(
        "accumulation_basis <> 'ACCUMULATED' OR accumulation_base_quantity IS NOT NULL",
        name="accumulation_needs_base",
    ),
)

# One row per canonical key per PO per shipment -- the extraction layer already
# collapses duplicate keys within a document and sums them, and this is that
# invariant enforced rather than trusted. Sizeless rows are exempt because
# aggregation deliberately never collapses them, which is exactly why key_size
# uses '' rather than NULL.
Index(
    "ux_proposed_changes_canonical_key",
    proposed_changes.c.shipment_po_id,
    proposed_changes.c.key_style,
    proposed_changes.c.key_color,
    proposed_changes.c.key_size,
    # Widened by migration 0004. A slip that splits one size across `By Sea` and
    # `By UPS` legitimately produces TWO rows for the same style/colour/size, and
    # the un-widened index forbade exactly that -- it would have rejected the
    # second row instead of letting a human assign it. `''` for the single-recap
    # documents keeps the original invariant intact for them.
    proposed_changes.c.key_recap_label,
    unique=True,
    **_partial("key_size <> ''"),
)
Index("ix_proposed_changes_state", proposed_changes.c.state)
Index("ix_proposed_changes_shipment_id", proposed_changes.c.shipment_id)

# TWO CHANGES MUST NOT TARGET ONE NETSUITE LINE. Added by migration 0004, and it
# closes the mirror image of an existing guard: `ux_change_candidates_one_selected`
# stops one change selecting two candidate lines, and nothing stopped two changes
# selecting one line. That is the failure this change makes reachable -- a slip
# with two transport-mode rows for one size, where an automatic pairing (or a
# careless human one) points both at the same line and the second write silently
# overwrites the first.
#
# Scoped per SHIPMENT, not globally: a later shipment updating the same line again
# is normal and correct. `IS NOT NULL` is required rather than tidy -- Azure SQL
# treats NULLs as equal in a unique index, so without it the many rows that have
# no target yet would collide with each other there while passing on SQLite.
Index(
    "ux_proposed_changes_one_line_per_shipment",
    proposed_changes.c.shipment_id,
    proposed_changes.c.ns_line_id,
    unique=True,
    **_partial("ns_line_id IS NOT NULL"),
)

change_candidates = Table(
    "change_candidates",
    metadata,
    Column("id", Unicode(36), primary_key=True),
    Column("change_id", Unicode(36), ForeignKey("proposed_changes.id"), nullable=False),
    Column("ns_line_id", Unicode(40), nullable=False),
    # Everything a human needs to choose between lines that share a canonical key.
    Column("quantity", Numeric(12, 3)),
    Column("quantity_received", Numeric(12, 3)),
    Column("quantity_billed", Numeric(12, 3)),
    Column("expected_receipt_date", Date),
    Column("updated_receipt_date", Date),
    Column("override_expected_receipt", Boolean),
    Column("rate", Numeric(12, 4)),
    Column("is_open", Boolean, nullable=False),
    Column("selected", Boolean, nullable=False, default=False),
    UniqueConstraint("change_id", "ns_line_id", name="uq_change_candidates_line"),
    # Deliberately NO column for custcol_sd_fg_excluderepspark -- here least of
    # all, since it is the obvious thing to show and it also failed as a
    # discriminator. Paula manages that field by hand; this tool never touches it.
)

# At most one candidate can be the chosen one.
Index(
    "ux_change_candidates_one_selected",
    change_candidates.c.change_id,
    unique=True,
    **_partial("selected = 1"),
)

#: The poll watermark: where the last successful batch got to, per mailbox.
#:
#: **Stored as a position, not a cursor.** Graph offers delta tokens; this uses
#: `received_at` deliberately, because a delta token is opaque state owned by the
#: server that cannot be reasoned about, replayed, or nudged backwards by an
#: operator who needs to re-read a week. A timestamp can.
#:
#: **Read back with an OVERLAP, never a bare `>`.** Two messages delivered in the
#: same second, or a clock a second out between Graph and here, silently lose mail
#: on a strict comparison -- and the loss is invisible, because nothing knows the
#: message existed. The overlap re-reads a little every time; dedup on
#: `graph_message_id` makes that free. See `poller.POLL_OVERLAP`.
#:
#: **Advanced only after the batch commits**, so a crash mid-batch re-reads rather
#: than skips.
poll_state = Table(
    "poll_state",
    metadata,
    Column("mailbox", Unicode(320), primary_key=True),
    #: High-water mark: the newest `received_at` that has been durably stored.
    Column("last_received_at", DateTime, nullable=False),
    #: When the poll ran. Distinct from the watermark: a poll that finds nothing
    #: still advances this, which is how "the job is alive but the mailbox is
    #: quiet" is told apart from "the job stopped running" (build plan Phase 4,
    #: monitoring).
    Column("last_polled_at", DateTime, nullable=False),
    Column("messages_seen", Integer, nullable=False, default=0),
)


write_attempts = Table(
    "write_attempts",
    metadata,
    Column("id", Unicode(36), primary_key=True),
    Column("change_id", Unicode(36), ForeignKey("proposed_changes.id"), nullable=False),
    # Per line AND per scope. One approval can fan out to writes across six POs and
    # the fourth can fail; recovery must not re-approve what already succeeded.
    Column("scope", Unicode(16), nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("ns_internal_id", Unicode(40), nullable=False),
    Column("ns_line_id", Unicode(40), nullable=False),
    # Exactly what was sent. For a date write that is all three fields with the
    # same value -- NetSuite does not derive expectedReceiptDate from the override
    # pair (tested 2026-08-12), so the payload proves what was actually asserted.
    Column("payload_json", UNBOUNDED_TEXT, nullable=False),
    Column("idempotency_key", Unicode(64), nullable=False),
    Column("outcome", Unicode(16), nullable=False),
    Column("http_status", Integer),
    # Retry policy hangs off this: TRANSIENT is retryable, PERMISSION and
    # LINE_CLOSED never are. NetSuite returns permission denials as 400s, so the
    # HTTP status alone cannot decide.
    Column("error_kind", Unicode(24)),
    Column("error_detail", UNBOUNDED_TEXT),
    Column("attempted_at", DateTime, nullable=False),
    UniqueConstraint("change_id", "scope", "attempt_no", name="uq_write_attempts_attempt"),
    CheckConstraint("scope IN ('QUANTITY','DATE')", name="scope"),
    CheckConstraint("outcome IN ('SUCCESS','FAILED')", name="outcome"),
    CheckConstraint(
        "error_kind IS NULL OR error_kind IN "
        "('PERMISSION','TRANSIENT','CONFLICT','LINE_CLOSED','VALIDATION','OTHER')",
        name="error_kind",
    ),
)

audit_log = Table(
    "audit_log",
    metadata,
    # APPEND ONLY. Nothing in the application may UPDATE or DELETE this table.
    # "Why did this line change, six months ago" has to be answerable for both
    # workflows, so `workflow` and `actor_kind` are recorded on every row rather
    # than inferred later from whether a message id happens to be present.
    Column("id", Unicode(36), primary_key=True),
    Column("occurred_at", DateTime, nullable=False),
    Column("workflow", Unicode(20), nullable=False),
    Column("actor", Unicode(320), nullable=False),
    Column("actor_kind", Unicode(8), nullable=False),
    Column("event", Unicode(64), nullable=False),
    Column("message_id", Unicode(36), ForeignKey("messages.id")),
    Column("shipment_id", Unicode(36), ForeignKey("shipments.id")),
    Column("change_id", Unicode(36), ForeignKey("proposed_changes.id")),
    Column("from_state", Unicode(32)),
    Column("to_state", Unicode(32)),
    Column("detail_json", UNBOUNDED_TEXT),
    CheckConstraint("workflow IN ('PACKING_SLIP','PAULA_DIRECTED')", name="workflow"),
    CheckConstraint("actor_kind IN ('HUMAN','SYSTEM')", name="actor_kind"),
)
Index("ix_audit_log_change_id_occurred_at", audit_log.c.change_id, audit_log.c.occurred_at)


# ---------------------------------------------------------------------------
# Views. ANSI-only SQL so one statement serves both engines.
# ---------------------------------------------------------------------------

#: The five figures, with `outstanding` derived rather than stored -- a stored copy
#: would be a second source of truth for simple arithmetic.
VIEW_REVIEW_LINES = """
CREATE VIEW v_review_lines AS
SELECT pc.id                        AS change_id,
       pc.shipment_id               AS shipment_id,
       sp.po_number_printed         AS po_number_printed,
       sp.ns_tranid                 AS ns_tranid,
       pc.src_style_text            AS style_printed,
       pc.src_color_text            AS color_printed,
       pc.src_size_text             AS size_printed,
       pc.src_recap_label           AS recap_label_printed,
       pc.state                     AS state,
       pc.ns_line_id                AS ns_line_id,
       pc.current_quantity          AS current_quantity,
       pc.current_quantity_received AS current_quantity_received,
       pc.proposed_quantity         AS proposed_quantity,
       pc.current_quantity - COALESCE(pc.current_quantity_received, 0) AS outstanding,
       pc.accumulation_basis        AS accumulation_basis,
       pc.accumulation_base_quantity AS accumulation_base_quantity,
       pc.colour_resolution_method  AS colour_resolution_method,
       pc.colour_resolved_code      AS colour_resolved_code,
       pc.colour_resolved_name      AS colour_resolved_name,
       pc.size_composition_method   AS size_composition_method,
       pc.src_size_axis_primary     AS size_axis_primary_printed,
       pc.src_size_axis_secondary   AS size_axis_secondary_printed
FROM proposed_changes pc
JOIN shipment_pos sp ON sp.id = pc.shipment_po_id
"""

#: The calibration corpus: the tool's claim beside what turned out to be true.
#: Only useful if the review UI records a verdict on EVERY line it shows,
#: including the ones accepted unchanged -- otherwise there are no negatives.
VIEW_CALIBRATION = """
CREATE VIEW v_calibration AS
SELECT pc.id                    AS change_id,
       s.parser                 AS parser,
       s.extractor_model        AS extractor_model,
       s.extractor_prompt_version AS extractor_prompt_version,
       pc.extraction_confidence AS extraction_confidence,
       pc.needs_review          AS needs_review,
       s.doc_needs_review       AS doc_needs_review,
       pc.state                 AS state,
       pc.human_verdict         AS human_verdict,
       pc.proposed_quantity     AS proposed_quantity,
       pc.approved_quantity     AS approved_quantity,
       CASE WHEN pc.approved_quantity IS NOT NULL
                 AND pc.approved_quantity <> pc.proposed_quantity
            THEN 1 ELSE 0 END   AS quantity_was_corrected,
       pc.verdict_at            AS verdict_at
FROM proposed_changes pc
JOIN shipments s ON s.id = pc.shipment_id
"""

VIEWS = (("v_review_lines", VIEW_REVIEW_LINES), ("v_calibration", VIEW_CALIBRATION))


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


@event.listens_for(Engine, "connect")
def _enforce_sqlite_foreign_keys(dbapi_connection, connection_record):
    """
    SQLite ignores foreign keys unless asked, per connection.

    Registered on the Engine class rather than an instance so no caller can forget
    it. Without this, every FK in this file is decorative -- and it fails silently,
    which is the worst way for a constraint to be absent.
    """
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def connect(url: str):
    """An engine with SQLite's foreign keys actually switched on."""
    from sqlalchemy import create_engine

    return create_engine(url)
