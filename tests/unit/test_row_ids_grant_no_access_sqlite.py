"""Knowing the id of a row never gives a key access to it, on SQLite.

SQLite has no HTTP route and no artifact table, so the doors a key reaches are the tools. Each case builds a vault with
sources, memories and open loops hidden for each reason, gives a real key of each profile, and calls every tool that
takes an id with every id that profile may not read. The rules are those of the PostgreSQL matrix in
``tests/integration/test_row_ids_grant_no_access_postgres.py``: the call changes exactly what a missing id changes, nothing of the row comes
back, and the answer is the one a missing id gets, except at the doors named in ``POLICY_REFUSAL_DOORS``.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_label_writes import without_insert_floor
from tests.integration.hidden_ids_postgres_support import (
    ALL_DOORS,
    HIDDEN_FOR,
    POLICY_REFUSAL_DOORS,
    PROFILES,
    Answer,
    normalize,
)

ALPHA = "prj_" + "a" * 16
BETA = "prj_" + "b" * 16
# reason -> (sensitivity, domain, project). A SQLite source is always an original, so it is never unverified.
REASONS = {
    "visible": ("public", "project", ALPHA),
    "confidential": ("confidential", "project", ALPHA),
    "private": ("private", "project", ALPHA),
    "health": ("public", "health", ALPHA),
    "beta": ("public", "project", BETA),
    "unverified": ("public", "project", ALPHA),
}
# Tables a tool can change. The key table records when a key was last used and the events record every refusal.
IGNORED_TABLES = {"agent_api_keys", "agent_identities", "event_log", "events"}


class SqliteEnv:
    def __init__(self, monkeypatch, url: str, user_id) -> None:
        self.monkeypatch = monkeypatch
        self.url = url
        self.user_id = user_id

    def tool(self, key, name: str, arguments: dict[str, object]) -> Answer:
        self.monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        if key:
            self.monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        context = MCPRuntimeContext(database_url=self.url, user_id=UUID(str(self.user_id)))
        try:
            result = call_mcp_tool(context, name=name, arguments=arguments)
        except MCPToolError as exc:
            return Answer(f"tool-error:{type(exc).__name__}", normalize(str(exc)))
        return Answer("tool-ok", normalize(result))


def snapshot(path: Path) -> dict[str, list[str]]:
    connection = sqlite3.connect(path)
    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        result = {}
        for name in names:
            if name in IGNORED_TABLES or "fts" in name:
                continue
            rows = connection.execute(f'SELECT * FROM "{name}"').fetchall()  # names come from the schema
            result[name] = sorted(json.dumps([str(value) for value in row]) for row in rows)
        return result
    finally:
        connection.close()


def build(tmp_path, monkeypatch, profile):
    user_id = uuid4()
    path = tmp_path / "vault.sqlite3"
    bootstrap_database(path, user_id=str(user_id), user_email="synthetic@example.invalid")
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    ids: dict[tuple[str, str], str] = {}
    secrets: dict[tuple[str, str], list[str]] = {}
    with sqlite_user_connection(path, user_id) as conn:
        store = SQLiteVNextStore(conn, user_id)
        for reason, (sensitivity, domain, project) in REASONS.items():
            tag = reason.upper()
            if reason != "unverified":
                source = store.create_source(
                    {
                        "source_type": "note", "title": f"TITLE-source-{tag}", "content_hash": str(uuid4()),
                        "domain": domain, "sensitivity": sensitivity,
                        "metadata_json": {"project_scope": [project], "raw_text": f"RAW-source-{tag}"},
                    }
                )
                ids[("source", reason)] = str(source["id"])
                secrets[("source", reason)] = [f"TITLE-source-{tag}", f"RAW-source-{tag}"]
            memory_metadata: dict[str, object] = {"project_scope": [project]}
            loop_metadata: dict[str, object] = {"project_scope": [project]}
            if reason == "unverified":
                memory_metadata["source_id"] = str(uuid4())
                loop_metadata.update({"discovered_by": "vnext_daily_brief", "source_id": str(uuid4())})
            with without_insert_floor():
                memory = store.create_memory(
                    {
                        "memory_key": str(uuid4()), "title": f"TITLE-memory-{tag}", "canonical_text": f"TEXT-memory-{tag}",
                        "status": "active", "domain": domain, "sensitivity": sensitivity, "metadata_json": memory_metadata,
                    }
                )
                loop = store.create_open_loop(
                    {
                        "title": f"TITLE-loop-{tag}", "description": f"TEXT-loop-{tag}", "domain": domain,
                        "sensitivity": sensitivity, "metadata_json": loop_metadata,
                    }
                )
            ids[("memory", reason)] = str(memory["id"])
            secrets[("memory", reason)] = [f"TITLE-memory-{tag}", f"TEXT-memory-{tag}"]
            ids[("loop", reason)] = str(loop["id"])
            secrets[("loop", reason)] = [f"TITLE-loop-{tag}", f"TEXT-loop-{tag}"]
        permission, bound = PROFILES[profile]
        _record, key = create_agent_key(
            store, user_id=user_id, agent_id=profile, permission_profile=permission, project_scope=ALPHA if bound else None
        )
    return path, SqliteEnv(monkeypatch, sqlite_url_for_path(path), user_id), key, ids, secrets


def hidden_for_profile(ids, profile):
    return {
        key: value
        for key, value in ids.items()
        if profile in HIDDEN_FOR.get(key[1], frozenset())
    }


@pytest.mark.parametrize("profile", list(PROFILES))
def test_a_hidden_id_gives_no_access_through_any_tool(tmp_path, monkeypatch, profile):
    path, env, key, ids, secrets = build(tmp_path, monkeypatch, profile)
    hidden = hidden_for_profile(ids, profile)
    if profile == "admin":
        assert not hidden
        return
    assert hidden, profile
    doors = [door for door in ALL_DOORS if door.name.startswith("tool")]
    failures: list[str] = []
    calls = 0
    for door in doors:
        before = snapshot(path) if door.write else None
        missing = door.call(env, key, str(uuid4()))
        after = snapshot(path) if door.write else None
        missing_changed = {t for t in (before or {}) if before[t] != after[t]}
        for (kind, reason), row_id in hidden.items():
            before = snapshot(path) if door.write else None
            got = door.call(env, key, row_id)
            after = snapshot(path) if door.write else None
            calls += 1
            label = f"{profile} / {door.name} / {kind} hidden as {reason}"
            changed = {t for t in (before or {}) if before[t] != after[t]}
            if changed != missing_changed:
                failures.append(f"{label}: changed {sorted(changed)} where a missing id changes {sorted(missing_changed)}")
            leaked = [word for word in secrets.get((kind, reason), []) if word in got.body]
            if leaked:
                failures.append(f"{label}: answered with {leaked}")
            if got != missing and not (door.name in POLICY_REFUSAL_DOORS and got.refused):
                failures.append(f"{label}: {got} differs from a missing id: {missing}")
    assert calls > 100, calls
    assert not failures, "\n".join(failures[:25])


def test_the_same_tools_serve_a_row_the_profile_may_read(tmp_path, monkeypatch):
    path, env, key, ids, _secrets = build(tmp_path, monkeypatch, "read_only")
    doors = {door.name: door for door in ALL_DOORS}
    for name in ("tool explain memory_id", "tool review item"):
        answer = doors[name].call(env, key, ids[("memory", "visible")])
        assert answer.status == "tool-ok", (name, answer)
        assert "TEXT-memory-VISIBLE" in answer.body
    # And the same tool, with the id of a row above the key's ceiling, is not served.
    answer = doors["tool explain memory_id"].call(env, key, ids[("memory", "confidential")])
    assert answer.refused and "TEXT-memory-CONFIDENTIAL" not in answer.body
