"""A stored memory whose ``metadata_json.source_id`` holds a reference spelling must not break the context pack.

The PostgreSQL store looks a memory's provenance source up with ``WHERE id = %s::uuid``, so a
source id stored in a reference spelling (``urn:uuid:``, ``source:``, ``uuid:``,
``alice://sources/``, any case) reaches the date lookup of the context pack and fails the cast.
The date lookup now resolves the value with the same read-side parser that already decided the
memory's source is a stored source (``cited_source_ids``, the reader the derived-label floor and
the saved-provenance fence read) and skips the lookup when it names no id. These tests seed the
row the way a restore or a direct store write would, and compare every spelling's pack with the
canonical spelling's pack for the same caller.
"""

from __future__ import annotations

import json
import sys
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import psycopg
import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, memory_commit_request_from_payload
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.test_vnext_live_workspace_api import seed_user

TEXT = "The cobalt door opens on Monday."
QUERY = "cobalt door"

REFERENCE_SPELLINGS = (
    "urn:uuid:{id}",
    "URN:UUID:{id}",
    "source:{id}",
    "uuid:{id}",
    "alice://sources/{id}",
)


def _call(method: str, path: str, *, body: bytes = b"", authorization: str | None = None) -> tuple[int, Any]:
    messages: list[dict[str, object]] = []
    delivered = False

    async def receive() -> dict[str, object]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    headers = [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": urlencode({}).encode(), "headers": headers,
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80), "root_path": "",
    }
    anyio.run(main_module.app, scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    raw = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return int(start["status"]), json.loads(raw)


def _post(path: str, payload: dict[str, Any], **kwargs: Any) -> tuple[int, Any]:
    return _call("POST", path, body=json.dumps(payload).encode(), **kwargs)


def _pack_memories(payload: dict[str, object]) -> list[tuple[str, str | None]]:
    """The (id, event_time) of every memory a pack carries, in order."""

    return [
        (str(item.get("id")), item.get("event_time"))
        for item in (payload.get("relevant_memories") or [])
    ]


AGENT = {"agent_id": "hermes", "agent_type": "personal_assistant", "permission_profile": "trusted_local_agent"}


def _seed_source_and_memory(database_url: str, *, source_sensitivity: str = "internal") -> tuple[str, str, str]:
    """One stored source and one memory whose metadata names it, as a restore would."""

    user_id = seed_user(database_url, email=f"spelling-{uuid4().hex[:8]}@example.com")
    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        captured = store.create_source({
            "source_type": "note", "title": "Cobalt note", "raw_path": None,
            "content_hash": "hash-" + uuid4().hex,
            "domain": "project", "sensitivity": source_sensitivity, "metadata_json": {},
        })
        source_id = str(captured["id"])
        request = memory_commit_request_from_payload(
            {
                "user_id": str(user_id), "agent": AGENT, "title": "Cobalt fact",
                "canonical_text": TEXT, "idempotency_key": "spelling-seed",
            },
            user_id=user_id,
        )
        memory = VNextMemoryCommitService(store).commit(identity=None, request=request)
        memory_id = str(memory["memory"]["id"])
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "UPDATE memories SET metadata_json = jsonb_set(coalesce(metadata_json, '{}'::jsonb), '{source_id}', to_jsonb(%s::text)) WHERE id = %s",
            (source_id, memory_id),
        )
        conn.commit()
    return str(user_id), source_id, memory_id


def _set_source_id(admin_url: str, memory_id: str, value: str) -> None:
    with psycopg.connect(admin_url) as conn:
        conn.execute(
            "UPDATE memories SET metadata_json = jsonb_set(metadata_json, '{source_id}', to_jsonb(%s::text)) WHERE id = %s",
            (value, memory_id),
        )
        conn.commit()


def _create_key(database_url: str, user_id: str, *, agent_id: str, permission_profile: str, project_scope: str | None = None) -> str:
    with user_connection(database_url, UUID(user_id)) as conn:
        _record, raw_key = create_agent_key(
            PostgresVNextStore(conn), user_id=user_id, agent_id=agent_id, permission_profile=permission_profile, project_scope=project_scope
        )
    return raw_key


@pytest.fixture
def app(migrated_database_urls, monkeypatch):
    url = migrated_database_urls["app"]
    for name, module in list(sys.modules.items()):
        if name.startswith("alicebot_api") and module is not None and hasattr(module, "get_settings"):
            monkeypatch.setattr(module, "get_settings", lambda: Settings(database_url=url))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")

    class App:
        pass

    handle = App()
    handle.url = url
    handle.admin_url = migrated_database_urls["admin"]
    return handle


def _pack(app, user_id: str, key: str | None) -> tuple[int, Any]:
    if key is None:
        return _post("/v0/vnext/context-packs", {"user_id": user_id, "query": QUERY})
    return _post("/v0/vnext/context-packs", {"user_id": user_id, "query": QUERY}, authorization=f"Bearer {key}")


@pytest.mark.parametrize("spelling", REFERENCE_SPELLINGS)
def test_http_pack_answers_a_reference_spelling_like_the_canonical_spelling(app, spelling) -> None:
    """Keyless and keyed packs for a reference spelling are the canonical spelling's pack.

    Each caller is its own user: a keyless install keeps its keyless owner,
    and a keyed user authenticates with the key at every call.
    """

    def checked(key: str | None, agent_id: str, profile: str, name: str) -> None:
        user_id, source_id, memory_id = _seed_source_and_memory(app.url)
        if key is not None:
            del key
        if agent_id:
            key = _create_key(app.url, user_id, agent_id=agent_id, permission_profile=profile)
        _set_source_id(app.admin_url, memory_id, source_id)
        canonical_status, canonical_answer = _pack(app, user_id, key)
        assert canonical_status in {200, 201}, (name, canonical_status)
        canonical = _pack_memories(canonical_answer)
        assert canonical and canonical[0][1] is not None, name
        _set_source_id(app.admin_url, memory_id, spelling.format(id=source_id))
        status, answer = _pack(app, user_id, key)
        assert status in {200, 201}, (name, status, answer)
        assert _pack_memories(answer) == canonical, name

    checked(None, "", "", "keyless owner")
    checked(None, "admin-reader", "admin_agent", "unbound admin_agent key")
    checked(None, "trusted-reader", "trusted_local_agent", "trusted key")


@pytest.mark.parametrize("spelling", REFERENCE_SPELLINGS)
def test_mcp_context_pack_answers_a_reference_spelling_like_the_canonical_spelling(app, spelling) -> None:
    """The tool result carries the memory exactly when the canonical spelling does, keyless and keyed."""

    user_id, source_id, memory_id = _seed_source_and_memory(app.url)
    admin_key = _create_key(app.url, user_id, agent_id="admin-reader", permission_profile="admin_agent")

    def tool(identity: AgentIdentity | None) -> dict[str, object]:
        context = MCPRuntimeContext(
            database_url=app.url, user_id=UUID(user_id), agent_identity=identity, agent_identity_resolved=identity is not None
        )
        return call_mcp_tool(context, name="alice_context_pack", arguments={"query": QUERY})

    def tool_memories(result: object) -> list[tuple[str, str | None]]:
        return [
            (str(item.get("id")), item.get("event_time"))
            for item in (result.get("memories") or [])  # type: ignore[union-attr]
        ]

    _set_source_id(app.admin_url, memory_id, source_id)
    canonical_keyless = tool(None)
    canonical_admin = tool(AgentIdentity(agent_id="admin-reader", permission_profile="admin_agent", auth="agent_key"))
    assert tool_memories(canonical_keyless) and tool_memories(canonical_keyless)[0][1] is not None

    _set_source_id(app.admin_url, memory_id, spelling.format(id=source_id))
    for name, result in (("keyless", tool(None)), ("keyed", tool(AgentIdentity(agent_id="admin-reader", permission_profile="admin_agent", auth="agent_key")))):
        assert "tool_execution_failed" not in json.dumps(result), (name, result)
    assert tool_memories(tool(None)) == tool_memories(canonical_keyless)
    assert tool_memories(tool(AgentIdentity(agent_id="admin-reader", permission_profile="admin_agent", auth="agent_key"))) == tool_memories(canonical_admin)


@pytest.mark.parametrize("value", ("manual-import", "manual import", "{id}#chunk-1", "   "))
def test_a_value_that_names_no_named_source_id_skips_the_date_lookup(app, value) -> None:
    """A source_id value the reader names no named id for leaves the date lookup out.

    A free label, a padded value, a `#fragment` and a blank name no named id
    (a padded value's id is named incidentally, which the lookup does not
    read), so the lookup is skipped and the pack answers HTTP 200 on
    PostgreSQL with no server error and no `event_time` taken from a
    source. This is the branch of the date lookup that answers for a value
    the reader does not resolve. Whether the memory itself is selected is
    the read fence's decision and not this lookup's: a free label keeps the
    memory out of the pack, as it did before this series, and a blank value
    keeps it in.
    """

    user_id, source_id, memory_id = _seed_source_and_memory(app.url)

    _set_source_id(app.admin_url, memory_id, value.format(id=source_id))
    status, answer = _pack(app, user_id, None)
    assert status in {200, 201}, (status, answer)
    memories = _pack_memories(answer)
    assert memories in ([], [(memory_id, None)]), answer
    if not memories:
        return

    _set_source_id(app.admin_url, memory_id, "")
    baseline_status, baseline_answer = _pack(app, user_id, None)
    assert baseline_status in {200, 201}
    assert _pack_memories(baseline_answer) == memories


@pytest.mark.parametrize("spelling", REFERENCE_SPELLINGS)
def test_a_source_the_key_may_not_read_reads_the_same_in_every_spelling(app, spelling) -> None:
    """A key below the source's sensitivity learns nothing new from any spelling.

    The source is stored confidential, above the ``trusted_local_agent``
    ceiling, so the memory that cites it is unverified for that key at every
    spelling, exactly as the canonical spelling leaves it.
    """

    hidden_user, hidden_source_id, hidden_memory_id = _seed_source_and_memory(app.url, source_sensitivity="confidential")
    hidden_key = _create_key(app.url, hidden_user, agent_id="hidden-reader", permission_profile="trusted_local_agent")

    _set_source_id(app.admin_url, hidden_memory_id, hidden_source_id)
    canonical_status, canonical_answer = _pack(app, hidden_user, hidden_key)
    assert canonical_status in {200, 201}
    assert _pack_memories(canonical_answer) == []

    _set_source_id(app.admin_url, hidden_memory_id, spelling.format(id=hidden_source_id))
    status, answer = _pack(app, hidden_user, hidden_key)
    assert status in {200, 201}, answer
    assert _pack_memories(answer) == _pack_memories(canonical_answer)


@pytest.mark.parametrize("spelling", REFERENCE_SPELLINGS)
def test_a_key_bound_to_another_project_reads_the_same_in_every_spelling(app, spelling) -> None:
    """A project-bound key's pack for every spelling is its canonical pack."""

    bound_user, bound_source_id, bound_memory_id = _seed_source_and_memory(app.url)
    bound_key = _create_key(app.url, bound_user, agent_id="bound-reader", permission_profile="trusted_local_agent", project_scope="SomeOtherProject")

    def pack() -> tuple[int, Any]:
        return _pack(app, bound_user, bound_key)

    _set_source_id(app.admin_url, bound_memory_id, bound_source_id)
    canonical_status, canonical_answer = pack()
    assert canonical_status in {200, 201}
    canonical = _pack_memories(canonical_answer)

    _set_source_id(app.admin_url, bound_memory_id, spelling.format(id=bound_source_id))
    status, answer = pack()
    assert status in {200, 201}, answer
    assert _pack_memories(answer) == canonical
