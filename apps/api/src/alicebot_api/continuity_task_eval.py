"""CI continuity-task eval: quote, last decision, and open loop.

Seeds one SQLite vault through the product write path, then scores three
named tasks. Import stays a source. Commit stays a fact. The receipt is
per-task pass/fail plus a count such as ``3/3``. This is not a
LongMemEval score and does not invent a percentage.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.project_view import ProjectView
from alicebot_api.session_briefing import compile_session_brief
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_retrieval import MEMORY_SEARCHABLE_STATUSES

QUOTE_CANARY = "The cobalt-pier-17 canary stays in the imported notebook."
DECISION_CANARY = "We decided to keep the Friday launch window for the cobalt pier."
LOOP_CANARY = "Follow up with the harbour master about the cobalt pier berth."
DEFAULT_PROJECT = "cobalt-pier"
DEFAULT_USER_EMAIL = "local@alice"

TASK_QUOTE = "quote_imported_line"
TASK_DECISION = "resume_last_decision"
TASK_LOOP = "list_open_loop"
TASK_NAMES = (TASK_QUOTE, TASK_DECISION, TASK_LOOP)

_LABEL_FACT = "fact"
_LABEL_SOURCE = "source"
_LABEL_OPEN_LOOP = "open loop"


def run_continuity_task_eval(
    *,
    data_dir: Path | str,
    user_id: UUID | str,
    effective_domains: tuple[str, ...],
    effective_sensitivity_allowed: tuple[str, ...],
    effective_project_scope: tuple[str, ...],
    quote_text: str = QUOTE_CANARY,
    decision_text: str = DECISION_CANARY,
    loop_text: str = LOOP_CANARY,
    project: str = DEFAULT_PROJECT,
    domain: str = "project",
    sensitivity: str = "public",
    seed: bool = True,
    commit_decision: bool = True,
    create_loop: bool = True,
) -> dict[str, Any]:
    """Seed a fixture vault, then score quote, last decision, and open loop.

    The three ``effective_*`` fences are required. A missing kwarg is a
    TypeError. Empty tuples are a written fence, not a silent default.
    """

    resolved_dir = Path(data_dir)
    acting_user = str(user_id)
    if seed:
        _seed_fixture(
            data_dir=resolved_dir,
            user_id=acting_user,
            quote_text=quote_text,
            decision_text=decision_text,
            loop_text=loop_text,
            project=project,
            domain=domain,
            sensitivity=sensitivity,
            commit_decision=commit_decision,
            create_loop=create_loop,
        )
    return _score_tasks(
        data_dir=resolved_dir,
        user_id=acting_user,
        effective_domains=effective_domains,
        effective_sensitivity_allowed=effective_sensitivity_allowed,
        effective_project_scope=effective_project_scope,
        quote_text=quote_text,
        decision_text=decision_text,
        loop_text=loop_text,
    )


def _seed_fixture(
    *,
    data_dir: Path,
    user_id: str,
    quote_text: str,
    decision_text: str,
    loop_text: str,
    project: str,
    domain: str,
    sensitivity: str,
    commit_decision: bool,
    create_loop: bool,
) -> None:
    context = _runtime_context(data_dir, user_id)
    capture = call_mcp_tool(
        context,
        name="alice_capture",
        arguments={
            "raw_text": f"# {project} notebook\n\nDecision: {quote_text}\n",
            "title": f"{project} notebook",
            "domain": domain,
            "sensitivity": sensitivity,
            "project_scope": [project],
        },
    )
    if capture.get("status") not in {"imported", "duplicate"}:
        raise RuntimeError(f"alice_capture did not import the quote note: {capture}")
    if commit_decision:
        committed = call_mcp_tool(
            context,
            name="alice_memory_commit",
            arguments={
                "title": f"{project} launch decision",
                "canonical_text": decision_text,
                "memory_type": "decision",
                "domain": domain,
                "sensitivity": sensitivity,
                "confidence": 0.96,
                "project_scope": [project],
                "rationale": "User said: remember this",
            },
        )
        if committed.get("status") != "committed":
            raise RuntimeError(f"alice_memory_commit did not commit the decision: {committed}")
    if create_loop:
        database = resolve_db_path(data_dir=str(data_dir), db=None)
        with sqlite_user_connection(database, user_id) as connection:
            store = SQLiteVNextStore(connection, user_id)
            store.create_open_loop(
                {
                    "title": loop_text,
                    "status": "open",
                    "domain": domain,
                    "sensitivity": sensitivity,
                    "metadata_json": {"project_scope": [project]},
                }
            )


def _score_tasks(
    *,
    data_dir: Path,
    user_id: str,
    effective_domains: tuple[str, ...],
    effective_sensitivity_allowed: tuple[str, ...],
    effective_project_scope: tuple[str, ...],
    quote_text: str,
    decision_text: str,
    loop_text: str,
) -> dict[str, Any]:
    context = _runtime_context(data_dir, user_id)
    fence_args = _tool_fence_args(
        effective_domains=effective_domains,
        effective_sensitivity_allowed=effective_sensitivity_allowed,
        effective_project_scope=effective_project_scope,
    )
    brief_quote = _compile_brief(
        data_dir,
        user_id,
        effective_domains=effective_domains,
        effective_sensitivity_allowed=effective_sensitivity_allowed,
        effective_project_scope=effective_project_scope,
        query=quote_text,
    )
    brief_open = _compile_brief(
        data_dir,
        user_id,
        effective_domains=effective_domains,
        effective_sensitivity_allowed=effective_sensitivity_allowed,
        effective_project_scope=effective_project_scope,
        query=None,
    )
    recall = call_mcp_tool(
        context,
        name="alice_recall",
        arguments={"query": quote_text, **fence_args},
    )
    resume = call_mcp_tool(context, name="alice_resume", arguments=dict(fence_args))

    quote_as_source = _text_in_labelled(brief_quote, _LABEL_SOURCE, quote_text)
    quote_as_fact = _text_in_labelled(brief_quote, _LABEL_FACT, quote_text)
    quote_in_recall_sources = _recall_sources_contain(recall, quote_text)
    quote_as_committed = _committed_memory_contains(data_dir, user_id, quote_text)
    quote_passed = (
        quote_as_source
        and quote_in_recall_sources
        and not quote_as_fact
        and not quote_as_committed
    )
    decision_passed = _last_decision_contains(resume, decision_text)
    loop_passed = _text_in_labelled(brief_open, _LABEL_OPEN_LOOP, loop_text) and _open_loops_contain(
        resume, loop_text
    )

    tasks = {
        TASK_QUOTE: {"passed": quote_passed},
        TASK_DECISION: {"passed": decision_passed},
        TASK_LOOP: {"passed": loop_passed},
    }
    passed_count = sum(1 for name in TASK_NAMES if tasks[name]["passed"])
    task_count = len(TASK_NAMES)
    return {
        "tasks": tasks,
        "passed_count": passed_count,
        "task_count": task_count,
        "score": f"{passed_count}/{task_count}",
        "brief_quote": brief_quote,
        "brief_open": brief_open,
        "recall": recall,
        "resume": resume,
    }


def _runtime_context(data_dir: Path, user_id: str) -> MCPRuntimeContext:
    acting_user = UUID(str(user_id))
    database = resolve_db_path(data_dir=str(data_dir), db=None)
    bootstrap_database(database, user_id=acting_user, user_email=DEFAULT_USER_EMAIL)
    return MCPRuntimeContext(
        database_url=sqlite_url_for_path(database),
        user_id=acting_user,
    )


def _compile_brief(
    data_dir: Path,
    user_id: str,
    *,
    effective_domains: tuple[str, ...],
    effective_sensitivity_allowed: tuple[str, ...],
    effective_project_scope: tuple[str, ...],
    query: str | None,
) -> str:
    database = resolve_db_path(data_dir=str(data_dir), db=None)
    with sqlite_user_connection(database, user_id) as connection:
        store = SQLiteVNextStore(connection, user_id)
        return compile_session_brief(
            store,
            effective_domains=effective_domains,
            effective_sensitivity_allowed=effective_sensitivity_allowed,
            effective_project_scope=effective_project_scope,
            # The eval carries explicit fences and no project view on purpose, so
            # it is not changed by a git checkout as the working folder.
            project_view=ProjectView.unscoped(),
            exclude_global_domains=frozenset(),
            query=query,
        )


def _tool_fence_args(
    *,
    effective_domains: tuple[str, ...],
    effective_sensitivity_allowed: tuple[str, ...],
    effective_project_scope: tuple[str, ...],
) -> dict[str, list[str]]:
    return {
        "domains": list(effective_domains),
        "sensitivity_allowed": list(effective_sensitivity_allowed),
        "project_scope": list(effective_project_scope),
    }


def _labelled_lines(brief: str, label: str) -> list[str]:
    prefix = f"**{label}**:"
    return [line for line in brief.splitlines() if line.startswith(prefix)]


def _text_in_labelled(brief: str, label: str, needle: str) -> bool:
    return any(needle in line for line in _labelled_lines(brief, label))


def _recall_sources_contain(recall: Mapping[str, Any], needle: str) -> bool:
    for source in recall.get("sources") or []:
        if not isinstance(source, Mapping):
            continue
        excerpt = source.get("excerpt")
        title = source.get("title")
        if needle in str(excerpt or "") or needle in str(title or ""):
            return True
    return False


def _last_decision_contains(resume: Mapping[str, Any], needle: str) -> bool:
    brief = resume.get("brief")
    last = brief.get("last_decision") if isinstance(brief, Mapping) else None
    if last is None:
        last = resume.get("last_decision")
    if not isinstance(last, Mapping):
        return False
    return needle in str(last.get("canonical_text") or "") or needle in str(last.get("title") or "")


def _open_loops_contain(resume: Mapping[str, Any], needle: str) -> bool:
    brief = resume.get("brief")
    loops = brief.get("open_loops") if isinstance(brief, Mapping) else None
    if loops is None:
        loops = resume.get("open_loops")
    if not isinstance(loops, Sequence) or isinstance(loops, (str, bytes)):
        return False
    for loop in loops:
        if not isinstance(loop, Mapping):
            continue
        if needle in str(loop.get("title") or "") or needle in str(loop.get("description") or ""):
            return True
    return False


def _committed_memory_contains(data_dir: Path, user_id: str, needle: str) -> bool:
    database = resolve_db_path(data_dir=str(data_dir), db=None)
    with sqlite_user_connection(database, user_id) as connection:
        store = SQLiteVNextStore(connection, user_id)
        rows = store.list_memories(status=None, statuses=MEMORY_SEARCHABLE_STATUSES)
    for row in rows:
        if needle in str(row.get("canonical_text") or "") or needle in str(row.get("title") or ""):
            return True
    return False


__all__ = [
    "DECISION_CANARY",
    "DEFAULT_PROJECT",
    "LOOP_CANARY",
    "QUOTE_CANARY",
    "TASK_DECISION",
    "TASK_LOOP",
    "TASK_NAMES",
    "TASK_QUOTE",
    "run_continuity_task_eval",
]
