"""Make project-update memory redaction cover every persisted coupled copy.

Revision ID: 20260716_0092
Revises: 20260715_0091

True redaction remains an exceptional, marker-shaped UPDATE path.  This
migration extends that path to terminal project-update artifacts, their
quality ratings, and provenance quotes.  It also tightens the existing event
and revision exceptions so only the exact content-free skeleton emitted by
the store is admissible.  Ordinary mutation remains unchanged and RLS still
binds every affected row to ``app.current_user_id()``.

A database already past this revision never runs this body again. A database
still before it must end in exactly the state the superuser path gives: the
same redaction backfill, whether the migrator is a cluster superuser or the
documented table owner that is NOSUPERUSER NOBYPASSRLS. The backfill reads
and writes under forced row-level security whose policies the owner cannot
satisfy unbound, so the owner bypass is used inside this migration's own
transaction, as 0067, 0070, 0093 and 0095 to 0097 do, and every forced
setting is restored before the transaction commits.
"""

from __future__ import annotations

from alembic import op


revision = "20260716_0092"
down_revision = "20260715_0091"
branch_labels = None
depends_on = None

REDACTION_MARKER = "[REDACTED]"


_UPGRADE_STATEMENTS: tuple[str, ...] = (
    # Exact event skeleton.  The three retained payload fields are structural
    # and are sufficient to bind project-update replay to the immutable row.
    f"""
    CREATE OR REPLACE FUNCTION app.reject_event_log_mutation()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      IF TG_OP = 'UPDATE'
         AND current_setting('app.redaction_in_progress', true) = 'on'
         AND OLD.id IS NOT DISTINCT FROM NEW.id
         AND OLD.user_id IS NOT DISTINCT FROM NEW.user_id
         AND OLD.event_type IS NOT DISTINCT FROM NEW.event_type
         AND OLD.actor_type IS NOT DISTINCT FROM NEW.actor_type
         AND OLD.actor_id IS NOT DISTINCT FROM NEW.actor_id
         AND OLD.target_type IS NOT DISTINCT FROM NEW.target_type
         AND OLD.target_id IS NOT DISTINCT FROM NEW.target_id
         AND OLD.occurred_at IS NOT DISTINCT FROM NEW.occurred_at
         AND OLD.trace_id IS NOT DISTINCT FROM NEW.trace_id
         AND OLD.run_id IS NOT DISTINCT FROM NEW.run_id
         AND NEW.integrity_hash IS NULL
         AND NEW.payload_json = jsonb_build_object(
               'redacted', true,
               'memory_id', NEW.payload_json ->> 'memory_id',
               'event_type', NEW.event_type
             )
         AND NULLIF(btrim(NEW.payload_json ->> 'memory_id'), '') IS NOT NULL
         -- The marker must retain the memory identity already proved by every
         -- supported immutable linkage on the old event.  A nonblank but
         -- fabricated UUID is not an authorized redaction skeleton.
         AND (
           OLD.target_type IS DISTINCT FROM 'memory'
           OR OLD.target_id = NEW.payload_json ->> 'memory_id'
         )
         AND (
           OLD.payload_memory_id IS NULL
           OR OLD.payload_memory_id = NEW.payload_json ->> 'memory_id'
         )
         AND (
           OLD.payload_candidate_memory_id IS NULL
           OR OLD.payload_candidate_memory_id = NEW.payload_json ->> 'memory_id'
         )
         AND (
           OLD.target_type IS DISTINCT FROM 'artifact'
           OR EXISTS (
             SELECT 1
             FROM generated_artifacts AS artifact
             WHERE artifact.id::text = OLD.target_id
               AND artifact.user_id = OLD.user_id
               AND artifact.artifact_type = 'project_update'
               AND artifact.metadata_json ->> 'candidate_memory_id' =
                   NEW.payload_json ->> 'memory_id'
           )
         )
         AND (
           OLD.payload_artifact_id IS NULL
           OR EXISTS (
             SELECT 1
             FROM generated_artifacts AS artifact
             WHERE artifact.id::text = OLD.payload_artifact_id
               AND artifact.user_id = OLD.user_id
               AND artifact.artifact_type = 'project_update'
               AND artifact.metadata_json ->> 'candidate_memory_id' =
                   NEW.payload_json ->> 'memory_id'
           )
         )
         AND (
           OLD.target_type IN ('memory', 'artifact')
           OR OLD.payload_memory_id IS NOT NULL
           OR OLD.payload_candidate_memory_id IS NOT NULL
           OR OLD.payload_artifact_id IS NOT NULL
         )
      THEN
        RETURN NEW;
      END IF;
      RAISE EXCEPTION 'event_log is append-only';
    END;
    $$;
    """,
    # Redaction now clears content-derived source-event references and replaces
    # the content-derived memory key with a deterministic non-secret key.
    f"""
    CREATE OR REPLACE FUNCTION app.reject_memory_revision_mutation()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      IF TG_OP = 'UPDATE'
         AND current_setting('app.redaction_in_progress', true) = 'on'
         AND OLD.id IS NOT DISTINCT FROM NEW.id
         AND OLD.user_id IS NOT DISTINCT FROM NEW.user_id
         AND OLD.memory_id IS NOT DISTINCT FROM NEW.memory_id
         AND OLD.sequence_no IS NOT DISTINCT FROM NEW.sequence_no
         AND OLD.action IS NOT DISTINCT FROM NEW.action
         AND NEW.memory_key = 'redacted.' || NEW.memory_id::text
         AND NEW.source_event_ids = '[]'::jsonb
         AND OLD.revision_number IS NOT DISTINCT FROM NEW.revision_number
         AND OLD.revision_type IS NOT DISTINCT FROM NEW.revision_type
         AND OLD.actor_type IS NOT DISTINCT FROM NEW.actor_type
         AND OLD.actor_id IS NOT DISTINCT FROM NEW.actor_id
         AND OLD.created_at IS NOT DISTINCT FROM NEW.created_at
         AND NEW.text_after = '{REDACTION_MARKER}'
         AND (
           (OLD.text_before IS NULL AND NEW.text_before IS NULL)
           OR (OLD.text_before IS NOT NULL AND NEW.text_before = '{REDACTION_MARKER}')
         )
         AND (
           (OLD.reason IS NULL AND NEW.reason IS NULL)
           OR (OLD.reason IS NOT NULL AND NEW.reason = '{REDACTION_MARKER}')
         )
         AND (
           (OLD.previous_value IS NULL AND NEW.previous_value IS NULL)
           OR (
             OLD.previous_value IS NOT NULL
             AND NEW.previous_value = '{{"redacted": true}}'::jsonb
           )
         )
         AND (
           (OLD.new_value IS NULL AND NEW.new_value IS NULL)
           OR (OLD.new_value IS NOT NULL AND NEW.new_value = '{{"redacted": true}}'::jsonb)
         )
         AND NEW.candidate = '{{"redacted": true}}'::jsonb
         AND NEW.metadata_json = '{{"redacted": true}}'::jsonb
      THEN
        RETURN NEW;
      END IF;
      RAISE EXCEPTION 'memory revisions are append-only';
    END;
    $$;
    """,
    # One canonical database predicate mirrors
    # is_redacted_project_update_artifact().  Every trigger that needs to
    # classify an artifact calls this function so INSERT, UPDATE, provenance,
    # and rating guards cannot drift to broader or narrower marker shapes.
    """
    CREATE OR REPLACE FUNCTION app.is_redacted_project_update_artifact(
      artifact_type_value text,
      status_value text,
      title_value text,
      content_markdown_value text,
      prompt_hash_value text,
      model_info_json_value jsonb,
      metadata_json_value jsonb
    )
    RETURNS boolean
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
    AS $$
      SELECT COALESCE(
        artifact_type_value = 'project_update'
        AND status_value IN ('accepted', 'rejected')
        AND title_value = '[REDACTED]'
        AND content_markdown_value = '[REDACTED]'
        AND prompt_hash_value IS NULL
        AND model_info_json_value = '{"redacted": true}'::jsonb
        AND metadata_json_value = jsonb_build_object(
              'redacted', true,
              'redacted_at', metadata_json_value ->> 'redacted_at',
              'workflow', 'project_auto_update',
              'project_id', metadata_json_value ->> 'project_id',
              'project_scope', jsonb_build_array(metadata_json_value ->> 'project_id'),
              'candidate_memory_id', metadata_json_value ->> 'candidate_memory_id',
              'review_action', metadata_json_value ->> 'review_action'
            )
        AND NULLIF(btrim(metadata_json_value ->> 'redacted_at'), '') IS NOT NULL
        AND NULLIF(btrim(metadata_json_value ->> 'project_id'), '') IS NOT NULL
        AND NULLIF(btrim(metadata_json_value ->> 'candidate_memory_id'), '') IS NOT NULL
        AND (
          (
            status_value = 'accepted'
            AND metadata_json_value ->> 'review_action' IN ('accept', 'edit')
          )
          OR (
            status_value = 'rejected'
            AND metadata_json_value ->> 'review_action' = 'reject'
          )
        ),
        false
      )
    $$;
    """,
    # Terminal project-update artifacts may be scrubbed exactly once.  Once a
    # row has the marker shape, every later UPDATE is rejected, even if a
    # caller manages to set the session flag.
    f"""
    CREATE OR REPLACE FUNCTION app.guard_generated_artifact_redaction()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    DECLARE
      new_is_redacted boolean;
      old_is_redacted boolean := false;
    BEGIN
      new_is_redacted := app.is_redacted_project_update_artifact(
        NEW.artifact_type,
        NEW.status,
        NEW.title,
        NEW.content_markdown,
        NEW.prompt_hash,
        NEW.model_info_json,
        NEW.metadata_json
      );

      IF TG_OP = 'INSERT' THEN
        IF new_is_redacted THEN
          RAISE EXCEPTION 'redacted project-update artifacts cannot be inserted';
        END IF;
        RETURN NEW;
      END IF;

      old_is_redacted := app.is_redacted_project_update_artifact(
        OLD.artifact_type,
        OLD.status,
        OLD.title,
        OLD.content_markdown,
        OLD.prompt_hash,
        OLD.model_info_json,
        OLD.metadata_json
      );

      IF old_is_redacted THEN
        RAISE EXCEPTION 'redacted artifacts are immutable';
      END IF;

      IF new_is_redacted
         AND current_setting('app.redaction_in_progress', true) IS DISTINCT FROM 'on'
      THEN
        RAISE EXCEPTION 'project-update artifact redaction requires authorized redaction mode';
      END IF;

      IF current_setting('app.redaction_in_progress', true) = 'on' THEN
        IF OLD.id IS NOT DISTINCT FROM NEW.id
         AND OLD.user_id IS NOT DISTINCT FROM NEW.user_id
         AND OLD.artifact_type IS NOT DISTINCT FROM NEW.artifact_type
         AND OLD.status IS NOT DISTINCT FROM NEW.status
         AND OLD.domain IS NOT DISTINCT FROM NEW.domain
         AND OLD.sensitivity IS NOT DISTINCT FROM NEW.sensitivity
         AND OLD.generated_by IS NOT DISTINCT FROM NEW.generated_by
         AND OLD.created_at IS NOT DISTINCT FROM NEW.created_at
         AND OLD.reviewed_at IS NOT DISTINCT FROM NEW.reviewed_at
         AND OLD.promoted_at IS NOT DISTINCT FROM NEW.promoted_at
         AND OLD.artifact_type = 'project_update'
         AND OLD.status IN ('accepted', 'rejected')
         AND new_is_redacted
         AND NEW.metadata_json = jsonb_build_object(
               'redacted', true,
               'redacted_at', NEW.metadata_json ->> 'redacted_at',
               'workflow', 'project_auto_update',
               'project_id', OLD.metadata_json ->> 'project_id',
               'project_scope', jsonb_build_array(OLD.metadata_json ->> 'project_id'),
               'candidate_memory_id', OLD.metadata_json ->> 'candidate_memory_id',
               'review_action', OLD.metadata_json ->> 'review_action'
             )
         AND NULLIF(btrim(NEW.metadata_json ->> 'redacted_at'), '') IS NOT NULL
         AND NULLIF(btrim(OLD.metadata_json ->> 'project_id'), '') IS NOT NULL
         AND NULLIF(btrim(OLD.metadata_json ->> 'candidate_memory_id'), '') IS NOT NULL
         AND OLD.metadata_json ->> 'workflow' = 'project_auto_update'
         AND OLD.metadata_json -> 'project_scope' =
             jsonb_build_array(OLD.metadata_json ->> 'project_id')
         AND (
           (OLD.status = 'accepted' AND OLD.metadata_json ->> 'review_action' IN ('accept', 'edit'))
           OR (OLD.status = 'rejected' AND OLD.metadata_json ->> 'review_action' = 'reject')
         )
        THEN
          RETURN NEW;
        END IF;
        RAISE EXCEPTION 'invalid project-update artifact redaction shape';
      END IF;

      RETURN NEW;
    END;
    $$;
    """,
    """
    CREATE TRIGGER generated_artifacts_redaction_guard
    BEFORE INSERT OR UPDATE ON generated_artifacts
    FOR EACH ROW
    EXECUTE FUNCTION app.guard_generated_artifact_redaction();
    """,
    # Provenance keeps identifiers, evidence role, confidence and timestamp;
    # only the quoted content is destroyed.
    """
    CREATE OR REPLACE FUNCTION app.guard_provenance_link_redaction()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    DECLARE
      target_is_redacted boolean := false;
    BEGIN
      IF TG_OP = 'INSERT' THEN
        IF NEW.quote IS NOT NULL AND NEW.target_type = 'artifact' THEN
          SELECT app.is_redacted_project_update_artifact(
            artifact.artifact_type,
            artifact.status,
            artifact.title,
            artifact.content_markdown,
            artifact.prompt_hash,
            artifact.model_info_json,
            artifact.metadata_json
          )
          INTO target_is_redacted
          FROM generated_artifacts AS artifact
          WHERE artifact.id::text = NEW.target_id
            AND artifact.user_id = NEW.user_id
          FOR SHARE;
        ELSIF NEW.quote IS NOT NULL AND NEW.target_type = 'memory' THEN
          SELECT (
            memory.memory_key = 'redacted.' || memory.id::text
            AND memory.canonical_text = '[REDACTED]'
            AND (memory.title IS NULL OR memory.title = '[REDACTED]')
            AND (memory.summary IS NULL OR memory.summary = '[REDACTED]')
            AND (memory.trust_reason IS NULL OR memory.trust_reason = '[REDACTED]')
            AND memory.value = '{"redacted": true}'::jsonb
            AND memory.source_event_ids = '[]'::jsonb
            AND memory.commit_digest IS NULL
            AND memory.confirmation_id IS NULL
            AND memory.embedding_vector IS NULL
            AND memory.fact_keys IS NULL
            AND memory.status = 'archived'
            AND memory.deleted_at IS NOT NULL
            AND memory.metadata_json -> 'redacted' = 'true'::jsonb
            AND NULLIF(btrim(memory.metadata_json ->> 'redacted_at'), '') IS NOT NULL
            AND memory.metadata_json - ARRAY[
                  'project_id', 'project_scope', 'superseded_by', 'supersedes',
                  'run_id', 'agent_id', 'created_by_agent_id', 'redacted', 'redacted_at'
                ]::text[] = '{}'::jsonb
          )
          INTO target_is_redacted
          FROM memories AS memory
          WHERE memory.id::text = NEW.target_id
            AND memory.user_id = NEW.user_id
          FOR SHARE;
        END IF;
        IF target_is_redacted THEN
          RAISE EXCEPTION 'quoted provenance cannot be added to a redacted target';
        END IF;
        RETURN NEW;
      END IF;

      IF TG_OP = 'UPDATE'
         AND current_setting('app.redaction_in_progress', true) = 'on'
         AND OLD.id IS NOT DISTINCT FROM NEW.id
         AND OLD.user_id IS NOT DISTINCT FROM NEW.user_id
         AND OLD.target_type IS NOT DISTINCT FROM NEW.target_type
         AND OLD.target_id IS NOT DISTINCT FROM NEW.target_id
         AND OLD.source_id IS NOT DISTINCT FROM NEW.source_id
         AND OLD.source_chunk_id IS NOT DISTINCT FROM NEW.source_chunk_id
         AND OLD.evidence_role IS NOT DISTINCT FROM NEW.evidence_role
         AND OLD.confidence IS NOT DISTINCT FROM NEW.confidence
         AND OLD.created_at IS NOT DISTINCT FROM NEW.created_at
         AND (
           (OLD.quote IS NULL AND NEW.quote IS NULL)
           OR (OLD.quote IS NOT NULL AND NEW.quote = '[REDACTED]')
         )
      THEN
        RETURN NEW;
      END IF;
      RAISE EXCEPTION 'provenance links are immutable outside true redaction';
    END;
    $$;
    """,
    """
    CREATE TRIGGER provenance_links_redaction_guard
    BEFORE INSERT OR UPDATE ON provenance_links
    FOR EACH ROW
    EXECUTE FUNCTION app.guard_provenance_link_redaction();
    """,
    # Ratings retain the numeric review skeleton and reviewer attribution, but
    # prose and arbitrary metadata are destroyed.  INSERT is race-safe against
    # concurrent redaction because the artifact row is locked before scrubbing
    # and this trigger observes its final marker shape.
    """
    CREATE OR REPLACE FUNCTION app.guard_artifact_quality_rating_redaction()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    DECLARE
      target_is_redacted boolean := false;
    BEGIN
      IF TG_OP = 'INSERT' THEN
        SELECT app.is_redacted_project_update_artifact(
          artifact.artifact_type,
          artifact.status,
          artifact.title,
          artifact.content_markdown,
          artifact.prompt_hash,
          artifact.model_info_json,
          artifact.metadata_json
        )
        INTO target_is_redacted
        FROM generated_artifacts AS artifact
        WHERE artifact.id = NEW.artifact_id
          AND artifact.user_id = NEW.user_id
        FOR SHARE;
        IF target_is_redacted THEN
          RAISE EXCEPTION 'ratings cannot be added to a redacted artifact';
        END IF;
        RETURN NEW;
      END IF;

      IF TG_OP = 'UPDATE'
         AND current_setting('app.redaction_in_progress', true) = 'on'
         AND OLD.id IS NOT DISTINCT FROM NEW.id
         AND OLD.user_id IS NOT DISTINCT FROM NEW.user_id
         AND OLD.artifact_id IS NOT DISTINCT FROM NEW.artifact_id
         AND OLD.reviewer_id IS NOT DISTINCT FROM NEW.reviewer_id
         AND OLD.usefulness IS NOT DISTINCT FROM NEW.usefulness
         AND OLD.accuracy IS NOT DISTINCT FROM NEW.accuracy
         AND OLD.source_grounding IS NOT DISTINCT FROM NEW.source_grounding
         AND OLD.novel_connections IS NOT DISTINCT FROM NEW.novel_connections
         AND OLD.actionability IS NOT DISTINCT FROM NEW.actionability
         AND OLD.hallucination_risk IS NOT DISTINCT FROM NEW.hallucination_risk
         AND OLD.verbosity IS NOT DISTINCT FROM NEW.verbosity
         AND OLD.created_at IS NOT DISTINCT FROM NEW.created_at
         AND (
           (OLD.missed_context IS NULL AND NEW.missed_context IS NULL)
           OR (OLD.missed_context IS NOT NULL AND NEW.missed_context = '[REDACTED]')
         )
         AND (
           (OLD.comments IS NULL AND NEW.comments IS NULL)
           OR (OLD.comments IS NOT NULL AND NEW.comments = '[REDACTED]')
         )
         AND NEW.metadata_json = '{"redacted": true}'::jsonb
      THEN
        RETURN NEW;
      END IF;
      RAISE EXCEPTION 'artifact quality ratings are immutable outside true redaction';
    END;
    $$;
    """,
    """
    CREATE TRIGGER artifact_quality_ratings_redaction_guard
    BEFORE INSERT OR UPDATE ON artifact_quality_ratings
    FOR EACH ROW
    EXECUTE FUNCTION app.guard_artifact_quality_rating_redaction();
    """,
    "GRANT UPDATE ON provenance_links TO alicebot_app",
    "GRANT UPDATE ON artifact_quality_ratings TO alicebot_app",
)


# Bounded repair for redactions already authorized before 0092.  Eligibility
# requires the complete prior memory marker, including its original
# redacted_at.  The repair never scans arbitrary payload prose: event coupling
# uses exact targets plus 0091's generated linkage columns.
_BACKFILL_STATEMENTS: tuple[str, ...] = (
    "SELECT set_config('app.redaction_in_progress', 'on', false)",
    # The reads and writes below run under forced row-level security whose
    # owner policies bind app.current_user_id. For the documented table owner
    # (NOSUPERUSER NOBYPASSRLS) that binding is unset during an upgrade, so
    # every read selected no rows, every backfill UPDATE matched none, and a
    # data-bearing vault ended unredacted where a superuser upgrade redacted.
    # Temporarily use the owner bypass, as 0067, 0070, 0093 and 0095 to 0097
    # do, inside Alembic's single transaction. Concurrent sessions never see
    # the relaxed FORCE setting; failure rolls back the bracket, the backfill
    # and the schema change together, and every forced setting is restored
    # before the transaction commits.
    "ALTER TABLE memories NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE event_log NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE generated_artifacts NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE artifact_quality_ratings NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE provenance_links NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE memory_revisions NO FORCE ROW LEVEL SECURITY",
    """
    CREATE TEMP TABLE alice_0092_redacted_memories (
      user_id uuid NOT NULL,
      memory_id uuid NOT NULL,
      redacted_at text NOT NULL,
      PRIMARY KEY (user_id, memory_id)
    ) ON COMMIT DROP
    """,
    """
    INSERT INTO alice_0092_redacted_memories (user_id, memory_id, redacted_at)
    SELECT memory.user_id, memory.id, memory.metadata_json ->> 'redacted_at'
    FROM memories AS memory
    WHERE memory.canonical_text = '[REDACTED]'
      AND (memory.title IS NULL OR memory.title = '[REDACTED]')
      AND (memory.summary IS NULL OR memory.summary = '[REDACTED]')
      AND (memory.trust_reason IS NULL OR memory.trust_reason = '[REDACTED]')
      AND memory.value = '{"redacted": true}'::jsonb
      AND memory.metadata_json -> 'redacted' = 'true'::jsonb
      AND NULLIF(btrim(memory.metadata_json ->> 'redacted_at'), '') IS NOT NULL
      AND memory.metadata_json - ARRAY[
        'consolidation_digest', 'project_id', 'project_scope', 'superseded_by',
        'supersedes', 'source_refs', 'run_id', 'agent_id',
        'created_by_agent_id', 'redacted', 'redacted_at'
      ]::text[] = '{}'::jsonb
      AND memory.embedding_vector IS NULL
      AND memory.fact_keys IS NULL
      AND memory.status = 'archived'
      AND memory.deleted_at IS NOT NULL
      AND EXISTS (
        SELECT 1
        FROM event_log AS receipt
        WHERE receipt.user_id = memory.user_id
          AND receipt.event_type = 'memory.redacted'
          AND receipt.target_type = 'memory'
          AND receipt.target_id = memory.id::text
      )
    ORDER BY memory.user_id, memory.id
    """,
    """
    CREATE TEMP TABLE alice_0092_redacted_artifacts (
      user_id uuid NOT NULL,
      artifact_id uuid NOT NULL,
      memory_id uuid NOT NULL,
      PRIMARY KEY (user_id, artifact_id),
      UNIQUE (user_id, memory_id, artifact_id)
    ) ON COMMIT DROP
    """,
    """
    INSERT INTO alice_0092_redacted_artifacts (user_id, artifact_id, memory_id)
    SELECT artifact.user_id, artifact.id, eligible.memory_id
    FROM generated_artifacts AS artifact
    JOIN alice_0092_redacted_memories AS eligible
      ON eligible.user_id = artifact.user_id
     AND artifact.metadata_json ->> 'candidate_memory_id' = eligible.memory_id::text
    WHERE artifact.artifact_type = 'project_update'
      AND artifact.status IN ('accepted', 'rejected')
      AND NULLIF(btrim(artifact.metadata_json ->> 'project_id'), '') IS NOT NULL
      AND artifact.metadata_json ->> 'workflow' = 'project_auto_update'
      AND artifact.metadata_json -> 'project_scope' =
          jsonb_build_array(artifact.metadata_json ->> 'project_id')
      AND (
        (artifact.status = 'accepted' AND artifact.metadata_json ->> 'review_action' IN ('accept', 'edit'))
        OR (artifact.status = 'rejected' AND artifact.metadata_json ->> 'review_action' = 'reject')
      )
    ORDER BY artifact.user_id, artifact.id
    """,
    """
    UPDATE generated_artifacts AS artifact
    SET title = '[REDACTED]',
        content_markdown = '[REDACTED]',
        prompt_hash = NULL,
        model_info_json = '{"redacted": true}'::jsonb,
        metadata_json = jsonb_build_object(
          'redacted', true,
          'redacted_at', eligible.redacted_at,
          'workflow', 'project_auto_update',
          'project_id', artifact.metadata_json ->> 'project_id',
          'project_scope', jsonb_build_array(artifact.metadata_json ->> 'project_id'),
          'candidate_memory_id', eligible.memory_id::text,
          'review_action', artifact.metadata_json ->> 'review_action'
        )
    FROM alice_0092_redacted_artifacts AS coupled
    JOIN alice_0092_redacted_memories AS eligible
      ON eligible.user_id = coupled.user_id
     AND eligible.memory_id = coupled.memory_id
    WHERE artifact.user_id = coupled.user_id
      AND artifact.id = coupled.artifact_id
      AND (
        artifact.title IS DISTINCT FROM '[REDACTED]'
        OR artifact.content_markdown IS DISTINCT FROM '[REDACTED]'
        OR artifact.prompt_hash IS NOT NULL
        OR artifact.model_info_json IS DISTINCT FROM '{"redacted": true}'::jsonb
        OR artifact.metadata_json IS DISTINCT FROM jsonb_build_object(
          'redacted', true,
          'redacted_at', eligible.redacted_at,
          'workflow', 'project_auto_update',
          'project_id', artifact.metadata_json ->> 'project_id',
          'project_scope', jsonb_build_array(artifact.metadata_json ->> 'project_id'),
          'candidate_memory_id', eligible.memory_id::text,
          'review_action', artifact.metadata_json ->> 'review_action'
        )
      )
    """,
    """
    UPDATE artifact_quality_ratings AS rating
    SET missed_context = CASE
          WHEN rating.missed_context IS NULL THEN NULL ELSE '[REDACTED]'
        END,
        comments = CASE WHEN rating.comments IS NULL THEN NULL ELSE '[REDACTED]' END,
        metadata_json = '{"redacted": true}'::jsonb
    FROM alice_0092_redacted_artifacts AS coupled
    WHERE rating.user_id = coupled.user_id
      AND rating.artifact_id = coupled.artifact_id
      AND (
        (rating.missed_context IS NOT NULL AND rating.missed_context IS DISTINCT FROM '[REDACTED]')
        OR (rating.comments IS NOT NULL AND rating.comments IS DISTINCT FROM '[REDACTED]')
        OR rating.metadata_json IS DISTINCT FROM '{"redacted": true}'::jsonb
      )
    """,
    """
    UPDATE provenance_links AS link
    SET quote = '[REDACTED]'
    WHERE link.quote IS NOT NULL
      AND link.quote IS DISTINCT FROM '[REDACTED]'
      AND (
        (link.target_type = 'memory' AND EXISTS (
          SELECT 1
          FROM alice_0092_redacted_memories AS eligible
          WHERE eligible.user_id = link.user_id
            AND eligible.memory_id::text = link.target_id
        ))
        OR
        (link.target_type = 'artifact' AND EXISTS (
          SELECT 1
          FROM alice_0092_redacted_artifacts AS coupled
          WHERE coupled.user_id = link.user_id
            AND coupled.artifact_id::text = link.target_id
        ))
      )
    """,
    """
    UPDATE memory_revisions AS revision
    SET memory_key = 'redacted.' || revision.memory_id::text,
        previous_value = CASE
          WHEN revision.previous_value IS NULL THEN NULL ELSE '{"redacted": true}'::jsonb
        END,
        new_value = CASE
          WHEN revision.new_value IS NULL THEN NULL ELSE '{"redacted": true}'::jsonb
        END,
        source_event_ids = '[]'::jsonb,
        candidate = '{"redacted": true}'::jsonb,
        text_before = CASE WHEN revision.text_before IS NULL THEN NULL ELSE '[REDACTED]' END,
        text_after = '[REDACTED]',
        reason = CASE WHEN revision.reason IS NULL THEN NULL ELSE '[REDACTED]' END,
        metadata_json = '{"redacted": true}'::jsonb
    FROM alice_0092_redacted_memories AS eligible
    WHERE revision.user_id = eligible.user_id
      AND revision.memory_id = eligible.memory_id
      AND (
        revision.memory_key IS DISTINCT FROM 'redacted.' || revision.memory_id::text
        OR revision.previous_value IS DISTINCT FROM CASE
          WHEN revision.previous_value IS NULL THEN NULL ELSE '{"redacted": true}'::jsonb
        END
        OR revision.new_value IS DISTINCT FROM CASE
          WHEN revision.new_value IS NULL THEN NULL ELSE '{"redacted": true}'::jsonb
        END
        OR revision.source_event_ids IS DISTINCT FROM '[]'::jsonb
        OR revision.candidate IS DISTINCT FROM '{"redacted": true}'::jsonb
        OR revision.text_before IS DISTINCT FROM CASE
          WHEN revision.text_before IS NULL THEN NULL ELSE '[REDACTED]'
        END
        OR revision.text_after IS DISTINCT FROM '[REDACTED]'
        OR revision.reason IS DISTINCT FROM CASE
          WHEN revision.reason IS NULL THEN NULL ELSE '[REDACTED]'
        END
        OR revision.metadata_json IS DISTINCT FROM '{"redacted": true}'::jsonb
      )
    """,
    """
    WITH linked_events AS (
      SELECT event.user_id, event.id AS event_id, eligible.memory_id::text AS memory_id
      FROM alice_0092_redacted_memories AS eligible
      JOIN event_log AS event
        ON event.user_id = eligible.user_id
       AND event.target_type = 'memory'
       AND event.target_id = eligible.memory_id::text
      UNION ALL
      SELECT event.user_id, event.id, eligible.memory_id::text
      FROM alice_0092_redacted_memories AS eligible
      JOIN event_log AS event
        ON event.user_id = eligible.user_id
       AND event.payload_memory_id = eligible.memory_id::text
      UNION ALL
      SELECT event.user_id, event.id, eligible.memory_id::text
      FROM alice_0092_redacted_memories AS eligible
      JOIN event_log AS event
        ON event.user_id = eligible.user_id
       AND event.payload_candidate_memory_id = eligible.memory_id::text
      UNION ALL
      SELECT event.user_id, event.id, coupled.memory_id::text
      FROM alice_0092_redacted_artifacts AS coupled
      JOIN event_log AS event
        ON event.user_id = coupled.user_id
       AND event.target_type = 'artifact'
       AND event.target_id = coupled.artifact_id::text
      UNION ALL
      SELECT event.user_id, event.id, coupled.memory_id::text
      FROM alice_0092_redacted_artifacts AS coupled
      JOIN event_log AS event
        ON event.user_id = coupled.user_id
       AND event.payload_artifact_id = coupled.artifact_id::text
    ),
    event_memory AS (
      SELECT user_id, event_id, min(memory_id) AS memory_id
      FROM linked_events
      GROUP BY user_id, event_id
    )
    UPDATE event_log AS event
    SET payload_json = jsonb_build_object(
          'redacted', true,
          'memory_id', event_memory.memory_id,
          'event_type', event.event_type
        ),
        integrity_hash = NULL
    FROM event_memory
    WHERE event.user_id = event_memory.user_id
      AND event.id = event_memory.event_id
      AND (
        event.payload_json IS DISTINCT FROM jsonb_build_object(
          'redacted', true,
          'memory_id', event_memory.memory_id,
          'event_type', event.event_type
        )
        OR event.integrity_hash IS NOT NULL
      )
    """,
    """
    UPDATE memories AS memory
    SET memory_key = 'redacted.' || memory.id::text,
        title = CASE WHEN memory.title IS NULL THEN NULL ELSE '[REDACTED]' END,
        canonical_text = '[REDACTED]',
        summary = CASE WHEN memory.summary IS NULL THEN NULL ELSE '[REDACTED]' END,
        trust_reason = CASE WHEN memory.trust_reason IS NULL THEN NULL ELSE '[REDACTED]' END,
        value = '{"redacted": true}'::jsonb,
        source_event_ids = '[]'::jsonb,
        metadata_json =
          CASE WHEN memory.metadata_json ? 'project_id'
            THEN jsonb_build_object('project_id', memory.metadata_json -> 'project_id')
            ELSE '{}'::jsonb END
          || CASE WHEN memory.metadata_json ? 'project_scope'
            THEN jsonb_build_object('project_scope', memory.metadata_json -> 'project_scope')
            ELSE '{}'::jsonb END
          || CASE WHEN memory.metadata_json ? 'superseded_by'
            THEN jsonb_build_object('superseded_by', memory.metadata_json -> 'superseded_by')
            ELSE '{}'::jsonb END
          || CASE WHEN memory.metadata_json ? 'supersedes'
            THEN jsonb_build_object('supersedes', memory.metadata_json -> 'supersedes')
            ELSE '{}'::jsonb END
          || CASE WHEN memory.metadata_json ? 'run_id'
            THEN jsonb_build_object('run_id', memory.metadata_json -> 'run_id')
            ELSE '{}'::jsonb END
          || CASE WHEN memory.metadata_json ? 'agent_id'
            THEN jsonb_build_object('agent_id', memory.metadata_json -> 'agent_id')
            ELSE '{}'::jsonb END
          || CASE WHEN memory.metadata_json ? 'created_by_agent_id'
            THEN jsonb_build_object('created_by_agent_id', memory.metadata_json -> 'created_by_agent_id')
            ELSE '{}'::jsonb END
          || jsonb_build_object(
            'redacted', true,
            'redacted_at', eligible.redacted_at
          ),
        commit_digest = NULL,
        confirmation_id = NULL,
        embedding_vector = NULL,
        fact_keys = NULL
    FROM alice_0092_redacted_memories AS eligible
    WHERE memory.user_id = eligible.user_id
      AND memory.id = eligible.memory_id
    """,
    "ALTER TABLE memories FORCE ROW LEVEL SECURITY",
    "ALTER TABLE event_log FORCE ROW LEVEL SECURITY",
    "ALTER TABLE generated_artifacts FORCE ROW LEVEL SECURITY",
    "ALTER TABLE artifact_quality_ratings FORCE ROW LEVEL SECURITY",
    "ALTER TABLE provenance_links FORCE ROW LEVEL SECURITY",
    "ALTER TABLE memory_revisions FORCE ROW LEVEL SECURITY",
    "SELECT set_config('app.redaction_in_progress', 'off', false)",
)


# 0092 must downgrade to 0079's redaction-aware trigger functions, not the
# pre-0079 unconditional append-only functions.  Its UPDATE grants and RLS
# policies deliberately remain in place.
_DOWNGRADE_STATEMENTS: tuple[str, ...] = (
    "REVOKE UPDATE ON artifact_quality_ratings FROM alicebot_app",
    "REVOKE UPDATE ON provenance_links FROM alicebot_app",
    "DROP TRIGGER IF EXISTS artifact_quality_ratings_redaction_guard ON artifact_quality_ratings",
    "DROP FUNCTION IF EXISTS app.guard_artifact_quality_rating_redaction()",
    "DROP TRIGGER IF EXISTS provenance_links_redaction_guard ON provenance_links",
    "DROP FUNCTION IF EXISTS app.guard_provenance_link_redaction()",
    "DROP TRIGGER IF EXISTS generated_artifacts_redaction_guard ON generated_artifacts",
    "DROP FUNCTION IF EXISTS app.guard_generated_artifact_redaction()",
    """
    DROP FUNCTION IF EXISTS app.is_redacted_project_update_artifact(
      text, text, text, text, text, jsonb, jsonb
    )
    """,
    f"""
    CREATE OR REPLACE FUNCTION app.reject_event_log_mutation()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      IF TG_OP = 'UPDATE'
         AND current_setting('app.redaction_in_progress', true) = 'on'
         AND OLD.id IS NOT DISTINCT FROM NEW.id
         AND OLD.user_id IS NOT DISTINCT FROM NEW.user_id
         AND OLD.event_type IS NOT DISTINCT FROM NEW.event_type
         AND OLD.actor_type IS NOT DISTINCT FROM NEW.actor_type
         AND OLD.actor_id IS NOT DISTINCT FROM NEW.actor_id
         AND OLD.target_type IS NOT DISTINCT FROM NEW.target_type
         AND OLD.target_id IS NOT DISTINCT FROM NEW.target_id
         AND OLD.occurred_at IS NOT DISTINCT FROM NEW.occurred_at
         AND OLD.trace_id IS NOT DISTINCT FROM NEW.trace_id
         AND OLD.run_id IS NOT DISTINCT FROM NEW.run_id
         AND NEW.integrity_hash IS NULL
         AND NEW.payload_json @> '{{"redacted": true}}'::jsonb
         AND NEW.payload_json - 'redacted' - 'memory_id' - 'event_type' = '{{}}'::jsonb
      THEN
        RETURN NEW;
      END IF;
      RAISE EXCEPTION 'event_log is append-only';
    END;
    $$;
    """,
    f"""
    CREATE OR REPLACE FUNCTION app.reject_memory_revision_mutation()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      IF TG_OP = 'UPDATE'
         AND current_setting('app.redaction_in_progress', true) = 'on'
         AND OLD.id IS NOT DISTINCT FROM NEW.id
         AND OLD.user_id IS NOT DISTINCT FROM NEW.user_id
         AND OLD.memory_id IS NOT DISTINCT FROM NEW.memory_id
         AND OLD.sequence_no IS NOT DISTINCT FROM NEW.sequence_no
         AND OLD.action IS NOT DISTINCT FROM NEW.action
         AND OLD.memory_key IS NOT DISTINCT FROM NEW.memory_key
         AND OLD.source_event_ids IS NOT DISTINCT FROM NEW.source_event_ids
         AND OLD.revision_number IS NOT DISTINCT FROM NEW.revision_number
         AND OLD.revision_type IS NOT DISTINCT FROM NEW.revision_type
         AND OLD.actor_type IS NOT DISTINCT FROM NEW.actor_type
         AND OLD.actor_id IS NOT DISTINCT FROM NEW.actor_id
         AND OLD.created_at IS NOT DISTINCT FROM NEW.created_at
         AND NEW.text_after = '{REDACTION_MARKER}'
         AND (NEW.text_before IS NULL OR NEW.text_before = '{REDACTION_MARKER}')
         AND (NEW.reason IS NULL OR NEW.reason = '{REDACTION_MARKER}')
         AND (NEW.previous_value IS NULL OR NEW.previous_value = '{{"redacted": true}}'::jsonb)
         AND (NEW.new_value IS NULL OR NEW.new_value = '{{"redacted": true}}'::jsonb)
         AND NEW.candidate = '{{"redacted": true}}'::jsonb
         AND NEW.metadata_json = '{{"redacted": true}}'::jsonb
      THEN
        RETURN NEW;
      END IF;
      RAISE EXCEPTION 'memory revisions are append-only';
    END;
    $$;
    """,
)


def _execute(statements: tuple[str, ...]) -> None:
    for statement in statements:
        op.execute(statement)


def upgrade() -> None:
    _execute(_UPGRADE_STATEMENTS)
    _execute(_BACKFILL_STATEMENTS)


def downgrade() -> None:
    _execute(_DOWNGRADE_STATEMENTS)
