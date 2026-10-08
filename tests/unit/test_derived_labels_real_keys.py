"""Core read doors authenticate actual SQLite keys before effective admission."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_doctor import VNextDoctorService
from alicebot_api.vnext_label_writes import without_insert_floor
from alicebot_api.vnext_secrets import InMemorySecretProvider
from tests.unit.test_vnext_doctor import DoctorStore


READERS = ("owner", "admin", "trusted", "read_only", "bound_admin", "bound_trusted", "bound_read_only")
ALPHA = "prj_" + "a" * 16


def seed_read_rows(store):
    source = store.create_source({"source_type": "note", "title": "Restricted parent", "content_hash": str(uuid4()),
                                 "domain": "health", "sensitivity": "confidential", "metadata_json": {"project_scope": [ALPHA]}})
    rows = {}
    for state in ("verified_public", "verified_confidential", "unverified"):
        metadata = {"project_scope": [ALPHA]}
        if state != "verified_public":
            metadata["source_id"] = str(source["id"]) if state == "verified_confidential" else str(uuid4())
        with without_insert_floor():
            rows[state] = store.create_memory({"memory_key": state, "canonical_text": f"Cedar sentinel {state}",
                                               "title": f"Hidden title {state}", "memory_type": "decision", "status": "active",
                                               "domain": "project", "sensitivity": "public", "metadata_json": metadata})
        append_event(store, event_type="memory.labels_raised", actor_type="system",
                     target_type="memory", target_id=str(rows[state]["id"]), payload={"cause": "repair_v3"})
    return rows


def real_reader_key(store, user_id, reader):
    if reader == "owner":
        return None
    profile = {"admin": "admin_agent", "trusted": "trusted_local_agent", "read_only": "read_only_agent"}[reader.removeprefix("bound_")]
    _record, raw = create_agent_key(store, user_id=user_id, agent_id=reader, permission_profile=profile,
                                    project_scope=ALPHA if reader.startswith("bound_") else None)
    return raw


def expected_read(reader, state):
    return state == "verified_public" or reader in {"owner", "admin"} or (reader == "bound_admin" and state == "verified_confidential")


@pytest.mark.parametrize("reader", READERS)
def test_sqlite_real_key_core_doors(tmp_path, monkeypatch, reader):
    user_id = uuid4()
    path = tmp_path / "reads.sqlite3"
    bootstrap_database(path, user_id=str(user_id), user_email="synthetic@example.invalid")
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    with sqlite_user_connection(path, user_id) as conn:
        store = SQLiteVNextStore(conn, user_id)
        rows = seed_read_rows(store)
        key = real_reader_key(store, user_id, reader)
    if key:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=UUID(str(user_id)))
    for state, row in rows.items():
        admitted = expected_read(reader, state)
        for tool in ("alice_recall", "alice_context_pack", "alice_recent_decisions", "alice_explain", "alice_resume"):
            arguments = {"memory_id": str(row["id"])} if tool == "alice_explain" else {"query": str(row["canonical_text"]), "sensitivity_allowed": list(ALL_SENSITIVITY)}
            try:
                result = call_mcp_tool(context, name=tool, arguments=arguments)
            except MCPToolError:
                assert not admitted, (reader, state, tool)
                continue
            rendered = json.dumps(result, default=str)
            assert (str(row["id"]) in rendered) is admitted, (reader, state, tool, rendered)
            if not admitted:
                assert str(row["title"]) not in rendered


@pytest.mark.parametrize("reader", READERS)
def test_original_loop_holds_back_stale_backing_memory_reference(tmp_path, monkeypatch, reader):
    user_id = uuid4()
    path = tmp_path / "loop-refs.sqlite3"
    bootstrap_database(path, user_id=str(user_id), user_email="synthetic@example.invalid")
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    with sqlite_user_connection(path, user_id) as conn:
        store = SQLiteVNextStore(conn, user_id)
        memory = seed_read_rows(store)["verified_confidential"]
        loop = store.create_open_loop({"title": "Visible original loop", "memory_id": str(memory["id"]), "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [ALPHA]}})
        key = real_reader_key(store, user_id, reader)
    if key:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    result = call_mcp_tool(MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=user_id), name="alice_open_loops", arguments={"sensitivity_allowed": list(ALL_SENSITIVITY)})
    item = next(row for row in result["items"] if str(row["id"]) == str(loop["id"]))
    admitted = expected_read(reader, "verified_confidential")
    assert item["memory_id"] == (str(memory["id"]) if admitted else None)
    assert (str(memory["id"]) in json.dumps(result, default=str)) is admitted


def test_full_owner_doctor_keeps_true_counts_and_filtered_view_skips_content(tmp_path):
    user_id = uuid4()
    path = tmp_path / "doctor.sqlite3"
    bootstrap_database(path, user_id=str(user_id), user_email="synthetic@example.invalid")
    with sqlite_user_connection(path, user_id) as conn:
        store = SQLiteVNextStore(conn, user_id)
        # SQLite has no provider/connector doctor protocol. Keep those synthetic
        # operations fixed while the real connection supplies content diagnostics.
        diagnostic_store = DoctorStore()
        diagnostic_store.conn = store.conn
        diagnostic_store.list_sources = lambda **kwargs: [row for batch in store.iter_label_rows("source") for row in batch]
        service = VNextDoctorService(diagnostic_store, secret_provider=InMemorySecretProvider(), env={}, cwd=tmp_path)
        before = service.run(include_content_diagnostics=False)
        rows = seed_read_rows(store)
        full = service.run()
        scoped = service.run(include_content_diagnostics=False)
    derived = next(check for check in full["checks"] if check["name"] == "derived_labels")
    assert derived["message"] == "derived labels: 1 below their inputs, 1 unverified"
    skipped = [check for check in scoped["checks"] if check["name"] in {"derived_labels", "flagged_sources"}]
    assert len(skipped) == 2
    assert all(check["status"] == "skipped" and check["details"] == {"scope": "filtered_workspace", "evaluated": False} for check in skipped)
    assert all(str(row["id"]) not in json.dumps(scoped) for row in rows.values())
    for field in ("status", "blocking_failure_count", "warning_count", "recommended_fixes"):
        assert scoped[field] == before[field]
