from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.vnext_agent_keys import resolve_protected_agent_identity


_AUTHENTICATION_FAILED = {
    "detail": {
        "code": "authentication_failed",
        "message": "Authentication failed",
    }
}

# Every locally-authorized route must have a deliberate target/scope source.
# The central middleware inventory alone cannot catch accidentally moving a
# new endpoint into the local-policy set without deciding where its policy
# inputs come from.
_LOCAL_POLICY_ENFORCEMENT_FAMILIES = {
    "request_scope": {
        ("POST", "/v0/vnext/sources"),
        ("POST", "/v0/vnext/agents/ingest-output"),
        ("POST", "/v0/vnext/context-packs"),
        ("POST", "/v0/vnext/memory-proposals"),
        ("POST", "/v0/vnext/memories/commit"),
        ("POST", "/v0/vnext/artifacts/generate/daily-brief"),
        ("POST", "/v0/vnext/artifacts/generate/weekly-synthesis"),
        ("POST", "/v0/vnext/artifacts/generate/connections"),
        ("POST", "/v0/vnext/artifacts/generate/contradictions"),
        ("POST", "/v0/vnext/queue/tasks"),
        ("POST", "/v0/vnext/projects/update-candidates"),
        ("POST", "/v0/vnext/open-loops"),
    },
    "persisted_memory_scope": {
        ("POST", "/v0/vnext/memories/{memory_id}/review"),
        ("POST", "/v0/vnext/memories/confirm"),
        ("POST", "/v0/vnext/memories/undo"),
        ("POST", "/v0/vnext/memories/correct"),
        ("POST", "/v0/vnext/memories/forget"),
        ("POST", "/v0/vnext/memories/expire"),
        ("POST", "/v0/vnext/memories/unexpire"),
        ("POST", "/v0/vnext/memories/accept-consolidation"),
        ("POST", "/v0/vnext/memories/redact"),
    },
    "source_read_fence": {
        ("GET", "/v0/vnext/sources/{source_id}"),
    },
    "persisted_artifact_scope": {
        ("POST", "/v0/vnext/artifacts/{artifact_id}/insight-feedback"),
        ("GET", "/v0/vnext/artifacts/{artifact_id}"),
        ("GET", "/v0/vnext/traces/artifacts/{artifact_id}"),
        ("POST", "/v0/vnext/artifacts/{artifact_id}/export"),
        ("POST", "/v0/vnext/artifacts/{artifact_id}/review"),
        ("POST", "/v0/vnext/artifacts/{artifact_id}/quality-ratings"),
    },
    "persisted_open_loop_scope": {
        ("POST", "/v0/vnext/open-loops/{loop_id}/review"),
    },
    "scheduler_or_global_control": {
        ("PATCH", "/v0/vnext/scheduler/workflows/{workflow_type}"),
        ("POST", "/v0/vnext/scheduler/workflows/{workflow_type}/run-now"),
        ("POST", "/v0/vnext/scheduler/run-due"),
        ("POST", "/v0/vnext/scheduler/pause"),
        ("POST", "/v0/vnext/scheduler/resume"),
    },
}


class _ActiveKeyStore:
    """Small authentication store with one active key and no valid candidates."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def count_active_agent_api_keys(self) -> int:
        return 1

    def get_agent_api_key_by_hash(self, _key_hash: str) -> None:
        return None

    def touch_agent_api_key(self, *, key_id: str) -> dict[str, object]:
        raise AssertionError(f"unexpected key touch: {key_id}")

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event


def _registered_vnext_routes() -> set[tuple[str, str]]:
    routes: list[object] = []
    for route in main_module.app.router.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        routes.extend(effective_route_contexts() if callable(effective_route_contexts) else (route,))
    return {
        (method, str(getattr(route, "path")))
        for route in routes
        if str(getattr(route, "path", "")).startswith("/v0/vnext")
        for method in (getattr(route, "methods", None) or set())
        if method != "OPTIONS"
    }


def _materialize_path(path_template: str) -> str:
    named_values = {
        "connector_name": "telegram",
        "workflow_type": "daily_brief",
    }

    def replacement(match: re.Match[str]) -> str:
        return named_values.get(match.group(1), str(uuid4()))

    return re.sub(r"\{([^{}]+)\}", replacement, path_template)


def _invoke_vnext_request(
    method: str,
    path: str,
    *,
    user_id: UUID,
    authorization: str | None = None,
) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, object]] = []
    payload = {} if method in {"GET", "HEAD"} else {"user_id": str(user_id)}
    body = b"" if not payload else json.dumps(payload).encode("utf-8")
    query = {"user_id": str(user_id)} if method in {"GET", "HEAD"} else {}
    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    headers: list[tuple[bytes, bytes]] = [(b"content-type", b"application/json")]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("utf-8")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": urlencode(query).encode("ascii"),
        "headers": headers,
        # Loopback, so the unconditional keyless off-loopback gate cannot answer
        # first. This sweep is about the agent-key requirement, and a synthetic
        # non-loopback host made it pass whether or not that requirement held.
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    anyio.run(main_module.app, scope, receive, send)

    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return int(start["status"]), json.loads(response_body)


@pytest.fixture
def active_key_auth_resolver(monkeypatch: pytest.MonkeyPatch) -> _ActiveKeyStore:
    store = _ActiveKeyStore()

    def resolve_auth(
        *,
        settings: object,
        user_id: UUID,
        raw_key: str | None,
        payload: dict[str, object],
        method: str,
        route_path: str,
    ) -> tuple[object, object]:
        del settings
        identity = resolve_protected_agent_identity(
            store,
            user_id=user_id,
            raw_key=raw_key,
            payload=payload,
        )
        return identity, main_module._vnext_central_route_policy(
            identity=identity,
            method=method,
            route_path=route_path,
        )

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(app_env="test", database_url="postgresql://stage-a-unused"),
    )
    monkeypatch.setattr(main_module, "_resolve_vnext_http_auth", resolve_auth)
    return store


def test_every_registered_vnext_route_rejects_keyless_requests_after_key_provisioning(
    active_key_auth_resolver: _ActiveKeyStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    del active_key_auth_resolver
    caplog.set_level(logging.CRITICAL)
    registered = _registered_vnext_routes()
    classified = main_module._VNEXT_ROUTE_LOCAL_POLICY | main_module._VNEXT_CENTRAL_OPERATOR_ROUTES

    assert registered == classified
    assert not (main_module._VNEXT_ROUTE_LOCAL_POLICY & main_module._VNEXT_CENTRAL_OPERATOR_ROUTES)

    user_id = uuid4()
    for method, path_template in sorted(registered):
        status, payload = _invoke_vnext_request(
            method,
            _materialize_path(path_template),
            user_id=user_id,
        )
        assert (status, payload) == (401, _AUTHENTICATION_FAILED), (method, path_template)


def test_every_local_policy_route_has_one_declared_scope_enforcement_family() -> None:
    family_routes = [
        route
        for routes in _LOCAL_POLICY_ENFORCEMENT_FAMILIES.values()
        for route in routes
    ]

    assert len(family_routes) == len(set(family_routes))
    assert set(family_routes) == set(main_module._VNEXT_ROUTE_LOCAL_POLICY)


def test_invalid_agent_key_is_absent_from_public_error_logs_and_audit_events(
    active_key_auth_resolver: _ActiveKeyStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_key = f"alice_sk_STAGE_A_AGENT_SECRET_{uuid4().hex}"

    with caplog.at_level(logging.ERROR, logger="alicebot_api.public_errors"):
        status, payload = _invoke_vnext_request(
            "GET",
            "/v0/vnext/projects",
            user_id=uuid4(),
            authorization=f"Bearer {raw_key}",
        )

    assert (status, payload) == (401, _AUTHENTICATION_FAILED)
    assert raw_key not in json.dumps(payload, sort_keys=True)
    assert raw_key not in caplog.text
    assert raw_key not in json.dumps(active_key_auth_resolver.events, sort_keys=True)
