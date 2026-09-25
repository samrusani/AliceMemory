from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import alicebot_api.main as main_module
from alicebot_api.routers import _api_shared
from alicebot_api.routers import providers as providers_router
from alicebot_api.routers import workspaces as workspaces_router


REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PATH = REPO_ROOT / "apps/api/src/alicebot_api/main.py"
ROUTER_PATH = REPO_ROOT / "apps/api/src/alicebot_api/routers/workspaces.py"

ROUTE_NAMES = (
    "get_vnext_workspace",
    "bootstrap_v1_workspace",
    "get_v1_workspace_bootstrap_status",
)
SUPPORT_NAMES = ("_vnext_status_counts", "_vnext_workspace_payload")
PARTITION_ROUTE_NAMES = {
    "core_router": ("get_vnext_workspace",),
    "bootstrap_router": (
        "bootstrap_v1_workspace",
        "get_v1_workspace_bootstrap_status",
    ),
}
CARRIER_NAMES = tuple(
    """
    _openapi_tag_for_path _OPENAPI_EXACT_RESPONSE_CONTRACTS
    _OPENAPI_CREATED_ONLY_OPERATIONS _OPENAPI_CONDITIONAL_SUCCESS_OPERATIONS
    LEGACY_HTTP_OPERATION_KEYS LEGACY_SURFACES_ENABLED
    _openapi_live_operation_keys AliceFastAPI app _alice_request_validation_error
    HealthStatus ServiceStatus
    DatabaseServicePayload RedisServicePayload ObjectStorageServicePayload
    HealthServicesPayload HealthcheckPayload _rewrite_user_id_query_param
    _rewrite_user_id_json_body _VNEXT_ROUTE_LOCAL_POLICY
    _VNEXT_CENTRAL_OPERATOR_ROUTES _BROWSER_CLIP_SIMPLE_CAPTURE_PATH
    _BROWSER_CLIP_SIMPLE_BODY_MAX_BYTES _prepare_browser_clip_simple_request
    _matched_vnext_route_path
    _vnext_central_route_policy _resolve_vnext_http_auth
    _vnext_protected_http_auth build_healthcheck_payload
    _request_client_is_loopback
    _keyless_request_is_off_loopback
    _authentication_failed_response _is_v1_path _v1_request_payload
    _v1_request_claims_other_user _resolve_v1_http_auth
    enforce_v1_agent_authentication
    _append_vary_header _cors_origin_allowed
    _resolve_cors_allow_origin_value _apply_cors_headers _apply_security_headers
    apply_http_security_posture enforce_authenticated_user_identity healthcheck
    _apply_legacy_surface_mount_policy
    """.split()
)
MAIN_PRUNED_BINDINGS = {
    "VNextConnectorService",
    "VNextDoctorService",
    "VNextDogfoodingService",
    "VNextMemoryCommitService",
    "VNextProjectService",
    "VNextProjectValidationError",
    "VNextSchedulerService",
    "_discover_provider_capability",
    "_persist_discovered_provider_capability",
    "_seed_workspace_provider_configs",
    "_vnext_int",
    "_vnext_source_trace",
    "daemon_status",
    "dict_row",
    "ensure_local_workspace",
    "get_local_workspace",
    "jsonable_encoder",
    "psycopg",
    "serialize_local_workspace",
    "summarize_agent_policy_telemetry",
}

EXPECTED_ROUTE_AST_SHA256 = "fb8925ebcda058598b0c6d5e7eca83abe18606a0126bdb6cde0fd1c22444a795"
EXPECTED_SUPPORT_AST_SHA256 = "878128b51d8e8fd091f189f4595ab7774c736667f68e251399e476f9086089df"
EXPECTED_ROUTE_NAME_MANIFEST_SHA256 = "225c57c08bd8314156c56352dd1c53ffed3f556ce285c666dd6fca125115d0b4"
EXPECTED_OPERATION_MANIFEST_SHA256 = "c320979b62d7ee8de244fe38bde5bf3761a4f9d76f76bf3cd8576c30fce9857e"
EXPECTED_IMPORT_MANIFEST_SHA256 = "8d9669a4024ea5258cd50f92ac290c2a040ff224dd0a67b5c60faed5ae722517"
EXPECTED_CARRIER_NAMES_SHA256 = "2c109fc234a05dd8f44e4c34bee49e797fbb5e49e92413391541a7e504da328b"
# Re-pinned 2026-09-26. _rewrite_user_id_json_body writes the rewritten JSON
# into request._body before call_next. A per-definition AST diff against
# origin/main showed only that definition changed.
EXPECTED_CARRIER_AST_SHA256 = "a7bef8b4e136e0ffc5dd4bbe2ede6b8d5496a28849d5536098101f03b7adfd10"
EXPECTED_ROUTE_NODE_SHA256 = {
    "get_vnext_workspace": "6c2151bf38b1b1311f016c00d14394afc7077a6ea219f7ce3dcfd9b701474ae7",
    "bootstrap_v1_workspace": "07b1fe2a4cd03a5ba69abe76e258a457e85e92b0bfba592520ee02d01d759c4b",
    "get_v1_workspace_bootstrap_status": "2849d7126ee37b6e3ffd9ebe84b2a8e719eb0f811da750a29f7e0a0798305faa",
}
EXPECTED_SUPPORT_NODE_SHA256 = {
    "_vnext_status_counts": "0bf0ed228a14bd648a9d18fcd5f99ebf8c585bd29f4b5e81e1df17fe0201fd15",
    "_vnext_workspace_payload": "166fc46cc669ff465eb7b1fb3b49be7e1b9d0aeba40872abcb3d958906914e90",
}
EXPECTED_ROUTE_MANIFEST = [
    ("GET", "/v0/vnext/workspace", "get_vnext_workspace"),
    ("POST", "/v1/workspaces/bootstrap", "bootstrap_v1_workspace"),
    ("GET", "/v1/workspaces/bootstrap/status", "get_v1_workspace_bootstrap_status"),
]


def _bound_names(node: ast.AST) -> set[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return {
            descendant.id for target in targets for descendant in ast.walk(target) if isinstance(descendant, ast.Name)
        }
    return set()


def _top_level_definition_occurrences(tree: ast.Module) -> dict[str, list[ast.AST]]:
    occurrences: dict[str, list[ast.AST]] = {}
    for node in tree.body:
        for name in _bound_names(node):
            occurrences.setdefault(name, []).append(node)
    return occurrences


def _top_level_definitions(tree: ast.Module) -> dict[str, ast.AST]:
    return {name: nodes[-1] for name, nodes in _top_level_definition_occurrences(tree).items()}


def _ast_digest(
    definitions: dict[str, ast.AST],
    names: tuple[str, ...],
    *,
    strip_decorators: bool = False,
) -> str:
    payload: list[str] = []
    for name in names:
        node = deepcopy(definitions[name])
        if strip_decorators:
            assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            node.decorator_list = []
        payload.append(f"{name}:{ast.dump(node, include_attributes=False)}")
    return hashlib.sha256("\n".join(payload).encode()).hexdigest()


def _node_digest(node: ast.AST, *, name: str, strip_decorators: bool = False) -> str:
    canonical = deepcopy(node)
    if strip_decorators:
        assert isinstance(canonical, (ast.FunctionDef, ast.AsyncFunctionDef))
        canonical.decorator_list = []
    payload = f"{name}:{ast.dump(canonical, include_attributes=False)}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _import_bindings(tree: ast.Module) -> set[str]:
    bindings: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        bindings.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    return bindings


def _import_manifest(tree: ast.Module) -> list[tuple[str, str, int, str | None, str | None, str | None]]:
    rows: list[tuple[str, str, int, str | None, str | None, str | None]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                binding = alias.asname or alias.name.split(".")[0]
                rows.append((binding, "import", 0, alias.name, None, alias.asname))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                binding = alias.asname or alias.name
                rows.append((binding, "from", node.level, node.module, alias.name, alias.asname))
    return sorted(rows)


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _dotted_name(node.value)
        return None if owner is None else f"{owner}.{node.attr}"
    return None


def _setattr_attribute_counts(path: str, target: str) -> dict[str, int]:
    tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setattr"
            and len(node.args) >= 2
            and _dotted_name(node.args[0]) == target
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            continue
        attribute = node.args[1].value
        counts[attribute] = counts.get(attribute, 0) + 1
    return counts


def _direct_settings_patch_count(function: ast.FunctionDef, target: str) -> int:
    return sum(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setattr"
        and len(node.args) >= 2
        and _dotted_name(node.args[0]) == target
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "get_settings"
        for node in ast.walk(function)
    )


def _router_contract_violations(tree: ast.Module) -> list[str]:
    violations: list[str] = []
    expected_definitions = set(ROUTE_NAMES) | set(SUPPORT_NAMES) | set(PARTITION_ROUTE_NAMES)
    occurrences = _top_level_definition_occurrences(tree)
    if set(occurrences) != expected_definitions:
        violations.append("definition_set")
    if any(len(nodes) != 1 for nodes in occurrences.values()):
        violations.append("definition_occurrences")

    for router_name in PARTITION_ROUTE_NAMES:
        nodes = occurrences.get(router_name, [])
        if len(nodes) != 1:
            continue
        assignment = nodes[0]
        if not (
            isinstance(assignment, ast.Assign)
            and len(assignment.targets) == 1
            and isinstance(assignment.targets[0], ast.Name)
            and assignment.targets[0].id == router_name
            and isinstance(assignment.value, ast.Call)
            and isinstance(assignment.value.func, ast.Name)
            and assignment.value.func.id == "APIRouter"
            and assignment.value.args == []
            and assignment.value.keywords == []
        ):
            violations.append(f"router_constructor:{router_name}")

    expected_routes = {
        name: (method, path, router_name)
        for (method, path, name), router_name in zip(
            EXPECTED_ROUTE_MANIFEST,
            ("core_router", "bootstrap_router", "bootstrap_router"),
            strict=True,
        )
    }
    observed_by_router: dict[str, list[str]] = {name: [] for name in PARTITION_ROUTE_NAMES}
    for name, (expected_method, expected_path, expected_router) in expected_routes.items():
        nodes = occurrences.get(name, [])
        if len(nodes) != 1 or not isinstance(nodes[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
            violations.append(f"route_definition:{name}")
            continue
        decorators = nodes[0].decorator_list
        if len(decorators) != 1 or not isinstance(decorators[0], ast.Call):
            violations.append(f"route_decorator:{name}")
            continue
        decorator = decorators[0]
        if not (
            isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == expected_router
            and decorator.func.attr.upper() == expected_method
            and len(decorator.args) == 1
            and isinstance(decorator.args[0], ast.Constant)
            and decorator.args[0].value == expected_path
            and decorator.keywords == []
        ):
            violations.append(f"route_contract:{name}")
            continue
        observed_by_router[expected_router].append(name)
    if observed_by_router != {name: list(names) for name, names in PARTITION_ROUTE_NAMES.items()}:
        violations.append("router_partitions")
    return violations


def _include_router_target(statement: ast.stmt) -> str | None:
    if not (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Attribute)
        and isinstance(statement.value.func.value, ast.Name)
        and statement.value.func.value.id == "app"
        and statement.value.func.attr == "include_router"
        and len(statement.value.args) == 1
        and statement.value.keywords == []
    ):
        return None
    return _dotted_name(statement.value.args[0])


def _mount_manifest(tree: ast.Module) -> list[str]:
    manifest: list[str] = []

    def visit(statements: list[ast.stmt]) -> None:
        for statement in statements:
            target = _include_router_target(statement)
            if target is not None:
                manifest.append(target)
            if isinstance(statement, ast.If):
                visit(statement.body)
                visit(statement.orelse)

    visit(tree.body)
    return manifest


def _mount_contract_violations(tree: ast.Module) -> list[str]:
    manifest = _mount_manifest(tree)
    violations: list[str] = []
    for target in ("workspaces.core_router", "workspaces.bootstrap_router"):
        if manifest.count(target) != 1:
            violations.append(f"mount_count:{target}")
    for expected_slice in (
        ["continuity.capture_router", "workspaces.core_router", "vnext_memories.source_create_router"],
        ["memories_legacy.memory_router", "workspaces.bootstrap_router", "providers.router"],
    ):
        if not all(target in manifest for target in expected_slice):
            violations.append(f"mount_missing:{expected_slice[1]}")
            continue
        start = manifest.index(expected_slice[0])
        if manifest[start : start + len(expected_slice)] != expected_slice:
            violations.append(f"mount_order:{expected_slice[1]}")
    return violations


def _late_import_contract_violations(tree: ast.Module) -> list[str]:
    violations: list[str] = []
    definitions = _top_level_definitions(tree)
    expected_imports = (
        ("alicebot_api.routers", ("providers",)),
        ("alicebot_api.routers.providers", ("redact_url_credentials",)),
        ("alicebot_api.routers", ("workspaces",)),
    )
    nodes: list[ast.ImportFrom] = []
    for module, names in expected_imports:
        matches = [
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and node.module == module
            and any(alias.name in names for alias in node.names)
        ]
        if len(matches) != 1:
            violations.append(f"late_import_count:{module}:{names}")
            continue
        match = matches[0]
        if tuple(alias.name for alias in match.names) != names or any(
            alias.asname is not None for alias in match.names
        ):
            violations.append(f"late_import_shape:{module}:{names}")
            continue
        nodes.append(match)
    if len(nodes) == len(expected_imports):
        if not (
            definitions["app"].end_lineno
            < nodes[0].lineno
            < nodes[1].lineno
            < nodes[2].lineno
            < definitions["HealthStatus"].lineno
        ):
            violations.append("late_import_order")
    return violations


def _carrier_contract_violations(tree: ast.Module) -> list[str]:
    occurrences = _top_level_definition_occurrences(tree)
    violations: list[str] = []
    if set(occurrences) != set(CARRIER_NAMES):
        violations.append("carrier_definition_set")
    if any(len(nodes) != 1 for nodes in occurrences.values()):
        violations.append("carrier_definition_occurrences")
    return violations


def _top_level_runtime_calls(tree: ast.Module) -> list[str | None]:
    calls: list[str | None] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        calls.extend(_dotted_name(descendant.func) for descendant in ast.walk(node) if isinstance(descendant, ast.Call))
    return calls


def test_workspace_handlers_supports_and_partitions_are_exact_mechanical_copies() -> None:
    tree = ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))
    definitions = _top_level_definitions(tree)
    occurrences = _top_level_definition_occurrences(tree)

    assert set(definitions) == set(ROUTE_NAMES) | set(SUPPORT_NAMES) | set(PARTITION_ROUTE_NAMES)
    assert {name: len(nodes) for name, nodes in occurrences.items()} == {name: 1 for name in definitions}
    assert _router_contract_violations(tree) == []
    assert _ast_digest(definitions, ROUTE_NAMES, strip_decorators=True) == EXPECTED_ROUTE_AST_SHA256
    assert _ast_digest(definitions, SUPPORT_NAMES) == EXPECTED_SUPPORT_AST_SHA256
    assert {
        name: _node_digest(definitions[name], name=name, strip_decorators=True) for name in ROUTE_NAMES
    } == EXPECTED_ROUTE_NODE_SHA256
    assert {name: _node_digest(definitions[name], name=name) for name in SUPPORT_NAMES} == (
        EXPECTED_SUPPORT_NODE_SHA256
    )

    observed: list[tuple[str, str, str]] = []
    observed_by_router: dict[str, list[str]] = {name: [] for name in PARTITION_ROUTE_NAMES}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in ROUTE_NAMES:
            continue
        assert len(node.decorator_list) == 1
        decorator = node.decorator_list[0]
        assert isinstance(decorator, ast.Call)
        assert isinstance(decorator.func, ast.Attribute)
        assert isinstance(decorator.func.value, ast.Name)
        assert decorator.func.value.id in PARTITION_ROUTE_NAMES
        assert len(decorator.args) == 1
        assert decorator.keywords == []
        assert isinstance(decorator.args[0], ast.Constant)
        observed.append((decorator.func.attr.upper(), decorator.args[0].value, node.name))
        observed_by_router[decorator.func.value.id].append(node.name)

    assert observed == EXPECTED_ROUTE_MANIFEST
    assert observed_by_router == {name: list(names) for name, names in PARTITION_ROUTE_NAMES.items()}
    assert hashlib.sha256(json.dumps(observed, separators=(",", ":")).encode()).hexdigest() == (
        EXPECTED_ROUTE_NAME_MANIFEST_SHA256
    )
    assert [route.endpoint.__name__ for route in workspaces_router.core_router.routes] == ["get_vnext_workspace"]
    assert [route.endpoint.__name__ for route in workspaces_router.bootstrap_router.routes] == [
        "bootstrap_v1_workspace",
        "get_v1_workspace_bootstrap_status",
    ]


def test_workspace_import_direction_pruning_timing_and_runtime_identities_are_exact() -> None:
    main_tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    router_tree = ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))
    main_definitions = _top_level_definitions(main_tree)
    router_imports = _import_manifest(router_tree)

    assert len(router_imports) == 30
    assert hashlib.sha256(json.dumps(router_imports, separators=(",", ":")).encode()).hexdigest() == (
        EXPECTED_IMPORT_MANIFEST_SHA256
    )
    assert not ((set(ROUTE_NAMES) | set(SUPPORT_NAMES)) & main_definitions.keys())
    assert set(main_definitions) == set(CARRIER_NAMES)
    assert _carrier_contract_violations(main_tree) == []
    assert {name: len(nodes) for name, nodes in _top_level_definition_occurrences(main_tree).items()} == {
        name: 1 for name in main_definitions
    }
    assert hashlib.sha256("\n".join(CARRIER_NAMES).encode()).hexdigest() == EXPECTED_CARRIER_NAMES_SHA256
    assert _ast_digest(main_definitions, CARRIER_NAMES) == EXPECTED_CARRIER_AST_SHA256
    assert _late_import_contract_violations(main_tree) == []

    for node in router_tree.body:
        if isinstance(node, ast.Import):
            assert all(not alias.name.startswith("alicebot_api.main") for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not (node.level == 0 and module.startswith("alicebot_api.main"))
            assert not (
                node.level == 0 and module == "alicebot_api" and any(alias.name == "main" for alias in node.names)
            )

    main_imports = _import_bindings(main_tree)
    router_import_bindings = _import_bindings(router_tree)
    assert MAIN_PRUNED_BINDINGS.isdisjoint(main_imports)
    assert MAIN_PRUNED_BINDINGS <= router_import_bindings
    assert main_imports & router_import_bindings == {
        "JSONResponse",
        "PostgresVNextStore",
        "Request",
        "UUID",
        "_resolve_authenticated_v1_user_id",
        "get_settings",
        "public_exception_response",
        "user_connection",
    }
    main_loads = {
        node.id for node in ast.walk(main_tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    # /v1 agent-key authentication resolves the bound user in main, so no
    # shared binding is a re-export-only import any more.
    assert (main_imports & router_import_bindings) - main_loads == set()

    provider_module_imports = [
        node
        for node in main_tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "alicebot_api.routers"
        and [alias.name for alias in node.names] == ["providers"]
    ]
    provider_alias_imports = [
        node
        for node in main_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "alicebot_api.routers.providers"
    ]
    workspace_module_imports = [
        node
        for node in main_tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "alicebot_api.routers"
        and [alias.name for alias in node.names] == ["workspaces"]
    ]
    assert len(provider_module_imports) == len(provider_alias_imports) == len(workspace_module_imports) == 1
    provider_module_import = provider_module_imports[0]
    provider_alias_import = provider_alias_imports[0]
    workspace_module_import = workspace_module_imports[0]
    assert [alias.name for alias in provider_alias_import.names] == ["redact_url_credentials"]
    assert (
        main_definitions["app"].end_lineno
        < provider_module_import.lineno
        < provider_alias_import.lineno
        < workspace_module_import.lineno
        < main_definitions["HealthStatus"].lineno
    )

    assert _top_level_runtime_calls(router_tree) == ["APIRouter", "APIRouter"]
    assert workspaces_router._resolve_authenticated_v1_user_id is _api_shared._resolve_authenticated_v1_user_id
    assert main_module._resolve_authenticated_v1_user_id is _api_shared._resolve_authenticated_v1_user_id
    assert workspaces_router._discover_provider_capability is providers_router._discover_provider_capability
    assert workspaces_router._persist_discovered_provider_capability is providers_router._persist_discovered_provider_capability
    assert workspaces_router._seed_workspace_provider_configs is providers_router._seed_workspace_provider_configs


def test_workspace_routes_preserve_mount_order_origins_and_operation_ids() -> None:
    main_tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    assert _mount_contract_violations(main_tree) == []
    schema = main_module.app.openapi()
    observed: list[tuple[str, str, str]] = []
    for route in (*workspaces_router.core_router.routes, *workspaces_router.bootstrap_router.routes):
        assert route.endpoint.__module__ == "alicebot_api.routers.workspaces"
        for method in sorted(route.methods or set()):
            operation_id = schema["paths"][route.path][method.lower()]["operationId"]
            assert operation_id == route.unique_id
            observed.append((method, route.path, operation_id))
    assert hashlib.sha256(json.dumps(observed, separators=(",", ":")).encode()).hexdigest() == (
        EXPECTED_OPERATION_MANIFEST_SHA256
    )

    effective_routes = []
    for route in main_module.app.router.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        effective_routes.extend(effective_route_contexts() if callable(effective_route_contexts) else (route,))
    effective_pairs = [
        (method, str(getattr(route, "path", "")))
        for route in effective_routes
        for method in sorted(getattr(route, "methods", None) or set())
        if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    ]
    expected_indices = (84, 224, 225) if main_module.LEGACY_SURFACES_ENABLED else (38, 175, 176)
    assert all(effective_pairs.count((method, path)) == 1 for method, path, _name in EXPECTED_ROUTE_MANIFEST)
    observed_indices = tuple(
        effective_pairs.index((method, path))
        for method, path, _name in EXPECTED_ROUTE_MANIFEST
    )
    assert observed_indices == expected_indices
    assert effective_pairs[observed_indices[0] - 1 : observed_indices[0] + 2] == [
        ("POST", "/v0/continuity/captures"),
        ("GET", "/v0/vnext/workspace"),
        ("POST", "/v0/vnext/sources"),
    ]
    assert effective_pairs[observed_indices[1] - 1 : observed_indices[2] + 2] == [
        ("GET", "/v0/entities/{entity_id}"),
        ("POST", "/v1/workspaces/bootstrap"),
        ("GET", "/v1/workspaces/bootstrap/status"),
        ("POST", "/v1/providers"),
    ]

    main_source = MAIN_PATH.read_text(encoding="utf-8")
    anchors = (
        "app.include_router(continuity.capture_router)",
        "app.include_router(workspaces.core_router)",
        "app.include_router(vnext_memories.source_create_router)",
        "app.include_router(memories_legacy.memory_router)",
        "app.include_router(workspaces.bootstrap_router)",
        "app.include_router(providers.router)",
    )
    positions = [main_source.index(anchor) for anchor in anchors]
    assert positions == sorted(positions)
    assert all(main_source.count(anchor) == 1 for anchor in anchors)
    assert '@app.get("/v0/vnext/workspace")' not in main_source
    assert '@app.post("/v1/workspaces/bootstrap")' not in main_source
    assert '@app.get("/v1/workspaces/bootstrap/status")' not in main_source


def test_workspace_test_patches_and_release_controls_follow_moved_ownership() -> None:
    expected_settings_patches = {
        "tests/integration/test_local_workspace_bootstrap_api.py": (2, 2),
        "tests/integration/test_default_surface_integration.py": (1, 1),
        "tests/integration/test_vnext_live_workspace_api.py": (3, 1),
    }
    for path, (main_count, workspace_count) in expected_settings_patches.items():
        assert _setattr_attribute_counts(path, "main_module").get("get_settings", 0) == main_count
        assert _setattr_attribute_counts(path, "workspaces_router") == {"get_settings": workspace_count}

    local_tree = ast.parse(
        (REPO_ROOT / "tests/integration/test_local_workspace_bootstrap_api.py").read_text(encoding="utf-8")
    )
    local_shapes = {
        function.name: (
            _direct_settings_patch_count(function, "main_module"),
            _direct_settings_patch_count(function, "workspaces_router"),
        )
        for function in local_tree.body
        if isinstance(function, ast.FunctionDef)
        and (
            _direct_settings_patch_count(function, "main_module")
            or _direct_settings_patch_count(function, "workspaces_router")
        )
    }
    assert local_shapes == {
        "configure_local_api": (1, 1),
        "test_local_workspace_bootstrap_requires_a_valid_identity_header": (1, 1),
    }

    default_tree = ast.parse(
        (REPO_ROOT / "tests/integration/test_default_surface_integration.py").read_text(encoding="utf-8")
    )
    default_shapes = {
        function.name: (
            _direct_settings_patch_count(function, "main_module"),
            _direct_settings_patch_count(function, "workspaces_router"),
        )
        for function in default_tree.body
        if isinstance(function, ast.FunctionDef)
        and (
            _direct_settings_patch_count(function, "main_module")
            or _direct_settings_patch_count(function, "workspaces_router")
        )
    }
    assert default_shapes == {"test_default_http_and_mcp_surfaces_complete_core_round_trip": (1, 1)}

    vnext_live_tree = ast.parse(
        (REPO_ROOT / "tests/integration/test_vnext_live_workspace_api.py").read_text(encoding="utf-8")
    )
    vnext_live_shapes = {
        function.name: (
            _direct_settings_patch_count(function, "main_module"),
            _direct_settings_patch_count(function, "workspaces_router"),
        )
        for function in vnext_live_tree.body
        if isinstance(function, ast.FunctionDef)
        and (
            _direct_settings_patch_count(function, "main_module")
            or _direct_settings_patch_count(function, "workspaces_router")
        )
    }
    assert vnext_live_shapes == {
        "test_vnext_live_workspace_happy_path_writes_reviewable_postgres_state": (1, 1),
        "test_assign_project_replaces_postgres_scope_for_memory_and_source_retrieval": (1, 0),
        "test_vnext_artifact_routes_enforce_persisted_scope_with_live_postgres": (1, 0),
    }

    provider_path = "tests/integration/test_provider_runtime_api.py"
    assert _setattr_attribute_counts(provider_path, "main_module") == {"get_settings": 4}
    assert _setattr_attribute_counts(provider_path, "providers_router") == {"get_settings": 4}
    assert _setattr_attribute_counts(provider_path, "workspaces_router") == {"get_settings": 4}

    vnext_tree = ast.parse((REPO_ROOT / "tests/unit/test_vnext_main.py").read_text(encoding="utf-8"))
    installer = _top_level_definitions(vnext_tree)["_install_fake_vnext_store"]
    assert isinstance(installer, ast.FunctionDef)
    module_loop = next(
        node
        for node in ast.walk(installer)
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == "module"
    )
    assert isinstance(module_loop.iter, ast.Tuple)
    assert [element.id for element in module_loop.iter.elts if isinstance(element, ast.Name)] == [
        "main_module",
        "vnext_memories_router",
        "vnext_projects_router",
        "vnext_retrieval_router",
        "vnext_review_router",
        "workspaces_router",
    ]
    setattr_attributes = {
        node.args[1].value
        for node in ast.walk(module_loop)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    }
    assert setattr_attributes == {"PostgresVNextStore", "get_settings", "user_connection"}
    vnext_source = (REPO_ROOT / "tests/unit/test_vnext_main.py").read_text(encoding="utf-8")
    assert "main_module.VNextProjectService" not in vnext_source
    assert "main_module.VNextMemoryCommitService" not in vnext_source

    workspace_path = "apps/api/src/alicebot_api/routers/workspaces.py"
    for relative_path in (
        "Makefile",
        ".github/workflows/tests.yml",
        ".github/workflows/publish-pypi.yml",
    ):
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        coverage_lines = [
            line for line in source.splitlines() if "scripts/check_python_coverage.py --coverage-json" in line
        ]
        assert coverage_lines
        assert all(line.count(f"--path {workspace_path}") == 1 for line in coverage_lines)

    paths = [MAIN_PATH, *sorted(ROUTER_PATH.parent.rglob("*.py"))]
    call_counts = {
        path.relative_to(REPO_ROOT).as_posix(): sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "public_exception_response"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
        for path in paths
    }
    assert call_counts["apps/api/src/alicebot_api/main.py"] == 4
    assert call_counts[workspace_path] == 4
    assert sum(call_counts.values()) == 300


def test_workspace_split_receipts_fail_on_old_or_mutated_carriers() -> None:
    router_tree = ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))
    main_tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    assert _router_contract_violations(router_tree) == []
    assert _mount_contract_violations(main_tree) == []
    assert _late_import_contract_violations(main_tree) == []
    assert _carrier_contract_violations(main_tree) == []

    mutated_route_tree = deepcopy(router_tree)
    mutated_route = _top_level_definitions(mutated_route_tree)[ROUTE_NAMES[0]]
    assert isinstance(mutated_route, ast.FunctionDef)
    decorator = mutated_route.decorator_list[0]
    assert isinstance(decorator, ast.Call)
    decorator.keywords.append(ast.keyword(arg="status_code", value=ast.Constant(value=201)))
    assert f"route_contract:{ROUTE_NAMES[0]}" in _router_contract_violations(mutated_route_tree)

    collapsed_router_tree = deepcopy(router_tree)
    collapsed_route = _top_level_definitions(collapsed_router_tree)["bootstrap_v1_workspace"]
    assert isinstance(collapsed_route, ast.FunctionDef)
    collapsed_decorator = collapsed_route.decorator_list[0]
    assert isinstance(collapsed_decorator, ast.Call)
    assert isinstance(collapsed_decorator.func, ast.Attribute)
    assert isinstance(collapsed_decorator.func.value, ast.Name)
    collapsed_decorator.func.value.id = "core_router"
    collapsed_violations = _router_contract_violations(collapsed_router_tree)
    assert "route_contract:bootstrap_v1_workspace" in collapsed_violations
    assert "router_partitions" in collapsed_violations

    configured_router_tree = deepcopy(router_tree)
    core_router_assignment = _top_level_definitions(configured_router_tree)["core_router"]
    assert isinstance(core_router_assignment, ast.Assign)
    assert isinstance(core_router_assignment.value, ast.Call)
    core_router_assignment.value.keywords.append(
        ast.keyword(arg="redirect_slashes", value=ast.Constant(value=False))
    )
    assert "router_constructor:core_router" in _router_contract_violations(configured_router_tree)

    duplicate_definition_tree = deepcopy(router_tree)
    duplicate_definition_tree.body.append(
        deepcopy(_top_level_definitions(duplicate_definition_tree)["_vnext_status_counts"])
    )
    assert "definition_occurrences" in _router_contract_violations(duplicate_definition_tree)

    mutated_support_tree = deepcopy(router_tree)
    mutated_support = _top_level_definitions(mutated_support_tree)[SUPPORT_NAMES[0]]
    assert isinstance(mutated_support, ast.FunctionDef)
    mutated_support.body.append(ast.Pass())
    assert _ast_digest(_top_level_definitions(mutated_support_tree), SUPPORT_NAMES) != EXPECTED_SUPPORT_AST_SHA256

    mutated_import_tree = deepcopy(router_tree)
    settings_import = next(
        node
        for node in mutated_import_tree.body
        if isinstance(node, ast.ImportFrom) and any(alias.name == "get_settings" for alias in node.names)
    )
    settings_import.module = "alicebot_api.mutated_config"
    assert hashlib.sha256(
        json.dumps(_import_manifest(mutated_import_tree), separators=(",", ":")).encode()
    ).hexdigest() != EXPECTED_IMPORT_MANIFEST_SHA256

    nested_io_tree = deepcopy(router_tree)
    nested_io_tree.body.append(
        ast.Assign(
            targets=[ast.Name(id="nested_import_time_call", ctx=ast.Store())],
            value=ast.Tuple(
                elts=[ast.Call(func=ast.Name(id="get_settings", ctx=ast.Load()), args=[], keywords=[])],
                ctx=ast.Load(),
            ),
        )
    )
    assert _top_level_runtime_calls(nested_io_tree) == ["APIRouter", "APIRouter", "get_settings"]

    displaced_mount_tree = deepcopy(main_tree)
    core_index = next(
        index
        for index, statement in enumerate(displaced_mount_tree.body)
        if _include_router_target(statement) == "workspaces.core_router"
    )
    core_mount = displaced_mount_tree.body.pop(core_index)
    source_index = next(
        index
        for index, statement in enumerate(displaced_mount_tree.body)
        if _include_router_target(statement) == "vnext_memories.source_create_router"
    )
    displaced_mount_tree.body.insert(source_index + 1, core_mount)
    assert "mount_order:workspaces.core_router" in _mount_contract_violations(displaced_mount_tree)

    duplicate_mount_tree = deepcopy(main_tree)
    core_mount = next(
        statement
        for statement in duplicate_mount_tree.body
        if _include_router_target(statement) == "workspaces.core_router"
    )
    duplicate_mount_tree.body.append(deepcopy(core_mount))
    assert "mount_count:workspaces.core_router" in _mount_contract_violations(duplicate_mount_tree)

    configured_mount_tree = deepcopy(main_tree)
    configured_core_mount = next(
        statement
        for statement in configured_mount_tree.body
        if _include_router_target(statement) == "workspaces.core_router"
    )
    assert isinstance(configured_core_mount, ast.Expr)
    assert isinstance(configured_core_mount.value, ast.Call)
    configured_core_mount.value.keywords.append(ast.keyword(arg="prefix", value=ast.Constant(value="/changed")))
    assert "mount_count:workspaces.core_router" in _mount_contract_violations(configured_mount_tree)

    duplicate_import_tree = deepcopy(main_tree)
    workspace_import = next(
        node
        for node in duplicate_import_tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "alicebot_api.routers"
        and [alias.name for alias in node.names] == ["workspaces"]
    )
    duplicate_import_tree.body.append(deepcopy(workspace_import))
    assert any(
        violation.startswith("late_import_count:alicebot_api.routers:('workspaces',)")
        for violation in _late_import_contract_violations(duplicate_import_tree)
    )

    grouped_early_import_tree = deepcopy(main_tree)
    app_index = next(
        index
        for index, statement in enumerate(grouped_early_import_tree.body)
        if "app" in _bound_names(statement)
    )
    grouped_early_import_tree.body.insert(
        app_index,
        ast.ImportFrom(
            module="alicebot_api.routers",
            names=[ast.alias(name="providers"), ast.alias(name="workspaces")],
            level=0,
        ),
    )
    grouped_import_violations = _late_import_contract_violations(grouped_early_import_tree)
    assert "late_import_count:alicebot_api.routers:('providers',)" in grouped_import_violations
    assert "late_import_count:alicebot_api.routers:('workspaces',)" in grouped_import_violations

    duplicate_carrier_tree = deepcopy(main_tree)
    duplicate_carrier_tree.body.append(
        deepcopy(_top_level_definitions(duplicate_carrier_tree)["build_healthcheck_payload"])
    )
    assert "carrier_definition_occurrences" in _carrier_contract_violations(duplicate_carrier_tree)
