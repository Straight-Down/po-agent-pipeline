"""text columns become NVARCHAR

Every text column in the schema changes from `VARCHAR` to `NVARCHAR` on SQL
Server. 108 sized columns plus 7 unbounded ones: 115 in total.

## Why, in one measurement

The deployment database is Azure SQL with a Latin-1 collation. Under it a
`VARCHAR` column converts anything outside the code page to `?` **on write**, and
the conversion is irreversible -- re-collating the column afterwards recovers
nothing. Measured server-side, with the client driver deliberately out of the
path (the CJK was built with `NCHAR()` so the query text was pure ASCII):

    'black' as VARCHAR -> 0x3f3f
    'red'   as VARCHAR -> 0x3f3f
    are they EQUAL?    -> YES

Two different vendor colours become the same value. `canonical()` does not
transliterate -- it folds full-width forms and dashes and nothing else, so CJK
reaches the canonical key columns intact -- and those columns are covered by
`ux_proposed_changes_canonical_key`, a UNIQUE index. So the second colour is
rejected as a duplicate key: a valid shipment line lost, and the error arrives at
the wrong layer, long after the conversion that caused it.

Applied to ALL 115 rather than the 39 that can currently receive non-ASCII.
Uniform beats classified here because the cost of classifying wrongly is silent,
and this repo has twice been bitten by a classification that was correct when it
was written.

## Why this is a no-op on SQLite

SQLite has type affinity, not types: `VARCHAR(64)` and `NVARCHAR(64)` are the
same thing to it and always were. There is nothing to alter. This is also why
`alembic check` reports NO DRIFT across all 115 changes -- it compares two
things that are genuinely identical in the dialect it runs against. A green
SQLite run is not weak evidence for this migration; it is no evidence. See
RUNBOOK section 8 lesson 24, and the acceptance list in the same entry, which is
verified against `sys.columns` on a real server instead.

## Why it is the largest migration in the project

`VARCHAR` -> `NVARCHAR` is a TYPE change, not a length change. Every SQL Server
exception that permits altering an indexed or constrained column applies only to
length changes of a variable-length type, so none of them apply. Everything
dependent must come down first, and every schema object in this database is
dependent:

    13 primary keys        every one is on a text column
    19 foreign keys        every one references such a primary key
     6 unique constraints
     8 indexes             4 of them FILTERED
    25 check constraints   every one names a converted column
     2 views
     1 default constraint  proposed_changes.key_recap_label

That default is the trap. It was created unnamed (`server_default=""` in 0004),
so SQL Server generated a name like `DF__proposed___key_r__3B75D760` which is
not knowable in advance. It has to be discovered from `sys.default_constraints`
at run time, dropped, and recreated. A migration that names it fails on every
database except the one it was written against.

## The filtered indexes are the risk

Four indexes carry a `WHERE` predicate. They are recreated here as hand-written
DDL, and `schema._partial()`'s compiler guard CANNOT see that -- it fires when
SQLAlchemy compiles an Index object for an unsupported dialect, not when a
migration spells one out by hand. Losing a predicate turns a filtered index into
a FULL unique index, which is a stricter and different constraint: without
`WHERE key_size <> ''` the second sizeless row on a PO is rejected, and without
`WHERE selected = 1` a change may hold only ONE candidate line. So the predicates
are literals below and are checked explicitly after the run.

## The downgrade is lossy, and refuses rather than pretend otherwise

Going back to `VARCHAR` destroys exactly what this migration exists to preserve.
The downgrade therefore scans every converted column for a value that would not
survive the round trip, and raises if it finds one. On an empty database -- which
is what the round-trip test uses -- it passes and the migration reverses cleanly.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-23 10:40:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None

#: (table, column, nvarchar_type, varchar_type, nullable). Frozen literals: this
#: migration never imports `schema.py`, so it keeps describing what it actually
#: did after the metadata moves on.
COLUMNS = (
    ('attachments', 'content_sha256', 'NVARCHAR(64)', 'VARCHAR(64)', False),
    ('attachments', 'doc_type', 'NVARCHAR(32)', 'VARCHAR(32)', False),
    ('attachments', 'doc_type_reason', 'NVARCHAR(1000)', 'VARCHAR(1000)', True),
    ('attachments', 'open_failure_reason', 'NVARCHAR(500)', 'VARCHAR(500)', True),
    ('attachments', 'stored_uri', 'NVARCHAR(1000)', 'VARCHAR(1000)', True),
    ('change_states', 'state', 'NVARCHAR(32)', 'VARCHAR(32)', False),
    ('change_states', 'description', 'NVARCHAR(500)', 'VARCHAR(500)', False),
    ('messages', 'id', 'NVARCHAR(36)', 'VARCHAR(36)', False),
    ('messages', 'graph_message_id', 'NVARCHAR(512)', 'VARCHAR(512)', False),
    ('messages', 'internet_message_id', 'NVARCHAR(512)', 'VARCHAR(512)', True),
    ('messages', 'mailbox', 'NVARCHAR(320)', 'VARCHAR(320)', False),
    ('messages', 'subject', 'NVARCHAR(1000)', 'VARCHAR(1000)', True),
    ('messages', 'from_address', 'NVARCHAR(320)', 'VARCHAR(320)', True),
    ('messages', 'forwarded_by', 'NVARCHAR(320)', 'VARCHAR(320)', True),
    ('messages', 'folder_id', 'NVARCHAR(512)', 'VARCHAR(512)', True),
    ('messages', 'poll_error', 'NVARCHAR(1000)', 'VARCHAR(1000)', True),
    ('messages', 'extraction_error', 'NVARCHAR(1000)', 'VARCHAR(1000)', True),
    ('poll_state', 'mailbox', 'NVARCHAR(320)', 'VARCHAR(320)', False),
    ('change_state_transitions', 'from_state', 'NVARCHAR(32)', 'VARCHAR(32)', False),
    ('change_state_transitions', 'to_state', 'NVARCHAR(32)', 'VARCHAR(32)', False),
    ('change_state_transitions', 'trigger', 'NVARCHAR(64)', 'VARCHAR(64)', False),
    ('change_state_transitions', 'actor_kind', 'NVARCHAR(8)', 'VARCHAR(8)', False),
    ('message_attachments', 'id', 'NVARCHAR(36)', 'VARCHAR(36)', False),
    ('message_attachments', 'message_id', 'NVARCHAR(36)', 'VARCHAR(36)', False),
    ('message_attachments', 'content_sha256', 'NVARCHAR(64)', 'VARCHAR(64)', False),
    ('message_attachments', 'filename', 'NVARCHAR(500)', 'VARCHAR(500)', False),
    ('shipments', 'id', 'NVARCHAR(36)', 'VARCHAR(36)', False),
    ('shipments', 'origin', 'NVARCHAR(16)', 'VARCHAR(16)', False),
    ('shipments', 'message_id', 'NVARCHAR(36)', 'VARCHAR(36)', True),
    ('shipments', 'primary_attachment_sha', 'NVARCHAR(64)', 'VARCHAR(64)', True),
    ('shipments', 'source_set_hash', 'NVARCHAR(64)', 'VARCHAR(64)', True),
    ('shipments', 'superseded_by_shipment_id', 'NVARCHAR(36)', 'VARCHAR(36)', True),
    ('shipments', 'vendor_name', 'NVARCHAR(200)', 'VARCHAR(200)', True),
    ('shipments', 'vendor_etd', 'NVARCHAR(40)', 'VARCHAR(40)', True),
    ('shipments', 'vendor_eta', 'NVARCHAR(40)', 'VARCHAR(40)', True),
    ('shipments', 'parser', 'NVARCHAR(64)', 'VARCHAR(64)', True),
    ('shipments', 'extractor_model', 'NVARCHAR(64)', 'VARCHAR(64)', True),
    ('shipments', 'extractor_prompt_version', 'NVARCHAR(64)', 'VARCHAR(64)', True),
    ('shipments', 'parse_warnings_json', 'NVARCHAR(MAX)', 'VARCHAR(MAX)', True),
    ('shipments', 'parse_notes_json', 'NVARCHAR(MAX)', 'VARCHAR(MAX)', True),
    ('shipments', 'created_by', 'NVARCHAR(320)', 'VARCHAR(320)', False),
    ('shipment_pos', 'id', 'NVARCHAR(36)', 'VARCHAR(36)', False),
    ('shipment_pos', 'shipment_id', 'NVARCHAR(36)', 'VARCHAR(36)', False),
    ('shipment_pos', 'po_number_printed', 'NVARCHAR(120)', 'VARCHAR(120)', False),
    ('shipment_pos', 'po_number_key', 'NVARCHAR(40)', 'VARCHAR(40)', False),
    ('shipment_pos', 'ns_tranid', 'NVARCHAR(40)', 'VARCHAR(40)', True),
    ('shipment_pos', 'ns_internal_id', 'NVARCHAR(40)', 'VARCHAR(40)', True),
    ('shipment_pos', 'resolution_status', 'NVARCHAR(16)', 'VARCHAR(16)', False),
    ('shipment_pos', 'resolution_strategy', 'NVARCHAR(64)', 'VARCHAR(64)', True),
    ('shipment_sources', 'id', 'NVARCHAR(36)', 'VARCHAR(36)', False),
    ('shipment_sources', 'shipment_id', 'NVARCHAR(36)', 'VARCHAR(36)', False),
    ('shipment_sources', 'content_sha256', 'NVARCHAR(64)', 'VARCHAR(64)', False),
    ('shipment_sources', 'role', 'NVARCHAR(16)', 'VARCHAR(16)', False),
    ('shipment_sources', 'exclusion_reason', 'NVARCHAR(500)', 'VARCHAR(500)', True),
    ('shipment_sources', 'agreement_json', 'NVARCHAR(MAX)', 'VARCHAR(MAX)', True),
    ('proposed_changes', 'id', 'NVARCHAR(36)', 'VARCHAR(36)', False),
    ('proposed_changes', 'shipment_id', 'NVARCHAR(36)', 'VARCHAR(36)', False),
    ('proposed_changes', 'shipment_po_id', 'NVARCHAR(36)', 'VARCHAR(36)', False),
    ('proposed_changes', 'state', 'NVARCHAR(32)', 'VARCHAR(32)', False),
    ('proposed_changes', 'key_style', 'NVARCHAR(64)', 'VARCHAR(64)', False),
    ('proposed_changes', 'key_color', 'NVARCHAR(64)', 'VARCHAR(64)', False),
    ('proposed_changes', 'key_size', 'NVARCHAR(32)', 'VARCHAR(32)', False),
    ('proposed_changes', 'src_style_text', 'NVARCHAR(200)', 'VARCHAR(200)', False),
    ('proposed_changes', 'src_color_text', 'NVARCHAR(200)', 'VARCHAR(200)', False),
    ('proposed_changes', 'src_size_text', 'NVARCHAR(100)', 'VARCHAR(100)', False),
    ('proposed_changes', 'src_quantity_text', 'NVARCHAR(100)', 'VARCHAR(100)', True),
    ('proposed_changes', 'source_hint', 'NVARCHAR(120)', 'VARCHAR(120)', True),
    ('proposed_changes', 'source_sha256', 'NVARCHAR(64)', 'VARCHAR(64)', True),
    ('proposed_changes', 'colour_resolution_method', 'NVARCHAR(12)', 'VARCHAR(12)', True),
    ('proposed_changes', 'colour_printed_key', 'NVARCHAR(64)', 'VARCHAR(64)', True),
    ('proposed_changes', 'colour_resolved_code', 'NVARCHAR(64)', 'VARCHAR(64)', True),
    ('proposed_changes', 'colour_resolved_name', 'NVARCHAR(200)', 'VARCHAR(200)', True),
    ('proposed_changes', 'colour_name_source_item_id', 'NVARCHAR(40)', 'VARCHAR(40)', True),
    ('proposed_changes', 'size_composition_method', 'NVARCHAR(24)', 'VARCHAR(24)', True),
    ('proposed_changes', 'src_size_axis_primary', 'NVARCHAR(60)', 'VARCHAR(60)', True),
    ('proposed_changes', 'src_size_axis_secondary', 'NVARCHAR(60)', 'VARCHAR(60)', True),
    ('proposed_changes', 'key_recap_label', 'NVARCHAR(40)', 'VARCHAR(40)', False),
    ('proposed_changes', 'src_recap_label', 'NVARCHAR(60)', 'VARCHAR(60)', True),
    ('proposed_changes', 'ns_line_id', 'NVARCHAR(40)', 'VARCHAR(40)', True),
    ('proposed_changes', 'ns_item_internal_id', 'NVARCHAR(40)', 'VARCHAR(40)', True),
    ('proposed_changes', 'accumulation_basis', 'NVARCHAR(16)', 'VARCHAR(16)', True),
    ('proposed_changes', 'extraction_confidence', 'NVARCHAR(8)', 'VARCHAR(8)', False),
    ('proposed_changes', 'extraction_note', 'NVARCHAR(1000)', 'VARCHAR(1000)', True),
    ('proposed_changes', 'attention_reason', 'NVARCHAR(MAX)', 'VARCHAR(MAX)', True),
    ('proposed_changes', 'human_verdict', 'NVARCHAR(20)', 'VARCHAR(20)', True),
    ('proposed_changes', 'human_verdict_note', 'NVARCHAR(1000)', 'VARCHAR(1000)', True),
    ('proposed_changes', 'verdict_by', 'NVARCHAR(320)', 'VARCHAR(320)', True),
    ('proposed_changes', 'quantity_approved_by', 'NVARCHAR(320)', 'VARCHAR(320)', True),
    ('proposed_changes', 'quantity_write_status', 'NVARCHAR(16)', 'VARCHAR(16)', False),
    ('proposed_changes', 'date_approved_by', 'NVARCHAR(320)', 'VARCHAR(320)', True),
    ('proposed_changes', 'date_write_status', 'NVARCHAR(16)', 'VARCHAR(16)', False),
    ('audit_log', 'id', 'NVARCHAR(36)', 'VARCHAR(36)', False),
    ('audit_log', 'workflow', 'NVARCHAR(20)', 'VARCHAR(20)', False),
    ('audit_log', 'actor', 'NVARCHAR(320)', 'VARCHAR(320)', False),
    ('audit_log', 'actor_kind', 'NVARCHAR(8)', 'VARCHAR(8)', False),
    ('audit_log', 'event', 'NVARCHAR(64)', 'VARCHAR(64)', False),
    ('audit_log', 'message_id', 'NVARCHAR(36)', 'VARCHAR(36)', True),
    ('audit_log', 'shipment_id', 'NVARCHAR(36)', 'VARCHAR(36)', True),
    ('audit_log', 'change_id', 'NVARCHAR(36)', 'VARCHAR(36)', True),
    ('audit_log', 'from_state', 'NVARCHAR(32)', 'VARCHAR(32)', True),
    ('audit_log', 'to_state', 'NVARCHAR(32)', 'VARCHAR(32)', True),
    ('audit_log', 'detail_json', 'NVARCHAR(MAX)', 'VARCHAR(MAX)', True),
    ('change_candidates', 'id', 'NVARCHAR(36)', 'VARCHAR(36)', False),
    ('change_candidates', 'change_id', 'NVARCHAR(36)', 'VARCHAR(36)', False),
    ('change_candidates', 'ns_line_id', 'NVARCHAR(40)', 'VARCHAR(40)', False),
    ('write_attempts', 'id', 'NVARCHAR(36)', 'VARCHAR(36)', False),
    ('write_attempts', 'change_id', 'NVARCHAR(36)', 'VARCHAR(36)', False),
    ('write_attempts', 'scope', 'NVARCHAR(16)', 'VARCHAR(16)', False),
    ('write_attempts', 'ns_internal_id', 'NVARCHAR(40)', 'VARCHAR(40)', False),
    ('write_attempts', 'ns_line_id', 'NVARCHAR(40)', 'VARCHAR(40)', False),
    ('write_attempts', 'payload_json', 'NVARCHAR(MAX)', 'VARCHAR(MAX)', False),
    ('write_attempts', 'idempotency_key', 'NVARCHAR(64)', 'VARCHAR(64)', False),
    ('write_attempts', 'outcome', 'NVARCHAR(16)', 'VARCHAR(16)', False),
    ('write_attempts', 'error_kind', 'NVARCHAR(24)', 'VARCHAR(24)', True),
    ('write_attempts', 'error_detail', 'NVARCHAR(MAX)', 'VARCHAR(MAX)', True),
)

#: (table, name, columns)
PRIMARY_KEYS = (
    ('attachments', 'pk_attachments', '[content_sha256]'),
    ('change_states', 'pk_change_states', '[state]'),
    ('messages', 'pk_messages', '[id]'),
    ('poll_state', 'pk_poll_state', '[mailbox]'),
    ('change_state_transitions', 'pk_change_state_transitions', '[from_state], [to_state], [trigger]'),
    ('message_attachments', 'pk_message_attachments', '[id]'),
    ('shipments', 'pk_shipments', '[id]'),
    ('shipment_pos', 'pk_shipment_pos', '[id]'),
    ('shipment_sources', 'pk_shipment_sources', '[id]'),
    ('proposed_changes', 'pk_proposed_changes', '[id]'),
    ('audit_log', 'pk_audit_log', '[id]'),
    ('change_candidates', 'pk_change_candidates', '[id]'),
    ('write_attempts', 'pk_write_attempts', '[id]'),
)

#: (table, name, columns)
UNIQUES = (
    ('messages', 'uq_messages_graph_message_id', '[graph_message_id]'),
    ('message_attachments', 'uq_message_attachments_pair', '[message_id], [content_sha256]'),
    ('shipment_pos', 'uq_shipment_pos_key', '[shipment_id], [po_number_key]'),
    ('shipment_sources', 'uq_shipment_sources_pair', '[shipment_id], [content_sha256]'),
    ('change_candidates', 'uq_change_candidates_line', '[change_id], [ns_line_id]'),
    ('write_attempts', 'uq_write_attempts_attempt', '[change_id], [scope], [attempt_no]'),
)

#: (table, name, local_columns, referred_table, referred_columns)
FOREIGN_KEYS = (
    ('change_state_transitions', 'fk_change_state_transitions_to_state', '[to_state]', 'change_states', '[state]'),
    ('change_state_transitions', 'fk_change_state_transitions_from_state', '[from_state]', 'change_states', '[state]'),
    ('message_attachments', 'fk_message_attachments_content_sha256', '[content_sha256]', 'attachments', '[content_sha256]'),
    ('message_attachments', 'fk_message_attachments_message_id', '[message_id]', 'messages', '[id]'),
    ('shipments', 'fk_shipments_superseded_by_shipment_id', '[superseded_by_shipment_id]', 'shipments', '[id]'),
    ('shipments', 'fk_shipments_primary_attachment_sha', '[primary_attachment_sha]', 'attachments', '[content_sha256]'),
    ('shipments', 'fk_shipments_message_id', '[message_id]', 'messages', '[id]'),
    ('shipment_pos', 'fk_shipment_pos_shipment_id', '[shipment_id]', 'shipments', '[id]'),
    ('shipment_sources', 'fk_shipment_sources_content_sha256', '[content_sha256]', 'attachments', '[content_sha256]'),
    ('shipment_sources', 'fk_shipment_sources_shipment_id', '[shipment_id]', 'shipments', '[id]'),
    ('proposed_changes', 'fk_proposed_changes_state', '[state]', 'change_states', '[state]'),
    ('proposed_changes', 'fk_proposed_changes_source_sha256', '[source_sha256]', 'attachments', '[content_sha256]'),
    ('proposed_changes', 'fk_proposed_changes_shipment_id', '[shipment_id]', 'shipments', '[id]'),
    ('proposed_changes', 'fk_proposed_changes_shipment_po_id', '[shipment_po_id]', 'shipment_pos', '[id]'),
    ('audit_log', 'fk_audit_log_change_id', '[change_id]', 'proposed_changes', '[id]'),
    ('audit_log', 'fk_audit_log_message_id', '[message_id]', 'messages', '[id]'),
    ('audit_log', 'fk_audit_log_shipment_id', '[shipment_id]', 'shipments', '[id]'),
    ('change_candidates', 'fk_change_candidates_change_id', '[change_id]', 'proposed_changes', '[id]'),
    ('write_attempts', 'fk_write_attempts_change_id', '[change_id]', 'proposed_changes', '[id]'),
)

#: (table, name, sqltext)
CHECKS = (
    ('attachments', 'ck_attachments_doc_type', "doc_type IN ('PACKING_LIST','COMMERCIAL_INVOICE','SHIPPING_ADVICE','SHIPPING_SCHEDULE','PAYMENT_REQUEST','INSPECTION_REPORT','OTHER','UNREADABLE','UNCLASSIFIED')"),
    ('change_state_transitions', 'ck_change_state_transitions_actor_kind', "actor_kind IN ('HUMAN','SYSTEM')"),
    ('shipments', 'ck_shipments_origin', "origin IN ('VENDOR_EMAIL','PAULA_DIRECTED')"),
    ('shipments', 'ck_shipments_provenance', "(origin = 'VENDOR_EMAIL' AND message_id IS NOT NULL) OR (origin = 'PAULA_DIRECTED' AND message_id IS NULL  AND primary_attachment_sha IS NULL)"),
    ('shipment_pos', 'ck_shipment_pos_resolution_status', "resolution_status IN ('UNRESOLVED','RESOLVED','NOT_FOUND','AMBIGUOUS')"),
    ('shipment_sources', 'ck_shipment_sources_role', "role IN ('PRIMARY','CROSS_CHECK','EXCLUDED')"),
    ('proposed_changes', 'ck_proposed_changes_composition_needs_both_axes', 'size_composition_method IS NULL OR (src_size_axis_primary IS NOT NULL AND src_size_axis_secondary IS NOT NULL)'),
    ('proposed_changes', 'ck_proposed_changes_human_verdict', "human_verdict IS NULL OR human_verdict IN ('ACCEPTED','CORRECTED','REJECTED','CANDIDATE_PICKED')"),
    ('proposed_changes', 'ck_proposed_changes_date_scope_needs_date', "date_write_status = 'NONE' OR confirmed_receipt_date IS NOT NULL"),
    ('proposed_changes', 'ck_proposed_changes_quantity_scope_needs_quantity', "quantity_write_status = 'NONE' OR approved_quantity IS NOT NULL"),
    ('proposed_changes', 'ck_proposed_changes_accumulation_basis', "accumulation_basis IS NULL OR accumulation_basis IN ('FIRST_SHIPMENT','ACCUMULATED','PRE_EXISTING_RECEIPT','DISPUTED')"),
    ('proposed_changes', 'ck_proposed_changes_accumulation_needs_base', "accumulation_basis <> 'ACCUMULATED' OR accumulation_base_quantity IS NOT NULL"),
    ('proposed_changes', 'ck_proposed_changes_target_required', "state NOT IN ('APPROVED','WRITTEN','WRITE_FAILED') OR ns_line_id IS NOT NULL"),
    ('proposed_changes', 'ck_proposed_changes_colour_resolution_method', "colour_resolution_method IS NULL OR colour_resolution_method IN ('CODE','NAME','AMBIGUOUS','UNRESOLVED')"),
    ('proposed_changes', 'ck_proposed_changes_extraction_confidence', "extraction_confidence IN ('high','medium','low')"),
    ('proposed_changes', 'ck_proposed_changes_name_resolution_needs_provenance', "colour_resolution_method <> 'NAME' OR (colour_resolved_code IS NOT NULL AND colour_resolved_name IS NOT NULL  AND colour_name_source_item_id IS NOT NULL)"),
    ('proposed_changes', 'ck_proposed_changes_date_write_status', "date_write_status IN ('NONE','APPROVED','WRITTEN','FAILED')"),
    ('proposed_changes', 'ck_proposed_changes_size_composition_method', "size_composition_method IS NULL OR size_composition_method IN ('COMPOSED','COMPOSITION_REJECTED')"),
    ('proposed_changes', 'ck_proposed_changes_quantity_write_status', "quantity_write_status IN ('NONE','APPROVED','WRITTEN','FAILED')"),
    ('proposed_changes', 'ck_proposed_changes_date_needs_human', 'confirmed_receipt_date IS NULL OR date_approved_by IS NOT NULL'),
    ('audit_log', 'ck_audit_log_workflow', "workflow IN ('PACKING_SLIP','PAULA_DIRECTED')"),
    ('audit_log', 'ck_audit_log_actor_kind', "actor_kind IN ('HUMAN','SYSTEM')"),
    ('write_attempts', 'ck_write_attempts_outcome', "outcome IN ('SUCCESS','FAILED')"),
    ('write_attempts', 'ck_write_attempts_error_kind', "error_kind IS NULL OR error_kind IN ('PERMISSION','TRANSIENT','CONFLICT','LINE_CLOSED','VALIDATION','OTHER')"),
    ('write_attempts', 'ck_write_attempts_scope', "scope IN ('QUANTITY','DATE')"),
)

#: (table, name, columns, unique, predicate-or-None). The predicate is the whole
#: point -- see the module docstring.
INDEXES = (
    ('shipments', 'ux_shipments_primary_attachment', '[primary_attachment_sha]', True, 'primary_attachment_sha IS NOT NULL AND superseded_by_shipment_id IS NULL'),
    ('shipments', 'ix_shipments_source_set_hash', '[source_set_hash]', False, None),
    ('proposed_changes', 'ux_proposed_changes_one_line_per_shipment', '[shipment_id], [ns_line_id]', True, 'ns_line_id IS NOT NULL'),
    ('proposed_changes', 'ix_proposed_changes_shipment_id', '[shipment_id]', False, None),
    ('proposed_changes', 'ux_proposed_changes_canonical_key', '[shipment_po_id], [key_style], [key_color], [key_size], [key_recap_label]', True, "key_size <> ''"),
    ('proposed_changes', 'ix_proposed_changes_state', '[state]', False, None),
    ('audit_log', 'ix_audit_log_change_id_occurred_at', '[change_id], [occurred_at]', False, None),
    ('change_candidates', 'ux_change_candidates_one_selected', '[change_id]', True, 'selected = 1'),
)

#: The one column carrying a server default, whose constraint name SQL Server
#: generated and nobody can predict.
DEFAULT_COLUMN = ('proposed_changes', 'key_recap_label', "''")

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


def _drop_views(bind):
    for view in ("v_review_lines", "v_calibration"):
        bind.execute(sa.text(f"DROP VIEW IF EXISTS {view}"))


def _create_views(bind):
    bind.execute(sa.text(VIEW_REVIEW_LINES))
    bind.execute(sa.text(VIEW_CALIBRATION))


def _drop_unnamed_default(bind, table, column):
    """
    Drop the auto-named DEFAULT constraint blocking ALTER COLUMN.

    Looked up rather than named: SQL Server invented the name when 0004 added
    `server_default=""`, and it differs per database.
    """
    bind.execute(sa.text(f"""
        DECLARE @n sysname;
        SELECT @n = dc.name
          FROM sys.default_constraints dc
          JOIN sys.columns c
            ON c.object_id = dc.parent_object_id
           AND c.column_id = dc.parent_column_id
         WHERE dc.parent_object_id = OBJECT_ID('{table}')
           AND c.name = '{column}';
        IF @n IS NOT NULL
            EXEC('ALTER TABLE [{table}] DROP CONSTRAINT [' + @n + ']');
    """))


def _convert(bind, to_nvarchar: bool) -> None:
    """Tear every dependent object down, retype 115 columns, put it all back."""
    _drop_views(bind)

    for table, name, *_ in FOREIGN_KEYS:
        bind.execute(sa.text(f"ALTER TABLE [{table}] DROP CONSTRAINT [{name}]"))
    for table, name, *_ in INDEXES:
        bind.execute(sa.text(f"DROP INDEX [{name}] ON [{table}]"))
    for table, name, _cols in UNIQUES:
        bind.execute(sa.text(f"ALTER TABLE [{table}] DROP CONSTRAINT [{name}]"))
    for table, name, _txt in CHECKS:
        bind.execute(sa.text(f"ALTER TABLE [{table}] DROP CONSTRAINT [{name}]"))
    for table, name, _cols in PRIMARY_KEYS:
        bind.execute(sa.text(f"ALTER TABLE [{table}] DROP CONSTRAINT [{name}]"))
    _drop_unnamed_default(bind, DEFAULT_COLUMN[0], DEFAULT_COLUMN[1])

    for table, column, nvarchar, varchar, nullable in COLUMNS:
        type_sql = nvarchar if to_nvarchar else varchar
        null_sql = "NULL" if nullable else "NOT NULL"
        bind.execute(sa.text(
            f"ALTER TABLE [{table}] ALTER COLUMN [{column}] {type_sql} {null_sql}"))

    tbl, col, default = DEFAULT_COLUMN
    bind.execute(sa.text(f"ALTER TABLE [{tbl}] ADD DEFAULT {default} FOR [{col}]"))
    for table, name, cols in PRIMARY_KEYS:
        bind.execute(sa.text(
            f"ALTER TABLE [{table}] ADD CONSTRAINT [{name}] PRIMARY KEY ({cols})"))
    for table, name, cols in UNIQUES:
        bind.execute(sa.text(
            f"ALTER TABLE [{table}] ADD CONSTRAINT [{name}] UNIQUE ({cols})"))
    for table, name, cols, unique, predicate in INDEXES:
        uniq = "UNIQUE " if unique else ""
        where = f" WHERE {predicate}" if predicate else ""
        bind.execute(sa.text(
            f"CREATE {uniq}INDEX [{name}] ON [{table}] ({cols}){where}"))
    for table, name, sqltext in CHECKS:
        bind.execute(sa.text(
            f"ALTER TABLE [{table}] ADD CONSTRAINT [{name}] CHECK ({sqltext})"))
    for table, name, local, ref_table, ref_cols in FOREIGN_KEYS:
        bind.execute(sa.text(
            f"ALTER TABLE [{table}] ADD CONSTRAINT [{name}] FOREIGN KEY ({local}) "
            f"REFERENCES [{ref_table}] ({ref_cols})"))

    _create_views(bind)


def _refuse_if_lossy(bind) -> None:
    """
    Refuse a downgrade that would destroy characters.

    `col <> CAST(col AS VARCHAR(...))` is true exactly when the value contains
    something the code page cannot represent, because the cast replaces it with
    `?`. Empty tables pass, which is what the round-trip test exercises.
    """
    damaged = []
    for table, column, _nv, varchar, _nullable in COLUMNS:
        found = bind.execute(sa.text(
            f"SELECT TOP 1 1 FROM [{table}] WHERE [{column}] IS NOT NULL "
            f"AND [{column}] <> CAST([{column}] AS {varchar})"
        )).fetchone()
        if found:
            damaged.append(f"{table}.{column}")
    if damaged:
        raise RuntimeError(
            "Refusing to downgrade 0009: these columns hold characters that "
            "VARCHAR cannot represent, and converting them back would replace each "
            f"one with '?' irreversibly -- {', '.join(damaged[:10])}"
            + (f" and {len(damaged) - 10} more" if len(damaged) > 10 else "")
            + ". Export the affected rows first if this is really intended."
        )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # Nothing to do, and that is not a shortcut: SQLite renders VARCHAR(n)
        # and NVARCHAR(n) identically, so there is no difference to apply. Doing
        # the work anyway would rebuild 13 tables to produce a byte-identical
        # schema, and would drop the CHECK constraints in the process, because
        # batch mode cannot reflect them on SQLite.
        return
    _convert(bind, to_nvarchar=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    _refuse_if_lossy(bind)
    _convert(bind, to_nvarchar=False)
