"""Restore, promotion and settled-label regressions from the security review."""

from __future__ import annotations

import json
import sqlite3

import pytest

from alicebot_api.onramp import bootstrap_database, main as onramp_main
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_derived_domain_backfill import plan_relabels
from tests.unit.per_project_s2_support import add_memory
from tests.unit.test_derived_domain_fence import USER


@pytest.mark.parametrize("existing_destination", (False, True))
def test_restore_repairs_derived_rows_before_publication(tmp_path, monkeypatch, existing_destination):
    from alicebot_api import sqlite_schema

    source = tmp_path / "old.sqlite3"
    destination = tmp_path / "restored.sqlite3"
    backup = tmp_path / "backup.jsonl"
    # Build the old backup without the new repair, as a previous release did.
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_schema, "_relabel_derived_domains", lambda conn: None)
        bootstrap_database(source, user_id=USER, user_email="local@alice")
        with sqlite_user_connection(source, USER) as conn:
            store = SQLiteVNextStore(conn, USER)
            health = add_memory(store, key="health", text="A restricted observation", domain="health")
            derived = store.create_memory(
                {
                    "memory_key": "derived",
                    "canonical_text": "A restricted summary",
                    "status": "active",
                    "domain": "unknown",
                    "sensitivity": "public",
                    "metadata_json": {"consolidation": {"cluster_member_ids": [health["id"]]}},
                }
            )
        assert onramp_main(["export", "--db", str(source), "--user-id", USER, "--out", str(backup)]) == 0
    if existing_destination:
        bootstrap_database(destination, user_id=USER, user_email="local@alice")
    argv = ["import", "--in", str(backup), "--db", str(destination), "--user-id", USER]
    assert onramp_main(argv) == 0
    # Do not open through bootstrap here: publication itself must be safe.
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT domain FROM memories WHERE id = ?", (derived["id"],)).fetchone()[0] == "health"
    from uuid import UUID
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp.types import MCPRuntimeContext
    from alicebot_api.onramp import sqlite_url_for_path
    from alicebot_api.vnext_agent_keys import create_agent_key

    with sqlite_user_connection(destination, USER) as conn:
        _, raw = create_agent_key(
            SQLiteVNextStore(conn, USER), user_id=USER, agent_id="restore-reader", permission_profile="read_only_agent"
        )
    monkeypatch.setenv("ALICE_AGENT_API_KEY", raw)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    from alicebot_api.mcp_tools import MCPToolError

    with pytest.raises(MCPToolError, match="requested explanation is unavailable"):
        call_mcp_tool(
            MCPRuntimeContext(database_url=sqlite_url_for_path(destination), user_id=UUID(USER)),
            name="alice_explain",
            arguments={"memory_id": str(derived["id"])},
        )
    assert onramp_main([*argv, "--mode", "skip"]) == 0
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT domain FROM memories WHERE id = ?", (derived["id"],)).fetchone()[0] == "health"
    # A logged label repair may reconcile only that label on a repeated import.
    with sqlite3.connect(destination) as conn:
        conn.execute("UPDATE memories SET canonical_text = ? WHERE id = ?", ("Changed local content", derived["id"]))
    assert onramp_main([*argv, "--mode", "skip"]) != 0
    with sqlite3.connect(destination) as conn:
        assert (
            conn.execute("SELECT canonical_text FROM memories WHERE id = ?", (derived["id"],)).fetchone()[0]
            == "Changed local content"
        )


@pytest.mark.parametrize("downstream", ("a", "z"))
def test_repair_uses_settled_input_labels_independently_of_id_order(downstream):
    def row(identifier, domain="unknown", **metadata):
        return {"id": identifier, "user_id": "u", "domain": domain, "metadata_json": metadata}

    tables = {
        "sources": [row("health", "health"), row("legal", "legal")],
        "generated_artifacts": [
            row(downstream, input_summary={"source_ids": ["health"], "artifact_ids": ["b", "c"]}),
            row("b", input_summary={"source_ids": ["legal"]}),
            row("c", input_summary={"source_ids": ["legal"]}),
        ],
    }
    updates = plan_relabels(tables)
    assert {item[2]: item[3] for item in updates}[downstream] == "legal"
    assert len(updates) == len({item[:3] for item in updates})


@pytest.mark.parametrize("reference", ("value", "metadata"))
def test_promoted_artifact_memory_follows_repaired_artifact(reference):
    memory = {"id": "promoted", "user_id": "u", "domain": "unknown", "value": {"kind": "promoted_artifact"}}
    if reference == "value":
        memory["value"]["artifact_id"] = "report"
    else:
        memory["metadata_json"] = {"source_artifact_id": "report"}
    tables = {
        "sources": [{"id": "source", "user_id": "u", "domain": "health"}],
        "generated_artifacts": [
            {
                "id": "report",
                "user_id": "u",
                "domain": "unknown",
                "metadata_json": {"input_summary": {"source_ids": ["source"]}},
            }
        ],
        "memories": [memory],
    }
    assert {item[2]: item[3] for item in plan_relabels(tables)} == {"report": "health", "promoted": "health"}


def test_repair_records_changed_rows_once(tmp_path):
    from alicebot_api.vnext_derived_domain_backfill import relabel_sqlite

    path = tmp_path / "audit.sqlite3"
    bootstrap_database(path, user_id=USER, user_email="local@alice")
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        source = add_memory(store, key="health", text="Private observation", domain="health")
        derived = store.create_memory(
            {
                "memory_key": "derived",
                "canonical_text": "Private summary",
                "domain": "unknown",
                "metadata_json": {"candidate_kind": "memory_rollup", "memory_ids": [source["id"]]},
            }
        )
        conn.execute("DELETE FROM alice_schema_state WHERE key LIKE 'derived_restricted_domains_%'")
        relabel_sqlite(conn)
        relabel_sqlite(conn)
        rows = conn.execute(
            "SELECT target_id, payload_json FROM event_log WHERE event_type = 'memory.domain_relabelled'"
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert str(row["target_id"]) == str(derived["id"])
        assert json.loads(row["payload_json"])["domain"] == "health"


def test_staleness_report_keeps_title_sensitivity(monkeypatch):
    from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
    from alicebot_api.vnext_scheduler import SchedulerRunRequest, VNextSchedulerService
    from tests.unit.test_vnext_scheduler import _staleness_store

    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    store = _staleness_store()
    store.memories[0]["sensitivity"] = "highly_sensitive"
    store.memories[0]["title"] = "Private fixture title"
    result = VNextSchedulerService(store).run_now(
        SchedulerRunRequest(
            workflow_type="staleness_sweep",
            sensitivity_allowed=ALL_SENSITIVITY,
            generated_for="2026-07-04",
            options={"reference_time": "2026-07-04T03:30:00Z"},
        )
    )
    assert "Private fixture title" in result["artifact"]["content_markdown"]
    assert result["artifact"]["sensitivity"] == "highly_sensitive"


def test_nonsettling_cycles_are_bounded_and_not_published(monkeypatch):
    from alicebot_api import vnext_derived_domain_backfill as repair

    selector = repair.derived_domain
    calls = 0

    def counted_selector(rows, *, fallback):
        nonlocal calls
        calls += 1
        assert calls < 100, "repair exceeded the bounded change budget"
        return selector(rows, fallback=fallback)

    monkeypatch.setattr(repair, "derived_domain", counted_selector)
    # Each row copies the next. Revisiting this inconsistent odd cycle
    # oscillates unless the repair detects its bounded-work limit.
    tables = {
        "generated_artifacts": [
            {
                "id": key,
                "user_id": "u",
                "domain": domain,
                "metadata_json": {"input_summary": {"artifact_ids": [parent]}},
            }
            for key, parent, domain in (("a", "b", "health"), ("b", "c", "legal"), ("c", "a", "health"))
        ]
    }
    with pytest.raises(ValueError, match="did not settle"):
        repair.plan_relabels(tables)
    assert [row["domain"] for row in tables["generated_artifacts"]] == ["health", "legal", "health"]


def test_sqlite_repair_follows_available_artifact_and_leaves_missing_inputs(tmp_path):
    from alicebot_api.vnext_derived_domain_backfill import relabel_sqlite

    path = tmp_path / "promoted.sqlite3"
    bootstrap_database(path, user_id=USER, user_email="local@alice")
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        # The local product does not store artifacts. Imported references to
        # absent artifacts are not guessed from copied text.
        missing = store.create_memory(
            {
                "memory_key": "missing",
                "canonical_text": "Private summary",
                "domain": "unknown",
                "value": {"kind": "promoted_artifact", "artifact_id": "absent"},
            }
        )
        relabel_sqlite(conn, restoring=True)
        assert store.get_memory(str(missing["id"]))["domain"] == "unknown"
        # Exercise the shared SQLite repair on a synthetic artifact-bearing schema.
        conn.execute("CREATE TABLE generated_artifacts (id TEXT, user_id TEXT, domain TEXT, metadata_json TEXT)")
        health = add_memory(store, key="input", text="Private observation", domain="health")
        conn.execute(
            "INSERT INTO generated_artifacts VALUES (?, ?, ?, ?)",
            ("report", USER, "unknown", json.dumps({"input_summary": {"memory_ids": [str(health["id"])]}})),
        )
        copied = store.create_memory(
            {
                "memory_key": "copy",
                "canonical_text": "Private summary",
                "domain": "unknown",
                "value": {"kind": "promoted_artifact", "artifact_id": "report"},
            }
        )
        relabel_sqlite(conn, restoring=True)
        assert store.get_memory(str(copied["id"]))["domain"] == "health"
        assert conn.execute("SELECT domain FROM generated_artifacts").fetchone()["domain"] == "health"


@pytest.mark.parametrize("reference", ("value", "metadata"))
def test_promoted_artifact_uuid_aliases_resolve(reference):
    from uuid import UUID
    identifier = "aaaaaaaa-1234-5678-9000-000000000001"
    memory = {"id": "copy", "user_id": "u", "domain": "unknown",
              "value": {"kind": "promoted_artifact"}}
    if reference == "value":
        memory["value"]["artifact_id"] = UUID(identifier).hex.upper()
    else:
        memory["metadata_json"] = {"source_artifact_id": UUID(identifier).hex.upper()}
    tables = {"generated_artifacts": [{"id": identifier, "user_id": "u", "domain": "health"}],
              "memories": [memory]}
    assert plan_relabels(tables) == [("memories", "u", "copy", "health")]
