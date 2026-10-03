"""``POST /v0/vnext/context-packs`` holds a restricted key to its permitted domains when the request names none.

Unreleased (on main, not in v0.20.0). On v0.20.0 a ``project_scoped_agent`` key that was refused when it asked for
``domains: ["health"]`` read the health notes and sources of its own project when it sent no ``domains``, because the
policy engine left ``effective_domains`` empty and the Postgres readers read an empty list as "no domain filter".

The route runs on Postgres only, so the unit tests (``tests/unit/test_omitted_domains_every_reader.py``) run the same
route function over the SQLite store. This file is the Postgres half: it is collected by the CI Postgres job
(``pytest tests/integration``), and no local Postgres was available when it was written, so its first run is the CI run.

Mutation: remove the ``if not domains: return permitted, ()`` branch of ``_filtered_domains`` in
``vnext_agent_control.py``. The restricted-key tests then return the health memory and the health source.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import anyio
import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.routers import vnext_retrieval as vnext_retrieval_router
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_store import PostgresVNextStore

PERMITTED = ["professional", "personal", "learning", "relationship", "project", "agent_run", "system", "unknown"]
HELD_BACK = ["family", "health", "spiritual", "legal", "financial"]
PROJECT = "kiln-alpha"
QUERY = "kiln"


def invoke_request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    authorization: str | None = None,
) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, object]] = []
    encoded_body = b"" if payload is None else json.dumps(payload).encode()
    request_received = False

    async def receive() -> dict[str, object]:
        nonlocal request_received
        if request_received:
            return {"type": "http.disconnect"}
        request_received = True
        return {"type": "http.request", "body": encoded_body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    headers = [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    anyio.run(main_module.app, scope, receive, send)
    start_message = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return int(start_message["status"]), json.loads(body)


def seed_user(database_url: str, *, email: str) -> UUID:
    user_id = uuid4()
    with user_connection(database_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, email, email.split("@", 1)[0].title())
    return user_id


def seed_rows(database_url: str, user_id: UUID) -> dict[str, dict[str, str]]:
    """One memory and one source of project ``kiln-alpha`` in a permitted and in a held-back domain."""

    rows: dict[str, dict[str, str]] = {}
    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        for domain in ("project", "health"):
            text = f"The kiln schedule entry SENTINEL{domain.upper()} is posted on the wall calendar."
            source = store.create_source(
                {
                    "source_type": "manual_text",
                    "title": f"kiln schedule posting {domain}",
                    "content_hash": f"sha256:{uuid4().hex}",
                    "domain": domain,
                    "sensitivity": "internal",
                    "metadata_json": {
                        "raw_text": text,
                        "project_id": PROJECT,
                        "project_scope": [PROJECT],
                    },
                },
                actor_type="user",
            )
            memory = store.create_memory(
                {
                    "memory_key": f"kiln.{domain}.{uuid4().hex}",
                    "value": {"text": text},
                    "status": "active",
                    "memory_type": "semantic",
                    "canonical_text": text,
                    "domain": domain,
                    "sensitivity": "internal",
                    "project_id": PROJECT,
                    "metadata_json": {"project_id": PROJECT, "project_scope": [PROJECT]},
                },
                actor_type="user",
            )
            rows[domain] = {"memory": str(memory["id"]), "source": str(source["id"])}
    return rows


def _patch_settings(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    for module in (main_module, vnext_retrieval_router):
        monkeypatch.setattr(module, "get_settings", lambda: Settings(database_url=database_url))


def _pack(user_id: UUID, *, authorization: str | None, scope: dict[str, object] | None = None, **identity: object):  # type: ignore[no-untyped-def]
    return invoke_request(
        "POST",
        "/v0/vnext/context-packs",
        authorization=authorization,
        payload={
            "user_id": str(user_id),
            "query": QUERY,
            "scope": scope or {},
            "options": {
                "include_sources": True,
                "sensitivity_allowed": ["public", "internal", "private", "unknown"],
                "max_items": 8,
            },
            **identity,
        },
    )


def _ids(pack: dict[str, Any]) -> tuple[set[str], set[str]]:
    memories = {str(row["id"]) for row in pack["relevant_memories"]}
    sources = {str(row["id"]) for row in pack["sources"]}
    return memories, sources


def test_a_restricted_key_that_names_no_domain_is_held_to_its_permitted_domains(
    migrated_database_urls,
    monkeypatch,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="omitted-domains-keyed@example.com")
    _patch_settings(monkeypatch, migrated_database_urls["app"])
    rows = seed_rows(migrated_database_urls["app"], user_id)
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = PostgresVNextStore(conn)
        _record, restricted_key = create_agent_key(
            store,
            user_id=user_id,
            agent_id="omitted-domains-restricted",
            permission_profile="project_scoped_agent",
            project_scope=PROJECT,
        )
        _record, trusted_key = create_agent_key(
            store,
            user_id=user_id,
            agent_id="omitted-domains-trusted",
            permission_profile="trusted_local_agent",
            project_scope=PROJECT,
        )

    status, pack = _pack(user_id, authorization=f"Bearer {restricted_key}")
    assert status == 201, pack
    memories, sources = _ids(pack)
    assert rows["project"]["memory"] in memories
    assert rows["project"]["source"] in sources
    assert rows["health"]["memory"] not in memories
    assert rows["health"]["source"] not in sources
    assert "SENTINELHEALTH" not in json.dumps(pack)
    assert pack["policy_decision"]["decision"] == "allowed"
    assert pack["policy_decision"]["requested_domains"] == []
    assert pack["policy_decision"]["effective_domains"] == PERMITTED
    assert pack["query_interpretation"]["domains"] == PERMITTED

    # An empty list is the same request.
    status, empty_pack = _pack(user_id, authorization=f"Bearer {restricted_key}", scope={"domains": []})
    assert status == 201, empty_pack
    memories, sources = _ids(empty_pack)
    assert rows["health"]["memory"] not in memories
    assert rows["health"]["source"] not in sources

    # A named permitted domain narrows the request, and a held-back one is still refused.
    status, named = _pack(user_id, authorization=f"Bearer {restricted_key}", scope={"domains": ["project"]})
    assert status == 201, named
    assert named["policy_decision"]["effective_domains"] == ["project"]
    assert rows["project"]["memory"] in _ids(named)[0]
    status, refused = _pack(user_id, authorization=f"Bearer {restricted_key}", scope={"domains": ["health"]})
    assert status == 403
    assert "all_requested_domains_restricted" in refused["policy_decision"]["reasons"]

    # A trusted key, with the same binding, still reads both domains when it names none.
    status, trusted_pack = _pack(user_id, authorization=f"Bearer {trusted_key}")
    assert status == 201, trusted_pack
    memories, sources = _ids(trusted_pack)
    assert {rows["project"]["memory"], rows["health"]["memory"]} <= memories
    assert {rows["project"]["source"], rows["health"]["source"]} <= sources
    assert trusted_pack["policy_decision"]["effective_domains"] == []
    assert trusted_pack["query_interpretation"]["domains"] == []


def test_a_keyless_request_that_declares_a_restricted_profile_is_held_to_it(
    migrated_database_urls,
    monkeypatch,
) -> None:
    """No key is issued for this user, so a declared identity is honoured, as on a keyless install. The declared profile
    holds it to the permitted domains, and the owner (no identity) and a declared trusted profile read both."""

    user_id = seed_user(migrated_database_urls["app"], email="omitted-domains-keyless@example.com")
    _patch_settings(monkeypatch, migrated_database_urls["app"])
    rows = seed_rows(migrated_database_urls["app"], user_id)

    status, restricted = _pack(
        user_id,
        authorization=None,
        agent_identity={
            "agent_id": "omitted-domains-declared",
            "agent_type": "coding_agent",
            "permission_profile": "read_only_agent",
        },
    )
    assert status == 201, restricted
    memories, sources = _ids(restricted)
    assert rows["project"]["memory"] in memories
    assert rows["health"]["memory"] not in memories
    assert rows["health"]["source"] not in sources
    assert restricted["policy_decision"]["effective_domains"] == PERMITTED

    status, trusted = _pack(
        user_id,
        authorization=None,
        agent_identity={
            "agent_id": "omitted-domains-declared-trusted",
            "agent_type": "coding_agent",
            "permission_profile": "trusted_local_agent",
        },
    )
    assert status == 201, trusted
    assert rows["health"]["memory"] in _ids(trusted)[0]

    status, owner = _pack(user_id, authorization=None)
    assert status == 201, owner
    assert {rows["project"]["memory"], rows["health"]["memory"]} <= _ids(owner)[0]
    assert owner["query_interpretation"]["domains"] == []
