"""PostgreSQL scheduler workflow, run, and claim store seam."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import cast
from uuid import uuid4

from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_stores.postgres.primitives import _json_object

SCHEDULER_WORKFLOW_COLUMNS = """
                  id,
                  user_id,
                  workflow_type,
                  enabled,
                  paused,
                  schedule_json,
                  timezone,
                  next_run_at,
                  last_run_id,
                  last_run_at,
                  last_result,
                  last_error,
                  claim_token,
                  claim_version,
                  claim_expires_at,
                  created_at,
                  updated_at,
                  metadata_json
                """

SCHEDULER_RUN_COLUMNS = """
                  id,
                  user_id,
                  workflow_id,
                  workflow_type,
                  status,
                  triggered_by,
                  trace_id,
                  started_at,
                  finished_at,
                  artifact_id,
                  error_message,
                  claim_token,
                  claim_version,
                  claim_expires_at,
                  scheduled_for,
                  policy_decision_json,
                  agent_identity_json,
                  metadata_json
                """

VNextRow = dict[str, object]

def upsert_scheduler_workflow(self, workflow: JsonObject, *, actor_type: str = "system") -> VNextRow:
    row = self._fetch_optional_one(
        f"""
                INSERT INTO scheduler_workflows (
                  id,
                  user_id,
                  workflow_type,
                  enabled,
                  paused,
                  schedule_json,
                  timezone,
                  next_run_at,
                  metadata_json
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s,
                  COALESCE(%s, false),
                  COALESCE(%s, false),
                  %s,
                  COALESCE(%s, 'UTC'),
                  %s,
                  %s
                )
                ON CONFLICT (user_id, workflow_type)
                DO UPDATE SET
                  enabled = EXCLUDED.enabled,
                  paused = EXCLUDED.paused,
                  schedule_json = EXCLUDED.schedule_json,
                  timezone = EXCLUDED.timezone,
                  next_run_at = EXCLUDED.next_run_at,
                  metadata_json = scheduler_workflows.metadata_json || EXCLUDED.metadata_json,
                  claim_token = NULL,
                  claim_version = scheduler_workflows.claim_version + 1,
                  claim_expires_at = NULL,
                  updated_at = clock_timestamp()
                WHERE scheduler_workflows.enabled IS DISTINCT FROM EXCLUDED.enabled
                   OR scheduler_workflows.paused IS DISTINCT FROM EXCLUDED.paused
                   OR scheduler_workflows.schedule_json IS DISTINCT FROM EXCLUDED.schedule_json
                   OR scheduler_workflows.timezone IS DISTINCT FROM EXCLUDED.timezone
                   OR scheduler_workflows.next_run_at IS DISTINCT FROM EXCLUDED.next_run_at
                   OR NOT (scheduler_workflows.metadata_json @> EXCLUDED.metadata_json)
                RETURNING {SCHEDULER_WORKFLOW_COLUMNS}
                """,
        (
            workflow.get("id"),
            workflow["workflow_type"],
            workflow.get("enabled"),
            workflow.get("paused"),
            _json_object(workflow.get("schedule_json")),
            workflow.get("timezone"),
            workflow.get("next_run_at"),
            _json_object(workflow.get("metadata_json")),
        ),
    )
    mutated = row is not None
    if row is None:
        row = self.get_scheduler_workflow(str(workflow["workflow_type"]))
    if row is None:
        raise ContinuityStoreInvariantError("upsert_scheduler_workflow could not resolve the persisted workflow")
    if mutated:
        self._append_mutation_event(
            event_type="scheduler.workflow_upserted",
            actor_type=actor_type,
            target_type="scheduler_workflow",
            target_id=row["id"],
            payload={
                "operation": "upsert",
                "workflow_type": str(row["workflow_type"]),
                "enabled": bool(row["enabled"]),
                "paused": bool(row["paused"]),
            },
        )
    return row

def update_scheduler_workflow(
    self, *, workflow_type: str, patch: JsonObject, actor_type: str = "system"
) -> VNextRow:
    result_keys = {
        "last_run_id",
        "last_run_at",
        "last_result",
        "last_error",
        "next_run_at",
    }
    preserve_live_claim = "last_run_id" in patch and set(patch) <= result_keys
    row = self._fetch_one(
        "update_scheduler_workflow",
        f"""
                UPDATE scheduler_workflows
                SET enabled = COALESCE(%s, enabled),
                    paused = COALESCE(%s, paused),
                    schedule_json = COALESCE(%s, schedule_json),
                    timezone = COALESCE(%s, timezone),
                    next_run_at = CASE
                      WHEN %s THEN %s::timestamptz
                      ELSE next_run_at
                    END,
                    last_run_id = COALESCE(%s::uuid, last_run_id),
                    last_run_at = COALESCE(%s::timestamptz, last_run_at),
                    last_result = COALESCE(%s, last_result),
                    last_error = CASE
                      WHEN %s THEN %s
                      ELSE last_error
                    END,
                    metadata_json = COALESCE(%s, metadata_json),
                    claim_token = CASE WHEN %s THEN claim_token ELSE NULL END,
                    claim_version = claim_version + CASE WHEN %s THEN 0 ELSE 1 END,
                    claim_expires_at = CASE WHEN %s THEN claim_expires_at ELSE NULL END,
                    updated_at = clock_timestamp()
                WHERE workflow_type = %s
                RETURNING {SCHEDULER_WORKFLOW_COLUMNS}
                """,
        (
            patch.get("enabled"),
            patch.get("paused"),
            _json_object(patch["schedule_json"]) if "schedule_json" in patch else None,
            patch.get("timezone"),
            "next_run_at" in patch,
            patch.get("next_run_at"),
            patch.get("last_run_id"),
            patch.get("last_run_at"),
            patch.get("last_result"),
            "last_error" in patch,
            patch.get("last_error"),
            _json_object(patch["metadata_json"]) if "metadata_json" in patch else None,
            preserve_live_claim,
            preserve_live_claim,
            preserve_live_claim,
            workflow_type,
        ),
    )
    self._append_mutation_event(
        event_type="scheduler.workflow_updated",
        actor_type=actor_type,
        target_type="scheduler_workflow",
        target_id=row["id"],
        payload={"operation": "update", "workflow_type": workflow_type, "changes": patch},
    )
    return row

def get_scheduler_workflow(self, workflow_type: str) -> VNextRow | None:
    return self._fetch_optional_one(
        f"""
                SELECT {SCHEDULER_WORKFLOW_COLUMNS}
                FROM scheduler_workflows
                WHERE workflow_type = %s
                """,
        (workflow_type,),
    )

def list_scheduler_workflows(self) -> list[VNextRow]:
    return self._fetch_all(
        f"""
                SELECT {SCHEDULER_WORKFLOW_COLUMNS}
                FROM scheduler_workflows
                ORDER BY workflow_type ASC
                """
    )

def create_scheduler_run(self, run: JsonObject, *, actor_type: str = "scheduler") -> VNextRow:
    row = self._fetch_one(
        "create_scheduler_run",
        f"""
                INSERT INTO scheduler_runs (
                  id,
                  user_id,
                  workflow_id,
                  workflow_type,
                  status,
                  triggered_by,
                  trace_id,
                  claim_token,
                  claim_version,
                  claim_expires_at,
                  scheduled_for,
                  policy_decision_json,
                  agent_identity_json,
                  metadata_json
                )
                VALUES (
                  COALESCE(%s::uuid, gen_random_uuid()),
                  app.current_user_id(),
                  %s::uuid,
                  %s,
                  COALESCE(%s, 'started'),
                  COALESCE(%s, 'scheduler'),
                  %s,
                  %s,
                  COALESCE(%s, 0),
                  %s,
                  %s,
                  %s,
                  %s,
                  %s
                )
                RETURNING {SCHEDULER_RUN_COLUMNS}
                """,
        (
            run.get("id"),
            run.get("workflow_id"),
            run["workflow_type"],
            run.get("status"),
            run.get("triggered_by"),
            run["trace_id"],
            run.get("claim_token"),
            run.get("claim_version"),
            run.get("claim_expires_at"),
            run.get("scheduled_for"),
            _json_object(run.get("policy_decision_json")),
            _json_object(run.get("agent_identity_json")),
            _json_object(run.get("metadata_json")),
        ),
    )
    self._append_mutation_event(
        event_type="scheduler.run_started",
        actor_type=actor_type,
        target_type="scheduler_run",
        target_id=row["id"],
        trace_id=str(row["trace_id"]),
        run_id=str(row["id"]),
        payload={"workflow_type": str(row["workflow_type"]), "triggered_by": str(row["triggered_by"])},
    )
    return row

def update_scheduler_run(self, *, run_id: str, patch: JsonObject, actor_type: str = "scheduler") -> VNextRow:
    row = self._fetch_one(
        "update_scheduler_run",
        f"""
                UPDATE scheduler_runs
                SET status = COALESCE(%s, status),
                    finished_at = CASE
                      WHEN %s IN ('succeeded', 'failed') THEN clock_timestamp()
                      ELSE finished_at
                    END,
                    artifact_id = COALESCE(%s::uuid, artifact_id),
                    error_message = COALESCE(%s, error_message),
                    policy_decision_json = COALESCE(%s, policy_decision_json),
                    agent_identity_json = COALESCE(%s, agent_identity_json),
                    metadata_json = CASE
                      WHEN %s::jsonb IS NULL THEN metadata_json
                      ELSE metadata_json || %s::jsonb
                    END
                WHERE id = %s::uuid
                RETURNING {SCHEDULER_RUN_COLUMNS}
                """,
        (
            patch.get("status"),
            patch.get("status"),
            patch.get("artifact_id"),
            patch.get("error_message"),
            _json_object(patch["policy_decision_json"]) if "policy_decision_json" in patch else None,
            _json_object(patch["agent_identity_json"]) if "agent_identity_json" in patch else None,
            _json_object(patch["metadata_json"]) if "metadata_json" in patch else None,
            _json_object(patch["metadata_json"]) if "metadata_json" in patch else None,
            run_id,
        ),
    )
    event_type = (
        "scheduler.run_succeeded"
        if row["status"] == "succeeded"
        else "scheduler.run_failed"
        if row["status"] == "failed"
        else "scheduler.run_updated"
    )
    self._append_mutation_event(
        event_type=event_type,
        actor_type=actor_type,
        target_type="scheduler_run",
        target_id=row["id"],
        trace_id=str(row["trace_id"]),
        run_id=str(row["id"]),
        payload={
            "workflow_type": str(row["workflow_type"]),
            "status": str(row["status"]),
            "artifact_id": str(row["artifact_id"]) if row.get("artifact_id") is not None else None,
            "error_message": row.get("error_message"),
        },
    )
    return row

def list_scheduler_runs(self, *, workflow_type: str | None = None, limit: int = 20) -> list[VNextRow]:
    return self._fetch_all(
        f"""
                SELECT {SCHEDULER_RUN_COLUMNS}
                FROM scheduler_runs
                WHERE (%s::text IS NULL OR workflow_type = %s)
                ORDER BY started_at DESC, id DESC
                LIMIT %s
                """,
        (workflow_type, workflow_type, limit),
    )

def claim_due_scheduler_workflow(
    self,
    *,
    checked_at: datetime,
    lease_expires_at: datetime,
    triggered_by: str,
    policy_decision_json: JsonObject | None = None,
    agent_identity_json: JsonObject | None = None,
) -> VNextRow | None:
    """Atomically claim the next due workflow and create its durable run."""

    candidate = self._fetch_optional_one(
        f"""
                SELECT {SCHEDULER_WORKFLOW_COLUMNS}
                FROM scheduler_workflows
                WHERE enabled = true
                  AND paused = false
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= %s::timestamptz
                  AND (claim_expires_at IS NULL OR claim_expires_at <= %s::timestamptz)
                ORDER BY next_run_at ASC, workflow_type ASC, id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
        (checked_at, checked_at),
    )
    if candidate is None:
        return None
    workflow_type = str(candidate["workflow_type"])
    if not self.try_scheduler_workflow_lock(workflow_type):
        return None
    # Re-read after both locks.  A configuration transaction may have
    # changed enabled/paused/next_run_at between the daemon's snapshot and
    # this claim attempt.
    current = self._fetch_optional_one(
        f"""
                SELECT {SCHEDULER_WORKFLOW_COLUMNS}
                FROM scheduler_workflows
                WHERE id = %s::uuid
                  AND enabled = true
                  AND paused = false
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= %s::timestamptz
                  AND (claim_expires_at IS NULL OR claim_expires_at <= %s::timestamptz)
                FOR UPDATE
                """,
        (str(candidate["id"]), checked_at, checked_at),
    )
    if current is None:
        return None
    scheduled_for = current["next_run_at"]
    claim_token = str(uuid4())
    claimed_workflow = self._fetch_one(
        "claim_due_scheduler_workflow",
        f"""
                UPDATE scheduler_workflows
                SET claim_token = %s,
                    claim_version = claim_version + 1,
                    claim_expires_at = %s::timestamptz,
                    updated_at = clock_timestamp()
                WHERE id = %s::uuid
                  AND enabled = true
                  AND paused = false
                  AND next_run_at = %s::timestamptz
                  AND (claim_expires_at IS NULL OR claim_expires_at <= %s::timestamptz)
                RETURNING {SCHEDULER_WORKFLOW_COLUMNS}
                """,
        (
            claim_token,
            lease_expires_at,
            str(current["id"]),
            scheduled_for,
            checked_at,
        ),
    )
    claim_version = int(cast(int, claimed_workflow["claim_version"]))
    trace_id = str(uuid4())
    run = self.create_scheduler_run(
        {
            "workflow_id": claimed_workflow["id"],
            "workflow_type": workflow_type,
            "status": "started",
            "triggered_by": triggered_by,
            "trace_id": trace_id,
            "claim_token": claim_token,
            "claim_version": claim_version,
            "claim_expires_at": lease_expires_at,
            "scheduled_for": scheduled_for,
            "policy_decision_json": policy_decision_json or {},
            "agent_identity_json": agent_identity_json or {},
            "metadata_json": {
                "durable_claim": True,
                "scheduled_for": scheduled_for,
            },
        },
        actor_type=triggered_by,
    )
    return {
        "workflow": claimed_workflow,
        "run": run,
        "claim_token": claim_token,
        "claim_version": claim_version,
        "claim_expires_at": lease_expires_at,
        "scheduled_for": scheduled_for,
    }

def heartbeat_scheduler_claim(
    self,
    *,
    run_id: str,
    claim_token: str,
    claim_version: int,
    lease_expires_at: datetime,
) -> bool:
    row = self._fetch_one(
        "heartbeat_scheduler_claim",
        """
                WITH eligible AS (
                  SELECT r.id AS run_id, r.workflow_id
                  FROM scheduler_runs AS r
                  JOIN scheduler_workflows AS w
                    ON w.id = r.workflow_id
                   AND w.user_id = r.user_id
                  WHERE r.id = %s::uuid
                    AND r.status = 'started'
                    AND r.claim_token = %s
                    AND r.claim_version = %s
                    AND r.claim_expires_at > clock_timestamp()
                    AND w.claim_token = %s
                    AND w.claim_version = %s
                    AND w.claim_expires_at > clock_timestamp()
                  FOR UPDATE OF r, w
                ),
                updated_run AS (
                  UPDATE scheduler_runs AS r
                  SET claim_expires_at = %s::timestamptz
                  FROM eligible AS e
                  WHERE r.id = e.run_id
                  RETURNING r.id
                ),
                updated_workflow AS (
                  UPDATE scheduler_workflows AS w
                  SET claim_expires_at = %s::timestamptz,
                      updated_at = clock_timestamp()
                  FROM eligible AS e
                  WHERE w.id = e.workflow_id
                    AND EXISTS (SELECT 1 FROM updated_run)
                  RETURNING w.id
                )
                SELECT (
                  EXISTS (SELECT 1 FROM updated_run)
                  AND EXISTS (SELECT 1 FROM updated_workflow)
                ) AS renewed
                """,
        (
            run_id,
            claim_token,
            claim_version,
            claim_token,
            claim_version,
            lease_expires_at,
            lease_expires_at,
        ),
    )
    return bool(row.get("renewed"))

def lock_scheduler_claim_for_publish(
    self,
    *,
    run_id: str,
    claim_token: str,
    claim_version: int,
) -> bool:
    """Lock and validate the live claim before publishing staged writes."""

    row = self._fetch_optional_one(
        """
                SELECT r.id AS run_id
                FROM scheduler_runs AS r
                JOIN scheduler_workflows AS w
                  ON w.id = r.workflow_id
                 AND w.user_id = r.user_id
                WHERE r.id = %s::uuid
                  AND r.status = 'started'
                  AND r.claim_token = %s
                  AND r.claim_version = %s
                  AND r.claim_expires_at > clock_timestamp()
                  AND w.claim_token = %s
                  AND w.claim_version = %s
                  AND w.claim_expires_at > clock_timestamp()
                FOR UPDATE OF r, w
                """,
        (
            run_id,
            claim_token,
            claim_version,
            claim_token,
            claim_version,
        ),
    )
    return row is not None

def finalize_scheduler_claim(
    self,
    *,
    run_id: str,
    claim_token: str,
    claim_version: int,
    status: str,
    artifact_id: str | None,
    error_message: str | None,
    next_run_at: str | None,
    metadata_json: JsonObject,
    actor_type: str = "scheduler",
) -> VNextRow | None:
    """Finalize only the still-live matching scheduler fence."""

    if status not in {"succeeded", "failed"}:
        raise ValueError("status must be succeeded or failed")
    row = self._fetch_optional_one(
        f"""
                WITH eligible AS (
                  SELECT r.id AS run_id, r.workflow_id
                  FROM scheduler_runs AS r
                  JOIN scheduler_workflows AS w
                    ON w.id = r.workflow_id
                   AND w.user_id = r.user_id
                  WHERE r.id = %s::uuid
                    AND r.status = 'started'
                    AND r.claim_token = %s
                    AND r.claim_version = %s
                    AND r.claim_expires_at > clock_timestamp()
                    AND w.claim_token = %s
                    AND w.claim_version = %s
                    AND w.claim_expires_at > clock_timestamp()
                  FOR UPDATE OF r, w
                ),
                updated_run AS (
                  UPDATE scheduler_runs AS r
                  SET status = %s,
                      finished_at = clock_timestamp(),
                      artifact_id = %s::uuid,
                      error_message = %s,
                      claim_token = NULL,
                      claim_expires_at = NULL,
                      metadata_json = r.metadata_json || %s::jsonb
                  FROM eligible AS e
                  WHERE r.id = e.run_id
                  RETURNING r.*
                ),
                updated_workflow AS (
                  UPDATE scheduler_workflows AS w
                  SET last_run_id = r.id,
                      last_run_at = r.finished_at,
                      last_result = r.status,
                      last_error = r.error_message,
                      next_run_at = %s::timestamptz,
                      claim_token = NULL,
                      claim_expires_at = NULL,
                      updated_at = clock_timestamp()
                  FROM updated_run AS r
                  WHERE w.id = r.workflow_id
                    AND w.claim_token = %s
                    AND w.claim_version = %s
                  RETURNING w.id
                )
                SELECT {SCHEDULER_RUN_COLUMNS}
                FROM updated_run
                WHERE EXISTS (SELECT 1 FROM updated_workflow)
                """,
        (
            run_id,
            claim_token,
            claim_version,
            claim_token,
            claim_version,
            status,
            artifact_id,
            error_message,
            _json_object(metadata_json),
            next_run_at,
            claim_token,
            claim_version,
        ),
    )
    if row is None:
        return None
    self._append_mutation_event(
        event_type="scheduler.run_succeeded" if status == "succeeded" else "scheduler.run_failed",
        actor_type=actor_type,
        target_type="scheduler_run",
        target_id=row["id"],
        trace_id=str(row["trace_id"]),
        run_id=str(row["id"]),
        payload={
            "workflow_type": str(row["workflow_type"]),
            "status": status,
            "artifact_id": artifact_id,
            "error_message": error_message,
            "claim_version": claim_version,
        },
    )
    return row

def reap_expired_scheduler_claims(
    self,
    *,
    reference_time: datetime,
    limit: int = 100,
    actor_type: str = "scheduler",
) -> list[VNextRow]:
    """Fence abandoned runs and release their workflow leases."""

    if limit < 1:
        raise ValueError("limit must be positive")
    rows = self._fetch_all(
        f"""
                WITH expired AS MATERIALIZED (
                  SELECT r.id, r.workflow_id, r.claim_token, r.claim_version
                  FROM scheduler_runs AS r
                  WHERE r.status = 'started'
                    AND r.claim_token IS NOT NULL
                    AND r.claim_expires_at <= %s::timestamptz
                  ORDER BY r.claim_expires_at ASC, r.id ASC
                  FOR UPDATE SKIP LOCKED
                  LIMIT %s
                ),
                updated_runs AS (
                  UPDATE scheduler_runs AS r
                  SET status = 'failed',
                      finished_at = clock_timestamp(),
                      error_message = COALESCE(r.error_message, 'scheduler claim lease expired'),
                      claim_token = NULL,
                      claim_expires_at = NULL,
                      metadata_json = r.metadata_json || jsonb_build_object(
                        'claim_reaped', true,
                        'claim_reaped_at', %s::timestamptz
                      )
                  FROM expired AS e
                  WHERE r.id = e.id
                    AND r.status = 'started'
                    AND r.claim_token = e.claim_token
                    AND r.claim_version = e.claim_version
                  RETURNING r.*, e.claim_token AS expired_claim_token,
                    e.claim_version AS expired_claim_version
                ),
                cleared_workflows AS (
                  UPDATE scheduler_workflows AS w
                  SET last_run_id = r.id,
                      last_run_at = r.finished_at,
                      last_result = 'failed',
                      last_error = r.error_message,
                      claim_token = NULL,
                      claim_expires_at = NULL,
                      updated_at = clock_timestamp()
                  FROM updated_runs AS r
                  WHERE w.id = r.workflow_id
                    AND w.claim_token = r.expired_claim_token
                    AND w.claim_version = r.expired_claim_version
                  RETURNING w.id
                )
                SELECT {SCHEDULER_RUN_COLUMNS}
                FROM updated_runs
                ORDER BY finished_at ASC, id ASC
                """,
        (reference_time, limit, reference_time),
    )
    for row in rows:
        self._append_mutation_event(
            event_type="scheduler.run_failed",
            actor_type=actor_type,
            target_type="scheduler_run",
            target_id=row["id"],
            trace_id=str(row["trace_id"]),
            run_id=str(row["id"]),
            payload={
                "workflow_type": str(row["workflow_type"]),
                "status": "failed",
                "error_message": row.get("error_message"),
                "claim_reaped": True,
            },
        )
    return rows

def try_scheduler_workflow_lock(self, workflow_type: str) -> bool:
    row = self._fetch_one(
        "try_scheduler_workflow_lock",
        """
                SELECT pg_try_advisory_xact_lock(
                  hashtextextended(
                    concat_ws(':', 'vnext_scheduler', app.current_user_id()::text, %s::text),
                    17
                  )
                ) AS acquired
                """,
        (workflow_type,),
    )
    return bool(row.get("acquired"))

for _method in (
    upsert_scheduler_workflow,
    update_scheduler_workflow,
    get_scheduler_workflow,
    list_scheduler_workflows,
    create_scheduler_run,
    update_scheduler_run,
    list_scheduler_runs,
    claim_due_scheduler_workflow,
    heartbeat_scheduler_claim,
    lock_scheduler_claim_for_publish,
    finalize_scheduler_claim,
    reap_expired_scheduler_claims,
    try_scheduler_workflow_lock,
):
    _method.__module__ = "alicebot_api.vnext_store"
    _method.__qualname__ = f"PostgresVNextStore.{_method.__name__}"
del _method
