from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import Response

from alicebot_api.public_errors import public_exception_response


ROOT = Path(__file__).resolve().parents[2]
MAIN_PATH = ROOT / "apps/api/src/alicebot_api/main.py"
ROUTERS_PATH = ROOT / "apps/api/src/alicebot_api/routers"
PUBLIC_EXCEPTION_RESPONSE_CALL_MANIFEST = {
    "apps/api/src/alicebot_api/main.py": 4,
    "apps/api/src/alicebot_api/routers/__init__.py": 0,
    "apps/api/src/alicebot_api/routers/_api_shared.py": 0,
    "apps/api/src/alicebot_api/routers/_vnext_automation.py": 0,
    "apps/api/src/alicebot_api/routers/_vnext_embeddings.py": 0,
    "apps/api/src/alicebot_api/routers/_vnext_shared.py": 1,
    "apps/api/src/alicebot_api/routers/continuity.py": 57,
    "apps/api/src/alicebot_api/routers/legacy_gated.py": 76,
    "apps/api/src/alicebot_api/routers/memories_legacy.py": 52,
    "apps/api/src/alicebot_api/routers/providers.py": 59,
    "apps/api/src/alicebot_api/routers/vnext_memories.py": 25,
    "apps/api/src/alicebot_api/routers/vnext_projects.py": 10,
    "apps/api/src/alicebot_api/routers/vnext_retrieval.py": 2,
    "apps/api/src/alicebot_api/routers/vnext_review.py": 11,
    "apps/api/src/alicebot_api/routers/workspaces.py": 4,
}


def _payload(response: object) -> dict[str, object]:
    body = getattr(response, "body")
    assert isinstance(body, bytes)
    payload = json.loads(body)
    assert isinstance(payload, dict)
    return payload


def _request(path: str, *, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers or [],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        }
    )


def _assert_private_error(
    response: object,
    caplog: object,
    *,
    status_code: int,
    code: str,
    message: str,
    sentinel: str,
) -> None:
    assert getattr(response, "status_code") == status_code
    payload = _payload(response)
    assert payload == {"detail": {"code": code, "message": message}}
    assert sentinel not in json.dumps(payload)
    assert sentinel in caplog.text  # type: ignore[attr-defined]


def test_public_exception_response_hides_sentinel_and_logs_it(caplog: object) -> None:
    sentinel = "PUBLIC-ERROR-PRIVATE-SENTINEL-9f44"

    with caplog.at_level(logging.ERROR, logger="alicebot_api.public_errors"):  # type: ignore[attr-defined]
        response = public_exception_response(ValueError(sentinel), status_code=400)

    assert response.status_code == 400
    payload = _payload(response)
    assert payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}
    assert sentinel not in json.dumps(payload)
    assert "ValueError" not in json.dumps(payload)
    assert sentinel in caplog.text  # type: ignore[attr-defined]
    assert "code=invalid_request status=400" in caplog.text  # type: ignore[attr-defined]


def test_public_exception_response_fails_closed_for_unregistered_status(caplog: object) -> None:
    sentinel = "PUBLIC-ERROR-UNKNOWN-STATUS-SENTINEL-a97c"

    with caplog.at_level(logging.ERROR, logger="alicebot_api.public_errors"):  # type: ignore[attr-defined]
        response = public_exception_response(RuntimeError(sentinel), status_code=418)

    assert response.status_code == 500
    assert _payload(response) == {"detail": {"code": "internal_error", "message": "An internal error occurred"}}
    assert sentinel in caplog.text  # type: ignore[attr-defined]


def _exception_text_response_violations(source: str) -> list[int]:
    tree = ast.parse(source)
    exception_names = {
        handler.name
        for handler in ast.walk(tree)
        if isinstance(handler, ast.ExceptHandler) and handler.name is not None
    }
    exception_names.update({"resolution_error", "execution_error", "lifecycle_error"})

    tainted_names = set(exception_names)
    changed = True
    while changed:
        changed = False
        for assignment in (
            node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
        ):
            value = assignment.value
            if value is None:
                continue
            if not any(
                isinstance(descendant, ast.Call)
                and isinstance(descendant.func, ast.Name)
                and descendant.func.id == "str"
                and descendant.args
                and isinstance(descendant.args[0], ast.Name)
                and descendant.args[0].id in tainted_names
                for descendant in ast.walk(value)
            ):
                continue
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in tainted_names:
                    tainted_names.add(target.id)
                    changed = True

    violations: list[int] = []
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not isinstance(call.func, ast.Name) or call.func.id not in {
            "JSONResponse",
            "_vnext_public_error_response",
        }:
            continue
        for descendant in ast.walk(call):
            if (
                isinstance(descendant, ast.Call)
                and isinstance(descendant.func, ast.Name)
                and descendant.func.id == "str"
                and descendant.args
                and isinstance(descendant.args[0], ast.Name)
                and descendant.args[0].id in tainted_names
            ):
                violations.append(call.lineno)
            if isinstance(descendant, ast.Name) and descendant.id in tainted_names:
                violations.append(call.lineno)
    return sorted(set(violations))


def test_http_modules_have_no_exception_text_to_public_response_conversion() -> None:
    """Fail on the old direct and delayed exception-to-detail response patterns."""

    source_paths = [MAIN_PATH, *sorted(ROUTERS_PATH.rglob("*.py"))]
    relative_paths = {str(path.relative_to(ROOT)) for path in source_paths}
    assert relative_paths == set(PUBLIC_EXCEPTION_RESPONSE_CALL_MANIFEST)

    sources = {str(path.relative_to(ROOT)): path.read_text() for path in source_paths}
    violations = {
        relative_path: _exception_text_response_violations(source)
        for relative_path, source in sources.items()
        if _exception_text_response_violations(source)
    }

    assert violations == {}
    call_counts = {
        relative_path: sum(
            1
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "public_exception_response"
        )
        for relative_path, source in sources.items()
    }
    assert call_counts == PUBLIC_EXCEPTION_RESPONSE_CALL_MANIFEST
    assert sum(call_counts.values()) == 301

    source = "\n".join(sources.values())
    assert 'content={"detail": f"thread {thread_id} was not found"}' not in source
    assert 'content={"detail": f"provider {provider_id} was not found"}' not in source
    assert 'content={"detail": f"provider {body.provider_id} was not found"}' not in source
    assert 'content={"detail": "local workspace is not bootstrapped;' not in source
    assert "error_detail = str(exc)" not in source
    assert "ModelInvocationError(str(exc))" not in source
    assert 'error_code="conflict"' in source
    assert '"code": result.error_code' in source
    assert '"message": result.detail' in source


def test_exception_text_hygiene_detects_delayed_response_conversion() -> None:
    source = """
try:
    raise ValueError("private")
except ValueError as exc:
    message = str(exc)
    response = JSONResponse(status_code=400, content={"detail": message})
"""

    assert _exception_text_response_violations(source) == [6]


def test_openapi_error_schema_requires_stable_code_and_message() -> None:
    from alicebot_api.main import app

    app.openapi_schema = None
    schema = app.openapi()
    error_detail = schema["components"]["schemas"]["APIErrorDetail"]
    error_response = schema["components"]["schemas"]["APIErrorResponse"]

    assert error_detail == {
        "title": "Stable API error detail",
        "type": "object",
        "required": ["code", "message"],
        "properties": {
            "code": {"type": "string", "minLength": 1},
            "message": {"type": "string", "minLength": 1},
        },
        "additionalProperties": True,
    }
    assert error_response["properties"]["detail"]["oneOf"] == [
        {"type": "string"},
        {"$ref": "#/components/schemas/APIErrorDetail"},
        {"type": "array", "items": {}},
    ]


def test_default_vnext_handler_keeps_exception_sentinel_private(monkeypatch: object, caplog: object) -> None:
    from alicebot_api.routers import vnext_memories as vnext_memories_router
    from alicebot_api.vnext_agent_control import AgentIdentityValidationError

    sentinel = "VNEXT-PRIVATE-SENTINEL-e1cb"

    def fail_identity(_request: object) -> object:
        raise AgentIdentityValidationError(sentinel)

    monkeypatch.setattr(vnext_memories_router, "_vnext_agent_identity", fail_identity)  # type: ignore[attr-defined]
    with caplog.at_level(logging.ERROR, logger="alicebot_api.public_errors"):  # type: ignore[attr-defined]
        response = vnext_memories_router.create_vnext_source(
            vnext_memories_router.VNextSourceCaptureRequest(user_id=uuid4(), raw_text="Fact")
        )

    _assert_private_error(
        response,
        caplog,
        status_code=400,
        code="invalid_request",
        message="The request is invalid",
        sentinel=sentinel,
    )


def test_provider_handler_keeps_exception_sentinel_private(monkeypatch: object, caplog: object) -> None:
    from alicebot_api.routers import providers as providers_router

    sentinel = "PROVIDER-PRIVATE-SENTINEL-58a7"

    def fail_auth(_settings: object, _request: object) -> object:
        raise ValueError(sentinel)

    monkeypatch.setattr(providers_router, "_resolve_authenticated_v1_user_id", fail_auth)  # type: ignore[attr-defined]
    with caplog.at_level(logging.ERROR, logger="alicebot_api.public_errors"):  # type: ignore[attr-defined]
        response = providers_router.get_v1_provider(uuid4(), _request("/v1/providers/example"))

    _assert_private_error(
        response,
        caplog,
        status_code=400,
        code="invalid_request",
        message="The request is invalid",
        sentinel=sentinel,
    )


def test_runtime_handler_keeps_exception_sentinel_private(monkeypatch: object, caplog: object) -> None:
    from alicebot_api.routers import providers as providers_router

    sentinel = "RUNTIME-PRIVATE-SENTINEL-3d91"

    def fail_auth(_settings: object, _request: object) -> object:
        raise ValueError(sentinel)

    monkeypatch.setattr(providers_router, "_resolve_authenticated_v1_user_id", fail_auth)  # type: ignore[attr-defined]
    request = _request(
        "/v1/runtime/invoke",
        headers=[(b"idempotency-key", b"http-hygiene-test")],
    )
    body = providers_router.RuntimeInvokeRequest(
        provider_id=uuid4(),
        thread_id=uuid4(),
        message="Hello",
    )
    with caplog.at_level(logging.ERROR, logger="alicebot_api.public_errors"):  # type: ignore[attr-defined]
        response = providers_router.invoke_v1_runtime(request, body)

    _assert_private_error(
        response,
        caplog,
        status_code=400,
        code="invalid_request",
        message="The request is invalid",
        sentinel=sentinel,
    )


def test_runtime_provider_value_error_keeps_exception_sentinel_private(caplog: object) -> None:
    from alicebot_api.routers import providers as providers_router

    sentinel = "RUNTIME-PROVIDER-PRIVATE-SENTINEL-1c72"

    class RejectingAdapter:
        def invoke(self, **_kwargs: object) -> object:
            raise ValueError(sentinel)

    with caplog.at_level(logging.ERROR, logger="alicebot_api.main"):  # type: ignore[attr-defined]
        outcome = providers_router._attempt_runtime_provider_model(
            adapter=RejectingAdapter(),  # type: ignore[arg-type]
            runtime_provider=object(),  # type: ignore[arg-type]
            settings=object(),  # type: ignore[arg-type]
            model_request=object(),  # type: ignore[arg-type]
        )

    assert outcome.response is None
    assert outcome.error is not None
    assert outcome.error_detail == "An upstream service failed"
    assert str(outcome.error) == "An upstream service failed"
    assert sentinel not in outcome.error_detail
    assert sentinel not in str(outcome.error)
    assert sentinel in caplog.text  # type: ignore[attr-defined]
    assert "code=upstream_failure status=502" in caplog.text  # type: ignore[attr-defined]


def test_runtime_model_invocation_error_keeps_exception_sentinel_private(caplog: object) -> None:
    from alicebot_api.routers import providers as providers_router
    from alicebot_api.response_generation import ModelInvocationError

    sentinel = "RUNTIME-MODEL-PRIVATE-SENTINEL-845d"

    class RejectingAdapter:
        def invoke(self, **_kwargs: object) -> object:
            raise ModelInvocationError(sentinel)

    with caplog.at_level(logging.ERROR, logger="alicebot_api.main"):  # type: ignore[attr-defined]
        outcome = providers_router._attempt_runtime_provider_model(
            adapter=RejectingAdapter(),  # type: ignore[arg-type]
            runtime_provider=object(),  # type: ignore[arg-type]
            settings=object(),  # type: ignore[arg-type]
            model_request=object(),  # type: ignore[arg-type]
        )

    assert outcome.response is None
    assert outcome.error is not None
    assert outcome.error_detail == "An upstream service failed"
    assert str(outcome.error) == "An upstream service failed"
    assert sentinel not in outcome.error_detail
    assert sentinel not in str(outcome.error)
    assert sentinel in caplog.text  # type: ignore[attr-defined]
    assert "code=upstream_failure status=502" in caplog.text  # type: ignore[attr-defined]


def test_auth_middleware_keeps_exception_sentinel_private(monkeypatch: object, caplog: object) -> None:
    from alicebot_api import main as main_module

    sentinel = "AUTH-MIDDLEWARE-PRIVATE-SENTINEL-b6f2"

    def fail_auth(_settings: object, _request: object) -> object:
        raise ValueError(sentinel)

    async def unexpected_call_next(_request: Request) -> Response:
        raise AssertionError("call_next must not run after an authentication failure")

    monkeypatch.setattr(main_module, "_resolve_authenticated_user_id", fail_auth)  # type: ignore[attr-defined]
    with caplog.at_level(logging.ERROR, logger="alicebot_api.public_errors"):  # type: ignore[attr-defined]
        response = asyncio.run(
            main_module.enforce_authenticated_user_identity(
                _request("/v0/vnext/workspace"),
                unexpected_call_next,
            )
        )

    _assert_private_error(
        response,
        caplog,
        status_code=401,
        code="authentication_failed",
        message="Authentication failed",
        sentinel=sentinel,
    )


def test_flag_on_legacy_handler_keeps_exception_sentinel_private_in_isolated_process() -> None:
    sentinel = "FLAG-ON-LEGACY-PRIVATE-SENTINEL-c210"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "apps/api/src")
    env["ALICE_LEGACY_SURFACES"] = "1"
    script = f"""
from contextlib import contextmanager
import json
from uuid import uuid4
from alicebot_api import main
from alicebot_api.config import Settings
from alicebot_api.routers import legacy_gated
from alicebot_api.tools import ToolNotFoundError

@contextmanager
def connection(*_args, **_kwargs):
    yield object()

def fail(*_args, **_kwargs):
    raise ToolNotFoundError({sentinel!r})

assert main.LEGACY_SURFACES_ENABLED is True
assert '/v0/tools' in main.app.openapi()['paths']
legacy_gated.get_settings = lambda: Settings(database_url='postgresql://app')
legacy_gated.user_connection = connection
legacy_gated.get_tool_record = fail
response = legacy_gated.get_tool(uuid4(), uuid4())
print(json.dumps({{'status_code': response.status_code, 'body': json.loads(response.body)}}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "status_code": 404,
        "body": {"detail": {"code": "not_found", "message": "The requested resource was not found"}},
    }
    assert sentinel not in completed.stdout
    assert sentinel in completed.stderr
