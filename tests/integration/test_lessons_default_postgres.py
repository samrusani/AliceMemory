"""The default MCP list includes lesson reads on PostgreSQL."""

import json

import pytest

from alicebot_api.db import user_connection
from alicebot_api.mcp.registry import call_mcp_tool, list_mcp_tools
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_store import PostgresVNextStore
from alicebot_api.vnext_stores.postgres.lessons import stream_lesson_memories
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.integration.test_mcp_server import seed_user


def test_keyed_default_server_lists_and_reads_lessons(migrated_database_urls, monkeypatch):
    """Guard: take alice_lessons off the default order and its call is refused."""
    url = migrated_database_urls["app"]
    user = seed_user(url, email="fixture-lessons-default@example.invalid")
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        _, admin = create_agent_key(store, user_id=user, agent_id="admin", permission_profile="admin_agent", project_scope="alpha")
        _, reader = create_agent_key(store, user_id=user, agent_id="reader", permission_profile="read_only_agent", project_scope="alpha")
    monkeypatch.delenv("ALICE_MCP_FULL_TOOLS", raising=False)
    monkeypatch.delenv("ALICE_MCP_LEGACY_TOOLS", raising=False)
    monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
    assert [tool["name"] for tool in list_mcp_tools()] == ["alice_memory_commit", "alice_recall", "alice_resume", "alice_lessons"]
    context = MCPRuntimeContext(database_url=url, user_id=user)
    created = call_mcp_tool(context, name="alice_memory_commit", arguments={"lesson": {
        "applies_when": "editing the release notes", "what_happened": "A check failed.", "do_instead": "Review the check."
    }})
    assert created["status"] == "committed"
    monkeypatch.setenv("ALICE_AGENT_API_KEY", reader)
    answer = call_mcp_tool(context, name="alice_lessons", arguments={"task": "editing release notes"})
    assert [row["id"] for row in answer["lessons"]] == [str(created["memory"]["id"])]


def test_postgres_prefilter_keeps_every_publishing_shape(migrated_database_urls, monkeypatch):
    """Guard: drop memory_review_promote from the SQL prefilter and its lesson disappears."""
    url = migrated_database_urls["app"]
    user = seed_user(url, email="fixture-lesson-shapes@example.invalid")
    with user_connection(url, user) as conn:
        _, admin = create_agent_key(PostgresVNextStore(conn), user_id=user, agent_id="admin",
                                    permission_profile="admin_agent", project_scope="alpha")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    context = MCPRuntimeContext(database_url=url, user_id=user)
    block = {"applies_when": "rebasing a branch", "what_happened": "A check failed.", "do_instead": "Review the check."}
    shapes = [("agentic_memory_commit", "created"), ("agentic_memory_confirm_confirm", "promoted"),
              ("memory_correct_approve", "promoted"), ("memory_review_accept", "promoted"),
              ("memory_review_promote", "promoted")]
    expected = set()
    for index, (action, revision_type) in enumerate(shapes):
        result = call_mcp_tool(context, name="alice_memory_commit", arguments={
            "memory_type": "procedure", "title": f"rebasing {index}", "canonical_text": f"rebasing branch {index}",
        })
        mid = str(result["memory"]["id"])
        expected.add(mid)
        with user_connection(url, user) as conn:
            store = PostgresVNextStore(conn)
            row = store.get_memory(mid)
            value = dict(row["value"])
            value["lesson"] = block
            store.update_memory(memory_id=mid, patch={"value": value})
            recorded = dict(reversed(list(block.items())))
            store.append_revision({"memory_id": mid, "memory_key": row["memory_key"],
                                   "new_value": {"lesson": json.dumps(recorded) if index == 0 else recorded},
                                   "action": action, "revision_type": revision_type, "actor_type": "user"})
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        candidates = list(stream_lesson_memories(store, domains=[], sensitivity=None, projects=("alpha",), lazy_metadata=False))
    answer = call_mcp_tool(context, name="alice_lessons", arguments={"task": "rebasing", "limit": 10})
    assert {str(row["id"]) for row in candidates} == expected == {str(row["id"]) for row in answer["lessons"]}


@pytest.fixture
def outcome_vault(label_harness, monkeypatch, tmp_path):
    from tests.integration.derived_labels_postgres_support import LabelHarness
    from tests.support.lesson_outcomes_contract import OutcomeVault

    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    def outcome(h, *, sibling=None):
        context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
        return OutcomeVault(context, h.store, monkeypatch, admin_url=h.urls["admin"], http=h.request, sibling=sibling)

    def sibling():
        urls = label_harness.urls
        user = seed_user(urls["app"], email="fixture-lessons-larger@example.invalid")
        return outcome(LabelHarness(urls, user))

    return outcome(label_harness, sibling=sibling)


def test_postgres_lesson_outcomes_follow_the_writer_key(outcome_vault):
    """Guard: a keyholder who wrote a lesson receives the three outcome counts."""
    from tests.support.lesson_outcomes_contract import check_writer_outcomes

    check_writer_outcomes(outcome_vault)


def test_postgres_lesson_outcomes_withhold_default_and_flagged_reasons(outcome_vault):
    """Guard: a writer's newest unpublished decision exposes only a shareable reason."""
    from tests.support.lesson_outcomes_contract import HTTP_REASONS, MCP_REASONS, check_default_and_withheld_reasons

    check_default_and_withheld_reasons(outcome_vault, (*MCP_REASONS, *HTTP_REASONS))


def test_postgres_lesson_outcomes_use_the_revision_cutoff(outcome_vault):
    """Guard: the timestamp prefilter leaves room for earlier row timestamps."""
    from tests.support.lesson_outcomes_contract import check_outcome_cutoff

    check_outcome_cutoff(outcome_vault)


def test_postgres_lesson_outcomes_preserve_read_bookkeeping(outcome_vault):
    """Guard: the existing lesson request event describes only the published results."""
    from tests.support.lesson_outcomes_contract import check_read_bookkeeping

    check_read_bookkeeping(outcome_vault)


@pytest.mark.parametrize("case", [
    "peer waiting", "own waiting", "own recent rejects", "own old rejects",
    "own recent publications", "same writer outside project",
])
def test_postgres_lesson_outcomes_read_budget(outcome_vault, case):
    """Guard: the indexed selection admits the writer's own waiting and timed rows."""
    from tests.support.lesson_outcomes_contract import check_outcome_growth

    check_outcome_growth(outcome_vault, case)
