from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import time
from uuid import uuid4
# Underscore aliases: the CLI facade copies every name in these modules onto
# ``alicebot_api.cli``, and its public names are pinned.
from alicebot_api.importer_paths import (
    DEFAULT_MAX_CHATGPT_EXPORT_BYTES as _DEFAULT_MAX_CHATGPT_EXPORT_BYTES,
    DEFAULT_MAX_TEXT_FILE_BYTES as _DEFAULT_MAX_TEXT_FILE_BYTES,
    MIB as _MIB,
)
from alicebot_api.vnext_agent_control import (
    AgentIdentity,
    append_policy_events,
    ensure_policy_allowed,
    evaluate_agent_policy,
    summarize_agent_policy_telemetry,
)
from alicebot_api.vnext_capture import VNextCaptureService, VNextCaptureValidationError
from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from alicebot_api.vnext_connectors import (
    VNextConnectorService,
    VNextConnectorValidationError,
    list_connector_definitions,
    load_connector_items_from_file,
    scan_local_folder,
)
from alicebot_api.vnext_dogfooding import VNextDogfoodingService
from alicebot_api.vnext_doctor import VNextDoctorService
from alicebot_api.vnext_projects import ProjectAutomationRequest, VNextProjectService
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_scheduler import SchedulerRunRequest, default_schedule
from alicebot_api.vnext_embeddings import DeferredMemoryEmbedding
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_store import PostgresVNextStore
from .constants import DEFAULT_VNEXT_DEMO_DATASET_PATH, DEMO_SECRET_MARKERS
from .models import CLIContext
from .arguments import _object_dict, _object_int, _object_list
from .shared import (
    _checked_batch_output,
    _json_dumps,
    _persist_deferred_capture_embeddings,
    _persist_deferred_embedding_inputs,
    _scheduler_service,
    _vnext_store_context,
)


def _run_vnext_sources_capture_text(ctx: CLIContext, args: argparse.Namespace) -> str:
    raw_text = " ".join(args.raw_text).strip()
    with _vnext_store_context(ctx) as store:
        result = VNextCaptureService(store, defer_embeddings=True).capture_text(
            raw_text,
            title=args.title,
            domain=args.domain,
            sensitivity=args.sensitivity,
        )
    _persist_deferred_capture_embeddings(ctx, result)
    return _json_dumps(result.to_record())


def _run_vnext_sources_capture_file(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        result = VNextCaptureService(store, defer_embeddings=True).capture_file(
            args.path,
            domain=args.domain,
            sensitivity=args.sensitivity,
        )
    _persist_deferred_capture_embeddings(ctx, result)
    return _json_dumps(result.to_record())


def _file_limit_bytes(args: argparse.Namespace, default_bytes: int) -> int:
    """The per-file import limit in bytes: ``--max-file-mib`` if given, else the default."""

    mib = getattr(args, "max_file_mib", None)
    return default_bytes if mib is None else mib * _MIB


def _run_vnext_sources_import_markdown(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        result = VNextCaptureService(store, defer_embeddings=True).import_markdown_folder(
            args.folder,
            domain=args.domain,
            sensitivity=args.sensitivity,
            max_file_bytes=_file_limit_bytes(args, _DEFAULT_MAX_TEXT_FILE_BYTES),
        )
    _persist_deferred_capture_embeddings(ctx, result)
    return _checked_batch_output(result.to_record())


def _run_vnext_sources_import_chatgpt(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        result = VNextCaptureService(store, defer_embeddings=True).import_chatgpt_export_file(
            args.path,
            domain=args.domain,
            sensitivity=args.sensitivity,
            max_file_bytes=_file_limit_bytes(args, _DEFAULT_MAX_CHATGPT_EXPORT_BYTES),
        )
    _persist_deferred_capture_embeddings(ctx, result)
    return _checked_batch_output(result.to_record())


def _run_vnext_connectors_list(_ctx: CLIContext, _args: argparse.Namespace) -> str:
    payload = {
        "items": [definition.to_record() for definition in list_connector_definitions()],
        "count": len(list_connector_definitions()),
        "order": [definition.name for definition in list_connector_definitions()],
    }
    return _json_dumps(payload)


def _run_vnext_connectors_ingest(ctx: CLIContext, args: argparse.Namespace) -> str:
    if str(args.connector_name).strip().casefold() == "telegram":
        raise VNextConnectorValidationError(
            "Telegram payloads must use POST /v0/vnext/connectors/telegram/sync "
            "so chat allowlist enforcement cannot be bypassed"
        )
    items = load_connector_items_from_file(args.payload_path)
    with _vnext_store_context(ctx) as store:
        result = VNextConnectorService(store, defer_embeddings=True).sync_items(
            args.connector_name,
            items,
            default_domain=args.domain,
            default_sensitivity=args.sensitivity,
        )
    _persist_deferred_capture_embeddings(ctx, result)
    return _checked_batch_output(result.to_record())


def _path_identity(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return str(Path(value).expanduser().resolve(strict=False))
    except OSError:
        return value.strip()


def _run_vnext_connectors_configure(ctx: CLIContext, args: argparse.Namespace) -> str:
    config_json: dict[str, object] = {}
    if getattr(args, "allowed_chat_id", None):
        config_json["allowed_chat_ids"] = list(args.allowed_chat_id)
    if getattr(args, "path", None):
        config_json["paths"] = list(args.path)
    if getattr(args, "recursive", None) is not None:
        config_json["recursive"] = bool(args.recursive)
    if getattr(args, "extension", None):
        config_json["extensions"] = list(args.extension)
    if getattr(args, "ignore_pattern", None):
        config_json["ignore_patterns"] = list(args.ignore_pattern)
    with _vnext_store_context(ctx) as store:
        service = VNextConnectorService(store)
        if args.connector_name == "local_folder" and (
            getattr(args, "merge_paths", False) or getattr(args, "remove_paths", False)
        ):
            existing_config = service.get_config("local_folder")
            existing_json = existing_config.get("config_json")
            if isinstance(existing_json, dict):
                config_json = {**existing_json, **config_json}
            existing_paths = [
                str(path)
                for path in (existing_json.get("paths", []) if isinstance(existing_json, dict) else [])
                if isinstance(path, str)
            ]
            requested_paths = [str(path) for path in getattr(args, "path", []) if isinstance(path, str)]
            if getattr(args, "merge_paths", False):
                merged_paths = list(dict.fromkeys([*existing_paths, *requested_paths]))
            else:
                remove_keys = set(requested_paths)
                remove_keys.update(key for path in requested_paths if (key := _path_identity(path)) is not None)
                merged_paths = [
                    path
                    for path in existing_paths
                    if path not in remove_keys and (_path_identity(path) or path) not in remove_keys
                ]
            config_json["paths"] = merged_paths
            if getattr(args, "remove_paths", False) and getattr(args, "enabled", None) is None:
                args.enabled = bool(merged_paths)
        payload = service.update_config(
            args.connector_name,
            enabled=args.enabled,
            default_domain=args.domain,
            default_sensitivity=args.sensitivity,
            secret_ref=args.secret_ref,
            sync_mode=getattr(args, "sync_mode", None),
            poll_interval_seconds=getattr(args, "poll_interval_seconds", None),
            config_json=config_json,
        )
    return _json_dumps(payload)


def _run_vnext_connectors_status(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        service = VNextConnectorService(store)
        payload: object
        if args.connector_name:
            payload = {
                "config": service.get_config(args.connector_name),
                "health": service.connector_health(args.connector_name),
            }
        else:
            payload = service.connector_health_all()
    return _json_dumps(payload)


def _run_vnext_connectors_health(ctx: CLIContext, _args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        payload = VNextConnectorService(store).connector_health_all()
    return _json_dumps(payload)


def _run_vnext_local_folder_sync(ctx: CLIContext, args: argparse.Namespace) -> str:
    paths = list(args.path)
    if not paths:
        with _vnext_store_context(ctx) as store:
            config = VNextConnectorService(store).get_config("local_folder")
            config_json = _object_dict(config.get("config_json"))
            configured_paths = _object_list(config_json.get("paths"))
            paths = [str(path) for path in configured_paths if isinstance(path, str)]
    scan = scan_local_folder(
        paths,
        recursive=not args.no_recursive,
        extensions=tuple(args.extension),
        ignore_patterns=tuple(args.ignore_pattern),
    )
    with _vnext_store_context(ctx) as store:
        result = VNextConnectorService(
            store,
            defer_embeddings=True,
        ).sync_local_folder_scan(
            scan,
            default_domain=args.domain,
            default_sensitivity=args.sensitivity,
        )
    _persist_deferred_capture_embeddings(ctx, result)
    return _checked_batch_output(result.to_record())


def _run_vnext_local_folder_watch(ctx: CLIContext, args: argparse.Namespace) -> str:
    if args.once:
        return _run_vnext_local_folder_sync(ctx, args)
    runs: list[dict[str, object]] = []
    for _index in range(args.max_runs):
        runs.append(json.loads(_run_vnext_local_folder_sync(ctx, args)))
        time.sleep(args.interval_seconds)
    return _json_dumps({"status": "stopped", "runs": runs, "watch_mode": "polling"})


def _run_vnext_browser_clip(ctx: CLIContext, args: argparse.Namespace) -> str:
    selected_text = args.selected_text
    page_text = args.page_text
    user_note = args.user_note
    if args.file:
        page_text = Path(args.file).read_text(encoding="utf-8")
    with _vnext_store_context(ctx) as store:
        result = VNextConnectorService(store, defer_embeddings=True).capture_browser_clip(
            {
                "url": args.url,
                "title": args.title,
                "selected_text": selected_text,
                "page_text": page_text,
                "user_note": user_note,
                "capture_token": args.capture_token,
                "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            },
            default_domain=args.domain,
            default_sensitivity=args.sensitivity,
        )
    _persist_deferred_capture_embeddings(ctx, result)
    return _json_dumps(result.to_record())


def _run_vnext_agents_ingest_output(ctx: CLIContext, args: argparse.Namespace) -> str:
    content = Path(args.file).read_text(encoding="utf-8") if args.file else " ".join(args.content or ()).strip()
    if not content:
        raise VNextConnectorValidationError("agent output content is required")
    identity = AgentIdentity.from_payload(
        {
            "agent_id": args.agent_id,
            "agent_type": args.agent_type,
            "agent_run_id": args.agent_run_id,
            "task_id": args.task_id,
            "project_scope": args.project_scope,
            "permission_profile": args.permission_profile,
        }
    )
    if identity is None:  # Defensive guard for malformed direct Namespace callers.
        raise VNextConnectorValidationError("agent identity is required")
    with _vnext_store_context(ctx) as store:
        decision = evaluate_agent_policy(
            identity=identity,
            action="source.capture",
            domains=(args.domain,),
            sensitivity_allowed=(args.sensitivity,),
            project_scope=tuple(args.project_scope),
            write_policy="proposal_only" if args.propose_memory else None,
        )
        append_policy_events(
            store, identity=identity, decision=decision, target_type="connector", target_id="agent_output"
        )
        ensure_policy_allowed(decision)
        result = VNextConnectorService(store, defer_embeddings=True).ingest_agent_output(
            {
                "agent_id": args.agent_id,
                "agent_type": args.agent_type,
                "agent_run_id": args.agent_run_id,
                "task_id": args.task_id,
                "project_scope": args.project_scope,
                "title": args.title,
                "content": content,
                "output_type": args.output_type,
                "domain": args.domain,
                "sensitivity": args.sensitivity,
                "source_refs": args.source_ref,
                "rationale": args.rationale,
                "propose_memory": args.propose_memory,
            },
            policy_decision=decision.to_record(),
        )
    _persist_deferred_capture_embeddings(
        ctx,
        result,
        actor_type="agent",
        actor_id=identity.agent_id,
        trace_id=decision.trace_id,
    )
    return _json_dumps(result.to_record())


def _run_vnext_dogfooding_dashboard(ctx: CLIContext, _args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        payload = VNextDogfoodingService(store).dashboard()
    return _json_dumps(payload)


def _run_vnext_doctor(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        payload = VNextDoctorService(store).run(fix_safe=args.fix_safe, ci=args.ci)
    output = _json_dumps(payload)
    if _object_int(payload.get("blocking_failure_count")) > 0:
        print(output)
        raise VNextConnectorValidationError("vNext doctor found blocking failures")
    return output


def _run_vnext_migrations_status(ctx: CLIContext, _args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        payload = VNextDoctorService(store).migration_status()
    return _json_dumps(payload)


def _load_vnext_demo_dataset(path: str | Path) -> JsonObject:
    dataset_path = Path(path).expanduser().resolve()
    payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise VNextCaptureValidationError("vNext demo dataset root must be an object")
    if not isinstance(payload.get("dataset_id"), str) or not str(payload["dataset_id"]).strip():
        raise VNextCaptureValidationError("vNext demo dataset requires dataset_id")
    serialized = json.dumps(payload, sort_keys=True).casefold()
    leaked_markers = [marker for marker in DEMO_SECRET_MARKERS if marker in serialized]
    if leaked_markers:
        raise VNextCaptureValidationError(
            f"vNext demo dataset contains forbidden marker(s): {', '.join(leaked_markers)}"
        )
    return payload


def _demo_tag(dataset_id: str) -> JsonObject:
    return {"demo": True, "demo_dataset_id": dataset_id}


def _reset_vnext_demo_dataset(store: PostgresVNextStore, *, dataset_id: str) -> JsonObject:
    with store.conn.cursor() as cur:
        cur.execute(
            """
            WITH demo_sources AS (
              SELECT id::text AS id
              FROM sources
              WHERE metadata_json ->> 'demo_dataset_id' = %s
                 OR metadata_json -> 'raw_payload' ->> 'demo_dataset_id' = %s
            ),
            demo_artifacts AS (
              SELECT id::text AS id
              FROM generated_artifacts
              WHERE metadata_json ->> 'demo_dataset_id' = %s
                 OR metadata_json ->> 'source_id' IN (SELECT id FROM demo_sources)
            ),
            reset_sources AS (
              UPDATE sources
              SET deleted_at = COALESCE(deleted_at, clock_timestamp())
              WHERE metadata_json ->> 'demo_dataset_id' = %s
                 OR metadata_json -> 'raw_payload' ->> 'demo_dataset_id' = %s
              RETURNING id
            ),
            reset_memories AS (
              UPDATE memories
              SET status = 'archived',
                  memory_key = memory_key || '#demo-reset:' || left(replace(gen_random_uuid()::text, '-', ''), 12),
                  metadata_json = metadata_json || %s::jsonb,
                  updated_at = clock_timestamp(),
                  deleted_at = COALESCE(deleted_at, clock_timestamp())
              WHERE deleted_at IS NULL
                AND (
                  metadata_json ->> 'demo_dataset_id' = %s
                  OR metadata_json ->> 'source_id' IN (SELECT id FROM demo_sources)
                  OR metadata_json ->> 'artifact_id' IN (SELECT id FROM demo_artifacts)
                )
              RETURNING id
            ),
            reset_artifacts AS (
              UPDATE generated_artifacts
              SET status = 'archived'
              WHERE metadata_json ->> 'demo_dataset_id' = %s
                 OR metadata_json ->> 'source_id' IN (SELECT id FROM demo_sources)
              RETURNING id
            ),
            reset_open_loops AS (
              UPDATE open_loops
              SET status = 'dismissed',
                  resolved_at = COALESCE(resolved_at, clock_timestamp()),
                  resolution_note = COALESCE(resolution_note, 'Reset synthetic demo dataset.'),
                  metadata_json = metadata_json || %s::jsonb,
                  updated_at = clock_timestamp()
              WHERE metadata_json ->> 'demo_dataset_id' = %s
                 OR source_id::text IN (SELECT id FROM demo_sources)
              RETURNING id
            ),
            reset_projects AS (
              UPDATE projects
              SET status = 'archived',
                  metadata_json = metadata_json || %s::jsonb,
                  updated_at = clock_timestamp()
              WHERE metadata_json ->> 'demo_dataset_id' = %s
              RETURNING id
            )
            SELECT
              (SELECT count(*) FROM reset_sources) AS sources,
              (SELECT count(*) FROM reset_memories) AS memories,
              (SELECT count(*) FROM reset_artifacts) AS artifacts,
              (SELECT count(*) FROM reset_open_loops) AS open_loops,
              (SELECT count(*) FROM reset_projects) AS projects
            """,
            (
                dataset_id,
                dataset_id,
                dataset_id,
                dataset_id,
                dataset_id,
                json.dumps({"demo_reset_at": datetime.now(UTC).isoformat()}),
                dataset_id,
                dataset_id,
                json.dumps({"demo_reset_at": datetime.now(UTC).isoformat()}),
                dataset_id,
                json.dumps({"demo_reset_at": datetime.now(UTC).isoformat()}),
                dataset_id,
            ),
        )
        row = cur.fetchone() or {}
    append_event(
        store,
        event_type="demo.dataset_reset",
        actor_type="system",
        target_type="demo_dataset",
        target_id=dataset_id,
        payload={"dataset_id": dataset_id, "reset_counts": dict(row)},
    )
    return {"status": "reset", "dataset_id": dataset_id, "reset_counts": dict(row)}


def _tag_demo_candidate_memories(store: PostgresVNextStore, *, dataset_id: str, source_ids: set[str]) -> int:
    updated = 0
    for memory in store.list_memories(status="candidate"):
        metadata = _object_dict(memory.get("metadata_json"))
        if str(metadata.get("source_id") or "") not in source_ids:
            continue
        store.update_memory(
            memory_id=str(memory["id"]),
            patch={"metadata_json": {**metadata, **_demo_tag(dataset_id)}},
            actor_type="system",
        )
        updated += 1
    return updated


def _tag_demo_artifact(store: PostgresVNextStore, *, artifact_id: str, dataset_id: str) -> None:
    artifact = store.get_artifact(artifact_id)
    if artifact is None:
        return
    metadata = _object_dict(artifact.get("metadata_json"))
    with store.conn.cursor() as cur:
        cur.execute(
            """
            UPDATE generated_artifacts
            SET metadata_json = %s::jsonb
            WHERE id = %s::uuid
            """,
            (json.dumps({**metadata, **_demo_tag(dataset_id)}), artifact_id),
        )


def _run_vnext_demo_reset(ctx: CLIContext, args: argparse.Namespace) -> str:
    dataset_id = args.dataset_id
    if not dataset_id and getattr(args, "fixture", None):
        dataset_id = str(_load_vnext_demo_dataset(args.fixture)["dataset_id"])
    if not dataset_id:
        dataset_id = str(_load_vnext_demo_dataset(DEFAULT_VNEXT_DEMO_DATASET_PATH)["dataset_id"])
    with _vnext_store_context(ctx) as store:
        payload = _reset_vnext_demo_dataset(store, dataset_id=dataset_id)
    return _json_dumps(payload)


def _run_vnext_demo_load(ctx: CLIContext, args: argparse.Namespace) -> str:
    dataset = _load_vnext_demo_dataset(args.fixture)
    dataset_id = str(dataset["dataset_id"])
    created_source_ids: set[str] = set()
    created_artifact_ids: list[str] = []
    created_project_ids: list[str] = []
    created_open_loop_ids: list[str] = []
    deferred_embedding_inputs: list[DeferredMemoryEmbedding] = []

    with _vnext_store_context(ctx) as store:
        if args.reset:
            _reset_vnext_demo_dataset(store, dataset_id=dataset_id)
        connector_service = VNextConnectorService(store, defer_embeddings=True)
        connector_service.ensure_default_settings()
        for project in _object_list(dataset.get("projects")):
            if not isinstance(project, dict):
                continue
            row = store.create_project(
                {
                    "name": str(project.get("name") or "Alice vNext Demo"),
                    "slug": f"demo-{dataset_id[:32]}-{uuid4().hex[:8]}"[:80],
                    "status": str(project.get("status") or "active"),
                    "description": "Synthetic public alpha demo project.",
                    "current_state": "Synthetic demo state for source review, project updates, and agent integration.",
                    "domain": str(project.get("domain") or "project"),
                    "sensitivity": str(project.get("sensitivity") or "private"),
                    "metadata_json": {**_demo_tag(dataset_id), "fixture_project_id": project.get("id")},
                },
                actor_type="system",
            )
            created_project_ids.append(str(row["id"]))

        capture_service = VNextCaptureService(store, defer_embeddings=True)
        for source in _object_list(dataset.get("sources")):
            if not isinstance(source, dict):
                continue
            capture_result = capture_service.capture_text(
                str(source.get("raw_text") or ""),
                title=str(source.get("title") or "Synthetic demo source"),
                domain=str(source.get("domain") or "project"),
                sensitivity=str(source.get("sensitivity") or "private"),
                metadata_json={**_demo_tag(dataset_id), "fixture_source_type": source.get("source_type")},
            )
            if capture_result.source_id is not None:
                created_source_ids.add(capture_result.source_id)
            deferred_embedding_inputs.extend(capture_result.deferred_embedding_inputs)

        connector_payloads = (
            dataset.get("connector_payloads") if isinstance(dataset.get("connector_payloads"), dict) else {}
        )
        browser_payload = connector_payloads.get("browser_clipper") if isinstance(connector_payloads, dict) else None
        if isinstance(browser_payload, dict) and isinstance(browser_payload.get("items"), list):
            browser_result = connector_service.sync_items(
                "browser_clipper",
                [{**item, **_demo_tag(dataset_id)} for item in browser_payload["items"] if isinstance(item, dict)],
                default_domain="project",
                default_sensitivity="private",
                use_cursor=False,
            )
            created_source_ids.update(browser_result.source_ids)
            deferred_embedding_inputs.extend(browser_result.deferred_embedding_inputs)

        telegram_payload = connector_payloads.get("telegram") if isinstance(connector_payloads, dict) else None
        if isinstance(telegram_payload, dict) and isinstance(telegram_payload.get("items"), list):
            telegram_result = connector_service.sync_telegram_updates(
                [{**item, **_demo_tag(dataset_id)} for item in telegram_payload["items"] if isinstance(item, dict)],
                allowed_chat_ids=("9001001",),
                default_domain="personal",
                default_sensitivity="private",
            )
            created_source_ids.update(telegram_result.source_ids)
            deferred_embedding_inputs.extend(telegram_result.deferred_embedding_inputs)

        project_id = created_project_ids[0] if created_project_ids else None
        agent_outputs = _object_list(dataset.get("agent_outputs"))
        fixture_agent_output = next((item for item in agent_outputs if isinstance(item, dict)), {})
        identity = AgentIdentity(
            agent_id=str(fixture_agent_output.get("agent_id") or "openclaw"),
            agent_type=str(fixture_agent_output.get("agent_type") or "coding_agent"),
            agent_run_id=str(fixture_agent_output.get("agent_run_id") or f"demo-{dataset_id}"),
            task_id=str(fixture_agent_output.get("task_id") or "demo-public-alpha"),
            project_scope=tuple(
                str(value) for value in (_object_list(fixture_agent_output.get("project_scope")) or ["Alice"])
            ),
            permission_profile=str(fixture_agent_output.get("permission_profile") or "project_scoped_agent"),
        )
        store.upsert_agent_identity(
            {
                "agent_id": identity.agent_id,
                "agent_type": identity.agent_type,
                "permission_profile": identity.permission_profile,
                "display_name": "OpenClaw Demo",
                "project_scope_json": list(identity.project_scope),
                "metadata_json": {**_demo_tag(dataset_id), "last_agent_run_id": identity.agent_run_id},
            },
            actor_type="agent",
        )
        agent_decision = evaluate_agent_policy(
            identity=identity,
            action="source.capture",
            domains=("project",),
            sensitivity_allowed=("private",),
            project_scope=identity.project_scope,
            write_policy="proposal_only",
        )
        append_policy_events(
            store, identity=identity, decision=agent_decision, target_type="connector", target_id="agent_output"
        )
        ensure_policy_allowed(agent_decision)
        agent_result = connector_service.ingest_agent_output(
            {
                "agent_id": identity.agent_id,
                "agent_type": identity.agent_type,
                "agent_run_id": identity.agent_run_id,
                "task_id": identity.task_id,
                "project_scope": list(identity.project_scope),
                "title": str(fixture_agent_output.get("title") or "OpenClaw public alpha demo sprint summary"),
                "content": str(
                    fixture_agent_output.get("content")
                    or (
                        "Decision: Public alpha agents should request scoped Alice context before acting.\n"
                        "TODO: Review the demo source-to-artifact trace in /vnext."
                    )
                ),
                "output_type": str(fixture_agent_output.get("output_type") or "sprint_summary"),
                "domain": str(fixture_agent_output.get("domain") or "project"),
                "sensitivity": str(fixture_agent_output.get("sensitivity") or "private"),
                "propose_memory": bool(fixture_agent_output.get("propose_memory", True)),
                **_demo_tag(dataset_id),
            },
            policy_decision=agent_decision.to_record(),
        )
        if agent_result.source_id:
            created_source_ids.add(agent_result.source_id)
        deferred_embedding_inputs.extend(agent_result.deferred_embedding_inputs)
        if agent_result.artifact_id:
            created_artifact_ids.append(agent_result.artifact_id)
            _tag_demo_artifact(store, artifact_id=agent_result.artifact_id, dataset_id=dataset_id)

        blocked_decision = evaluate_agent_policy(
            identity=identity,
            action="context_pack.request",
            domains=("family", "health"),
            sensitivity_allowed=("private", "highly_sensitive"),
            project_scope=identity.project_scope,
        )
        append_policy_events(
            store, identity=identity, decision=blocked_decision, target_type="context_pack", target_id=dataset_id
        )

        _tag_demo_candidate_memories(store, dataset_id=dataset_id, source_ids=created_source_ids)
        if project_id is not None and created_source_ids:
            loop = store.create_open_loop(
                {
                    "title": "Review public alpha demo trace",
                    "description": "Synthetic open loop created by the demo dataset loader.",
                    "priority": "normal",
                    "source_id": sorted(created_source_ids)[0],
                    "project_id": project_id,
                    "domain": "project",
                    "sensitivity": "private",
                    "metadata_json": _demo_tag(dataset_id),
                },
                actor_type="system",
            )
            created_open_loop_ids.append(str(loop["id"]))

        daily = VNextBrainService(store).generate_daily_brief(
            BrainArtifactRequest(
                domains=("project",),
                sensitivity_allowed=("public", "internal", "private", "unknown"),
                generated_for="2026-05-12",
                metadata_json=_demo_tag(dataset_id),
            )
        )
        created_artifact_ids.append(str(daily["id"]))
        store.create_artifact_quality_rating(
            {
                "artifact_id": str(daily["id"]),
                "reviewer_id": "demo",
                "usefulness": 5,
                "accuracy": 5,
                "source_grounding": 5,
                "novel_connections": 4,
                "actionability": 4,
                "hallucination_risk": 1,
                "verbosity": "right_sized",
                "metadata_json": _demo_tag(dataset_id),
            },
            actor_type="user",
        )
        if project_id is not None:
            project_update = VNextProjectService(store).generate_project_update_candidate(
                ProjectAutomationRequest(
                    domains=("project",),
                    sensitivity_allowed=("public", "internal", "private", "unknown"),
                    project_id=project_id,
                    metadata_json=_demo_tag(dataset_id),
                )
            )
            created_artifact_ids.append(str(project_update["id"]))
        scheduler = _scheduler_service(store)
        scheduler.configure_workflow(
            workflow_type="daily_brief",
            enabled=True,
            paused=False,
            schedule_json=default_schedule("daily_brief"),
            timezone="UTC",
            actor_type="system",
        )
        scheduled = scheduler.run_now(
            SchedulerRunRequest(
                workflow_type="daily_brief",
                domains=("project",),
                sensitivity_allowed=("public", "internal", "private", "unknown"),
                generated_for="2026-05-12",
                triggered_by="user",
                options={"generation_mode": "deterministic"},
            )
        )
        scheduled_artifact = _object_dict(scheduled.get("artifact"))
        if scheduled_artifact.get("id"):
            artifact_id = str(scheduled_artifact["id"])
            created_artifact_ids.append(artifact_id)
            _tag_demo_artifact(store, artifact_id=artifact_id, dataset_id=dataset_id)
        health = connector_service.connector_health_all()
        telemetry = summarize_agent_policy_telemetry(
            agent_events=store.list_agent_events(agent_id="openclaw", limit=100),
            artifacts=store.list_artifacts(limit=100),
            memories=store.list_memories(status=None),
        )
        append_event(
            store,
            event_type="demo.dataset_loaded",
            actor_type="system",
            target_type="demo_dataset",
            target_id=dataset_id,
            payload={
                "dataset_id": dataset_id,
                "source_ids": sorted(created_source_ids),
                "artifact_ids": created_artifact_ids,
                "project_ids": created_project_ids,
            },
        )

    _persist_deferred_embedding_inputs(ctx, deferred_embedding_inputs)
    payload = {
        "status": "loaded",
        "dataset_id": dataset_id,
        "source_count": len(created_source_ids),
        "artifact_count": len(created_artifact_ids),
        "project_count": len(created_project_ids),
        "open_loop_count": len(created_open_loop_ids),
        "agent_activity_visible": _object_int(telemetry.get("total_agent_events")) > 0,
        "policy_block_recorded": blocked_decision.decision == "blocked",
        "connector_health_count": health.get("count"),
    }
    return _json_dumps(payload)


def _run_vnext_artifact_insight_feedback(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        payload = VNextDogfoodingService(store).record_insight_feedback(
            artifact_id=args.artifact_id,
            useful_insight=args.useful_insight,
            surfaced_missed=args.surfaced_missed,
            comments=args.comments,
        )
    return _json_dumps(payload)
