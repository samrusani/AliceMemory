from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from alicebot_api.routers import legacy_gated as legacy_gated_router
from alicebot_api.routers import memories_legacy as memories_legacy_router


REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PATH = REPO_ROOT / "apps/api/src/alicebot_api/main.py"
ROUTER_PATH = REPO_ROOT / "apps/api/src/alicebot_api/routers/legacy_gated.py"

ROUTE_NAMES = tuple(
    """
    create_tool list_tools evaluate_tools_allowlist route_tool create_approval_request
    list_approvals get_approval approve_approval reject_approval execute_approved_proxy
    list_tasks get_task create_task_run list_task_runs get_task_run tick_task_run
    pause_task_run resume_task_run cancel_task_run connect_gmail_account
    list_gmail_accounts get_gmail_account ingest_gmail_message connect_calendar_account
    list_calendar_accounts get_calendar_account list_calendar_events ingest_calendar_event
    create_task_workspace list_task_workspaces get_task_workspace list_task_steps get_task_step
    register_task_artifact retrieve_task_artifact_chunks retrieve_semantic_task_artifact_chunks
    create_next_task_step transition_task_step create_execution_budget list_execution_budgets
    get_execution_budget deactivate_execution_budget supersede_execution_budget
    list_tool_executions get_tool_execution get_tool post_v0_task_brief_compile
    get_v0_task_brief post_v0_task_brief_compare
    """.split()
)
OWNED_SUPPORT_NAMES = tuple(
    """
    CreateToolRequest EvaluateToolAllowlistRequest RouteToolRequest CreateApprovalRequest
    ResolveApprovalRequest ExecuteApprovedProxyRequest ConnectGmailAccountRequest
    IngestGmailMessageRequest ConnectCalendarAccountRequest IngestCalendarEventRequest
    CreateTaskWorkspaceRequest RegisterTaskArtifactRequest TaskStepRequestSnapshot
    TaskStepOutcomeRequest TaskStepLineageRequest CreateNextTaskStepRequest
    TransitionTaskStepRequest _task_step_request_record _task_step_outcome_snapshot
    CreateTaskRunRequest MutateTaskRunRequest CreateExecutionBudgetRequest
    DeactivateExecutionBudgetRequest SupersedeExecutionBudgetRequest TaskBriefCompileSpec
    TaskBriefCompileRequest TaskBriefCompareRequest _mutate_task_run
    """.split()
)
FULL_SUPPORT_NAMES = (
    "RetrieveSemanticArtifactChunksRequest",
    *OWNED_SUPPORT_NAMES[:12],
    "RetrieveArtifactChunksRequest",
    *OWNED_SUPPORT_NAMES[12:],
)
PARTITION_ROUTE_NAMES = {
    "core_router": ROUTE_NAMES[:34],
    "task_artifact_retrieval_router": ROUTE_NAMES[34:35],
    "task_artifact_semantic_router": ROUTE_NAMES[35:36],
    "operations_router": ROUTE_NAMES[36:46],
    "task_brief_router": ROUTE_NAMES[46:],
}
EXPECTED_ROUTE_MANIFEST = (
    ("core_router", "post", "/v0/tools", "create_tool"),
    ("core_router", "get", "/v0/tools", "list_tools"),
    (
        "core_router",
        "post",
        "/v0/tools/allowlist/evaluate",
        "evaluate_tools_allowlist",
    ),
    ("core_router", "post", "/v0/tools/route", "route_tool"),
    ("core_router", "post", "/v0/approvals/requests", "create_approval_request"),
    ("core_router", "get", "/v0/approvals", "list_approvals"),
    ("core_router", "get", "/v0/approvals/{approval_id}", "get_approval"),
    (
        "core_router",
        "post",
        "/v0/approvals/{approval_id}/approve",
        "approve_approval",
    ),
    (
        "core_router",
        "post",
        "/v0/approvals/{approval_id}/reject",
        "reject_approval",
    ),
    (
        "core_router",
        "post",
        "/v0/approvals/{approval_id}/execute",
        "execute_approved_proxy",
    ),
    ("core_router", "get", "/v0/tasks", "list_tasks"),
    ("core_router", "get", "/v0/tasks/{task_id}", "get_task"),
    ("core_router", "post", "/v0/tasks/{task_id}/runs", "create_task_run"),
    ("core_router", "get", "/v0/tasks/{task_id}/runs", "list_task_runs"),
    ("core_router", "get", "/v0/task-runs/{task_run_id}", "get_task_run"),
    ("core_router", "post", "/v0/task-runs/{task_run_id}/tick", "tick_task_run"),
    ("core_router", "post", "/v0/task-runs/{task_run_id}/pause", "pause_task_run"),
    ("core_router", "post", "/v0/task-runs/{task_run_id}/resume", "resume_task_run"),
    ("core_router", "post", "/v0/task-runs/{task_run_id}/cancel", "cancel_task_run"),
    ("core_router", "post", "/v0/gmail-accounts", "connect_gmail_account"),
    ("core_router", "get", "/v0/gmail-accounts", "list_gmail_accounts"),
    (
        "core_router",
        "get",
        "/v0/gmail-accounts/{gmail_account_id}",
        "get_gmail_account",
    ),
    (
        "core_router",
        "post",
        "/v0/gmail-accounts/{gmail_account_id}/messages/{provider_message_id}/ingest",
        "ingest_gmail_message",
    ),
    ("core_router", "post", "/v0/calendar-accounts", "connect_calendar_account"),
    ("core_router", "get", "/v0/calendar-accounts", "list_calendar_accounts"),
    (
        "core_router",
        "get",
        "/v0/calendar-accounts/{calendar_account_id}",
        "get_calendar_account",
    ),
    (
        "core_router",
        "get",
        "/v0/calendar-accounts/{calendar_account_id}/events",
        "list_calendar_events",
    ),
    (
        "core_router",
        "post",
        "/v0/calendar-accounts/{calendar_account_id}/events/{provider_event_id}/ingest",
        "ingest_calendar_event",
    ),
    (
        "core_router",
        "post",
        "/v0/tasks/{task_id}/workspace",
        "create_task_workspace",
    ),
    ("core_router", "get", "/v0/task-workspaces", "list_task_workspaces"),
    (
        "core_router",
        "get",
        "/v0/task-workspaces/{task_workspace_id}",
        "get_task_workspace",
    ),
    ("core_router", "get", "/v0/tasks/{task_id}/steps", "list_task_steps"),
    ("core_router", "get", "/v0/task-steps/{task_step_id}", "get_task_step"),
    (
        "core_router",
        "post",
        "/v0/task-workspaces/{task_workspace_id}/artifacts",
        "register_task_artifact",
    ),
    (
        "task_artifact_retrieval_router",
        "post",
        "/v0/tasks/{task_id}/artifact-chunks/retrieve",
        "retrieve_task_artifact_chunks",
    ),
    (
        "task_artifact_semantic_router",
        "post",
        "/v0/tasks/{task_id}/artifact-chunks/semantic-retrieval",
        "retrieve_semantic_task_artifact_chunks",
    ),
    (
        "operations_router",
        "post",
        "/v0/tasks/{task_id}/steps",
        "create_next_task_step",
    ),
    (
        "operations_router",
        "post",
        "/v0/task-steps/{task_step_id}/transition",
        "transition_task_step",
    ),
    (
        "operations_router",
        "post",
        "/v0/execution-budgets",
        "create_execution_budget",
    ),
    (
        "operations_router",
        "get",
        "/v0/execution-budgets",
        "list_execution_budgets",
    ),
    (
        "operations_router",
        "get",
        "/v0/execution-budgets/{execution_budget_id}",
        "get_execution_budget",
    ),
    (
        "operations_router",
        "post",
        "/v0/execution-budgets/{execution_budget_id}/deactivate",
        "deactivate_execution_budget",
    ),
    (
        "operations_router",
        "post",
        "/v0/execution-budgets/{execution_budget_id}/supersede",
        "supersede_execution_budget",
    ),
    ("operations_router", "get", "/v0/tool-executions", "list_tool_executions"),
    (
        "operations_router",
        "get",
        "/v0/tool-executions/{execution_id}",
        "get_tool_execution",
    ),
    ("operations_router", "get", "/v0/tools/{tool_id}", "get_tool"),
    (
        "task_brief_router",
        "post",
        "/v0/task-briefs/compile",
        "post_v0_task_brief_compile",
    ),
    (
        "task_brief_router",
        "get",
        "/v0/task-briefs/{task_brief_id}",
        "get_v0_task_brief",
    ),
    (
        "task_brief_router",
        "post",
        "/v0/task-briefs/compare",
        "post_v0_task_brief_compare",
    ),
)

EXPECTED_ROUTE_AST_SHA256 = "9666f75cff198b78e45c1aed6c1a3259c20b7e2e53771be633399fd77228b9a4"
EXPECTED_OWNED_SUPPORT_AST_SHA256 = "8bbbcb0cf52c4bd1da5fce4e50878d8a62599be59ee25e09dcd905c94950d406"
EXPECTED_FULL_SUPPORT_AST_SHA256 = "07acb5deafb700e040c459969610e8877e86e62b04d4e9d86493fe314cb02868"
EXPECTED_GATED_OPERATION_SHA256 = "55490460fd78990a6872ebf22c6cfd927f7d0a5548b6df90adde8261ede8da65"

# Source regeneration adds one default route before source review.
EXPECTED_DEFAULT_DEEP_ROUTE_SHA256 = "9d9e83c35818387a13c719d455c88f4dcb4d139bf5f4dd5888bc6ff3723077a7"
EXPECTED_LEGACY_DEEP_ROUTE_SHA256 = "5ba780b5ec62584b844cf1fcab8fc35b703f4494c1486db870588ab68a1f57a6"

EXPECTED_INTEGRATION_PATCH_COUNTS = {
    "tests/integration/test_approval_api.py": 8,
    "tests/integration/test_calendar_accounts_api.py": 7,
    "tests/integration/test_execution_budgets_api.py": 8,
    "tests/integration/test_gmail_accounts_api.py": 11,
    "tests/integration/test_mvp_magnesium_reorder_flow.py": 1,
    "tests/integration/test_proxy_execution_api.py": 17,
    "tests/integration/test_semantic_artifact_chunk_retrieval_api.py": 3,
    "tests/integration/test_task_artifacts_api.py": 15,
    "tests/integration/test_task_briefing_api.py": 1,
    "tests/integration/test_task_runs_api.py": 3,
    "tests/integration/test_task_workspaces_api.py": 1,
    "tests/integration/test_tasks_api.py": 3,
    "tests/integration/test_tool_api.py": 8,
}

EXPECTED_DIRECT_UNIT_PATCHES = {
    "tests/unit/test_approvals_main.py": {
        "approve_approval_record": 3,
        "get_approval_record": 1,
        "get_settings": 9,
        "list_approval_records": 1,
        "reject_approval_record": 2,
        "submit_approval_request": 2,
        "user_connection": 9,
    },
    "tests/unit/test_artifacts_main.py": {
        "get_settings": 8,
        "register_task_artifact_record": 3,
        "retrieve_task_scoped_artifact_chunk_records": 3,
        "retrieve_task_scoped_semantic_artifact_chunk_records": 2,
        "user_connection": 8,
    },
    "tests/unit/test_calendar_main.py": {
        "create_calendar_account_record": 3,
        "get_calendar_account_record": 1,
        "get_settings": 8,
        "ingest_calendar_event_record": 7,
        "list_calendar_account_records": 1,
        "list_calendar_event_records": 5,
        "user_connection": 8,
    },
    "tests/unit/test_execution_budgets_main.py": {
        "create_execution_budget_record": 2,
        "deactivate_execution_budget_record": 2,
        "get_execution_budget_record": 2,
        "get_settings": 8,
        "list_execution_budget_records": 1,
        "supersede_execution_budget_record": 1,
        "user_connection": 8,
    },
    "tests/unit/test_executions_main.py": {
        "get_settings": 3,
        "get_tool_execution_record": 2,
        "list_tool_execution_records": 1,
        "user_connection": 3,
    },
    "tests/unit/test_gmail_main.py": {
        "create_gmail_account_record": 3,
        "get_gmail_account_record": 1,
        "get_settings": 7,
        "ingest_gmail_message_record": 8,
        "list_gmail_account_records": 1,
        "user_connection": 7,
    },
    "tests/unit/test_proxy_execution_main.py": {
        "get_settings": 7,
        "user_connection": 7,
    },
    "tests/unit/test_task_runs_main.py": {
        "cancel_task_run_record": 1,
        "create_task_run_record": 3,
        "get_settings": 5,
        "get_task_run_record": 2,
        "list_task_run_records": 1,
        "pause_task_run_record": 1,
        "resume_task_run_record": 1,
        "tick_task_run_record": 1,
        "user_connection": 5,
    },
    "tests/unit/test_tasks_main.py": {
        "create_next_task_step_record": 1,
        "get_settings": 5,
        "get_task_step_record": 1,
        "list_task_step_records": 2,
        "transition_task_step_record": 1,
        "user_connection": 5,
    },
    "tests/unit/test_tools_main.py": {
        "create_tool_record": 1,
        "evaluate_tool_allowlist": 2,
        "get_settings": 6,
        "get_tool_record": 1,
        "route_tool_invocation": 2,
        "user_connection": 6,
    },
    "tests/unit/test_workspaces_main.py": {
        "create_task_workspace_record": 2,
        "get_settings": 4,
        "get_task_workspace_record": 1,
        "list_task_workspace_records": 1,
        "user_connection": 4,
    },
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
    return {name: node for node in tree.body for name in _bound_names(node)}


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


def _setattr_attribute_counts(path: str, target: str) -> dict[str, int]:
    tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == target
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            continue
        attribute = node.args[1].value
        counts[attribute] = counts.get(attribute, 0) + 1
    return counts


def _settings_patch_count(function: ast.FunctionDef, target: str) -> int:
    return sum(
        1
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == target
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "get_settings"
    )


def _isolated_surface_manifest(flag_value: str | None) -> dict[str, object]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "apps/api/src")
    if flag_value is None:
        env.pop("ALICE_LEGACY_SURFACES", None)
    else:
        env["ALICE_LEGACY_SURFACES"] = flag_value
    script = """
import hashlib
import json
import sys
from alicebot_api.main import app, LEGACY_HTTP_OPERATION_KEYS

schema = app.openapi()
operations = {
    (method.upper(), path)
    for path, item in schema["paths"].items()
    for method in item
    if method in {"get", "post", "put", "patch", "delete"}
}
deep_records = []
gated_records = []
gated_modules = set()
for route in app.router.routes:
    effective_route_contexts = getattr(route, "effective_route_contexts", None)
    contexts = effective_route_contexts() if callable(effective_route_contexts) else (route,)
    for context in contexts:
        path = str(getattr(context, "path", ""))
        for method in sorted(getattr(context, "methods", None) or set()):
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            deep_records.append((method, path))
            if (method, path) in LEGACY_HTTP_OPERATION_KEYS:
                gated_records.append((method, path, schema["paths"][path][method.lower()]["operationId"]))
                gated_modules.add(context.endpoint.__module__)
print(json.dumps({
    "operation_count": len(operations),
    "legacy_count": len(operations & LEGACY_HTTP_OPERATION_KEYS),
    "deep_count": len(deep_records),
    "deep_digest": hashlib.sha256(json.dumps(deep_records, separators=(",", ":")).encode()).hexdigest(),
    "gated_count": len(gated_records),
    "gated_digest": hashlib.sha256(json.dumps(gated_records, separators=(",", ":")).encode()).hexdigest(),
    "gated_modules": sorted(gated_modules),
    "legacy_module_loaded": "alicebot_api.routers.legacy_gated" in sys.modules,
    "proxy_module_loaded": "alicebot_api.proxy_execution" in sys.modules,
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return payload


def test_legacy_gated_definitions_are_exact_mechanical_moves() -> None:
    tree = ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))
    definitions = _top_level_definitions(tree)

    assert len(ROUTE_NAMES) == 49
    assert len(OWNED_SUPPORT_NAMES) == 28
    assert len(FULL_SUPPORT_NAMES) == 30
    assert set(ROUTE_NAMES) | set(FULL_SUPPORT_NAMES) <= definitions.keys()
    assert _ast_digest(definitions, ROUTE_NAMES, strip_decorators=True) == EXPECTED_ROUTE_AST_SHA256
    assert _ast_digest(definitions, OWNED_SUPPORT_NAMES, strip_decorators=False) == EXPECTED_OWNED_SUPPORT_AST_SHA256
    assert _ast_digest(definitions, FULL_SUPPORT_NAMES, strip_decorators=False) == EXPECTED_FULL_SUPPORT_AST_SHA256


def test_five_partitions_register_exact_handlers_and_modules() -> None:
    tree = ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))
    observed_by_router = {name: [] for name in PARTITION_ROUTE_NAMES}
    observed_manifest = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
            ):
                continue
            if decorator.func.value.id in observed_by_router:
                assert len(decorator.args) == 1
                assert isinstance(decorator.args[0], ast.Constant)
                assert isinstance(decorator.args[0].value, str)
                assert decorator.keywords == []
                observed_by_router[decorator.func.value.id].append(node.name)
                observed_manifest.append(
                    (
                        decorator.func.value.id,
                        decorator.func.attr,
                        decorator.args[0].value,
                        node.name,
                    )
                )
    assert observed_by_router == {name: list(route_names) for name, route_names in PARTITION_ROUTE_NAMES.items()}
    assert tuple(observed_manifest) == EXPECTED_ROUTE_MANIFEST

    for name, expected_names in PARTITION_ROUTE_NAMES.items():
        router = getattr(legacy_gated_router, name)
        assert [route.endpoint.__name__ for route in router.routes] == list(expected_names)
        assert {route.endpoint.__module__ for route in router.routes} == {"alicebot_api.routers.legacy_gated"}


def test_borrowed_request_models_preserve_memories_router_identity() -> None:
    assert legacy_gated_router.RetrieveArtifactChunksRequest is memories_legacy_router.RetrieveArtifactChunksRequest
    assert (
        legacy_gated_router.RetrieveSemanticArtifactChunksRequest
        is memories_legacy_router.RetrieveSemanticArtifactChunksRequest
    )


def test_main_owns_no_moved_definition_and_import_direction_is_one_way() -> None:
    main_tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    router_tree = ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))
    main_definitions = _top_level_definitions(main_tree)
    assert not ((set(ROUTE_NAMES) | set(FULL_SUPPORT_NAMES)) & main_definitions.keys())
    assert not ((set(ROUTE_NAMES) | set(FULL_SUPPORT_NAMES)) & _import_bindings(main_tree))

    for node in router_tree.body:
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

    shared_imports = _import_bindings(main_tree) & _import_bindings(router_tree)
    assert shared_imports == {
        "Callable",
        "JSONResponse",
        "Literal",
        "TaskBriefComparisonResponse",
        "TaskBriefResponse",
        "UUID",
        "_json_object",
        "get_settings",
        "memories_legacy",
        "public_exception_response",
        "user_connection",
    }
    main_loads = {
        node.id for node in ast.walk(main_tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    assert shared_imports - {"_json_object"} <= main_loads
    assert shared_imports - main_loads == {"_json_object"}


def test_main_preserves_frozen_flag_policy_and_five_mount_seams() -> None:
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    definitions = _top_level_definitions(tree)
    flag_node = definitions["LEGACY_SURFACES_ENABLED"]
    policy_node = definitions["_apply_legacy_surface_mount_policy"]
    policy_call = next(
        node
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_apply_legacy_surface_mount_policy"
    )
    digest = lambda node: hashlib.sha256(  # noqa: E731
        ast.dump(node, include_attributes=False).encode()
    ).hexdigest()
    assert digest(flag_node) == "73bf0b4d6071cca7759ea774bbe1b071922cef78966c49a71ea967b048fc2018"
    assert digest(policy_node) == "540065f608a38b4c42fad847296c1aecaa6ad5d2f32bc8aedc997a3a182d62c6"
    assert digest(policy_call) == "b7096af7c6f1438ec832cb76948a0238997adb859cc4180ba11610b964475e17"
    assert (
        sum(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "legacy_surfaces_enabled"
            for node in ast.walk(tree)
        )
        == 1
    )

    conditional_mounts = [
        node
        for node in tree.body
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "LEGACY_SURFACES_ENABLED"
    ]
    assert all(len(node.body) == 1 and node.orelse == [] for node in conditional_mounts)
    assert [ast.unparse(node.body[0]) for node in conditional_mounts] == [
        "app.include_router(legacy_gated.core_router)",
        "app.include_router(legacy_gated.task_artifact_retrieval_router)",
        "app.include_router(legacy_gated.task_artifact_semantic_router)",
        "app.include_router(legacy_gated.operations_router)",
        "app.include_router(legacy_gated.task_brief_router)",
    ]

    expected_order = [
        "memories_legacy.core_router",
        "legacy_gated.core_router",
        "memories_legacy.task_artifact_router",
        "legacy_gated.task_artifact_retrieval_router",
        "memories_legacy.task_artifact_retrieval_router",
        "legacy_gated.task_artifact_semantic_router",
        "memories_legacy.task_artifact_semantic_router",
        "legacy_gated.operations_router",
        "memories_legacy.signals_router",
        "continuity.capture_router",
        "continuity.operations_router",
        "legacy_gated.task_brief_router",
        "memories_legacy.memory_router",
    ]
    relevant = set(expected_order)
    observed = []
    for node in sorted(
        (node for node in ast.walk(tree) if isinstance(node, ast.Call)),
        key=lambda node: (node.lineno, node.col_offset),
    ):
        if not (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "app"
            and node.func.attr == "include_router"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and isinstance(node.args[0].value, ast.Name)
        ):
            continue
        name = f"{node.args[0].value.id}.{node.args[0].attr}"
        if name in relevant:
            observed.append(name)
    assert observed == expected_order


def test_flagged_surface_preserves_deep_order_ids_and_import_timing() -> None:
    default = _isolated_surface_manifest(None)
    assert default == {
        "operation_count": 184,
        "legacy_count": 0,
        "deep_count": 188,
        "deep_digest": EXPECTED_DEFAULT_DEEP_ROUTE_SHA256,
        "gated_count": 0,
        "gated_digest": hashlib.sha256(b"[]").hexdigest(),
        "gated_modules": [],
        "legacy_module_loaded": True,
        "proxy_module_loaded": False,
    }
    legacy = _isolated_surface_manifest("1")
    assert legacy == {
        "operation_count": 233,
        "legacy_count": 49,
        "deep_count": 237,
        "deep_digest": EXPECTED_LEGACY_DEEP_ROUTE_SHA256,
        "gated_count": 49,
        "gated_digest": EXPECTED_GATED_OPERATION_SHA256,
        "gated_modules": ["alicebot_api.routers.legacy_gated"],
        "legacy_module_loaded": True,
        "proxy_module_loaded": False,
    }


def test_direct_unit_monkeypatches_follow_new_defining_module_exactly() -> None:
    total = 0
    for relative_path, expected in EXPECTED_DIRECT_UNIT_PATCHES.items():
        assert _setattr_attribute_counts(relative_path, "legacy_gated_router") == expected
        assert _setattr_attribute_counts(relative_path, "main_module") == {}
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "main_module" not in source
        total += sum(expected.values())
    assert total == 223


def test_integration_settings_patches_are_exact_per_test_not_only_in_aggregate() -> None:
    test_count = 0
    patch_count = 0
    unpatched_tests = []
    for relative_path, expected_count in EXPECTED_INTEGRATION_PATCH_COUNTS.items():
        tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
        observed = 0
        for function in functions:
            main_count = _settings_patch_count(function, "main_module")
            router_count = _settings_patch_count(function, "legacy_gated_router")
            assert (main_count, router_count) in {(0, 0), (1, 1)}, (
                relative_path,
                function.name,
                main_count,
                router_count,
            )
            if (main_count, router_count) == (0, 0):
                unpatched_tests.append((relative_path, function.name))
            observed += router_count
        assert observed == expected_count
        test_count += len(functions)
        patch_count += observed
    assert (test_count, patch_count) == (87, 86)
    assert unpatched_tests == [
        (
            "tests/integration/test_execution_budgets_api.py",
            "test_execution_budget_active_scope_uniqueness_is_enforced_in_database",
        )
    ]

    artifact_tree = ast.parse((REPO_ROOT / "tests/integration/test_task_artifacts_api.py").read_text(encoding="utf-8"))
    artifact_tests = [
        node for node in artifact_tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]
    assert len(artifact_tests) == 15
    for function in artifact_tests:
        assert (
            _settings_patch_count(function, "main_module"),
            _settings_patch_count(function, "legacy_gated_router"),
            _settings_patch_count(function, "memories_legacy_router"),
        ) == (1, 1, 1), function.name
