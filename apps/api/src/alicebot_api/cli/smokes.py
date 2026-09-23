from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from alicebot_api.config import Settings
from alicebot_api.vnext_agent_control import (
    AgentIdentity,
    agent_metadata,
    append_policy_events,
    ensure_policy_allowed,
    evaluate_agent_policy,
    summarize_agent_policy_telemetry,
)
from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from alicebot_api.vnext_capture import VNextCaptureService
from alicebot_api.vnext_connectors import (
    VNextConnectorService,
    VNextConnectorValidationError,
    scan_local_folder,
)
from alicebot_api.vnext_dogfooding import VNextDogfoodingService
from alicebot_api.vnext_doctor import (
    LOCAL_VNEXT_FRONTEND_ORIGINS,
    VNextDoctorService,
    local_live_cors_status,
)
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_memory_commit import (
    VNextMemoryCommitService,
    memory_commit_request_from_payload,
)
from alicebot_api.vnext_queue import VNextQueueService
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_retrieval import VNextRetrievalRequest
from alicebot_api.vnext_scheduler import SchedulerRunRequest, WORKFLOW_TYPES, default_schedule
from alicebot_api.vnext_scheduler_runtime import (
    SchedulerRuntimeConfig,
    run_due_workflows_durable,
    run_foreground_daemon,
)
from alicebot_api.vnext_secrets import InMemorySecretProvider

from .arguments import _object_dict, _object_int, _object_list
from .capture import _run_vnext_demo_load, _run_vnext_demo_reset
from .constants import DEFAULT_VNEXT_DEMO_DATASET_PATH, logger
from .models import CLIContext
from .shared import (
    _json_dumps,
    _persist_deferred_capture_embeddings,
    _persist_deferred_embedding_inputs,
    _retrieval_service,
    _scheduler_service,
    _vnext_store_context,
)


def _run_vnext_smoke_agentic_scheduler(ctx: CLIContext, _args: argparse.Namespace) -> str:
    smoke_run_id = f"cli-agentic-scheduler-smoke-{uuid4()}"
    with _vnext_store_context(ctx) as store:
        service = _scheduler_service(store)
        initial_status = service.status()
        daily_workflow = service.configure_workflow(
            workflow_type="daily_brief",
            enabled=True,
            paused=False,
            schedule_json={"kind": "daily", "time_of_day": "08:00", "days_of_week": ["monday"]},
            timezone="UTC",
            actor_type="user",
        )
        weekly_workflow = service.configure_workflow(
            workflow_type="weekly_synthesis",
            enabled=True,
            paused=False,
            schedule_json={"kind": "weekly", "day_of_week": "monday", "time_of_day": "09:00"},
            timezone="UTC",
            actor_type="user",
        )
        identity = AgentIdentity(
            agent_id="hermes",
            agent_type="personal_assistant",
            agent_run_id=smoke_run_id,
            project_scope=("Alice",),
            permission_profile="trusted_local_agent",
        )
        store.upsert_agent_identity(
            {
                "agent_id": identity.agent_id,
                "agent_type": identity.agent_type,
                "permission_profile": identity.permission_profile,
                "project_scope_json": list(identity.project_scope),
                "metadata_json": {"last_agent_run_id": identity.agent_run_id, "last_task_id": identity.task_id},
            },
            actor_type="agent",
        )
        proposal_decision = evaluate_agent_policy(
            identity=identity,
            action="memory.propose",
            domains=("project",),
            sensitivity_allowed=("private",),
            project_scope=identity.project_scope,
        )
        append_policy_events(store, identity=identity, decision=proposal_decision)
        ensure_policy_allowed(proposal_decision)
        proposal = store.create_memory(
            {
                "memory_type": "semantic",
                "memory_key": f"agent_proposal.smoke.{uuid4()}",
                "value": {
                    "proposal_type": "candidate_memory",
                    "text": "Agentic scheduler smoke validates proposal-only memory writes.",
                },
                "status": "candidate",
                "confidence": 0.5,
                "title": "Agentic scheduler smoke proposal",
                "canonical_text": "Agentic scheduler smoke validates proposal-only memory writes.",
                "summary": "Agentic scheduler smoke validates proposal-only memory writes.",
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {
                    "proposal_type": "candidate_memory",
                    "review_required": True,
                    **agent_metadata(identity, proposal_decision),
                },
            },
            actor_type="agent",
        )
        append_event(
            store,
            event_type="agent.memory_proposed",
            actor_type="agent",
            actor_id=identity.agent_id,
            target_type="memory",
            target_id=str(proposal["id"]),
            trace_id=proposal_decision.trace_id,
            run_id=identity.agent_run_id,
            payload={"proposal_type": "candidate_memory", "agent_identity": identity.to_record()},
        )
        daily_decision = evaluate_agent_policy(
            identity=identity,
            action="scheduler.run_now",
            domains=("project",),
            sensitivity_allowed=("public", "internal", "private", "unknown"),
            project_scope=identity.project_scope,
            workflow_type="daily_brief",
        )
        append_policy_events(store, identity=identity, decision=daily_decision)
        ensure_policy_allowed(daily_decision)
        daily_run = service.run_now(
            SchedulerRunRequest(
                workflow_type="daily_brief",
                domains=daily_decision.effective_domains,
                sensitivity_allowed=daily_decision.effective_sensitivity_allowed,
                triggered_by="agent",
                agent_identity=identity,
                policy_decision=daily_decision,
            )
        )
        weekly_decision = evaluate_agent_policy(
            identity=identity,
            action="scheduler.run_now",
            domains=("project",),
            sensitivity_allowed=("public", "internal", "private", "unknown"),
            project_scope=identity.project_scope,
            workflow_type="weekly_synthesis",
        )
        append_policy_events(store, identity=identity, decision=weekly_decision)
        ensure_policy_allowed(weekly_decision)
        weekly_run = service.run_now(
            SchedulerRunRequest(
                workflow_type="weekly_synthesis",
                domains=weekly_decision.effective_domains,
                sensitivity_allowed=weekly_decision.effective_sensitivity_allowed,
                triggered_by="agent",
                agent_identity=identity,
                policy_decision=weekly_decision,
            )
        )
        due_decision = evaluate_agent_policy(
            identity=identity,
            action="scheduler.run_due",
            project_scope=identity.project_scope,
        )
        append_policy_events(store, identity=identity, decision=due_decision)
        ensure_policy_allowed(due_decision)
        store.update_scheduler_workflow(
            workflow_type="daily_brief",
            patch={"enabled": True, "paused": False, "next_run_at": "2000-01-01T00:00:00+00:00"},
            actor_type="system",
        )
    due_payload = run_due_workflows_durable(
        database_url=ctx.database_url,
        user_id=ctx.user_id,
        limit=1,
        triggered_by="agent",
        agent_identity=identity,
        policy_decision=due_decision,
    )
    with _vnext_store_context(ctx) as store:
        service = _scheduler_service(store)
        readonly_identity = AgentIdentity(
            agent_id="readonly-smoke",
            agent_type="unknown",
            agent_run_id=smoke_run_id,
            permission_profile="read_only_agent",
        )
        blocked_decision = evaluate_agent_policy(identity=readonly_identity, action="scheduler.pause")
        append_policy_events(store, identity=readonly_identity, decision=blocked_decision)
        pause_payload = service.pause_all(actor_type="user")
        resume_payload = service.resume_all(actor_type="user")
        final_status = service.status()

    daily_run_record = _object_dict(daily_run.get("run"))
    weekly_run_record = _object_dict(weekly_run.get("run"))
    due_runs = _object_list(due_payload.get("runs"))
    first_due = _object_dict(due_runs[0]) if due_runs else {}
    first_due_run = _object_dict(first_due.get("run"))
    gates = {
        "scheduler_defaults_exist": len(_object_list(initial_status.get("workflows"))) >= 6,
        "scheduler_disabled_by_default": initial_status.get("disabled_by_default") is True,
        "daily_workflow_enabled": daily_workflow.get("enabled") is True,
        "weekly_workflow_enabled": weekly_workflow.get("enabled") is True,
        "memory_proposal_candidate": proposal.get("status") == "candidate",
        "daily_run_succeeded": daily_run_record.get("status") == "succeeded",
        "weekly_run_succeeded": weekly_run_record.get("status") == "succeeded",
        "due_scan_executed": due_payload.get("due_count") == 1 and first_due_run.get("status") == "succeeded",
        "scheduler_artifacts_reviewable": _object_dict(daily_run.get("artifact")).get("status") == "needs_review"
        and _object_dict(weekly_run.get("artifact")).get("status") == "needs_review",
        "blocked_policy_recorded": blocked_decision.decision == "blocked",
        "pause_resume_completed": _object_int(pause_payload.get("paused_count")) >= 6
        and _object_int(resume_payload.get("resumed_count")) >= 6,
        "run_history_visible": len(_object_list(final_status.get("recent_runs"))) >= 2,
    }
    payload = {
        "status": "passed" if all(gates.values()) else "failed",
        "smoke": "agentic-scheduler",
        "gates": gates,
        "agent_identity": identity.to_record(),
        "proposal_id": str(proposal.get("id")),
        "daily_run_id": str(daily_run_record.get("id")),
        "weekly_run_id": str(weekly_run_record.get("id")),
        "due_run_id": str(first_due_run.get("id")),
        "policy_decisions": {
            "proposal": proposal_decision.to_record(),
            "daily_run": daily_decision.to_record(),
            "weekly_run": weekly_decision.to_record(),
            "due_run": due_decision.to_record(),
            "blocked": blocked_decision.to_record(),
        },
    }
    if payload["status"] != "passed":
        raise RuntimeError(_json_dumps(payload))
    return _json_dumps(payload)


def _seed_local_runtime_smoke_inputs(store, smoke_id: str) -> None:
    source = store.create_source(
        {
            "source_type": "manual_text",
            "title": f"Local runtime smoke source {smoke_id}",
            "content_hash": f"sha256:local-runtime-smoke-{smoke_id}",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {
                "raw_text": (
                    "Decision: Local runtime smoke should keep scheduled artifacts reviewable. "
                    "TODO: inspect scheduler failures and policy telemetry after daemon scans."
                ),
                "smoke": "local-runtime",
            },
        },
        actor_type="system",
    )
    memory = store.create_memory(
        {
            "memory_type": "project_state",
            "memory_key": f"local_runtime_smoke.{smoke_id}",
            "value": {"text": "The local runtime daemon runs governed scheduler workflows into reviewable artifacts."},
            "status": "active",
            "confidence": 0.8,
            "title": "Local runtime smoke state",
            "canonical_text": "The local runtime daemon runs governed scheduler workflows into reviewable artifacts.",
            "summary": "Local runtime daemon smoke state.",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {"source_id": str(source["id"]), "smoke": "local-runtime"},
        },
        actor_type="system",
    )
    project = store.create_project(
        {
            "name": f"Local Runtime Smoke {smoke_id[:8]}",
            "slug": f"local-runtime-smoke-{smoke_id[:8]}",
            "status": "active",
            "description": "Project used by the vNext local runtime smoke.",
            "current_state": "Needs scheduled project update scan validation.",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {"smoke": "local-runtime"},
        },
        actor_type="system",
    )
    store.create_open_loop(
        {
            "memory_id": str(memory["id"]),
            "title": "Inspect local runtime smoke output",
            "status": "open",
            "description": "Confirm daemon due scans appear in scheduler history and event logs.",
            "priority": "normal",
            "project_id": str(project["id"]),
            "source_id": str(source["id"]),
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {"smoke": "local-runtime"},
        },
        actor_type="system",
    )


def _run_vnext_smoke_local_runtime(ctx: CLIContext, _args: argparse.Namespace) -> str:
    smoke_id = str(uuid4())
    due_at = "2000-01-01T00:00:00+00:00"
    with tempfile.TemporaryDirectory(prefix="alicebot-vnext-scheduler-") as tmpdir:
        runtime_dir = Path(tmpdir)
        with _vnext_store_context(ctx) as store:
            service = _scheduler_service(store)
            service.ensure_default_workflows()
            _seed_local_runtime_smoke_inputs(store, smoke_id)
            for workflow_type in WORKFLOW_TYPES:
                store.update_scheduler_workflow(
                    workflow_type=workflow_type,
                    patch={
                        "enabled": True,
                        "paused": False,
                        "schedule_json": default_schedule(workflow_type),
                        "timezone": "UTC",
                        "next_run_at": due_at,
                        "last_error": None,
                    },
                    actor_type="system",
                )

        daemon_payload = run_foreground_daemon(
            SchedulerRuntimeConfig(
                database_url=ctx.database_url,
                user_id=ctx.user_id,
                interval_seconds=0.1,
                limit=len(WORKFLOW_TYPES),
                pid_file=runtime_dir / "scheduler.pid",
                status_file=runtime_dir / "scheduler-status.json",
                log_file=runtime_dir / "scheduler.log",
                once=True,
            )
        )

    last_due_scan = _object_dict(daemon_payload.get("last_due_scan"))
    due_runs = [_object_dict(item) for item in _object_list(last_due_scan.get("runs"))]
    artifacts = [_object_dict(run.get("artifact")) for run in due_runs if isinstance(run.get("artifact"), dict)]
    required_metadata = {
        "workflow_type",
        "scheduler_run_id",
        "trace_id",
        "source_refs",
        "generated_by",
        "review_status",
    }
    observed_workflows = {str(run.get("workflow_type")) for run in due_runs}
    metadata_complete = all(
        artifact.get("generated_by") == "scheduler"
        and artifact.get("status") == "needs_review"
        and artifact.get("domain") is not None
        and artifact.get("sensitivity") is not None
        and required_metadata.issubset(set(_object_dict(artifact.get("metadata_json")).keys()))
        for artifact in artifacts
    )
    gates = {
        "daemon_once_completed": daemon_payload.get("running") is False and daemon_payload.get("last_error") is None,
        "due_scan_executed_all_workflows": observed_workflows == set(WORKFLOW_TYPES),
        "daily_brief_scheduled": "daily_brief" in observed_workflows,
        "weekly_synthesis_scheduled": "weekly_synthesis" in observed_workflows,
        "connection_report_scheduled": "connection_report" in observed_workflows,
        "contradiction_report_scheduled": "contradiction_report" in observed_workflows,
        "open_loop_review_scheduled": "open_loop_review" in observed_workflows,
        "project_update_scan_scheduled": "project_update_scan" in observed_workflows,
        "scheduled_artifacts_reviewable": len(artifacts) == len(WORKFLOW_TYPES)
        and all(artifact.get("status") == "needs_review" for artifact in artifacts),
        "scheduled_artifact_metadata_complete": metadata_complete,
    }
    daemon_summary = {
        key: daemon_payload.get(key)
        for key in (
            "configured",
            "running",
            "mode",
            "pid",
            "started_at",
            "stopped_at",
            "last_heartbeat_at",
            "last_due_scan_at",
            "last_due_count",
            "last_error",
            "last_error_type",
        )
        if key in daemon_payload
    }
    run_summaries: list[dict[str, object]] = []
    for run in due_runs:
        run_record = _object_dict(run.get("run"))
        artifact_record = _object_dict(run.get("artifact"))
        run_summaries.append(
            {
                "workflow_type": run.get("workflow_type"),
                "run_id": run_record.get("id"),
                "status": run_record.get("status"),
                "artifact_id": artifact_record.get("id"),
                "artifact_type": artifact_record.get("artifact_type"),
            }
        )
    payload = {
        "status": "passed" if all(gates.values()) else "failed",
        "smoke": "local-runtime",
        "gates": gates,
        "daemon": daemon_summary,
        "runs": run_summaries,
    }
    if payload["status"] != "passed":
        raise RuntimeError(_json_dumps(payload))
    return _json_dumps(payload)


def _run_vnext_smoke_model_backed(ctx: CLIContext, _args: argparse.Namespace) -> str:
    smoke_id = str(uuid4())
    due_at = "2000-01-01T00:00:00+00:00"
    with _vnext_store_context(ctx) as store:
        service = _scheduler_service(store)
        service.ensure_default_workflows()
        _seed_local_runtime_smoke_inputs(store, smoke_id)
        store.update_scheduler_workflow(
            workflow_type="daily_brief",
            patch={
                "enabled": True,
                "paused": False,
                "schedule_json": default_schedule("daily_brief"),
                "timezone": "UTC",
                "next_run_at": due_at,
                "last_error": None,
                "metadata_json": {
                    "model_options": {
                        "generation_mode": "model_backed",
                        "model_route_mode": "local_only",
                        "model_provider": "deterministic_local",
                        "model": "alice-vnext-grounded-synthesizer-v1",
                    }
                },
            },
            actor_type="system",
        )
    payload = run_due_workflows_durable(
        database_url=ctx.database_url,
        user_id=ctx.user_id,
        limit=1,
        triggered_by="scheduler",
    )

    runs = _object_list(payload.get("runs"))
    run = _object_dict(runs[0]) if runs else {}
    run_record = _object_dict(run.get("run"))
    artifact = _object_dict(run.get("artifact"))
    metadata = _object_dict(artifact.get("metadata_json"))
    model_info = _object_dict(artifact.get("model_info_json"))
    model_routing = _object_dict(metadata.get("model_routing"))
    content = str(artifact.get("content_markdown", ""))
    gates = {
        "due_scan_ran_one_workflow": payload.get("due_count") == 1,
        "run_succeeded": run_record.get("status") == "succeeded",
        "artifact_reviewable": artifact.get("status") == "needs_review",
        "artifact_model_backed": metadata.get("generation_mode") == "model_backed",
        "local_route_enforced": model_routing.get("route_mode") == "local_only",
        "provider_metadata_present": all(
            model_info.get(key)
            for key in ("provider", "model", "prompt_hash", "input_context_hash", "created_at", "policy_mode")
        ),
        "source_grounded_sections_present": all(
            section in content
            for section in (
                "## Facts",
                "## Inferences",
                "## Recommendations",
                "## Uncertainties",
                "## Source References",
                "## Contradictions Considered",
                "## Open Questions",
            )
        ),
        "source_refs_present": bool(metadata.get("source_refs")),
    }
    result = {
        "status": "passed" if all(gates.values()) else "failed",
        "smoke": "model-backed",
        "gates": gates,
        "artifact_id": artifact.get("id"),
        "run_id": run_record.get("id"),
        "model_info": model_info,
    }
    if result["status"] != "passed":
        raise RuntimeError(_json_dumps(result))
    return _json_dumps(result)


def _run_vnext_smoke_live_capture_connectors(ctx: CLIContext, _args: argparse.Namespace) -> str:
    smoke_id = str(uuid4())
    telegram_update_id = int(time.time() * 1000)
    browser_capture_token = f"clip-smoke-{smoke_id}"
    secrets = InMemorySecretProvider(
        {
            "browser.capture_token.live_smoke": browser_capture_token,
        }
    )
    with tempfile.TemporaryDirectory(prefix="alice-live-capture-") as temp_dir:
        note_path = Path(temp_dir) / "daily.md"
        ignored_dir = Path(temp_dir) / "generated"
        ignored_dir.mkdir()
        note_path.write_text(f"Fact: live capture smoke {smoke_id} reaches Alice.\n", encoding="utf-8")
        (ignored_dir / "skip.md").write_text("Fact: generated output should be ignored.\n", encoding="utf-8")
        local_scan = scan_local_folder((temp_dir,))
        with _vnext_store_context(ctx) as store:
            service = VNextConnectorService(store, secret_provider=secrets, defer_embeddings=True)
            service.update_config(
                "telegram",
                enabled=True,
                config_json={"allowed_chat_ids": ["999001"]},
            )
            service.update_config("browser_clipper", enabled=True, secret_ref="browser.capture_token.live_smoke")
            telegram = service.sync_telegram_updates(
                [
                    {
                        "update_id": telegram_update_id,
                        "message": {
                            "message_id": telegram_update_id + 1,
                            "date": 1_778_400_000,
                            "chat": {"id": 999001, "type": "private"},
                            "from": {"id": 1001, "username": "samir"},
                            "text": f"Fact: Telegram smoke {smoke_id} should be reviewable.",
                        },
                    },
                    {
                        "update_id": telegram_update_id + 2,
                        "message": {
                            "message_id": telegram_update_id + 3,
                            "date": 1_778_400_010,
                            "chat": {"id": 123, "type": "private"},
                            "from": {"id": 1002},
                            "text": "Fact: rejected chat should not import.",
                        },
                    },
                ],
                allowed_chat_ids=("999001",),
            )
            local = service.sync_local_folder_scan(
                local_scan,
                default_domain="project",
                default_sensitivity="private",
            )
            browser = service.capture_browser_clip(
                {
                    "url": f"https://example.test/live-capture/{smoke_id}",
                    "title": "Live capture smoke",
                    "selected_text": f"Fact: Browser clip smoke {smoke_id} is untrusted source material.",
                    "user_note": "Remember: verify capture health.",
                    "capture_token": browser_capture_token,
                },
                default_domain="professional",
                default_sensitivity="private",
            )
            agent = service.ingest_agent_output(
                {
                    "agent_id": "openclaw",
                    "agent_type": "coding_agent",
                    "agent_run_id": f"smoke-{smoke_id}",
                    "project_scope": ["Alice"],
                    "title": "Live capture smoke agent output",
                    "content": f"Decision: Agent output smoke {smoke_id} should stay review-only.",
                    "output_type": "sprint_summary",
                    "domain": "project",
                    "sensitivity": "private",
                    "propose_memory": True,
                },
                policy_decision={"decision": "allowed", "action": "source.capture"},
            )
            health = service.connector_health_all()
    _persist_deferred_embedding_inputs(
        ctx,
        (
            *telegram.deferred_embedding_inputs,
            *local.deferred_embedding_inputs,
            *browser.deferred_embedding_inputs,
            *agent.deferred_embedding_inputs,
        ),
    )
    health_items = {
        str(item["connector_name"]): item
        for item in _object_list(health.get("items"))
        if isinstance(item, dict) and "connector_name" in item
    }
    gates = {
        "telegram_imported_allowlisted": telegram.imported_count == 1 and telegram.skipped_count == 1,
        "local_folder_imported_and_ignored_generated": local.imported_count == 1,
        "browser_clip_imported": browser.imported_count == 1,
        "agent_output_review_only": agent.artifact_id is not None and agent.memory_id is not None,
        "health_telemetry_present": all(
            name in health_items for name in ("telegram", "local_folder", "browser_clipper", "agent_output")
        ),
    }
    payload = {
        "status": "passed" if all(gates.values()) else "failed",
        "smoke": "live-capture-connectors",
        "gates": gates,
    }
    if payload["status"] != "passed":
        raise RuntimeError(_json_dumps(payload))
    return _json_dumps(payload)


def _run_vnext_smoke_capture_to_brief(ctx: CLIContext, _args: argparse.Namespace) -> str:
    smoke_id = str(uuid4())
    browser_capture_token = f"brief-smoke-{smoke_id}"
    secrets = InMemorySecretProvider({"browser.capture_token.brief_smoke": browser_capture_token})
    with _vnext_store_context(ctx) as store:
        connector_service = VNextConnectorService(store, secret_provider=secrets, defer_embeddings=True)
        connector_service.update_config("browser_clipper", enabled=True, secret_ref="browser.capture_token.brief_smoke")
        capture = connector_service.capture_browser_clip(
            {
                "url": f"https://example.test/capture-to-brief/{smoke_id}",
                "title": "Capture to brief smoke",
                "selected_text": f"Fact: capture to brief smoke {smoke_id} should appear in Daily Brief.",
                "user_note": "TODO: rate the generated brief.",
                "capture_token": browser_capture_token,
            },
            default_domain="project",
            default_sensitivity="private",
        )
        source_id = capture.source_ids[0] if capture.source_ids else None
        pack = _retrieval_service(store).compile_context_pack(
            VNextRetrievalRequest(query=smoke_id, domains=("project",), sensitivity_allowed=("private", "unknown"))
        )
        artifact = VNextBrainService(store).generate_daily_brief(
            BrainArtifactRequest(
                domains=("project",), sensitivity_allowed=("private", "unknown"), generated_for="2026-05-11"
            )
        )
        rating = store.create_artifact_quality_rating(
            {
                "artifact_id": artifact["id"],
                "reviewer_id": "smoke",
                "usefulness": 5,
                "accuracy": 5,
                "source_grounding": 5,
                "novel_connections": 3,
                "actionability": 4,
                "hallucination_risk": 1,
                "verbosity": "right_sized",
                "metadata_json": {"smoke": "capture-to-brief", "source_id": source_id},
            },
            actor_type="system",
        )
        dogfooding = VNextDogfoodingService(store).dashboard()
    _persist_deferred_capture_embeddings(ctx, capture)
    artifact_refs = _object_dict(artifact.get("metadata_json")).get("source_refs")
    pack_source_ids = [
        str(source.get("id")) for source in _object_list(pack.get("sources")) if isinstance(source, dict)
    ]
    gates = {
        "source_captured": source_id is not None,
        "context_pack_includes_source": source_id in pack_source_ids,
        "daily_brief_created": artifact.get("artifact_type") == "daily_brief"
        and artifact.get("status") == "needs_review",
        "artifact_has_source_reference": bool(artifact_refs),
        "rating_recorded": str(rating.get("artifact_id")) == str(artifact["id"]),
        "dogfooding_reflects_rating": _object_int(dogfooding.get("artifact_quality_rating_count")) >= 1,
    }
    payload = {"status": "passed" if all(gates.values()) else "failed", "smoke": "capture-to-brief", "gates": gates}
    if payload["status"] != "passed":
        raise RuntimeError(_json_dumps(payload))
    return _json_dumps(payload)


def _run_vnext_smoke_operator_console(ctx: CLIContext, _args: argparse.Namespace) -> str:
    smoke_id = str(uuid4())
    browser_capture_token = f"operator-console-smoke-{smoke_id}"
    secrets = InMemorySecretProvider(
        {
            "browser.capture_token.operator_console": browser_capture_token,
        }
    )
    with _vnext_store_context(ctx) as store:
        connector_service = VNextConnectorService(store, secret_provider=secrets, defer_embeddings=True)
        connector_service.ensure_default_settings()
        connector_service.update_config(
            "telegram",
            enabled=False,
            config_json={"allowed_chat_ids": ["999001"]},
        )
        connector_service.update_config(
            "browser_clipper", enabled=True, secret_ref="browser.capture_token.operator_console"
        )
        capture = connector_service.capture_browser_clip(
            {
                "url": f"https://example.test/operator-console/{smoke_id}",
                "title": "Operator console smoke source",
                "selected_text": f"Fact: operator console smoke {smoke_id} should be traceable from capture to brief.",
                "user_note": "TODO: review the operator console trace.",
                "capture_token": browser_capture_token,
            },
            default_domain="project",
            default_sensitivity="private",
        )
        source_id = capture.source_ids[0] if capture.source_ids else None
        if source_id is None:
            raise RuntimeError("operator console smoke failed to capture source")

        source = store.get_source(source_id)
        source_metadata = _object_dict(source.get("metadata_json") if source is not None else None)
        reviewed_source = store.update_source(
            source_id=source_id,
            patch={
                "metadata_json": {
                    **source_metadata,
                    "review_status": "reviewed",
                    "reviewed_at": datetime.now(UTC).isoformat(),
                    "review_note": "Reviewed by operator-console smoke.",
                }
            },
            actor_type="user",
        )
        append_event(
            store,
            event_type="source.reviewed",
            actor_type="user",
            target_type="source",
            target_id=source_id,
            payload={"smoke": "operator-console"},
        )

        project = store.create_project(
            {
                "name": f"Operator console smoke {smoke_id[:8]}",
                "slug": f"operator-console-{smoke_id[:8]}",
                "status": "active",
                "current_state": "Smoke project created for operator console traceability.",
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {"smoke": "operator-console"},
            },
            actor_type="user",
        )
        candidate_memory = next(
            (
                memory
                for memory in store.list_memories(status="candidate")
                if str(_object_dict(memory.get("metadata_json")).get("source_id")) == source_id
            ),
            None,
        )
        if candidate_memory is None:
            raise RuntimeError("operator console smoke did not create a candidate memory")
        memory_metadata = _object_dict(candidate_memory.get("metadata_json"))
        reviewed_memory = store.update_memory(
            memory_id=str(candidate_memory["id"]),
            patch={
                "status": "active",
                "metadata_json": {
                    **memory_metadata,
                    "project_id": str(project["id"]),
                    "reviewed_by": "operator-console-smoke",
                },
                "last_reviewed_at": datetime.now(UTC).isoformat(),
            },
            actor_type="user",
        )
        store.append_revision(
            {
                "memory_id": str(reviewed_memory["id"]),
                "memory_key": str(reviewed_memory["memory_key"]),
                "previous_value": candidate_memory.get("value"),
                "new_value": reviewed_memory.get("value"),
                "revision_type": "promoted",
                "action": "operator_console_smoke_memory_review",
                "text_before": candidate_memory.get("canonical_text"),
                "text_after": reviewed_memory.get("canonical_text"),
                "reason": "Operator console smoke accepted candidate memory.",
                "actor_type": "user",
                "metadata_json": {"smoke": "operator-console", "source_id": source_id},
            },
            actor_type="user",
        )
        loop = store.create_open_loop(
            {
                "title": f"Review operator console smoke {smoke_id[:8]}",
                "description": "Source-backed open loop from operator console smoke.",
                "priority": "normal",
                "source_id": source_id,
                "memory_id": str(reviewed_memory["id"]),
                "project_id": str(project["id"]),
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {"smoke": "operator-console"},
            },
            actor_type="user",
        )
        artifact = VNextBrainService(store).generate_daily_brief(
            BrainArtifactRequest(
                domains=("project",),
                sensitivity_allowed=("private", "unknown"),
                generated_for="2026-05-12",
                discover_open_loops=True,
                create_candidate_memories=False,
            )
        )
        reviewed_artifact = VNextQueueService(store).review_artifact(
            artifact_id=str(artifact["id"]),
            action="review",
            actor_type="user",
            actor_id=str(ctx.user_id),
        )
        rating = store.create_artifact_quality_rating(
            {
                "artifact_id": str(artifact["id"]),
                "reviewer_id": "operator-console-smoke",
                "usefulness": 5,
                "accuracy": 5,
                "source_grounding": 5,
                "novel_connections": 3,
                "actionability": 4,
                "hallucination_risk": 1,
                "verbosity": "right_sized",
                "metadata_json": {"smoke": "operator-console", "source_id": source_id},
            },
            actor_type="user",
        )
        scheduler = _scheduler_service(store)
        scheduler.configure_workflow(
            workflow_type="daily_brief",
            enabled=True,
            paused=False,
            schedule_json=default_schedule("daily_brief"),
            timezone="UTC",
            actor_type="user",
        )
        scheduled = scheduler.run_now(
            SchedulerRunRequest(
                workflow_type="daily_brief",
                domains=("project",),
                sensitivity_allowed=("private", "unknown"),
                generated_for="2026-05-12",
                triggered_by="user",
                options={"generation_mode": "deterministic"},
            )
        )
        pack = _retrieval_service(store).compile_context_pack(
            VNextRetrievalRequest(query=smoke_id, domains=("project",), sensitivity_allowed=("private", "unknown"))
        )
        health = connector_service.connector_health_all()
        doctor = VNextDoctorService(store, secret_provider=secrets).run(fix_safe=True, ci=True)
        events = store.list_events(limit=100)
    _persist_deferred_capture_embeddings(ctx, capture)

    health_items = {
        str(item["connector_name"]): item
        for item in _object_list(health.get("items"))
        if isinstance(item, dict) and "connector_name" in item
    }
    pack_source_ids = [
        str(source.get("id")) for source in _object_list(pack.get("sources")) if isinstance(source, dict)
    ]
    artifact_refs = _object_dict(artifact.get("metadata_json")).get("source_refs")
    serialized_refs = _json_dumps(artifact_refs)
    gates = {
        "source_review_action_persisted": _object_dict(reviewed_source.get("metadata_json")).get("review_status")
        == "reviewed",
        "memory_review_action_persisted": reviewed_memory.get("status") == "active",
        "artifact_review_and_rating_persisted": reviewed_artifact.get("status") == "reviewed"
        and str(rating.get("artifact_id")) == str(artifact["id"]),
        "open_loop_created_from_source": str(loop.get("source_id")) == source_id and loop.get("status") == "open",
        "scheduler_run_now_created_artifact": scheduled.get("artifact") is not None
        and _object_dict(scheduled.get("run")).get("status") == "succeeded",
        "connector_health_visible": "browser_clipper" in health_items,
        "doctor_readiness_available": doctor.get("status") in {"pass", "warn"}
        and doctor.get("blocking_failure_count") == 0,
        "capture_to_brief_trace_exists": source_id in pack_source_ids or source_id in serialized_refs,
        "event_log_records_actions": any(event.get("event_type") == "source.reviewed" for event in events),
    }
    payload = {"status": "passed" if all(gates.values()) else "failed", "smoke": "operator-console", "gates": gates}
    if payload["status"] != "passed":
        raise RuntimeError(_json_dumps(payload))
    return _json_dumps(payload)


def _run_vnext_smoke_agent_integration_pack(ctx: CLIContext, _args: argparse.Namespace) -> str:
    smoke_id = str(uuid4())
    agent_run_id = f"agent-pack-smoke-{smoke_id}"
    with _vnext_store_context(ctx) as store:
        source = VNextCaptureService(store, defer_embeddings=True).capture_text(
            "\n".join(
                [
                    f"Decision: Agent integration pack smoke {smoke_id} uses scoped project context.",
                    "TODO: Review the agent output proposal before promotion.",
                    "Fact: Agent outputs are evidence, not trusted memory.",
                ]
            ),
            title="Agent integration pack seed",
            domain="project",
            sensitivity="private",
            metadata_json={"smoke": "agent-integration-pack"},
        )
        if source.source_id is None:
            raise RuntimeError("agent integration smoke did not create a source")
        project = store.create_project(
            {
                "name": f"Agent integration smoke {smoke_id[:8]}",
                "slug": f"agent-pack-{smoke_id[:8]}",
                "status": "active",
                "current_state": "Agent integration pack smoke project.",
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {"smoke": "agent-integration-pack"},
            },
            actor_type="system",
        )
        identity = AgentIdentity(
            agent_id="openclaw",
            agent_type="coding_agent",
            agent_run_id=agent_run_id,
            task_id=f"task-{smoke_id[:8]}",
            project_scope=("Alice",),
            permission_profile="project_scoped_agent",
        )
        agent_identity = store.upsert_agent_identity(
            {
                "agent_id": identity.agent_id,
                "agent_type": identity.agent_type,
                "permission_profile": identity.permission_profile,
                "project_scope_json": list(identity.project_scope),
                "metadata_json": {"last_agent_run_id": identity.agent_run_id, "smoke": "agent-integration-pack"},
            },
            actor_type="agent",
        )
        context_decision = evaluate_agent_policy(
            identity=identity,
            action="context_pack.request",
            domains=("project",),
            sensitivity_allowed=("public", "internal", "private", "unknown"),
            project_scope=identity.project_scope,
        )
        append_policy_events(
            store, identity=identity, decision=context_decision, target_type="context_pack", target_id=smoke_id
        )
        ensure_policy_allowed(context_decision)
        context_pack = _retrieval_service(store).compile_context_pack(
            VNextRetrievalRequest(
                query=smoke_id,
                domains=context_decision.effective_domains,
                projects=identity.project_scope,
                sensitivity_allowed=context_decision.effective_sensitivity_allowed,
                max_items=8,
            )
        )
        append_event(
            store,
            event_type="agent.context_pack_requested",
            actor_type="agent",
            actor_id=identity.agent_id,
            target_type="context_pack",
            target_id=smoke_id,
            trace_id=context_decision.trace_id,
            run_id=identity.agent_run_id,
            payload={"agent_identity": identity.to_record(), "policy_decision": context_decision.to_record()},
        )
        output_decision = evaluate_agent_policy(
            identity=identity,
            action="source.capture",
            domains=("project",),
            sensitivity_allowed=("private",),
            project_scope=identity.project_scope,
            write_policy="proposal_only",
        )
        append_policy_events(
            store, identity=identity, decision=output_decision, target_type="connector", target_id="agent_output"
        )
        ensure_policy_allowed(output_decision)
        agent_output = VNextConnectorService(store, defer_embeddings=True).ingest_agent_output(
            {
                "agent_id": identity.agent_id,
                "agent_type": identity.agent_type,
                "agent_run_id": identity.agent_run_id,
                "task_id": identity.task_id,
                "project_scope": list(identity.project_scope),
                "permission_profile": identity.permission_profile,
                "title": "OpenClaw agent integration pack smoke summary",
                "content": (
                    f"Decision: Agent integration pack smoke {smoke_id} keeps durable memory review-only.\n"
                    "TODO: Human should inspect /vnext agent activity."
                ),
                "output_type": "sprint_summary",
                "domain": "project",
                "sensitivity": "private",
                "source_refs": [source.source_id],
                "rationale": "Validate public alpha agent integration pack.",
                "propose_memory": True,
            },
            policy_decision=output_decision.to_record(),
        )
        if agent_output.memory_id is not None:
            append_event(
                store,
                event_type="agent.memory_proposed",
                actor_type="agent",
                actor_id=identity.agent_id,
                target_type="memory",
                target_id=agent_output.memory_id,
                trace_id=output_decision.trace_id,
                run_id=identity.agent_run_id,
                payload={"agent_identity": identity.to_record(), "source_id": agent_output.source_id},
            )
        store.create_open_loop(
            {
                "title": "Review agent integration smoke proposal",
                "description": "Open loop created by the agent integration pack smoke.",
                "priority": "normal",
                "source_id": agent_output.source_id or source.source_id,
                "project_id": str(project["id"]),
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {"smoke": "agent-integration-pack", "agent_id": identity.agent_id},
            },
            actor_type="agent",
        )
        blocked_decision = evaluate_agent_policy(
            identity=identity,
            action="context_pack.request",
            domains=("family", "health", "spiritual"),
            sensitivity_allowed=("private", "highly_sensitive"),
            project_scope=identity.project_scope,
        )
        append_policy_events(
            store, identity=identity, decision=blocked_decision, target_type="context_pack", target_id=smoke_id
        )
        events = store.list_agent_events(agent_id=identity.agent_id, limit=100)
        event_types = {str(event.get("event_type")) for event in events}
        candidate_memories = store.list_memories(status="candidate")
        active_memories = store.list_memories(status="active")
        telemetry = summarize_agent_policy_telemetry(
            agent_events=events,
            artifacts=store.list_artifacts(limit=100),
            memories=store.list_memories(status=None),
        )
        agent_identities = store.list_agent_identities(limit=20)

    _persist_deferred_embedding_inputs(
        ctx,
        (*source.deferred_embedding_inputs, *agent_output.deferred_embedding_inputs),
    )
    pack_source_ids = [
        str(item.get("id")) for item in _object_list(context_pack.get("sources")) if isinstance(item, dict)
    ]
    candidate_ids = {str(memory.get("id")) for memory in candidate_memories}

    def _matches_smoke_active_agent_memory(memory: JsonObject) -> bool:
        metadata = _object_dict(memory.get("metadata_json"))
        identity_payload = metadata.get("agent_identity")
        if isinstance(identity_payload, dict) and identity_payload.get("agent_run_id") == agent_run_id:
            return True
        return metadata.get("agent_run_id") == agent_run_id

    gates = {
        "agent_identified_as_openclaw": agent_identity.get("agent_id") == "openclaw"
        and agent_identity.get("permission_profile") == "project_scoped_agent",
        "agent_requested_project_context_pack": context_decision.decision == "allowed"
        and source.source_id in pack_source_ids,
        "scoped_context_pack_returned": _object_int(_object_dict(context_pack.get("trace")).get("selected_count")) >= 1,
        "agent_output_stored_as_reviewable_source_or_artifact": agent_output.source_id is not None
        and agent_output.artifact_id is not None,
        "memory_proposal_in_review_queue": agent_output.memory_id is not None
        and agent_output.memory_id in candidate_ids,
        "no_trusted_memory_auto_promoted": not any(
            _matches_smoke_active_agent_memory(memory) for memory in active_memories
        ),
        "event_log_records_full_flow": {
            "agent.context_pack_requested",
            "agent.output_ingested",
            "agent.memory_proposed",
            "agent.policy_blocked",
        }.issubset(event_types),
        "policy_blocks_restricted_domain_request": blocked_decision.decision == "blocked"
        and "all_requested_domains_restricted" in blocked_decision.reasons,
        "vnext_agent_activity_visible": bool(agent_identities)
        and _object_int(telemetry.get("total_agent_events")) >= 4
        and bool(telemetry.get("policy_blocks_by_agent")),
    }
    payload = {
        "status": "passed" if all(gates.values()) else "failed",
        "smoke": "agent-integration-pack",
        "gates": gates,
        "agent_identity": identity.to_record(),
        "context_trace": context_pack.get("trace"),
        "agent_output": agent_output.to_record(),
        "policy_decisions": {
            "context_pack": context_decision.to_record(),
            "output_ingest": output_decision.to_record(),
            "restricted_request": blocked_decision.to_record(),
        },
    }
    if payload["status"] != "passed":
        raise RuntimeError(_json_dumps(payload))
    return _json_dumps(payload)


def _run_alpha_smoke(ctx: CLIContext, *, name: str, runner) -> JsonObject:
    try:
        return {"name": name, "result": json.loads(runner(ctx, argparse.Namespace())), "status": "passed"}
    except Exception as exc:
        logger.debug(
            "Alpha smoke failed name=%s",
            name,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return {
            "name": name,
            "status": "failed",
            "error_type": None,
            "error_code": "smoke_failed",
            "error": "The smoke check failed",
        }


def _headless_file_contains(path: Path, markers: tuple[str, ...]) -> bool:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return all(marker in content for marker in markers)


def _run_vnext_smoke_headless_ubuntu(_ctx: CLIContext, _args: argparse.Namespace) -> str:
    repo_root = Path(__file__).resolve().parents[5]
    install_script = repo_root / "scripts" / "install-ubuntu.sh"
    uninstall_script = repo_root / "scripts" / "uninstall-ubuntu.sh"
    service_paths = {
        "api": repo_root / "packaging" / "systemd" / "alice-api.service",
        "web": repo_root / "packaging" / "systemd" / "alice-web.service",
        "scheduler": repo_root / "packaging" / "systemd" / "alice-scheduler.service",
    }
    doc_paths = {
        "install": repo_root / "docs" / "alpha" / "headless-ubuntu-install.md",
        "hermes": repo_root / "docs" / "alpha" / "hermes-dogfood-ubuntu.md",
        "release_notes": repo_root / "docs" / "release" / "v0.6.0-alpha-rc.2-release-notes.md",
        "cto": repo_root / "docs" / "archive" / "process" / "vnext-headless-ubuntu-cto-summary.md",
        "env": repo_root / "packaging" / "ubuntu" / "alicebot.env.example",
    }
    service_contents = {
        name: path.read_text(encoding="utf-8") if path.exists() else "" for name, path in service_paths.items()
    }
    gates = {
        "installer_exists_and_is_executable": install_script.exists() and os.access(install_script, os.X_OK),
        "installer_supports_required_flags": _headless_file_contains(
            install_script,
            ("--tag", "--branch", "--install-dir", "--skip-postgres-install", "--non-interactive"),
        ),
        "uninstaller_exists_and_confirms_destructive_actions": _headless_file_contains(
            uninstall_script,
            ("--remove-repo", "--remove-config", "--drop-database", "confirm"),
        ),
        "systemd_templates_exist": all(path.exists() for path in service_paths.values()),
        "systemd_runs_as_non_root": all("User=__ALICE_USER__" in content for content in service_contents.values()),
        "systemd_binds_localhost_by_default": all("127.0.0.1" in content for content in service_contents.values())
        and not any("0.0.0.0" in content for content in service_contents.values()),
        "env_template_documents_config_layout": _headless_file_contains(
            doc_paths["env"],
            ("DATABASE_URL=", "ALICE_API_HOST=127.0.0.1", "ALICE_WEB_HOST=127.0.0.1", "ALICE_SECRET_PROVIDER="),
        ),
        "headless_install_doc_covers_ssh_tunnel": _headless_file_contains(
            doc_paths["install"],
            ("ssh -L 3000:127.0.0.1:3000", "Do not expose", "alicebot vnext alpha check --headless"),
        ),
        "hermes_guide_covers_identity_and_policy": _headless_file_contains(
            doc_paths["hermes"],
            (
                "agent_id: hermes",
                "trusted_local_agent",
                "policy-boundary test",
                "alicebot vnext smoke agent-integration-pack",
            ),
        ),
        "rc_release_notes_exist": _headless_file_contains(
            doc_paths["release_notes"],
            ("v0.6.0-alpha-rc.2", "pre-release", "not latest", "headless Ubuntu"),
        ),
        "cto_summary_exists": _headless_file_contains(
            doc_paths["cto"],
            ("Headless Ubuntu", "Hermes dogfood", "SSH tunnel", "v0.6.0-alpha-rc.2"),
        ),
    }
    payload = {
        "status": "passed" if all(gates.values()) else "failed",
        "smoke": "headless-ubuntu",
        "gates": gates,
        "checked_paths": {
            "install_script": str(install_script.relative_to(repo_root)),
            "uninstall_script": str(uninstall_script.relative_to(repo_root)),
            "systemd": {name: str(path.relative_to(repo_root)) for name, path in service_paths.items()},
            "docs": {name: str(path.relative_to(repo_root)) for name, path in doc_paths.items()},
        },
    }
    if payload["status"] != "passed":
        raise RuntimeError(_json_dumps(payload))
    return _json_dumps(payload)


def _run_vnext_smoke_local_cors(ctx: CLIContext, _args: argparse.Namespace) -> str:
    repo_root = Path(__file__).resolve().parents[5]
    root_env = repo_root / ".env.example"
    ubuntu_env = repo_root / "packaging" / "ubuntu" / "alicebot.env.example"
    web_env = repo_root / "apps" / "web" / ".env.local.example"
    required_origin_markers = tuple(LOCAL_VNEXT_FRONTEND_ORIGINS)
    active_status = local_live_cors_status(settings=ctx.settings, cwd=repo_root)
    template_paths = (root_env, ubuntu_env)
    template_contents = {
        str(path.relative_to(repo_root)): path.read_text(encoding="utf-8") if path.exists() else ""
        for path in (*template_paths, web_env)
    }
    gates = {
        "strict_default_empty_allowlist": Settings().cors_allowed_origins == (),
        "root_env_template_has_explicit_local_origins": _headless_file_contains(root_env, required_origin_markers)
        and "*" not in template_contents[str(root_env.relative_to(repo_root))],
        "ubuntu_env_template_has_explicit_local_origins": _headless_file_contains(ubuntu_env, required_origin_markers)
        and "*" not in template_contents[str(ubuntu_env.relative_to(repo_root))],
        "web_env_template_points_to_local_api": _headless_file_contains(
            web_env,
            (
                "NEXT_PUBLIC_ALICEBOT_API_BASE_URL=http://127.0.0.1:8000",
                "NEXT_PUBLIC_ALICEBOT_USER_ID=",
            ),
        ),
        "active_live_cors_valid_when_configured": bool(active_status.get("ok")),
        "active_cors_does_not_use_wildcard": not bool(active_status.get("wildcard_present")),
    }
    payload = {
        "status": "passed" if all(gates.values()) else "failed",
        "smoke": "local-cors",
        "gates": gates,
        "active_status": active_status,
        "checked_paths": {
            "root_env": str(root_env.relative_to(repo_root)),
            "ubuntu_env": str(ubuntu_env.relative_to(repo_root)),
            "web_env": str(web_env.relative_to(repo_root)),
        },
    }
    if payload["status"] != "passed":
        raise RuntimeError(_json_dumps(payload))
    return _json_dumps(payload)


def _run_vnext_smoke_agentic_memory_commit(ctx: CLIContext, _args: argparse.Namespace) -> str:
    smoke_id = str(uuid4())
    hermes = AgentIdentity(
        agent_id="hermes",
        agent_type="personal_assistant",
        agent_run_id=f"agentic-memory-smoke-{smoke_id}",
        task_id="agentic-memory-commit-smoke",
        permission_profile="trusted_local_agent",
    )
    openclaw = AgentIdentity(
        agent_id="openclaw",
        agent_type="coding_agent",
        agent_run_id=f"agentic-memory-smoke-{smoke_id}",
        task_id="agentic-memory-commit-smoke",
        project_scope=("Alice",),
        permission_profile="project_scoped_agent",
    )
    read_only = AgentIdentity(
        agent_id="readonly-smoke",
        agent_type="unknown",
        agent_run_id=f"agentic-memory-smoke-{smoke_id}",
        task_id="agentic-memory-commit-smoke",
        permission_profile="read_only_agent",
    )
    gates: dict[str, bool] = {}
    with _vnext_store_context(ctx) as store:
        service = VNextMemoryCommitService(store)
        committed = service.commit(
            identity=hermes,
            request=memory_commit_request_from_payload(
                {
                    "title": f"Agentic memory smoke commit {smoke_id}",
                    "canonical_text": f"Agentic memory smoke {smoke_id} commits explicit trusted facts through Alice.",
                    "domain": "professional",
                    "sensitivity": "internal",
                    "confidence": 0.97,
                    "idempotency_key": f"agentic-memory-smoke-commit-{smoke_id}",
                },
                user_id=ctx.user_id,
            ),
        )
        committed_memory = _object_dict(committed.get("memory"))
        committed_memory_id = str(committed_memory.get("id"))
        gates["trusted_hermes_commit_active"] = (
            committed.get("status") == "committed" and committed_memory.get("status") == "active"
        )

        before_undo_context = _retrieval_service(store).compile_context_pack(
            VNextRetrievalRequest(
                query=f"Agentic memory smoke {smoke_id}",
                domains=("professional",),
                sensitivity_allowed=("public", "internal", "private", "unknown"),
                max_items=8,
            )
        )
        gates["committed_memory_enters_context"] = any(
            str(memory.get("id")) == committed_memory_id
            for memory in _object_list(before_undo_context.get("relevant_memories"))
            if isinstance(memory, dict)
        )

        sensitive = service.commit(
            identity=hermes,
            request=memory_commit_request_from_payload(
                {
                    "title": f"Agentic memory smoke sensitive {smoke_id}",
                    "canonical_text": f"Agentic memory smoke {smoke_id} keeps sensitive health details behind confirmation.",
                    "domain": "health",
                    "sensitivity": "private",
                    "confidence": 0.94,
                },
                user_id=ctx.user_id,
            ),
        )
        confirmation_id = str(sensitive.get("confirmation_id"))
        gates["sensitive_memory_requires_confirmation"] = sensitive.get(
            "status"
        ) == "confirmation_required" and confirmation_id.startswith("confirm-")
        confirmed = service.confirm(identity=hermes, confirmation_id=confirmation_id)
        confirmed_memory = _object_dict(confirmed.get("memory"))
        gates["inline_confirmation_commits"] = (
            confirmed.get("status") == "committed" and confirmed_memory.get("status") == "active"
        )

        above_ceiling = service.commit(
            identity=hermes,
            request=memory_commit_request_from_payload(
                {
                    "title": f"Agentic memory smoke above ceiling {smoke_id}",
                    "canonical_text": f"Agentic memory smoke {smoke_id} refuses a confidential hermes commit.",
                    "domain": "professional",
                    "sensitivity": "confidential",
                    "confidence": 0.94,
                },
                user_id=ctx.user_id,
            ),
        )
        gates["confidential_hermes_commit_refused"] = (
            above_ceiling.get("status") == "rejected"
            and above_ceiling.get("reason") == "sensitivity_above_agent_ceiling"
        )

        external = service.commit(
            identity=hermes,
            request=memory_commit_request_from_payload(
                {
                    "title": f"Agentic memory smoke external {smoke_id}",
                    "canonical_text": f"Agentic memory smoke {smoke_id} browser evidence stays reviewable.",
                    "domain": "professional",
                    "sensitivity": "internal",
                    "confidence": 0.91,
                    "source_type": "browser_clip",
                },
                user_id=ctx.user_id,
            ),
        )
        external_memory = _object_dict(external.get("memory"))
        gates["external_source_review_required"] = (
            external.get("status") == "review_required" and external_memory.get("status") == "candidate"
        )

        blocked = service.commit(
            identity=read_only,
            request=memory_commit_request_from_payload(
                {
                    "title": f"Agentic memory smoke blocked {smoke_id}",
                    "canonical_text": f"Agentic memory smoke {smoke_id} read-only agents cannot write.",
                    "domain": "professional",
                    "sensitivity": "internal",
                    "confidence": 0.91,
                },
                user_id=ctx.user_id,
            ),
        )
        gates["read_only_rejected"] = blocked.get("status") == "rejected"

        project_commit = service.commit(
            identity=openclaw,
            request=memory_commit_request_from_payload(
                {
                    "title": f"Agentic memory smoke project {smoke_id}",
                    "canonical_text": f"Agentic memory smoke {smoke_id} lets OpenClaw commit scoped project facts.",
                    "domain": "project",
                    "sensitivity": "private",
                    "confidence": 0.93,
                    "project_scope": ["Alice"],
                },
                user_id=ctx.user_id,
            ),
        )
        gates["project_scoped_commit_allowed"] = project_commit.get("status") == "committed"

        out_of_scope = service.commit(
            identity=openclaw,
            request=memory_commit_request_from_payload(
                {
                    "title": f"Agentic memory smoke out of scope {smoke_id}",
                    "canonical_text": f"Agentic memory smoke {smoke_id} blocks non-project OpenClaw writes.",
                    "domain": "family",
                    "sensitivity": "private",
                    "confidence": 0.93,
                    "project_scope": ["Alice"],
                },
                user_id=ctx.user_id,
            ),
        )
        gates["project_scoped_out_of_scope_rejected"] = out_of_scope.get("status") == "rejected"

        corrected = service.correct(
            identity=hermes,
            memory_id=str(confirmed_memory.get("id")),
            canonical_text=f"Agentic memory smoke {smoke_id} confirms and corrects sensitive details safely.",
            reason="Agentic memory smoke correction.",
        )
        gates["correction_revises_memory"] = corrected.get("status") == "committed" and "corrects" in str(
            _object_dict(corrected.get("memory")).get("canonical_text")
        )

        forgotten = service.forget(
            identity=hermes,
            memory_id=str(confirmed_memory.get("id")),
            reason="Agentic memory smoke forget.",
        )
        gates["forget_preserves_audit_and_excludes_context"] = (
            forgotten.get("status") == "forgotten"
            and (_object_dict(forgotten.get("memory"))).get("status") == "superseded"
        )

        undone = service.undo(identity=hermes, memory_id=committed_memory_id, reason="Agentic memory smoke undo.")
        gates["undo_supersedes_committed_memory"] = (
            undone.get("status") == "undone" and (_object_dict(undone.get("memory"))).get("status") == "superseded"
        )

        after_undo_context = _retrieval_service(store).compile_context_pack(
            VNextRetrievalRequest(
                query=f"Agentic memory smoke {smoke_id}",
                domains=("professional",),
                sensitivity_allowed=("public", "internal", "private", "unknown"),
                max_items=8,
            )
        )
        gates["undone_memory_leaves_context"] = all(
            str(memory.get("id")) != committed_memory_id
            for memory in _object_list(after_undo_context.get("relevant_memories"))
            if isinstance(memory, dict)
        )

        audit = service.audit(memory_id=committed_memory_id)
        gates["audit_includes_revision_and_undo_event"] = bool(audit.get("revisions")) and any(
            event.get("event_type") == "agent.memory_undone"
            for event in _object_list(audit.get("events"))
            if isinstance(event, dict)
        )
        recent = service.recent_commits(limit=20)
        gates["recent_commits_visible"] = any(
            str(memory.get("id")) == committed_memory_id
            for memory in _object_list(recent.get("recent_commits"))
            if isinstance(memory, dict)
        )

    payload = {
        "status": "passed" if all(gates.values()) else "failed",
        "smoke": "agentic-memory-commit",
        "gates": gates,
    }
    if payload["status"] != "passed":
        raise RuntimeError(_json_dumps(payload))
    return _json_dumps(payload)


def _check_headless_http_url(url: str | None) -> JsonObject:
    if not url:
        return {
            "status": "skipped",
            "url": None,
            "message": "No URL supplied. Pass --api-url or --web-url after services are running.",
        }
    request = Request(url, method="GET", headers={"User-Agent": "alicebot-headless-alpha-check"})
    try:
        with urlopen(request, timeout=2.0) as response:
            status_code = int(response.getcode())
    except (OSError, URLError) as exc:
        logger.debug(
            "Headless reachability check failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return {
            "status": "failed",
            "url": url,
            "error_type": None,
            "error_code": "reachability_failed",
            "error": "The reachability check failed",
        }
    return {"status": "passed" if status_code < 500 else "failed", "url": url, "status_code": status_code}


def _check_headless_mcp_import() -> JsonObject:
    try:
        import importlib.util

        spec = importlib.util.find_spec("alicebot_api.mcp_server")
    except Exception as exc:
        logger.debug(
            "Headless MCP import check failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return {
            "status": "failed",
            "error_type": None,
            "error_code": "import_check_failed",
            "error": "The MCP import check failed",
        }
    if spec is None:
        return {"status": "failed", "error": "alicebot_api.mcp_server module was not found"}
    return {
        "status": "passed",
        "command": "./.venv/bin/python -m alicebot_api.mcp_server",
        "message": "MCP server module is importable from the installed Alice package.",
    }


def _run_vnext_alpha_check(ctx: CLIContext, args: argparse.Namespace) -> str:
    alpha_secret_provider = InMemorySecretProvider(
        {
            "browser.capture_token.default": "alpha-check-placeholder",
        }
    )
    with _vnext_store_context(ctx) as store:
        doctor = VNextDoctorService(store, secret_provider=alpha_secret_provider).run(fix_safe=True, ci=True)
        scheduler = _scheduler_service(store).status()
        connector_storage = store.connector_storage_status()
        connector_settings = store.list_connector_settings()
        connector_states = store.list_connector_states()

    headless: JsonObject | None = None
    if args.headless:
        headless_smoke = _run_alpha_smoke(ctx, name="headless-ubuntu", runner=_run_vnext_smoke_headless_ubuntu)
        headless = {
            "mode": "headless_ubuntu",
            "package": headless_smoke,
            "api_reachability": _check_headless_http_url(args.api_url),
            "web_reachability": _check_headless_http_url(args.web_url),
            "mcp": _check_headless_mcp_import(),
            "demo_cycle": {"status": "skipped", "message": "Pass --demo-cycle to run demo load/reset."},
        }
        if args.demo_cycle:
            try:
                demo_load = json.loads(
                    _run_vnext_demo_load(
                        ctx, argparse.Namespace(fixture=str(DEFAULT_VNEXT_DEMO_DATASET_PATH), reset=True)
                    )
                )
                demo_reset = json.loads(
                    _run_vnext_demo_reset(
                        ctx, argparse.Namespace(dataset_id=None, fixture=str(DEFAULT_VNEXT_DEMO_DATASET_PATH))
                    )
                )
                headless["demo_cycle"] = {"status": "passed", "load": demo_load, "reset": demo_reset}
            except Exception as exc:
                logger.debug(
                    "Headless demo cycle failed",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                headless["demo_cycle"] = {
                    "status": "failed",
                    "error_type": None,
                    "error_code": "demo_cycle_failed",
                    "error": "The demo cycle failed",
                }

    smokes: list[JsonObject] = []
    if not args.skip_smokes:
        smoke_runners = (
            ("connector-hardening", _run_vnext_smoke_connector_hardening),
            ("local-cors", _run_vnext_smoke_local_cors),
            ("secret-redaction", _run_vnext_smoke_secret_redaction),
            ("dogfood-doctor", _run_vnext_smoke_dogfood_doctor),
            ("live-capture-connectors", _run_vnext_smoke_live_capture_connectors),
            ("capture-to-brief", _run_vnext_smoke_capture_to_brief),
            ("agentic-memory-commit", _run_vnext_smoke_agentic_memory_commit),
            ("agentic-scheduler", _run_vnext_smoke_agentic_scheduler),
            ("operator-console", _run_vnext_smoke_operator_console),
            ("agent-integration-pack", _run_vnext_smoke_agent_integration_pack),
            *((("headless-ubuntu", _run_vnext_smoke_headless_ubuntu),) if args.headless else ()),
        )
        smokes = [_run_alpha_smoke(ctx, name=name, runner=runner) for name, runner in smoke_runners]

    blocking: list[str] = []
    if doctor.get("status") == "fail" or _object_int(doctor.get("blocking_failure_count")) > 0:
        blocking.append("doctor")
    if not bool(connector_storage.get("connector_settings_exists")) or not bool(
        connector_storage.get("connector_state_exists")
    ):
        blocking.append("connector_storage")
    failed_smokes = [str(smoke.get("name")) for smoke in smokes if smoke.get("status") != "passed"]
    blocking.extend(f"smoke:{name}" for name in failed_smokes)
    if isinstance(headless, dict):
        package_status = _object_dict(headless.get("package")).get("status")
        if package_status != "passed":
            blocking.append("headless:package")
        for key in ("api_reachability", "web_reachability", "mcp", "demo_cycle"):
            item = headless.get(key)
            if isinstance(item, dict) and item.get("status") == "failed":
                blocking.append(f"headless:{key}")

    payload = {
        "status": "failed" if blocking else "passed" if doctor.get("status") == "pass" else "warning",
        "alpha_check": "vnext_public_alpha",
        "headless": headless,
        "blocking_failures": blocking,
        "doctor": doctor,
        "scheduler": {
            "disabled_by_default": scheduler.get("disabled_by_default"),
            "workflow_count": len(_object_list(scheduler.get("workflows"))),
            "recent_run_count": len(_object_list(scheduler.get("recent_runs"))),
        },
        "connector_storage": connector_storage,
        "connector_settings_count": len(connector_settings),
        "connector_state_count": len(connector_states),
        "smokes": smokes,
        "eval_suite": {
            "status": "summarized",
            "command": "alicebot eval run --suite all",
            "expected": (
                "retrieval_quality passes against a live store "
                "(ALICEBOT_EVAL_DATABASE_URL); without one it reports skipped, never a fabricated pass"
            ),
        },
        "recommended_next_commands": [
            "alicebot eval run --suite all",
            "pnpm --dir apps/web test",
            "pnpm --dir apps/web lint",
            "pnpm --dir apps/web build",
        ],
    }
    output = _json_dumps(payload)
    if payload["status"] == "failed":
        print(output)
        raise VNextConnectorValidationError("vNext alpha check found blocking failures")
    return output


def _run_vnext_smoke_connector_hardening(ctx: CLIContext, _args: argparse.Namespace) -> str:
    smoke_id = str(uuid4())
    telegram_update_id = int(time.time() * 1000)
    with tempfile.TemporaryDirectory(prefix="alice-connector-hardening-") as temp_dir:
        root = Path(temp_dir)
        note_path = root / "daily.md"
        note_path.write_text(f"Fact: connector hardening smoke {smoke_id} is captured once.\n", encoding="utf-8")
        generated_dir = root / "generated"
        generated_dir.mkdir()
        (generated_dir / "loop.md").write_text("Fact: generated output should not recapture.\n", encoding="utf-8")
        local_scan = scan_local_folder((root,))
        with _vnext_store_context(ctx) as store:
            service = VNextConnectorService(store, defer_embeddings=True)
            service.ensure_default_settings()
            service.update_config(
                "telegram",
                enabled=True,
                config_json={"allowed_chat_ids": ["999001"]},
            )
            service.update_config(
                "local_folder",
                enabled=True,
                sync_mode="watch",
                config_json={"paths": [str(root)], "recursive": True, "extensions": [".md", ".txt"]},
            )
            telegram = service.sync_telegram_updates(
                [
                    {
                        "update_id": telegram_update_id,
                        "message": {
                            "message_id": telegram_update_id + 1,
                            "date": 1_778_400_000,
                            "chat": {"id": 999001, "type": "private"},
                            "from": {"id": 1001, "username": "samir"},
                            "text": f"Fact: Telegram hardening smoke {smoke_id} is allowlisted.",
                        },
                    },
                    {
                        "update_id": telegram_update_id + 1,
                        "message": {
                            "message_id": telegram_update_id + 2,
                            "date": 1_778_400_001,
                            "chat": {"id": 777, "type": "private"},
                            "from": {"id": 2002},
                            "text": "Fact: this chat is rejected.",
                        },
                    },
                ],
                allowed_chat_ids=("999001",),
            )
            restarted = VNextConnectorService(store, defer_embeddings=True)
            repeated = restarted.sync_telegram_updates(
                [
                    {
                        "update_id": telegram_update_id,
                        "message": {
                            "message_id": telegram_update_id + 1,
                            "date": 1_778_400_000,
                            "chat": {"id": 999001, "type": "private"},
                            "from": {"id": 1001},
                            "text": f"Fact: Telegram hardening smoke {smoke_id} is allowlisted.",
                        },
                    }
                ],
                allowed_chat_ids=("999001",),
            )
            local_first = restarted.sync_local_folder_scan(
                local_scan,
                default_domain="project",
                default_sensitivity="private",
            )
            local_second = restarted.sync_local_folder_scan(
                local_scan,
                default_domain="project",
                default_sensitivity="private",
            )
            health = restarted.connector_health_all()
            events = store.list_events(target_type="connector", target_id="telegram", limit=25)
    _persist_deferred_embedding_inputs(
        ctx,
        (
            *telegram.deferred_embedding_inputs,
            *repeated.deferred_embedding_inputs,
            *local_first.deferred_embedding_inputs,
            *local_second.deferred_embedding_inputs,
        ),
    )
    health_items = {
        str(item["connector_name"]): item
        for item in _object_list(health.get("items"))
        if isinstance(item, dict) and "connector_name" in item
    }
    gates = {
        "settings_rows_available": all(
            name in health_items for name in ("telegram", "local_folder", "browser_clipper", "agent_output")
        ),
        "telegram_cursor_persisted": telegram.sync_cursor == str(telegram_update_id + 1)
        and repeated.status == "skipped",
        "telegram_rejected_chat_logged": any(event.get("event_type") == "connector.item_rejected" for event in events),
        "local_folder_ignores_generated": local_first.imported_count == 1,
        "local_folder_dedupes_unchanged_restart": local_second.duplicate_count >= 1,
        "health_counts_present": _object_int(health_items.get("telegram", {}).get("items_seen")) >= 2,
    }
    payload = {"status": "passed" if all(gates.values()) else "failed", "smoke": "connector-hardening", "gates": gates}
    if payload["status"] != "passed":
        raise RuntimeError(_json_dumps(payload))
    return _json_dumps(payload)


def _run_vnext_smoke_secret_redaction(ctx: CLIContext, _args: argparse.Namespace) -> str:
    secrets = InMemorySecretProvider(
        {
            "browser.capture_token.default": "clip-token",
        }
    )
    with _vnext_store_context(ctx) as store:
        service = VNextConnectorService(store, secret_provider=secrets, defer_embeddings=True)
        service.update_config("browser_clipper", enabled=True, secret_ref="browser.capture_token.default")
        clip = service.capture_browser_clip(
            {
                "url": "https://example.test/secret-redaction",
                "title": "Secret redaction smoke",
                "selected_text": "Fact: redaction smoke stores content as untrusted evidence.",
                "capture_token": "clip-token",
            }
        )
        sources = store.list_sources(limit=10)
        events = store.list_events(limit=50)
    _persist_deferred_capture_embeddings(ctx, clip)
    serialized = _json_dumps({"sources": sources, "events": events})
    gates = {
        "clip_imported_or_deduped": clip.imported_count + clip.duplicate_count >= 1,
        "browser_token_absent": "clip-token" not in serialized,
        "capture_token_redacted": '"capture_token": "***"' in serialized,
    }
    payload = {"status": "passed" if all(gates.values()) else "failed", "smoke": "secret-redaction", "gates": gates}
    if payload["status"] != "passed":
        raise RuntimeError(_json_dumps(payload))
    return _json_dumps(payload)


def _run_vnext_smoke_dogfood_doctor(ctx: CLIContext, _args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        payload = VNextDoctorService(
            store,
            secret_provider=InMemorySecretProvider({}),
        ).run(fix_safe=True, ci=True)
    gates = {
        "doctor_ran": payload.get("status") in {"pass", "warn"},
        "no_blocking_failures": payload.get("blocking_failure_count") == 0,
        "migration_status_present": isinstance(payload.get("migration_status"), dict),
        "connector_settings_checked": any(
            isinstance(check, dict) and check.get("name") == "connector_settings"
            for check in _object_list(payload.get("checks"))
            if isinstance(check, dict)
        ),
    }
    result = {
        "status": "passed" if all(gates.values()) else "failed",
        "smoke": "dogfood-doctor",
        "gates": gates,
        "doctor": payload,
    }
    if result["status"] != "passed":
        raise RuntimeError(_json_dumps(result))
    return _json_dumps(result)
