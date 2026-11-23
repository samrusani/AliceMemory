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


def test_postgres_lesson_outcomes_respect_the_key_ceiling(outcome_vault):
    """Guard: the key's ceiling applies even when the agent id matches the writer."""
    from tests.support.lesson_outcomes_contract import check_outcome_ceiling

    check_outcome_ceiling(outcome_vault)


def test_postgres_lesson_outcomes_ignore_ordinary_memories(outcome_vault):
    """Guard: an outcome requires a lesson block."""
    from tests.support.lesson_outcomes_contract import check_ordinary_memories_are_not_outcomes

    check_ordinary_memories_are_not_outcomes(outcome_vault)


def test_postgres_lesson_outcomes_withhold_the_consolidation_reason(outcome_vault):
    """Guard: the writer receives the supersession without its generated reason."""
    from tests.support.lesson_outcomes_contract import check_consolidation_outcome_reason

    check_consolidation_outcome_reason(outcome_vault)


def _seed_outcome_waiting_rows(vault, writer, start, stop, *, project="alpha"):
    from psycopg.types.json import Jsonb

    from tests.support.lesson_outcomes_contract import BLOCK

    with vault.store() as store:
        store.conn.execute(
            """
            INSERT INTO memories (user_id, memory_key, value, status, source_event_ids,
                                  memory_type, canonical_text, domain, sensitivity,
                                  created_by_agent_id, project_id, metadata_json)
            SELECT app.current_user_id(), 'outcome-budget.' || n, %s, 'candidate', '[]',
                   'procedure', %s, 'project', 'private', %s, %s, %s
            FROM generate_series(%s::integer, %s::integer) AS n
            """,
            (Jsonb({"lesson": BLOCK}), BLOCK["applies_when"], writer, project,
             Jsonb({"project_scope": [project]}), start, stop),
        )


def _analyze_outcome_rows(vault):
    import psycopg

    with psycopg.connect(vault.admin_url, autocommit=True) as conn:
        conn.execute("ANALYZE memories, memory_revisions")


def _outcome_jit_settings(store):
    return store.conn.execute(
        "SELECT current_setting('jit') AS jit, current_setting('jit_above_cost')::float8 AS cost, "
        "pg_jit_available() AS available, current_setting('server_version') AS version"
    ).fetchone()


def test_postgres_lesson_outcome_statements_stay_below_jit_cost(outcome_vault, monkeypatch, capsys):
    """Guard: the planner must price every outcome statement below the JIT threshold."""
    from datetime import UTC, datetime

    import psycopg

    from alicebot_api.mcp import lessons
    from tests.support.lesson_outcomes_contract import seed_record

    vault = outcome_vault
    writer = "plan-writer"
    bound = vault.key(writer, profile="project_scoped_agent")
    unbound = vault.key(writer, None, "admin_agent")
    _seed_outcome_waiting_rows(vault, "other-writer", 1, 1997)
    _seed_outcome_waiting_rows(vault, writer, 1998, 1998)
    now = datetime.now(UTC)
    with vault.store() as store:
        seed_record(vault, store, writer=writer, status="active", updated_at=now, decision_at=now,
                    action="memory_correct_approve", kind="promoted")
        seed_record(vault, store, writer=writer, status="rejected", updated_at=now, decision_at=now,
                    action="memory_correct_reject", kind="rejected")
    _analyze_outcome_rows(vault)
    original = lessons._lesson_outcomes
    execute = psycopg.Cursor.execute
    statements = []

    def record(cursor, query, params=None, **kwargs):
        statements.append((query, params))
        return execute(cursor, query, params, **kwargs)

    def track(*args, **kwargs):
        with monkeypatch.context() as capture:
            capture.setattr(psycopg.Cursor, "execute", record)
            return original(*args, **kwargs)

    monkeypatch.setattr(lessons, "_lesson_outcomes", track)
    for binding, key in (("bound", bound), ("unbound", unbound)):
        statements.clear()
        outcomes = vault.read(key)["yours"]
        assert (outcomes["waiting"], outcomes["published_in_last_7_days"], outcomes["not_published_in_last_7_days"]) == (1, 1, 1)
        assert any("FROM memories " in query for query, _ in statements)
        assert any("JOIN memory_revisions " in query for query, _ in statements)
        with vault.store() as store:
            runtime = _outcome_jit_settings(store)
            # -1 explicitly disables JIT. Apply the default threshold there too.
            threshold = runtime["cost"] if runtime["cost"] >= 0 else 100_000
            plans = [
                store.conn.execute("EXPLAIN (FORMAT JSON) " + query, params).fetchone()["QUERY PLAN"][0]
                for query, params in statements
            ]
        costs = [plan["Plan"]["Total Cost"] for plan in plans]
        with capsys.disabled():
            print(f"Lesson outcomes {binding}, PostgreSQL {runtime['version']}: "
                  f"jit={runtime['jit']}, pg_jit_available={runtime['available']}, "
                  f"jit_above_cost={runtime['cost']}, statement costs={costs}", flush=True)
        assert all(cost < threshold for cost in costs), (binding, threshold, costs)


_LESSON_TIMING_WORKER = """
import json
import os
import sys
from time import perf_counter
from uuid import UUID

from alicebot_api.mcp.types import MCPRuntimeContext
from tests.unit.test_identity_claim_codes import wire

database, user, key, arguments = json.loads(sys.stdin.readline())
os.environ["ALICE_AGENT_API_KEY"] = key
context = MCPRuntimeContext(database_url=database, user_id=UUID(user))
for line in sys.stdin:
    start = perf_counter()
    failed, response = wire(context, "alice_lessons", arguments)
    elapsed = perf_counter() - start
    print(json.dumps([failed, response, elapsed]), flush=True)
"""


def _lesson_timing_checkout(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    from zipfile import ZipFile

    if configured := os.getenv("ALICE_READ_MERGE_BASE_CHECKOUT"):
        return Path(configured)
    checkout = tmp_path / "main"
    archive = tmp_path / "main.zip"
    repository = Path(os.environ.get("BENCH_TEST_SOURCE", Path(__file__).resolve().parents[2]))
    with archive.open("wb") as output:
        subprocess.run(["git", "archive", "--format=zip", "d75ee2a1"], cwd=repository, stdout=output, check=True)
    with ZipFile(archive) as source:
        source.extractall(checkout)
    return checkout


def _lesson_timing_process(checkout, repository):
    import os
    import subprocess
    import sys

    return subprocess.Popen(
        [sys.executable, "-c", _LESSON_TIMING_WORKER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(checkout / "apps/api/src"), str(repository)))},
    )


def test_postgres_no_writer_outcomes_meet_jit_latency_budget(outcome_vault, tmp_path, capsys):
    """Guard: bound keys with no lessons have a small additive cost even when JIT is available."""
    from pathlib import Path
    from statistics import median

    import psycopg
    from psycopg.rows import dict_row

    from tests.support.lesson_outcomes_contract import QUERY

    vault = outcome_vault
    with psycopg.connect(vault.context.database_url, row_factory=dict_row) as conn:
        runtime = _outcome_jit_settings(PostgresVNextStore(conn))
    if runtime["jit"] != "on" or not runtime["available"]:
        pytest.skip("The timing budget requires a PostgreSQL default of jit=on and a JIT provider")
    reader = vault.key("no-lesson-writer", profile="project_scoped_agent")
    repository = Path(__file__).resolve().parents[2]
    baseline = _lesson_timing_checkout(tmp_path)
    with _lesson_timing_process(baseline, repository) as main, _lesson_timing_process(repository, repository) as head:
        try:
            for process in (main, head):
                process.stdin.write(json.dumps([vault.context.database_url, str(vault.context.user_id), reader, QUERY]) + "\n")
                process.stdin.flush()
            start = 1
            for size, slack in ((1_000, 0.005), (10_000, 0.015)):
                _seed_outcome_waiting_rows(vault, "other-writer", start, size)
                _analyze_outcome_rows(vault)
                start = size + 1
                samples = {main: [], head: []}
                for sample in range(16):
                    answers = {}
                    pair = (main, head) if sample % 2 == 0 else (head, main)
                    for process in pair:
                        process.stdin.write("measure\n")
                        process.stdin.flush()
                        failed, answer, elapsed = json.loads(process.stdout.readline())
                        assert not failed, answer
                        answers[process] = answer
                        if sample:
                            samples[process].append(elapsed)
                    assert answers[head] == answers[main]
                    assert "yours" not in answers[head]
                main_ms, head_ms = (median(samples[p]) * 1000 for p in (main, head))
                with capsys.disabled():
                    print(f"Lesson JIT timing at {size}: main {main_ms:.3f} ms, head {head_ms:.3f} ms, "
                          f"limit +{slack * 1000:g} ms", flush=True)
                assert head_ms - main_ms <= slack * 1000
        finally:
            for process in (main, head):
                process.stdin.close()
                assert process.wait(timeout=15) == 0
