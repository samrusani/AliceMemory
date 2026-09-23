"""PostgreSQL memory lifecycle, redaction, and provenance store seam."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime

from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_project_scope import canonical_memory_metadata
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_stores.memory_lifecycle_common import (
    REDACTED_JSON_VALUE,
    REDACTION_MARKER,
    is_prior_redacted_memory_marker,
    is_redacted_memory,
    is_redacted_project_update_artifact,
    redacted_memory_metadata,
    refuse_created_credential_activation,
    refuse_updated_credential_activation,
)
from alicebot_api.vnext_stores.postgres.columns import (
    ARTIFACT_COLUMNS,
    MEMORY_COLUMNS,
    PROVENANCE_COLUMNS,
)
from alicebot_api.vnext_stores.postgres.primitives import (
    _json_list,
    _json_object,
    _sorted_field_names,
)

VNextRow = dict[str, object]

def create_memory(self, memory: JsonObject, *, actor_type: str = "system") -> VNextRow:
    refuse_created_credential_activation(memory)
    row = self._fetch_one(
        "create_memory",
        f"""
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
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  COALESCE(%s::timestamptz, clock_timestamp()),
                  COALESCE(%s::timestamptz, clock_timestamp()),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s,
                  %s::uuid,
                  %s::uuid,
                  clock_timestamp(),
                  clock_timestamp()
                )
                -- Targetless form stays executable while migration tests
                -- run current code against pre-0083 schemas, where the
                -- commit-digest UNIQUE index does not exist yet. Once 0083
                -- is installed, that index still makes retries atomic.
                ON CONFLICT DO NOTHING
                RETURNING {MEMORY_COLUMNS}
                """,
        (
            memory.get("id"),
            memory.get("agent_profile_id", "assistant_default"),
            memory["memory_key"],
            _json_object(memory.get("value")),
            memory.get("status", "candidate"),
            _json_list(memory.get("source_event_ids")),
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
            memory.get("valid_from"),
            memory.get("valid_to"),
            memory.get("last_confirmed_at"),
            memory.get("title"),
            memory.get("canonical_text", ""),
            memory.get("summary"),
            memory.get("domain", "unknown"),
            memory.get("sensitivity", "unknown"),
            memory.get("first_seen_at"),
            memory.get("last_seen_at"),
            memory.get("last_reviewed_at"),
            _json_object(canonical_memory_metadata(memory)),
            memory.get("commit_digest"),
            memory.get("confirmation_id"),
            memory.get("project_id"),
            memory.get("created_by_agent_id"),
            memory.get("run_id"),
            memory.get("superseded_by"),
            memory.get("supersedes"),
        ),
    )
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
    except ContinuityStoreInvariantError:
        # ``create_memory`` uses INSERT ... ON CONFLICT DO NOTHING.  A
        # conflict is therefore a successful, transaction-safe no-op;
        # resolve only the exact tenant/profile/key identity so unrelated
        # uniqueness conflicts still fail closed.
        existing = self.get_memory_by_key(
            memory_key=memory_key,
            agent_profile_id=agent_profile_id,
        )
        if existing is None:
            raise
        return existing

def get_memory_for_update(self, memory_id: str) -> VNextRow | None:
    """Load and lock one memory for a review/lifecycle decision."""
    return self._fetch_optional_one(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE id = %s::uuid
                  AND deleted_at IS NULL
                FOR UPDATE
                """,
        (memory_id,),
    )

def get_memory_for_redaction(self, memory_id: str) -> VNextRow | None:
    """Lock a redaction target even after forget archived/tombstoned it."""

    return self._fetch_optional_one(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE id = %s::uuid
                FOR UPDATE
                """,
        (memory_id,),
    )

def lock_project_update_artifacts_for_redaction(self, memory_id: str) -> list[VNextRow]:
    """Lock every artifact coupled to a candidate memory in UUID order."""

    return self._fetch_all(
        f"""
                SELECT {ARTIFACT_COLUMNS}
                FROM generated_artifacts
                WHERE artifact_type = 'project_update'
                  AND metadata_json ->> 'candidate_memory_id' = %s
                ORDER BY id ASC
                FOR UPDATE
                """,
        (memory_id,),
    )

def memory_redaction_bundle_is_exact(self, memory_id: str, artifact_ids: Sequence[str]) -> bool:
    """Return whether every coupled mutable copy is already marker-shaped."""

    row = self._fetch_one(
        "check exact memory redaction bundle",
        """
                WITH input AS (
                  SELECT %s::uuid AS memory_id, %s::text[] AS artifact_ids
                )
                SELECT
                  EXISTS (
                    SELECT 1 FROM event_log AS event, input
                    WHERE event.event_type = 'memory.redacted'
                      AND event.target_type = 'memory'
                      AND event.target_id = input.memory_id::text
                  )
                  AND EXISTS (
                    SELECT 1 FROM memories AS memory, input
                    WHERE memory.id = input.memory_id
                      AND memory.embedding_vector IS NULL
                      AND memory.fact_keys IS NULL
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM memory_revisions AS revision, input
                    WHERE revision.memory_id = input.memory_id
                      AND (
                        revision.memory_key IS DISTINCT FROM
                          'redacted.' || revision.memory_id::text
                        OR revision.source_event_ids IS DISTINCT FROM '[]'::jsonb
                        OR revision.candidate IS DISTINCT FROM '{"redacted": true}'::jsonb
                        OR revision.text_after IS DISTINCT FROM '[REDACTED]'
                        OR revision.text_before IS DISTINCT FROM CASE
                          WHEN revision.text_before IS NULL THEN NULL ELSE '[REDACTED]'
                        END
                        OR revision.reason IS DISTINCT FROM CASE
                          WHEN revision.reason IS NULL THEN NULL ELSE '[REDACTED]'
                        END
                        OR revision.previous_value IS DISTINCT FROM CASE
                          WHEN revision.previous_value IS NULL
                            THEN NULL
                            ELSE '{"redacted": true}'::jsonb
                        END
                        OR revision.new_value IS DISTINCT FROM CASE
                          WHEN revision.new_value IS NULL
                            THEN NULL
                            ELSE '{"redacted": true}'::jsonb
                        END
                        OR revision.metadata_json IS DISTINCT FROM '{"redacted": true}'::jsonb
                      )
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM event_log AS event, input
                    WHERE (
                        (event.target_type = 'memory' AND event.target_id = input.memory_id::text)
                        OR (
                          event.target_type = 'artifact'
                          AND event.target_id = ANY(input.artifact_ids)
                        )
                        OR event.payload_artifact_id = ANY(input.artifact_ids)
                        OR event.payload_candidate_memory_id = input.memory_id::text
                        OR event.payload_memory_id = input.memory_id::text
                      )
                      AND (
                        event.payload_json IS DISTINCT FROM jsonb_build_object(
                          'redacted', true,
                          'memory_id', input.memory_id::text,
                          'event_type', event.event_type
                        )
                        OR event.integrity_hash IS NOT NULL
                      )
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM artifact_quality_ratings AS rating, input
                    WHERE rating.artifact_id::text = ANY(input.artifact_ids)
                      AND (
                        rating.missed_context IS DISTINCT FROM CASE
                          WHEN rating.missed_context IS NULL THEN NULL ELSE '[REDACTED]'
                        END
                        OR rating.comments IS DISTINCT FROM CASE
                          WHEN rating.comments IS NULL THEN NULL ELSE '[REDACTED]'
                        END
                        OR rating.metadata_json IS DISTINCT FROM '{"redacted": true}'::jsonb
                      )
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM provenance_links AS provenance, input
                    WHERE (
                        (
                          provenance.target_type = 'memory'
                          AND provenance.target_id = input.memory_id::text
                        )
                        OR (
                          provenance.target_type = 'artifact'
                          AND provenance.target_id = ANY(input.artifact_ids)
                        )
                      )
                      AND provenance.quote IS NOT NULL
                      AND provenance.quote IS DISTINCT FROM '[REDACTED]'
                  ) AS exact
                """,
        (memory_id, list(artifact_ids)),
    )
    return bool(row.get("exact"))

def lock_graph_mutation(self) -> None:
    """Serialize lifecycle graph/candidate mutation per user.

        A transaction-scoped advisory lock keyed on the current user so two
        concurrent supersessions cannot each pass an unlocked cycle check and
        together close a cycle. The same pre-row boundary also serializes
        consolidation candidate acceptance/invalidation against member
        correction, forgetting, and transitions. Released automatically at
        commit/rollback.
        """
    with self.conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext('vnext_supersession'), hashtext(app.current_user_id()::text))"
        )

def list_memory_ids_with_embeddings(self, ids: "Sequence[str]") -> set[str]:
    """Exact-ID embedding-presence read for a specific set of memory IDs.

        Consolidation and rollups must know which *selected* rows have stored
        vectors. A global ANN probe returns nearest neighbors, not a presence
        test, so selected rows can be missed when unrelated neighbors dominate.
        This reads presence directly by ID.
        """
    id_list = [str(value) for value in ids if str(value)]
    if not id_list:
        return set()
    rows = self._fetch_all(
        """
                SELECT id
                FROM memories
                WHERE id = ANY(%s::uuid[])
                  AND deleted_at IS NULL
                  AND embedding_vector IS NOT NULL
                """,
        (id_list,),
    )
    return {str(row["id"]) for row in rows}

def update_memory_fact_keys(self, *, memory_id: str, fact_keys: str | None) -> VNextRow | None:
    """Store derived retrieval keys; the generated ``search_tsv`` column
        (migration ``20260707_0082``) re-indexes them at 'D' weight.

        ``None`` resets the row to the "never derived" state the backfill
        pass scans for; ``""`` marks "derived, nothing to add". Mirrors
        ``update_memory_embedding``: a plain indexing write, no revision.
        """
    if fact_keys is not None and not isinstance(fact_keys, str):
        raise ContinuityStoreInvariantError("fact_keys must be a string or None")
    normalized = re.sub(r"\s+", " ", fact_keys).strip() if isinstance(fact_keys, str) else None
    return self._fetch_optional_one(
        """
                UPDATE memories
                SET fact_keys = %s
                WHERE id = %s::uuid
                  AND deleted_at IS NULL
                RETURNING id
                """,
        (normalized, memory_id),
    )

def list_memories_missing_fact_keys(self, *, limit: int = 100, after_id: str | None = None) -> list[VNextRow]:
    """Backfill pagination over rows whose fact_keys was never derived."""
    return self._fetch_all(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE deleted_at IS NULL
                  AND fact_keys IS NULL
                  AND (%s::uuid IS NULL OR id > %s::uuid)
                ORDER BY id ASC
                LIMIT %s
                """,
        (after_id, after_id, limit),
    )

def update_memory(self, *, memory_id: str, patch: JsonObject, actor_type: str = "system") -> VNextRow:
    refuse_updated_credential_activation(patch, lambda: self.get_memory(str(memory_id)))
    row = self._fetch_one(
        "update_memory",
        f"""
                UPDATE memories
                SET value = COALESCE(%s, value),
                    status = COALESCE(%s, status),
                    source_event_ids = COALESCE(%s, source_event_ids),
                    memory_type = COALESCE(%s, memory_type),
                    confidence = COALESCE(%s, confidence),
                    salience = COALESCE(%s, salience),
                    confirmation_status = COALESCE(%s, confirmation_status),
                    trust_class = COALESCE(%s, trust_class),
                    promotion_eligibility = COALESCE(%s, promotion_eligibility),
                    evidence_count = COALESCE(%s, evidence_count),
                    independent_source_count = COALESCE(%s, independent_source_count),
                    extracted_by_model = COALESCE(%s, extracted_by_model),
                    trust_reason = COALESCE(%s, trust_reason),
                    valid_from = COALESCE(%s, valid_from),
                    valid_to = COALESCE(%s, valid_to),
                    last_confirmed_at = COALESCE(%s, last_confirmed_at),
                    title = COALESCE(%s, title),
                    canonical_text = COALESCE(%s, canonical_text),
                    summary = COALESCE(%s, summary),
                    domain = COALESCE(%s, domain),
                    sensitivity = COALESCE(%s, sensitivity),
                    last_seen_at = COALESCE(%s, last_seen_at),
                    last_reviewed_at = COALESCE(%s, last_reviewed_at),
                    metadata_json = COALESCE(%s, metadata_json),
                    project_id = COALESCE(%s, project_id),
                    superseded_by = COALESCE(%s::uuid, superseded_by),
                    supersedes = COALESCE(%s::uuid, supersedes),
                    updated_at = clock_timestamp(),
                    deleted_at = CASE
                      WHEN %s = 'archived' THEN clock_timestamp()
                      ELSE deleted_at
                    END
                WHERE id = %s::uuid
                  AND deleted_at IS NULL
                RETURNING {MEMORY_COLUMNS}
                """,
        (
            _json_object(patch["value"]) if "value" in patch else None,
            patch.get("status"),
            _json_list(patch["source_event_ids"]) if "source_event_ids" in patch else None,
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
            patch.get("valid_from"),
            patch.get("valid_to"),
            patch.get("last_confirmed_at"),
            patch.get("title"),
            patch.get("canonical_text"),
            patch.get("summary"),
            patch.get("domain"),
            patch.get("sensitivity"),
            patch.get("last_seen_at"),
            patch.get("last_reviewed_at"),
            _json_object(patch["metadata_json"]) if "metadata_json" in patch else None,
            patch.get("project_id"),
            patch.get("superseded_by"),
            patch.get("supersedes"),
            patch.get("status"),
            memory_id,
        ),
    )
    self._append_mutation_event(
        event_type="memory.updated",
        actor_type=actor_type,
        target_type="memory",
        target_id=row["id"],
        payload={"operation": "update", "changes": patch},
    )
    return row

@contextmanager
def _redaction_mode(self) -> Iterator[None]:
    """Set/reset the privileged redaction session flag around a block."""
    with self.conn.cursor() as cur:
        cur.execute("SELECT set_config('app.redaction_in_progress', 'on', false)")
    try:
        yield
    finally:
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT set_config('app.redaction_in_progress', 'off', false)")
        except Exception:
            # The failing statement aborted the transaction, so the
            # reset statement cannot run -- but the rollback that
            # follows discards the session-scoped flag with it
            # (set_config assignments are transactional).
            pass

def redact_memory_bundle(
    self,
    *,
    memory_id: str,
    project_update_artifacts: Sequence[Mapping[str, object]],
    actor_type: str = "user",
) -> VNextRow:
    """Atomically scrub a memory and every persisted coupled copy.

        The caller must acquire the user graph lock, the memory lock (including
        deleted rows), and the project-update artifact locks in deterministic
        id order before entering this method.  Every UPDATE is marker-shaped
        and runs under the narrowly-scoped database redaction flag.  A single
        content-free receipt is appended only when at least one stored value
        changed, making a replay a byte-preserving no-op.
        """

    current = self._fetch_optional_one(
        f"""
                SELECT {MEMORY_COLUMNS},
                       embedding_vector IS NULL AS _redaction_embedding_cleared,
                       fact_keys IS NULL AS _redaction_fact_keys_cleared
                FROM memories
                WHERE id = %s::uuid
            """,
        (memory_id,),
    )
    if current is None:
        raise ContinuityStoreInvariantError("redact_memory_bundle did not find the memory to redact")

    current_metadata = current.get("metadata_json")
    prior_receipt = self._fetch_optional_one(
        """
                SELECT id
                FROM event_log
                WHERE event_type = 'memory.redacted'
                  AND target_type = 'memory'
                  AND target_id = %s
                ORDER BY occurred_at ASC, id ASC
                LIMIT 1
            """,
        (memory_id,),
    )
    prior_redacted_at = ""
    if is_prior_redacted_memory_marker(current) and prior_receipt is not None:
        assert isinstance(current_metadata, Mapping)
        prior_redacted_at = str(current_metadata.get("redacted_at") or "").strip()
    redacted_at = prior_redacted_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    memory_metadata = redacted_memory_metadata(current_metadata, redacted_at=redacted_at)
    redacted_memory_key = f"redacted.{memory_id}"

    artifacts = sorted(project_update_artifacts, key=lambda artifact: str(artifact.get("id") or ""))
    artifact_ids: list[str] = []
    artifact_metadata_by_id: dict[str, JsonObject] = {}
    for artifact in artifacts:
        artifact_id = str(artifact.get("id") or "").strip()
        metadata_value = artifact.get("metadata_json")
        metadata = dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
        project_id = str(metadata.get("project_id") or "").strip()
        candidate_memory_id = str(metadata.get("candidate_memory_id") or "").strip()
        workflow = str(metadata.get("workflow") or "").strip()
        project_scope = metadata.get("project_scope")
        action = str(metadata.get("review_action") or "").strip()
        status = str(artifact.get("status") or "").strip()
        valid_action = (status == "accepted" and action in {"accept", "edit"}) or (
            status == "rejected" and action == "reject"
        )
        if (
            not artifact_id
            or artifact.get("artifact_type") != "project_update"
            or candidate_memory_id != memory_id
            or not project_id
            or workflow != "project_auto_update"
            or project_scope != [project_id]
            or not valid_action
        ):
            raise ContinuityStoreInvariantError("project-update redaction requires exact terminal artifact linkage")
        artifact_ids.append(artifact_id)
        artifact_metadata_by_id[artifact_id] = {
            "redacted": True,
            "redacted_at": redacted_at,
            "workflow": "project_auto_update",
            "project_id": project_id,
            "project_scope": [project_id],
            "candidate_memory_id": memory_id,
            "review_action": action,
        }

    changed_artifact_ids: list[str] = []
    redacted_quality_ratings = 0
    redacted_provenance_links = 0
    redacted_revisions = 0
    redacted_events = 0
    memory_changed = False

    with self._redaction_mode():
        # Scrub artifacts first while their original structural metadata is
        # still available to the 0092 marker-shape trigger.
        for artifact_id in artifact_ids:
            desired_metadata = artifact_metadata_by_id[artifact_id]
            changed = self._fetch_optional_one(
                f"""
                    UPDATE generated_artifacts
                    SET title = %s,
                        content_markdown = %s,
                        prompt_hash = NULL,
                        model_info_json = %s,
                        metadata_json = %s
                    WHERE id = %s::uuid
                      AND (
                        title IS DISTINCT FROM %s
                        OR content_markdown IS DISTINCT FROM %s
                        OR prompt_hash IS NOT NULL
                        OR model_info_json IS DISTINCT FROM %s::jsonb
                        OR metadata_json IS DISTINCT FROM %s::jsonb
                      )
                    RETURNING {ARTIFACT_COLUMNS}
                    """,
                (
                    REDACTION_MARKER,
                    REDACTION_MARKER,
                    _json_object(REDACTED_JSON_VALUE),
                    _json_object(desired_metadata),
                    artifact_id,
                    REDACTION_MARKER,
                    REDACTION_MARKER,
                    _json_object(REDACTED_JSON_VALUE),
                    _json_object(desired_metadata),
                ),
            )
            if changed is not None:
                changed_artifact_ids.append(artifact_id)

        if artifact_ids:
            ratings = self._fetch_all(
                """
                    UPDATE artifact_quality_ratings
                    SET missed_context = CASE
                          WHEN missed_context IS NULL THEN NULL ELSE %s
                        END,
                        comments = CASE WHEN comments IS NULL THEN NULL ELSE %s END,
                        metadata_json = %s
                    WHERE artifact_id = ANY(%s::uuid[])
                      AND (
                        (missed_context IS NOT NULL AND missed_context IS DISTINCT FROM %s)
                        OR (comments IS NOT NULL AND comments IS DISTINCT FROM %s)
                        OR metadata_json IS DISTINCT FROM %s::jsonb
                      )
                    RETURNING id
                    """,
                (
                    REDACTION_MARKER,
                    REDACTION_MARKER,
                    _json_object(REDACTED_JSON_VALUE),
                    artifact_ids,
                    REDACTION_MARKER,
                    REDACTION_MARKER,
                    _json_object(REDACTED_JSON_VALUE),
                ),
            )
            redacted_quality_ratings = len(ratings)

        provenance_target_ids = [memory_id, *artifact_ids]
        provenance = self._fetch_all(
            """
                UPDATE provenance_links
                SET quote = %s
                WHERE quote IS NOT NULL
                  AND quote IS DISTINCT FROM %s
                  AND (
                    (target_type = 'memory' AND target_id = %s)
                    OR (target_type = 'artifact' AND target_id = ANY(%s::text[]))
                  )
                RETURNING id
                """,
            (REDACTION_MARKER, REDACTION_MARKER, memory_id, artifact_ids),
        )
        redacted_provenance_links = len(provenance)

        revisions = self._fetch_all(
            """
                UPDATE memory_revisions
                SET memory_key = %s,
                    previous_value = CASE WHEN previous_value IS NULL THEN NULL ELSE %s END,
                    new_value = CASE WHEN new_value IS NULL THEN NULL ELSE %s END,
                    source_event_ids = '[]'::jsonb,
                    candidate = %s,
                    text_before = CASE WHEN text_before IS NULL THEN NULL ELSE %s END,
                    text_after = %s,
                    reason = CASE WHEN reason IS NULL THEN NULL ELSE %s END,
                    metadata_json = %s
                WHERE memory_id = %s::uuid
                  AND (
                    memory_key IS DISTINCT FROM %s
                    OR previous_value IS DISTINCT FROM
                      CASE WHEN previous_value IS NULL THEN NULL ELSE %s::jsonb END
                    OR new_value IS DISTINCT FROM
                      CASE WHEN new_value IS NULL THEN NULL ELSE %s::jsonb END
                    OR source_event_ids IS DISTINCT FROM '[]'::jsonb
                    OR candidate IS DISTINCT FROM %s::jsonb
                    OR text_before IS DISTINCT FROM
                      CASE WHEN text_before IS NULL THEN NULL ELSE %s END
                    OR text_after IS DISTINCT FROM %s
                    OR reason IS DISTINCT FROM CASE WHEN reason IS NULL THEN NULL ELSE %s END
                    OR metadata_json IS DISTINCT FROM %s::jsonb
                  )
                RETURNING id
                """,
            (
                redacted_memory_key,
                _json_object(REDACTED_JSON_VALUE),
                _json_object(REDACTED_JSON_VALUE),
                _json_object(REDACTED_JSON_VALUE),
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                _json_object(REDACTED_JSON_VALUE),
                memory_id,
                redacted_memory_key,
                _json_object(REDACTED_JSON_VALUE),
                _json_object(REDACTED_JSON_VALUE),
                _json_object(REDACTED_JSON_VALUE),
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                _json_object(REDACTED_JSON_VALUE),
            ),
        )
        redacted_revisions = len(revisions)

        # Exact 0091 linkage columns and exact targets avoid the old
        # payload_json::text sweep, which could redact an unrelated event
        # that merely happened to mention the UUID in prose.
        events = self._fetch_all(
            """
                UPDATE event_log
                SET payload_json = jsonb_build_object(
                      'redacted', true,
                      'memory_id', %s::text,
                      'event_type', event_type
                    ),
                    integrity_hash = NULL
                WHERE (
                    (target_type = 'memory' AND target_id = %s)
                    OR (target_type = 'artifact' AND target_id = ANY(%s::text[]))
                    OR payload_artifact_id = ANY(%s::text[])
                    OR payload_candidate_memory_id = %s
                    OR payload_memory_id = %s
                  )
                  AND (
                    payload_json IS DISTINCT FROM jsonb_build_object(
                      'redacted', true,
                      'memory_id', %s::text,
                      'event_type', event_type
                    )
                    OR integrity_hash IS NOT NULL
                  )
                RETURNING id
                """,
            (
                memory_id,
                memory_id,
                artifact_ids,
                artifact_ids,
                memory_id,
                memory_id,
                memory_id,
            ),
        )
        redacted_events = len(events)

        redacted_memory = self._fetch_optional_one(
            f"""
                UPDATE memories
                SET memory_key = %s,
                    title = CASE WHEN title IS NULL THEN NULL ELSE %s END,
                    canonical_text = %s,
                    summary = CASE WHEN summary IS NULL THEN NULL ELSE %s END,
                    trust_reason = CASE WHEN trust_reason IS NULL THEN NULL ELSE %s END,
                    value = %s,
                    source_event_ids = '[]'::jsonb,
                    metadata_json = %s,
                    commit_digest = NULL,
                    confirmation_id = NULL,
                    embedding_vector = NULL,
                    fact_keys = NULL,
                    status = 'archived',
                    deleted_at = COALESCE(deleted_at, clock_timestamp()),
                    updated_at = clock_timestamp()
                WHERE id = %s::uuid
                  AND (
                    memory_key IS DISTINCT FROM %s
                    OR title IS DISTINCT FROM CASE WHEN title IS NULL THEN NULL ELSE %s END
                    OR canonical_text IS DISTINCT FROM %s
                    OR summary IS DISTINCT FROM CASE WHEN summary IS NULL THEN NULL ELSE %s END
                    OR trust_reason IS DISTINCT FROM CASE WHEN trust_reason IS NULL THEN NULL ELSE %s END
                    OR value IS DISTINCT FROM %s::jsonb
                    OR source_event_ids IS DISTINCT FROM '[]'::jsonb
                    OR metadata_json IS DISTINCT FROM %s::jsonb
                    OR commit_digest IS NOT NULL
                    OR confirmation_id IS NOT NULL
                    OR embedding_vector IS NOT NULL
                    OR fact_keys IS NOT NULL
                    OR status IS DISTINCT FROM 'archived'
                    OR deleted_at IS NULL
                  )
                RETURNING {MEMORY_COLUMNS}
                """,
            (
                redacted_memory_key,
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                _json_object(REDACTED_JSON_VALUE),
                _json_object(memory_metadata),
                memory_id,
                redacted_memory_key,
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                _json_object(REDACTED_JSON_VALUE),
                _json_object(memory_metadata),
            ),
        )
        memory_changed = redacted_memory is not None

    if redacted_memory is None:
        redacted_memory = self._fetch_optional_one(
            f"SELECT {MEMORY_COLUMNS} FROM memories WHERE id = %s::uuid",
            (memory_id,),
        )
    if redacted_memory is None:  # pragma: no cover - locked row cannot disappear
        raise ContinuityStoreInvariantError("redact_memory_bundle lost its locked memory")

    bundle_changed = bool(
        memory_changed
        or changed_artifact_ids
        or redacted_quality_ratings
        or redacted_provenance_links
        or redacted_revisions
        or redacted_events
    )
    if bundle_changed:
        receipt = build_event_log_record(
            event_type="memory.redacted",
            actor_type=actor_type,
            target_type="memory",
            target_id=memory_id,
            payload={
                "redacted": True,
                "memory_id": memory_id,
                "event_type": "memory.redacted",
            },
        )
        # This receipt is already an exact content-free skeleton; no
        # content-derived integrity hash needs to survive or be scrubbed on
        # replay.
        receipt["integrity_hash"] = None
        self.append_event(receipt)

    return {
        "memory": redacted_memory,
        "redacted_revisions": redacted_revisions,
        "redacted_events": redacted_events,
        "redacted_artifacts": len(changed_artifact_ids),
        "redacted_artifact_ids": changed_artifact_ids,
        "redacted_quality_ratings": redacted_quality_ratings,
        "redacted_provenance_links": redacted_provenance_links,
        "idempotent_replay": not bundle_changed,
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
    current = self._fetch_optional_one(
        """
                SELECT metadata_json
                FROM memories
                WHERE id = %s::uuid
                """,
        (memory_id,),
    )
    if current is None:
        raise ContinuityStoreInvariantError(
            "redact_memory_content did not find the memory to redact",
        )
    redacted_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    scrubbed = redacted_memory_metadata(current.get("metadata_json"), redacted_at=redacted_at)
    with self._redaction_mode():
        row = self._fetch_one(
            "redact_memory_content",
            f"""
                    UPDATE memories
                    SET memory_key = 'redacted.' || id::text,
                        title = CASE WHEN title IS NULL THEN NULL ELSE %s END,
                        canonical_text = %s,
                        summary = CASE WHEN summary IS NULL THEN NULL ELSE %s END,
                        trust_reason = CASE WHEN trust_reason IS NULL THEN NULL ELSE %s END,
                        value = %s,
                        source_event_ids = '[]'::jsonb,
                        metadata_json = %s,
                        commit_digest = NULL,
                        confirmation_id = NULL,
                        embedding_vector = NULL,
                        fact_keys = NULL,
                        status = 'archived',
                        deleted_at = COALESCE(deleted_at, clock_timestamp()),
                        updated_at = clock_timestamp()
                    WHERE id = %s::uuid
                    RETURNING {MEMORY_COLUMNS}
                    """,
            (
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                _json_object(REDACTED_JSON_VALUE),
                _json_object(scrubbed),
                memory_id,
            ),
        )
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
    with self._redaction_mode():
        redacted = self._fetch_all(
            """
                    UPDATE memory_revisions
                    SET memory_key = 'redacted.' || memory_id::text,
                        previous_value = CASE WHEN previous_value IS NULL THEN NULL ELSE %s END,
                        new_value = CASE WHEN new_value IS NULL THEN NULL ELSE %s END,
                        source_event_ids = '[]'::jsonb,
                        candidate = %s,
                        text_before = CASE WHEN text_before IS NULL THEN NULL ELSE %s END,
                        text_after = %s,
                        reason = CASE WHEN reason IS NULL THEN NULL ELSE %s END,
                        metadata_json = %s
                    WHERE memory_id = %s::uuid
                    RETURNING id
                    """,
            (
                _json_object(REDACTED_JSON_VALUE),
                _json_object(REDACTED_JSON_VALUE),
                _json_object(REDACTED_JSON_VALUE),
                REDACTION_MARKER,
                REDACTION_MARKER,
                REDACTION_MARKER,
                _json_object(REDACTED_JSON_VALUE),
                memory_id,
            ),
        )
    self._append_mutation_event(
        event_type="memory.redacted",
        actor_type=actor_type,
        target_type="memory",
        target_id=memory_id,
        payload={"operation": "redact_memory_revisions", "redacted_revisions": len(redacted)},
    )
    return {"memory_id": memory_id, "redacted_revisions": len(redacted)}

def redact_memory_events(self, *, memory_id: str, actor_type: str = "user") -> VNextRow:
    """Expunge event payloads that reference a memory.

        Matching rows keep event_type, actor columns, target columns,
        occurred_at, and trace/run references; payload_json becomes
        {"redacted": true, "memory_id": ..., "event_type": <own column>}
        and integrity_hash is cleared (it derives from the payload, so
        keeping it would allow confirming guesses of redacted content).
        """
    with self._redaction_mode():
        redacted = self._fetch_all(
            """
                    UPDATE event_log
                    SET payload_json = jsonb_build_object(
                          'redacted', true,
                          'memory_id', %s::text,
                          'event_type', event_type
                        ),
                        integrity_hash = NULL
                    WHERE (target_type = 'memory' AND target_id = %s)
                       OR payload_candidate_memory_id = %s
                       OR payload_memory_id = %s
                    RETURNING id
                    """,
            (memory_id, memory_id, memory_id, memory_id),
        )
    self._append_mutation_event(
        event_type="memory.redacted",
        actor_type=actor_type,
        target_type="memory",
        target_id=memory_id,
        payload={"operation": "redact_memory_events", "redacted_events": len(redacted)},
    )
    return {"memory_id": memory_id, "redacted_events": len(redacted)}

def create_provenance_link(self, link: JsonObject, *, actor_type: str = "system") -> VNextRow:
    if link.get("quote") is not None:
        target_type = str(link.get("target_type") or "")
        target_id = str(link.get("target_id") or "")
        if target_type == "artifact":
            artifact = self.get_artifact_for_update(target_id)
            if artifact is not None and is_redacted_project_update_artifact(artifact):
                raise ValueError("quoted provenance cannot be added to a redacted target")
        elif target_type == "memory":
            memory = self.get_memory_for_redaction(target_id)
            if memory is not None and is_redacted_memory(memory):
                raise ValueError("quoted provenance cannot be added to a redacted target")
    row = self._fetch_one(
        "create_provenance_link",
        f"""
                INSERT INTO provenance_links (
                  id,
                  user_id,
                  target_type,
                  target_id,
                  source_id,
                  source_chunk_id,
                  quote,
                  evidence_role,
                  confidence
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s,
                  %s,
                  %s::uuid,
                  %s::uuid,
                  %s,
                  %s,
                  %s
                )
                RETURNING {PROVENANCE_COLUMNS}
                """,
        (
            link.get("id"),
            link["target_type"],
            link["target_id"],
            link.get("source_id"),
            link.get("source_chunk_id"),
            link.get("quote"),
            link.get("evidence_role", "supports"),
            link.get("confidence", 0.5),
        ),
    )
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
                SELECT {PROVENANCE_COLUMNS}
                FROM provenance_links
                WHERE target_type = %s
                  AND target_id = %s
                ORDER BY created_at DESC, id DESC
                """,
        (target_type, target_id),
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
    return self._fetch_all(
        f"""
                SELECT {PROVENANCE_COLUMNS}
                FROM provenance_links
                WHERE target_type = %s
                  AND target_id = ANY(%s::text[])
                ORDER BY created_at DESC, id DESC
                """,
        (target_type, ids),
    )


for _method in (
    create_memory,
    upsert_memory_by_key,
    get_memory_for_update,
    get_memory_for_redaction,
    lock_project_update_artifacts_for_redaction,
    memory_redaction_bundle_is_exact,
    lock_graph_mutation,
    list_memory_ids_with_embeddings,
    update_memory_fact_keys,
    list_memories_missing_fact_keys,
    update_memory,
    _redaction_mode,
    redact_memory_bundle,
    redact_memory_content,
    redact_memory_revisions,
    redact_memory_events,
    create_provenance_link,
    list_provenance_links,
    list_provenance_links_for_targets,
):
    _method.__module__ = "alicebot_api.vnext_store"
    _method.__qualname__ = f"PostgresVNextStore.{_method.__name__}"
del _method

_redaction_generator = getattr(_redaction_mode, "__wrapped__")
_redaction_generator.__module__ = "alicebot_api.vnext_store"
_redaction_generator.__qualname__ = "PostgresVNextStore._redaction_mode"
del _redaction_generator
