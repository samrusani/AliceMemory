"""The list of recent memory commits holds what its caller may read, on the local SQLite store.

The service, the legacy MCP tool ``alice_vnext_recent_memory_commits`` and the command line verb read one list. A
caller with limits is shown the commits its policy and the effective labels of each commit admit, and the count is the
length of that list. The owner and an unbound admin key are shown every commit.
"""
from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_label_writes import without_insert_floor
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService

ALPHA = "prj_" + "a" * 16
COMMIT = {"agentic_memory": {"kind": "agentic_memory_commit"}}

#: name -> (text, domain, sensitivity, extra metadata, stored below the label of its input)
COMMITS = {
    "public": ("PUBLIC-COMMIT", "project", "public", {}, False),
    "private": ("PRIVATE-COMMIT", "project", "private", {}, False),
    "confidential": ("CONFIDENTIAL-COMMIT", "project", "confidential", {}, False),
    "health": ("HEALTH-COMMIT", "health", "public", {}, False),
    "alpha": ("ALPHA-COMMIT", "project", "public", {"project_scope": [ALPHA]}, False),
    "derived": ("DERIVED-COMMIT", "project", "public", {"source_id": "{source}"}, True),
}


def _seed(store) -> dict[str, dict]:
    source = store.create_source(
        {"source_type": "note", "title": "Confidential parent", "content_hash": str(uuid4()),
         "domain": "project", "sensitivity": "confidential"}
    )
    rows = {}
    # Oldest first, so the newest commit is the one built from the confidential source.
    for name in ("public", "private", "health", "alpha", "confidential", "derived"):
        text, domain, sensitivity, extra, stale = COMMITS[name]
        metadata = {**COMMIT, **{key: (str(source["id"]) if value == "{source}" else value) for key, value in extra.items()}}
        record = {
            "memory_key": f"commit-{name}", "canonical_text": text, "title": text, "status": "active",
            "domain": domain, "sensitivity": sensitivity, "metadata_json": metadata,
        }
        if stale:
            with without_insert_floor():
                rows[name] = store.create_memory(record)
        else:
            rows[name] = store.create_memory(record)
    return rows


@pytest.fixture
def vault(tmp_path, monkeypatch):
    user_id = uuid4()
    path = tmp_path / "commits.sqlite3"
    bootstrap_database(path, user_id=str(user_id), user_email="synthetic@example.invalid")
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    with sqlite_user_connection(path, user_id) as conn:
        _seed(SQLiteVNextStore(conn, user_id))

    class Vault:
        def __init__(self):
            self.path, self.user_id = path, user_id

        def store(self):
            return sqlite_user_connection(path, user_id)

        def context(self):
            return MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=user_id)

    return Vault()


def _texts(payload) -> list[str]:
    return [row["canonical_text"] for row in payload["recent_commits"]]


def _list(vault, identity, limit=20):
    with vault.store() as conn:
        return VNextMemoryCommitService(SQLiteVNextStore(conn, vault.user_id)).recent_commits(limit=limit, identity=identity)


def test_the_owner_is_shown_every_commit_newest_first(vault):
    payload = _list(vault, None)
    assert _texts(payload) == [COMMITS[name][0] for name in ("derived", "confidential", "alpha", "health", "private", "public")]
    assert payload["count"] == 6


@pytest.mark.parametrize("profile", ["trusted_local_agent", "admin_agent"])
def test_an_unbound_trusted_or_admin_key_is_shown_what_its_ceiling_allows(vault, profile):
    payload = _list(vault, AgentIdentity(agent_id="reader", permission_profile=profile, auth="agent_api_key"))
    expected = ["alpha", "health", "private", "public"]
    if profile == "admin_agent":
        expected = ["derived", "confidential", *expected]
    assert _texts(payload) == [COMMITS[name][0] for name in expected]
    assert payload["count"] == len(expected)


def test_a_commit_above_the_ceiling_or_built_from_a_hidden_input_is_left_out_and_not_counted(vault):
    trusted = AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent", auth="agent_api_key")
    payload = _list(vault, trusted)
    assert "CONFIDENTIAL-COMMIT" not in json.dumps(payload) and "DERIVED-COMMIT" not in json.dumps(payload)
    assert payload["count"] == len(payload["recent_commits"]) == 4


def test_the_list_is_refilled_from_older_commits_when_the_newest_are_hidden(vault):
    trusted = AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent", auth="agent_api_key")
    # The two newest commits are hidden from this caller; a list of one is the newest it may read.
    payload = _list(vault, trusted, limit=1)
    assert _texts(payload) == ["ALPHA-COMMIT"] and payload["count"] == 1


def test_a_read_only_key_does_not_read_private_commits_or_a_restricted_domain(vault):
    read_only = AgentIdentity(agent_id="reader", permission_profile="read_only_agent", auth="agent_api_key")
    payload = _list(vault, read_only)
    assert set(_texts(payload)) == {"PUBLIC-COMMIT", "ALPHA-COMMIT"}


def test_a_key_bound_to_a_project_is_shown_only_the_commits_inside_its_binding(vault):
    bound = AgentIdentity(
        agent_id="reader", permission_profile="trusted_local_agent", auth="agent_api_key",
        project_scope=(ALPHA,), project_scope_locked=True,
    )
    assert _texts(_list(vault, bound)) == ["ALPHA-COMMIT"]


def test_a_caller_the_policy_blocks_is_refused_and_not_shown_an_empty_list(vault):
    from alicebot_api.vnext_agent_control import AgentPolicyBlockedError

    # A proposal-only key may not read recent commits at all.
    proposal = AgentIdentity(agent_id="proposer", permission_profile="memory_proposal_agent", auth="agent_api_key")
    try:
        payload = _list(vault, proposal)
    except AgentPolicyBlockedError:
        return
    # The policy lets this profile read, so it is held to the same ceiling as the others.
    assert "CONFIDENTIAL-COMMIT" not in json.dumps(payload)


def test_a_listed_commit_loses_the_saved_quote_of_a_source_the_caller_may_not_read(vault):
    """A commit the caller may read still holds the quote it saved from a source, and the source may have been archived
    since. The owner is shown what was stored; a key is shown the commit without the quote."""

    quote = "SAVED-QUOTE-OF-AN-ARCHIVED-SOURCE"
    with vault.store() as conn:
        store = SQLiteVNextStore(conn, vault.user_id)
        source = store.create_source(
            {"source_type": "note", "title": "Archived since", "content_hash": str(uuid4()),
             "domain": "project", "sensitivity": "public"}
        )
        store.create_memory(
            {
                "memory_key": "commit-quoting", "canonical_text": "QUOTING-COMMIT", "title": "QUOTING-COMMIT",
                "status": "active", "domain": "project", "sensitivity": "public",
                "metadata_json": {
                    **COMMIT,
                    "provenance": {"source_id": str(source["id"]), "quote": quote, "evidence_role": "supports", "confidence": 0.8},
                },
            }
        )
        conn.execute("UPDATE sources SET deleted_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (str(source["id"]),))
    trusted = AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent", auth="agent_api_key")
    owner_view, key_view = _list(vault, None), _list(vault, trusted)
    assert "QUOTING-COMMIT" in _texts(owner_view) and quote in json.dumps(owner_view)
    assert "QUOTING-COMMIT" in _texts(key_view) and quote not in json.dumps(key_view)


def _mcp(vault, arguments=None):
    return call_mcp_tool(vault.context(), name="alice_vnext_recent_memory_commits", arguments=arguments or {})


def test_the_legacy_tool_holds_a_call_that_declares_a_profile_to_that_profile(vault):
    owner = _mcp(vault)
    assert len(owner["recent_commits"]) == 6 and owner["count"] == 6
    trusted = _mcp(vault, {"agent_id": "reader", "permission_profile": "trusted_local_agent"})
    assert set(_texts(trusted)) == {"ALPHA-COMMIT", "HEALTH-COMMIT", "PRIVATE-COMMIT", "PUBLIC-COMMIT"}
    assert trusted["count"] == 4
    read_only = _mcp(vault, {"agent_id": "reader", "permission_profile": "read_only_agent"})
    assert set(_texts(read_only)) == {"ALPHA-COMMIT", "PUBLIC-COMMIT"} and read_only["count"] == 2
    admin = _mcp(vault, {"agent_id": "reader", "permission_profile": "admin_agent"})
    assert admin["count"] == 6
    one = _mcp(vault, {"agent_id": "reader", "permission_profile": "trusted_local_agent", "limit": 1})
    assert _texts(one) == ["ALPHA-COMMIT"] and one["count"] == 1


def test_a_server_bound_to_a_real_key_does_not_offer_the_legacy_tool(vault, monkeypatch):
    """The legacy tools are suppressed whenever a key is configured, so a limited key cannot reach the list this way.

    A key reaches the list over HTTP, where the operator gate and the route hold it to its ceiling.
    """

    from alicebot_api.mcp_tools import MCPToolNotFoundError

    with vault.store() as conn:
        _record, raw = create_agent_key(
            SQLiteVNextStore(conn, vault.user_id), user_id=vault.user_id, agent_id="real-reader",
            permission_profile="trusted_local_agent",
        )
    monkeypatch.setenv("ALICE_AGENT_API_KEY", raw)
    with pytest.raises(MCPToolNotFoundError):
        _mcp(vault)


def test_the_command_line_verb_lists_for_the_declared_profile(vault, monkeypatch):
    import argparse
    from contextlib import contextmanager

    from alicebot_api.cli import memories as cli_memories

    @contextmanager
    def store_context(_ctx):
        with vault.store() as conn:
            yield SQLiteVNextStore(conn, vault.user_id)

    monkeypatch.setattr(cli_memories, "_vnext_store_context", store_context)

    def listed(**declared):
        args = argparse.Namespace(
            **{
                "agent_id": None, "agent_type": None, "permission_profile": None, "agent_run_id": None,
                "task_id": None, "project_scope": [], "sensitivity_allowed": None, "limit": 20, **declared,
            }
        )
        return json.loads(cli_memories._run_vnext_memory_recent(argparse.Namespace(user_id=vault.user_id), args))

    assert listed()["count"] == 6
    trusted = listed(agent_id="reader", permission_profile="trusted_local_agent")
    assert trusted["count"] == 4 and "CONFIDENTIAL-COMMIT" not in json.dumps(trusted)
    read_only = listed(agent_id="reader", permission_profile="read_only_agent")
    assert set(_texts(read_only)) == {"ALPHA-COMMIT", "PUBLIC-COMMIT"}


@pytest.mark.parametrize("limit", [0, -1])
def test_a_limit_below_one_lists_the_newest_commit_it_may_read_as_it_always_did(vault, limit):
    trusted = AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent", auth="agent_api_key")
    assert _texts(_list(vault, None, limit=limit)) == ["DERIVED-COMMIT"]
    assert _texts(_list(vault, trusted, limit=limit)) == ["ALPHA-COMMIT"]


@pytest.mark.parametrize("value", ["0", "-1", "two", "1.5"])
def test_the_command_line_verb_refuses_a_limit_that_is_not_a_positive_whole_number(value, capsys):
    from alicebot_api.cli.parser import build_parser

    with pytest.raises(SystemExit) as stopped:
        build_parser().parse_args(["vnext", "memories", "recent", "--limit", value])
    assert stopped.value.code == 2
    assert "--limit" in capsys.readouterr().err
    assert build_parser().parse_args(["vnext", "memories", "recent", "--limit", "3"]).limit == 3
    assert build_parser().parse_args(["vnext", "memories", "recent"]).limit == 20


def test_the_service_has_no_default_for_who_is_asking():
    with pytest.raises(TypeError):
        VNextMemoryCommitService(object()).recent_commits(limit=1)  # type: ignore[call-arg]


def test_a_store_without_the_bounded_query_is_scanned(vault):
    """A store that implements only ``list_memories`` is listed the same way, with the same limits."""

    class Scanning:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            if name == "list_recent_agentic_commits":
                raise AttributeError(name)
            return getattr(self.inner, name)

    with vault.store() as conn:
        store = Scanning(SQLiteVNextStore(conn, vault.user_id))
        owner = VNextMemoryCommitService(store).recent_commits(limit=20, identity=None)
        trusted = VNextMemoryCommitService(store).recent_commits(
            limit=20, identity=AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent")
        )
    assert owner["count"] == 6 and trusted["count"] == 4
    assert "CONFIDENTIAL-COMMIT" not in json.dumps(trusted)
