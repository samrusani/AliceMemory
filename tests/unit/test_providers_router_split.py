from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import alicebot_api.main as main_module
from alicebot_api.routers import _api_shared
from alicebot_api.routers import _vnext_shared
from alicebot_api.routers import providers as providers_router
from alicebot_api.routers import workspaces as workspaces_router


REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PATH = REPO_ROOT / "apps/api/src/alicebot_api/main.py"
ROUTER_PATH = REPO_ROOT / "apps/api/src/alicebot_api/routers/providers.py"
PROVIDER_RUNTIME_PATH = REPO_ROOT / "apps/api/src/alicebot_api/provider_runtime.py"

ROUTE_NAMES = tuple(
    """
    register_v1_provider register_v1_ollama_provider register_v1_llamacpp_provider
    register_v1_vllm_provider register_v1_azure_provider list_v1_providers
    get_v1_provider update_v1_provider test_v1_provider invoke_v1_runtime
    """.split()
)
SUPPORT_NAMES = tuple(
    """
    provider_adapter_registry _object_dict _serialize_model_provider
    _serialize_provider_capability _runtime_provider_config_or_none
    _normalize_provider_path _provider_config_fingerprint
    _fallback_provider_capability_snapshot _ProviderDiscoveryOutcome
    _discover_provider_capability _persist_discovered_provider_capability
    _RuntimeProviderInvocationOutcome _attempt_runtime_provider_model
    _record_runtime_provider_invocation ProviderConfigurationChangedError
    _StagedProviderSecret _stage_provider_secret
    _retire_provider_secret_if_unreferenced _discard_staged_provider_secret
    _resolve_owned_provider_workspace _assert_provider_write_context
    _create_workspace_provider_durable _register_workspace_provider
    _normalize_azure_api_version _register_workspace_azure_provider
    _create_workspace_azure_provider_durable _update_workspace_provider
    _update_workspace_provider_durable _seed_workspace_provider_configs
    redact_url_credentials _response_job_headers _response_job_public_status
    _terminal_response_job_replay _response_job_replay_or_in_progress
    RegisterProviderRequest RegisterOllamaProviderRequest
    RegisterLlamaCppProviderRequest RegisterVllmProviderRequest
    RegisterAzureProviderRequest TestProviderRequest UpdateProviderRequest
    RuntimeInvokeRequest _require_local_provider_workspace
    """.split()
)
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
    _vnext_protected_http_auth
    build_healthcheck_payload _request_client_is_loopback
    _keyless_request_is_off_loopback
    _authentication_failed_response _is_v1_path _v1_request_payload
    _v1_request_claims_other_user _resolve_v1_http_auth
    enforce_v1_agent_authentication
    _append_vary_header
    _cors_origin_allowed _resolve_cors_allow_origin_value _apply_cors_headers
    _apply_security_headers apply_http_security_posture
    enforce_authenticated_user_identity healthcheck _apply_legacy_surface_mount_policy
    """.split()
)

EXPECTED_ROUTE_AST_SHA256 = "9e5d6c2c79cc1391688b74bb5138ccaa881033546e7b0cfd34ad92e8d98ba614"
EXPECTED_SUPPORT_AST_SHA256 = "bb694bc545e514bb81e2aa568eba1cb72ba813373015d979bac452d23d4dbd74"
EXPECTED_CARRIER_NAMES_SHA256 = "2c109fc234a05dd8f44e4c34bee49e797fbb5e49e92413391541a7e504da328b"
# Re-pinned 2026-09-26. _rewrite_user_id_json_body writes the rewritten JSON
# into request._body before call_next. A per-definition AST diff against
# origin/main showed only that definition changed.
EXPECTED_CARRIER_AST_SHA256 = "a7bef8b4e136e0ffc5dd4bbe2ede6b8d5496a28849d5536098101f03b7adfd10"
EXPECTED_ROUTE_NAME_MANIFEST_SHA256 = "1a438538e16120361f92d30375cc94679d598fe4b78ba5a58a7d8a4dda6af83c"
EXPECTED_OPERATION_MANIFEST_SHA256 = "8b79ceaf996b8c51b5bb2f3f38a8c19a4e33796955d8b8f7a66e7ac01ea1732d"
EXPECTED_IMPORT_MANIFEST_SHA256 = "17484ccdd460e42e2ad5c82a8ca867664694feaf871118c410a134996a532358"

EXPECTED_ROUTE_MANIFEST = [
    ("POST", "/v1/providers", "register_v1_provider"),
    ("POST", "/v1/providers/ollama/register", "register_v1_ollama_provider"),
    ("POST", "/v1/providers/llamacpp/register", "register_v1_llamacpp_provider"),
    ("POST", "/v1/providers/vllm/register", "register_v1_vllm_provider"),
    ("POST", "/v1/providers/azure/register", "register_v1_azure_provider"),
    ("GET", "/v1/providers", "list_v1_providers"),
    ("GET", "/v1/providers/{provider_id}", "get_v1_provider"),
    ("PATCH", "/v1/providers/{provider_id}", "update_v1_provider"),
    ("POST", "/v1/providers/test", "test_v1_provider"),
    ("POST", "/v1/runtime/invoke", "invoke_v1_runtime"),
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


def _top_level_definitions(tree: ast.Module) -> dict[str, ast.AST]:
    return {name: node for node in tree.body for name in _bound_names(node)}


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


def test_moved_handlers_and_supports_are_exact_mechanical_copies() -> None:
    router_tree = ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))
    definitions = _top_level_definitions(router_tree)

    assert len(ROUTE_NAMES) == 10
    assert len(SUPPORT_NAMES) == 43
    assert set(ROUTE_NAMES) <= definitions.keys()
    assert set(SUPPORT_NAMES) <= definitions.keys()
    assert set(definitions) == set(ROUTE_NAMES) | set(SUPPORT_NAMES) | {"router"}
    assert _ast_digest(definitions, ROUTE_NAMES, strip_decorators=True) == EXPECTED_ROUTE_AST_SHA256
    assert _ast_digest(definitions, SUPPORT_NAMES) == EXPECTED_SUPPORT_AST_SHA256

    observed: list[tuple[str, str, str]] = []
    for node in router_tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in ROUTE_NAMES:
            continue
        assert len(node.decorator_list) == 1
        decorator = node.decorator_list[0]
        assert isinstance(decorator, ast.Call)
        assert isinstance(decorator.func, ast.Attribute)
        assert isinstance(decorator.func.value, ast.Name)
        assert decorator.func.value.id == "router"
        assert len(decorator.args) == 1
        assert decorator.keywords == []
        assert isinstance(decorator.args[0], ast.Constant)
        observed.append((decorator.func.attr.upper(), decorator.args[0].value, node.name))

    assert observed == EXPECTED_ROUTE_MANIFEST
    digest = hashlib.sha256(json.dumps(observed, separators=(",", ":")).encode()).hexdigest()
    assert digest == EXPECTED_ROUTE_NAME_MANIFEST_SHA256


def test_main_carrier_is_unchanged_and_owns_no_moved_definition() -> None:
    main_tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    definitions = _top_level_definitions(main_tree)

    assert not ((set(ROUTE_NAMES) | set(SUPPORT_NAMES)) & definitions.keys())
    assert set(definitions) == set(CARRIER_NAMES)
    assert hashlib.sha256("\n".join(CARRIER_NAMES).encode()).hexdigest() == EXPECTED_CARRIER_NAMES_SHA256
    assert _ast_digest(definitions, CARRIER_NAMES) == EXPECTED_CARRIER_AST_SHA256

    provider_alias_imports = [
        node
        for node in main_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "alicebot_api.routers.providers"
    ]
    assert len(provider_alias_imports) == 1
    provider_alias_import = provider_alias_imports[0]
    assert [alias.name for alias in provider_alias_import.names] == ["redact_url_credentials"]
    provider_module_imports = [
        node
        for node in main_tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "alicebot_api.routers"
        and any(alias.name == "providers" for alias in node.names)
    ]
    assert len(provider_module_imports) == 1
    provider_module_import = provider_module_imports[0]
    workspace_module_imports = [
        node
        for node in main_tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "alicebot_api.routers"
        and any(alias.name == "workspaces" for alias in node.names)
    ]
    assert len(workspace_module_imports) == 1
    workspace_module_import = workspace_module_imports[0]
    all_provider_imports = [
        node
        for node in main_tree.body
        if isinstance(node, ast.ImportFrom)
        and (
            node.module == "alicebot_api.routers.providers"
            or (node.module == "alicebot_api.routers" and any(alias.name == "providers" for alias in node.names))
        )
    ]
    assert all_provider_imports == [provider_module_import, provider_alias_import]
    assert (
        definitions["app"].end_lineno
        < provider_module_import.lineno
        < provider_alias_import.lineno
        < workspace_module_import.lineno
        < definitions["HealthStatus"].lineno
    )
    assert _import_bindings(main_tree) & (set(ROUTE_NAMES) | set(SUPPORT_NAMES)) == {"redact_url_credentials"}

    api_shared_import = next(
        node
        for node in main_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "alicebot_api.routers._api_shared"
    )
    compatibility_aliases = {
        alias.name: alias.asname
        for alias in api_shared_import.names
        if alias.name in {"LOGGER", "_json_object", "_json_value"}
    }
    assert compatibility_aliases == {
        "LOGGER": "LOGGER",
        "_json_object": "_json_object",
        "_json_value": "_json_value",
    }


def test_provider_import_direction_pruning_and_runtime_identities_are_exact() -> None:
    main_tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    router_tree = ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))

    import_blob = json.dumps(_import_manifest(router_tree), separators=(",", ":")).encode()
    assert len(_import_manifest(router_tree)) == 88
    assert hashlib.sha256(import_blob).hexdigest() == EXPECTED_IMPORT_MANIFEST_SHA256

    for node in router_tree.body:
        if isinstance(node, ast.Import):
            assert all(not alias.name.startswith("alicebot_api.main") for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not (node.level == 0 and module.startswith("alicebot_api.main"))
            assert not (
                node.level == 0 and module == "alicebot_api" and any(alias.name == "main" for alias in node.names)
            )

    shared_imports = _import_bindings(main_tree) & _import_bindings(router_tree)
    assert shared_imports == {
        "Any",
        "JSONResponse",
        "LOGGER",
        "Literal",
        "Request",
        "Settings",
        "UUID",
        "_json_object",
        "_resolve_authenticated_v1_user_id",
        "get_settings",
        "public_exception_response",
        "user_connection",
    }
    main_load_counts: dict[str, int] = {}
    for node in ast.walk(main_tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            main_load_counts[node.id] = main_load_counts.get(node.id, 0) + 1
    # /v1 agent-key authentication resolves the bound user in main, so
    # _resolve_authenticated_v1_user_id is no longer a re-export-only binding.
    assert {name for name in shared_imports if main_load_counts.get(name, 0) == 0} == {
        "LOGGER",
        "_json_object",
    }

    assert providers_router.LOGGER is _api_shared.LOGGER
    assert providers_router._json_object is _api_shared._json_object
    assert providers_router._resolve_authenticated_v1_user_id is _api_shared._resolve_authenticated_v1_user_id
    assert providers_router.BaseModel is _vnext_shared.BaseModel
    assert main_module.LOGGER is _api_shared.LOGGER
    assert main_module._json_object is _api_shared._json_object
    assert providers_router.LOGGER.name == "alicebot_api.main"
    assert workspaces_router._seed_workspace_provider_configs is providers_router._seed_workspace_provider_configs
    assert workspaces_router._discover_provider_capability is providers_router._discover_provider_capability
    assert (
        workspaces_router._persist_discovered_provider_capability
        is providers_router._persist_discovered_provider_capability
    )
    assert workspaces_router._resolve_authenticated_v1_user_id is _api_shared._resolve_authenticated_v1_user_id
    assert main_module._resolve_authenticated_v1_user_id is _api_shared._resolve_authenticated_v1_user_id
    assert main_module.redact_url_credentials is providers_router.redact_url_credentials


def test_split_receipts_detect_import_origin_decorator_and_definition_mutations() -> None:
    router_tree = ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))
    mutated_import_tree = deepcopy(router_tree)
    settings_import = next(
        node
        for node in mutated_import_tree.body
        if isinstance(node, ast.ImportFrom) and any(alias.name == "Settings" for alias in node.names)
    )
    settings_import.module = "alicebot_api.mutated_config"
    mutated_import_blob = json.dumps(
        _import_manifest(mutated_import_tree),
        separators=(",", ":"),
    ).encode()
    assert hashlib.sha256(mutated_import_blob).hexdigest() != EXPECTED_IMPORT_MANIFEST_SHA256

    mutated_route_tree = deepcopy(router_tree)
    mutated_route = _top_level_definitions(mutated_route_tree)[ROUTE_NAMES[0]]
    assert isinstance(mutated_route, ast.FunctionDef)
    route_decorator = mutated_route.decorator_list[0]
    assert isinstance(route_decorator, ast.Call)
    route_decorator.keywords.append(ast.keyword(arg="status_code", value=ast.Constant(value=201)))
    assert route_decorator.keywords != []

    mutated_main_tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    mutated_main_tree.body.append(
        ast.FunctionDef(
            name="unexpected_carrier_definition",
            args=ast.arguments(
                posonlyargs=[],
                args=[],
                kwonlyargs=[],
                kw_defaults=[],
                defaults=[],
            ),
            body=[ast.Pass()],
            decorator_list=[],
        )
    )
    assert set(_top_level_definitions(mutated_main_tree)) != set(CARRIER_NAMES)


def test_provider_registry_is_singleton_initialized_with_no_new_import_time_io() -> None:
    main_tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    router_tree = ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))

    top_level_calls = [
        _dotted_name(node.value.func)
        for node in router_tree.body
        if isinstance(node, (ast.Assign, ast.Expr)) and isinstance(node.value, ast.Call)
    ]
    assert top_level_calls == ["APIRouter", "make_provider_adapter_registry"]
    assert (
        sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "make_provider_adapter_registry"
            for node in ast.walk(router_tree)
        )
        == 1
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "make_provider_adapter_registry"
        for node in ast.walk(main_tree)
    )
    assert providers_router.provider_adapter_registry.keys() == [
        "azure",
        "llamacpp",
        "ollama",
        "openai_compatible",
        "vllm",
    ]

    runtime_tree = ast.parse(PROVIDER_RUNTIME_PATH.read_text(encoding="utf-8"))
    factory = _top_level_definitions(runtime_tree)["make_provider_adapter_registry"]
    assert isinstance(factory, ast.FunctionDef)
    registration_order = [
        call.args[0].func.id
        for call in (node for node in ast.walk(factory) if isinstance(node, ast.Call))
        if isinstance(call.func, ast.Attribute)
        and call.func.attr == "register"
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Call)
        and isinstance(call.args[0].func, ast.Name)
    ]
    assert registration_order == [
        "OpenAICompatibleAdapter",
        "AzureAdapter",
        "OllamaAdapter",
        "LlamaCppAdapter",
        "VllmAdapter",
    ]

    forbidden_import_time_calls = {
        "get_settings",
        "read_provider_api_key",
        "write_provider_api_key",
        "delete_provider_api_key",
        "resolve_provider_api_key",
        "connect",
        "user_connection",
        "validate_provider_base_url",
        "invoke",
        "discover",
    }
    for node in router_tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for descendant in ast.walk(node):
            if not isinstance(descendant, ast.Call):
                continue
            call_name = _dotted_name(descendant.func)
            assert call_name is not None
            assert call_name.split(".")[-1] not in forbidden_import_time_calls


def test_provider_routes_preserve_mount_order_origins_and_operation_ids() -> None:
    schema = main_module.app.openapi()
    observed: list[tuple[str, str, str]] = []
    for route in providers_router.router.routes:
        assert route.endpoint.__module__ == "alicebot_api.routers.providers"
        for method in sorted(route.methods or set()):
            operation = schema["paths"][route.path][method.lower()]
            observed.append((method, route.path, operation["operationId"]))
            assert operation["operationId"] == route.unique_id

    assert len(observed) == len(set(observed)) == 10
    digest = hashlib.sha256(json.dumps(observed, separators=(",", ":")).encode()).hexdigest()
    assert digest == EXPECTED_OPERATION_MANIFEST_SHA256

    effective_routes = []
    for route in main_module.app.router.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        effective_routes.extend(effective_route_contexts() if callable(effective_route_contexts) else (route,))
    effective_pairs = [
        (method, str(getattr(route, "path", "")))
        for route in effective_routes
        for method in sorted(getattr(route, "methods", None) or set())
    ]
    expected_slice = [
        ("POST", "/v1/workspaces/bootstrap"),
        ("GET", "/v1/workspaces/bootstrap/status"),
        *[(method, path) for method, path, _name in EXPECTED_ROUTE_MANIFEST],
    ]
    start = effective_pairs.index(expected_slice[0])
    assert effective_pairs[start : start + len(expected_slice)] == expected_slice

    main_source = MAIN_PATH.read_text(encoding="utf-8")
    anchors = [
        "app.include_router(workspaces.bootstrap_router)",
        "app.include_router(providers.router)",
        "def _apply_legacy_surface_mount_policy",
    ]
    positions = [main_source.index(anchor) for anchor in anchors]
    assert positions == sorted(positions)
    assert all(f'@app.{method.lower()}("{path}")' not in main_source for method, path, _name in EXPECTED_ROUTE_MANIFEST)


def test_provider_test_patches_follow_moved_definition_ownership() -> None:
    assert _setattr_attribute_counts("tests/unit/test_main.py", "main_module") == {
        "_BROWSER_CLIP_SIMPLE_BODY_MAX_BYTES": 3,
        "_resolve_vnext_http_auth": 1,
        "get_settings": 5,
        "ping_database": 2,
    }
    assert _setattr_attribute_counts("tests/unit/test_main.py", "providers_router") == {
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
    }
    assert _setattr_attribute_counts("tests/unit/test_main.py", "providers_router.psycopg") == {
        "connect": 2,
    }
    assert _setattr_attribute_counts(
        "tests/unit/test_main.py",
        "memories_legacy_router.ContinuityStore",
    ) == {"get_thread": 3}
    assert _setattr_attribute_counts("tests/unit/test_public_errors.py", "providers_router") == {
        "_resolve_authenticated_v1_user_id": 2,
    }
    assert _setattr_attribute_counts("tests/unit/test_public_errors.py", "main_module") == {
        "_resolve_authenticated_user_id": 1,
    }

    integration_path = "tests/integration/test_provider_runtime_api.py"
    assert _setattr_attribute_counts(integration_path, "main_module") == {
        "get_settings": 4,
    }
    assert _setattr_attribute_counts(integration_path, "providers_router") == {
        "get_settings": 4,
    }
    assert _setattr_attribute_counts(integration_path, "workspaces_router") == {
        "get_settings": 4,
    }
    integration_tree = ast.parse((REPO_ROOT / integration_path).read_text(encoding="utf-8"))
    integration_functions = {node.name: node for node in integration_tree.body if isinstance(node, ast.FunctionDef)}

    def direct_settings_patches(function: ast.FunctionDef, target: str) -> int:
        return sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == target
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "get_settings"
            for node in ast.walk(function)
        )

    configure_function = integration_functions["_configure_settings"]
    assert direct_settings_patches(configure_function, "main_module") == 1
    assert direct_settings_patches(configure_function, "providers_router") == 1
    assert direct_settings_patches(configure_function, "workspaces_router") == 1

    tests = [
        node for node in integration_tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]
    configure_calls = sum(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_configure_settings"
        for function in tests
        for node in ast.walk(function)
    )
    assert len(tests) == 22
    assert configure_calls == 19

    direct_config_tests = {
        "test_workspace_bootstrap_config_seed_and_provider_update_refresh_capabilities",
        "test_workspace_bootstrap_config_invokes_openai_compatible_without_auth",
        "test_workspace_bootstrap_config_seeds_vllm_provider",
    }
    observed_shapes: dict[str, tuple[int, int, int, int]] = {}
    for function in tests:
        helper_calls = sum(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_configure_settings"
            for node in ast.walk(function)
        )
        observed_shapes[function.name] = (
            helper_calls,
            direct_settings_patches(function, "main_module"),
            direct_settings_patches(function, "providers_router"),
            direct_settings_patches(function, "workspaces_router"),
        )
    assert {name for name, shape in observed_shapes.items() if shape == (0, 1, 1, 1)} == direct_config_tests
    assert all(
        shape == ((0, 1, 1, 1) if name in direct_config_tests else (1, 0, 0, 0))
        for name, shape in observed_shapes.items()
    )


def test_public_error_and_coverage_controls_include_provider_router_once() -> None:
    paths = [MAIN_PATH, *sorted((ROUTER_PATH.parent).rglob("*.py"))]
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
    assert call_counts["apps/api/src/alicebot_api/routers/providers.py"] == 59
    assert call_counts["apps/api/src/alicebot_api/routers/workspaces.py"] == 4
    assert sum(call_counts.values()) == 301

    provider_path = "apps/api/src/alicebot_api/routers/providers.py"
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
        assert all(line.count(f"--path {provider_path}") == 1 for line in coverage_lines)
