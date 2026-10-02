"""Continuity-task eval: 3/3 on the fixture vault, named mutations fail.

Each test names the edit that makes it fail. HOME and data_dir stay on
tmp_path. Import stays a source. Commit stays a fact.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from alicebot_api.continuity_task_eval import (
    DECISION_CANARY,
    LOOP_CANARY,
    QUOTE_CANARY,
    TASK_DECISION,
    TASK_LOOP,
    TASK_NAMES,
    TASK_QUOTE,
    run_continuity_task_eval,
)
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext
from alicebot_api.onramp import resolve_db_path, sqlite_url_for_path
from alicebot_api.session_briefing import compile_session_brief
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MODEL_ENV,
)
from alicebot_api.project_view import ProjectView

USER_ID = "00000000-0000-0000-0000-000000000001"

PROJECT_A = "cobalt-pier"
PROJECT_B = "amber-quay"

QUOTE_B = "The amber-quay-88 canary stays in the imported notebook."
DECISION_B = "We decided to delay the Tuesday harbour inspection for the amber quay."
LOOP_B = "Ask the lock keeper about the amber quay mooring."
SHARED_SOURCE_QUERY = "canary stays in the imported notebook"

UNSCOPED_FENCES = {
    "effective_domains": (),
    "effective_sensitivity_allowed": ("public", "internal", "private", "unknown"),
    "effective_project_scope": (),
}


def _prepare(tmp_path: Path, monkeypatch) -> None:
    for env_name in (
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
        EMBEDDINGS_API_KEY_ENV,
        AGENT_API_KEY_ENV,
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _run(tmp_path: Path, **kwargs):
    fences = dict(UNSCOPED_FENCES)
    extra = {key: value for key, value in kwargs.items() if key not in fences}
    fences.update({key: value for key, value in kwargs.items() if key in fences})
    return run_continuity_task_eval(
        data_dir=tmp_path,
        user_id=USER_ID,
        **fences,
        **extra,
    )


def _compact_recall_contains_quote(recall: dict, needle: str = QUOTE_CANARY) -> bool:
    """True when compact alice_recall results[] expose the imported quote.

    Compact recall uses ``text`` (and ``status`` when present), not
    ``canonical_text`` / ``title``. Named mutation: promote the import
    to ``active``; this must become True.
    """

    for row in recall.get("results") or []:
        if not isinstance(row, dict):
            continue
        if needle not in str(row.get("text") or ""):
            continue
        status = row.get("status")
        if status is None or status in {"active", "accepted"}:
            return True
    return False


def test_full_run_on_the_fixture_vault_is_three_of_three(tmp_path: Path, monkeypatch) -> None:
    """A seeded vault must score 3/3 with each task named.

    Fails if capture, commit, or create_open_loop is skipped, or if the
    scorer drops a named task.
    """

    _prepare(tmp_path, monkeypatch)
    report = _run(tmp_path)

    assert report["score"] == "3/3"
    assert report["passed_count"] == 3
    assert report["task_count"] == 3
    assert tuple(report["tasks"]) == TASK_NAMES
    for name in TASK_NAMES:
        assert report["tasks"][name]["passed"] is True, report
    assert QUOTE_CANARY in report["brief_quote"]
    assert DECISION_CANARY in str((report["resume"].get("brief") or {}).get("last_decision") or "")
    assert LOOP_CANARY in report["brief_open"]


def test_quote_imported_line_fails_when_source_excerpts_are_skipped(
    tmp_path: Path, monkeypatch
) -> None:
    """Named mutation: skip ``search_source_excerpts``.

    Compile the brief without source excerpts. Task ``quote_imported_line``
    must fail. Fails if the scorer treats a fact-only brief as a quote hit.
    """

    from alicebot_api.vnext_retrieval import VNextRetrievalService

    _prepare(tmp_path, monkeypatch)

    def _no_excerpts(self, **_kwargs):
        return [], "skipped"

    monkeypatch.setattr(VNextRetrievalService, "search_source_excerpts", _no_excerpts)
    report = _run(tmp_path)

    assert report["tasks"][TASK_QUOTE]["passed"] is False, report
    assert report["score"] == "2/3"
    assert report["tasks"][TASK_DECISION]["passed"] is True
    assert report["tasks"][TASK_LOOP]["passed"] is True


def test_resume_last_decision_fails_when_commit_is_skipped(tmp_path: Path, monkeypatch) -> None:
    """Named mutation: skip ``alice_memory_commit``.

    Task ``resume_last_decision`` must fail. Fails if last_decision is
    invented from the captured candidate.
    """

    _prepare(tmp_path, monkeypatch)
    report = _run(tmp_path, commit_decision=False)

    assert report["tasks"][TASK_DECISION]["passed"] is False, report
    assert report["score"] == "2/3"
    assert report["tasks"][TASK_QUOTE]["passed"] is True
    assert report["tasks"][TASK_LOOP]["passed"] is True


def test_list_open_loop_fails_when_create_open_loop_is_skipped(
    tmp_path: Path, monkeypatch
) -> None:
    """Named mutation: skip ``create_open_loop``.

    Task ``list_open_loop`` must fail. Fails if the scorer treats the
    decision title as an open loop.
    """

    _prepare(tmp_path, monkeypatch)
    report = _run(tmp_path, create_loop=False)

    assert report["tasks"][TASK_LOOP]["passed"] is False, report
    assert report["score"] == "2/3"
    assert report["tasks"][TASK_QUOTE]["passed"] is True
    assert report["tasks"][TASK_DECISION]["passed"] is True


def test_imported_quote_is_still_not_a_searchable_memory(tmp_path: Path, monkeypatch) -> None:
    """After a passing run the imported quote stays a source.

    ``alice_recall`` of the quote canary has memory ``count=1`` because
    FTS ranks the committed decision on shared cobalt-pier tokens. That
    is allowed. The quote itself is not a ``**fact**`` and must not
    appear as compact recall ``text``. A source row exists.

    Named mutation: promote the import to ``active``. The recall-results
    ``text`` assert goes red on its own. ``create_memory(...,
    status="candidate")`` instead of capture leaves no source.
    """

    _prepare(tmp_path, monkeypatch)
    report = _run(tmp_path)
    assert report["score"] == "3/3", report

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)
    recall = call_mcp_tool(context, name="alice_recall", arguments={"query": QUOTE_CANARY})
    # count may be 1 (the decision). The imported line must not appear as
    # compact recall text or as a **fact**.
    assert not _compact_recall_contains_quote(recall), recall
    assert any(
        QUOTE_CANARY in str((source or {}).get("excerpt") or "")
        for source in (recall.get("sources") or [])
    ), recall
    assert not any(
        line.startswith("**fact**:") and QUOTE_CANARY in line
        for line in report["brief_quote"].splitlines()
    ), report["brief_quote"]

    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        candidates = store.list_memories(status="candidate")
        committed = store.list_memories(status=None, statuses=("active", "accepted"))
        sources = store.list_events(target_type="source")
    assert candidates, "capture created no candidate; the no-promote assert is vacuous"
    assert all(row.get("status") == "candidate" for row in candidates)
    assert any(QUOTE_CANARY in str(row.get("canonical_text") or "") for row in candidates)
    assert not any(QUOTE_CANARY in str(row.get("canonical_text") or "") for row in committed)
    assert sources, "capture created no source; create_memory-only would still pass"


def test_scoped_read_of_project_a_does_not_see_project_b(tmp_path: Path, monkeypatch) -> None:
    """Fence: scoped project A must not quote, resume, or list project B.

    Unscoped reads must still see B, or the scoped asserts are vacuous.
    Fails if a read drops ``effective_project_scope``. The three fence
    kwargs have no default.
    """

    parameters = inspect.signature(run_continuity_task_eval).parameters
    for name in (
        "effective_domains",
        "effective_sensitivity_allowed",
        "effective_project_scope",
    ):
        assert parameters[name].default is inspect.Parameter.empty, (
            f"run_continuity_task_eval.{name} gained a default. A default of () "
            "means no fence for any caller that forgets it."
        )
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY

    _prepare(tmp_path, monkeypatch)
    seeded_a = _run(tmp_path, project=PROJECT_A)
    assert seeded_a["score"] == "3/3", seeded_a
    seeded_b = _run(
        tmp_path,
        project=PROJECT_B,
        quote_text=QUOTE_B,
        decision_text=DECISION_B,
        loop_text=LOOP_B,
        domain="personal",
        sensitivity="private",
    )
    assert seeded_b["score"] == "3/3", seeded_b

    unscoped_b = _run(
        tmp_path,
        seed=False,
        quote_text=QUOTE_B,
        decision_text=DECISION_B,
        loop_text=LOOP_B,
    )
    assert unscoped_b["score"] == "3/3", unscoped_b
    assert QUOTE_B in unscoped_b["brief_quote"]
    assert DECISION_B in str(
        (unscoped_b["resume"].get("brief") or {}).get("last_decision") or ""
    )
    assert LOOP_B in unscoped_b["brief_open"]

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        unscoped_shared = compile_session_brief(
            store,
            **UNSCOPED_FENCES,
            query=SHARED_SOURCE_QUERY,
            project_view=ProjectView.unscoped(),
            exclude_global_domains=frozenset(),
        )
        scoped_shared = compile_session_brief(
            store,
            effective_domains=(),
            effective_sensitivity_allowed=UNSCOPED_FENCES["effective_sensitivity_allowed"],
            effective_project_scope=(PROJECT_A,),
            query=SHARED_SOURCE_QUERY,
            project_view=ProjectView.unscoped(),
            exclude_global_domains=frozenset(),
        )
    assert QUOTE_B in unscoped_shared, "unscoped brief lost B's source; scoped asserts are vacuous"
    assert QUOTE_B not in scoped_shared
    assert QUOTE_CANARY in scoped_shared

    scoped_a = _run(
        tmp_path,
        seed=False,
        project=PROJECT_A,
        quote_text=QUOTE_CANARY,
        decision_text=DECISION_CANARY,
        loop_text=LOOP_CANARY,
        effective_project_scope=(PROJECT_A,),
    )
    assert scoped_a["score"] == "3/3", scoped_a
    assert QUOTE_B not in scoped_a["brief_quote"]
    assert QUOTE_B not in scoped_a["brief_open"]
    assert DECISION_B not in str((scoped_a["resume"].get("brief") or {}).get("last_decision") or "")
    assert LOOP_B not in scoped_a["brief_open"]
    open_loops = (scoped_a["resume"].get("brief") or {}).get("open_loops") or []
    assert not any(LOOP_B in str((loop or {}).get("title") or "") for loop in open_loops)
    recall_sources = scoped_a["recall"].get("sources") or []
    assert not any(QUOTE_B in str((source or {}).get("excerpt") or "") for source in recall_sources)
