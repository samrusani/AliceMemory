"""Review reproductions through real SQLite readers and scoped source links."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService
from alicebot_api.vnext_source_fence import SourceReadFence
from tests.unit.per_project_s2_support import add_memory
from tests.unit.test_entity_disclosure_fence import USER, _graph, _graph_fixture


@pytest.mark.parametrize("profile", (None, "admin_agent", "read_only_agent"))
@pytest.mark.parametrize("tool", ("alice_recall", "alice_context_pack"))
def test_default_entity_counts_follow_identity(tmp_path, monkeypatch, profile, tool):
    path = tmp_path / "identity.sqlite3"
    bootstrap_database(path, user_id=USER, user_email="fixture@example.invalid")
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        for name, count in (("Meridian", 31), ("Briar", 2)):
            memory = add_memory(store, key=name, text="A neutral observation")
            entity = store.create_entity({"name": name, "entity_type": "person", "mention_count": count})
            store.create_graph_edge({"from_type": "memory", "from_id": memory["id"],
                                     "to_type": "entity", "to_id": entity["id"], "edge_type": "mentions"})
        if profile:
            _, key = create_agent_key(store, user_id=USER, agent_id="reader", permission_profile=profile)
            monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=UUID(USER))
    result = call_mcp_tool(context, name=tool, arguments={"query": "Meridian Briar", "debug": True})
    entities = result["entities"]
    if profile == "read_only_agent":
        assert all("mention_count" not in row for row in entities)
    else:
        assert [row["name"] for row in entities] == ["Meridian", "Briar"]
        assert [row["mention_count"] for row in entities] == [31, 2]


@pytest.mark.parametrize("alias", (False, True))
def test_grounding_accepts_an_entity_admitted_through_a_readable_link(tmp_path, alias):
    path = tmp_path / "grounding.sqlite3"
    bootstrap_database(path, user_id=USER, user_email="fixture@example.invalid")
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        memory = add_memory(store, key="plain", text="A neutral observation")
        canonical = "Marcus Visible" if alias else "Marcus Hidden"
        entity = store.create_entity({"name": canonical, "entity_type": "person", "aliases": ["marcus hidden"] if alias else []})
        store.create_graph_edge({"from_type": "memory", "from_id": memory["id"],
                                 "to_type": "entity", "to_id": entity["id"], "edge_type": "mentions"})
        identity = AgentIdentity(agent_id="reader", permission_profile="read_only_agent")
        pack = VNextRetrievalService(store).compile_context_pack(
            VNextRetrievalRequest(query="What do you know about Marcus Hidden today?", domains=("project",)),
            source_fence=SourceReadFence.for_identity(identity),
        )
        assert [row["name"] for row in pack["entities"]] == [canonical]
        assert "Marcus Hidden" not in pack.get("grounding", {}).get("unsupported_entities", [])


@pytest.mark.parametrize("filter_kind", ("person", "since"))
def test_source_only_entity_obeys_person_and_since_filters(filter_kind):
    store = _graph_fixture()
    store.memories = []
    store.sources = [{"id": "source", "domain": "project", "sensitivity": "private",
                      "source_created_at": "2026-10-04T00:00:00Z", "metadata_json": {"people": ["dana"]}}]
    store.edges = [{"from_type": "source", "from_id": "source", "to_type": "entity",
                    "to_id": "entity", "edge_type": "mentions"}]
    allowed, denied = ({"scope_people": ("dana",)}, {"scope_people": ("alex",)}) if filter_kind == "person" else (
        {"scope_window_start": datetime(2026, 10, 3, tzinfo=UTC)},
        {"scope_window_start": datetime(2026, 10, 5, tzinfo=UTC)},
    )
    assert _graph(store, **allowed)[2]
    assert not _graph(store, **denied)[2]


def test_fenced_entity_seeds_rank_by_readable_mentions():
    from tests.unit.test_vnext_retrieval import _entity_row, _memory_row, _mention_edge

    store = _graph_fixture()
    store.entities.append(_entity_row("briar", "Briar", mention_count=9000))
    store.edges.append(_mention_edge("memory", "briar"))
    store.memories.append(_memory_row("second", "A second readable observation", domain="project"))
    store.edges.append(_mention_edge("second", "entity"))
    assert [row["name"] for row in _graph(store, query="Meridian Briar")[2]] == ["Meridian", "Briar"]


def test_readable_source_mentions_count_even_for_memory_linked_entities():
    from tests.unit.test_vnext_retrieval import _entity_row, _mention_edge

    store = _graph_fixture()
    store.entities.append(_entity_row("briar", "Briar", mention_count=9000))
    store.edges.append(_mention_edge("memory", "briar"))
    store.sources = [{"id": "source", "domain": "project", "sensitivity": "private"}]
    store.edges.extend([
        {"from_type": "source", "from_id": "source", "to_type": "entity", "to_id": "entity", "edge_type": "mentions"},
        {"from_type": "entity", "from_id": "entity", "to_type": "source", "to_id": "source", "edge_type": "about"},
    ])
    assert [row["name"] for row in _graph(store, query="Meridian Briar")[2]] == ["Meridian", "Briar"]


@pytest.mark.parametrize("profile,bound,expected", (
    (None, False, False), ("admin_agent", False, False), ("admin_agent", True, True),
    ("trusted_local_agent", False, True), ("read_only_agent", False, True),
))
def test_entity_metadata_fence_comes_from_policy(profile, bound, expected):
    identity = None if profile is None else AgentIdentity(agent_id="reader", permission_profile=profile,
        project_scope=("alpha",) if bound else (), project_scope_locked=bound)
    fence = SourceReadFence.for_identity(identity)
    assert fence.entity_read_fenced is expected
    assert fence.fenced is (identity is not None), "saved-quote checks retain their existing meaning"


def test_recall_validity_respects_an_until_only_bound(tmp_path, monkeypatch):
    from tests.unit.test_memory_id_pointers_respect_the_read_fence import (
        _captured, _context, _recall, _result, _set, _supersede,
    )

    context = _context(tmp_path, monkeypatch)
    _source, memory = _captured(context, monkeypatch)
    replacement = _supersede(context, memory)
    _set(context, memory, status="active", valid_from="2026-09-01T00:00:00Z")
    _set(context, replacement, valid_from="2026-10-02T00:00:00Z", sensitivity="public")
    assert _result(_recall(context), memory)["validity"]["superseded_by_memory_id"] == replacement
    result = _result(_recall(context, until="2026-10-01T00:00:00Z"), memory)
    assert result["validity"]["superseded"] is True
    assert "superseded_by_memory_id" not in result["validity"]


def test_admitted_display_name_without_table_lookup():
    from alicebot_api.vnext_grounding import corpus_support
    assert corpus_support(["Marcus Hidden"], object(), allow_entity_lookup=False,
                          admitted_entity_names=("Marcus Hidden",)) == {"Marcus Hidden": True}


def test_alias_grounding_never_admits_an_unreadable_entity(tmp_path):
    from alicebot_api.vnext_grounding import corpus_support
    path = tmp_path / "aliases.sqlite3"
    bootstrap_database(path, user_id=USER, user_email="fixture@example.invalid")
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        visible = store.create_entity({"name": "Briar Visible", "entity_type": "person"})
        store.create_entity({"name": "Marcus Private", "aliases": ["marcus hidden"], "entity_type": "person"})
        support = corpus_support(["Marcus Hidden"], store, domains=("project",),
            sensitivity_allowed=("public",), allow_entity_lookup=False,
            admitted_entity_names=("Briar Visible",), admitted_entity_ids=(str(visible["id"]),))
        assert support == {"Marcus Hidden": False}
