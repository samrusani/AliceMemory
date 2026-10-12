from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import alicebot_api.main as main_module
from alicebot_api.routers import memories_legacy as memories_legacy_router


REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PATH = REPO_ROOT / "apps/api/src/alicebot_api/main.py"
ROUTER_PATH = REPO_ROOT / "apps/api/src/alicebot_api/routers/memories_legacy.py"

ROUTE_NAMES = tuple(
    """
    list_agent_profiles compile_context create_thread list_threads
    get_threads_health_dashboard get_thread list_thread_sessions list_thread_events
    get_thread_resumption_brief list_traces get_trace list_trace_events admit_memory
    list_open_loops get_open_loop create_open_loop update_open_loop_status upsert_consent
    list_consents create_policy list_policies get_policy evaluate_policy list_task_artifacts
    get_task_artifact ingest_task_artifact list_task_artifact_chunks
    retrieve_task_artifact_chunks_for_artifact retrieve_semantic_artifact_chunks_for_artifact
    extract_explicit_preferences extract_explicit_commitments capture_explicit_signals
    list_memories list_memory_review_queue get_memories_quality_gate
    get_memories_trust_dashboard get_memories_hygiene_dashboard
    get_memories_evaluation_summary retrieve_semantic_memories get_memory
    list_memory_revisions create_memory_review_label list_memory_review_labels
    create_embedding_config list_embedding_configs upsert_memory_embedding
    upsert_task_artifact_chunk_embedding list_memory_embeddings
    list_task_artifact_chunk_embeddings_for_artifact list_task_artifact_chunk_embeddings
    get_memory_embedding get_task_artifact_chunk_embedding create_entity create_entity_edge
    list_entities list_entity_edges get_entity
    """.split()
)
SUPPORT_NAMES = tuple(
    """
    CompileContextSemanticRequest CompileContextTaskScopedArtifactRetrievalRequest
    CompileContextArtifactScopedArtifactRetrievalRequest CompileContextArtifactRetrievalRequest
    CompileContextTaskScopedSemanticArtifactRetrievalRequest
    CompileContextArtifactScopedSemanticArtifactRetrievalRequest
    CompileContextSemanticArtifactRetrievalRequest CompileContextRequest CreateThreadRequest
    AdmitMemoryOpenLoopRequest AdmitMemoryRequest ExtractExplicitPreferencesRequest
    ExtractExplicitCommitmentsRequest CaptureExplicitSignalsRequest CreateMemoryReviewLabelRequest
    CreateOpenLoopRequest UpdateOpenLoopStatusRequest CreateEntityRequest CreateEntityEdgeRequest
    CreateEmbeddingConfigRequest UpsertMemoryEmbeddingRequest UpsertTaskArtifactChunkEmbeddingRequest
    RetrieveSemanticMemoriesRequest RetrieveSemanticArtifactChunksRequest UpsertConsentRequest
    CreatePolicyRequest EvaluatePolicyRequest IngestTaskArtifactRequest RetrieveArtifactChunksRequest
    _serialize_thread _thread_agent_profile_id _serialize_thread_session _serialize_thread_event
    """.split()
)
ROUTER_NAMES = {
    "core_router",
    "task_artifact_router",
    "task_artifact_retrieval_router",
    "task_artifact_semantic_router",
    "signals_router",
    "memory_router",
}

EXPECTED_ROUTE_AST_SHA256 = "fe7161e22a80ee48d6ab3be642fe77585c60db692138979d5f242e2214acb9c4"
# Re-pinned 2026-10-11 (write limits on caller JSON). The free JSON field of five request models (`value`, `metadata`,
# `conditions` and `attributes`) is typed `BoundedJsonValue` or `BoundedJsonObject` (alicebot_api.http_json_types), so a
# value nested past 64 levels, or one that holds NaN or Infinity, is HTTP 422 before the handler runs. Previous receipt
# 9ab78c03.... The route definitions, the set of definitions and the published schema are the same.
EXPECTED_SUPPORT_AST_SHA256 = "bff93fce9dc6bc7693c1bdcc841c51f00ad862178721763b9b9f316d0958c30e"
EXPECTED_OPERATION_MANIFEST_SHA256 = "14109d63265453c664352c357d930b91cc617257dda1034ad0917a5ce974b34e"

INTEGRATION_SETTINGS_PATCH_COUNTS = {
    "tests/integration/test_context_compile.py": 14,
    "tests/integration/test_continuity_api.py": 7,
    "tests/integration/test_embeddings_api.py": 8,
    "tests/integration/test_entities_api.py": 4,
    "tests/integration/test_entity_edges_api.py": 4,
    "tests/integration/test_explicit_commitments_api.py": 3,
    "tests/integration/test_explicit_preferences_api.py": 5,
    "tests/integration/test_explicit_signal_capture_api.py": 3,
    "tests/integration/test_memory_admission.py": 5,
    "tests/integration/test_memory_quality_gate_api.py": 6,
    "tests/integration/test_memory_review_api.py": 12,
    "tests/integration/test_memory_review_labels_api.py": 4,
    "tests/integration/test_mvp_magnesium_reorder_flow.py": 1,
    "tests/integration/test_open_loops_api.py": 5,
    "tests/integration/test_policy_api.py": 7,
    "tests/integration/test_task_artifact_chunk_embeddings_api.py": 3,
    "tests/integration/test_task_artifacts_api.py": 15,
    "tests/integration/test_traces_api.py": 2,
}


def _bound_names(node: ast.AST) -> set[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return {
            descendant.id for target in targets for descendant in ast.walk(target) if isinstance(descendant, ast.Name)
        }
    return set()


def _top_level_definitions(tree: ast.Module) -> dict[str, ast.AST]:
    definitions: dict[str, ast.AST] = {}
    for node in tree.body:
        for name in _bound_names(node):
            definitions[name] = node
    return definitions


def _ast_digest(
    definitions: dict[str, ast.AST],
    names: tuple[str, ...],
    *,
    strip_decorators: bool,
) -> str:
    payload: list[str] = []
    for name in names:
        node = deepcopy(definitions[name])
        if strip_decorators:
            assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            node.decorator_list = []
        payload.append(f"{name}:{ast.dump(node, include_attributes=False)}")
    return hashlib.sha256("\n".join(payload).encode()).hexdigest()


def _import_bindings(tree: ast.Module) -> set[str]:
    bindings: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        bindings.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    return bindings


def _settings_patch_counts(function: ast.FunctionDef) -> tuple[int, int]:
    main_count = 0
    router_count = 0
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "setattr"
            and isinstance(node.args[0], ast.Name)
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "get_settings"
        ):
            continue
        if node.args[0].id == "main_module":
            main_count += 1
        if node.args[0].id == "memories_legacy_router":
            router_count += 1
    return main_count, router_count


def _setattr_attribute_counts(path: str, target: str) -> dict[str, int]:
    tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))
    counts: dict[str, int] = {}

    def dotted_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            owner = dotted_name(node.value)
            return None if owner is None else f"{owner}.{node.attr}"
        return None

    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setattr"
            and len(node.args) >= 2
            and dotted_name(node.args[0]) == target
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            continue
        attribute = node.args[1].value
        counts[attribute] = counts.get(attribute, 0) + 1
    return counts


def test_moved_handlers_and_supports_are_exact_mechanical_copies() -> None:
    router_tree = ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))
    definitions = _top_level_definitions(router_tree)

    assert len(ROUTE_NAMES) == 57
    assert len(SUPPORT_NAMES) == 33
    assert set(ROUTE_NAMES) <= definitions.keys()
    assert set(SUPPORT_NAMES) <= definitions.keys()
    assert _ast_digest(definitions, ROUTE_NAMES, strip_decorators=True) == EXPECTED_ROUTE_AST_SHA256
    assert _ast_digest(definitions, SUPPORT_NAMES, strip_decorators=False) == EXPECTED_SUPPORT_AST_SHA256

    decorated_routes = []
    for node in router_tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id in ROUTER_NAMES
            for decorator in node.decorator_list
        ):
            decorated_routes.append(node.name)
    assert decorated_routes == list(ROUTE_NAMES)


def test_main_no_longer_owns_moved_definitions_or_dependency_imports() -> None:
    main_tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    router_tree = ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))
    main_definitions = _top_level_definitions(main_tree)

    assert not (set(ROUTE_NAMES) & main_definitions.keys())
    assert not (set(SUPPORT_NAMES) & main_definitions.keys())

    main_load_counts: dict[str, int] = {}
    for node in ast.walk(main_tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            main_load_counts[node.id] = main_load_counts.get(node.id, 0) + 1
    stale_router_only_imports = {
        name
        for name in _import_bindings(main_tree) & _import_bindings(router_tree)
        if main_load_counts.get(name, 0) == 0
    }
    assert stale_router_only_imports == {"_json_object", "_json_value"}


def test_memories_legacy_router_has_no_main_import_cycle() -> None:
    tree = ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(not alias.name.startswith("alicebot_api.main") for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        assert not (node.level == 0 and module.startswith("alicebot_api.main"))
        assert not (node.level == 0 and module == "alicebot_api" and any(alias.name == "main" for alias in node.names))
        assert not (
            node.level > 0
            and (module == "main" or module.startswith("main.") or any(alias.name == "main" for alias in node.names))
        )


def test_moved_openapi_operations_are_registered_once_with_stable_ids() -> None:
    schema = main_module.app.openapi()
    moved_routes = [
        route
        for router in (
            memories_legacy_router.core_router,
            memories_legacy_router.task_artifact_router,
            memories_legacy_router.task_artifact_retrieval_router,
            memories_legacy_router.task_artifact_semantic_router,
            memories_legacy_router.signals_router,
            memories_legacy_router.memory_router,
        )
        for route in router.routes
    ]
    observed: list[tuple[str, str, str]] = []
    for route in moved_routes:
        for method in sorted(route.methods or set()):
            operation = schema["paths"][route.path][method.lower()]
            observed.append((method, route.path, operation["operationId"]))
            assert operation["operationId"] == route.unique_id
    assert len(observed) == len(set(observed)) == 57
    operation_manifest_sha256 = hashlib.sha256(json.dumps(observed, separators=(",", ":")).encode()).hexdigest()
    assert operation_manifest_sha256 == EXPECTED_OPERATION_MANIFEST_SHA256


def test_integration_get_settings_patches_follow_defining_module_per_test() -> None:
    for relative_path, expected_count in INTEGRATION_SETTINGS_PATCH_COUNTS.items():
        tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
        observed_main = 0
        observed_router = 0
        for function in (node for node in tree.body if isinstance(node, ast.FunctionDef)):
            main_count, router_count = _settings_patch_counts(function)
            assert main_count == router_count, (relative_path, function.name)
            observed_main += main_count
            observed_router += router_count
        assert (observed_main, observed_router) == (expected_count, expected_count)

    semantic_path = REPO_ROOT / "tests/integration/test_semantic_artifact_chunk_retrieval_api.py"
    semantic_tree = ast.parse(semantic_path.read_text(encoding="utf-8"))
    semantic_counts = {
        function.name: _settings_patch_counts(function)
        for function in semantic_tree.body
        if isinstance(function, ast.FunctionDef) and any(_settings_patch_counts(function))
    }
    assert semantic_counts == {
        "test_semantic_artifact_chunk_retrieval_endpoints_return_deterministic_task_and_artifact_results": (1, 1),
        "test_semantic_artifact_chunk_retrieval_rejects_invalid_config_dimension_mismatch_and_cross_user_scope": (1, 1),
        "test_semantic_artifact_chunk_retrieval_supports_empty_results_and_per_user_isolation": (1, 0),
    }
    assert sum(INTEGRATION_SETTINGS_PATCH_COUNTS.values()) + 2 == 110


def test_direct_unit_monkeypatches_follow_moved_definition_ownership() -> None:
    expected_targets = {
        "tests/unit/test_artifacts_main.py": {
            "main_module": {},
            "legacy_gated_router": {
                "get_settings": 8,
                "register_task_artifact_record": 3,
                "retrieve_task_scoped_artifact_chunk_records": 3,
                "retrieve_task_scoped_semantic_artifact_chunk_records": 2,
                "user_connection": 8,
            },
            "memories_legacy_router": {
                "get_settings": 7,
                "get_task_artifact_record": 1,
                "ingest_task_artifact_record": 2,
                "list_task_artifact_chunk_records": 1,
                "list_task_artifact_records": 1,
                "retrieve_artifact_scoped_artifact_chunk_records": 1,
                "retrieve_artifact_scoped_semantic_artifact_chunk_records": 1,
                "user_connection": 7,
            },
        },
        "tests/unit/test_events.py": {
            "main_module": {},
            "memories_legacy_router": {
                "ContinuityStore": 1,
                "get_settings": 1,
                "user_connection": 1,
            },
        },
        "tests/unit/test_main.py": {
            "main_module": {
                "_BROWSER_CLIP_SIMPLE_BODY_MAX_BYTES": 3,
                "_resolve_vnext_http_auth": 1,
                "get_settings": 5,
                "ping_database": 2,
            },
            "providers_router": {
                "ContinuityStore": 3,
                "ResponseGenerationJobStore": 1,
                "_assert_provider_write_context": 2,
                "_register_workspace_provider": 1,
                "_require_local_provider_workspace": 1,
                "_resolve_authenticated_v1_user_id": 1,
                "_resolve_owned_provider_workspace": 1,
                "delete_provider_api_key": 2,
                "get_settings": 1,
                "resolve_runtime_provider_config_secrets": 1,
                "set_current_user_account": 1,
                "user_connection": 1,
                "validate_provider_base_url": 1,
                "write_provider_api_key": 1,
            },
            "providers_router.psycopg": {
                "connect": 2,
            },
            "memories_legacy_router": {
                "admit_memory_candidate": 3,
                "compile_and_persist_trace": 5,
                "create_embedding_config_record": 2,
                "create_entity_edge_record": 2,
                "create_entity_record": 2,
                "create_memory_review_label_record": 2,
                "create_open_loop_record": 1,
                "extract_and_admit_explicit_commitments": 2,
                "extract_and_admit_explicit_preferences": 2,
                "extract_and_admit_explicit_signals": 2,
                "get_entity_record": 2,
                "get_memory_embedding_record": 2,
                "get_memory_evaluation_summary": 1,
                "get_memory_hygiene_dashboard_summary": 1,
                "get_memory_quality_gate_summary": 1,
                "get_memory_review_record": 1,
                "get_open_loop_record": 2,
                "get_settings": 42,
                "get_task_artifact_chunk_embedding_record": 2,
                "get_thread_health_dashboard": 1,
                "list_entity_edge_records": 2,
                "list_entity_records": 1,
                "list_memory_embedding_records": 1,
                "list_memory_review_label_records": 2,
                "list_memory_review_queue_records": 1,
                "list_memory_review_records": 1,
                "list_memory_revision_review_records": 1,
                "list_open_loop_records": 1,
                "list_task_artifact_chunk_embedding_records_for_artifact": 2,
                "list_task_artifact_chunk_embedding_records_for_chunk": 2,
                "retrieve_semantic_memory_records": 2,
                "update_open_loop_status_record": 2,
                "upsert_memory_embedding_record": 2,
                "upsert_task_artifact_chunk_embedding_record": 2,
                "user_connection": 42,
            },
            "memories_legacy_router.ContinuityStore": {
                "get_thread": 3,
            },
        },
        "tests/unit/test_policy_main.py": {
            "main_module": {},
            "memories_legacy_router": {
                "evaluate_policy_request": 2,
                "get_policy_record": 1,
                "get_settings": 4,
                "upsert_consent_record": 1,
                "user_connection": 4,
            },
        },
        "tests/unit/test_traces.py": {
            "main_module": {},
            "memories_legacy_router": {
                "get_settings": 3,
                "get_trace_record": 1,
                "list_trace_event_records": 1,
                "list_trace_records": 1,
                "user_connection": 3,
            },
        },
    }
    for relative_path, expected_by_target in expected_targets.items():
        for target, expected_counts in expected_by_target.items():
            assert _setattr_attribute_counts(relative_path, target) == expected_counts
