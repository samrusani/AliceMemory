"""Run one probe call against the mounted application and read what it carried."""
from __future__ import annotations

import json
from urllib.parse import urlencode

import anyio

import alicebot_api.main as main_module

from tests.integration.operator_route_probes import Call
from tests.integration.operator_route_vault import Vault


def invoke(method, path, *, user_id, query=None, payload=None, key=None):
    """Invoke the mounted HTTP application with a query string and a body; return the status and the raw text."""

    messages = []
    body = json.dumps(payload).encode() if payload is not None else b""
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    headers = [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")]
    if key:
        headers.append((b"authorization", f"Bearer {key}".encode()))
    items = [("user_id", str(user_id))]
    for name, value in (query or {}).items():
        for item in value if isinstance(value, list) else [value]:
            items.append((name, str(item).lower() if isinstance(item, bool) else str(item)))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": urlencode(items).encode(),
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    anyio.run(main_module.app, scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    raw = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return int(start["status"]), raw.decode("utf-8", "replace")


def run_call(vault: Vault, method: str, template: str, call: Call, key: str | None):
    """Run one call and return its status and body text. A handler that raises answers 500 with the error's text."""

    path = template
    for name, value in call.path.items():
        path = path.replace("{" + name + "}", value)
    payload = None
    if call.body is not None:
        payload = {"user_id": str(vault.harness.user_id), **call.body}
    try:
        return invoke(method, path, user_id=vault.harness.user_id, query=call.query, payload=payload, key=key)
    except Exception as exc:  # the application raised, which a server answers with a bare 500
        return 500, repr(exc)


def carried(vault: Vault, text: str) -> list[str]:
    """The names of the sentinel texts found in ``text``, other than the ones a probe may itself send."""

    return [name for name, value in vault._texts.items() if value in text and name not in Vault.SENDABLE]
