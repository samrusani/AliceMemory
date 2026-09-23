"""SQLite memory lifecycle, redaction, and provenance store seam."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager

from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_project_scope import canonical_memory_metadata
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_stores.memory_lifecycle_common import (
    REDACTED_JSON_VALUE,
    REDACTION_MARKER,
    is_prior_redacted_memory_marker,
    is_redacted_memory,
    redacted_memory_metadata,
    refuse_created_credential_activation,
    refuse_updated_credential_activation,
)
from alicebot_api.vnext_stores.sqlite.columns import MEMORY_COLUMNS, PROVENANCE_COLUMNS
from alicebot_api.vnext_stores.sqlite.primitives import (
    _iso_or_none,
    _iso_or_now,
    _json_list_text,
    _json_object_text,
    _new_id,
    _sorted_field_names,
    _utc_now_iso,
    _uuid_text,
)
from alicebot_api.vnext_stores.sqlite.vector_scan import bump_embedding_stamp

VNextRow = dict[str, object]

def create_memory(self, memory: JsonObject, *, actor_type: str = "system") -> VNextRow:
    refuse_created_credential_activation(memory)
    memory_id = _new_id(memory.get("id"))
    now = _utc_now_iso()
    self._execute(
        """
                INSERT INTO memories (
                  id,
                  user_id,
                  agent_profile_id,
                  memory_key,
                  value,
                  status,
                  source_event_ids,
                  memory_type,
                  confidence,
                  salience,
                  confirmation_status,
                  trust_class,
                  promotion_eligibility,
                  evidence_count,
                  independent_source_count,
                  extracted_by_model,
                  trust_reason,
                  valid_from,
                  valid_to,
                  last_confirmed_at,
                  title,
                  canonical_text,
                  summary,
                  domain,
                  sensitivity,
                  first_seen_at,
                  last_seen_at,
                  last_reviewed_at,
                  metadata_json,
                  commit_digest,
                  confirmation_id,
                  project_id,
                  created_by_agent_id,
                  run_id,
                  superseded_by,
                  supersedes,
                  created_at,
                  updated_at
                )
                VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(user_id, commit_digest)
                  WHERE commit_digest IS NOT NULL
                  DO NOTHING
                """,
        (
            memory_id,
            self.user_id,
            memory.get("agent_profile_id", "assistant_default"),
            memory["memory_key"],
            _json_object_text(memory.get("value")),
            memory.get("status", "candidate"),
            _json_list_text(memory.get("source_event_ids")),
            memory.get("memory_type", "semantic"),
            memory.get("confidence"),
            memory.get("salience"),
            memory.get("confirmation_status", "unconfirmed"),
            memory.get("trust_class", "deterministic"),
            memory.get("promotion_eligibility", "promotable"),
            memory.get("evidence_count"),
            memory.get("independent_source_count"),
            memory.get("extracted_by_model"),
            memory.get("trust_reason"),
            _iso_or_none(memory.get("valid_from")),
            _iso_or_none(memory.get("valid_to")),
            _iso_or_none(memory.get("last_confirmed_at")),
            memory.get("title"),
            memory.get("canonical_text", ""),
            memory.get("summary"),
            memory.get("domain", "unknown"),
            memory.get("sensitivity", "unknown"),
            _iso_or_now(memory.get("first_seen_at")),
            _iso_or_now(memory.get("last_seen_at")),
            _iso_or_none(memory.get("last_reviewed_at")),
            _json_object_text(canonical_memory_metadata(memory)),
            memory.get("commit_digest"),
            memory.get("confirmation_id"),
            memory.get("project_id"),
            memory.get("created_by_agent_id"),
            memory.get("run_id"),
            _uuid_text(memory.get("superseded_by")),
            _uuid_text(memory.get("supersedes")),
            now,
            now,
        ),
    )
    row = self._get_row("create_memory", "memories", MEMORY_COLUMNS, memory_id)
    self._append_mutation_event(
        event_type="memory.created",
        actor_type=actor_type,
        target_type="memory",
        target_id=row["id"],
        payload={"operation": "create", "fields": _sorted_field_names(memory)},
    )
    return row

def upsert_memory_by_key(self, memory: JsonObject, *, actor_type: str = "system") -> VNextRow:
    """Create a deterministic-key memory or replay its existing row."""

    memory_key = str(memory.get("memory_key") or "").strip()
    if memory_key == "":
        raise ValueError("memory_key must not be empty")
    agent_profile_id = str(memory.get("agent_profile_id") or "assistant_default")
    try:
        return self.create_memory(memory, actor_type=actor_type)
    except sqlite3.IntegrityError:
        existing = self.get_memory_by_key(
            memory_key=memory_key,
            agent_profile_id=agent_profile_id,
        )
        if existing is None:
            raise
        return existing

def get_memory_for_update(self, memory_id: str) -> VNextRow | None:
    """Acquire SQLite's writer lock before a review/lifecycle decision."""
    if not self.conn.in_transaction:
        self.conn.execute("BEGIN IMMEDIATE")
    return self.get_memory(memory_id)

def get_memory_for_redaction(self, memory_id: str) -> VNextRow | None:
    """Acquire the writer lock and load a redaction target tombstone."""

    if not self.conn.in_transaction:
        self.conn.execute("BEGIN IMMEDIATE")
    return self._fetch_optional_one(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE id = ?
                  AND user_id = ?
                """,
        (str(memory_id), self.user_id),
    )

def lock_project_update_artifacts_for_redaction(self, memory_id: str) -> list[VNextRow]:
    """SQLite intentionally has no generated-artifact repository."""

    del memory_id
    return []

def memory_redaction_bundle_is_exact(self, memory_id: str, artifact_ids: Sequence[str]) -> bool:
    if artifact_ids:
        return False
    mid = str(memory_id)
    row = self._fetch_one(
        "check exact sqlite memory redaction bundle",
        """
                SELECT
                  EXISTS (
                    SELECT 1 FROM event_log
                    WHERE user_id = ?
                      AND event_type = 'memory.redacted'
                      AND target_type = 'memory'
                      AND target_id = ?
                  )
                  AND EXISTS (
                    SELECT 1 FROM memories
                    WHERE user_id = ? AND id = ?
                      AND embedding IS NULL AND fact_keys IS NULL
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM memory_revisions
                    WHERE user_id = ? AND memory_id = ?
                      AND (
                        memory_key IS NOT 'redacted.' || memory_id
                        OR json(source_event_ids) IS NOT json('[]')
                        OR json(candidate) IS NOT json('{"redacted":true}')
                        OR text_after IS NOT '[REDACTED]'
                        OR text_before IS NOT CASE WHEN text_before IS NULL THEN NULL ELSE '[REDACTED]' END
                        OR reason IS NOT CASE WHEN reason IS NULL THEN NULL ELSE '[REDACTED]' END
                        OR json(previous_value) IS NOT json(CASE
                          WHEN previous_value IS NULL THEN NULL ELSE '{"redacted":true}' END)
                        OR json(new_value) IS NOT json(CASE
                          WHEN new_value IS NULL THEN NULL ELSE '{"redacted":true}' END)
                        OR json(metadata_json) IS NOT json('{"redacted":true}')
                      )
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM event_log
                    WHERE user_id = ?
                      AND (
                        (target_type = 'memory' AND target_id = ?)
                        OR json_extract(payload_json, '$.memory_id') = ?
                        OR json_extract(payload_json, '$.candidate_memory_id') = ?
                      )
                      AND (
                        json(payload_json) IS NOT json(json_object(
                          'redacted', json('true'), 'memory_id', ?, 'event_type', event_type
                        ))
                        OR integrity_hash IS NOT NULL
                      )
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM provenance_links
                    WHERE user_id = ? AND target_type = 'memory' AND target_id = ?
                      AND quote IS NOT NULL AND quote IS NOT '[REDACTED]'
                  ) AS exact
                """,
        (
            self.user_id,
            mid,
            self.user_id,
            mid,
            self.user_id,
            mid,
            self.user_id,
            mid,
            mid,
            mid,
            mid,
            self.user_id,
            mid,
        ),
    )
    return bool(row.get("exact"))

def update_memory(self, *, memory_id: str, patch: JsonObject, actor_type: str = "system") -> VNextRow:
    refuse_updated_credential_activation(patch, lambda: self.get_memory(str(memory_id)))
    cursor = self._execute(
        """
                UPDATE memories
                SET value = COALESCE(?, value),
                    status = COALESCE(?, status),
                    source_event_ids = COALESCE(?, source_event_ids),
                    memory_type = COALESCE(?, memory_type),
                    confidence = COALESCE(?, confidence),
                    salience = COALESCE(?, salience),
                    confirmation_status = COALESCE(?, confirmation_status),
                    trust_class = COALESCE(?, trust_class),
                    promotion_eligibility = COALESCE(?, promotion_eligibility),
                    evidence_count = COALESCE(?, evidence_count),
                    independent_source_count = COALESCE(?, independent_source_count),
                    extracted_by_model = COALESCE(?, extracted_by_model),
                    trust_reason = COALESCE(?, trust_reason),
                    valid_from = COALESCE(?, valid_from),
                    valid_to = COALESCE(?, valid_to),
                    last_confirmed_at = COALESCE(?, last_confirmed_at),
                    title = COALESCE(?, title),
                    canonical_text = COALESCE(?, canonical_text),
                    summary = COALESCE(?, summary),
                    domain = COALESCE(?, domain),
                    sensitivity = COALESCE(?, sensitivity),
                    last_seen_at = COALESCE(?, last_seen_at),
                    last_reviewed_at = COALESCE(?, last_reviewed_at),
                    metadata_json = COALESCE(?, metadata_json),
                    project_id = COALESCE(?, project_id),
                    superseded_by = COALESCE(?, superseded_by),
                    supersedes = COALESCE(?, supersedes),
                    updated_at = ?,
                    deleted_at = CASE
                      WHEN ? = 'archived' THEN ?
                      ELSE deleted_at
                    END
                WHERE id = ?
                  AND user_id = ?
                  AND deleted_at IS NULL
                """,
        (
            _json_object_text(patch["value"]) if "value" in patch else None,
            patch.get("status"),
            _json_list_text(patch["source_event_ids"]) if "source_event_ids" in patch else None,
            patch.get("memory_type"),
            patch.get("confidence"),
            patch.get("salience"),
            patch.get("confirmation_status"),
            patch.get("trust_class"),
            patch.get("promotion_eligibility"),
            patch.get("evidence_count"),
            patch.get("independent_source_count"),
            patch.get("extracted_by_model"),
            patch.get("trust_reason"),
            _iso_or_none(patch.get("valid_from")),
            _iso_or_none(patch.get("valid_to")),
            _iso_or_none(patch.get("last_confirmed_at")),
            patch.get("title"),
            patch.get("canonical_text"),
            patch.get("summary"),
            patch.get("domain"),
            patch.get("sensitivity"),
            _iso_or_none(patch.get("last_seen_at")),
            _iso_or_none(patch.get("last_reviewed_at")),
            _json_object_text(patch["metadata_json"]) if "metadata_json" in patch else None,
            patch.get("project_id"),
            _uuid_text(patch.get("superseded_by")),
            _uuid_text(patch.get("supersedes")),
            _utc_now_iso(),
            patch.get("status"),
            _utc_now_iso(),
            str(memory_id),
            self.user_id,
        ),
    )
    if cursor.rowcount == 0:
        raise ContinuityStoreInvariantError(
            "update_memory did not return a row from the database",
        )
    row = self._get_row("update_memory", "memories", MEMORY_COLUMNS, str(memory_id))
    self._append_mutation_event(
        event_type="memory.updated",
        actor_type=actor_type,
        target_type="memory",
        target_id=row["id"],
        payload={"operation": "update", "changes": patch},
    )
    return row

def lock_graph_mutation(self) -> None:
    """No-op: SQLite serializes writes with a single writer, so there is no
        concurrent supersession to guard against. Present for store parity with
        the Postgres advisory lock."""
    return None

def list_memory_ids_with_embeddings(self, ids: "Sequence[str]") -> set[str]:
    """Exact-ID embedding-presence read for a specific set of memory IDs.

        Consolidation and rollups must know which *selected* rows have stored
        vectors; a global ANN probe returns nearest neighbors, not a presence
        test. This reads presence directly by ID, chunked to stay within
        SQLite's bound-parameter limit.
        """
    id_list = [str(value) for value in ids if str(value)]
    present: set[str] = set()
    chunk_size = 400
    for start in range(0, len(id_list), chunk_size):
        chunk = id_list[start : start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        rows = self._fetch_all(
            f"""
                    SELECT id
                    FROM memories
                    WHERE user_id = ?
                      AND deleted_at IS NULL
                      AND embedding IS NOT NULL
                      AND id IN ({placeholders})
                    """,
            (self.user_id, *chunk),
        )
        present.update(str(row["id"]) for row in rows)
    return present

def update_memory_fact_keys(self, *, memory_id: str, fact_keys: str | None) -> VNextRow | None:
    """Store derived retrieval keys; the FTS sync triggers re-index them.

        ``None`` resets the row to the "never derived" state the backfill
        pass scans for; ``""`` marks "derived, nothing to add". Mirrors
        ``update_memory_embedding``: a plain indexing write, no revision.
        """
    if fact_keys is not None and not isinstance(fact_keys, str):
        raise ContinuityStoreInvariantError("fact_keys must be a string or None")
    normalized = re.sub(r"\s+", " ", fact_keys).strip() if isinstance(fact_keys, str) else None
    cursor = self._execute(
        """
                UPDATE memories
                SET fact_keys = ?
                WHERE id = ?
                  AND user_id = ?
                  AND deleted_at IS NULL
                """,
        (normalized, str(memory_id), self.user_id),
    )
    if cursor.rowcount == 0:
        return None
    return self._fetch_optional_one(
        """
                SELECT id
                FROM memories
                WHERE id = ?
                  AND user_id = ?
                """,
        (str(memory_id), self.user_id),
    )

def list_memories_missing_fact_keys(self, *, limit: int = 100, after_id: str | None = None) -> list[VNextRow]:
    """Backfill pagination over rows whose fact_keys was never derived."""
    return self._fetch_all(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  AND fact_keys IS NULL
                  AND (? IS NULL OR id > ?)
                ORDER BY id ASC
                LIMIT ?
                """,
        (self.user_id, after_id, after_id, limit),
    )

@contextmanager
def _redaction_mode(self) -> Iterator[None]:
    """Set/reset the privileged redaction flag around a block."""
    self._execute("UPDATE redaction_mode SET enabled = 1 WHERE id = 1")
    try:
        yield
    finally:
        self._execute("UPDATE redaction_mode SET enabled = 0 WHERE id = 1")

def redact_memory_bundle(
    self,
    *,
    memory_id: str,
    project_update_artifacts: Sequence[Mapping[str, object]],
    actor_type: str = "user",
) -> VNextRow:
    """Scrub the SQLite memory/revision/event/provenance parity surface.

        SQLite deliberately has no generated-artifact or quality-rating
        subsystem, so those response counts are always zero rather than being
        silently inferred from an unavailable table.
        """

    if project_update_artifacts:
        raise ContinuityStoreInvariantError("SQLite cannot redact generated artifacts")
    mid = str(memory_id)
    current = self._fetch_optional_one(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)},
                       embedding IS NULL AS _redaction_embedding_cleared,
                       fact_keys IS NULL AS _redaction_fact_keys_cleared
                FROM memories
                WHERE id = ? AND user_id = ?
            """,
        (mid, self.user_id),
    )
    if current is None:
        raise ContinuityStoreInvariantError("redact_memory_bundle did not find the memory to redact")
    current_metadata = current.get("metadata_json")
    prior_receipt = self._fetch_optional_one(
        """
                SELECT id FROM event_log
                WHERE user_id = ?
                  AND event_type = 'memory.redacted'
                  AND target_type = 'memory'
                  AND target_id = ?
                ORDER BY occurred_at ASC, id ASC
                LIMIT 1
            """,
        (self.user_id, mid),
    )
    prior_redacted_at = ""
    if is_prior_redacted_memory_marker(current) and prior_receipt is not None:
        assert isinstance(current_metadata, Mapping)
        prior_redacted_at = str(current_metadata.get("redacted_at") or "").strip()
    redacted_at = prior_redacted_at or _utc_now_iso()
    memory_metadata = redacted_memory_metadata(current_metadata, redacted_at=redacted_at)
    memory_key = f"redacted.{mid}"
    marker_json = _json_object_text(REDACTED_JSON_VALUE)
    metadata_json = _json_object_text(memory_metadata)

    with self._redaction_mode():
        provenance = self._execute(
            """
                UPDATE provenance_links
                SET quote = ?
                WHERE user_id = ?
                  AND target_type = 'memory'
                  AND target_id = ?
                  AND quote IS NOT NULL
                  AND quote IS NOT ?
                """,
            (REDACTION_MARKER, self.user_id, mid, REDACTION_MARKER),
        )
        redacted_provenance_links = provenance.rowcount

        revisions = self._execute(
            """
                UPDATE memory_revisions
                SET memory_key = ?,
                    previous_value = CASE WHEN previous_value IS NULL THEN NULL ELSE ? END,
                    new_value = CASE WHEN new_value IS NULL THEN NULL ELSE ? END,
                    source_event_ids = '[]',
                    candidate = ?,
                    text_before = CASE WHEN text_before IS NULL THEN NULL ELSE ? END,
                    text_after = ?,
                    reason = CASE WHEN reason IS NULL THEN NULL ELSE ? END,
                    metadata_json = ?
                WHERE memory_id = ?
                  AND user_id = ?
                  AND (
                    memory_key IS NOT ?
                    OR previous_value IS NOT CASE WHEN previous_value IS NULL THEN NULL ELSE ? END
                    OR new_value IS NOT CASE WHEN new_value IS NULL THEN NULL ELSE ? END
                    OR json(source_event_ids) IS NOT json('[]')
                    OR json(candidate) IS NOT json(?)
                    OR text_before IS NOT CASE WHEN text_before IS NULL THEN NULL ELSE ? END
                    OR text_after IS NOT ?
                    OR reason IS NOT CASE WHEN reason IS NULL THEN NULL ELSE ? END
                    OR json(metadata_json) IS NOT json(?)
                  )
                """,
            (
                memory_key,
                marker_json,
                marker_json,
                marker_json,
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                marker_json,
                mid,
                self.user_id,
                memory_key,
                marker_json,
                marker_json,
                marker_json,
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                marker_json,
            ),
        )
        redacted_revisions = revisions.rowcount

        events = self._execute(
            """
                UPDATE event_log
                SET payload_json = json_object(
                      'redacted', json('true'),
                      'memory_id', ?,
                      'event_type', event_type
                    ),
                    integrity_hash = NULL
                WHERE user_id = ?
                  AND (
                    (target_type = 'memory' AND target_id = ?)
                    OR (
                      json_type(payload_json, '$.memory_id') = 'text'
                      AND json_extract(payload_json, '$.memory_id') = ?
                    )
                    OR (
                      json_type(payload_json, '$.candidate_memory_id') = 'text'
                      AND json_extract(payload_json, '$.candidate_memory_id') = ?
                    )
                  )
                  AND (
                    json(payload_json) IS NOT json(json_object(
                      'redacted', json('true'),
                      'memory_id', ?,
                      'event_type', event_type
                    ))
                    OR integrity_hash IS NOT NULL
                  )
                """,
            (mid, self.user_id, mid, mid, mid, mid),
        )
        redacted_events = events.rowcount

        now = _utc_now_iso()
        memory_update = self._execute(
            """
                UPDATE memories
                SET memory_key = ?,
                    title = CASE WHEN title IS NULL THEN NULL ELSE ? END,
                    canonical_text = ?,
                    summary = CASE WHEN summary IS NULL THEN NULL ELSE ? END,
                    trust_reason = CASE WHEN trust_reason IS NULL THEN NULL ELSE ? END,
                    value = ?,
                    source_event_ids = '[]',
                    metadata_json = ?,
                    commit_digest = NULL,
                    confirmation_id = NULL,
                    embedding = NULL,
                    fact_keys = NULL,
                    status = 'archived',
                    deleted_at = COALESCE(deleted_at, ?),
                    updated_at = ?
                WHERE id = ?
                  AND user_id = ?
                  AND (
                    memory_key IS NOT ?
                    OR title IS NOT CASE WHEN title IS NULL THEN NULL ELSE ? END
                    OR canonical_text IS NOT ?
                    OR summary IS NOT CASE WHEN summary IS NULL THEN NULL ELSE ? END
                    OR trust_reason IS NOT CASE WHEN trust_reason IS NULL THEN NULL ELSE ? END
                    OR json(value) IS NOT json(?)
                    OR json(source_event_ids) IS NOT json('[]')
                    OR json(metadata_json) IS NOT json(?)
                    OR commit_digest IS NOT NULL
                    OR confirmation_id IS NOT NULL
                    OR embedding IS NOT NULL
                    OR fact_keys IS NOT NULL
                    OR status IS NOT 'archived'
                    OR deleted_at IS NULL
                  )
                """,
            (
                memory_key,
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                marker_json,
                metadata_json,
                now,
                now,
                mid,
                self.user_id,
                memory_key,
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                marker_json,
                metadata_json,
            ),
        )
        memory_changed = memory_update.rowcount > 0
        if not current.get("_redaction_embedding_cleared"):
            # Redaction NULLed a live vector: evict every resident vector
            # cache in the same transaction (owner-decided prompt eviction).
            bump_embedding_stamp(self._execute)

    redacted_memory = self._get_row("redact_memory_bundle", "memories", MEMORY_COLUMNS, mid)
    changed = bool(memory_changed or redacted_provenance_links or redacted_revisions or redacted_events)
    if changed:
        receipt = build_event_log_record(
            event_type="memory.redacted",
            actor_type=actor_type,
            target_type="memory",
            target_id=mid,
            payload={"redacted": True, "memory_id": mid, "event_type": "memory.redacted"},
        )
        receipt["integrity_hash"] = None
        self.append_event(receipt)

    return {
        "memory": redacted_memory,
        "redacted_revisions": redacted_revisions,
        "redacted_events": redacted_events,
        "redacted_artifacts": 0,
        "redacted_artifact_ids": [],
        "redacted_quality_ratings": 0,
        "redacted_provenance_links": redacted_provenance_links,
        "idempotent_replay": not changed,
    }

def redact_memory_content(self, *, memory_id: str, actor_type: str = "user") -> VNextRow:
    """Expunge a memory's content in place, keeping the skeleton.

        Content columns (title, canonical_text, summary, trust_reason,
        value) become the redaction marker, metadata_json is scrubbed to
        structural keys plus redacted_at, the content-derived columns
        (embedding, fact_keys) are cleared, and the row is archived.
        Applies to already-archived (soft-deleted) rows too -- that is
        the primary redaction target.
        """
    mid = str(memory_id)
    current = self._fetch_optional_one(
        """
                SELECT metadata_json,
                       (embedding IS NOT NULL) AS _redaction_had_embedding
                FROM memories
                WHERE id = ?
                  AND user_id = ?
                """,
        (mid, self.user_id),
    )
    if current is None:
        raise ContinuityStoreInvariantError(
            "redact_memory_content did not find the memory to redact",
        )
    now = _utc_now_iso()
    scrubbed = redacted_memory_metadata(current.get("metadata_json"), redacted_at=now)
    with self._redaction_mode():
        self._execute(
            """
                    UPDATE memories
                    SET memory_key = 'redacted.' || id,
                        title = CASE WHEN title IS NULL THEN NULL ELSE ? END,
                        canonical_text = ?,
                        summary = CASE WHEN summary IS NULL THEN NULL ELSE ? END,
                        trust_reason = CASE WHEN trust_reason IS NULL THEN NULL ELSE ? END,
                        value = ?,
                        source_event_ids = '[]',
                        metadata_json = ?,
                        commit_digest = NULL,
                        confirmation_id = NULL,
                        embedding = NULL,
                        fact_keys = NULL,
                        status = 'archived',
                        deleted_at = COALESCE(deleted_at, ?),
                        updated_at = ?
                    WHERE id = ?
                      AND user_id = ?
                    """,
            (
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                _json_object_text(REDACTED_JSON_VALUE),
                _json_object_text(scrubbed),
                now,
                now,
                mid,
                self.user_id,
            ),
        )
        if current.get("_redaction_had_embedding"):
            # Redaction NULLed a live vector: evict every resident vector
            # cache in the same transaction (owner-decided prompt eviction).
            bump_embedding_stamp(self._execute)
    row = self._get_row("redact_memory_content", "memories", MEMORY_COLUMNS, mid)
    self._append_mutation_event(
        event_type="memory.redacted",
        actor_type=actor_type,
        target_type="memory",
        target_id=row["id"],
        payload={"operation": "redact_memory_content"},
    )
    return row

def redact_memory_revisions(self, *, memory_id: str, actor_type: str = "user") -> VNextRow:
    """Expunge revision content for a memory, keeping the skeleton.

        text_before/text_after/reason become the marker (reasons can
        carry content, so they are redacted too); previous_value/
        new_value/candidate/metadata_json become {"redacted": true}.
        NULL content stays NULL so the created-vs-edited shape survives.
        ids, sequence/revision numbers, revision_type, actor columns,
        and created_at are untouched.
        """
    mid = str(memory_id)
    redacted_json = _json_object_text(REDACTED_JSON_VALUE)
    with self._redaction_mode():
        cursor = self._execute(
            """
                    UPDATE memory_revisions
                    SET memory_key = 'redacted.' || memory_id,
                        previous_value = CASE WHEN previous_value IS NULL THEN NULL ELSE ? END,
                        new_value = CASE WHEN new_value IS NULL THEN NULL ELSE ? END,
                        source_event_ids = '[]',
                        candidate = ?,
                        text_before = CASE WHEN text_before IS NULL THEN NULL ELSE ? END,
                        text_after = ?,
                        reason = CASE WHEN reason IS NULL THEN NULL ELSE ? END,
                        metadata_json = ?
                    WHERE memory_id = ?
                      AND user_id = ?
                    """,
            (
                redacted_json,
                redacted_json,
                redacted_json,
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                redacted_json,
                mid,
                self.user_id,
            ),
        )
        redacted_count = cursor.rowcount
    self._append_mutation_event(
        event_type="memory.redacted",
        actor_type=actor_type,
        target_type="memory",
        target_id=mid,
        payload={"operation": "redact_memory_revisions", "redacted_revisions": redacted_count},
    )
    return {"memory_id": mid, "redacted_revisions": redacted_count}

def redact_memory_events(self, *, memory_id: str, actor_type: str = "user") -> VNextRow:
    """Expunge event payloads that reference a memory.

        Matching rows keep event_type, actor columns, target columns,
        occurred_at, and trace/run references; payload_json becomes
        {"redacted": true, "memory_id": ..., "event_type": <own column>}
        and integrity_hash is cleared (it derives from the payload, so
        keeping it would allow confirming guesses of redacted content).
        """
    mid = str(memory_id)
    with self._redaction_mode():
        cursor = self._execute(
            """
                    UPDATE event_log
                    SET payload_json = json_object(
                          'redacted', json('true'),
                          'memory_id', ?,
                          'event_type', event_type
                        ),
                        integrity_hash = NULL
                    WHERE user_id = ?
                      AND (
                        (target_type = 'memory' AND target_id = ?)
                        OR (
                          json_type(payload_json, '$.memory_id') = 'text'
                          AND json_extract(payload_json, '$.memory_id') = ?
                        )
                        OR (
                          json_type(payload_json, '$.candidate_memory_id') = 'text'
                          AND json_extract(payload_json, '$.candidate_memory_id') = ?
                        )
                      )
                    """,
            (mid, self.user_id, mid, mid, mid),
        )
        redacted_count = cursor.rowcount
    self._append_mutation_event(
        event_type="memory.redacted",
        actor_type=actor_type,
        target_type="memory",
        target_id=mid,
        payload={"operation": "redact_memory_events", "redacted_events": redacted_count},
    )
    return {"memory_id": mid, "redacted_events": redacted_count}

def create_provenance_link(self, link: JsonObject, *, actor_type: str = "system") -> VNextRow:
    if (
        link.get("quote") is not None
        and link.get("target_type") == "memory"
        and (memory := self.get_memory_for_redaction(str(link.get("target_id") or ""))) is not None
        and is_redacted_memory(memory)
    ):
        raise ValueError("quoted provenance cannot be added to a redacted target")
    link_id = _new_id(link.get("id"))
    self._execute(
        """
                INSERT INTO provenance_links (
                  id,
                  user_id,
                  target_type,
                  target_id,
                  source_id,
                  source_chunk_id,
                  quote,
                  evidence_role,
                  confidence,
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
        (
            link_id,
            self.user_id,
            link["target_type"],
            link["target_id"],
            _uuid_text(link.get("source_id")),
            _uuid_text(link.get("source_chunk_id")),
            link.get("quote"),
            link.get("evidence_role", "supports"),
            link.get("confidence", 0.5),
            _utc_now_iso(),
        ),
    )
    row = self._get_row("create_provenance_link", "provenance_links", PROVENANCE_COLUMNS, link_id)
    self._append_mutation_event(
        event_type="provenance_link.created",
        actor_type=actor_type,
        target_type=str(row["target_type"]),
        target_id=str(row["target_id"]),
        payload={"operation": "create", "provenance_link_id": str(row["id"])},
    )
    return row

def list_provenance_links(self, *, target_type: str, target_id: str) -> list[VNextRow]:
    return self._fetch_all(
        f"""
                SELECT {", ".join(PROVENANCE_COLUMNS)}
                FROM provenance_links
                WHERE target_type = ?
                  AND target_id = ?
                  AND user_id = ?
                ORDER BY created_at DESC, id DESC
                """,
        (target_type, target_id, self.user_id),
    )

def list_provenance_links_for_targets(
    self,
    *,
    target_type: str,
    target_ids: Sequence[str],
) -> list[VNextRow]:
    ids = list(dict.fromkeys(str(target_id) for target_id in target_ids if target_id))
    if not ids:
        return []
    placeholders = self._placeholders(ids)
    return self._fetch_all(
        f"""
                SELECT {", ".join(PROVENANCE_COLUMNS)}
                FROM provenance_links
                WHERE user_id = ?
                  AND target_type = ?
                  AND target_id IN ({placeholders})
                ORDER BY created_at DESC, id DESC
                """,
        (self.user_id, target_type, *ids),
    )


for _method in (
    create_memory,
    upsert_memory_by_key,
    get_memory_for_update,
    get_memory_for_redaction,
    lock_project_update_artifacts_for_redaction,
    memory_redaction_bundle_is_exact,
    update_memory,
    lock_graph_mutation,
    list_memory_ids_with_embeddings,
    update_memory_fact_keys,
    list_memories_missing_fact_keys,
    _redaction_mode,
    redact_memory_bundle,
    redact_memory_content,
    redact_memory_revisions,
    redact_memory_events,
    create_provenance_link,
    list_provenance_links,
    list_provenance_links_for_targets,
):
    _method.__module__ = "alicebot_api.sqlite_store"
    _method.__qualname__ = f"SQLiteVNextStore.{_method.__name__}"
del _method

_redaction_generator = getattr(_redaction_mode, "__wrapped__")
_redaction_generator.__module__ = "alicebot_api.sqlite_store"
_redaction_generator.__qualname__ = "SQLiteVNextStore._redaction_mode"
del _redaction_generator
