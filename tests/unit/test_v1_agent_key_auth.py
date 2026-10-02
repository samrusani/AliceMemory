from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import logging
from typing import Any, Iterator
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import Request, Response

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.routers import continuity as continuity_router
from alicebot_api.vnext_agent_keys import hash_agent_key


_AUTHENTICATION_FAILED = {
    "detail": {
        "code": "authentication_failed",
        "message": "Authentication failed",
    }
}

# A credential-shaped literal is the point of these tests: the fixture proves
# that presenting a key changes the outcome.
_FIXTURE_AGENT_KEY = "alice_sk_v1_auth_fixture_key_000000000000"  # gitleaks:allow

_LOOPBACK_CLIENT = "127.0.0.1"
_REMOTE_CLIENT = "203.0.113.10"

# One representative operation per /v1 router family named in the hotfix.
_V1_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("GET", "/v1/providers"),
    ("POST", "/v1/providers"),
    ("POST", "/v1/runtime/invoke"),
    ("GET", "/v1/memory/operations"),
    ("POST", "/v1/memory/operations/commit"),
    ("POST", "/v1/workspaces/bootstrap"),
    ("GET", "/v1/workspaces/bootstrap/status"),
)


class _AgentKeyStore:
    """Minimal agent-key store: one active key, nothing else resolvable."""

    def __init__(self, *, user_id: UUID, active_key_count: int = 1) -> None:
        self.user_id = user_id
        self.active_key_count = active_key_count
        self.events: list[dict[str, object]] = []
        self.touched: list[str] = []
        self.record: dict[str, object] = {
            "id": str(uuid4()),
            "user_id": str(user_id),
            "agent_id": "v1-fixture-agent",
            "permission_profile": "read_only_agent",
            "project_scope": None,
            "key_hash": hash_agent_key(_FIXTURE_AGENT_KEY),
            "key_prefix": _FIXTURE_AGENT_KEY[:12],
            "revoked_at": None,
        }

    def count_active_agent_api_keys(self) -> int:
        return self.active_key_count

    def get_agent_api_key_by_hash(self, key_hash: str) -> dict[str, object] | None:
        if self.active_key_count > 0 and key_hash == self.record["key_hash"]:
            return dict(self.record)
        return None

    def touch_agent_api_key(self, *, key_id: str) -> dict[str, object]:
        self.touched.append(key_id)
        return dict(self.record)

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event


def _install_agent_key_store(
    monkeypatch: pytest.MonkeyPatch,
    store: _AgentKeyStore,
    *,
    settings: Settings,
) -> None:
    @contextmanager
    def fake_user_connection(database_url: str, current_user_id: object) -> Iterator[object]:
        assert database_url == settings.database_url
        assert current_user_id is not None
        yield object()

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "user_connection", fake_user_connection)
    monkeypatch.setattr(main_module, "PostgresVNextStore", lambda _conn: store)


def _settings(
    user_id: UUID,
    *,
    app_env: str = "test",
    app_host: str = "127.0.0.1",
    trust_proxy_headers: bool = False,
    trusted_proxy_ips: tuple[str, ...] = (),
) -> Settings:
    return Settings(
        app_env=app_env,
        app_host=app_host,
        database_url="postgresql://alice-v1-auth-unreachable",
        auth_user_id=str(user_id),
        trust_proxy_headers=trust_proxy_headers,
        trusted_proxy_ips=trusted_proxy_ips,
    )


def _scope(
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    authorization: str | None = None,
    client_host: str = _LOOPBACK_CLIENT,
    forwarded_for: str | None = None,
    content_type: str = "application/json",
) -> dict[str, object]:
    # A keyless request must name this machine in Host (DB-005), as every real client does.
    headers: list[tuple[bytes, bytes]] = [
        (b"host", b"127.0.0.1:8000"),
        (b"content-type", content_type.encode("utf-8")),
    ]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("utf-8")))
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("utf-8")))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": urlencode(query or {}).encode("ascii"),
        "headers": headers,
        "client": (client_host, 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }


def _invoke_app(
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    body: dict[str, object] | None = None,
    authorization: str | None = None,
    client_host: str = _LOOPBACK_CLIENT,
    content_type: str = "application/json",
) -> tuple[int, Any]:
    """Drive the assembled ASGI app so every middleware runs in order."""

    messages: list[dict[str, object]] = []
    encoded_body = b"" if body is None else json.dumps(body).encode("utf-8")
    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": encoded_body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    anyio.run(
        main_module.app,
        _scope(
            method,
            path,
            query=query,
            authorization=authorization,
            client_host=client_host,
            content_type=content_type,
        ),
        receive,
        send,
    )

    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return int(start["status"]), json.loads(response_body)


def _build_request(
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    body: dict[str, object] | None = None,
    authorization: str | None = None,
    client_host: str = _LOOPBACK_CLIENT,
    forwarded_for: str | None = None,
) -> Request:
    encoded_body = b"" if body is None else json.dumps(body).encode("utf-8")

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": encoded_body, "more_body": False}

    return Request(
        _scope(
            method,
            path,
            query=query,
            authorization=authorization,
            client_host=client_host,
            forwarded_for=forwarded_for,
        ),
        receive,
    )


def _run_v1_middleware(request: Request) -> tuple[Response, list[Request]]:
    reached: list[Request] = []

    async def call_next(inner_request: Request) -> Response:
        reached.append(inner_request)
        return Response(status_code=204)

    response = asyncio.run(main_module.enforce_v1_agent_authentication(request, call_next))
    return response, reached


@pytest.mark.parametrize(("method", "path"), _V1_OPERATIONS)
def test_v1_routes_reject_keyless_requests_once_an_agent_key_exists(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    method: str,
    path: str,
) -> None:
    caplog.set_level(logging.CRITICAL)
    user_id = uuid4()
    settings = _settings(user_id)
    _install_agent_key_store(monkeypatch, _AgentKeyStore(user_id=user_id), settings=settings)

    # The routers keep their own database handle, so any handler that ran would
    # fail on the unreachable URL instead of returning a clean 401.
    status, payload = _invoke_app(method, path, body=None if method == "GET" else {})

    assert (status, payload) == (401, _AUTHENTICATION_FAILED)


@pytest.mark.parametrize(("method", "path"), _V1_OPERATIONS)
def test_v1_routes_reject_an_unknown_bearer_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    method: str,
    path: str,
) -> None:
    caplog.set_level(logging.CRITICAL)
    user_id = uuid4()
    settings = _settings(user_id)
    _install_agent_key_store(monkeypatch, _AgentKeyStore(user_id=user_id), settings=settings)

    status, payload = _invoke_app(
        method,
        path,
        body=None if method == "GET" else {},
        authorization="Bearer alice_sk_not_the_provisioned_key_0000",  # gitleaks:allow
    )

    assert (status, payload) == (401, _AUTHENTICATION_FAILED)


def test_v1_request_with_a_valid_key_resolves_that_keys_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid4()
    settings = _settings(user_id)
    store = _AgentKeyStore(user_id=user_id)
    _install_agent_key_store(monkeypatch, store, settings=settings)

    response, reached = _run_v1_middleware(
        _build_request(
            "GET",
            "/v1/providers",
            authorization=f"Bearer {_FIXTURE_AGENT_KEY}",
        )
    )

    assert response.status_code == 204
    assert len(reached) == 1
    identity = reached[0].state.v1_agent_identity
    assert identity is not None
    assert identity.agent_id == "v1-fixture-agent"
    assert identity.permission_profile == "read_only_agent"
    assert identity.auth == "agent_api_key"
    assert store.touched == [str(store.record["id"])]


def test_v1_payload_user_id_cannot_widen_privilege(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.CRITICAL)
    user_id = uuid4()
    settings = _settings(user_id)
    _install_agent_key_store(
        monkeypatch,
        _AgentKeyStore(user_id=user_id, active_key_count=0),
        settings=settings,
    )

    response, reached = _run_v1_middleware(
        _build_request("POST", "/v1/workspaces/bootstrap", body={"user_id": str(uuid4())})
    )

    assert response.status_code == 401
    assert reached == []


def test_v1_query_user_id_cannot_widen_privilege(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.CRITICAL)
    user_id = uuid4()
    settings = _settings(user_id)
    _install_agent_key_store(
        monkeypatch,
        _AgentKeyStore(user_id=user_id, active_key_count=0),
        settings=settings,
    )

    response, reached = _run_v1_middleware(
        _build_request("GET", "/v1/providers", query={"user_id": str(uuid4())})
    )

    assert response.status_code == 401
    assert reached == []


def test_v1_keyless_loopback_request_is_still_served_while_no_key_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    settings = _settings(user_id)
    _install_agent_key_store(
        monkeypatch,
        _AgentKeyStore(user_id=user_id, active_key_count=0),
        settings=settings,
    )

    response, reached = _run_v1_middleware(_build_request("GET", "/v1/providers"))

    assert response.status_code == 204
    assert len(reached) == 1
    assert reached[0].state.v1_agent_identity is None


@pytest.mark.parametrize(
    ("app_env", "app_host"),
    (
        # The gate reads the peer address only. Settings that would once have
        # switched it off are pinned here so none of them can switch it off
        # again. The third case is the hole this hotfix closes: default env,
        # default bind, and a process actually reachable from the network.
        ("development", "127.0.0.1"),
        ("test", "127.0.0.1"),
        ("development", ""),
        ("test", "0.0.0.0"),
        ("production", "127.0.0.1"),
        ("production", "0.0.0.0"),
    ),
)
def test_v1_keyless_request_off_loopback_is_refused_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    app_env: str,
    app_host: str,
) -> None:
    caplog.set_level(logging.CRITICAL)
    user_id = uuid4()
    settings = _settings(user_id, app_env=app_env, app_host=app_host)

    def unreachable_store(_conn: object) -> object:
        raise AssertionError("off-loopback keyless requests must not reach the key store")

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "PostgresVNextStore", unreachable_store)

    response, reached = _run_v1_middleware(
        _build_request("GET", "/v1/providers", client_host=_REMOTE_CLIENT)
    )

    assert response.status_code == 401
    assert reached == []


def test_v1_keyless_request_from_loopback_is_served_on_an_exposed_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wide bind is fine as long as the peer that arrived is local."""

    user_id = uuid4()
    settings = _settings(user_id, app_host="0.0.0.0")
    _install_agent_key_store(
        monkeypatch,
        _AgentKeyStore(user_id=user_id, active_key_count=0),
        settings=settings,
    )

    response, reached = _run_v1_middleware(
        _build_request("GET", "/v1/providers", client_host=_LOOPBACK_CLIENT)
    )

    assert response.status_code == 204
    assert len(reached) == 1


def test_v1_forwarded_for_is_ignored_unless_the_peer_is_a_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spoofed forwarded header must not turn a local peer into a remote one."""

    user_id = uuid4()
    settings = _settings(user_id)
    _install_agent_key_store(
        monkeypatch,
        _AgentKeyStore(user_id=user_id, active_key_count=0),
        settings=settings,
    )

    response, reached = _run_v1_middleware(
        _build_request(
            "GET",
            "/v1/providers",
            client_host=_LOOPBACK_CLIENT,
            forwarded_for=_REMOTE_CLIENT,
        )
    )

    assert response.status_code == 204
    assert len(reached) == 1


def test_v1_forwarded_for_from_a_trusted_proxy_refuses_the_remote_client(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.CRITICAL)
    user_id = uuid4()
    settings = _settings(
        user_id,
        trust_proxy_headers=True,
        trusted_proxy_ips=(_LOOPBACK_CLIENT,),
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    response, reached = _run_v1_middleware(
        _build_request(
            "GET",
            "/v1/providers",
            client_host=_LOOPBACK_CLIENT,
            forwarded_for=_REMOTE_CLIENT,
        )
    )

    assert response.status_code == 401
    assert reached == []


def test_v1_post_body_still_reaches_the_route_after_middleware_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The middleware inspects the JSON body; the handler must still get it."""

    user_id = uuid4()
    candidate_id = uuid4()
    settings = _settings(user_id)
    _install_agent_key_store(
        monkeypatch,
        _AgentKeyStore(user_id=user_id, active_key_count=0),
        settings=settings,
    )

    @contextmanager
    def fake_user_connection(_database_url: str, _current_user_id: object) -> Iterator[object]:
        yield object()

    observed: dict[str, object] = {}

    def fake_commit(_store: object, *, user_id: UUID, request: object) -> dict[str, object]:
        observed["user_id"] = str(user_id)
        observed["candidate_ids"] = [str(value) for value in getattr(request, "candidate_ids")]
        observed["include_review_required"] = getattr(request, "include_review_required")
        return {"committed": observed["candidate_ids"]}

    monkeypatch.setattr(continuity_router, "get_settings", lambda: settings)
    monkeypatch.setattr(continuity_router, "user_connection", fake_user_connection)
    monkeypatch.setattr(continuity_router, "ContinuityStore", lambda _conn: object())
    monkeypatch.setattr(continuity_router, "commit_memory_operations", fake_commit)

    status, payload = _invoke_app(
        "POST",
        "/v1/memory/operations/commit",
        body={"candidate_ids": [str(candidate_id)], "include_review_required": True},
    )

    assert (status, payload) == (200, {"committed": [str(candidate_id)]})
    assert observed == {
        "user_id": str(user_id),
        "candidate_ids": [str(candidate_id)],
        "include_review_required": True,
    }


def test_every_v1_route_taking_a_body_pins_strict_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """The widening check reads JSON bodies only; FastAPI must reject the rest.

    ``_v1_request_payload`` returns ``{}`` unless the content type is JSON, so
    a foreign ``user_id`` sent as ``text/plain`` is never inspected by the
    middleware. That is safe only because every ``/v1`` route that binds a body
    model carries ``strict_content_type=True`` and so refuses a non-JSON body
    before the handler runs. Relaxing that on any route silently opens a
    cross-user path, so pin it here rather than relying on the alignment.
    """

    del monkeypatch
    relaxed: list[tuple[str, str]] = []
    for route in main_module.app.router.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        for context in contexts() if callable(contexts) else (route,):
            path = str(getattr(context, "path", ""))
            if not path.startswith("/v1"):
                continue
            dependant = getattr(context, "dependant", None)
            if not (getattr(dependant, "body_params", None) or []):
                continue
            if getattr(context, "strict_content_type", False) is not True:
                for method in sorted((getattr(context, "methods", None) or set()) - {"HEAD", "OPTIONS"}):
                    relaxed.append((method, path))

    assert relaxed == []


def test_v1_foreign_user_id_under_a_non_json_content_type_never_reaches_the_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body the middleware cannot inspect must not reach a handler either."""

    user_id = uuid4()
    settings = _settings(user_id)
    _install_agent_key_store(
        monkeypatch,
        _AgentKeyStore(user_id=user_id, active_key_count=0),
        settings=settings,
    )
    monkeypatch.setattr(
        continuity_router,
        "commit_memory_operations",
        lambda *_args, **_kwargs: pytest.fail("a non-JSON body must not reach the route"),
    )

    status, _payload = _invoke_app(
        "POST",
        "/v1/memory/operations/commit",
        body={"user_id": str(uuid4()), "candidate_ids": []},
        content_type="text/plain",
    )

    assert status != 200


def test_vnext_route_rejects_a_keyless_loopback_request_once_a_key_exists(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cover the keyed /v0/vnext refusal from a client the gate would accept.

    The repository's existing route sweep drives /v0/vnext from a synthetic,
    non-loopback client host, so the unconditional loopback gate now answers
    it before the key check runs. Pin the key requirement over HTTP from a
    loopback peer, where only the key check can produce the 401.
    """

    caplog.set_level(logging.CRITICAL)
    user_id = uuid4()
    settings = _settings(user_id)
    _install_agent_key_store(monkeypatch, _AgentKeyStore(user_id=user_id), settings=settings)

    status, payload = _invoke_app(
        "GET",
        "/v0/vnext/projects",
        query={"user_id": str(user_id)},
        client_host=_LOOPBACK_CLIENT,
    )

    assert (status, payload) == (401, _AUTHENTICATION_FAILED)


def test_vnext_keyless_request_off_loopback_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.CRITICAL)
    user_id = uuid4()
    # Default environment and default loopback bind: the configuration in
    # which /v0/vnext was previously reachable by any remote client.
    settings = _settings(user_id, app_env="development", app_host="127.0.0.1")

    def unreachable_auth(**_kwargs: object) -> tuple[object, object]:
        raise AssertionError("off-loopback keyless vNext requests must not reach the key store")

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "_resolve_vnext_http_auth", unreachable_auth)

    reached: list[Request] = []

    async def call_next(inner_request: Request) -> Response:
        reached.append(inner_request)
        return Response(status_code=204)

    request = _build_request(
        "GET",
        "/v0/vnext/projects",
        query={"user_id": str(user_id)},
        client_host=_REMOTE_CLIENT,
    )
    response = asyncio.run(main_module._vnext_protected_http_auth(request, call_next))

    assert response.status_code == 401
    assert reached == []


def test_vnext_keyless_request_on_loopback_still_reaches_the_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    settings = _settings(user_id, app_env="development", app_host="127.0.0.1")
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        main_module,
        "_resolve_vnext_http_auth",
        lambda **_kwargs: (None, None),
    )

    reached: list[Request] = []

    async def call_next(inner_request: Request) -> Response:
        reached.append(inner_request)
        return Response(status_code=204)

    request = _build_request(
        "GET",
        "/v0/vnext/projects",
        query={"user_id": str(user_id)},
        client_host=_LOOPBACK_CLIENT,
    )
    response = asyncio.run(main_module._vnext_protected_http_auth(request, call_next))

    assert response.status_code == 204
    assert len(reached) == 1
