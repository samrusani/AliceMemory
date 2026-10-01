"""Drive the real app over raw ASGI with a fake Postgres store, for the API edge tests.

Every middleware runs in order, the bytes of the body are exactly what the test
wrote, and a streamed body is generated on demand so a test can offer 64 MiB
without holding it. ``Edge.call`` reports what the app answered, how many bytes
it pulled through ``receive`` and whether a route handler was reached.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import anyio
import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.routers import vnext_projects
from alicebot_api.vnext_agent_keys import hash_agent_key

USER = "00000000-0000-0000-0000-000000000001"
SENTINEL = "PRIVATE-CHARTER-SENTINEL-5521"
# Runtime-built, so it is a fixture and not a credential literal.
KEY = "alice_sk_" + "v1_" + "edge_fixture_key_" + "0" * 12

CHARTER_PATH = "/v0/vnext/settings/brain-charter"
EVAL_RUNS_PATH = "/v1/evals/runs"
BOOTSTRAP_PATH = "/v1/workspaces/bootstrap"
V1_LIST_PATH = "/v1/memory/operations"


class Store:
    """The slice of the vNext store the gates and the charter route touch."""

    def __init__(self, *, active_keys: int) -> None:
        self.active_keys = active_keys
        self.record: dict[str, Any] = {
            "id": "11111111-1111-1111-1111-111111111111",
            "user_id": USER,
            "agent_id": "edge-fixture-admin",
            "permission_profile": "admin_agent",
            "project_scope": None,
            "key_hash": hash_agent_key(KEY),
            "key_prefix": KEY[:12],
            "revoked_at": None,
        }
        self.charter: dict[str, Any] = {"content_markdown": SENTINEL}
        self.writes: list[dict[str, Any]] = []

    def count_active_agent_api_keys(self) -> int:
        return self.active_keys

    def get_agent_api_key_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        if self.active_keys > 0 and key_hash == self.record["key_hash"]:
            return dict(self.record)
        return None

    def touch_agent_api_key(self, *, key_id: str) -> dict[str, Any]:
        return dict(self.record)

    def append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        return event

    def get_brain_charter(self) -> dict[str, Any]:
        return self.charter

    def upsert_brain_charter(self, data: dict[str, Any], actor_type: str = "user") -> dict[str, Any]:
        self.writes.append(dict(data))
        self.charter = dict(data)
        return self.charter


class HandlerReached(Exception):
    """A route handler other than the two the fake store serves opened its store connection."""


@dataclass
class Outcome:
    status: int
    body: bytes
    resolver_calls: int
    reached_handler: bool = False
    pulled_bytes: int = 0
    receive_calls: int = 0
    headers: dict[str, str] | None = None
    response_starts: int = 0

    @property
    def refused(self) -> bool:
        return self.status == 401 and b"authentication_failed" in self.body


class Edge:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.store = Store(active_keys=0)
        self.resolver_calls = 0
        self.settings = Settings(app_env="development", auth_user_id="")

    def configure(self, *, keys: int = 0, bound: bool = False, **settings: Any) -> Store:
        self.store.active_keys = keys
        self.settings = Settings(
            app_env="development",
            auth_user_id=USER if bound else "",
            database_url="postgresql://edge-unreachable",
            **settings,
        )
        monkeypatch = self.monkeypatch
        for name, module in list(sys.modules.items()):
            if name.startswith("alicebot_api") and module is not None and hasattr(module, "get_settings"):
                monkeypatch.setattr(module, "get_settings", lambda: self.settings)

        @contextmanager
        def connection(database_url: str, current_user_id: object) -> Iterator[object]:
            yield object()

        def tripwire(database_url: str, current_user_id: object) -> Any:
            raise HandlerReached()

        for name, module in list(sys.modules.items()):
            if name.startswith("alicebot_api.routers") and module is not None and hasattr(module, "user_connection"):
                monkeypatch.setattr(module, "user_connection", tripwire)
        for module in (main_module, vnext_projects):
            monkeypatch.setattr(module, "user_connection", connection)
            monkeypatch.setattr(module, "PostgresVNextStore", lambda _conn: self.store)

        original = main_module._resolve_v1_http_auth

        def counted_v1_resolver(**kwargs: Any) -> Any:
            self.resolver_calls += 1
            return original(**kwargs)

        monkeypatch.setattr(main_module, "_resolve_v1_http_auth", counted_v1_resolver)
        return self.store

    def call(
        self,
        method: str,
        path: str,
        *,
        host: str | None = "127.0.0.1:8000",
        origin: str | None = None,
        authorization: str | None = None,
        body: bytes = b"",
        stream_bytes: int | None = None,
        chunk_size: int = 1 << 20,
        declared_length: int | None = None,
        content_type: str | None = None,
        query: dict[str, str] | None = None,
        extra_headers: list[tuple[str, str]] | None = None,
        peer: str = "127.0.0.1",
    ) -> Outcome:
        """One request. ``stream_bytes`` offers that many bytes in ``chunk_size`` pieces instead of ``body``."""

        headers: list[tuple[bytes, bytes]] = []
        if host is not None:
            headers.append((b"host", host.encode()))
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        if authorization is not None:
            headers.append((b"authorization", authorization.encode()))
        if content_type is not None:
            headers.append((b"content-type", content_type.encode()))
        if declared_length is not None:
            headers.append((b"content-length", str(declared_length).encode()))
        for name, value in extra_headers or []:
            headers.append((name.lower().encode(), value.encode()))
        messages: list[dict[str, Any]] = []
        pulled = 0
        calls = 0
        offered = 0
        sent_single = False

        async def receive() -> dict[str, Any]:
            nonlocal pulled, calls, offered, sent_single
            calls += 1
            if stream_bytes is None:
                if sent_single:
                    return {"type": "http.disconnect"}
                sent_single = True
                pulled += len(body)
                return {"type": "http.request", "body": body, "more_body": False}
            if offered >= stream_bytes:
                return {"type": "http.disconnect"}
            size = min(chunk_size, stream_bytes - offered)
            offered += size
            pulled += size
            return {"type": "http.request", "body": b"x" * size, "more_body": offered < stream_bytes}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": urlencode(query or {}).encode(),
            "headers": headers,
            "client": (peer, 50000),
            "server": ("127.0.0.1", 8000),
            "root_path": "",
        }
        self.resolver_calls = 0
        try:
            anyio.run(main_module.app, scope, receive, send)
        except HandlerReached:
            return Outcome(0, b"", self.resolver_calls, reached_handler=True, pulled_bytes=pulled, receive_calls=calls)
        starts = [m for m in messages if m["type"] == "http.response.start"]
        start = starts[0]
        data = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return Outcome(
            int(start["status"]),
            data,
            self.resolver_calls,
            pulled_bytes=pulled,
            receive_calls=calls,
            headers={key.decode().lower(): value.decode() for key, value in start["headers"]},
            response_starts=len(starts),
        )

    def charter(self, **kwargs: Any) -> Outcome:
        query = None if self.settings.auth_user_id else {"user_id": USER}
        return self.call("GET", CHARTER_PATH, query=query, **kwargs)
