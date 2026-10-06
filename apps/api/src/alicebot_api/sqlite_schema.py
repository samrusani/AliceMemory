"""SQLite DDL bootstrap for the zero-infrastructure vNext on-ramp.

Mirrors the Postgres vNext memory-kernel schema for exactly the store
surface the core MCP tools use. Allowed enum values mirror
``apps/api/alembic/versions/20260510_0067_vnext_memory_kernel_schema.py``
(plus ``procedure`` from ``20260621_0071``) and
``alicebot_api.vnext_memory_commit``.

Conventions:
- UUIDs are stored as TEXT (``str(uuid4())``).
- Timestamps are ISO-8601 TEXT in UTC with a trailing ``Z``.
- JSON columns are TEXT holding ``json.dumps(json_safe(value))``.
- ``event_log`` (and ``memory_revisions``, mirroring Postgres) are
  append-only, enforced by triggers. Mirroring Postgres migration
  ``20260706_0079``, the UPDATE triggers admit exactly one privileged
  exception -- true redaction: SQLite has no session variables, so a
  one-row ``redaction_mode`` flag table stands in for Postgres's
  ``current_setting('app.redaction_in_progress', true)``. The trigger's
  WHEN clause only lets an UPDATE through while ``redaction_mode.enabled``
  is 1 AND every skeleton column (ids, timestamps, types, actor columns)
  is unchanged AND the content columns hold nothing but the literal
  redaction marker ``'[REDACTED]'`` (JSON content columns must carry the
  ``{"redacted": true}`` shape). ``SQLiteVNextStore`` flips the flag
  around its redaction statements and resets it even on error paths;
  bootstrap also defensively resets it to 0. DELETEs are always
  rejected.
- ``memories_fts`` is an external-content FTS5 index over
  memories(title, canonical_text, summary, memory_key, fact_keys), kept
  in sync by AFTER INSERT/UPDATE/DELETE triggers. It uses the
  ``porter unicode61`` tokenizer so inflected query terms match the way
  stemmed Postgres FTS (``websearch_to_tsquery('english', ...)``) does.
  ``fact_keys`` holds the derived retrieval keys from
  ``alicebot_api.vnext_fact_keys`` (mirroring the Postgres ``'D'``
  ``search_tsv`` weight from migration ``20260707_0082``).
- ``source_chunks_fts`` is the same construction over
  source_chunks(text) (mirroring the Postgres ``search_tsv`` column from
  migration ``20260707_0081``), so source retrieval can match captured
  CONTENT instead of only title/uri/metadata. External-content FTS
  tables created against a pre-existing database file start empty (the
  sync triggers only see later writes), so bootstrap issues a one-shot
  ``'rebuild'`` for any FTS table it just created. A pre-existing FTS
  table whose column set predates the current DDL (``CREATE VIRTUAL
  TABLE IF NOT EXISTS`` never alters one) is dropped together with its
  sync triggers and recreated the same way.
"""

from __future__ import annotations

from hashlib import md5
import json
import sqlite3
from uuid import uuid4

from alicebot_api.vnext_project_scope import (
    normalize_project_scope,
    project_scope_identity,
    resolve_source_metadata_project_scope,
)

DOMAINS = (
    "professional",
    "personal",
    "family",
    "health",
    "spiritual",
    "financial",
    "legal",
    "learning",
    "relationship",
    "project",
    "agent_run",
    "system",
    "unknown",
)

SENSITIVITY_LEVELS = (
    "public",
    "internal",
    "private",
    "confidential",
    "highly_sensitive",
    "sacred",
    "regulated",
    "unknown",
)

MEMORY_TYPES = (
    "preference",
    "identity_fact",
    "relationship_fact",
    "project_fact",
    "decision",
    "commitment",
    "routine",
    "procedure",
    "constraint",
    "working_style",
    "episode",
    "semantic",
    "project_state",
    "belief",
    "thesis",
    "person",
    "relationship",
    "open_loop",
    "value",
    "pattern",
    "contradiction",
    "question",
    "answer",
    "artifact_summary",
    "agent_run",
    "system",
)

MEMORY_STATUSES = (
    "candidate",
    "active",
    "accepted",
    "rejected",
    "superseded",
    # Demoted-but-kept memories (e.g. long-unconfirmed facts). Excluded
    # from retrieval by default alongside superseded/rejected.
    "stale",
    "archived",
    "needs_review",
    "private_only",
)

MEMORY_CONFIRMATION_STATUSES = (
    "unconfirmed",
    "confirmed",
    "contested",
)

MEMORY_TRUST_CLASSES = (
    "deterministic",
    "llm_single_source",
    "llm_corroborated",
    "human_curated",
)

MEMORY_PROMOTION_ELIGIBILITIES = (
    "promotable",
    "not_promotable",
)

REVISION_TYPES = (
    "created",
    "edited",
    "corrected",
    "promoted",
    "rejected",
    "superseded",
    "merged",
    "split",
    "archived",
    "restored",
)

EVIDENCE_ROLES = (
    "supports",
    "contradicts",
    "mentions",
    "inferred_from",
    "quoted_from",
    "summarizes",
    "background",
)

OPEN_LOOP_STATUSES = (
    "open",
    "resolved",
    "dismissed",
)

OPEN_LOOP_PRIORITIES = ("low", "normal", "high", "urgent")

EDGE_TYPES = (
    "supports",
    "contradicts",
    "caused_by",
    "influenced_by",
    "similar_to",
    "supersedes",
    "depends_on",
    "mentions",
    "asks",
    "answers",
    "reframes",
    "predicts",
    "invalidates",
    "reopens",
    "same_problem",
    "same_principle",
    "cross_domain_pattern",
    "old_idea_now_relevant",
    "belief_reinforcement",
    "belief_challenge",
    "owned_by",
    "belongs_to_project",
    "related_to_person",
)

AGENT_TYPES = (
    "personal_assistant",
    "coding_agent",
    "research_agent",
    "workflow_agent",
    "unknown",
)

# Mirrors ENTITY_TYPES in alicebot_api.vnext_entity_names and the CHECK
# constraint in Postgres migration 20260705_0078.
ENTITY_TYPES = (
    "person",
    "organization",
    "project",
    "topic",
    "technology",
    "market",
    "report",
    "agent",
    "other",
)

PERMISSION_PROFILES = (
    "read_only_agent",
    "project_scoped_agent",
    "trusted_local_agent",
    "memory_proposal_agent",
    "admin_agent",
)


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


_DOMAINS_SQL = _sql_list(DOMAINS)
_SENSITIVITY_SQL = _sql_list(SENSITIVITY_LEVELS)
_MEMORY_TYPES_SQL = _sql_list(MEMORY_TYPES)
_MEMORY_STATUSES_SQL = _sql_list(MEMORY_STATUSES)
_MEMORY_CONFIRMATION_STATUSES_SQL = _sql_list(MEMORY_CONFIRMATION_STATUSES)
_MEMORY_TRUST_CLASSES_SQL = _sql_list(MEMORY_TRUST_CLASSES)
_MEMORY_PROMOTION_ELIGIBILITIES_SQL = _sql_list(MEMORY_PROMOTION_ELIGIBILITIES)
_REVISION_TYPES_SQL = _sql_list(REVISION_TYPES)
_EVIDENCE_ROLES_SQL = _sql_list(EVIDENCE_ROLES)
_OPEN_LOOP_STATUSES_SQL = _sql_list(OPEN_LOOP_STATUSES)
_OPEN_LOOP_PRIORITIES_SQL = _sql_list(OPEN_LOOP_PRIORITIES)
_EDGE_TYPES_SQL = _sql_list(EDGE_TYPES)
_AGENT_TYPES_SQL = _sql_list(AGENT_TYPES)
_PERMISSION_PROFILES_SQL = _sql_list(PERMISSION_PROFILES)
_ENTITY_TYPES_SQL = _sql_list(ENTITY_TYPES)
_PROJECT_UPDATE_EVENT_TYPES = (
    "project.update_candidate_created",
    "project.update_candidate_accepted",
    "project.update_candidate_rejected",
)
_PROJECT_UPDATE_EVENT_TYPES_SQL = _sql_list(_PROJECT_UPDATE_EVENT_TYPES)

# Default matches the store's Python-generated ISO-8601 UTC "Z" convention
# closely enough for lexicographic ordering (milliseconds vs microseconds).
_NOW_UTC_ISO_SQL = "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"

# Canonical true-redaction marker. Mirrors REDACTION_MARKER in
# alicebot_api.vnext_store and Postgres migration 20260706_0079.
REDACTION_MARKER = "[REDACTED]"

_TABLE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS alice_schema_state (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS users (
      id TEXT PRIMARY KEY,
      email TEXT NOT NULL UNIQUE,
      display_name TEXT NULL,
      created_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL}
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sources (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      source_type TEXT NOT NULL,
      title TEXT NULL,
      author TEXT NULL,
      uri TEXT NULL,
      raw_path TEXT NULL,
      content_hash TEXT NOT NULL,
      dedupe_key TEXT NULL,
      captured_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      source_created_at TEXT NULL,
      source_modified_at TEXT NULL,
      connector_name TEXT NULL,
      external_id TEXT NULL,
      domain TEXT NOT NULL DEFAULT 'unknown',
      sensitivity TEXT NOT NULL DEFAULT 'unknown',
      metadata_json TEXT NOT NULL DEFAULT '{{}}',
      deleted_at TEXT NULL,
      UNIQUE (id, user_id),
      CONSTRAINT sources_source_type_length_check
        CHECK (length(source_type) BETWEEN 1 AND 120),
      CONSTRAINT sources_content_hash_length_check
        CHECK (length(content_hash) BETWEEN 1 AND 200),
      CONSTRAINT sources_dedupe_key_length_check
        CHECK (dedupe_key IS NULL OR length(dedupe_key) BETWEEN 1 AND 200),
      CONSTRAINT sources_domain_check
        CHECK (domain IN ({_DOMAINS_SQL})),
      CONSTRAINT sources_sensitivity_check
        CHECK (sensitivity IN ({_SENSITIVITY_SQL})),
      CONSTRAINT sources_metadata_json_object_check
        CHECK (json_type(metadata_json) = 'object')
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS source_chunks (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      source_id TEXT NOT NULL,
      chunk_index INTEGER NOT NULL,
      text TEXT NOT NULL,
      token_count INTEGER NULL,
      metadata_json TEXT NOT NULL DEFAULT '{{}}',
      created_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      UNIQUE (id, user_id),
      UNIQUE (user_id, source_id, chunk_index),
      CONSTRAINT source_chunks_source_fkey
        FOREIGN KEY (source_id, user_id)
        REFERENCES sources(id, user_id)
        ON DELETE CASCADE,
      CONSTRAINT source_chunks_chunk_index_check
        CHECK (chunk_index >= 0),
      CONSTRAINT source_chunks_token_count_check
        CHECK (token_count IS NULL OR token_count >= 0),
      CONSTRAINT source_chunks_metadata_json_object_check
        CHECK (json_type(metadata_json) = 'object')
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS memories (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      agent_profile_id TEXT NOT NULL DEFAULT 'assistant_default',
      memory_key TEXT NOT NULL,
      value TEXT NOT NULL,
      status TEXT NOT NULL,
      source_event_ids TEXT NOT NULL,
      memory_type TEXT NOT NULL DEFAULT 'preference',
      confidence REAL NULL,
      salience REAL NULL,
      confirmation_status TEXT NOT NULL DEFAULT 'unconfirmed',
      trust_class TEXT NOT NULL DEFAULT 'deterministic',
      promotion_eligibility TEXT NOT NULL DEFAULT 'promotable',
      evidence_count INTEGER NULL,
      independent_source_count INTEGER NULL,
      extracted_by_model TEXT NULL,
      trust_reason TEXT NULL,
      valid_from TEXT NULL,
      valid_to TEXT NULL,
      last_confirmed_at TEXT NULL,
      title TEXT NULL,
      canonical_text TEXT NOT NULL DEFAULT '',
      summary TEXT NULL,
      domain TEXT NOT NULL DEFAULT 'unknown',
      sensitivity TEXT NOT NULL DEFAULT 'unknown',
      first_seen_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      last_seen_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      last_reviewed_at TEXT NULL,
      metadata_json TEXT NOT NULL DEFAULT '{{}}',
      commit_digest TEXT NULL,
      confirmation_id TEXT NULL,
      project_id TEXT NULL,
      created_by_agent_id TEXT NULL,
      run_id TEXT NULL,
      superseded_by TEXT NULL,
      supersedes TEXT NULL,
      embedding BLOB NULL,
      fact_keys TEXT NULL,
      created_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      updated_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      deleted_at TEXT NULL,
      UNIQUE (id, user_id),
      CONSTRAINT memories_user_profile_memory_key_key
        UNIQUE (user_id, agent_profile_id, memory_key),
      CONSTRAINT memories_memory_type_check
        CHECK (memory_type IN ({_MEMORY_TYPES_SQL})),
      CONSTRAINT memories_status_check
        CHECK (status IN ({_MEMORY_STATUSES_SQL})),
      CONSTRAINT memories_confirmation_status_check
        CHECK (confirmation_status IN ({_MEMORY_CONFIRMATION_STATUSES_SQL})),
      CONSTRAINT memories_trust_class_check
        CHECK (trust_class IN ({_MEMORY_TRUST_CLASSES_SQL})),
      CONSTRAINT memories_promotion_eligibility_check
        CHECK (promotion_eligibility IN ({_MEMORY_PROMOTION_ELIGIBILITIES_SQL})),
      CONSTRAINT memories_domain_check
        CHECK (domain IN ({_DOMAINS_SQL})),
      CONSTRAINT memories_sensitivity_check
        CHECK (sensitivity IN ({_SENSITIVITY_SQL})),
      CONSTRAINT memories_confidence_range_check
        CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
      CONSTRAINT memories_salience_range_check
        CHECK (salience IS NULL OR (salience >= 0.0 AND salience <= 1.0)),
      CONSTRAINT memories_evidence_count_non_negative_check
        CHECK (evidence_count IS NULL OR evidence_count >= 0),
      CONSTRAINT memories_independent_source_count_non_negative_check
        CHECK (independent_source_count IS NULL OR independent_source_count >= 0),
      CONSTRAINT memories_valid_range_check
        CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_to >= valid_from),
      CONSTRAINT memories_seen_range_check
        CHECK (last_seen_at >= first_seen_at),
      CONSTRAINT memories_value_json_check
        CHECK (json_valid(value)),
      CONSTRAINT memories_source_event_ids_array_check
        CHECK (json_type(source_event_ids) = 'array'),
      CONSTRAINT memories_metadata_json_object_check
        CHECK (json_type(metadata_json) = 'object')
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS memory_revisions (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      memory_id TEXT NOT NULL,
      sequence_no INTEGER NOT NULL,
      action TEXT NOT NULL,
      memory_key TEXT NOT NULL,
      previous_value TEXT NULL,
      new_value TEXT NULL,
      source_event_ids TEXT NOT NULL,
      candidate TEXT NOT NULL,
      revision_number INTEGER NOT NULL,
      revision_type TEXT NOT NULL,
      text_before TEXT NULL,
      text_after TEXT NOT NULL,
      reason TEXT NULL,
      actor_type TEXT NOT NULL DEFAULT 'system',
      actor_id TEXT NULL,
      metadata_json TEXT NOT NULL DEFAULT '{{}}',
      created_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      UNIQUE (id, user_id),
      UNIQUE (memory_id, sequence_no),
      CONSTRAINT memory_revisions_memory_fkey
        FOREIGN KEY (memory_id, user_id)
        REFERENCES memories(id, user_id)
        ON DELETE CASCADE,
      CONSTRAINT memory_revisions_revision_type_check
        CHECK (revision_type IN ({_REVISION_TYPES_SQL})),
      CONSTRAINT memory_revisions_revision_number_positive_check
        CHECK (revision_number >= 1),
      CONSTRAINT memory_revisions_metadata_json_object_check
        CHECK (json_type(metadata_json) = 'object')
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS provenance_links (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      target_type TEXT NOT NULL,
      target_id TEXT NOT NULL,
      source_id TEXT NULL,
      source_chunk_id TEXT NULL,
      quote TEXT NULL,
      evidence_role TEXT NOT NULL,
      confidence REAL NOT NULL DEFAULT 0.5,
      created_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      UNIQUE (id, user_id),
      CONSTRAINT provenance_links_source_fkey
        FOREIGN KEY (source_id, user_id)
        REFERENCES sources(id, user_id)
        ON DELETE SET NULL,
      CONSTRAINT provenance_links_source_chunk_fkey
        FOREIGN KEY (source_chunk_id, user_id)
        REFERENCES source_chunks(id, user_id)
        ON DELETE SET NULL,
      CONSTRAINT provenance_links_evidence_role_check
        CHECK (evidence_role IN ({_EVIDENCE_ROLES_SQL})),
      CONSTRAINT provenance_links_confidence_range_check
        CHECK (confidence >= 0.0 AND confidence <= 1.0)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS graph_edges (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      from_type TEXT NOT NULL,
      from_id TEXT NOT NULL,
      to_type TEXT NOT NULL,
      to_id TEXT NOT NULL,
      edge_type TEXT NOT NULL,
      confidence REAL NOT NULL DEFAULT 0.5,
      explanation TEXT NULL,
      created_by TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      observed_at TEXT NULL,
      valid_from TEXT NULL,
      valid_to TEXT NULL,
      metadata_json TEXT NOT NULL DEFAULT '{{}}',
      UNIQUE (id, user_id),
      CONSTRAINT graph_edges_edge_type_check
        CHECK (edge_type IN ({_EDGE_TYPES_SQL})),
      CONSTRAINT graph_edges_confidence_range_check
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
      CONSTRAINT graph_edges_valid_range_check
        CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_to >= valid_from),
      CONSTRAINT graph_edges_metadata_json_object_check
        CHECK (json_type(metadata_json) = 'object')
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS open_loops (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      memory_id TEXT NULL,
      title TEXT NOT NULL,
      status TEXT NOT NULL,
      opened_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      due_at TEXT NULL,
      resolved_at TEXT NULL,
      resolution_note TEXT NULL,
      created_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      updated_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      description TEXT NULL,
      priority TEXT NOT NULL DEFAULT 'normal',
      project_id TEXT NULL,
      person_id TEXT NULL,
      source_id TEXT NULL,
      closed_at TEXT NULL,
      domain TEXT NOT NULL DEFAULT 'unknown',
      sensitivity TEXT NOT NULL DEFAULT 'unknown',
      metadata_json TEXT NOT NULL DEFAULT '{{}}',
      UNIQUE (id, user_id),
      CONSTRAINT open_loops_memory_fkey
        FOREIGN KEY (memory_id, user_id)
        REFERENCES memories(id, user_id)
        ON DELETE SET NULL,
      CONSTRAINT open_loops_source_fkey
        FOREIGN KEY (source_id, user_id)
        REFERENCES sources(id, user_id)
        ON DELETE SET NULL,
      CONSTRAINT open_loops_status_check
        CHECK (status IN ({_OPEN_LOOP_STATUSES_SQL})),
      CONSTRAINT open_loops_priority_check
        CHECK (priority IN ({_OPEN_LOOP_PRIORITIES_SQL})),
      CONSTRAINT open_loops_title_length_check
        CHECK (length(title) <= 280),
      CONSTRAINT open_loops_resolution_note_length_check
        CHECK (resolution_note IS NULL OR length(resolution_note) <= 2000),
      CONSTRAINT open_loops_resolved_state_check
        CHECK (
          (status = 'open' AND resolved_at IS NULL AND resolution_note IS NULL)
          OR (status IN ('resolved', 'dismissed') AND resolved_at IS NOT NULL)
        ),
      CONSTRAINT open_loops_domain_check
        CHECK (domain IN ({_DOMAINS_SQL})),
      CONSTRAINT open_loops_sensitivity_check
        CHECK (sensitivity IN ({_SENSITIVITY_SQL})),
      CONSTRAINT open_loops_metadata_json_object_check
        CHECK (json_type(metadata_json) = 'object')
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS event_log (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      event_type TEXT NOT NULL,
      actor_type TEXT NOT NULL,
      actor_id TEXT NULL,
      target_type TEXT NULL,
      target_id TEXT NULL,
      occurred_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      payload_json TEXT NOT NULL DEFAULT '{{}}',
      trace_id TEXT NULL,
      run_id TEXT NULL,
      integrity_hash TEXT NULL,
      UNIQUE (id, user_id),
      CONSTRAINT event_log_event_type_length_check
        CHECK (length(event_type) BETWEEN 1 AND 160),
      CONSTRAINT event_log_actor_type_length_check
        CHECK (length(actor_type) BETWEEN 1 AND 80),
      CONSTRAINT event_log_payload_json_object_check
        CHECK (json_type(payload_json) = 'object')
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS agent_identities (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      agent_id TEXT NOT NULL,
      agent_type TEXT NOT NULL DEFAULT 'unknown',
      permission_profile TEXT NOT NULL DEFAULT 'read_only_agent',
      display_name TEXT NULL,
      project_scope_json TEXT NOT NULL DEFAULT '[]',
      metadata_json TEXT NOT NULL DEFAULT '{{}}',
      created_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      updated_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      UNIQUE (id, user_id),
      UNIQUE (user_id, agent_id),
      CONSTRAINT agent_identities_agent_type_check
        CHECK (agent_type IN ({_AGENT_TYPES_SQL})),
      CONSTRAINT agent_identities_permission_profile_check
        CHECK (permission_profile IN ({_PERMISSION_PROFILES_SQL})),
      CONSTRAINT agent_identities_project_scope_json_array_check
        CHECK (json_type(project_scope_json) = 'array'),
      CONSTRAINT agent_idvnext_entities_metadata_json_object_check
        CHECK (json_type(metadata_json) = 'object')
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS agent_api_keys (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      agent_id TEXT NOT NULL,
      permission_profile TEXT NOT NULL,
      project_scope TEXT NULL,
      key_hash TEXT NOT NULL UNIQUE,
      key_prefix TEXT NOT NULL,
      label TEXT NULL,
      created_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      revoked_at TEXT NULL,
      last_used_at TEXT NULL,
      UNIQUE (id, user_id),
      CONSTRAINT agent_api_keys_permission_profile_check
        CHECK (permission_profile IN ({_PERMISSION_PROFILES_SQL}))
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS browser_clip_capabilities (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      capability_hash TEXT NOT NULL UNIQUE,
      origin TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      consumed_at TEXT NULL,
      created_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      UNIQUE (id, user_id),
      CONSTRAINT browser_clip_capabilities_hash_check
        CHECK (
          length(capability_hash) = 64
          AND capability_hash = lower(capability_hash)
          AND capability_hash NOT GLOB '*[^0-9a-f]*'
        ),
      CONSTRAINT browser_clip_capabilities_origin_length_check
        CHECK (length(origin) BETWEEN 8 AND 2048),
      CONSTRAINT browser_clip_capabilities_expiry_range_check
        CHECK (
          julianday(expires_at) IS NOT NULL
          AND julianday(created_at) IS NOT NULL
          AND julianday(expires_at) > julianday(created_at)
          -- One millisecond absorbs SQLite julianday floating-point error at
          -- the exact 300-second boundary; the write seam still rejects >300.
          AND julianday(expires_at) <= julianday(created_at) + (300.001 / 86400.0)
        ),
      CONSTRAINT browser_clip_capabilities_consumed_range_check
        CHECK (
          consumed_at IS NULL
          OR (
            julianday(consumed_at) IS NOT NULL
            AND julianday(consumed_at) >= julianday(created_at)
          )
        )
    )
    """,
    # Entity substrate (mirrors Postgres migration 20260705_0078):
    # entities.normalized_name is the resolution key, aliases is a JSON
    # array of alternate normalized names.
    f"""
    CREATE TABLE IF NOT EXISTS vnext_entities (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      entity_type TEXT NOT NULL,
      name TEXT NOT NULL,
      normalized_name TEXT NOT NULL,
      aliases TEXT NOT NULL DEFAULT '[]',
      metadata_json TEXT NOT NULL DEFAULT '{{}}',
      created_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      updated_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      deleted_at TEXT NULL,
      first_observed_at TEXT NULL,
      last_observed_at TEXT NULL,
      mention_count INTEGER NOT NULL DEFAULT 0,
      UNIQUE (id, user_id),
      CONSTRAINT vnext_entities_user_type_normalized_name_key
        UNIQUE (user_id, entity_type, normalized_name),
      CONSTRAINT vnext_entities_entity_type_check
        CHECK (entity_type IN ({_ENTITY_TYPES_SQL})),
      CONSTRAINT vnext_entities_name_length_check
        CHECK (length(name) BETWEEN 1 AND 500),
      CONSTRAINT vnext_entities_normalized_name_length_check
        CHECK (length(normalized_name) BETWEEN 1 AND 500),
      CONSTRAINT vnext_entities_aliases_array_check
        CHECK (json_type(aliases) = 'array'),
      CONSTRAINT vnext_entities_metadata_json_object_check
        CHECK (json_type(metadata_json) = 'object'),
      CONSTRAINT vnext_entities_mention_count_non_negative_check
        CHECK (mention_count >= 0),
      CONSTRAINT vnext_entities_observed_range_check
        CHECK (
          first_observed_at IS NULL
          OR last_observed_at IS NULL
          OR last_observed_at >= first_observed_at
        )
    )
    """,
    # One-row flag table standing in for Postgres's
    # app.redaction_in_progress session setting (SQLite triggers cannot
    # read connection-local state). The append-only UPDATE triggers below
    # consult it in their WHEN clause; SQLiteVNextStore sets enabled=1
    # only for the duration of its redaction statements and resets it on
    # every exit path, and bootstrap_sqlite_schema resets it defensively.
    """
    CREATE TABLE IF NOT EXISTS redaction_mode (
      id INTEGER PRIMARY KEY CHECK (id = 1),
      enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1))
    )
    """,
    # One-row invalidation stamp for the process-local resident vector cache
    # (vnext_stores/sqlite/vector_scan.py). The token is REWRITTEN to a fresh
    # random value -- never incremented -- whenever a non-NULL memory embedding
    # is overwritten or cleared, so a restored database snapshot can never
    # alias a token a live cache entry was built at. Seeded by
    # bootstrap_sqlite_schema with a random token (INSERT OR IGNORE keeps the
    # existing token on re-bootstrap: an unchanged token must keep meaning
    # "vectors unchanged").
    """
    CREATE TABLE IF NOT EXISTS embedding_stamp (
      id INTEGER PRIMARY KEY CHECK (id = 1),
      token TEXT NOT NULL
    )
    """,
    # Append-only relationship history (append-only enforced by triggers
    # below, mirroring event_log). source_id carries NO foreign key on
    # purpose: an FK with ON DELETE SET NULL would have to UPDATE this
    # append-only table when a source is deleted, which the trigger
    # rejects.
    f"""
    CREATE TABLE IF NOT EXISTS entity_relationship_events (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      entity_id TEXT NOT NULL,
      relationship_type_before TEXT NULL,
      relationship_type_after TEXT NOT NULL,
      changed_at TEXT NOT NULL DEFAULT {_NOW_UTC_ISO_SQL},
      source_id TEXT NULL,
      metadata_json TEXT NOT NULL DEFAULT '{{}}',
      UNIQUE (id, user_id),
      CONSTRAINT entity_relationship_events_entity_fkey
        FOREIGN KEY (entity_id, user_id)
        REFERENCES vnext_entities(id, user_id)
        ON DELETE CASCADE,
      CONSTRAINT entity_relationship_events_after_length_check
        CHECK (length(relationship_type_after) BETWEEN 1 AND 120),
      CONSTRAINT entity_relationship_events_metadata_json_object_check
        CHECK (json_type(metadata_json) = 'object')
    )
    """,
)

# Columns added to existing tables after their CREATE TABLE first shipped
# (mirrors Postgres migrations 20260704_0076 and 20260704_0077). The
# bootstrap's idempotent CREATE TABLE IF NOT EXISTS never alters an existing
# table, so pre-existing database files get these via a PRAGMA
# table_info-guarded ALTER TABLE in bootstrap_sqlite_schema. Runs before the
# index statements because memories_user_project_idx references
# memories.project_id (and memories_user_superseded_by_idx references
# memories.superseded_by).
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("sources", "dedupe_key", "TEXT NULL"),
    ("memories", "project_id", "TEXT NULL"),
    ("memories", "created_by_agent_id", "TEXT NULL"),
    ("memories", "run_id", "TEXT NULL"),
    ("memories", "superseded_by", "TEXT NULL"),
    ("memories", "supersedes", "TEXT NULL"),
    # Derived retrieval keys (mirrors Postgres migration 20260707_0082):
    # NULL = never derived (backfill target), '' = derived, nothing to add.
    ("memories", "fact_keys", "TEXT NULL"),
    ("agent_api_keys", "project_scope", "TEXT NULL"),
)

# One-shot backfills that run only when the paired additive column was just
# added to a pre-existing file (mirrors the Postgres 20260704_0077 backfill:
# supersession pointers were previously recorded only in metadata_json; the
# metadata copies are kept for backward compatibility).
_ADDITIVE_COLUMN_BACKFILLS: dict[tuple[str, str], str] = {
    ("memories", "superseded_by"): """
        UPDATE memories
        SET superseded_by = json_extract(metadata_json, '$.superseded_by')
        WHERE superseded_by IS NULL
          AND json_type(metadata_json, '$.superseded_by') = 'text'
        """,
    ("memories", "supersedes"): """
        UPDATE memories
        SET supersedes = json_extract(metadata_json, '$.supersedes')
        WHERE supersedes IS NULL
          AND json_type(metadata_json, '$.supersedes') = 'text'
        """,
}

_SOURCE_DEDUPE_BACKFILL_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS sources_missing_dedupe_key_idx
      ON sources (captured_at ASC, id ASC)
      WHERE deleted_at IS NULL AND dedupe_key IS NULL
    """

_INDEX_AND_TRIGGER_STATEMENTS: tuple[str, ...] = (
    # Indexes mirroring the hot Postgres access paths.
    """
    CREATE INDEX IF NOT EXISTS sources_user_content_hash_idx
      ON sources (user_id, content_hash)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS sources_user_dedupe_key_unique_idx
      ON sources (user_id, dedupe_key)
      WHERE deleted_at IS NULL AND dedupe_key IS NOT NULL
    """,
    _SOURCE_DEDUPE_BACKFILL_INDEX_SQL,
    """
    CREATE INDEX IF NOT EXISTS sources_user_captured_idx
      ON sources (user_id, captured_at DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS source_chunks_source_index_idx
      ON source_chunks (source_id, chunk_index)
    """,
    """
    CREATE INDEX IF NOT EXISTS memories_user_status_updated_idx
      ON memories (user_id, status, updated_at DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS memories_user_domain_sensitivity_updated_idx
      ON memories (user_id, domain, sensitivity, updated_at DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS memories_user_live_canonical_text_idx
      ON memories (user_id, lower(canonical_text), domain, sensitivity)
      WHERE deleted_at IS NULL
        AND canonical_text IS NOT NULL
        AND status IN ('candidate', 'active', 'accepted', 'needs_review', 'private_only')
    """,
    """
    CREATE INDEX IF NOT EXISTS memories_user_pending_confirmation_idx
      ON memories (user_id, updated_at DESC, id DESC)
      WHERE deleted_at IS NULL
        AND status = 'needs_review'
        AND confirmation_status = 'unconfirmed'
        AND json_extract(metadata_json, '$.agentic_memory.confirmation.status') = 'pending'
    """,
    # Partial project-scope index (mirrors memories_user_project_idx from
    # Postgres migration 20260704_0076).
    """
    CREATE INDEX IF NOT EXISTS memories_user_project_idx
      ON memories (user_id, project_id)
      WHERE project_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS memories_user_project_staleness_idx
      ON memories (
        user_id,
        project_id,
        memory_type,
        COALESCE(last_confirmed_at, last_seen_at, created_at)
      )
      WHERE deleted_at IS NULL AND status = 'active'
    """,
    """
    CREATE INDEX IF NOT EXISTS memories_user_project_rollup_digest_idx
      ON memories (
        user_id,
        project_id,
        json_extract(metadata_json, '$.candidate_kind'),
        json_extract(metadata_json, '$.rollup_digest'),
        updated_at DESC,
        id DESC
      )
      WHERE deleted_at IS NULL
        AND status = 'candidate'
        AND json_extract(metadata_json, '$.rollup_digest') IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS memories_user_project_rollup_key_idx
      ON memories (
        user_id,
        project_id,
        json_extract(metadata_json, '$.candidate_kind'),
        json_extract(metadata_json, '$.rollup_key'),
        updated_at DESC,
        id DESC
      )
      WHERE deleted_at IS NULL
        AND status IN ('active', 'accepted')
        AND json_extract(metadata_json, '$.rollup_key') IS NOT NULL
    """,
    # Partial supersession index (mirrors memories_user_superseded_by_idx
    # from Postgres migration 20260704_0077).
    """
    CREATE INDEX IF NOT EXISTS memories_user_superseded_by_idx
      ON memories (user_id, superseded_by)
      WHERE superseded_by IS NOT NULL
    """,
    # Partial idempotency/confirmation lookup indexes: the commit service's
    # duck-typed fast paths (get_memory_by_commit_digest/confirmation_id)
    # need these to stay O(log n); the scale benchmark measured the Python
    # fallback at 222ms p50 by 10k memories.
    "DROP INDEX IF EXISTS memories_user_commit_digest_idx",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS memories_user_commit_digest_unique_idx
      ON memories (user_id, commit_digest)
      WHERE commit_digest IS NOT NULL
    """,
    "DROP INDEX IF EXISTS memories_user_confirmation_id_idx",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS memories_user_confirmation_id_unique_idx
      ON memories (user_id, confirmation_id)
      WHERE confirmation_id IS NOT NULL
    """,
    # Mirrors graph_edges_user_edge_idx from Postgres migration 20260510_0067.
    """
    CREATE INDEX IF NOT EXISTS graph_edges_user_edge_idx
      ON graph_edges (user_id, edge_type, created_at DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS memory_revisions_memory_created_idx
      ON memory_revisions (memory_id, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS provenance_links_target_idx
      ON provenance_links (user_id, target_type, target_id, created_at DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS open_loops_user_status_opened_idx
      ON open_loops (user_id, status, opened_at DESC, created_at DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS open_loops_user_automation_digest_idx
      ON open_loops (
        user_id,
        json_extract(metadata_json, '$.automation_digest'),
        project_id,
        person_id,
        created_at DESC,
        id DESC
      )
      WHERE json_extract(metadata_json, '$.automation_digest') IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS open_loops_user_idempotency_digest_uidx
      ON open_loops (user_id, json_extract(metadata_json, '$.idempotency_digest'))
      WHERE json_extract(metadata_json, '$.idempotency_digest') IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS event_log_user_type_occurred_idx
      ON event_log (user_id, event_type, occurred_at DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS event_log_user_target_occurred_idx
      ON event_log (user_id, target_type, target_id, occurred_at DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS event_log_user_occurred_idx
      ON event_log (user_id, occurred_at DESC, id DESC)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS event_log_project_update_target_idx
      ON event_log (
        user_id,
        target_type,
        target_id,
        event_type,
        occurred_at DESC,
        id DESC
      )
      WHERE event_type IN ({_PROJECT_UPDATE_EVENT_TYPES_SQL})
        AND target_type IS NOT NULL
        AND target_id IS NOT NULL
    """,
    f"""
    CREATE INDEX IF NOT EXISTS event_log_project_update_artifact_id_idx
      ON event_log (
        user_id,
        event_type,
        json_extract(payload_json, '$.artifact_id'),
        occurred_at DESC,
        id DESC
      )
      WHERE event_type IN ({_PROJECT_UPDATE_EVENT_TYPES_SQL})
    """,
    f"""
    CREATE INDEX IF NOT EXISTS event_log_project_update_candidate_memory_id_idx
      ON event_log (
        user_id,
        event_type,
        json_extract(payload_json, '$.candidate_memory_id'),
        occurred_at DESC,
        id DESC
      )
      WHERE event_type IN ({_PROJECT_UPDATE_EVENT_TYPES_SQL})
    """,
    f"""
    CREATE INDEX IF NOT EXISTS event_log_project_update_memory_id_idx
      ON event_log (
        user_id,
        event_type,
        json_extract(payload_json, '$.memory_id'),
        occurred_at DESC,
        id DESC
      )
      WHERE event_type IN ({_PROJECT_UPDATE_EVENT_TYPES_SQL})
    """,
    """
    CREATE INDEX IF NOT EXISTS agent_api_keys_user_agent_idx
      ON agent_api_keys (user_id, agent_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS browser_clip_capabilities_live_expiry_idx
      ON browser_clip_capabilities (user_id, expires_at)
      WHERE consumed_at IS NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS browser_clip_capabilities_consumed_idx
      ON browser_clip_capabilities (user_id, consumed_at)
      WHERE consumed_at IS NOT NULL
    """,
    # Mirrors vnext_entities_user_normalized_name_idx and
    # entity_relationship_events_entity_changed_idx from Postgres
    # migration 20260705_0078.
    """
    CREATE INDEX IF NOT EXISTS vnext_entities_user_normalized_name_idx
      ON vnext_entities (user_id, normalized_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS entity_relationship_events_entity_changed_idx
      ON entity_relationship_events (user_id, entity_id, changed_at DESC, id DESC)
    """,
    # Content-derived memory/entity links must never outlive the text that
    # produced them. Expire them in the same database transaction as any
    # content update, including writers that do not call the commit service.
    "DROP TRIGGER IF EXISTS memories_expire_derived_entity_edges",
    f"""
    CREATE TRIGGER memories_expire_derived_entity_edges
    BEFORE UPDATE OF title, canonical_text, summary, value ON memories
    WHEN NEW.title IS NOT OLD.title
      OR NEW.canonical_text IS NOT OLD.canonical_text
      OR NEW.summary IS NOT OLD.summary
      OR NEW.value IS NOT OLD.value
    BEGIN
      UPDATE graph_edges
      SET valid_to = CASE
        WHEN valid_from IS NOT NULL AND valid_from > {_NOW_UTC_ISO_SQL}
          THEN valid_from
        ELSE {_NOW_UTC_ISO_SQL}
      END
      WHERE user_id = OLD.user_id
        AND from_type = 'memory'
        AND from_id = OLD.id
        AND edge_type IN ('mentions', 'related_to_person')
        AND valid_to IS NULL;
    END
    """,
    # Append-only enforcement (mirrors app.reject_event_log_mutation and
    # app.reject_memory_revision_mutation in Postgres, as replaced by
    # migration 20260706_0079). Append-only stays the default posture:
    # DELETE is always rejected, and UPDATE is rejected unless the store
    # is in redaction mode AND only content columns change, to the
    # redaction marker shape (see module docstring). The UPDATE triggers
    # are DROPped before re-creation so database files created under the
    # old unconditional trigger bodies pick up the conditional ones
    # (CREATE TRIGGER IF NOT EXISTS alone would keep the stale body).
    # NULL-safe comparisons use IS, never =, so a NULL skeleton column
    # (e.g. actor_id) cannot slip an UPDATE past the WHEN clause.
    "DROP TRIGGER IF EXISTS event_log_append_only_update",
    f"""
    CREATE TRIGGER event_log_append_only_update
    BEFORE UPDATE ON event_log
    WHEN NOT (
      COALESCE((SELECT enabled FROM redaction_mode WHERE id = 1), 0) = 1
      AND NEW.id IS OLD.id
      AND NEW.user_id IS OLD.user_id
      AND NEW.event_type IS OLD.event_type
      AND NEW.actor_type IS OLD.actor_type
      AND NEW.actor_id IS OLD.actor_id
      AND NEW.target_type IS OLD.target_type
      AND NEW.target_id IS OLD.target_id
      AND NEW.occurred_at IS OLD.occurred_at
      AND NEW.trace_id IS OLD.trace_id
      AND NEW.run_id IS OLD.run_id
      AND NEW.integrity_hash IS NULL
      AND json_extract(NEW.payload_json, '$.redacted') IS 1
      AND json_type(NEW.payload_json, '$.memory_id') = 'text'
      AND length(trim(json_extract(NEW.payload_json, '$.memory_id'))) > 0
      AND json_extract(NEW.payload_json, '$.event_type') IS NEW.event_type
      AND (SELECT COUNT(*) FROM json_each(NEW.payload_json)) = 3
      -- Retain only the memory identity already proved by the immutable old
      -- event. Every direct linkage that is present must agree. Artifact-only
      -- linkages are resolved through same-user project-update events carrying
      -- an immutable candidate_memory_id; a missing or conflicting resolution
      -- fails closed.
      AND (
        OLD.target_type IS NOT 'memory'
        OR OLD.target_id IS json_extract(NEW.payload_json, '$.memory_id')
      )
      AND (
        json_type(OLD.payload_json, '$.memory_id') IS NOT 'text'
        OR json_extract(OLD.payload_json, '$.memory_id')
             IS json_extract(NEW.payload_json, '$.memory_id')
      )
      AND (
        json_type(OLD.payload_json, '$.candidate_memory_id') IS NOT 'text'
        OR json_extract(OLD.payload_json, '$.candidate_memory_id')
             IS json_extract(NEW.payload_json, '$.memory_id')
      )
      AND (
        OLD.target_type IS NOT 'artifact'
        OR (
          SELECT COUNT(DISTINCT json_extract(
                   artifact_event.payload_json,
                   '$.candidate_memory_id'
                 )) = 1
             AND MAX(json_extract(
                   artifact_event.payload_json,
                   '$.candidate_memory_id'
                 )) IS json_extract(NEW.payload_json, '$.memory_id')
          FROM event_log AS artifact_event
          WHERE artifact_event.user_id = OLD.user_id
            AND artifact_event.event_type IN ({_PROJECT_UPDATE_EVENT_TYPES_SQL})
            AND json_type(
                  artifact_event.payload_json,
                  '$.candidate_memory_id'
                ) = 'text'
            AND length(trim(json_extract(
                  artifact_event.payload_json,
                  '$.candidate_memory_id'
                ))) > 0
            AND (
              (
                artifact_event.target_type = 'artifact'
                AND artifact_event.target_id = OLD.target_id
              )
              OR (
                json_type(artifact_event.payload_json, '$.artifact_id') = 'text'
                AND json_extract(artifact_event.payload_json, '$.artifact_id')
                      = OLD.target_id
              )
            )
        )
      )
      AND (
        json_type(OLD.payload_json, '$.artifact_id') IS NOT 'text'
        OR (
          SELECT COUNT(DISTINCT json_extract(
                   artifact_event.payload_json,
                   '$.candidate_memory_id'
                 )) = 1
             AND MAX(json_extract(
                   artifact_event.payload_json,
                   '$.candidate_memory_id'
                 )) IS json_extract(NEW.payload_json, '$.memory_id')
          FROM event_log AS artifact_event
          WHERE artifact_event.user_id = OLD.user_id
            AND artifact_event.event_type IN ({_PROJECT_UPDATE_EVENT_TYPES_SQL})
            AND json_type(
                  artifact_event.payload_json,
                  '$.candidate_memory_id'
                ) = 'text'
            AND length(trim(json_extract(
                  artifact_event.payload_json,
                  '$.candidate_memory_id'
                ))) > 0
            AND (
              (
                artifact_event.target_type = 'artifact'
                AND artifact_event.target_id = json_extract(
                      OLD.payload_json,
                      '$.artifact_id'
                    )
              )
              OR (
                json_type(artifact_event.payload_json, '$.artifact_id') = 'text'
                AND json_extract(artifact_event.payload_json, '$.artifact_id')
                      = json_extract(OLD.payload_json, '$.artifact_id')
              )
            )
        )
      )
      AND (
        OLD.target_type IS 'memory'
        OR OLD.target_type IS 'artifact'
        OR json_type(OLD.payload_json, '$.memory_id') IS 'text'
        OR json_type(OLD.payload_json, '$.candidate_memory_id') IS 'text'
        OR json_type(OLD.payload_json, '$.artifact_id') IS 'text'
      )
    )
    BEGIN
      SELECT RAISE(ABORT, 'event_log is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_log_append_only_delete
    BEFORE DELETE ON event_log
    BEGIN
      SELECT RAISE(ABORT, 'event_log is append-only');
    END
    """,
    "DROP TRIGGER IF EXISTS memory_revisions_append_only_update",
    f"""
    CREATE TRIGGER memory_revisions_append_only_update
    BEFORE UPDATE ON memory_revisions
    WHEN NOT (
      COALESCE((SELECT enabled FROM redaction_mode WHERE id = 1), 0) = 1
      AND NEW.id IS OLD.id
      AND NEW.user_id IS OLD.user_id
      AND NEW.memory_id IS OLD.memory_id
      AND NEW.sequence_no IS OLD.sequence_no
      AND NEW.action IS OLD.action
      AND NEW.memory_key IS ('redacted.' || NEW.memory_id)
      AND json(NEW.source_event_ids) IS json('[]')
      AND NEW.revision_number IS OLD.revision_number
      AND NEW.revision_type IS OLD.revision_type
      AND NEW.actor_type IS OLD.actor_type
      AND NEW.actor_id IS OLD.actor_id
      AND NEW.created_at IS OLD.created_at
      AND NEW.text_after IS '{REDACTION_MARKER}'
      AND (
        (OLD.text_before IS NULL AND NEW.text_before IS NULL)
        OR (OLD.text_before IS NOT NULL AND NEW.text_before IS '{REDACTION_MARKER}')
      )
      AND (
        (OLD.reason IS NULL AND NEW.reason IS NULL)
        OR (OLD.reason IS NOT NULL AND NEW.reason IS '{REDACTION_MARKER}')
      )
      AND (
        (OLD.previous_value IS NULL AND NEW.previous_value IS NULL)
        OR (
          OLD.previous_value IS NOT NULL
          AND json(NEW.previous_value) IS json('{{"redacted":true}}')
        )
      )
      AND (
        (OLD.new_value IS NULL AND NEW.new_value IS NULL)
        OR (
          OLD.new_value IS NOT NULL
          AND json(NEW.new_value) IS json('{{"redacted":true}}')
        )
      )
      AND json(NEW.candidate) IS json('{{"redacted":true}}')
      AND json(NEW.metadata_json) IS json('{{"redacted":true}}')
    )
    BEGIN
      SELECT RAISE(ABORT, 'memory_revisions is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memory_revisions_append_only_delete
    BEFORE DELETE ON memory_revisions
    BEGIN
      SELECT RAISE(ABORT, 'memory_revisions is append-only');
    END
    """,
    # Relationship history is append-only (mirrors
    # app.reject_entity_relationship_event_mutation in Postgres
    # migration 20260705_0078).
    """
    CREATE TRIGGER IF NOT EXISTS entity_relationship_events_append_only_update
    BEFORE UPDATE ON entity_relationship_events
    BEGIN
      SELECT RAISE(ABORT, 'entity_relationship_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS entity_relationship_events_append_only_delete
    BEFORE DELETE ON entity_relationship_events
    BEGIN
      SELECT RAISE(ABORT, 'entity_relationship_events is append-only');
    END
    """,
    # External-content FTS5 index over the same fields as the Postgres
    # search_tsv column (plus memory_key), synced by triggers. The porter
    # stemmer mirrors the 'english' text-search configuration Postgres FTS
    # uses, so "approved" matches a query for "approve". fact_keys carries
    # the derived retrieval keys (alicebot_api.vnext_fact_keys), mirroring
    # the Postgres 'D'-weighted search_tsv term from migration
    # 20260707_0082.
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
      title,
      canonical_text,
      summary,
      memory_key,
      fact_keys,
      content='memories',
      tokenize='porter unicode61'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_after_insert
    AFTER INSERT ON memories
    BEGIN
      INSERT INTO memories_fts(rowid, title, canonical_text, summary, memory_key, fact_keys)
      VALUES (new.rowid, new.title, new.canonical_text, new.summary, new.memory_key, new.fact_keys);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_after_delete
    AFTER DELETE ON memories
    BEGIN
      INSERT INTO memories_fts(memories_fts, rowid, title, canonical_text, summary, memory_key, fact_keys)
      VALUES ('delete', old.rowid, old.title, old.canonical_text, old.summary, old.memory_key, old.fact_keys);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_after_update
    AFTER UPDATE ON memories
    BEGIN
      INSERT INTO memories_fts(memories_fts, rowid, title, canonical_text, summary, memory_key, fact_keys)
      VALUES ('delete', old.rowid, old.title, old.canonical_text, old.summary, old.memory_key, old.fact_keys);
      INSERT INTO memories_fts(rowid, title, canonical_text, summary, memory_key, fact_keys)
      VALUES (new.rowid, new.title, new.canonical_text, new.summary, new.memory_key, new.fact_keys);
    END
    """,
    # External-content FTS5 index over source_chunks.text (mirrors the
    # Postgres search_tsv column from migration 20260707_0081), synced by
    # triggers, so source retrieval can find the session that SAYS the
    # thing instead of only title/recency matches. Same porter stemmer as
    # memories_fts for Postgres 'english' text-search parity.
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS source_chunks_fts USING fts5(
      text,
      content='source_chunks',
      tokenize='porter unicode61'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS source_chunks_fts_after_insert
    AFTER INSERT ON source_chunks
    BEGIN
      INSERT INTO source_chunks_fts(rowid, text)
      VALUES (new.rowid, new.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS source_chunks_fts_after_delete
    AFTER DELETE ON source_chunks
    BEGIN
      INSERT INTO source_chunks_fts(source_chunks_fts, rowid, text)
      VALUES ('delete', old.rowid, old.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS source_chunks_fts_after_update
    AFTER UPDATE ON source_chunks
    BEGIN
      INSERT INTO source_chunks_fts(source_chunks_fts, rowid, text)
      VALUES ('delete', old.rowid, old.text);
      INSERT INTO source_chunks_fts(rowid, text)
      VALUES (new.rowid, new.text);
    END
    """,
)


# Full statement list, kept for introspection; bootstrap_sqlite_schema
# interleaves the additive-column guard between the two halves.
_SCHEMA_STATEMENTS: tuple[str, ...] = _TABLE_STATEMENTS + _INDEX_AND_TRIGGER_STATEMENTS

# External-content FTS5 tables whose sync triggers only cover writes made
# after the table exists. When bootstrap creates one of these against a
# pre-existing database file that already holds content rows, the fresh
# index would silently hide every earlier row from full-text search, so
# bootstrap issues a one-shot FTS5 'rebuild' for exactly the tables it
# just created (a rebuild of a brand-new empty database is a no-op).
_EXTERNAL_CONTENT_FTS_TABLES: tuple[str, ...] = ("memories_fts", "source_chunks_fts")

# The column set each FTS table's current DDL declares. CREATE VIRTUAL
# TABLE IF NOT EXISTS never alters an existing table, so a database file
# written before a column shipped (e.g. memories_fts before fact_keys)
# keeps the old shape -- and its old sync triggers would keep feeding the
# old column list. Bootstrap detects the mismatch, drops the table AND
# its triggers, and lets the normal create-plus-rebuild path recreate
# both against the full current column set.
_FTS_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "memories_fts": ("title", "canonical_text", "summary", "memory_key", "fact_keys"),
    "source_chunks_fts": ("text",),
}

_FTS_SYNC_TRIGGERS: dict[str, tuple[str, ...]] = {
    "memories_fts": (
        "memories_fts_after_insert",
        "memories_fts_after_delete",
        "memories_fts_after_update",
    ),
    "source_chunks_fts": (
        "source_chunks_fts_after_insert",
        "source_chunks_fts_after_delete",
        "source_chunks_fts_after_update",
    ),
}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    # Tolerate any row_factory the caller installed (tuples, sqlite3.Row,
    # or the store's dict rows): resolve the "name" column positionally
    # from the cursor description instead of hardcoding an index.
    cursor = conn.execute(f"PRAGMA table_info({table})")
    name_index = next(index for index, description in enumerate(cursor.description) if description[0] == "name")
    columns: set[str] = set()
    for row in cursor.fetchall():
        if isinstance(row, dict):
            columns.add(str(row["name"]))
        else:
            columns.add(str(row[name_index]))
    return columns


def _table_column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    """Column names in storage order, tolerant of the caller's row factory."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    name_index = next(index for index, description in enumerate(cursor.description) if description[0] == "name")
    names: list[str] = []
    for row in cursor.fetchall():
        names.append(str(row["name"] if isinstance(row, dict) else row[name_index]))
    return names


def _memories_table_sql(conn: sqlite3.Connection) -> str | None:
    cursor = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'memories'")
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        value = row.get("sql")
    else:
        value = row[0]
    return str(value) if value is not None else None


def _current_memories_create_statement() -> str:
    for statement in _TABLE_STATEMENTS:
        if "CREATE TABLE IF NOT EXISTS memories (" in statement:
            return statement
    raise RuntimeError("current memories CREATE TABLE statement is missing")


def _ensure_current_memories_status_constraint(conn: sqlite3.Connection) -> None:
    """Rebuild pre-staleness ``memories`` tables without losing child rows.

    SQLite cannot alter a named CHECK constraint. Files created by v0.7.0
    therefore retained a status vocabulary that rejected the later ``stale``
    lifecycle value even after every additive column had been installed.
    Rebuild the parent table transactionally with foreign-key enforcement
    temporarily disabled, copy all common columns, then validate every child
    reference before commit. FTS is recreated and rebuilt by the ordinary
    bootstrap path below.
    """
    table_sql = _memories_table_sql(conn)
    if table_sql is None or "'stale'" in table_sql:
        return

    # bootstrap is an opening-time schema operation. Finish any caller-owned
    # setup transaction before toggling foreign_keys; SQLite ignores that
    # PRAGMA while a transaction is active.
    if conn.in_transaction:
        conn.commit()
    foreign_keys_row = conn.execute("PRAGMA foreign_keys").fetchone()
    foreign_keys_value = (
        next(iter(foreign_keys_row.values())) if isinstance(foreign_keys_row, dict) else foreign_keys_row[0]
    )
    foreign_keys_enabled = int(foreign_keys_value)
    old_columns = _table_column_names(conn, "memories")
    temp_table = "memories__schema_upgrade"
    create_sql = _current_memories_create_statement().replace(
        "CREATE TABLE IF NOT EXISTS memories (",
        f"CREATE TABLE {temp_table} (",
        1,
    )

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for trigger in _FTS_SYNC_TRIGGERS["memories_fts"]:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DROP TABLE IF EXISTS memories_fts")
        conn.execute(f"DROP TABLE IF EXISTS {temp_table}")
        conn.execute(create_sql)
        new_columns = _table_column_names(conn, temp_table)
        common_columns = [column for column in new_columns if column in old_columns]
        quoted = ", ".join(f'"{column}"' for column in common_columns)
        conn.execute(f"INSERT INTO {temp_table} ({quoted}) SELECT {quoted} FROM memories")
        conn.execute("DROP TABLE memories")
        conn.execute(f"ALTER TABLE {temp_table} RENAME TO memories")
        for table, column, _declaration in _ADDITIVE_COLUMNS:
            if table == "memories" and column not in old_columns:
                backfill = _ADDITIVE_COLUMN_BACKFILLS.get((table, column))
                if backfill is not None:
                    conn.execute(backfill)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"memories schema upgrade would leave {len(violations)} foreign-key violation(s)"
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys_enabled else 'OFF'}")


def _ensure_additive_columns(conn: sqlite3.Connection) -> None:
    """Upgrade pre-existing database files with columns added after v1 DDL.

    A column's backfill (if any) runs exactly once, in the same bootstrap
    that adds the column; fresh files whose CREATE TABLE already carries the
    column never re-run it.
    """
    for table, column, declaration in _ADDITIVE_COLUMNS:
        if column not in _table_columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            backfill = _ADDITIVE_COLUMN_BACKFILLS.get((table, column))
            if backfill is not None:
                conn.execute(backfill)


def _backfill_legacy_memory_project_scopes(conn: sqlite3.Connection) -> None:
    """Promote the legacy nested agentic scope without guessing.

    Early agentic-memory writers persisted scope only at
    ``metadata_json.agentic_memory.project_scope``.  Canonical readers use the
    top-level metadata array, while the singular ``project_id`` column is safe
    only when the normalized scope contains exactly one project.  The query is
    deliberately limited to rows where the canonical key is absent. An
    existing empty or malformed canonical value is authoritative and must
    never be rewritten from stale compatibility metadata.
    """
    cursor = conn.execute(
        """
        SELECT id, project_id, metadata_json
        FROM memories
        WHERE json_type(metadata_json, '$.agentic_memory.project_scope') = 'array'
          AND json_array_length(metadata_json, '$.agentic_memory.project_scope') > 0
          AND json_type(metadata_json, '$.project_scope') IS NULL
        """
    )
    column_indexes = {description[0]: index for index, description in enumerate(cursor.description)}
    for raw_row in cursor.fetchall():
        if isinstance(raw_row, dict):
            memory_id = raw_row["id"]
            project_id = raw_row["project_id"]
            metadata_text = raw_row["metadata_json"]
        else:
            memory_id = raw_row[column_indexes["id"]]
            project_id = raw_row[column_indexes["project_id"]]
            metadata_text = raw_row[column_indexes["metadata_json"]]
        try:
            metadata = json.loads(str(metadata_text))
        except (TypeError, ValueError):
            continue
        if not isinstance(metadata, dict):
            continue
        agentic = metadata.get("agentic_memory")
        if not isinstance(agentic, dict):
            continue
        scope = normalize_project_scope(agentic.get("project_scope"))
        if not scope:
            continue
        metadata["project_scope"] = list(scope)
        singleton_project_id = scope[0] if len(scope) == 1 and project_id is None else project_id
        conn.execute(
            """
            UPDATE memories
            SET metadata_json = ?, project_id = ?
            WHERE id = ?
            """,
            (
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                singleton_project_id,
                memory_id,
            ),
        )


def _source_capture_dedupe_key(
    *,
    raw_text: str,
    project_scope: object,
    domain: object,
    sensitivity: object,
) -> str:
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    scope = project_scope_identity(project_scope)
    if scope:
        normalized += "\x1fproject_scope:" + "\x1f".join(scope)
    normalized += "\x1fdomain:" + str(domain or "unknown").strip().casefold()
    normalized += "\x1fsensitivity:" + str(sensitivity or "unknown").strip().casefold()
    return "capture-md5:" + md5(normalized.encode("utf-8"), usedforsecurity=False).hexdigest()


def _backfill_source_dedupe_keys(conn: sqlite3.Connection) -> None:
    """Backfill only live sources that still lack a capture identity.

    The partial work index makes the common reopening path proportional to
    missing rows instead of the complete source corpus. Metadata is decoded
    only for those rows, while a correlated conflict check preserves the
    earliest identity when historical duplicates already exist.
    """
    rows = conn.execute(
        """
        SELECT id, user_id, content_hash, metadata_json, domain, sensitivity
        FROM sources
        WHERE deleted_at IS NULL
          AND dedupe_key IS NULL
        ORDER BY captured_at ASC, id ASC
        """
    ).fetchall()
    for row in rows:
        if isinstance(row, dict):
            record = dict(row)
        else:
            record = {
                "id": row[0],
                "user_id": row[1],
                "content_hash": row[2],
                "metadata_json": row[3],
                "domain": row[4],
                "sensitivity": row[5],
            }
        metadata_raw = record["metadata_json"]
        if isinstance(metadata_raw, str):
            try:
                metadata = json.loads(metadata_raw)
            except json.JSONDecodeError:
                metadata = {}
        elif isinstance(metadata_raw, dict):
            metadata = metadata_raw
        else:
            metadata = {}
        raw_text = metadata.get("raw_text")
        # Resolve the persisted source row through the same presence-aware
        # canonical/legacy precedence as runtime reads.  In particular, early
        # sources may carry scope under agentic/agent identity metadata or the
        # singular project aliases; treating only metadata.project_scope as
        # scoped would permanently assign those rows the global dedupe key.
        source_scope = resolve_source_metadata_project_scope(metadata).values
        if isinstance(raw_text, str):
            # The capture surface rejects a Python-whitespace-only string.
            # Such a historical row has no reproducible capture identity, so
            # leave its live dedupe key NULL instead of substituting the
            # content hash used only when raw_text is absent/non-string.
            key = (
                _source_capture_dedupe_key(
                    raw_text=raw_text,
                    project_scope=source_scope,
                    domain=record["domain"],
                    sensitivity=record["sensitivity"],
                )
                if raw_text.strip()
                else None
            )
        else:
            key = str(record["content_hash"])
        conn.execute(
            """
            UPDATE sources AS candidate
            SET dedupe_key = ?
            WHERE candidate.id = ?
              AND candidate.deleted_at IS NULL
              AND candidate.dedupe_key IS NULL
              AND NOT EXISTS (
                SELECT 1
                FROM sources AS occupied
                WHERE occupied.user_id = candidate.user_id
                  AND occupied.dedupe_key = ?
                  AND occupied.deleted_at IS NULL
              )
            """,
            (key, str(record["id"]), key),
        )


_SOURCE_DEDUPE_IDENTITY_STATE_KEY = "source_dedupe_identity_version"
_SOURCE_DEDUPE_IDENTITY_VERSION = "6"


def _repair_source_dedupe_identity(conn: sqlite3.Connection) -> None:
    """Versioned repair to the current conservative project identity."""

    state = conn.execute(
        "SELECT value FROM alice_schema_state WHERE key = ?",
        (_SOURCE_DEDUPE_IDENTITY_STATE_KEY,),
    ).fetchone()
    state_value = state.get("value") if isinstance(state, dict) else (state[0] if state else None)
    if state_value == _SOURCE_DEDUPE_IDENTITY_VERSION:
        _backfill_source_dedupe_keys(conn)
        return

    conn.execute("DROP INDEX IF EXISTS sources_user_dedupe_key_unique_idx")
    conn.execute("UPDATE sources SET dedupe_key = NULL WHERE deleted_at IS NULL")
    _backfill_source_dedupe_keys(conn)
    conn.execute(
        """
        CREATE UNIQUE INDEX sources_user_dedupe_key_unique_idx
          ON sources (user_id, dedupe_key)
          WHERE deleted_at IS NULL AND dedupe_key IS NOT NULL
        """
    )
    conn.execute(
        """
        INSERT INTO alice_schema_state (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (_SOURCE_DEDUPE_IDENTITY_STATE_KEY, _SOURCE_DEDUPE_IDENTITY_VERSION),
    )


def _backfill_memory_agent_attribution(conn: sqlite3.Connection) -> None:
    """Promote historical JSON-only agent/run attribution into scope columns."""
    conn.execute(
        """
        UPDATE memories
        SET created_by_agent_id = COALESCE(
              json_extract(metadata_json, '$.agent_id'),
              json_extract(metadata_json, '$.agent_identity.agent_id'),
              json_extract(metadata_json, '$.agentic_memory.agent_id'),
              json_extract(metadata_json, '$.agentic_memory.agent_identity.agent_id')
            )
        WHERE created_by_agent_id IS NULL
          AND COALESCE(
                json_extract(metadata_json, '$.agent_id'),
                json_extract(metadata_json, '$.agent_identity.agent_id'),
                json_extract(metadata_json, '$.agentic_memory.agent_id'),
                json_extract(metadata_json, '$.agentic_memory.agent_identity.agent_id')
              ) IS NOT NULL
        """
    )
    conn.execute(
        """
        UPDATE memories
        SET run_id = COALESCE(
              json_extract(metadata_json, '$.agent_run_id'),
              json_extract(metadata_json, '$.run_id'),
              json_extract(metadata_json, '$.agent_identity.agent_run_id'),
              json_extract(metadata_json, '$.agentic_memory.agent_run_id'),
              json_extract(metadata_json, '$.agentic_memory.agent_identity.agent_run_id')
            )
        WHERE run_id IS NULL
          AND COALESCE(
                json_extract(metadata_json, '$.agent_run_id'),
                json_extract(metadata_json, '$.run_id'),
                json_extract(metadata_json, '$.agent_identity.agent_run_id'),
                json_extract(metadata_json, '$.agentic_memory.agent_run_id'),
                json_extract(metadata_json, '$.agentic_memory.agent_identity.agent_run_id')
              ) IS NOT NULL
        """
    )


def _deduplicate_memory_lookup_values(conn: sqlite3.Connection) -> None:
    """Preserve rows while making retry/confirmation identifiers unique.

    Old files could contain duplicates because their lookup indexes were not
    unique. The oldest *active* (non-deleted) row remains the canonical replay
    target; later rows keep their content and audit history but relinquish the
    ambiguous lookup value, with the canonical row id recorded in metadata.

    Preferring an active row matters because the runtime replay lookups
    (``get_memory_by_commit_digest`` / ``get_memory_by_confirmation_id``) filter
    ``deleted_at IS NULL``. Keeping the identifier on an older tombstone would
    make replay return nothing while the partial unique index still blocks
    re-inserting the same key. Only when every duplicate is deleted does the
    oldest surviving (deleted) row keep the value.
    """
    index_rows = conn.execute("PRAGMA index_list(memories)").fetchall()
    unique_indexes: set[str] = set()
    for row in index_rows:
        if isinstance(row, dict):
            name, unique = str(row["name"]), int(row["unique"])
        else:
            # PRAGMA index_list: seq, name, unique, origin, partial.
            name, unique = str(row[1]), int(row[2])
        if unique:
            unique_indexes.add(name)

    specifications = (
        (
            "commit_digest",
            "$.agentic_memory.idempotency_key",
            "duplicate_commit_digest_canonical_memory_id",
            "memories_user_commit_digest_unique_idx",
        ),
        (
            "confirmation_id",
            "$.agentic_memory.confirmation.confirmation_id",
            "duplicate_confirmation_id_canonical_memory_id",
            "memories_user_confirmation_id_unique_idx",
        ),
    )
    if all(specification[3] in unique_indexes for specification in specifications):
        return

    for column, json_path, migration_key, unique_index_name in specifications:
        if unique_index_name in unique_indexes:
            continue
        duplicate_groups = conn.execute(
            f"""
            SELECT user_id, lookup_value, id AS canonical_id
            FROM (
              SELECT
                id,
                user_id,
                {column} AS lookup_value,
                ROW_NUMBER() OVER (
                  PARTITION BY user_id, {column}
                  ORDER BY (deleted_at IS NOT NULL), created_at ASC, id ASC
                ) AS duplicate_rank,
                COUNT(*) OVER (PARTITION BY user_id, {column}) AS duplicate_count
              FROM memories
              WHERE {column} IS NOT NULL
            ) AS ranked
            WHERE duplicate_rank = 1
              AND duplicate_count > 1
            """
        ).fetchall()
        for row in duplicate_groups:
            if isinstance(row, dict):
                user_id = row["user_id"]
                value = row["lookup_value"]
                canonical_id = row["canonical_id"]
            else:
                user_id, value, canonical_id = row
            conn.execute(
                f"""
                UPDATE memories
                SET {column} = NULL,
                    metadata_json = json_set(
                      json_remove(metadata_json, ?),
                      ?,
                      ?
                    )
                WHERE user_id = ?
                  AND {column} = ?
                  AND id <> ?
                """,
                (
                    json_path,
                    f"$.lifecycle_migration.{migration_key}",
                    str(canonical_id),
                    str(user_id),
                    value,
                    str(canonical_id),
                ),
            )


def _repair_tombstone_lookup_value_holders(conn: sqlite3.Connection) -> None:
    """Move retry/confirmation identifiers off tombstones onto the live row.

    The dedup shipped in v0.9.2 kept the *earliest* duplicate regardless of
    deletion, so a file already opened under that version can have the
    identifier stranded on an older deleted row while the newer active row was
    cleared. This corrective pass runs on every bootstrap and repairs that
    exact shape: for any deleted row still holding an identifier whose cleared
    sibling (recorded via the ``lifecycle_migration`` back-pointer) is live, it
    releases the identifier from the tombstone and restores it onto the oldest
    such live row. It is idempotent — a healthy file matches nothing and no row
    is touched. The move is done as release-then-restore so the partial unique
    index is never momentarily violated when it is already in place.
    """
    specifications = (
        (
            "commit_digest",
            "$.agentic_memory.idempotency_key",
            "$.lifecycle_migration.duplicate_commit_digest_canonical_memory_id",
        ),
        (
            "confirmation_id",
            "$.agentic_memory.confirmation.confirmation_id",
            "$.lifecycle_migration.duplicate_confirmation_id_canonical_memory_id",
        ),
    )
    for column, mirror_path, pointer_path in specifications:
        repairs = conn.execute(
            f"""
            SELECT holder_id, holder_value, candidate_id
            FROM (
              SELECT
                holder.id AS holder_id,
                holder.{column} AS holder_value,
                (
                  SELECT candidate.id
                  FROM memories AS candidate
                  WHERE candidate.user_id = holder.user_id
                    AND candidate.deleted_at IS NULL
                    AND candidate.{column} IS NULL
                    AND json_extract(candidate.metadata_json, ?) = holder.id
                  ORDER BY candidate.created_at ASC, candidate.id ASC
                  LIMIT 1
                ) AS candidate_id
              FROM memories AS holder
              WHERE holder.{column} IS NOT NULL
                AND holder.deleted_at IS NOT NULL
            ) AS ranked
            WHERE candidate_id IS NOT NULL
            """,
            (pointer_path,),
        ).fetchall()
        for row in repairs:
            if isinstance(row, dict):
                holder_id = row["holder_id"]
                holder_value = row["holder_value"]
                candidate_id = row["candidate_id"]
            else:
                holder_id, holder_value, candidate_id = row
            # Release the identifier from the tombstone and repoint it at the
            # new live canonical row.
            conn.execute(
                f"""
                UPDATE memories
                SET {column} = NULL,
                    metadata_json = json_set(metadata_json, ?, ?)
                WHERE id = ?
                """,
                (pointer_path, str(candidate_id), str(holder_id)),
            )
            # Restore the identifier onto the live row, drop its stale
            # back-pointer, and re-populate the mirrored metadata value so the
            # repaired row matches a correctly-deduplicated canonical row.
            conn.execute(
                f"""
                UPDATE memories
                SET {column} = ?,
                    metadata_json = json_set(
                      json_remove(metadata_json, ?),
                      ?,
                      ?
                    )
                WHERE id = ?
                """,
                (
                    holder_value,
                    pointer_path,
                    mirror_path,
                    str(holder_value),
                    str(candidate_id),
                ),
            )


# The tables whose rows ``apply_row_backfills`` can change.
ROW_BACKFILL_TABLES = frozenset({"memories", "sources"})


def apply_row_backfills(conn: sqlite3.Connection) -> None:
    """Run the bootstrap steps that fill a column of a row from the row itself.

    ``bootstrap_sqlite_schema`` runs these on every open, so a row stored with
    one of these columns empty is rewritten the next time the file is opened:
    a source's ``dedupe_key``, a memory's ``created_by_agent_id`` and
    ``run_id``, and the legacy nested project scope in ``metadata_json`` and
    ``project_id``. ``alice-memory import --mode skip`` runs this on a scratch
    database to learn what a file row will hold once the vault is next opened,
    and compares that with the stored row. A step added to the bootstrap that
    does the same kind of fill belongs here too; a test runs both over the same
    rows and fails when they leave different results.
    """

    _backfill_source_dedupe_keys(conn)
    _backfill_legacy_memory_project_scopes(conn)
    _backfill_memory_agent_attribution(conn)


def _missing_fts_tables(conn: sqlite3.Connection) -> list[str]:
    """FTS tables not present yet; tolerant of any installed row_factory."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({})".format(
            ", ".join("?" for _ in _EXTERNAL_CONTENT_FTS_TABLES)
        ),
        _EXTERNAL_CONTENT_FTS_TABLES,
    )
    present: set[str] = set()
    for row in cursor.fetchall():
        if isinstance(row, dict):
            present.add(str(row["name"]))
        else:
            present.add(str(row[0]))
    return [name for name in _EXTERNAL_CONTENT_FTS_TABLES if name not in present]


def _drop_outdated_fts_tables(conn: sqlite3.Connection) -> None:
    """Drop FTS tables (and their sync triggers) whose columns are stale.

    Runs before ``_missing_fts_tables`` is consulted, so a dropped table
    counts as "just created" and gets the one-shot ``'rebuild'`` that
    re-indexes every pre-existing content row under the new column set.
    """
    missing = set(_missing_fts_tables(conn))
    for name in _EXTERNAL_CONTENT_FTS_TABLES:
        if name in missing:
            continue
        required = set(_FTS_TABLE_COLUMNS[name])
        if required.issubset(_table_columns(conn, name)):
            continue
        for trigger in _FTS_SYNC_TRIGGERS[name]:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute(f"DROP TABLE {name}")


def _relabel_derived_domains(conn: sqlite3.Connection) -> None:
    from alicebot_api.vnext_derived_domain_backfill import relabel_sqlite

    relabel_sqlite(conn)


def _relabel_derived_labels(conn: sqlite3.Connection) -> None:
    """Repair derived labels. A failure leaves the vault open and the key unstamped."""

    import logging

    from alicebot_api.vnext_label_repair import relabel_labels_sqlite

    conn.execute("SAVEPOINT alice_derived_labels_v3")
    try:
        relabel_labels_sqlite(conn)
    except Exception as exc:
        conn.execute("ROLLBACK TO SAVEPOINT alice_derived_labels_v3")
        conn.execute("RELEASE SAVEPOINT alice_derived_labels_v3")
        left = getattr(exc, "left", "an unknown number of")
        logging.getLogger(__name__).warning(
            "alice-memory: label repair did not run, %s rows left to the read check; run alice-memory labels check",
            left,
        )
        return
    conn.execute("RELEASE SAVEPOINT alice_derived_labels_v3")


def bootstrap_sqlite_schema(conn: sqlite3.Connection, *, repair_labels: bool = True) -> None:
    """Create or upgrade the vNext SQLite schema. Safe to call repeatedly."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    for statement in _TABLE_STATEMENTS:
        conn.execute(statement)
    # CREATE TABLE IF NOT EXISTS cannot extend CHECK vocabularies. Upgrade
    # v0.7-era files before additive columns/indexes are considered.
    _ensure_current_memories_status_constraint(conn)
    # Existing files created before the scope columns shipped need ALTERs
    # before the index statements reference the new columns.
    _ensure_additive_columns(conn)
    # Install the empty-work index before probing for rows. On healthy current
    # files this keeps every open independent of total source count.
    conn.execute(_SOURCE_DEDUPE_BACKFILL_INDEX_SQL)
    _repair_source_dedupe_identity(conn)
    _backfill_legacy_memory_project_scopes(conn)
    _backfill_memory_agent_attribution(conn)
    _deduplicate_memory_lookup_values(conn)
    # Corrective pass for files a buggy v0.9.2 dedup already stranded an
    # identifier on a tombstone (audit P1 #3); a no-op on healthy files.
    _repair_tombstone_lookup_value_holders(conn)
    _relabel_derived_domains(conn)
    if repair_labels:
        _relabel_derived_labels(conn)
    # The redaction flag row must exist before the append-only triggers
    # reference it, and it must be OFF: a crashed process must never leave
    # a database file with redaction mode stuck open.
    conn.execute("INSERT OR IGNORE INTO redaction_mode (id, enabled) VALUES (1, 0)")
    conn.execute("UPDATE redaction_mode SET enabled = 0 WHERE id = 1")
    # Seed the resident-vector-cache invalidation stamp with a random token.
    # INSERT OR IGNORE: an existing token survives re-bootstrap on purpose
    # (embedding bytes did not change, so caches built at that token stay
    # valid). Embedding writers rewrite it via vector_scan.bump_embedding_stamp.
    conn.execute(
        "INSERT OR IGNORE INTO embedding_stamp (id, token) VALUES (1, ?)",
        (uuid4().hex,),
    )
    # FTS tables with a stale column set are dropped (with their triggers)
    # so the create-plus-rebuild path below re-indexes existing rows.
    _drop_outdated_fts_tables(conn)
    created_fts_tables = _missing_fts_tables(conn)
    for statement in _INDEX_AND_TRIGGER_STATEMENTS:
        conn.execute(statement)
    # One-shot backfill for FTS tables this bootstrap just created over
    # pre-existing content rows; see _EXTERNAL_CONTENT_FTS_TABLES.
    for name in created_fts_tables:
        conn.execute(f"INSERT INTO {name}({name}) VALUES('rebuild')")


__all__ = [
    "AGENT_TYPES",
    "DOMAINS",
    "EDGE_TYPES",
    "ENTITY_TYPES",
    "EVIDENCE_ROLES",
    "MEMORY_CONFIRMATION_STATUSES",
    "MEMORY_PROMOTION_ELIGIBILITIES",
    "MEMORY_STATUSES",
    "MEMORY_TRUST_CLASSES",
    "MEMORY_TYPES",
    "OPEN_LOOP_PRIORITIES",
    "OPEN_LOOP_STATUSES",
    "PERMISSION_PROFILES",
    "REDACTION_MARKER",
    "REVISION_TYPES",
    "ROW_BACKFILL_TABLES",
    "SENSITIVITY_LEVELS",
    "apply_row_backfills",
    "bootstrap_sqlite_schema",
]
