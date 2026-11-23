"""Closest lessons on the PostgreSQL MCP server."""

from alicebot_api.mcp.types import MCPRuntimeContext
import pytest

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.support.closest_lessons_contract import ClosestLessonsContract, make_vault


@pytest.fixture(params=[False, True], ids=["words-and-paths", "vectors"])
def vectors_on(request):
    return request.param


@pytest.fixture
def closest_vault(label_harness, monkeypatch, tmp_path, vectors_on):
    context = MCPRuntimeContext(database_url=label_harness.urls["app"], user_id=label_harness.user_id)
    return make_vault(context, label_harness.store, monkeypatch, tmp_path, vector=vectors_on,
                      admin_url=label_harness.urls["admin"], http=label_harness.request)


class TestPostgresClosestLessons(ClosestLessonsContract):
    pass


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("case", [
    "commit-60", "commit-500", "review-60", "review-500",
    "review-500-cap", "ordinary-commit", "empty-review", "lessons",
])
def test_closest_lesson_wall_budget(request, tmp_path, monkeypatch, backend, case, capsys):
    """Guard: use the additive paired wall limit for each read or write shape."""
    from tests.support.closest_lessons_budget import paired_budget
    from tests.unit.test_closest_lessons import sqlite_closest_vault

    if backend == "postgres":
        h = request.getfixturevalue("label_harness")
        context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
        vault = make_vault(context, h.store, monkeypatch, tmp_path, vector=False, admin_url=h.urls["admin"])
    else:
        vault = sqlite_closest_vault(tmp_path, monkeypatch, vector=False)
    with capsys.disabled():
        paired_budget(vault, case)


def test_review_reads_while_lifecycle_locks_are_held(closest_vault, capsys):
    """Guard: the extra read must complete while the label and lifecycle locks are held."""
    from concurrent.futures import ThreadPoolExecutor
    from time import perf_counter
    from tests.support.closest_lessons_contract import commit, detail, draft, review_list

    v = closest_vault
    admin, writer = v.key("decider", None, "admin_agent"), v.key("writer")
    query = draft()
    published = commit(v, admin, query, project_scope=["alpha"])["memory"]["id"]
    waiting = commit(v, writer, query)["memory"]["id"]

    def timed(call):
        start = perf_counter()
        answer = call()
        return answer, (perf_counter() - start) * 1000

    calls = (("list", lambda: review_list(v, admin)[0]), ("detail", lambda: detail(v, admin, waiting)))
    results = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        for name, call in calls:
            before, unlocked_ms = timed(call)
            assert [x["id"] for x in before["similar"]] == [published]
            with v.store() as blocker:
                blocker.lock_graph_mutation()
                blocker.lock_label_writes(exclusive=True)
                after, locked_ms = pool.submit(timed, call).result(timeout=2)
            assert before == after
            results.append(f"{name} unlocked {unlocked_ms:.3f} ms, locked {locked_ms:.3f} ms")
    with capsys.disabled():
        print("CLOSEST_LOCK " + ", ".join(results), flush=True)


def test_http_lesson_commit_has_the_existing_response(closest_vault):
    """Guard: the HTTP commit continues to return the established receipt."""
    from tests.support.closest_lessons_contract import commit, draft

    v = closest_vault
    admin, writer = v.key("publisher", None, "admin_agent"), v.key("writer")
    query = draft()
    commit(v, admin, query, project_scope=["alpha"])
    status, answer, _ = v.http("POST", "/v0/vnext/memories/commit", key=writer,
                               payload={"lesson": query, "sensitivity": "private"})
    assert status == 201
    assert set(answer) == {"lesson", "memory", "policy_decision", "proposal_id", "receipt", "status", "write_mode"}
    assert answer["status"] == "review_required" and answer["receipt"] == "waiting in review."
