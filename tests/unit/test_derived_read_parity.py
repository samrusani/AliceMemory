"""Unrestricted reads of derived rows, including the documented differences."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_brain import _artifact_domain
from tests.unit.per_project_s2_support import add_memory
from tests.unit.test_derived_domain_fence import USER


def _normalize(value, key=""):
    if key in ("duration_ms", "elapsed_ms", "latency_ms"):
        return "<duration>"
    if isinstance(value, dict):
        return {k: _normalize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", value)
        return re.sub(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?", "<timestamp>", value)
    return value


@pytest.mark.parametrize("profile", (None, "trusted_local_agent", "admin_agent"))
@pytest.mark.parametrize("tool", ("alice_recall", "alice_context_pack", "alice_explain"))
@pytest.mark.parametrize("filtered", (False, True))
def test_unrestricted_derived_read_contract(tmp_path, monkeypatch, profile, tool, filtered):
    if filtered and tool == "alice_explain":
        pytest.skip("explain is by ID and has no domains request filter")
    path = tmp_path / "derived.sqlite3"
    bootstrap_database(path, user_id=USER, user_email="local@alice")
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        health = add_memory(store, key="health", text="Private input", domain="health")
        project = add_memory(store, key="project", text="Project input", domain="project")
        metadata = {"discovered_by": "vnext_weekly_synthesis"}
        derived = store.create_memory(
            {
                "memory_key": "synthesis",
                "canonical_text": "Synthesis fixture observation",
                "status": "active",
                "domain": "unknown",
                "sensitivity": "public",
                "metadata_json": metadata,
            }
        )
        if profile:
            _, raw = create_agent_key(store, user_id=USER, agent_id="reader", permission_profile=profile)
            monkeypatch.setenv("ALICE_AGENT_API_KEY", raw)
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=UUID(USER))
    arguments = (
        {"memory_id": str(derived["id"])} if tool == "alice_explain" else {"query": "Synthesis fixture observation"}
    )
    if filtered:
        arguments["domains"] = ["learning"]

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = cls(2026, 10, 4, 12, tzinfo=UTC)
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    for name, module in list(sys.modules.items()):
        if name.startswith("alicebot_api") and getattr(module, "datetime", None) is datetime:
            monkeypatch.setattr(module, "datetime", FrozenDateTime)
    pristine = tmp_path / "pristine.sqlite3"
    with sqlite3.connect(path) as src, sqlite3.connect(pristine) as dst:
        src.backup(dst)
    before = call_mcp_tool(context, name=tool, arguments=arguments)
    # Compare first reads so policy audit events do not accumulate.
    with sqlite3.connect(pristine) as src, sqlite3.connect(path) as dst:
        src.backup(dst)
    summary = {"memory_ids": [str(health["id"]), str(project["id"])]}
    new_domain = _artifact_domain(SimpleNamespace(domains=()), [health, project])
    assert new_domain == "health"
    with sqlite_user_connection(path, USER) as conn:
        conn.execute(
            "UPDATE memories SET domain = ?, metadata_json = ? WHERE id = ?",
            (new_domain, json.dumps({**metadata, "input_summary": summary}), str(derived["id"])),
        )
    after = call_mcp_tool(context, name=tool, arguments=arguments)
    if tool == "alice_explain":
        assert after["memory"]["metadata_json"]["input_summary"] == summary

    def contains(value):
        if isinstance(value, dict):
            return value.get("id") == str(derived["id"]) or any(contains(v) for v in value.values())
        return isinstance(value, list) and any(contains(v) for v in value)

    assert contains(before), "the old unknown-labelled derived row must actually be read"
    if filtered:
        assert not contains(after), "a repaired health summary no longer matches unrelated learning"
        return
    assert contains(after)

    def without_documented_changes(value):
        if isinstance(value, dict):
            value = {k: without_documented_changes(v) for k, v in value.items()}
            if value.get("id") == str(derived["id"]):
                if "domain" in value:
                    value["domain"] = "unknown"
                if isinstance(value.get("metadata_json"), dict):
                    value["metadata_json"].pop("input_summary", None)
            return value
        if isinstance(value, list):
            return [without_documented_changes(v) for v in value]
        return value

    if tool == "alice_context_pack":
        # Additive input metadata has an intentional serialization cost.
        for field in ("full_pack_serialized_token_estimate", "full_pack_excluded_token_estimate", "token_estimate"):
            if field in before["token_report"]:
                assert after["token_report"][field] >= before["token_report"][field]
                after["token_report"][field] = before["token_report"][field]
    if tool == "alice_explain" and profile:
        for old_event, new_event in zip(before["events"], after["events"], strict=True):
            if new_event["event_type"] == "policy.decision":
                old_policy = old_event["payload_json"]["policy_decision"]
                new_policy = new_event["payload_json"]["policy_decision"]
                for field in ("requested_domains", "effective_domains"):
                    assert old_policy[field] == ["unknown"]
                    assert new_policy[field] == ["health"]
                    new_policy[field] = old_policy[field]
                # The integrity hash changes with the recorded policy labels.
                assert new_event["integrity_hash"] != old_event["integrity_hash"]
                new_event["integrity_hash"] = old_event["integrity_hash"]
    assert _normalize(without_documented_changes(after)) == _normalize(before)
