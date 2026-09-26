from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import import_module
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import Request
from fastapi.responses import Response
from pydantic import ValidationError
import alicebot_api.main as main_module
from alicebot_api.routers import memories_legacy as memories_legacy_router
from alicebot_api.routers import providers as providers_router
import alicebot_api.openapi_operation_contracts as openapi_contracts
from alicebot_api.config import Settings
from alicebot_api.routers import _api_shared as api_shared
from alicebot_api.routers import vnext_memories as vnext_memories_router
from alicebot_api.routers import vnext_projects as vnext_projects_router
from alicebot_api.routers import vnext_review as vnext_review_router
from alicebot_api.artifacts import TaskArtifactNotFoundError
from alicebot_api.compiler import CompiledTraceRun
from alicebot_api.contracts import AdmissionDecisionOutput
from alicebot_api.embedding import (
    EmbeddingConfigValidationError,
    MemoryEmbeddingNotFoundError,
    MemoryEmbeddingValidationError,
    TaskArtifactChunkEmbeddingNotFoundError,
    TaskArtifactChunkEmbeddingValidationError,
)
from alicebot_api.entity import EntityNotFoundError, EntityValidationError
from alicebot_api.entity_edge import EntityEdgeValidationError
from alicebot_api.memory import (
    MemoryAdmissionValidationError,
    MemoryReviewNotFoundError,
    OpenLoopNotFoundError,
    OpenLoopValidationError,
)
from alicebot_api.response_generation import ResponseFailure
from alicebot_api.response_jobs import ResponseJobLookup
from alicebot_api.semantic_retrieval import (
    SemanticArtifactChunkRetrievalValidationError,
    SemanticMemoryRetrievalValidationError,
)
from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_connections import VNextConnectionService
from alicebot_api.vnext_connectors import ConnectorSyncResult
from alicebot_api.vnext_contradictions import VNextContradictionService
from alicebot_api.vnext_projects import VNextProjectService
from alicebot_api.vnext_queue import QueueProcessResult
from alicebot_api.vnext_scheduler_runtime import daemon_status
from alicebot_api.vnext_store import (
    ARTIFACT_COLUMNS,
    BELIEF_COLUMNS,
    EVENT_LOG_COLUMNS,
    GRAPH_EDGE_COLUMNS,
    OPEN_LOOP_COLUMNS,
    QUALITY_RATING_COLUMNS,
    SOURCE_COLUMNS,
    TASK_COLUMNS,
)


def _registered_route_paths() -> set[str]:
    return set(main_module.app.openapi()["paths"])


def _openapi_schema_accepts(value: object, schema: dict[str, object]) -> bool:
    """Validate the JSON-Schema subset emitted by response contracts."""

    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and not any(
        isinstance(candidate, dict) and _openapi_schema_accepts(value, candidate) for candidate in any_of
    ):
        return False
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        matching_variants = sum(
            1 for candidate in one_of if isinstance(candidate, dict) and _openapi_schema_accepts(value, candidate)
        )
        if matching_variants != 1:
            return False

    schema_type = schema.get("type")
    if schema_type == "null":
        return value is None
    if schema_type == "boolean" and not isinstance(value, bool):
        return False
    if schema_type == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        return False
    if schema_type == "string" and not isinstance(value, str):
        return False
    if schema_type == "array":
        if not isinstance(value, list):
            return False
        item_schema = schema.get("items")
        if isinstance(item_schema, dict) and not all(_openapi_schema_accepts(item, item_schema) for item in value):
            return False
    if schema_type == "object":
        if not isinstance(value, dict):
            return False
        required = schema.get("required")
        if isinstance(required, list) and not set(required) <= set(value):
            return False
        properties = schema.get("properties")
        if isinstance(properties, dict):
            if schema.get("additionalProperties") is False and not set(value) <= set(properties):
                return False
            for field, field_value in value.items():
                field_schema = properties.get(field)
                if isinstance(field_schema, dict) and not _openapi_schema_accepts(field_value, field_schema):
                    return False
    return True


def _openapi_object_properties(schema: dict[str, object]) -> dict[str, object]:
    properties = schema.get("properties")
    assert isinstance(properties, dict)
    assert all(isinstance(field, str) for field in properties)
    return properties


def _openapi_required_fields(schema: dict[str, object]) -> set[str]:
    required = schema.get("required")
    assert isinstance(required, list)
    assert all(isinstance(field, str) for field in required)
    return set(required)


class _FakeResponseGenerationJobStore:
    jobs: dict[tuple[str, str], dict[str, object]] = {}

    def __init__(self, _conn: object) -> None:
        pass

    @classmethod
    def reset(cls) -> None:
        cls.jobs = {}

    def create_or_get_for_update(self, **kwargs) -> ResponseJobLookup:
        key = (kwargs["endpoint"], kwargs["idempotency_key"])
        existing = self.jobs.get(key)
        if existing is not None:
            return ResponseJobLookup(job=existing, created=False)  # type: ignore[arg-type]
        now = datetime.now(UTC)
        job: dict[str, object] = {
            "id": uuid4(),
            "user_id": kwargs["user_id"],
            "workspace_id": kwargs["workspace_id"],
            "endpoint": kwargs["endpoint"],
            "idempotency_key_hash": "0" * 64,
            "idempotency_key_preview": kwargs["idempotency_key"][:12],
            "request_fingerprint_sha256": kwargs["request_fingerprint_sha256"],
            "state": "pending",
            "lease_token": None,
            "lease_expires_at": None,
            "provider_call_started_at": None,
            "user_event_id": None,
            "user_event_sequence_no": None,
            "response_status_code": None,
            "response_payload": None,
            "error_payload": None,
            "completed_at": None,
            "created_at": now,
            "updated_at": now,
        }
        self.jobs[key] = job
        return ResponseJobLookup(job=job, created=True)  # type: ignore[arg-type]

    def get_for_update(self, **kwargs):
        return self.jobs.get((kwargs["endpoint"], kwargs["idempotency_key"]))

    def claim_pending(self, **kwargs):
        job = next(job for job in self.jobs.values() if job["id"] == kwargs["job_id"])
        job.update(
            state="running",
            lease_token=kwargs["lease_token"],
            user_event_id=kwargs["user_event_id"],
            user_event_sequence_no=kwargs["user_event_sequence_no"],
        )
        return job

    def fail_pending(self, **kwargs):
        job = next(job for job in self.jobs.values() if job["id"] == kwargs["job_id"])
        job.update(
            state="failed",
            response_status_code=kwargs["status_code"],
            error_payload=kwargs["error_payload"],
            completed_at=datetime.now(UTC),
        )
        return job

    def finalize(self, **kwargs):
        job = next(job for job in self.jobs.values() if job["id"] == kwargs["job_id"])
        job.update(
            state=kwargs["state"],
            response_status_code=kwargs["status_code"],
            response_payload=kwargs["payload"] if kwargs["state"] == "succeeded" else None,
            error_payload=kwargs["payload"] if kwargs["state"] == "failed" else None,
            completed_at=datetime.now(UTC),
        )
        return job

    def fail_if_abandoned(self, **_kwargs):
        return None


def test_healthcheck_reports_ok_when_database_is_reachable(monkeypatch) -> None:
    settings = Settings(
        app_env="test",
        database_url="postgresql://db",
        redis_url="redis://alicebot:supersecret@cache:6379/0",
        s3_endpoint_url="http://object-store",
        healthcheck_timeout_seconds=7,
    )
    ping_calls: list[tuple[str, int]] = []

    def fake_get_settings() -> Settings:
        return settings

    def fake_ping_database(database_url: str, timeout_seconds: int) -> bool:
        ping_calls.append((database_url, timeout_seconds))
        return True

    monkeypatch.setattr(main_module, "get_settings", fake_get_settings)
    monkeypatch.setattr(main_module, "ping_database", fake_ping_database)

    response = main_module.healthcheck()

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload == {
        "status": "ok",
        "environment": "test",
        "services": {
            "database": {"status": "ok"},
            "redis": {"status": "not_checked", "url": "redis://cache:6379/0"},
            "object_storage": {"status": "not_checked"},
        },
    }
    assert "endpoint_url" not in payload["services"]["object_storage"]
    assert ping_calls == [("postgresql://db", 7)]


def test_healthcheck_reports_degraded_when_database_is_unreachable(monkeypatch) -> None:
    settings = Settings(
        app_env="test",
        database_url="postgresql://db",
        redis_url="redis://alicebot:supersecret@cache:6379/0",
        s3_endpoint_url="http://object-store",
        healthcheck_timeout_seconds=4,
    )

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ping_database", lambda *_args, **_kwargs: False)

    response = main_module.healthcheck()

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "status": "degraded",
        "environment": "test",
        "services": {
            "database": {"status": "unreachable"},
            "redis": {"status": "not_checked", "url": "redis://cache:6379/0"},
            "object_storage": {"status": "not_checked"},
        },
    }
    assert "endpoint_url" not in json.loads(response.body)["services"]["object_storage"]


def test_healthcheck_route_is_registered() -> None:
    route_paths = _registered_route_paths()

    assert "/healthz" in route_paths
    assert "/v0/context/compile" in route_paths
    assert "/v0/responses" not in route_paths
    assert "/v1/runtime/invoke" in route_paths
    assert "/v0/memories/admit" in route_paths
    assert "/v0/open-loops" in route_paths
    assert "/v0/open-loops/{open_loop_id}" in route_paths
    assert "/v0/open-loops/{open_loop_id}/status" in route_paths
    assert "/v0/consents" in route_paths
    assert "/v0/policies" in route_paths
    assert "/v0/policies/{policy_id}" in route_paths
    assert "/v0/policies/evaluate" in route_paths


def test_request_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="unexpected"):
        memories_legacy_router.CreateThreadRequest.model_validate(
            {
                "user_id": str(uuid4()),
                "title": "Strict request",
                "unexpected": True,
            }
        )


def test_openapi_has_concrete_success_contracts_and_accurate_statuses() -> None:
    route_paths = _registered_route_paths()
    schema = main_module.app.openapi()
    components = schema["components"]["schemas"]
    operations_by_key = {
        (method.upper(), path): operation
        for path, path_item in schema["paths"].items()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }
    operations = list(operations_by_key.values())

    assert len(operations) == 183
    assert all(operation.get("tags") for operation in operations)
    assert all(operation.get("description") for operation in operations)
    assert all("default" in operation["responses"] for operation in operations)
    success_schemas = [
        json_body["schema"]
        for operation in operations
        for status, response in operation["responses"].items()
        if status.startswith("2")
        for json_body in [response.get("content", {}).get("application/json", {})]
    ]
    assert len(success_schemas) == 187
    assert "APIJsonDocument" not in components
    assert all(document.get("$ref", "").startswith("#/components/schemas/") for document in success_schemas)
    resolved_success_schemas = [components[document["$ref"].rsplit("/", 1)[-1]] for document in success_schemas]
    assert all(document.get("type") == "object" for document in resolved_success_schemas)
    assert all(document.get("properties") for document in resolved_success_schemas)

    exact_keys = set(main_module._OPENAPI_EXACT_RESPONSE_CONTRACTS) & set(operations_by_key)
    operation_registry = {
        key: value for key, value in main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS.items() if key in operations_by_key
    }
    polymorphic_operations = {
        key: value
        for key, value in main_module.OPENAPI_INTENTIONALLY_POLYMORPHIC_OPERATIONS.items()
        if key in operations_by_key
    }
    coverage_report = {
        "exact": sorted(exact_keys),
        "per_operation": sorted(operation_registry),
        "intentionally_polymorphic": sorted(polymorphic_operations),
    }
    assert exact_keys | set(operation_registry) == set(operations_by_key), coverage_report
    assert exact_keys.isdisjoint(operation_registry), coverage_report
    assert len(exact_keys) == 42
    assert len(operation_registry) == 141
    assert 0 < len(polymorphic_operations) <= 3
    assert set(polymorphic_operations) <= set(operation_registry)
    assert all(reason.strip() for reason in polymorphic_operations.values())

    source_verified_operations = main_module.OPENAPI_SOURCE_VERIFIED_OPERATIONS & set(operations_by_key)
    open_response_operations = main_module.OPENAPI_OPEN_RESPONSE_OPERATIONS & set(operations_by_key)
    assert source_verified_operations | open_response_operations == set(operation_registry)
    assert source_verified_operations.isdisjoint(open_response_operations)
    assert set(polymorphic_operations) <= open_response_operations
    assert not set(openapi_contracts.PERMANENTLY_REMOVED_OPENAPI_OPERATIONS) & set(operations_by_key)

    broad_components = {
        "OperationsResponse",
        "VNextMemoryAPIResponse",
        "ContinuityAPIResponse",
        "AuthenticationAPIResponse",
        "ChannelsAPIResponse",
        "ProvidersAPIResponse",
        "HostedAPIResponse",
    }
    referenced_component_names = {document["$ref"].rsplit("/", 1)[-1] for document in success_schemas}
    all_response_component_names = {
        json_body["schema"]["$ref"].rsplit("/", 1)[-1]
        for operation in operations
        for response in operation["responses"].values()
        for json_body in [response.get("content", {}).get("application/json", {})]
        if isinstance(json_body.get("schema"), dict) and isinstance(json_body["schema"].get("$ref"), str)
    }
    assert broad_components.isdisjoint(components)
    assert broad_components.isdisjoint(referenced_component_names)
    assert broad_components.isdisjoint(all_response_component_names)

    clip_request_properties = components["VNextBrowserClipperCaptureRequest"]["properties"]
    assert clip_request_properties["capture_capability"]["writeOnly"] is True
    clip_request_content = operations_by_key[
        ("POST", "/v0/vnext/connectors/browser-clipper/capture")
    ]["requestBody"]["content"]
    assert set(clip_request_content) == {"application/json", "text/plain"}
    assert clip_request_content["text/plain"]["schema"] == {
        "type": "string",
        "contentMediaType": "application/json",
        "description": (
            "JSON-encoded VNextBrowserClipperCaptureRequest used by the "
            "CORS-safelisted one-time bookmarklet transport."
        ),
    }
    capability_response = components["CreateVnextBrowserClipCapabilitySuccessResponse"]
    assert capability_response["properties"]["capability"]["readOnly"] is True
    assert "example" not in json.dumps(
        operations_by_key[("POST", "/v0/vnext/connectors/browser-clipper/capabilities")]
    ).casefold()

    registry_component_names: list[str] = []
    for operation_key, (component_name, registered_schema) in operation_registry.items():
        registry_component_names.append(component_name)
        registered_properties = _openapi_object_properties(registered_schema)
        assert registered_properties
        assert all(isinstance(property_schema, dict) for property_schema in registered_properties.values())
        assert components[component_name] == registered_schema
        operation = operations_by_key[operation_key]
        operation_success_refs = {
            response["content"]["application/json"]["schema"]["$ref"]
            for status, response in operation["responses"].items()
            if str(status).startswith("2")
        }
        assert operation_success_refs == {f"#/components/schemas/{component_name}"}
    assert len(registry_component_names) == len(set(registry_component_names))

    for operation_key in exact_keys:
        component_name = main_module._OPENAPI_EXACT_RESPONSE_CONTRACTS[operation_key][0]
        exact_schema = components[component_name]
        assert exact_schema.get("required")
        assert exact_schema.get("additionalProperties") is False

    for operation_key in source_verified_operations:
        component_name = operation_registry[operation_key][0]
        verified_schema = components[component_name]
        assert verified_schema.get("required")
        assert verified_schema.get("additionalProperties") is False
        assert set(verified_schema["required"]) <= set(verified_schema["properties"])
    assert open_response_operations == set(polymorphic_operations)
    for operation_key in open_response_operations:
        component_name = operation_registry[operation_key][0]
        open_schema = components[component_name]
        assert open_schema.get("additionalProperties") is True
        variants = open_schema.get("oneOf")
        assert isinstance(variants, list) and variants
        for variant in variants:
            assert variant["additionalProperties"] is False
            assert set(variant["required"]) == set(variant["properties"])

    def operation_properties(operation_key: tuple[str, str]) -> set[str]:
        component_name = operation_registry[operation_key][0]
        return set(components[component_name]["properties"])

    assert {"status", "environment", "services"} <= components["HealthcheckSuccessResponse"]["properties"].keys()
    assert {"thread"} <= components["ThreadCreateResponse"]["properties"].keys()
    assert operation_properties(("POST", "/v0/context/compile")) == {
        "context_pack",
        "metadata",
        "trace_event_count",
        "trace_id",
    }
    assert operation_properties(("GET", "/v0/vnext/projects")) == {"count", "items", "order"}
    assert operation_properties(("POST", "/v1/providers")) == {"capabilities", "provider"}
    assert operation_properties(("POST", "/v1/workspaces/bootstrap")) == {
        "bootstrap",
        "seeded_provider_count",
        "workspace",
    }

    def operation_property_schema(operation_key: tuple[str, str], field: str) -> dict[str, object]:
        component_name = operation_registry[operation_key][0]
        return components[component_name]["properties"][field]

    assert operation_property_schema(("POST", "/v0/context/compile"), "trace_event_count") == {"type": "integer"}
    assert operation_property_schema(("POST", "/v0/vnext/memory-proposals"), "review_required") == {"type": "boolean"}
    assert operation_property_schema(("POST", "/v0/vnext/open-loops/extract"), "created_count") == {"type": "integer"}
    assert operation_property_schema(("POST", "/v0/vnext/open-loops/extract"), "open_loops")["type"] == "array"
    assert operation_property_schema(("POST", "/v1/workspaces/bootstrap"), "seeded_provider_count") == {
        "type": "integer"
    }
    for operation_key in polymorphic_operations:
        component_name = operation_registry[operation_key][0]
        assert len(components[component_name]["oneOf"]) == 2

    assert set(schema["paths"]["/v0/threads"]["post"]["responses"]) == {"201", "422", "default"}
    for path in (
        "/v0/consents",
        "/v0/vnext/memories/commit",
    ):
        assert {"200", "201"} <= schema["paths"][path]["post"]["responses"].keys()
    assert {"200", "503"} <= schema["paths"]["/healthz"]["get"]["responses"].keys()
    assert json.loads(json.dumps(schema))["openapi"] == schema["openapi"]
    assert "/v0/memories/extract-explicit-preferences" in route_paths
    assert "/v0/open-loops/extract-explicit-commitments" in route_paths
    assert "/v0/memories/capture-explicit-signals" in route_paths
    assert "/v0/memories" in route_paths
    assert "/v0/memories/review-queue" in route_paths
    assert "/v0/memories/quality-gate" in route_paths
    assert "/v0/memories/hygiene-dashboard" in route_paths
    assert "/v0/memories/evaluation-summary" in route_paths
    assert "/v0/memories/semantic-retrieval" in route_paths
    assert "/v0/memories/{memory_id}" in route_paths
    assert "/v0/memories/{memory_id}/revisions" in route_paths
    assert "/v0/memories/{memory_id}/labels" in route_paths
    assert "/v0/embedding-configs" in route_paths
    assert "/v0/memory-embeddings" in route_paths
    assert "/v0/memories/{memory_id}/embeddings" in route_paths
    assert "/v0/memory-embeddings/{memory_embedding_id}" in route_paths
    assert "/v0/admin/debug/continuity/lifecycle" in route_paths
    assert "/v0/admin/debug/continuity/lifecycle/{continuity_object_id}" in route_paths
    assert "/v0/admin/debug/continuity/artifacts/{artifact_id}" in route_paths
    assert "/v0/continuity/explain/{continuity_object_id}" in route_paths
    assert "/v1/contradictions/detect" in route_paths
    assert "/v1/contradictions/cases" in route_paths
    assert "/v1/contradictions/cases/{contradiction_case_id}" in route_paths
    assert "/v1/contradictions/cases/{contradiction_case_id}/resolve" in route_paths
    assert "/v1/trust/signals" in route_paths
    assert "/v1/evals/suites" in route_paths
    assert "/v1/evals/runs" in route_paths
    assert "/v1/evals/runs/{eval_run_id}" in route_paths
    assert "/v0/patterns" in route_paths
    assert "/v0/patterns/{pattern_id}" in route_paths
    assert "/v0/playbooks" in route_paths
    assert "/v0/playbooks/{playbook_id}" in route_paths
    assert "/v0/task-artifact-chunk-embeddings" in route_paths
    assert "/v0/task-artifacts/{task_artifact_id}/chunk-embeddings" in route_paths
    assert "/v0/task-artifact-chunks/{task_artifact_chunk_id}/embeddings" in route_paths
    assert "/v0/task-artifact-chunk-embeddings/{task_artifact_chunk_embedding_id}" in route_paths
    assert "/v0/entities" in route_paths
    assert "/v0/entity-edges" in route_paths
    assert "/v0/threads/health-dashboard" in route_paths
    assert "/v0/threads/{thread_id}/resumption-brief" in route_paths
    assert "/v0/task-artifacts" in route_paths
    assert "/v0/task-artifacts/{task_artifact_id}" in route_paths
    assert "/v0/task-artifacts/{task_artifact_id}/ingest" in route_paths
    assert "/v0/task-artifacts/{task_artifact_id}/chunks" in route_paths
    assert "/v0/task-artifacts/{task_artifact_id}/chunks/semantic-retrieval" in route_paths
    assert "/v0/entities/{entity_id}" in route_paths
    assert "/v0/entities/{entity_id}/edges" in route_paths
    gated_paths = {path for _method, path in main_module.LEGACY_HTTP_OPERATION_KEYS}
    assert route_paths.isdisjoint(gated_paths)
    assert not any(path.startswith("/v1/channels/telegram/") for path in route_paths)
    assert not any(path.startswith("/v1/admin/hosted/") for path in route_paths)
    assert not any(path.startswith("/v1/model-packs") for path in route_paths)


def test_openapi_direct_service_envelopes_do_not_publish_fabricated_wrappers() -> None:
    artifact_fields = {column.strip() for column in ARTIFACT_COLUMNS.split(",") if column.strip()}
    expected_fields = {
        ("POST", "/v0/memories/extract-explicit-preferences"): {"admissions", "candidates", "summary"},
        ("POST", "/v0/open-loops/extract-explicit-commitments"): {"admissions", "candidates", "summary"},
        ("POST", "/v0/memories/capture-explicit-signals"): {"commitments", "preferences", "summary"},
        ("POST", "/v0/vnext/artifacts/generate/daily-brief"): artifact_fields,
        ("POST", "/v0/vnext/artifacts/generate/weekly-synthesis"): artifact_fields,
        ("POST", "/v0/vnext/artifacts/generate/connections"): artifact_fields,
        ("POST", "/v0/vnext/artifacts/generate/contradictions"): artifact_fields,
        ("POST", "/v0/vnext/projects/update-candidates"): artifact_fields,
        ("POST", "/v0/vnext/projects/update-candidates/{artifact_id}/review"): artifact_fields,
        ("GET", "/v0/vnext/graph/neighborhood/{target_id}"): {
            "target_id",
            "from_edges",
            "to_edges",
            "edge_count",
        },
        ("GET", "/v0/vnext/projects/{project_id}/dashboard"): {
            "project",
            "state",
            "memories",
            "open_loops",
            "artifacts",
            "counts",
        },
        ("POST", "/v0/vnext/queue/process-next"): {
            "status",
            "task_id",
            "artifact_id",
            "error_code",
            "error_message",
        },
        ("POST", "/v0/vnext/connectors/telegram/sync"): {
            "status",
            "connector_name",
            "item_count",
            "imported_count",
            "duplicate_count",
            "skipped_count",
            "failed_count",
            "previous_cursor",
            "sync_cursor",
            "error_code",
            "source_ids",
            "failed_external_ids",
            "errors",
        },
        ("POST", "/v0/vnext/beliefs/{belief_id}/review"): {
            column.strip() for column in BELIEF_COLUMNS.split(",") if column.strip()
        },
    }
    fabricated_fields = {
        "capture_explicit_signal",
        "connection",
        "contradiction",
        "dashboard",
        "daily_brief",
        "extract_explicit_commitment",
        "extract_explicit_preference",
        "neighborhood",
        "result",
        "sync",
        "update_candidate",
        "weekly_synthesi",
    }

    for operation_key, expected in expected_fields.items():
        response_schema = main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[operation_key][1]
        properties = set(_openapi_object_properties(response_schema))
        assert properties == expected
        assert properties.isdisjoint(fabricated_fields)

    artifact_schema = main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[
        ("POST", "/v0/vnext/artifacts/generate/daily-brief")
    ][1]
    artifact_properties = _openapi_object_properties(artifact_schema)
    assert artifact_properties["id"] == {"type": "string", "format": "uuid"}
    assert artifact_properties["user_id"] == {"type": "string", "format": "uuid"}
    assert artifact_properties["created_at"] == {"type": "string", "format": "date-time"}
    for field in ("reviewed_at", "promoted_at"):
        assert artifact_properties[field] == {"anyOf": [{"type": "string", "format": "date-time"}, {"type": "null"}]}


def test_openapi_helper_backed_contracts_track_authoritative_response_types() -> None:
    authoritative_bindings = {
        **{
            operation_key: ("alicebot_api.contracts", type_name)
            for operation_key, type_name in openapi_contracts._OPENAPI_CONTRACT_RESPONSE_TYPES.items()
        },
        **openapi_contracts._OPENAPI_OTHER_AUTHORITATIVE_RESPONSE_TYPES,
    }

    assert len(authoritative_bindings) == 102
    for operation_key, (module_name, type_name) in authoritative_bindings.items():
        response_type = getattr(import_module(module_name), type_name)
        response_schema = main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[operation_key][1]
        expected_fields = set(response_type.__annotations__)
        expected_required = set(response_type.__required_keys__)

        assert set(_openapi_object_properties(response_schema)) == expected_fields, operation_key
        assert _openapi_required_fields(response_schema) == expected_required, operation_key
        assert response_schema["additionalProperties"] is False, operation_key
        assert operation_key in main_module.OPENAPI_SOURCE_VERIFIED_OPERATIONS
        assert "#/$defs/" not in json.dumps(response_schema)


def test_openapi_store_row_contracts_track_authoritative_column_sets() -> None:
    def column_fields(columns: str) -> set[str]:
        return {column.strip() for column in columns.split(",") if column.strip()}

    row_contracts = {
        ("GET", "/v0/vnext/sources/{source_id}"): SOURCE_COLUMNS,
        ("DELETE", "/v0/vnext/sources/{source_id}"): SOURCE_COLUMNS,
        ("POST", "/v0/vnext/queue/tasks"): TASK_COLUMNS,
        ("POST", "/v0/vnext/graph/edges/{edge_id}/review"): GRAPH_EDGE_COLUMNS,
        ("POST", "/v0/vnext/beliefs/{belief_id}/review"): BELIEF_COLUMNS,
        ("POST", "/v0/vnext/open-loops/{loop_id}/review"): OPEN_LOOP_COLUMNS,
        ("POST", "/v0/vnext/artifacts/{artifact_id}/quality-ratings"): QUALITY_RATING_COLUMNS,
        ("POST", "/v0/vnext/artifacts/{artifact_id}/insight-feedback"): EVENT_LOG_COLUMNS,
    }
    for operation_key in openapi_contracts._OPENAPI_ARTIFACT_ROW_OPERATIONS:
        row_contracts[operation_key] = ARTIFACT_COLUMNS

    for operation_key, columns in row_contracts.items():
        response_schema = main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[operation_key][1]
        expected_fields = column_fields(columns)
        assert set(_openapi_object_properties(response_schema)) == expected_fields, operation_key
        assert _openapi_required_fields(response_schema) == expected_fields, operation_key
        assert response_schema["additionalProperties"] is False, operation_key


def test_openapi_audit_samples_accept_actual_service_payloads_and_reject_phantom_keys() -> None:
    class GraphStore:
        def list_edges(
            self,
            *,
            from_id: str | None = None,
            to_id: str | None = None,
        ) -> list[dict[str, object]]:
            if from_id is not None:
                return [{"id": "edge-from", "from_id": from_id}]
            return [{"id": "edge-to", "to_id": to_id}]

    class ProjectStore:
        def get_project(self, project_id: str) -> dict[str, object]:
            return {
                "id": project_id,
                "name": "Alice release",
                "domain": "work",
                "current_state": "shipping",
            }

        def search_memories(self, **_kwargs: object) -> list[dict[str, object]]:
            return [{"id": "memory-1", "status": "active", "project_scope": ["project-1"]}]

        def list_open_loops(self, **_kwargs: object) -> list[dict[str, object]]:
            return [{"id": "loop-1", "project_scope": ["project-1"]}]

        def list_artifacts(self, **_kwargs: object) -> list[dict[str, object]]:
            return [{"id": "artifact-1", "project_scope": ["project-1"]}]

    class BeliefStore:
        def __init__(self) -> None:
            self.belief: dict[str, object] = {
                "id": "belief-1",
                "user_id": "user-1",
                "memory_id": "memory-1",
                "claim": "The release is ready",
                "status": "candidate",
                "confidence": 0.8,
                "first_seen_at": "2026-07-14T00:00:00Z",
                "last_reinforced_at": None,
                "last_challenged_at": None,
                "superseded_by": None,
                "metadata_json": {},
            }
            self.events: list[dict[str, object]] = [
                {"payload_json": {"status": "candidate"}},
            ]

        def update_belief_status(
            self,
            *,
            belief_id: str,
            status: str,
            confidence: float | None = None,
            superseded_by: str | None = None,
        ) -> dict[str, object]:
            assert belief_id == self.belief["id"]
            self.belief = {
                **self.belief,
                "status": status,
                "confidence": confidence if confidence is not None else self.belief["confidence"],
                "superseded_by": superseded_by,
            }
            return self.belief

        def append_event(self, event: dict[str, object]) -> dict[str, object]:
            self.events.append(event)
            return event

        def get_belief(self, belief_id: str) -> dict[str, object] | None:
            return self.belief if belief_id == self.belief["id"] else None

        def list_events(self, **_kwargs: object) -> list[dict[str, object]]:
            return list(self.events)

    queue_payload = QueueProcessResult(status="idle").to_record()
    connector_payload = ConnectorSyncResult(
        status="ok",
        connector_name="telegram",
        item_count=1,
        imported_count=1,
        duplicate_count=0,
        skipped_count=0,
        failed_count=0,
        previous_cursor=None,
        sync_cursor="cursor-1",
        source_ids=("source-1",),
    ).to_record()
    neighborhood_payload = VNextConnectionService(GraphStore()).graph_neighborhood(target_id="project-1")  # type: ignore[arg-type]
    dashboard_payload = VNextProjectService(ProjectStore()).project_dashboard(project_id="project-1")  # type: ignore[arg-type]
    belief_service = VNextContradictionService(BeliefStore())  # type: ignore[arg-type]
    belief_review_payload = belief_service.review_belief(
        belief_id="belief-1",
        action="reinforce",
        confidence=0.9,
    )

    payloads_by_operation = {
        ("POST", "/v0/vnext/queue/process-next"): queue_payload,
        ("POST", "/v0/vnext/connectors/telegram/sync"): connector_payload,
        ("GET", "/v0/vnext/graph/neighborhood/{target_id}"): neighborhood_payload,
        ("GET", "/v0/vnext/projects/{project_id}/dashboard"): dashboard_payload,
        ("POST", "/v0/vnext/beliefs/{belief_id}/review"): belief_review_payload,
    }
    for operation_key, payload in payloads_by_operation.items():
        response_schema = main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[operation_key][1]
        assert set(payload) == set(_openapi_object_properties(response_schema)), operation_key
        assert _openapi_schema_accepts(payload, response_schema), operation_key
        assert not _openapi_schema_accepts({**payload, "phantom_wrapper": {}}, response_schema), operation_key

    queue_schema = main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[("POST", "/v0/vnext/queue/process-next")][1]
    assert "error_code" in _openapi_required_fields(queue_schema)
    assert queue_schema["additionalProperties"] is False

    for operation_key in {
        ("POST", "/v0/vnext/connectors/{connector_name}/sync"),
        ("POST", "/v0/vnext/connectors/telegram/sync"),
        ("POST", "/v0/vnext/connectors/local-folder/sync"),
        ("POST", "/v0/vnext/connectors/browser-clipper/capture"),
    }:
        connector_schema = main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[operation_key][1]
        assert "error_code" in _openapi_required_fields(connector_schema), operation_key
        assert connector_schema["additionalProperties"] is False, operation_key


def test_scheduler_status_endpoint_payload_matches_its_generated_openapi_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class SchedulerStatusStore:
        def __init__(self) -> None:
            self.workflows: dict[str, dict[str, object]] = {
                "daily_brief": {
                    "id": "workflow-daily-brief",
                    "workflow_type": "daily_brief",
                    "enabled": True,
                    "paused": False,
                    "schedule_json": {"kind": "daily", "time_of_day": "08:00"},
                    "timezone": "UTC",
                    "next_run_at": "2026-07-15T08:00:00+00:00",
                    "metadata_json": {},
                },
                "weekly_synthesis": {
                    "id": "workflow-weekly-synthesis",
                    "workflow_type": "weekly_synthesis",
                    "enabled": True,
                    "paused": True,
                    "schedule_json": {"kind": "weekly", "weekday": "monday", "time_of_day": "09:00"},
                    "timezone": "UTC",
                    "next_run_at": "2026-07-20T09:00:00+00:00",
                    "metadata_json": {},
                },
            }

        def list_scheduler_workflows(self) -> list[dict[str, object]]:
            return list(self.workflows.values())

        def upsert_scheduler_workflow(
            self,
            workflow: dict[str, object],
            *,
            actor_type: str = "system",
        ) -> dict[str, object]:
            del actor_type
            workflow_type = str(workflow["workflow_type"])
            row = {"id": f"workflow-{workflow_type}", **workflow}
            self.workflows[workflow_type] = row
            return row

        def list_scheduler_runs(
            self,
            *,
            workflow_type: str | None = None,
            limit: int = 20,
        ) -> list[dict[str, object]]:
            runs = [
                {"id": "run-started", "workflow_type": "daily_brief", "status": "started"},
                {"id": "run-failed", "workflow_type": "weekly_synthesis", "status": "failed"},
                {"id": "run-succeeded", "workflow_type": "daily_brief", "status": "succeeded"},
            ]
            if workflow_type is not None:
                runs = [run for run in runs if run["workflow_type"] == workflow_type]
            return runs[:limit]

        def list_events(self, *, limit: int = 100, **_kwargs: object) -> list[dict[str, object]]:
            return [
                {
                    "id": "event-due-scan",
                    "event_type": "scheduler.due_scan",
                    "payload_json": {"due_count": 1},
                }
            ][:limit]

    status_file = tmp_path / "scheduler-status.json"
    status_file.write_text(
        json.dumps(
            {
                "pid": 999_999_999,
                "running": True,
                "started_at": "2026-07-14T08:00:00+00:00",
                "last_heartbeat_at": "2026-07-14T08:01:00+00:00",
                "interval_seconds": 60.0,
                "limit": 10,
                "mode": "foreground",
            }
        ),
        encoding="utf-8",
    )
    store = SchedulerStatusStore()
    requested_user_id = uuid4()

    @contextmanager
    def fake_user_connection(database_url: str, user_id):
        assert database_url == "postgresql://scheduler-status"
        assert user_id == requested_user_id
        yield object()

    monkeypatch.setattr(
        vnext_projects_router,
        "get_settings",
        lambda: Settings(database_url="postgresql://scheduler-status"),
    )
    monkeypatch.setattr(vnext_projects_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(vnext_projects_router, "PostgresVNextStore", lambda _conn: store)
    monkeypatch.setattr(
        vnext_projects_router,
        "daemon_status",
        lambda: daemon_status(
            pid_file=tmp_path / "scheduler.pid",
            status_file=status_file,
        ),
    )

    response = vnext_projects_router.get_vnext_scheduler_status(requested_user_id)
    assert response.status_code == 200
    payload = json.loads(response.body)
    operation_key = ("GET", "/v0/vnext/scheduler/status")
    component_name = main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[operation_key][0]
    generated_schema = main_module.app.openapi()["components"]["schemas"][component_name]
    expected_fields = {
        "mode",
        "disabled_by_default",
        "workflows",
        "recent_runs",
        "enabled_count",
        "paused_count",
        "last_failure",
        "recent_failures",
        "last_due_scan",
        "next_due_workflow",
        "currently_running_workflow",
        "last_success_by_workflow",
        "daemon",
    }

    assert set(payload) == expected_fields
    assert set(_openapi_object_properties(generated_schema)) == expected_fields
    assert _openapi_required_fields(generated_schema) == expected_fields
    assert generated_schema["additionalProperties"] is False
    assert _openapi_schema_accepts(payload, generated_schema)
    assert not _openapi_schema_accepts({**payload, "phantom_wrapper": {}}, generated_schema)


def test_openapi_typed_contracts_validate_representative_runtime_envelopes() -> None:
    schema = main_module.app.openapi()
    components = schema["components"]["schemas"]

    def response_schema(operation_key: tuple[str, str]) -> dict[str, object]:
        component_name = main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[operation_key][0]
        return components[component_name]

    compile_payload = {
        "trace_id": "trace-123",
        "trace_event_count": 5,
        "context_pack": {"events": [], "memories": []},
        "metadata": {"agent_profile_id": "assistant_default"},
    }
    proposal_payload = {
        "proposal": {"id": "memory-1", "status": "candidate"},
        "policy_decision": {"decision": "allowed"},
        "review_required": True,
    }
    extracted_loops_payload = {
        "open_loops": [{"id": "loop-1", "status": "open"}],
        "created_count": 1,
    }
    bootstrap_payload = {
        "workspace": {"id": "workspace-1", "bootstrap_status": "ready"},
        "bootstrap": {"status": "ready"},
        "seeded_provider_count": 0,
    }
    bootstrap_status_payload = {
        "workspace": bootstrap_payload["workspace"],
        "bootstrap": bootstrap_payload["bootstrap"],
    }
    now = datetime.now(UTC)
    active_job = {
        "id": uuid4(),
        "state": "running",
        "endpoint": providers_router.RESPONSE_JOB_ENDPOINT_RUNTIME,
        "request_fingerprint_sha256": "a" * 64,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    accepted_response = providers_router._response_job_replay_or_in_progress(
        store=_FakeResponseGenerationJobStore(object()),  # type: ignore[arg-type]
        job=active_job,  # type: ignore[arg-type]
        expected_request_fingerprint="a" * 64,
    )
    assert accepted_response is not None
    assert accepted_response.status_code == 202
    accepted_job_payload = json.loads(accepted_response.body)
    completed_job_payload = {
        "assistant": {"text": "Done"},
        "metadata": {"agent_profile_id": "assistant_default"},
        "trace": {"trace_id": "trace-123"},
    }

    payloads_by_operation = {
        ("POST", "/v0/context/compile"): compile_payload,
        ("POST", "/v0/memories/admit"): {
            "decision": "rejected",
            "reason": "candidate did not meet the admission threshold",
            "memory": None,
            "revision": None,
        },
        ("POST", "/v0/vnext/memory-proposals"): proposal_payload,
        ("POST", "/v0/vnext/open-loops/extract"): extracted_loops_payload,
        ("POST", "/v1/workspaces/bootstrap"): bootstrap_payload,
        ("GET", "/v1/workspaces/bootstrap/status"): bootstrap_status_payload,
    }
    for operation_key, payload in payloads_by_operation.items():
        assert _openapi_schema_accepts(payload, response_schema(operation_key)), operation_key

    operation_schema = response_schema(("POST", "/v1/runtime/invoke"))
    assert _openapi_schema_accepts(accepted_job_payload, operation_schema)
    assert _openapi_schema_accepts(completed_job_payload, operation_schema)

    assert not _openapi_schema_accepts(
        {**compile_payload, "trace_event_count": "5"},
        response_schema(("POST", "/v0/context/compile")),
    )
    assert not _openapi_schema_accepts(
        {**proposal_payload, "review_required": 1},
        response_schema(("POST", "/v0/vnext/memory-proposals")),
    )
    assert not _openapi_schema_accepts(
        {**extracted_loops_payload, "created_count": True},
        response_schema(("POST", "/v0/vnext/open-loops/extract")),
    )


def test_redact_url_credentials_strips_embedded_secrets() -> None:
    assert providers_router.redact_url_credentials("redis://alicebot:supersecret@cache:6379/0") == (
        "redis://cache:6379/0"
    )
    assert providers_router.redact_url_credentials("redis://cache:6379/0") == "redis://cache:6379/0"


def test_build_healthcheck_payload_keeps_boundary_statuses_consistent() -> None:
    settings = Settings(
        app_env="test",
        redis_url="redis://alicebot:supersecret@cache:6379/0",
        s3_endpoint_url="http://object-store",
    )

    assert main_module.build_healthcheck_payload(settings, database_ok=True) == {
        "status": "ok",
        "environment": "test",
        "services": {
            "database": {"status": "ok"},
            "redis": {"status": "not_checked", "url": "redis://cache:6379/0"},
            "object_storage": {"status": "not_checked"},
        },
    }
    assert (
        "endpoint_url"
        not in main_module.build_healthcheck_payload(
            settings,
            database_ok=True,
        )["services"]["object_storage"]
    )
    assert main_module.build_healthcheck_payload(settings, database_ok=False)["services"]["database"] == {
        "status": "unreachable"
    }


def _build_request(
    *,
    method: str,
    path: str,
    query_string: str = "",
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    client_host: str = "127.0.0.1",
) -> Request:
    encoded_headers = [(key.lower().encode("utf-8"), value.encode("utf-8")) for key, value in (headers or {}).items()]

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": query_string.encode("utf-8"),
            "headers": encoded_headers,
            "client": (client_host, 12345),
            "server": ("testserver", 80),
        },
        receive,
    )


def test_resolve_authenticated_user_id_prefers_configured_identity() -> None:
    configured_user_id = uuid4()
    request = _build_request(
        method="GET",
        path="/v0/threads",
        headers={"x-alicebot-user-id": str(uuid4())},
    )

    resolved = api_shared._resolve_authenticated_user_id(
        Settings(app_env="test", auth_user_id=str(configured_user_id)),
        request,
    )

    assert resolved == configured_user_id


def test_resolve_authenticated_user_id_allows_dev_without_header() -> None:
    request = _build_request(method="GET", path="/v0/threads")

    resolved = api_shared._resolve_authenticated_user_id(
        Settings(app_env="test", auth_user_id=""),
        request,
    )

    assert resolved is None


def test_request_client_is_loopback_accepts_localhost() -> None:
    request = _build_request(method="GET", path="/v0/threads")

    assert main_module._request_client_is_loopback(request, Settings()) is True


def test_request_client_is_loopback_rejects_remote_clients() -> None:
    request = _build_request(
        method="GET",
        path="/v0/threads",
        client_host="203.0.113.10",
    )

    assert main_module._request_client_is_loopback(request, Settings()) is False


def test_api_shared_bindings_preserve_main_compatibility_and_logger_identity() -> None:
    assert main_module._json_value is api_shared._json_value
    assert main_module._json_object is api_shared._json_object
    assert main_module._resolve_authenticated_user_id is api_shared._resolve_authenticated_user_id
    assert main_module._resolve_authenticated_v1_user_id is api_shared._resolve_authenticated_v1_user_id
    assert main_module._request_client_identifier is api_shared._request_client_identifier
    assert main_module.AUTH_USER_HEADER == api_shared.AUTH_USER_HEADER
    assert main_module.LOGGER is api_shared.LOGGER
    assert api_shared.LOGGER.name == "alicebot_api.main"


def test_v0_middleware_blocks_non_dev_when_legacy_api_disabled(monkeypatch) -> None:
    request = _build_request(method="GET", path="/v0/threads")
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(app_env="production", auth_user_id=str(uuid4())),
    )

    async def call_next(_: Request) -> Response:
        return Response(status_code=204)

    response = asyncio.run(main_module.enforce_authenticated_user_identity(request, call_next))

    assert response.status_code == 404


def test_v0_middleware_blocks_remote_clients_outside_dev(monkeypatch) -> None:
    request = _build_request(
        method="GET",
        path="/v0/threads",
        client_host="203.0.113.10",
    )
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            app_env="production",
            auth_user_id=str(uuid4()),
            legacy_v0_enabled_outside_dev=True,
        ),
    )

    async def call_next(_: Request) -> Response:
        return Response(status_code=204)

    response = asyncio.run(main_module.enforce_authenticated_user_identity(request, call_next))

    assert response.status_code == 403


def test_rewrite_user_id_query_param_rejects_mismatch() -> None:
    request = _build_request(
        method="GET",
        path="/v0/threads",
        query_string="user_id=00000000-0000-0000-0000-000000000002",
    )

    with pytest.raises(ValueError, match="query user_id does not match authenticated user"):
        main_module._rewrite_user_id_query_param(
            request,
            uuid4(),
        )


def test_rewrite_user_id_json_body_injects_missing_user_id() -> None:
    authenticated_user_id = uuid4()
    thread_id = uuid4()
    retained_path = "/v1/runtime/invoke"
    assert retained_path in _registered_route_paths()
    request = _build_request(
        method="POST",
        path=retained_path,
        body=json.dumps({"thread_id": str(thread_id), "message": "hello"}).encode("utf-8"),
        headers={"content-type": "application/json"},
    )

    rewritten_request = asyncio.run(main_module._rewrite_user_id_json_body(request, authenticated_user_id))
    rewritten_body = asyncio.run(rewritten_request.body())

    assert json.loads(rewritten_body) == {
        "thread_id": str(thread_id),
        "message": "hello",
        "user_id": str(authenticated_user_id),
    }
    # The middleware replays this cache. The returned Request is not enough.
    assert request._body == rewritten_body


def test_rewrite_user_id_json_body_rejects_mismatch() -> None:
    retained_path = "/v1/runtime/invoke"
    assert retained_path in _registered_route_paths()
    request = _build_request(
        method="POST",
        path=retained_path,
        body=json.dumps(
            {
                "user_id": "00000000-0000-0000-0000-000000000001",
                "thread_id": str(uuid4()),
                "message": "hello",
            }
        ).encode("utf-8"),
        headers={"content-type": "application/json"},
    )

    with pytest.raises(ValueError, match="request user_id does not match authenticated user"):
        asyncio.run(main_module._rewrite_user_id_json_body(request, uuid4()))


def test_browser_clip_simple_transport_is_bounded_and_rewritten_to_json(monkeypatch) -> None:
    user_id = str(uuid4())
    raw_body = json.dumps(
        {
            "user_id": user_id,
            "url": "https://example.test/article",
            "selected_text": "Bounded simple request.",
            "capture_capability": f"alice_clip_{'A' * 43}",
        }
    ).encode("utf-8")
    request = _build_request(
        method="POST",
        path="/v0/vnext/connectors/browser-clipper/capture",
        body=raw_body,
        headers={"content-type": "text/plain;charset=UTF-8"},
    )

    rewritten, payload = asyncio.run(main_module._prepare_browser_clip_simple_request(request))

    assert payload is not None and payload["user_id"] == user_id
    assert rewritten.headers["content-type"] == "application/json"
    assert asyncio.run(rewritten.body()) == raw_body

    monkeypatch.setattr(main_module, "_BROWSER_CLIP_SIMPLE_BODY_MAX_BYTES", len(raw_body) - 1)
    oversized = _build_request(
        method="POST",
        path="/v0/vnext/connectors/browser-clipper/capture",
        body=raw_body,
        headers={"content-type": "text/plain;charset=UTF-8"},
    )
    with pytest.raises(ValueError, match="too large"):
        asyncio.run(main_module._prepare_browser_clip_simple_request(oversized))


def test_browser_clip_simple_transport_stops_buffering_at_limit(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_BROWSER_CLIP_SIMPLE_BODY_MAX_BYTES", 8)
    received_chunks = 0
    handler_called = False
    chunks = (
        {"type": "http.request", "body": b'{"cap":', "more_body": True},
        {"type": "http.request", "body": b'"too-large', "more_body": True},
        {"type": "http.request", "body": b'-never-read"}', "more_body": False},
    )
    base_request = _build_request(
        method="POST",
        path="/v0/vnext/connectors/browser-clipper/capture",
        headers={"content-type": "text/plain;charset=UTF-8"},
    )

    async def receive() -> dict[str, object]:
        nonlocal received_chunks
        message = chunks[received_chunks]
        received_chunks += 1
        return message

    async def call_next(_: Request) -> Response:
        nonlocal handler_called
        handler_called = True
        return Response(status_code=204)

    request = Request(base_request.scope, receive)
    response = asyncio.run(main_module._vnext_protected_http_auth(request, call_next))

    assert response.status_code == 400
    assert received_chunks == 2
    assert handler_called is False


def test_browser_clip_simple_transport_rejects_declared_oversize_before_read(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_BROWSER_CLIP_SIMPLE_BODY_MAX_BYTES", 8)
    receive_called = False
    base_request = _build_request(
        method="POST",
        path="/v0/vnext/connectors/browser-clipper/capture",
        headers={
            "content-type": "text/plain;charset=UTF-8",
            "content-length": "9",
        },
    )

    async def receive() -> dict[str, object]:
        nonlocal receive_called
        receive_called = True
        return {"type": "http.request", "body": b"oversized", "more_body": False}

    request = Request(base_request.scope, receive)
    with pytest.raises(ValueError, match="too large"):
        asyncio.run(main_module._prepare_browser_clip_simple_request(request))

    assert receive_called is False


@pytest.mark.parametrize(
    "payload",
    (
        [],
        {"user_id": "00000000-0000-0000-0000-000000000001"},
        {"capture_capability": ""},
    ),
)
def test_browser_clip_simple_transport_rejects_non_object_or_missing_capability(payload: object) -> None:
    request = _build_request(
        method="POST",
        path="/v0/vnext/connectors/browser-clipper/capture",
        body=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "text/plain;charset=UTF-8"},
    )

    with pytest.raises(ValueError):
        asyncio.run(main_module._prepare_browser_clip_simple_request(request))


def test_vnext_capability_auth_exception_is_confined_to_capture_route(monkeypatch) -> None:
    user_id = str(uuid4())
    capability = f"alice_clip_{'A' * 43}"
    auth_calls: list[str] = []

    def fake_resolve_vnext_http_auth(**kwargs):
        auth_calls.append(str(kwargs["route_path"]))
        return None, None

    async def call_next(_: Request) -> Response:
        return Response(status_code=204)

    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url="postgresql://db"))
    monkeypatch.setattr(main_module, "_resolve_vnext_http_auth", fake_resolve_vnext_http_auth)
    capture = _build_request(
        method="POST",
        path="/v0/vnext/connectors/browser-clipper/capture",
        body=json.dumps(
            {
                "user_id": user_id,
                "url": "https://example.test/article",
                "capture_capability": capability,
            }
        ).encode("utf-8"),
        headers={"content-type": "application/json", "origin": "https://example.test"},
    )
    other = _build_request(
        method="POST",
        path="/v0/vnext/sources",
        body=json.dumps(
            {
                "user_id": user_id,
                "raw_text": "The capability must not authorize this route.",
                "capture_capability": capability,
            }
        ).encode("utf-8"),
        headers={"content-type": "application/json"},
    )

    assert asyncio.run(main_module._vnext_protected_http_auth(capture, call_next)).status_code == 204
    assert auth_calls == []
    assert asyncio.run(main_module._vnext_protected_http_auth(other, call_next)).status_code == 204
    assert auth_calls == ["/v0/vnext/sources"]


def test_request_client_identifier_ignores_forwarded_header_when_proxy_not_trusted() -> None:
    retained_path = "/v1/continuity/brief"
    assert retained_path in _registered_route_paths()
    request = _build_request(
        method="POST",
        path=retained_path,
        headers={"x-forwarded-for": "203.0.113.9, 127.0.0.1"},
    )

    client_identifier = api_shared._request_client_identifier(
        request,
        Settings(database_url="postgresql://app"),
    )

    assert client_identifier == "127.0.0.1"


def test_request_client_identifier_uses_forwarded_header_for_trusted_proxy() -> None:
    retained_path = "/v1/continuity/brief"
    assert retained_path in _registered_route_paths()
    request = _build_request(
        method="POST",
        path=retained_path,
        headers={"x-forwarded-for": "203.0.113.9, 127.0.0.1"},
    )

    client_identifier = api_shared._request_client_identifier(
        request,
        Settings(
            database_url="postgresql://app",
            trust_proxy_headers=True,
            trusted_proxy_ips=("127.0.0.1",),
        ),
    )

    assert client_identifier == "203.0.113.9"


def test_compile_context_returns_trace_and_context_pack(monkeypatch) -> None:
    user_id = uuid4()
    thread_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_compile_and_persist_trace(
        store,
        *,
        user_id,
        thread_id,
        limits,
        semantic_retrieval,
        artifact_retrieval,
        semantic_artifact_retrieval,
    ):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["thread_id"] = thread_id
        captured["limits"] = limits
        captured["semantic_retrieval"] = semantic_retrieval
        captured["artifact_retrieval"] = artifact_retrieval
        captured["semantic_artifact_retrieval"] = semantic_artifact_retrieval
        return CompiledTraceRun(
            trace_id="trace-123",
            trace_event_count=5,
            context_pack={
                "compiler_version": "continuity_v0",
                "scope": {"user_id": str(user_id), "thread_id": str(thread_id)},
                "limits": {
                    "max_sessions": 2,
                    "max_events": 4,
                    "max_memories": 3,
                    "max_entities": 2,
                    "max_entity_edges": 6,
                },
                "user": {
                    "id": str(user_id),
                    "email": "owner@example.com",
                    "display_name": "Owner",
                    "created_at": "2026-03-11T09:00:00+00:00",
                },
                "thread": {
                    "id": str(thread_id),
                    "title": "Thread",
                    "created_at": "2026-03-11T09:00:00+00:00",
                    "updated_at": "2026-03-11T09:01:00+00:00",
                },
                "sessions": [],
                "events": [],
                "memories": [
                    {
                        "id": "memory-123",
                        "memory_key": "user.preference.coffee",
                        "value": {"likes": "oat milk"},
                        "status": "active",
                        "source_event_ids": ["event-1"],
                        "created_at": "2026-03-11T09:00:00+00:00",
                        "updated_at": "2026-03-11T09:02:00+00:00",
                        "source_provenance": {"sources": ["symbolic"], "semantic_score": None},
                    }
                ],
                "memory_summary": {
                    "candidate_count": 2,
                    "included_count": 1,
                    "excluded_deleted_count": 1,
                    "excluded_limit_count": 0,
                    "hybrid_retrieval": {
                        "requested": False,
                        "embedding_config_id": None,
                        "query_vector_dimensions": 0,
                        "semantic_limit": 0,
                        "symbolic_selected_count": 1,
                        "semantic_selected_count": 0,
                        "merged_candidate_count": 1,
                        "deduplicated_count": 0,
                        "included_symbolic_only_count": 1,
                        "included_semantic_only_count": 0,
                        "included_dual_source_count": 0,
                        "similarity_metric": None,
                        "source_precedence": ["symbolic", "semantic"],
                        "symbolic_order": ["updated_at_asc", "created_at_asc", "id_asc"],
                        "semantic_order": ["score_desc", "created_at_asc", "id_asc"],
                    },
                },
                "artifact_chunks": [],
                "artifact_chunk_summary": {
                    "requested": False,
                    "lexical_requested": False,
                    "semantic_requested": False,
                    "scope": None,
                    "query": None,
                    "query_terms": [],
                    "embedding_config_id": None,
                    "query_vector_dimensions": 0,
                    "limit": 0,
                    "lexical_limit": 0,
                    "semantic_limit": 0,
                    "searched_artifact_count": 0,
                    "lexical_candidate_count": 0,
                    "semantic_candidate_count": 0,
                    "merged_candidate_count": 0,
                    "deduplicated_count": 0,
                    "included_count": 0,
                    "included_lexical_only_count": 0,
                    "included_semantic_only_count": 0,
                    "included_dual_source_count": 0,
                    "excluded_uningested_artifact_count": 0,
                    "excluded_limit_count": 0,
                    "matching_rule": None,
                    "similarity_metric": None,
                    "source_precedence": ["lexical", "semantic"],
                    "lexical_order": [
                        "matched_query_term_count_desc",
                        "first_match_char_start_asc",
                        "relative_path_asc",
                        "sequence_no_asc",
                        "id_asc",
                    ],
                    "semantic_order": ["score_desc", "relative_path_asc", "sequence_no_asc", "id_asc"],
                    "merged_order": [
                        "source_precedence_asc",
                        "lexical_rank_asc",
                        "semantic_rank_asc",
                        "relative_path_asc",
                        "sequence_no_asc",
                        "id_asc",
                    ],
                },
                "entities": [
                    {
                        "id": "entity-123",
                        "entity_type": "project",
                        "name": "AliceBot",
                        "source_memory_ids": ["memory-123"],
                        "created_at": "2026-03-11T09:03:00+00:00",
                    }
                ],
                "entity_summary": {
                    "candidate_count": 2,
                    "included_count": 1,
                    "excluded_limit_count": 1,
                },
                "entity_edges": [
                    {
                        "id": "edge-123",
                        "from_entity_id": "entity-123",
                        "to_entity_id": "entity-999",
                        "relationship_type": "depends_on",
                        "valid_from": "2026-03-11T09:04:00+00:00",
                        "valid_to": None,
                        "source_memory_ids": ["memory-123"],
                        "created_at": "2026-03-11T09:04:00+00:00",
                    }
                ],
                "entity_edge_summary": {
                    "anchor_entity_count": 1,
                    "candidate_count": 2,
                    "included_count": 1,
                    "excluded_limit_count": 1,
                },
            },
        )

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router.ContinuityStore,
        "get_thread",
        lambda _self, thread_id: {
            "id": thread_id,
            "user_id": user_id,
            "title": "Thread",
            "agent_profile_id": "assistant_default",
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
        },
    )
    monkeypatch.setattr(memories_legacy_router, "compile_and_persist_trace", fake_compile_and_persist_trace)

    response = memories_legacy_router.compile_context(
        memories_legacy_router.CompileContextRequest(
            user_id=user_id,
            thread_id=thread_id,
            max_sessions=2,
            max_events=4,
            max_memories=3,
            max_entities=2,
            max_entity_edges=6,
        )
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "trace_id": "trace-123",
        "trace_event_count": 5,
        "context_pack": {
            "compiler_version": "continuity_v0",
            "scope": {"user_id": str(user_id), "thread_id": str(thread_id)},
            "limits": {
                "max_sessions": 2,
                "max_events": 4,
                "max_memories": 3,
                "max_entities": 2,
                "max_entity_edges": 6,
            },
            "user": {
                "id": str(user_id),
                "email": "owner@example.com",
                "display_name": "Owner",
                "created_at": "2026-03-11T09:00:00+00:00",
            },
            "thread": {
                "id": str(thread_id),
                "title": "Thread",
                "created_at": "2026-03-11T09:00:00+00:00",
                "updated_at": "2026-03-11T09:01:00+00:00",
            },
            "sessions": [],
            "events": [],
            "memories": [
                {
                    "id": "memory-123",
                    "memory_key": "user.preference.coffee",
                    "value": {"likes": "oat milk"},
                    "status": "active",
                    "source_event_ids": ["event-1"],
                    "created_at": "2026-03-11T09:00:00+00:00",
                    "updated_at": "2026-03-11T09:02:00+00:00",
                    "source_provenance": {"sources": ["symbolic"], "semantic_score": None},
                }
            ],
            "memory_summary": {
                "candidate_count": 2,
                "included_count": 1,
                "excluded_deleted_count": 1,
                "excluded_limit_count": 0,
                "hybrid_retrieval": {
                    "requested": False,
                    "embedding_config_id": None,
                    "query_vector_dimensions": 0,
                    "semantic_limit": 0,
                    "symbolic_selected_count": 1,
                    "semantic_selected_count": 0,
                    "merged_candidate_count": 1,
                    "deduplicated_count": 0,
                    "included_symbolic_only_count": 1,
                    "included_semantic_only_count": 0,
                    "included_dual_source_count": 0,
                    "similarity_metric": None,
                    "source_precedence": ["symbolic", "semantic"],
                    "symbolic_order": ["updated_at_asc", "created_at_asc", "id_asc"],
                    "semantic_order": ["score_desc", "created_at_asc", "id_asc"],
                },
            },
            "artifact_chunks": [],
            "artifact_chunk_summary": {
                "requested": False,
                "lexical_requested": False,
                "semantic_requested": False,
                "scope": None,
                "query": None,
                "query_terms": [],
                "embedding_config_id": None,
                "query_vector_dimensions": 0,
                "limit": 0,
                "lexical_limit": 0,
                "semantic_limit": 0,
                "searched_artifact_count": 0,
                "lexical_candidate_count": 0,
                "semantic_candidate_count": 0,
                "merged_candidate_count": 0,
                "deduplicated_count": 0,
                "included_count": 0,
                "included_lexical_only_count": 0,
                "included_semantic_only_count": 0,
                "included_dual_source_count": 0,
                "excluded_uningested_artifact_count": 0,
                "excluded_limit_count": 0,
                "matching_rule": None,
                "similarity_metric": None,
                "source_precedence": ["lexical", "semantic"],
                "lexical_order": [
                    "matched_query_term_count_desc",
                    "first_match_char_start_asc",
                    "relative_path_asc",
                    "sequence_no_asc",
                    "id_asc",
                ],
                "semantic_order": ["score_desc", "relative_path_asc", "sequence_no_asc", "id_asc"],
                "merged_order": [
                    "source_precedence_asc",
                    "lexical_rank_asc",
                    "semantic_rank_asc",
                    "relative_path_asc",
                    "sequence_no_asc",
                    "id_asc",
                ],
            },
            "entities": [
                {
                    "id": "entity-123",
                    "entity_type": "project",
                    "name": "AliceBot",
                    "source_memory_ids": ["memory-123"],
                    "created_at": "2026-03-11T09:03:00+00:00",
                }
            ],
            "entity_summary": {
                "candidate_count": 2,
                "included_count": 1,
                "excluded_limit_count": 1,
            },
            "entity_edges": [
                {
                    "id": "edge-123",
                    "from_entity_id": "entity-123",
                    "to_entity_id": "entity-999",
                    "relationship_type": "depends_on",
                    "valid_from": "2026-03-11T09:04:00+00:00",
                    "valid_to": None,
                    "source_memory_ids": ["memory-123"],
                    "created_at": "2026-03-11T09:04:00+00:00",
                }
            ],
            "entity_edge_summary": {
                "anchor_entity_count": 1,
                "candidate_count": 2,
                "included_count": 1,
                "excluded_limit_count": 1,
            },
        },
        "metadata": {
            "agent_profile_id": "assistant_default",
        },
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id
    assert captured["thread_id"] == thread_id
    assert captured["limits"].max_sessions == 2
    assert captured["limits"].max_events == 4
    assert captured["limits"].max_memories == 3
    assert captured["limits"].max_entities == 2
    assert captured["limits"].max_entity_edges == 6
    assert captured["semantic_retrieval"] is None
    assert captured["artifact_retrieval"] is None
    assert captured["semantic_artifact_retrieval"] is None


def test_compile_context_returns_not_found_when_scope_row_is_missing(monkeypatch) -> None:
    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url="postgresql://app"))
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router.ContinuityStore,
        "get_thread",
        lambda _self, thread_id: {
            "id": thread_id,
            "user_id": uuid4(),
            "title": "Thread",
            "agent_profile_id": "assistant_default",
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
        },
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "compile_and_persist_trace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ContinuityStoreInvariantError("get_thread did not return a row from the database")
        ),
    )

    response = memories_legacy_router.compile_context(
        memories_legacy_router.CompileContextRequest(user_id=uuid4(), thread_id=uuid4())
    )

    assert response.status_code == 404
    assert json.loads(response.body) == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_compile_context_routes_semantic_and_artifact_inputs_and_validation_errors(
    monkeypatch,
) -> None:
    user_id = uuid4()
    thread_id = uuid4()
    config_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_compile_and_persist_trace(
        store,
        *,
        user_id,
        thread_id,
        limits,
        semantic_retrieval,
        artifact_retrieval,
        semantic_artifact_retrieval,
    ):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["thread_id"] = thread_id
        captured["limits"] = limits
        captured["semantic_retrieval"] = semantic_retrieval
        captured["artifact_retrieval"] = artifact_retrieval
        captured["semantic_artifact_retrieval"] = semantic_artifact_retrieval
        return CompiledTraceRun(
            trace_id="trace-semantic",
            trace_event_count=7,
            context_pack={
                "compiler_version": "continuity_v0",
                "scope": {"user_id": str(user_id), "thread_id": str(thread_id)},
                "limits": {
                    "max_sessions": 3,
                    "max_events": 8,
                    "max_memories": 5,
                    "max_entities": 5,
                    "max_entity_edges": 10,
                },
                "user": {
                    "id": str(user_id),
                    "email": "owner@example.com",
                    "display_name": "Owner",
                    "created_at": "2026-03-12T09:00:00+00:00",
                },
                "thread": {
                    "id": str(thread_id),
                    "title": "Thread",
                    "created_at": "2026-03-12T09:00:00+00:00",
                    "updated_at": "2026-03-12T09:01:00+00:00",
                },
                "sessions": [],
                "events": [],
                "memories": [
                    {
                        "id": "memory-123",
                        "memory_key": "user.preference.coffee",
                        "value": {"likes": "oat milk"},
                        "status": "active",
                        "source_event_ids": ["event-123"],
                        "created_at": "2026-03-12T09:00:00+00:00",
                        "updated_at": "2026-03-12T09:00:00+00:00",
                        "source_provenance": {
                            "sources": ["symbolic", "semantic"],
                            "semantic_score": 0.99,
                        },
                    }
                ],
                "memory_summary": {
                    "candidate_count": 1,
                    "included_count": 1,
                    "excluded_deleted_count": 0,
                    "excluded_limit_count": 0,
                    "hybrid_retrieval": {
                        "requested": True,
                        "embedding_config_id": str(config_id),
                        "query_vector_dimensions": 3,
                        "semantic_limit": 2,
                        "symbolic_selected_count": 1,
                        "semantic_selected_count": 1,
                        "merged_candidate_count": 1,
                        "deduplicated_count": 1,
                        "included_symbolic_only_count": 0,
                        "included_semantic_only_count": 0,
                        "included_dual_source_count": 1,
                        "similarity_metric": "cosine_similarity",
                        "source_precedence": ["symbolic", "semantic"],
                        "symbolic_order": ["updated_at_asc", "created_at_asc", "id_asc"],
                        "semantic_order": ["score_desc", "created_at_asc", "id_asc"],
                    },
                },
                "artifact_chunks": [
                    {
                        "id": "chunk-123",
                        "task_id": "task-123",
                        "task_artifact_id": "artifact-123",
                        "relative_path": "docs/spec.txt",
                        "media_type": "text/plain",
                        "sequence_no": 1,
                        "char_start": 0,
                        "char_end_exclusive": 16,
                        "text": "alpha beta spec",
                        "source_provenance": {
                            "sources": ["lexical", "semantic"],
                            "lexical_match": {
                                "matched_query_terms": ["alpha", "beta"],
                                "matched_query_term_count": 2,
                                "first_match_char_start": 0,
                            },
                            "semantic_score": 0.99,
                        },
                    }
                ],
                "artifact_chunk_summary": {
                    "requested": True,
                    "lexical_requested": True,
                    "semantic_requested": True,
                    "scope": {"kind": "task", "task_id": "task-123"},
                    "query": "alpha beta",
                    "query_terms": ["alpha", "beta"],
                    "embedding_config_id": str(config_id),
                    "query_vector_dimensions": 3,
                    "limit": 2,
                    "lexical_limit": 2,
                    "semantic_limit": 2,
                    "searched_artifact_count": 1,
                    "lexical_candidate_count": 1,
                    "semantic_candidate_count": 1,
                    "merged_candidate_count": 1,
                    "deduplicated_count": 1,
                    "included_count": 1,
                    "included_lexical_only_count": 0,
                    "included_semantic_only_count": 0,
                    "included_dual_source_count": 1,
                    "excluded_uningested_artifact_count": 0,
                    "excluded_limit_count": 0,
                    "matching_rule": "casefolded_unicode_word_overlap_unique_query_terms_v1",
                    "similarity_metric": "cosine_similarity",
                    "source_precedence": ["lexical", "semantic"],
                    "lexical_order": [
                        "matched_query_term_count_desc",
                        "first_match_char_start_asc",
                        "relative_path_asc",
                        "sequence_no_asc",
                        "id_asc",
                    ],
                    "semantic_order": ["score_desc", "relative_path_asc", "sequence_no_asc", "id_asc"],
                    "merged_order": [
                        "source_precedence_asc",
                        "lexical_rank_asc",
                        "semantic_rank_asc",
                        "relative_path_asc",
                        "sequence_no_asc",
                        "id_asc",
                    ],
                },
                "entities": [],
                "entity_summary": {
                    "candidate_count": 0,
                    "included_count": 0,
                    "excluded_limit_count": 0,
                },
                "entity_edges": [],
                "entity_edge_summary": {
                    "anchor_entity_count": 0,
                    "candidate_count": 0,
                    "included_count": 0,
                    "excluded_limit_count": 0,
                },
            },
        )

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router.ContinuityStore,
        "get_thread",
        lambda _self, thread_id: {
            "id": thread_id,
            "user_id": user_id,
            "title": "Thread",
            "agent_profile_id": "assistant_default",
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
        },
    )
    monkeypatch.setattr(memories_legacy_router, "compile_and_persist_trace", fake_compile_and_persist_trace)

    response = memories_legacy_router.compile_context(
        memories_legacy_router.CompileContextRequest(
            user_id=user_id,
            thread_id=thread_id,
            semantic=memories_legacy_router.CompileContextSemanticRequest(
                embedding_config_id=config_id,
                query_vector=[0.1, 0.2, 0.3],
                limit=2,
            ),
            artifact_retrieval=memories_legacy_router.CompileContextTaskScopedArtifactRetrievalRequest(
                kind="task",
                task_id=uuid4(),
                query="alpha beta",
                limit=2,
            ),
            semantic_artifact_retrieval=(
                memories_legacy_router.CompileContextTaskScopedSemanticArtifactRetrievalRequest(
                    kind="task",
                    task_id=uuid4(),
                    embedding_config_id=config_id,
                    query_vector=[0.1, 0.2, 0.3],
                    limit=2,
                )
            ),
        )
    )

    assert response.status_code == 200
    assert json.loads(response.body)["context_pack"]["memory_summary"]["hybrid_retrieval"] == {
        "requested": True,
        "embedding_config_id": str(config_id),
        "query_vector_dimensions": 3,
        "semantic_limit": 2,
        "symbolic_selected_count": 1,
        "semantic_selected_count": 1,
        "merged_candidate_count": 1,
        "deduplicated_count": 1,
        "included_symbolic_only_count": 0,
        "included_semantic_only_count": 0,
        "included_dual_source_count": 1,
        "similarity_metric": "cosine_similarity",
        "source_precedence": ["symbolic", "semantic"],
        "symbolic_order": ["updated_at_asc", "created_at_asc", "id_asc"],
        "semantic_order": ["score_desc", "created_at_asc", "id_asc"],
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["semantic_retrieval"].embedding_config_id == config_id
    assert captured["semantic_retrieval"].query_vector == (0.1, 0.2, 0.3)
    assert captured["semantic_retrieval"].limit == 2
    assert captured["artifact_retrieval"].task_id is not None
    assert captured["artifact_retrieval"].query == "alpha beta"
    assert captured["artifact_retrieval"].limit == 2
    assert captured["semantic_artifact_retrieval"].task_id is not None
    assert captured["semantic_artifact_retrieval"].embedding_config_id == config_id
    assert captured["semantic_artifact_retrieval"].query_vector == (0.1, 0.2, 0.3)
    assert captured["semantic_artifact_retrieval"].limit == 2

    monkeypatch.setattr(
        memories_legacy_router,
        "compile_and_persist_trace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SemanticMemoryRetrievalValidationError(
                "embedding_config_id must reference an existing embedding config owned by the user"
            )
        ),
    )

    error_response = memories_legacy_router.compile_context(
        memories_legacy_router.CompileContextRequest(
            user_id=user_id,
            thread_id=thread_id,
            semantic=memories_legacy_router.CompileContextSemanticRequest(
                embedding_config_id=config_id,
                query_vector=[0.1, 0.2, 0.3],
                limit=2,
            ),
        )
    )

    assert error_response.status_code == 400
    assert json.loads(error_response.body) == {
        "detail": {"code": "invalid_request", "message": "The request is invalid"}
    }

    monkeypatch.setattr(
        memories_legacy_router,
        "compile_and_persist_trace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SemanticArtifactChunkRetrievalValidationError(
                "query_vector length must match embedding config dimensions (3): 2"
            )
        ),
    )

    semantic_artifact_error_response = memories_legacy_router.compile_context(
        memories_legacy_router.CompileContextRequest(
            user_id=user_id,
            thread_id=thread_id,
            semantic_artifact_retrieval=(
                memories_legacy_router.CompileContextTaskScopedSemanticArtifactRetrievalRequest(
                    kind="task",
                    task_id=uuid4(),
                    embedding_config_id=config_id,
                    query_vector=[0.1, 0.2],
                    limit=2,
                )
            ),
        )
    )

    assert semantic_artifact_error_response.status_code == 400
    assert json.loads(semantic_artifact_error_response.body) == {
        "detail": {"code": "invalid_request", "message": "The request is invalid"}
    }


def test_compile_context_request_rejects_invalid_artifact_scope_shape() -> None:
    with pytest.raises(Exception) as exc_info:
        memories_legacy_router.CompileContextRequest(
            user_id=uuid4(),
            thread_id=uuid4(),
            artifact_retrieval={
                "kind": "task",
                "task_artifact_id": str(uuid4()),
                "query": "alpha beta",
            },
        )

    assert "task_id" in str(exc_info.value)


def test_compile_context_request_rejects_invalid_semantic_artifact_scope_shape() -> None:
    with pytest.raises(Exception) as exc_info:
        memories_legacy_router.CompileContextRequest(
            user_id=uuid4(),
            thread_id=uuid4(),
            semantic_artifact_retrieval={
                "kind": "task",
                "task_artifact_id": str(uuid4()),
                "embedding_config_id": str(uuid4()),
                "query_vector": [0.1, 0.2, 0.3],
            },
        )

    assert "task_id" in str(exc_info.value)


def test_runtime_invoke_replays_terminal_job_before_provider_resolution(monkeypatch) -> None:
    user_id = uuid4()
    workspace_id = uuid4()
    provider_id = uuid4()
    body = providers_router.RuntimeInvokeRequest(
        provider_id=provider_id,
        thread_id=uuid4(),
        message="Replay the completed turn.",
    )
    idempotency_key = "runtime-replay-before-provider"
    fingerprint = providers_router.request_fingerprint(
        {
            "workspace_id": str(workspace_id),
            "body": body.model_dump(mode="json"),
        }
    )

    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    _FakeResponseGenerationJobStore.reset()
    monkeypatch.setattr(providers_router, "ResponseGenerationJobStore", _FakeResponseGenerationJobStore)
    monkeypatch.setattr(
        _FakeResponseGenerationJobStore,
        "get_for_update",
        lambda *_args, **_kwargs: pytest.fail("runtime replay must use atomic get-or-create, not an absent-row lookup"),
    )
    lookup = _FakeResponseGenerationJobStore(object()).create_or_get_for_update(
        user_id=user_id,
        workspace_id=workspace_id,
        endpoint=providers_router.RESPONSE_JOB_ENDPOINT_RUNTIME,
        idempotency_key=idempotency_key,
        request_fingerprint_sha256=fingerprint,
    )
    lookup.job.update(
        state="succeeded",
        response_status_code=200,
        response_payload={"assistant": {"text": "Durable replay"}},
        completed_at=datetime.now(UTC),
    )

    monkeypatch.setattr(providers_router, "get_settings", lambda: Settings(database_url="postgresql://app"))
    monkeypatch.setattr(
        providers_router,
        "_resolve_authenticated_v1_user_id",
        lambda *_args, **_kwargs: user_id,
    )
    monkeypatch.setattr(
        providers_router,
        "_require_local_provider_workspace",
        lambda **_kwargs: (workspace_id, user_id),
    )
    monkeypatch.setattr(providers_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(providers_router, "set_current_user_account", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        providers_router,
        "ContinuityStore",
        lambda *_args, **_kwargs: pytest.fail("terminal replay must not read provider state"),
    )
    monkeypatch.setattr(
        providers_router,
        "resolve_runtime_provider_config_secrets",
        lambda *_args, **_kwargs: pytest.fail("terminal replay must not resolve provider secrets"),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/runtime/invoke",
            "headers": [
                (b"x-alicebot-user-id", str(user_id).encode()),
                (b"idempotency-key", idempotency_key.encode()),
            ],
        }
    )

    response = providers_router.invoke_v1_runtime(request, body)

    assert response.status_code == 200
    assert response.headers["Idempotency-Replayed"] == "true"
    assert json.loads(response.body) == {"assistant": {"text": "Durable replay"}}


def test_provider_registration_stages_secret_outside_transaction_and_compensates(monkeypatch) -> None:
    workspace_id = uuid4()
    user_id = uuid4()
    transaction_depth = 0
    events: list[str] = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @contextmanager
        def transaction(self):
            nonlocal transaction_depth
            transaction_depth += 1
            try:
                yield
            finally:
                transaction_depth -= 1

    def fake_write_provider_api_key(**_kwargs) -> None:
        assert transaction_depth == 0
        events.append("secret_staged")

    def fake_register_workspace_provider(**_kwargs):
        assert transaction_depth == 1
        events.append("database_rejected")
        raise RuntimeError("simulated database rejection")

    def fake_delete_provider_api_key(**_kwargs) -> None:
        assert transaction_depth == 0
        events.append("secret_compensated")

    class FakeStore:
        def __init__(self, _conn) -> None:
            pass

        def is_provider_secret_reference_in_use(self, **_kwargs) -> bool:
            assert transaction_depth == 1
            return False

    monkeypatch.setattr(
        providers_router,
        "_resolve_owned_provider_workspace",
        lambda **_kwargs: (workspace_id, user_id),
    )
    monkeypatch.setattr(providers_router.psycopg, "connect", lambda *_args, **_kwargs: FakeConnection())
    monkeypatch.setattr(providers_router, "_assert_provider_write_context", lambda **_kwargs: None)
    monkeypatch.setattr(providers_router, "ContinuityStore", FakeStore)
    monkeypatch.setattr(
        providers_router,
        "validate_provider_base_url",
        lambda value, **_kwargs: value,
    )
    monkeypatch.setattr(providers_router, "write_provider_api_key", fake_write_provider_api_key)
    monkeypatch.setattr(providers_router, "_register_workspace_provider", fake_register_workspace_provider)
    monkeypatch.setattr(providers_router, "delete_provider_api_key", fake_delete_provider_api_key)

    with pytest.raises(RuntimeError, match="simulated database rejection"):
        providers_router._create_workspace_provider_durable(
            settings=Settings(database_url="postgresql://app"),
            authenticated_user_id=user_id,
            provider_key="openai_compatible",
            display_name="Provider",
            base_url="https://provider.example",
            api_key="staged-secret",
            auth_mode="bearer",
            default_model="model",
            model_list_path="/models",
            healthcheck_path="/health",
            invoke_path="/responses",
            metadata={},
        )

    assert events == ["secret_staged", "database_rejected", "secret_compensated"]


def test_staged_provider_secret_is_not_deleted_when_database_references_it(
    monkeypatch,
) -> None:
    workspace_id = uuid4()
    user_id = uuid4()
    encoded_reference = f"provider_secret_ref:workspaces/{workspace_id}/model-provider-secrets/secret.json"

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @contextmanager
        def transaction(self):
            yield

    class FakeStore:
        def __init__(self, _conn) -> None:
            pass

        def is_provider_secret_reference_in_use(self, **_kwargs) -> bool:
            return True

    monkeypatch.setattr(providers_router.psycopg, "connect", lambda *_args, **_kwargs: FakeConnection())
    monkeypatch.setattr(providers_router, "_assert_provider_write_context", lambda **_kwargs: None)
    monkeypatch.setattr(providers_router, "ContinuityStore", FakeStore)
    monkeypatch.setattr(
        providers_router,
        "delete_provider_api_key",
        lambda **_kwargs: pytest.fail("an ambiguously committed, referenced secret must never be deleted"),
    )

    providers_router._discard_staged_provider_secret(
        settings=Settings(database_url="postgresql://app"),
        workspace_id=workspace_id,
        user_account_id=user_id,
        staged_secret=providers_router._StagedProviderSecret(
            secret_ref=f"workspaces/{workspace_id}/model-provider-secrets/secret.json",
            encoded_reference=encoded_reference,
        ),
    )


def test_provider_update_reports_atomic_cas_loss() -> None:
    provider_id = uuid4()
    workspace_id = uuid4()
    user_id = uuid4()
    expected_revision = 4
    expected_fingerprint = "a" * 64

    class LostUpdateStore:
        def update_model_provider(self, **kwargs):
            assert kwargs["expected_config_revision"] == expected_revision
            assert kwargs["expected_config_fingerprint_sha256"] == expected_fingerprint
            return None

    existing_provider = {
        "id": provider_id,
        "workspace_id": workspace_id,
        "created_by_user_account_id": user_id,
        "provider_key": "openai_compatible",
        "model_provider": "openai_responses",
        "display_name": "Original Provider",
        "base_url": "https://provider.example/v1",
        "api_key": "provider_secret_ref:workspaces/example/secret.json",
        "auth_mode": "bearer",
        "default_model": "gpt-5-mini",
        "status": "active",
        "model_list_path": "/models",
        "healthcheck_path": "/models",
        "invoke_path": "/responses",
        "azure_api_version": "",
        "azure_auth_secret_ref": "",
        "metadata": {},
        "config_revision": expected_revision,
        "config_fingerprint_sha256": expected_fingerprint,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    with pytest.raises(
        providers_router.ProviderConfigurationChangedError,
        match="changed while the update was being committed",
    ):
        providers_router._update_workspace_provider(
            store=LostUpdateStore(),  # type: ignore[arg-type]
            existing_provider=existing_provider,  # type: ignore[arg-type]
            updated_by_user_account_id=user_id,
            display_name="Losing Update",
            base_url=None,
            api_key=None,
            ad_token=None,
            credential_secret_ref=None,
            auth_mode=None,
            default_model=None,
            model_list_path=None,
            healthcheck_path=None,
            invoke_path=None,
            api_version=None,
            metadata=None,
        )


def test_admit_memory_returns_decision_payload(monkeypatch) -> None:
    user_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_admit_memory_candidate(store, *, user_id, candidate):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["candidate"] = candidate
        return AdmissionDecisionOutput(
            action="ADD",
            reason="source_backed_add",
            memory={
                "id": "memory-123",
                "user_id": str(user_id),
                "memory_key": "user.preference.coffee",
                "value": {"likes": "oat milk"},
                "status": "active",
                "source_event_ids": ["event-1"],
                "created_at": "2026-03-11T09:00:00+00:00",
                "updated_at": "2026-03-11T09:00:00+00:00",
                "deleted_at": None,
            },
            revision={
                "id": "revision-123",
                "user_id": str(user_id),
                "memory_id": "memory-123",
                "sequence_no": 1,
                "action": "ADD",
                "memory_key": "user.preference.coffee",
                "previous_value": None,
                "new_value": {"likes": "oat milk"},
                "source_event_ids": ["event-1"],
                "candidate": {
                    "memory_key": "user.preference.coffee",
                    "value": {"likes": "oat milk"},
                    "source_event_ids": ["event-1"],
                    "delete_requested": False,
                },
                "created_at": "2026-03-11T09:00:00+00:00",
            },
        )

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(memories_legacy_router, "admit_memory_candidate", fake_admit_memory_candidate)

    response = memories_legacy_router.admit_memory(
        memories_legacy_router.AdmitMemoryRequest(
            user_id=user_id,
            memory_key="user.preference.coffee",
            value={"likes": "oat milk"},
            source_event_ids=[uuid4()],
        )
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "decision": "ADD",
        "reason": "source_backed_add",
        "memory": {
            "id": "memory-123",
            "user_id": str(user_id),
            "memory_key": "user.preference.coffee",
            "value": {"likes": "oat milk"},
            "status": "active",
            "source_event_ids": ["event-1"],
            "created_at": "2026-03-11T09:00:00+00:00",
            "updated_at": "2026-03-11T09:00:00+00:00",
            "deleted_at": None,
        },
        "revision": {
            "id": "revision-123",
            "user_id": str(user_id),
            "memory_id": "memory-123",
            "sequence_no": 1,
            "action": "ADD",
            "memory_key": "user.preference.coffee",
            "previous_value": None,
            "new_value": {"likes": "oat milk"},
            "source_event_ids": ["event-1"],
            "candidate": {
                "memory_key": "user.preference.coffee",
                "value": {"likes": "oat milk"},
                "source_event_ids": ["event-1"],
                "delete_requested": False,
            },
            "created_at": "2026-03-11T09:00:00+00:00",
        },
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id
    assert captured["candidate"].memory_key == "user.preference.coffee"


def test_admit_memory_includes_open_loop_payload_when_created(monkeypatch) -> None:
    user_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    def fake_admit_memory_candidate(_store, *, user_id, candidate):
        captured["user_id"] = user_id
        captured["candidate"] = candidate
        return AdmissionDecisionOutput(
            action="NOOP",
            reason="memory_unchanged",
            memory=None,
            revision=None,
            open_loop={
                "id": "loop-123",
                "memory_id": "memory-123",
                "title": "Confirm before reorder",
                "status": "open",
                "opened_at": "2026-03-23T10:00:00+00:00",
                "due_at": "2026-03-25T10:00:00+00:00",
                "resolved_at": None,
                "resolution_note": None,
                "created_at": "2026-03-23T10:00:00+00:00",
                "updated_at": "2026-03-23T10:00:00+00:00",
            },
        )

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(memories_legacy_router, "admit_memory_candidate", fake_admit_memory_candidate)

    response = memories_legacy_router.admit_memory(
        memories_legacy_router.AdmitMemoryRequest(
            user_id=user_id,
            memory_key="user.preference.coffee",
            value={"likes": "oat milk"},
            source_event_ids=[uuid4()],
            open_loop=memories_legacy_router.AdmitMemoryOpenLoopRequest(
                title="Confirm before reorder",
                due_at="2026-03-25T10:00:00+00:00",
            ),
        )
    )

    assert response.status_code == 200
    assert json.loads(response.body)["open_loop"] == {
        "id": "loop-123",
        "memory_id": "memory-123",
        "title": "Confirm before reorder",
        "status": "open",
        "opened_at": "2026-03-23T10:00:00+00:00",
        "due_at": "2026-03-25T10:00:00+00:00",
        "resolved_at": None,
        "resolution_note": None,
        "created_at": "2026-03-23T10:00:00+00:00",
        "updated_at": "2026-03-23T10:00:00+00:00",
    }
    assert captured["candidate"].open_loop is not None
    assert captured["candidate"].open_loop.title == "Confirm before reorder"


def test_admit_memory_returns_bad_request_when_source_validation_fails(monkeypatch) -> None:
    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url="postgresql://app"))
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "admit_memory_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            MemoryAdmissionValidationError("source_event_ids must all reference existing events owned by the user")
        ),
    )

    response = memories_legacy_router.admit_memory(
        memories_legacy_router.AdmitMemoryRequest(
            user_id=uuid4(),
            memory_key="user.preference.coffee",
            value={"likes": "black"},
            source_event_ids=[uuid4()],
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body) == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_extract_explicit_preferences_returns_payload(monkeypatch) -> None:
    user_id = uuid4()
    source_event_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_extract_and_admit_explicit_preferences(store, *, user_id, request):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["request"] = request
        return {
            "candidates": [
                {
                    "memory_key": "user.preference.black_coffee",
                    "value": {
                        "kind": "explicit_preference",
                        "preference": "like",
                        "text": "black coffee",
                    },
                    "source_event_ids": [str(source_event_id)],
                    "delete_requested": False,
                    "pattern": "i_like",
                    "subject_text": "black coffee",
                }
            ],
            "admissions": [
                {
                    "decision": "ADD",
                    "reason": "source_backed_add",
                    "memory": {
                        "id": "memory-123",
                        "user_id": str(user_id),
                        "memory_key": "user.preference.black_coffee",
                        "value": {
                            "kind": "explicit_preference",
                            "preference": "like",
                            "text": "black coffee",
                        },
                        "status": "active",
                        "source_event_ids": [str(source_event_id)],
                        "created_at": "2026-03-12T09:00:00+00:00",
                        "updated_at": "2026-03-12T09:00:00+00:00",
                        "deleted_at": None,
                    },
                    "revision": {
                        "id": "revision-123",
                        "user_id": str(user_id),
                        "memory_id": "memory-123",
                        "sequence_no": 1,
                        "action": "ADD",
                        "memory_key": "user.preference.black_coffee",
                        "previous_value": None,
                        "new_value": {
                            "kind": "explicit_preference",
                            "preference": "like",
                            "text": "black coffee",
                        },
                        "source_event_ids": [str(source_event_id)],
                        "candidate": {
                            "memory_key": "user.preference.black_coffee",
                            "value": {
                                "kind": "explicit_preference",
                                "preference": "like",
                                "text": "black coffee",
                            },
                            "source_event_ids": [str(source_event_id)],
                            "delete_requested": False,
                        },
                        "created_at": "2026-03-12T09:00:00+00:00",
                    },
                }
            ],
            "summary": {
                "source_event_id": str(source_event_id),
                "source_event_kind": "message.user",
                "candidate_count": 1,
                "admission_count": 1,
                "persisted_change_count": 1,
                "noop_count": 0,
            },
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "extract_and_admit_explicit_preferences",
        fake_extract_and_admit_explicit_preferences,
    )

    response = memories_legacy_router.extract_explicit_preferences(
        memories_legacy_router.ExtractExplicitPreferencesRequest(
            user_id=user_id,
            source_event_id=source_event_id,
        )
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert set(payload) == set(
        main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[("POST", "/v0/memories/extract-explicit-preferences")][1][
            "properties"
        ]
    )
    assert payload == {
        "candidates": [
            {
                "memory_key": "user.preference.black_coffee",
                "value": {
                    "kind": "explicit_preference",
                    "preference": "like",
                    "text": "black coffee",
                },
                "source_event_ids": [str(source_event_id)],
                "delete_requested": False,
                "pattern": "i_like",
                "subject_text": "black coffee",
            }
        ],
        "admissions": [
            {
                "decision": "ADD",
                "reason": "source_backed_add",
                "memory": {
                    "id": "memory-123",
                    "user_id": str(user_id),
                    "memory_key": "user.preference.black_coffee",
                    "value": {
                        "kind": "explicit_preference",
                        "preference": "like",
                        "text": "black coffee",
                    },
                    "status": "active",
                    "source_event_ids": [str(source_event_id)],
                    "created_at": "2026-03-12T09:00:00+00:00",
                    "updated_at": "2026-03-12T09:00:00+00:00",
                    "deleted_at": None,
                },
                "revision": {
                    "id": "revision-123",
                    "user_id": str(user_id),
                    "memory_id": "memory-123",
                    "sequence_no": 1,
                    "action": "ADD",
                    "memory_key": "user.preference.black_coffee",
                    "previous_value": None,
                    "new_value": {
                        "kind": "explicit_preference",
                        "preference": "like",
                        "text": "black coffee",
                    },
                    "source_event_ids": [str(source_event_id)],
                    "candidate": {
                        "memory_key": "user.preference.black_coffee",
                        "value": {
                            "kind": "explicit_preference",
                            "preference": "like",
                            "text": "black coffee",
                        },
                        "source_event_ids": [str(source_event_id)],
                        "delete_requested": False,
                    },
                    "created_at": "2026-03-12T09:00:00+00:00",
                },
            }
        ],
        "summary": {
            "source_event_id": str(source_event_id),
            "source_event_kind": "message.user",
            "candidate_count": 1,
            "admission_count": 1,
            "persisted_change_count": 1,
            "noop_count": 0,
        },
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id
    assert captured["request"].source_event_id == source_event_id


def test_extract_explicit_preferences_returns_bad_request_when_source_event_is_invalid(
    monkeypatch,
) -> None:
    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url="postgresql://app"))
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "extract_and_admit_explicit_preferences",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            memories_legacy_router.ExplicitPreferenceExtractionValidationError(
                "source_event_id must reference an existing message.user event owned by the user"
            )
        ),
    )

    response = memories_legacy_router.extract_explicit_preferences(
        memories_legacy_router.ExtractExplicitPreferencesRequest(
            user_id=uuid4(),
            source_event_id=uuid4(),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body) == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_extract_explicit_commitments_returns_payload(monkeypatch) -> None:
    user_id = uuid4()
    source_event_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_extract_and_admit_explicit_commitments(store, *, user_id, request):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["request"] = request
        return {
            "candidates": [
                {
                    "memory_key": "user.commitment.submit_tax_forms",
                    "value": {
                        "kind": "explicit_commitment",
                        "text": "submit tax forms",
                    },
                    "source_event_ids": [str(source_event_id)],
                    "delete_requested": False,
                    "pattern": "remind_me_to",
                    "commitment_text": "submit tax forms",
                    "open_loop_title": "Remember to submit tax forms",
                }
            ],
            "admissions": [
                {
                    "decision": "ADD",
                    "reason": "source_backed_add",
                    "memory": {
                        "id": "memory-123",
                        "user_id": str(user_id),
                        "memory_key": "user.commitment.submit_tax_forms",
                        "value": {
                            "kind": "explicit_commitment",
                            "text": "submit tax forms",
                        },
                        "status": "active",
                        "source_event_ids": [str(source_event_id)],
                        "memory_type": "commitment",
                        "created_at": "2026-03-23T09:00:00+00:00",
                        "updated_at": "2026-03-23T09:00:00+00:00",
                        "deleted_at": None,
                    },
                    "revision": {
                        "id": "revision-123",
                        "user_id": str(user_id),
                        "memory_id": "memory-123",
                        "sequence_no": 1,
                        "action": "ADD",
                        "memory_key": "user.commitment.submit_tax_forms",
                        "previous_value": None,
                        "new_value": {
                            "kind": "explicit_commitment",
                            "text": "submit tax forms",
                        },
                        "source_event_ids": [str(source_event_id)],
                        "candidate": {
                            "memory_key": "user.commitment.submit_tax_forms",
                            "value": {
                                "kind": "explicit_commitment",
                                "text": "submit tax forms",
                            },
                            "source_event_ids": [str(source_event_id)],
                            "delete_requested": False,
                            "memory_type": "commitment",
                        },
                        "created_at": "2026-03-23T09:00:00+00:00",
                    },
                    "open_loop": {
                        "decision": "CREATED",
                        "reason": "created_open_loop_for_memory",
                        "open_loop": {
                            "id": "loop-123",
                            "memory_id": "memory-123",
                            "title": "Remember to submit tax forms",
                            "status": "open",
                            "opened_at": "2026-03-23T09:00:00+00:00",
                            "due_at": None,
                            "resolved_at": None,
                            "resolution_note": None,
                            "created_at": "2026-03-23T09:00:00+00:00",
                            "updated_at": "2026-03-23T09:00:00+00:00",
                        },
                    },
                }
            ],
            "summary": {
                "source_event_id": str(source_event_id),
                "source_event_kind": "message.user",
                "candidate_count": 1,
                "admission_count": 1,
                "persisted_change_count": 1,
                "noop_count": 0,
                "open_loop_created_count": 1,
                "open_loop_noop_count": 0,
            },
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "extract_and_admit_explicit_commitments",
        fake_extract_and_admit_explicit_commitments,
    )

    response = memories_legacy_router.extract_explicit_commitments(
        memories_legacy_router.ExtractExplicitCommitmentsRequest(
            user_id=user_id,
            source_event_id=source_event_id,
        )
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert set(payload) == set(
        main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[("POST", "/v0/open-loops/extract-explicit-commitments")][1][
            "properties"
        ]
    )
    assert payload["summary"] == {
        "source_event_id": str(source_event_id),
        "source_event_kind": "message.user",
        "candidate_count": 1,
        "admission_count": 1,
        "persisted_change_count": 1,
        "noop_count": 0,
        "open_loop_created_count": 1,
        "open_loop_noop_count": 0,
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id
    assert captured["request"].source_event_id == source_event_id


def test_extract_explicit_commitments_returns_bad_request_when_source_event_is_invalid(
    monkeypatch,
) -> None:
    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url="postgresql://app"))
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "extract_and_admit_explicit_commitments",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            memories_legacy_router.ExplicitCommitmentExtractionValidationError(
                "source_event_id must reference an existing message.user event owned by the user"
            )
        ),
    )

    response = memories_legacy_router.extract_explicit_commitments(
        memories_legacy_router.ExtractExplicitCommitmentsRequest(
            user_id=uuid4(),
            source_event_id=uuid4(),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body) == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_capture_explicit_signals_returns_payload(monkeypatch) -> None:
    user_id = uuid4()
    source_event_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_extract_and_admit_explicit_signals(store, *, user_id, request):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["request"] = request
        return {
            "preferences": {
                "candidates": [],
                "admissions": [],
                "summary": {
                    "source_event_id": str(source_event_id),
                    "source_event_kind": "message.user",
                    "candidate_count": 0,
                    "admission_count": 0,
                    "persisted_change_count": 0,
                    "noop_count": 0,
                },
            },
            "commitments": {
                "candidates": [
                    {
                        "memory_key": "user.commitment.submit_tax_forms",
                        "value": {
                            "kind": "explicit_commitment",
                            "text": "submit tax forms",
                        },
                        "source_event_ids": [str(source_event_id)],
                        "delete_requested": False,
                        "pattern": "remind_me_to",
                        "commitment_text": "submit tax forms",
                        "open_loop_title": "Remember to submit tax forms",
                    }
                ],
                "admissions": [],
                "summary": {
                    "source_event_id": str(source_event_id),
                    "source_event_kind": "message.user",
                    "candidate_count": 1,
                    "admission_count": 0,
                    "persisted_change_count": 0,
                    "noop_count": 0,
                    "open_loop_created_count": 0,
                    "open_loop_noop_count": 0,
                },
            },
            "summary": {
                "source_event_id": str(source_event_id),
                "source_event_kind": "message.user",
                "candidate_count": 1,
                "admission_count": 0,
                "persisted_change_count": 0,
                "noop_count": 0,
                "open_loop_created_count": 0,
                "open_loop_noop_count": 0,
                "preference_candidate_count": 0,
                "preference_admission_count": 0,
                "commitment_candidate_count": 1,
                "commitment_admission_count": 0,
            },
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "extract_and_admit_explicit_signals",
        fake_extract_and_admit_explicit_signals,
    )

    response = memories_legacy_router.capture_explicit_signals(
        memories_legacy_router.CaptureExplicitSignalsRequest(
            user_id=user_id,
            source_event_id=source_event_id,
        )
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert set(payload) == set(
        main_module.OPENAPI_OPERATION_RESPONSE_SCHEMAS[("POST", "/v0/memories/capture-explicit-signals")][1][
            "properties"
        ]
    )
    assert payload["summary"] == {
        "source_event_id": str(source_event_id),
        "source_event_kind": "message.user",
        "candidate_count": 1,
        "admission_count": 0,
        "persisted_change_count": 0,
        "noop_count": 0,
        "open_loop_created_count": 0,
        "open_loop_noop_count": 0,
        "preference_candidate_count": 0,
        "preference_admission_count": 0,
        "commitment_candidate_count": 1,
        "commitment_admission_count": 0,
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id
    assert captured["request"].source_event_id == source_event_id


def test_capture_explicit_signals_returns_bad_request_when_source_event_is_invalid(
    monkeypatch,
) -> None:
    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url="postgresql://app"))
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "extract_and_admit_explicit_signals",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            memories_legacy_router.ExplicitSignalCaptureValidationError(
                "source_event_id must reference an existing message.user event owned by the user"
            )
        ),
    )

    response = memories_legacy_router.capture_explicit_signals(
        memories_legacy_router.CaptureExplicitSignalsRequest(
            user_id=uuid4(),
            source_event_id=uuid4(),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body) == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_list_memories_returns_review_payload(monkeypatch) -> None:
    user_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_list_memory_review_records(store, *, user_id, status, limit):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["status"] = status
        captured["limit"] = limit
        return {
            "items": [
                {
                    "id": "memory-123",
                    "memory_key": "user.preference.coffee",
                    "value": {"likes": "oat milk"},
                    "status": "active",
                    "source_event_ids": ["event-1"],
                    "created_at": "2026-03-11T09:00:00+00:00",
                    "updated_at": "2026-03-11T09:02:00+00:00",
                    "deleted_at": None,
                }
            ],
            "summary": {
                "status": "active",
                "limit": 10,
                "returned_count": 1,
                "total_count": 1,
                "has_more": False,
                "order": ["updated_at_desc", "created_at_desc", "id_desc"],
            },
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(memories_legacy_router, "list_memory_review_records", fake_list_memory_review_records)

    response = memories_legacy_router.list_memories(user_id=user_id, status="active", limit=10)

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "items": [
            {
                "id": "memory-123",
                "memory_key": "user.preference.coffee",
                "value": {"likes": "oat milk"},
                "status": "active",
                "source_event_ids": ["event-1"],
                "created_at": "2026-03-11T09:00:00+00:00",
                "updated_at": "2026-03-11T09:02:00+00:00",
                "deleted_at": None,
            }
        ],
        "summary": {
            "status": "active",
            "limit": 10,
            "returned_count": 1,
            "total_count": 1,
            "has_more": False,
            "order": ["updated_at_desc", "created_at_desc", "id_desc"],
        },
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id
    assert captured["status"] == "active"
    assert captured["limit"] == 10


def test_open_loop_routes_return_payload_and_errors(monkeypatch) -> None:
    user_id = uuid4()
    open_loop_id = uuid4()
    memory_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_list_open_loop_records(store, *, user_id, status, limit):
        captured["list_store_type"] = type(store).__name__
        captured["list_user_id"] = user_id
        captured["list_status"] = status
        captured["list_limit"] = limit
        return {
            "items": [
                {
                    "id": str(open_loop_id),
                    "memory_id": str(memory_id),
                    "title": "Follow up",
                    "status": "open",
                    "opened_at": "2026-03-23T09:00:00+00:00",
                    "due_at": None,
                    "resolved_at": None,
                    "resolution_note": None,
                    "created_at": "2026-03-23T09:00:00+00:00",
                    "updated_at": "2026-03-23T09:00:00+00:00",
                }
            ],
            "summary": {
                "status": "open",
                "limit": 10,
                "returned_count": 1,
                "total_count": 1,
                "has_more": False,
                "order": ["opened_at_desc", "created_at_desc", "id_desc"],
            },
        }

    def fake_get_open_loop_record(_store, *, user_id, open_loop_id):
        captured["detail_user_id"] = user_id
        captured["detail_open_loop_id"] = open_loop_id
        return {
            "open_loop": {
                "id": str(open_loop_id),
                "memory_id": str(memory_id),
                "title": "Follow up",
                "status": "open",
                "opened_at": "2026-03-23T09:00:00+00:00",
                "due_at": None,
                "resolved_at": None,
                "resolution_note": None,
                "created_at": "2026-03-23T09:00:00+00:00",
                "updated_at": "2026-03-23T09:00:00+00:00",
            }
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(memories_legacy_router, "list_open_loop_records", fake_list_open_loop_records)
    monkeypatch.setattr(memories_legacy_router, "get_open_loop_record", fake_get_open_loop_record)

    list_response = memories_legacy_router.list_open_loops(user_id=user_id, status="open", limit=10)
    detail_response = memories_legacy_router.get_open_loop(open_loop_id=open_loop_id, user_id=user_id)

    assert list_response.status_code == 200
    assert json.loads(list_response.body)["summary"]["status"] == "open"
    assert detail_response.status_code == 200
    assert json.loads(detail_response.body)["open_loop"]["id"] == str(open_loop_id)
    assert captured["list_status"] == "open"
    assert captured["list_limit"] == 10
    assert captured["detail_open_loop_id"] == open_loop_id

    monkeypatch.setattr(
        memories_legacy_router,
        "get_open_loop_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OpenLoopNotFoundError("open loop hidden")),
    )
    not_found_response = memories_legacy_router.get_open_loop(open_loop_id=open_loop_id, user_id=user_id)
    assert not_found_response.status_code == 404
    assert json.loads(not_found_response.body) == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_open_loop_mutation_routes_handle_create_and_status_validation(monkeypatch) -> None:
    user_id = uuid4()
    open_loop_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_create_open_loop_record(_store, *, user_id, open_loop):
        captured["create_user_id"] = user_id
        captured["create_open_loop"] = open_loop
        return {
            "open_loop": {
                "id": str(open_loop_id),
                "memory_id": None,
                "title": open_loop.title,
                "status": "open",
                "opened_at": "2026-03-23T09:00:00+00:00",
                "due_at": None,
                "resolved_at": None,
                "resolution_note": None,
                "created_at": "2026-03-23T09:00:00+00:00",
                "updated_at": "2026-03-23T09:00:00+00:00",
            }
        }

    def fake_update_open_loop_status_record(_store, *, user_id, open_loop_id, request):
        captured["status_user_id"] = user_id
        captured["status_open_loop_id"] = open_loop_id
        captured["status_request"] = request
        return {
            "open_loop": {
                "id": str(open_loop_id),
                "memory_id": None,
                "title": "Follow up",
                "status": "resolved",
                "opened_at": "2026-03-23T09:00:00+00:00",
                "due_at": None,
                "resolved_at": "2026-03-24T09:00:00+00:00",
                "resolution_note": "Resolved",
                "created_at": "2026-03-23T09:00:00+00:00",
                "updated_at": "2026-03-24T09:00:00+00:00",
            }
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(memories_legacy_router, "create_open_loop_record", fake_create_open_loop_record)
    monkeypatch.setattr(memories_legacy_router, "update_open_loop_status_record", fake_update_open_loop_status_record)

    create_response = memories_legacy_router.create_open_loop(
        memories_legacy_router.CreateOpenLoopRequest(
            user_id=user_id,
            title="Follow up",
        )
    )
    status_response = memories_legacy_router.update_open_loop_status(
        open_loop_id=open_loop_id,
        request=memories_legacy_router.UpdateOpenLoopStatusRequest(
            user_id=user_id,
            status="resolved",
            resolution_note="Resolved",
        ),
    )

    assert create_response.status_code == 201
    assert json.loads(create_response.body)["open_loop"]["title"] == "Follow up"
    assert status_response.status_code == 200
    assert json.loads(status_response.body)["open_loop"]["status"] == "resolved"
    assert captured["status_request"].status == "resolved"

    monkeypatch.setattr(
        memories_legacy_router,
        "update_open_loop_status_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OpenLoopValidationError("status invalid")),
    )
    bad_status_response = memories_legacy_router.update_open_loop_status(
        open_loop_id=open_loop_id,
        request=memories_legacy_router.UpdateOpenLoopStatusRequest(user_id=user_id, status="invalid"),
    )
    assert bad_status_response.status_code == 400
    assert json.loads(bad_status_response.body) == {
        "detail": {"code": "invalid_request", "message": "The request is invalid"}
    }


def test_get_memory_returns_not_found_when_memory_is_inaccessible(monkeypatch) -> None:
    memory_id = uuid4()

    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url="postgresql://app"))
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "get_memory_review_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            memories_legacy_router.MemoryReviewNotFoundError(f"memory {memory_id} was not found")
        ),
    )

    response = memories_legacy_router.get_memory(memory_id=memory_id, user_id=uuid4())

    assert response.status_code == 404
    assert json.loads(response.body) == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_list_memory_review_queue_returns_unlabeled_active_queue_payload(monkeypatch) -> None:
    user_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_list_memory_review_queue_records(store, *, user_id, limit, priority_mode):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["limit"] = limit
        captured["priority_mode"] = priority_mode
        return {
            "items": [
                {
                    "id": "memory-123",
                    "memory_key": "user.preference.coffee",
                    "value": {"likes": "oat milk"},
                    "status": "active",
                    "source_event_ids": ["event-1"],
                    "is_high_risk": True,
                    "is_stale_truth": False,
                    "queue_priority_mode": "high_risk_first",
                    "priority_reason": "high_risk",
                    "created_at": "2026-03-12T09:00:00+00:00",
                    "updated_at": "2026-03-12T09:02:00+00:00",
                }
            ],
            "summary": {
                "memory_status": "active",
                "review_state": "unlabeled",
                "priority_mode": "high_risk_first",
                "available_priority_modes": [
                    "oldest_first",
                    "recent_first",
                    "high_risk_first",
                    "stale_truth_first",
                ],
                "limit": 7,
                "returned_count": 1,
                "total_count": 1,
                "has_more": False,
                "order": [
                    "is_high_risk_desc",
                    "confidence_asc_nulls_first",
                    "updated_at_desc",
                    "created_at_desc",
                    "id_desc",
                ],
            },
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router, "list_memory_review_queue_records", fake_list_memory_review_queue_records
    )

    response = memories_legacy_router.list_memory_review_queue(
        user_id=user_id,
        limit=7,
        priority_mode="high_risk_first",
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "items": [
            {
                "id": "memory-123",
                "memory_key": "user.preference.coffee",
                "value": {"likes": "oat milk"},
                "status": "active",
                "source_event_ids": ["event-1"],
                "is_high_risk": True,
                "is_stale_truth": False,
                "queue_priority_mode": "high_risk_first",
                "priority_reason": "high_risk",
                "created_at": "2026-03-12T09:00:00+00:00",
                "updated_at": "2026-03-12T09:02:00+00:00",
            }
        ],
        "summary": {
            "memory_status": "active",
            "review_state": "unlabeled",
            "priority_mode": "high_risk_first",
            "available_priority_modes": [
                "oldest_first",
                "recent_first",
                "high_risk_first",
                "stale_truth_first",
            ],
            "limit": 7,
            "returned_count": 1,
            "total_count": 1,
            "has_more": False,
            "order": [
                "is_high_risk_desc",
                "confidence_asc_nulls_first",
                "updated_at_desc",
                "created_at_desc",
                "id_desc",
            ],
        },
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id
    assert captured["limit"] == 7
    assert captured["priority_mode"] == "high_risk_first"


def test_get_memories_evaluation_summary_returns_aggregate_payload(monkeypatch) -> None:
    user_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_get_memory_evaluation_summary(store, *, user_id):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        return {
            "summary": {
                "total_memory_count": 4,
                "active_memory_count": 3,
                "deleted_memory_count": 1,
                "labeled_memory_count": 2,
                "unlabeled_memory_count": 2,
                "total_label_row_count": 3,
                "label_row_counts_by_value": {
                    "correct": 1,
                    "incorrect": 0,
                    "outdated": 1,
                    "insufficient_evidence": 1,
                },
                "label_value_order": [
                    "correct",
                    "incorrect",
                    "outdated",
                    "insufficient_evidence",
                ],
            }
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(memories_legacy_router, "get_memory_evaluation_summary", fake_get_memory_evaluation_summary)

    response = memories_legacy_router.get_memories_evaluation_summary(user_id=user_id)

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "summary": {
            "total_memory_count": 4,
            "active_memory_count": 3,
            "deleted_memory_count": 1,
            "labeled_memory_count": 2,
            "unlabeled_memory_count": 2,
            "total_label_row_count": 3,
            "label_row_counts_by_value": {
                "correct": 1,
                "incorrect": 0,
                "outdated": 1,
                "insufficient_evidence": 1,
            },
            "label_value_order": [
                "correct",
                "incorrect",
                "outdated",
                "insufficient_evidence",
            ],
        }
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id


def test_get_memories_quality_gate_returns_canonical_payload(monkeypatch) -> None:
    user_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_get_memory_quality_gate_summary(store, *, user_id):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        return {
            "summary": {
                "status": "needs_review",
                "precision": 0.9,
                "precision_target": 0.8,
                "adjudicated_sample_count": 10,
                "minimum_adjudicated_sample": 10,
                "remaining_to_minimum_sample": 0,
                "unlabeled_memory_count": 1,
                "high_risk_memory_count": 1,
                "stale_truth_count": 0,
                "superseded_active_conflict_count": 0,
                "counts": {
                    "active_memory_count": 11,
                    "labeled_active_memory_count": 10,
                    "adjudicated_correct_count": 9,
                    "adjudicated_incorrect_count": 1,
                    "outdated_label_count": 0,
                    "insufficient_evidence_label_count": 0,
                },
            }
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(memories_legacy_router, "get_memory_quality_gate_summary", fake_get_memory_quality_gate_summary)

    response = memories_legacy_router.get_memories_quality_gate(user_id=user_id)

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "summary": {
            "status": "needs_review",
            "precision": 0.9,
            "precision_target": 0.8,
            "adjudicated_sample_count": 10,
            "minimum_adjudicated_sample": 10,
            "remaining_to_minimum_sample": 0,
            "unlabeled_memory_count": 1,
            "high_risk_memory_count": 1,
            "stale_truth_count": 0,
            "superseded_active_conflict_count": 0,
            "counts": {
                "active_memory_count": 11,
                "labeled_active_memory_count": 10,
                "adjudicated_correct_count": 9,
                "adjudicated_incorrect_count": 1,
                "outdated_label_count": 0,
                "insufficient_evidence_label_count": 0,
            },
        }
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id


def test_get_memories_hygiene_dashboard_returns_canonical_payload(monkeypatch) -> None:
    user_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_get_memory_hygiene_dashboard_summary(store, *, user_id):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        return {
            "dashboard": {
                "posture": "watch",
                "reason": "Duplicate and contradiction pressure is visible.",
                "duplicate_group_count": 1,
                "duplicate_memory_count": 2,
                "stale_fact_count": 1,
                "unresolved_contradiction_count": 1,
                "weak_trust_count": 2,
                "review_queue_pressure": {
                    "posture": "watch",
                    "total_count": 2,
                    "stale_over_72h_count": 0,
                    "aging_24h_to_72h_count": 1,
                    "reason": "Backlog exists and should be drained before it becomes stale.",
                },
                "duplicate_groups": [
                    {
                        "group_key": 'preference:{"merchant":"Fixture"}',
                        "memory_type": "preference",
                        "normalized_value": '{"merchant":"Fixture"}',
                        "count": 2,
                        "memory_ids": ["memory-1", "memory-2"],
                        "memory_keys": ["user.preference.primary", "user.preference.secondary"],
                        "latest_updated_at": "2026-03-18T10:05:00+00:00",
                    }
                ],
                "focus": [
                    {
                        "kind": "duplicates",
                        "posture": "watch",
                        "count": 2,
                        "reason": "Multiple active memories share the same normalized value.",
                        "action": "Review duplicate groups and keep one canonical fact per repeated value.",
                        "sample_ids": ["memory-1", "memory-2"],
                    }
                ],
                "sources": ["memories", "contradiction_cases", "trust_signals", "continuity_recall"],
            }
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "get_memory_hygiene_dashboard_summary",
        fake_get_memory_hygiene_dashboard_summary,
    )

    response = memories_legacy_router.get_memories_hygiene_dashboard(user_id=user_id)

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "dashboard": {
            "posture": "watch",
            "reason": "Duplicate and contradiction pressure is visible.",
            "duplicate_group_count": 1,
            "duplicate_memory_count": 2,
            "stale_fact_count": 1,
            "unresolved_contradiction_count": 1,
            "weak_trust_count": 2,
            "review_queue_pressure": {
                "posture": "watch",
                "total_count": 2,
                "stale_over_72h_count": 0,
                "aging_24h_to_72h_count": 1,
                "reason": "Backlog exists and should be drained before it becomes stale.",
            },
            "duplicate_groups": [
                {
                    "group_key": 'preference:{"merchant":"Fixture"}',
                    "memory_type": "preference",
                    "normalized_value": '{"merchant":"Fixture"}',
                    "count": 2,
                    "memory_ids": ["memory-1", "memory-2"],
                    "memory_keys": ["user.preference.primary", "user.preference.secondary"],
                    "latest_updated_at": "2026-03-18T10:05:00+00:00",
                }
            ],
            "focus": [
                {
                    "kind": "duplicates",
                    "posture": "watch",
                    "count": 2,
                    "reason": "Multiple active memories share the same normalized value.",
                    "action": "Review duplicate groups and keep one canonical fact per repeated value.",
                    "sample_ids": ["memory-1", "memory-2"],
                }
            ],
            "sources": ["memories", "contradiction_cases", "trust_signals", "continuity_recall"],
        }
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id


def test_get_threads_health_dashboard_returns_canonical_payload(monkeypatch) -> None:
    user_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_get_thread_health_dashboard(store, *, user_id):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        return {
            "dashboard": {
                "posture": "critical",
                "total_thread_count": 2,
                "recent_thread_count": 1,
                "stale_thread_count": 0,
                "risky_thread_count": 1,
                "watch_thread_count": 0,
                "thresholds": {
                    "recent_window_hours": 24.0,
                    "stale_window_hours": 72.0,
                    "risky_score_threshold": 2,
                },
                "recent_threads": [
                    {
                        "thread": {
                            "id": "thread-1",
                            "title": "Recent thread",
                            "agent_profile_id": "assistant_default",
                            "created_at": "2026-03-18T09:00:00+00:00",
                            "updated_at": "2026-03-18T10:00:00+00:00",
                        },
                        "health_posture": "healthy",
                        "activity_posture": "recent",
                        "risk_posture": "normal",
                        "risk_score": 0,
                        "last_activity_at": "2026-03-18T10:00:00+00:00",
                        "last_conversation_at": "2026-03-18T10:00:00+00:00",
                        "hours_since_last_activity": 2.0,
                        "conversation_event_count": 3,
                        "operational_event_count": 1,
                        "active_session_count": 1,
                        "open_loop_count": 0,
                        "stale_open_loop_count": 0,
                        "unresolved_contradiction_count": 0,
                        "weak_trust_signal_count": 0,
                        "reasons": [
                            "No active contradiction, stale open-loop, or weak-trust pressure is currently visible."
                        ],
                        "recommended_action": "No immediate intervention required.",
                    }
                ],
                "stale_threads": [],
                "risky_threads": [
                    {
                        "thread": {
                            "id": "thread-2",
                            "title": "Risky thread",
                            "agent_profile_id": "assistant_default",
                            "created_at": "2026-03-18T09:00:00+00:00",
                            "updated_at": "2026-03-18T08:00:00+00:00",
                        },
                        "health_posture": "critical",
                        "activity_posture": "current",
                        "risk_posture": "risky",
                        "risk_score": 3,
                        "last_activity_at": "2026-03-18T08:00:00+00:00",
                        "last_conversation_at": "2026-03-18T08:00:00+00:00",
                        "hours_since_last_activity": 26.0,
                        "conversation_event_count": 2,
                        "operational_event_count": 0,
                        "active_session_count": 0,
                        "open_loop_count": 1,
                        "stale_open_loop_count": 1,
                        "unresolved_contradiction_count": 1,
                        "weak_trust_signal_count": 1,
                        "reasons": ["1 unresolved contradiction case(s).", "1 stale open-loop item(s)."],
                        "recommended_action": "Resolve contradiction cases before trusting this thread for recall or briefing.",
                    }
                ],
                "items": [],
                "sources": [
                    "threads",
                    "thread_sessions",
                    "thread_events",
                    "continuity_recall",
                    "contradiction_cases",
                    "trust_signals",
                ],
            }
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "get_thread_health_dashboard",
        fake_get_thread_health_dashboard,
    )

    response = memories_legacy_router.get_threads_health_dashboard(user_id=user_id)

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "dashboard": {
            "posture": "critical",
            "total_thread_count": 2,
            "recent_thread_count": 1,
            "stale_thread_count": 0,
            "risky_thread_count": 1,
            "watch_thread_count": 0,
            "thresholds": {
                "recent_window_hours": 24.0,
                "stale_window_hours": 72.0,
                "risky_score_threshold": 2,
            },
            "recent_threads": [
                {
                    "thread": {
                        "id": "thread-1",
                        "title": "Recent thread",
                        "agent_profile_id": "assistant_default",
                        "created_at": "2026-03-18T09:00:00+00:00",
                        "updated_at": "2026-03-18T10:00:00+00:00",
                    },
                    "health_posture": "healthy",
                    "activity_posture": "recent",
                    "risk_posture": "normal",
                    "risk_score": 0,
                    "last_activity_at": "2026-03-18T10:00:00+00:00",
                    "last_conversation_at": "2026-03-18T10:00:00+00:00",
                    "hours_since_last_activity": 2.0,
                    "conversation_event_count": 3,
                    "operational_event_count": 1,
                    "active_session_count": 1,
                    "open_loop_count": 0,
                    "stale_open_loop_count": 0,
                    "unresolved_contradiction_count": 0,
                    "weak_trust_signal_count": 0,
                    "reasons": [
                        "No active contradiction, stale open-loop, or weak-trust pressure is currently visible."
                    ],
                    "recommended_action": "No immediate intervention required.",
                }
            ],
            "stale_threads": [],
            "risky_threads": [
                {
                    "thread": {
                        "id": "thread-2",
                        "title": "Risky thread",
                        "agent_profile_id": "assistant_default",
                        "created_at": "2026-03-18T09:00:00+00:00",
                        "updated_at": "2026-03-18T08:00:00+00:00",
                    },
                    "health_posture": "critical",
                    "activity_posture": "current",
                    "risk_posture": "risky",
                    "risk_score": 3,
                    "last_activity_at": "2026-03-18T08:00:00+00:00",
                    "last_conversation_at": "2026-03-18T08:00:00+00:00",
                    "hours_since_last_activity": 26.0,
                    "conversation_event_count": 2,
                    "operational_event_count": 0,
                    "active_session_count": 0,
                    "open_loop_count": 1,
                    "stale_open_loop_count": 1,
                    "unresolved_contradiction_count": 1,
                    "weak_trust_signal_count": 1,
                    "reasons": ["1 unresolved contradiction case(s).", "1 stale open-loop item(s)."],
                    "recommended_action": "Resolve contradiction cases before trusting this thread for recall or briefing.",
                }
            ],
            "items": [],
            "sources": [
                "threads",
                "thread_sessions",
                "thread_events",
                "continuity_recall",
                "contradiction_cases",
                "trust_signals",
            ],
        }
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id


def test_list_memory_revisions_returns_review_payload(monkeypatch) -> None:
    user_id = uuid4()
    memory_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_list_memory_revision_review_records(store, *, user_id, memory_id, limit):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["memory_id"] = memory_id
        captured["limit"] = limit
        return {
            "items": [
                {
                    "id": "revision-123",
                    "memory_id": str(memory_id),
                    "sequence_no": 1,
                    "action": "ADD",
                    "memory_key": "user.preference.coffee",
                    "previous_value": None,
                    "new_value": {"likes": "black"},
                    "source_event_ids": ["event-1"],
                    "created_at": "2026-03-11T09:00:00+00:00",
                }
            ],
            "summary": {
                "memory_id": str(memory_id),
                "limit": 5,
                "returned_count": 1,
                "total_count": 1,
                "has_more": False,
                "order": ["sequence_no_asc"],
            },
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "list_memory_revision_review_records",
        fake_list_memory_revision_review_records,
    )

    response = memories_legacy_router.list_memory_revisions(memory_id=memory_id, user_id=user_id, limit=5)

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "items": [
            {
                "id": "revision-123",
                "memory_id": str(memory_id),
                "sequence_no": 1,
                "action": "ADD",
                "memory_key": "user.preference.coffee",
                "previous_value": None,
                "new_value": {"likes": "black"},
                "source_event_ids": ["event-1"],
                "created_at": "2026-03-11T09:00:00+00:00",
            }
        ],
        "summary": {
            "memory_id": str(memory_id),
            "limit": 5,
            "returned_count": 1,
            "total_count": 1,
            "has_more": False,
            "order": ["sequence_no_asc"],
        },
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id
    assert captured["memory_id"] == memory_id
    assert captured["limit"] == 5


def test_create_memory_review_label_returns_created_payload(monkeypatch) -> None:
    memory_id = uuid4()
    user_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_create_memory_review_label_record(store, *, user_id, memory_id, label, note):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["memory_id"] = memory_id
        captured["label"] = label
        captured["note"] = note
        return {
            "label": {
                "id": "label-123",
                "memory_id": str(memory_id),
                "reviewer_user_id": str(user_id),
                "label": "correct",
                "note": "Backed by the latest source.",
                "created_at": "2026-03-12T09:00:00+00:00",
            },
            "summary": {
                "memory_id": str(memory_id),
                "total_count": 1,
                "counts_by_label": {
                    "correct": 1,
                    "incorrect": 0,
                    "outdated": 0,
                    "insufficient_evidence": 0,
                },
                "order": ["created_at_asc", "id_asc"],
            },
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "create_memory_review_label_record",
        fake_create_memory_review_label_record,
    )

    response = memories_legacy_router.create_memory_review_label(
        memory_id,
        memories_legacy_router.CreateMemoryReviewLabelRequest(
            user_id=user_id,
            label="correct",
            note="Backed by the latest source.",
        ),
    )

    assert response.status_code == 201
    assert json.loads(response.body) == {
        "label": {
            "id": "label-123",
            "memory_id": str(memory_id),
            "reviewer_user_id": str(user_id),
            "label": "correct",
            "note": "Backed by the latest source.",
            "created_at": "2026-03-12T09:00:00+00:00",
        },
        "summary": {
            "memory_id": str(memory_id),
            "total_count": 1,
            "counts_by_label": {
                "correct": 1,
                "incorrect": 0,
                "outdated": 0,
                "insufficient_evidence": 0,
            },
            "order": ["created_at_asc", "id_asc"],
        },
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["memory_id"] == memory_id
    assert captured["label"] == "correct"
    assert captured["note"] == "Backed by the latest source."


def test_create_memory_review_label_returns_not_found_for_inaccessible_memory(monkeypatch) -> None:
    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url="postgresql://app"))

    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "create_memory_review_label_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(MemoryReviewNotFoundError("memory missing")),
    )

    response = memories_legacy_router.create_memory_review_label(
        uuid4(),
        memories_legacy_router.CreateMemoryReviewLabelRequest(user_id=uuid4(), label="incorrect"),
    )

    assert response.status_code == 404
    assert json.loads(response.body) == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_list_memory_review_labels_returns_deterministic_items_and_summary(monkeypatch) -> None:
    memory_id = uuid4()
    user_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_list_memory_review_label_records(store, *, user_id, memory_id):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["memory_id"] = memory_id
        return {
            "items": [
                {
                    "id": "label-123",
                    "memory_id": str(memory_id),
                    "reviewer_user_id": str(user_id),
                    "label": "incorrect",
                    "note": "Conflicts with the latest event.",
                    "created_at": "2026-03-12T09:00:00+00:00",
                },
                {
                    "id": "label-124",
                    "memory_id": str(memory_id),
                    "reviewer_user_id": str(user_id),
                    "label": "outdated",
                    "note": None,
                    "created_at": "2026-03-12T09:01:00+00:00",
                },
            ],
            "summary": {
                "memory_id": str(memory_id),
                "total_count": 2,
                "counts_by_label": {
                    "correct": 0,
                    "incorrect": 1,
                    "outdated": 1,
                    "insufficient_evidence": 0,
                },
                "order": ["created_at_asc", "id_asc"],
            },
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "list_memory_review_label_records",
        fake_list_memory_review_label_records,
    )

    response = memories_legacy_router.list_memory_review_labels(memory_id=memory_id, user_id=user_id)

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "items": [
            {
                "id": "label-123",
                "memory_id": str(memory_id),
                "reviewer_user_id": str(user_id),
                "label": "incorrect",
                "note": "Conflicts with the latest event.",
                "created_at": "2026-03-12T09:00:00+00:00",
            },
            {
                "id": "label-124",
                "memory_id": str(memory_id),
                "reviewer_user_id": str(user_id),
                "label": "outdated",
                "note": None,
                "created_at": "2026-03-12T09:01:00+00:00",
            },
        ],
        "summary": {
            "memory_id": str(memory_id),
            "total_count": 2,
            "counts_by_label": {
                "correct": 0,
                "incorrect": 1,
                "outdated": 1,
                "insufficient_evidence": 0,
            },
            "order": ["created_at_asc", "id_asc"],
        },
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["memory_id"] == memory_id


def test_list_memory_review_labels_returns_not_found_for_inaccessible_memory(monkeypatch) -> None:
    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url="postgresql://app"))

    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "list_memory_review_label_records",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(MemoryReviewNotFoundError("memory hidden")),
    )

    response = memories_legacy_router.list_memory_review_labels(uuid4(), uuid4())

    assert response.status_code == 404
    assert json.loads(response.body) == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_create_embedding_config_returns_created_payload(monkeypatch) -> None:
    user_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_create_embedding_config_record(store, *, user_id, config):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["config"] = config
        return {
            "embedding_config": {
                "id": "config-123",
                "provider": "openai",
                "model": "text-embedding-3-large",
                "version": "2026-03-12",
                "dimensions": 3,
                "status": "active",
                "metadata": {"task": "memory_retrieval"},
                "created_at": "2026-03-12T10:00:00+00:00",
            }
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(memories_legacy_router, "create_embedding_config_record", fake_create_embedding_config_record)

    response = memories_legacy_router.create_embedding_config(
        memories_legacy_router.CreateEmbeddingConfigRequest(
            user_id=user_id,
            provider="openai",
            model="text-embedding-3-large",
            version="2026-03-12",
            dimensions=3,
            status="active",
            metadata={"task": "memory_retrieval"},
        )
    )

    assert response.status_code == 201
    assert json.loads(response.body) == {
        "embedding_config": {
            "id": "config-123",
            "provider": "openai",
            "model": "text-embedding-3-large",
            "version": "2026-03-12",
            "dimensions": 3,
            "status": "active",
            "metadata": {"task": "memory_retrieval"},
            "created_at": "2026-03-12T10:00:00+00:00",
        }
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id
    assert captured["config"].provider == "openai"


def test_create_embedding_config_returns_bad_request_for_validation_failure(monkeypatch) -> None:
    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url="postgresql://app"))

    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "create_embedding_config_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            EmbeddingConfigValidationError(
                "embedding config already exists for provider/model/version under the user scope: "
                "openai/text-embedding-3-large/2026-03-12"
            )
        ),
    )

    response = memories_legacy_router.create_embedding_config(
        memories_legacy_router.CreateEmbeddingConfigRequest(
            user_id=uuid4(),
            provider="openai",
            model="text-embedding-3-large",
            version="2026-03-12",
            dimensions=3,
            status="active",
            metadata={"task": "memory_retrieval"},
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body) == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_upsert_memory_embedding_routes_success_and_validation_errors(monkeypatch) -> None:
    user_id = uuid4()
    memory_id = uuid4()
    config_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_upsert_memory_embedding_record(store, *, user_id, request):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["request"] = request
        return {
            "embedding": {
                "id": "embedding-123",
                "memory_id": str(memory_id),
                "embedding_config_id": str(config_id),
                "dimensions": 3,
                "vector": [0.1, 0.2, 0.3],
                "created_at": "2026-03-12T10:00:00+00:00",
                "updated_at": "2026-03-12T10:00:00+00:00",
            },
            "write_mode": "created",
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(memories_legacy_router, "upsert_memory_embedding_record", fake_upsert_memory_embedding_record)

    response = memories_legacy_router.upsert_memory_embedding(
        memories_legacy_router.UpsertMemoryEmbeddingRequest(
            user_id=user_id,
            memory_id=memory_id,
            embedding_config_id=config_id,
            vector=[0.1, 0.2, 0.3],
        )
    )

    assert response.status_code == 201
    assert json.loads(response.body)["write_mode"] == "created"
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id
    assert captured["request"].memory_id == memory_id

    monkeypatch.setattr(
        memories_legacy_router,
        "upsert_memory_embedding_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            MemoryEmbeddingValidationError(
                "embedding_config_id must reference an existing embedding config owned by the user"
            )
        ),
    )

    error_response = memories_legacy_router.upsert_memory_embedding(
        memories_legacy_router.UpsertMemoryEmbeddingRequest(
            user_id=user_id,
            memory_id=memory_id,
            embedding_config_id=config_id,
            vector=[0.1, 0.2, 0.3],
        )
    )

    assert error_response.status_code == 400
    assert json.loads(error_response.body) == {
        "detail": {"code": "invalid_request", "message": "The request is invalid"}
    }


def test_retrieve_semantic_memories_routes_success_and_validation_errors(monkeypatch) -> None:
    user_id = uuid4()
    config_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_retrieve_semantic_memory_records(store, *, user_id, request):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["request"] = request
        return {
            "items": [
                {
                    "memory_id": "memory-123",
                    "memory_key": "user.preference.coffee",
                    "value": {"likes": "oat milk"},
                    "source_event_ids": ["event-123"],
                    "created_at": "2026-03-12T10:00:00+00:00",
                    "updated_at": "2026-03-12T10:00:00+00:00",
                    "score": 0.99,
                }
            ],
            "summary": {
                "embedding_config_id": str(config_id),
                "limit": 5,
                "returned_count": 1,
                "similarity_metric": "cosine_similarity",
                "order": ["score_desc", "created_at_asc", "id_asc"],
            },
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "retrieve_semantic_memory_records",
        fake_retrieve_semantic_memory_records,
    )

    response = memories_legacy_router.retrieve_semantic_memories(
        memories_legacy_router.RetrieveSemanticMemoriesRequest(
            user_id=user_id,
            embedding_config_id=config_id,
            query_vector=[0.1, 0.2, 0.3],
            limit=5,
        )
    )

    assert response.status_code == 200
    assert json.loads(response.body)["summary"] == {
        "embedding_config_id": str(config_id),
        "limit": 5,
        "returned_count": 1,
        "similarity_metric": "cosine_similarity",
        "order": ["score_desc", "created_at_asc", "id_asc"],
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id
    assert captured["request"].embedding_config_id == config_id
    assert captured["request"].query_vector == (0.1, 0.2, 0.3)

    monkeypatch.setattr(
        memories_legacy_router,
        "retrieve_semantic_memory_records",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SemanticMemoryRetrievalValidationError(
                "embedding_config_id must reference an existing embedding config owned by the user"
            )
        ),
    )

    error_response = memories_legacy_router.retrieve_semantic_memories(
        memories_legacy_router.RetrieveSemanticMemoriesRequest(
            user_id=user_id,
            embedding_config_id=config_id,
            query_vector=[0.1, 0.2, 0.3],
            limit=5,
        )
    )

    assert error_response.status_code == 400
    assert json.loads(error_response.body) == {
        "detail": {"code": "invalid_request", "message": "The request is invalid"}
    }


def test_memory_embedding_read_routes_return_payload_and_not_found(monkeypatch) -> None:
    user_id = uuid4()
    memory_id = uuid4()
    embedding_id = uuid4()
    settings = Settings(database_url="postgresql://app")

    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "list_memory_embedding_records",
        lambda *_args, **_kwargs: {
            "items": [
                {
                    "id": str(embedding_id),
                    "memory_id": str(memory_id),
                    "embedding_config_id": "config-123",
                    "dimensions": 3,
                    "vector": [0.1, 0.2, 0.3],
                    "created_at": "2026-03-12T10:00:00+00:00",
                    "updated_at": "2026-03-12T10:00:00+00:00",
                }
            ],
            "summary": {
                "memory_id": str(memory_id),
                "total_count": 1,
                "order": ["created_at_asc", "id_asc"],
            },
        },
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_memory_embedding_record",
        lambda *_args, **_kwargs: {
            "embedding": {
                "id": str(embedding_id),
                "memory_id": str(memory_id),
                "embedding_config_id": "config-123",
                "dimensions": 3,
                "vector": [0.1, 0.2, 0.3],
                "created_at": "2026-03-12T10:00:00+00:00",
                "updated_at": "2026-03-12T10:00:00+00:00",
            }
        },
    )

    list_response = memories_legacy_router.list_memory_embeddings(memory_id=memory_id, user_id=user_id)
    detail_response = memories_legacy_router.get_memory_embedding(memory_embedding_id=embedding_id, user_id=user_id)

    assert list_response.status_code == 200
    assert json.loads(list_response.body)["summary"]["memory_id"] == str(memory_id)
    assert detail_response.status_code == 200
    assert json.loads(detail_response.body)["embedding"]["id"] == str(embedding_id)

    monkeypatch.setattr(
        memories_legacy_router,
        "get_memory_embedding_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            MemoryEmbeddingNotFoundError(f"memory embedding {embedding_id} was not found")
        ),
    )

    not_found_response = memories_legacy_router.get_memory_embedding(
        memory_embedding_id=embedding_id,
        user_id=user_id,
    )

    assert not_found_response.status_code == 404
    assert json.loads(not_found_response.body) == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_task_artifact_chunk_embedding_routes_success_and_validation_errors(monkeypatch) -> None:
    user_id = uuid4()
    chunk_id = uuid4()
    config_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_upsert_task_artifact_chunk_embedding_record(store, *, user_id, request):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["request"] = request
        return {
            "embedding": {
                "id": "artifact-embedding-123",
                "task_artifact_id": "artifact-123",
                "task_artifact_chunk_id": str(chunk_id),
                "task_artifact_chunk_sequence_no": 2,
                "embedding_config_id": str(config_id),
                "dimensions": 3,
                "vector": [0.1, 0.2, 0.3],
                "created_at": "2026-03-14T12:00:00+00:00",
                "updated_at": "2026-03-14T12:00:00+00:00",
            },
            "write_mode": "created",
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "upsert_task_artifact_chunk_embedding_record",
        fake_upsert_task_artifact_chunk_embedding_record,
    )

    response = memories_legacy_router.upsert_task_artifact_chunk_embedding(
        memories_legacy_router.UpsertTaskArtifactChunkEmbeddingRequest(
            user_id=user_id,
            task_artifact_chunk_id=chunk_id,
            embedding_config_id=config_id,
            vector=[0.1, 0.2, 0.3],
        )
    )

    assert response.status_code == 201
    assert json.loads(response.body)["write_mode"] == "created"
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id
    assert captured["request"].task_artifact_chunk_id == chunk_id

    monkeypatch.setattr(
        memories_legacy_router,
        "upsert_task_artifact_chunk_embedding_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TaskArtifactChunkEmbeddingValidationError(
                "task_artifact_chunk_id must reference an existing task artifact chunk owned by the user"
            )
        ),
    )

    error_response = memories_legacy_router.upsert_task_artifact_chunk_embedding(
        memories_legacy_router.UpsertTaskArtifactChunkEmbeddingRequest(
            user_id=user_id,
            task_artifact_chunk_id=chunk_id,
            embedding_config_id=config_id,
            vector=[0.1, 0.2, 0.3],
        )
    )

    assert error_response.status_code == 400
    assert json.loads(error_response.body) == {
        "detail": {"code": "invalid_request", "message": "The request is invalid"}
    }


def test_task_artifact_chunk_embedding_read_routes_return_payload_and_not_found(monkeypatch) -> None:
    user_id = uuid4()
    artifact_id = uuid4()
    chunk_id = uuid4()
    embedding_id = uuid4()
    settings = Settings(database_url="postgresql://app")

    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "list_task_artifact_chunk_embedding_records_for_artifact",
        lambda *_args, **_kwargs: {
            "items": [],
            "summary": {
                "total_count": 0,
                "order": ["task_artifact_chunk_sequence_no_asc", "created_at_asc", "id_asc"],
                "scope": {
                    "kind": "artifact",
                    "task_artifact_id": str(artifact_id),
                },
            },
        },
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "list_task_artifact_chunk_embedding_records_for_chunk",
        lambda *_args, **_kwargs: {
            "items": [],
            "summary": {
                "total_count": 0,
                "order": ["task_artifact_chunk_sequence_no_asc", "created_at_asc", "id_asc"],
                "scope": {
                    "kind": "chunk",
                    "task_artifact_id": str(artifact_id),
                    "task_artifact_chunk_id": str(chunk_id),
                },
            },
        },
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_task_artifact_chunk_embedding_record",
        lambda *_args, **_kwargs: {
            "embedding": {
                "id": str(embedding_id),
                "task_artifact_id": str(artifact_id),
                "task_artifact_chunk_id": str(chunk_id),
                "task_artifact_chunk_sequence_no": 2,
                "embedding_config_id": "config-123",
                "dimensions": 3,
                "vector": [0.1, 0.2, 0.3],
                "created_at": "2026-03-14T12:00:00+00:00",
                "updated_at": "2026-03-14T12:00:00+00:00",
            }
        },
    )

    artifact_response = memories_legacy_router.list_task_artifact_chunk_embeddings_for_artifact(
        task_artifact_id=artifact_id,
        user_id=user_id,
    )
    chunk_response = memories_legacy_router.list_task_artifact_chunk_embeddings(
        task_artifact_chunk_id=chunk_id,
        user_id=user_id,
    )
    detail_response = memories_legacy_router.get_task_artifact_chunk_embedding(
        task_artifact_chunk_embedding_id=embedding_id,
        user_id=user_id,
    )

    assert artifact_response.status_code == 200
    assert json.loads(artifact_response.body)["summary"]["scope"]["task_artifact_id"] == str(artifact_id)
    assert chunk_response.status_code == 200
    assert json.loads(chunk_response.body)["summary"]["scope"]["task_artifact_chunk_id"] == str(chunk_id)
    assert detail_response.status_code == 200
    assert json.loads(detail_response.body)["embedding"]["id"] == str(embedding_id)

    monkeypatch.setattr(
        memories_legacy_router,
        "list_task_artifact_chunk_embedding_records_for_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TaskArtifactNotFoundError(f"task artifact {artifact_id} was not found")
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "list_task_artifact_chunk_embedding_records_for_chunk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TaskArtifactChunkEmbeddingNotFoundError(f"task artifact chunk {chunk_id} was not found")
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_task_artifact_chunk_embedding_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TaskArtifactChunkEmbeddingNotFoundError(f"task artifact chunk embedding {embedding_id} was not found")
        ),
    )

    missing_artifact_response = memories_legacy_router.list_task_artifact_chunk_embeddings_for_artifact(
        task_artifact_id=artifact_id,
        user_id=user_id,
    )
    missing_chunk_response = memories_legacy_router.list_task_artifact_chunk_embeddings(
        task_artifact_chunk_id=chunk_id,
        user_id=user_id,
    )
    missing_detail_response = memories_legacy_router.get_task_artifact_chunk_embedding(
        task_artifact_chunk_embedding_id=embedding_id,
        user_id=user_id,
    )

    assert missing_artifact_response.status_code == 404
    assert json.loads(missing_artifact_response.body) == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }
    assert missing_chunk_response.status_code == 404
    assert json.loads(missing_chunk_response.body) == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }
    assert missing_detail_response.status_code == 404
    assert json.loads(missing_detail_response.body) == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_create_entity_returns_created_payload(monkeypatch) -> None:
    user_id = uuid4()
    first_memory_id = uuid4()
    second_memory_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_create_entity_record(store, *, user_id, entity):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["entity"] = entity
        return {
            "entity": {
                "id": "entity-123",
                "entity_type": "project",
                "name": "AliceBot",
                "source_memory_ids": [str(first_memory_id), str(second_memory_id)],
                "created_at": "2026-03-12T10:00:00+00:00",
            }
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(memories_legacy_router, "create_entity_record", fake_create_entity_record)

    response = memories_legacy_router.create_entity(
        memories_legacy_router.CreateEntityRequest(
            user_id=user_id,
            entity_type="project",
            name="AliceBot",
            source_memory_ids=[first_memory_id, second_memory_id],
        )
    )

    assert response.status_code == 201
    assert json.loads(response.body) == {
        "entity": {
            "id": "entity-123",
            "entity_type": "project",
            "name": "AliceBot",
            "source_memory_ids": [str(first_memory_id), str(second_memory_id)],
            "created_at": "2026-03-12T10:00:00+00:00",
        }
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id
    assert captured["entity"].entity_type == "project"
    assert captured["entity"].name == "AliceBot"


def test_create_entity_returns_bad_request_when_source_memory_validation_fails(monkeypatch) -> None:
    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url="postgresql://app"))
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "create_entity_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            EntityValidationError("source_memory_ids must all reference existing memories owned by the user")
        ),
    )

    response = memories_legacy_router.create_entity(
        memories_legacy_router.CreateEntityRequest(
            user_id=uuid4(),
            entity_type="person",
            name="Alex",
            source_memory_ids=[uuid4()],
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body) == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_create_entity_edge_returns_created_payload(monkeypatch) -> None:
    user_id = uuid4()
    from_entity_id = uuid4()
    to_entity_id = uuid4()
    source_memory_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_create_entity_edge_record(store, *, user_id, edge):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["edge"] = edge
        return {
            "edge": {
                "id": "edge-123",
                "from_entity_id": str(from_entity_id),
                "to_entity_id": str(to_entity_id),
                "relationship_type": "works_on",
                "valid_from": "2026-03-12T10:00:00+00:00",
                "valid_to": None,
                "source_memory_ids": [str(source_memory_id)],
                "created_at": "2026-03-12T10:01:00+00:00",
            }
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(memories_legacy_router, "create_entity_edge_record", fake_create_entity_edge_record)

    response = memories_legacy_router.create_entity_edge(
        memories_legacy_router.CreateEntityEdgeRequest(
            user_id=user_id,
            from_entity_id=from_entity_id,
            to_entity_id=to_entity_id,
            relationship_type="works_on",
            valid_from="2026-03-12T10:00:00+00:00",
            source_memory_ids=[source_memory_id],
        )
    )

    assert response.status_code == 201
    assert json.loads(response.body) == {
        "edge": {
            "id": "edge-123",
            "from_entity_id": str(from_entity_id),
            "to_entity_id": str(to_entity_id),
            "relationship_type": "works_on",
            "valid_from": "2026-03-12T10:00:00+00:00",
            "valid_to": None,
            "source_memory_ids": [str(source_memory_id)],
            "created_at": "2026-03-12T10:01:00+00:00",
        }
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id
    assert captured["edge"].from_entity_id == from_entity_id
    assert captured["edge"].to_entity_id == to_entity_id


def test_create_entity_edge_returns_bad_request_for_validation_failure(monkeypatch) -> None:
    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url="postgresql://app"))
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "create_entity_edge_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            EntityEdgeValidationError("valid_to must be greater than or equal to valid_from")
        ),
    )

    response = memories_legacy_router.create_entity_edge(
        memories_legacy_router.CreateEntityEdgeRequest(
            user_id=uuid4(),
            from_entity_id=uuid4(),
            to_entity_id=uuid4(),
            relationship_type="works_on",
            valid_from="2026-03-12T11:00:00+00:00",
            valid_to="2026-03-12T10:00:00+00:00",
            source_memory_ids=[uuid4()],
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body) == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_list_entities_returns_deterministic_payload(monkeypatch) -> None:
    user_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_list_entity_records(store, *, user_id):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        return {
            "items": [
                {
                    "id": "entity-123",
                    "entity_type": "project",
                    "name": "AliceBot",
                    "source_memory_ids": ["memory-1"],
                    "created_at": "2026-03-12T10:00:00+00:00",
                }
            ],
            "summary": {
                "total_count": 1,
                "order": ["created_at_asc", "id_asc"],
            },
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(memories_legacy_router, "list_entity_records", fake_list_entity_records)

    response = memories_legacy_router.list_entities(user_id=user_id)

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "items": [
            {
                "id": "entity-123",
                "entity_type": "project",
                "name": "AliceBot",
                "source_memory_ids": ["memory-1"],
                "created_at": "2026-03-12T10:00:00+00:00",
            }
        ],
        "summary": {
            "total_count": 1,
            "order": ["created_at_asc", "id_asc"],
        },
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id


def test_list_entity_edges_returns_deterministic_payload(monkeypatch) -> None:
    user_id = uuid4()
    entity_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_list_entity_edge_records(store, *, user_id, entity_id):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["entity_id"] = entity_id
        return {
            "items": [
                {
                    "id": "edge-123",
                    "from_entity_id": str(entity_id),
                    "to_entity_id": "entity-456",
                    "relationship_type": "works_on",
                    "valid_from": None,
                    "valid_to": None,
                    "source_memory_ids": ["memory-1"],
                    "created_at": "2026-03-12T10:00:00+00:00",
                }
            ],
            "summary": {
                "entity_id": str(entity_id),
                "total_count": 1,
                "order": ["created_at_asc", "id_asc"],
            },
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(memories_legacy_router, "list_entity_edge_records", fake_list_entity_edge_records)

    response = memories_legacy_router.list_entity_edges(entity_id=entity_id, user_id=user_id)

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "items": [
            {
                "id": "edge-123",
                "from_entity_id": str(entity_id),
                "to_entity_id": "entity-456",
                "relationship_type": "works_on",
                "valid_from": None,
                "valid_to": None,
                "source_memory_ids": ["memory-1"],
                "created_at": "2026-03-12T10:00:00+00:00",
            }
        ],
        "summary": {
            "entity_id": str(entity_id),
            "total_count": 1,
            "order": ["created_at_asc", "id_asc"],
        },
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id
    assert captured["entity_id"] == entity_id


def test_list_entity_edges_returns_not_found_for_inaccessible_entity(monkeypatch) -> None:
    entity_id = uuid4()

    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url="postgresql://app"))
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "list_entity_edge_records",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(EntityNotFoundError(f"entity {entity_id} was not found")),
    )

    response = memories_legacy_router.list_entity_edges(entity_id=entity_id, user_id=uuid4())

    assert response.status_code == 404
    assert json.loads(response.body) == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_get_entity_returns_detail_payload(monkeypatch) -> None:
    user_id = uuid4()
    entity_id = uuid4()
    settings = Settings(database_url="postgresql://app")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_user_connection(database_url: str, current_user_id):
        captured["database_url"] = database_url
        captured["current_user_id"] = current_user_id
        yield object()

    def fake_get_entity_record(store, *, user_id, entity_id):
        captured["store_type"] = type(store).__name__
        captured["user_id"] = user_id
        captured["entity_id"] = entity_id
        return {
            "entity": {
                "id": str(entity_id),
                "entity_type": "person",
                "name": "Alex",
                "source_memory_ids": ["memory-1"],
                "created_at": "2026-03-12T10:00:00+00:00",
            }
        }

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: settings)
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(memories_legacy_router, "get_entity_record", fake_get_entity_record)

    response = memories_legacy_router.get_entity(entity_id=entity_id, user_id=user_id)

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "entity": {
            "id": str(entity_id),
            "entity_type": "person",
            "name": "Alex",
            "source_memory_ids": ["memory-1"],
            "created_at": "2026-03-12T10:00:00+00:00",
        }
    }
    assert captured["database_url"] == "postgresql://app"
    assert captured["current_user_id"] == user_id
    assert captured["user_id"] == user_id
    assert captured["entity_id"] == entity_id


def test_get_entity_returns_not_found_for_inaccessible_entity(monkeypatch) -> None:
    entity_id = uuid4()

    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id):
        yield object()

    monkeypatch.setattr(memories_legacy_router, "get_settings", lambda: Settings(database_url="postgresql://app"))
    monkeypatch.setattr(memories_legacy_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(
        memories_legacy_router,
        "get_entity_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(EntityNotFoundError(f"entity {entity_id} was not found")),
    )

    response = memories_legacy_router.get_entity(entity_id=entity_id, user_id=uuid4())

    assert response.status_code == 404
    assert json.loads(response.body) == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_vnext_memory_review_defers_embedding_until_primary_transaction_closes(monkeypatch) -> None:
    user_id = uuid4()
    memory_id = uuid4()
    transaction_depth = 0
    calls: list[str] = []
    deferred_input = object()
    memory = {
        "id": str(memory_id),
        "memory_key": "review.memory",
        "value": {"text": "Review this memory."},
        "status": "candidate",
        "canonical_text": "Review this memory.",
        "domain": "professional",
        "sensitivity": "internal",
        "metadata_json": {},
    }

    @contextmanager
    def fake_user_connection(_database_url: str, _user_id):
        nonlocal transaction_depth
        transaction_depth += 1
        try:
            yield object()
        finally:
            transaction_depth -= 1

    class FakeStore:
        def get_memory(self, _memory_id: str):
            return memory

        def get_memory_for_update(self, _memory_id: str):
            return memory

        def update_memory(self, *, memory_id: str, patch: dict[str, object], **_kwargs):
            assert transaction_depth == 1
            assert memory_id == str(memory_id)
            memory.update(patch)
            return memory

        def append_revision(self, _revision: dict[str, object], **_kwargs):
            return {}

        def append_event(self, event: dict[str, object]):
            return event

    class AllowedDecision:
        decision = "allowed"

    class FakeMemoryService:
        def __init__(self, _store, *, defer_embeddings: bool = False) -> None:
            assert transaction_depth == 1
            assert defer_embeddings is True
            self.deferred_embedding_inputs = (deferred_input,)

        def lock_supersession_graph(self) -> None:
            pass

        def refresh_memory_derived_state(self, _memory, **_kwargs) -> None:
            assert transaction_depth == 1
            calls.append("refresh")

    def fake_persist(**kwargs) -> None:
        assert transaction_depth == 0
        assert kwargs["result"].deferred_embedding_inputs == (deferred_input,)
        calls.append("embedding")

    store = FakeStore()
    monkeypatch.setattr(vnext_memories_router, "get_settings", lambda: Settings(database_url="postgresql://db"))
    monkeypatch.setattr(vnext_memories_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(vnext_memories_router, "PostgresVNextStore", lambda _conn: store)
    monkeypatch.setattr(vnext_memories_router, "_vnext_authenticated_agent_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(vnext_memories_router, "_vnext_policy_checked", lambda **_kwargs: AllowedDecision())
    monkeypatch.setattr(vnext_memories_router, "VNextMemoryCommitService", FakeMemoryService)
    monkeypatch.setattr(vnext_memories_router, "_persist_vnext_deferred_embeddings", fake_persist)

    response = vnext_memories_router.review_vnext_memory(
        memory_id,
        vnext_memories_router.VNextMemoryReviewRequest(user_id=user_id, action="accept"),
    )

    assert response.status_code == 200
    assert calls == ["refresh", "embedding"]


def test_vnext_consolidation_defers_embedding_until_primary_transaction_closes(monkeypatch) -> None:
    user_id = uuid4()
    memory_id = uuid4()
    transaction_depth = 0
    calls: list[str] = []
    deferred_input = object()

    @contextmanager
    def fake_user_connection(_database_url: str, _user_id):
        nonlocal transaction_depth
        transaction_depth += 1
        try:
            yield object()
        finally:
            transaction_depth -= 1

    class FakeMemoryService:
        def __init__(self, _store, *, defer_embeddings: bool = False) -> None:
            assert transaction_depth == 1
            assert defer_embeddings is True
            self.deferred_embedding_inputs = (deferred_input,)

        def lock_supersession_graph(self) -> None:
            assert transaction_depth == 1

        def accept_consolidation_candidate(self, memory_id: str, **_kwargs):
            assert transaction_depth == 1
            calls.append("accept")
            return {"memory": {"id": memory_id, "status": "active"}}

    def fake_persist(**kwargs) -> None:
        assert transaction_depth == 0
        assert kwargs["result"].deferred_embedding_inputs == (deferred_input,)
        calls.append("embedding")

    monkeypatch.setattr(vnext_memories_router, "get_settings", lambda: Settings(database_url="postgresql://db"))
    monkeypatch.setattr(vnext_memories_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(vnext_memories_router, "PostgresVNextStore", lambda _conn: object())
    monkeypatch.setattr(vnext_memories_router, "_vnext_authenticated_agent_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(vnext_memories_router, "VNextMemoryCommitService", FakeMemoryService)
    monkeypatch.setattr(vnext_memories_router, "_persist_vnext_deferred_embeddings", fake_persist)

    response = vnext_memories_router.accept_vnext_memory_consolidation(
        vnext_memories_router.VNextMemoryAcceptConsolidationRequest(
            user_id=user_id,
            memory_id=memory_id,
            reason="Merge reviewed duplicates.",
        )
    )

    assert response.status_code == 200
    assert calls == ["accept", "embedding"]


def test_vnext_project_review_defers_embedding_and_preserves_human_attribution(monkeypatch) -> None:
    user_id = uuid4()
    transaction_depth = 0
    calls: list[str] = []
    review_kwargs: dict[str, object] = {}
    deferred_input = object()
    decision = main_module.PolicyDecision(
        decision="allowed",
        action="artifact.review",
        permission_profile="admin_agent",
        trace_id="policy-trace-1",
    )

    @contextmanager
    def fake_user_connection(_database_url: str, _user_id):
        nonlocal transaction_depth
        transaction_depth += 1
        try:
            yield object()
        finally:
            transaction_depth -= 1

    class FakeProjectService:
        def __init__(self, _store, *, defer_embeddings: bool = False) -> None:
            assert transaction_depth == 1
            assert defer_embeddings is True
            self.deferred_embedding_inputs = (deferred_input,)

        def review_project_update(self, **kwargs):
            assert transaction_depth == 1
            review_kwargs.update(kwargs)
            calls.append("review")
            return {"id": "artifact-1", "status": "accepted"}

    def fake_persist(**kwargs) -> None:
        assert transaction_depth == 0
        assert kwargs["result"].deferred_embedding_inputs == (deferred_input,)
        assert kwargs["actor_type"] == "user"
        assert kwargs["actor_id"] == str(user_id)
        assert kwargs["trace_id"] == "request-trace-1"
        calls.append("embedding")

    monkeypatch.setattr(vnext_review_router, "get_settings", lambda: Settings(database_url="postgresql://db"))
    monkeypatch.setattr(vnext_review_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(vnext_review_router, "PostgresVNextStore", lambda _conn: object())
    monkeypatch.setattr(vnext_review_router, "_vnext_authenticated_agent_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        vnext_review_router,
        "_vnext_authorized_artifact",
        lambda **_kwargs: ({"id": "artifact-1", "status": "needs_review"}, decision),
    )
    monkeypatch.setattr(vnext_review_router, "VNextProjectService", FakeProjectService)
    monkeypatch.setattr(vnext_review_router, "_persist_vnext_deferred_embeddings", fake_persist)

    response = vnext_review_router.review_vnext_project_update_candidate(
        "artifact-1",
        vnext_review_router.VNextProjectUpdateReviewRequest(
            user_id=user_id,
            action="accept",
            trace_id="request-trace-1",
        ),
    )

    assert response.status_code == 200
    assert calls == ["review", "embedding"]
    assert review_kwargs["actor_type"] == "user"
    assert review_kwargs["actor_id"] == str(user_id)
    assert review_kwargs["trace_id"] == "request-trace-1"
    assert review_kwargs["run_id"] is None
