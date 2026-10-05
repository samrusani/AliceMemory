"""Every exact reader is classified, and each exact door calls the guard."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "apps/api/src"

READS = {
    "get_memory",
    "get_memory_for_update",
    "get_memories_by_ids",
    "list_memories",
    "list_memories_by_statuses",
    "search_memories",
    "search_memories_fts",
    "search_memories_vector",
    "search_memories_by_time",
    "list_open_loops",
    "get_open_loop",
    "list_artifacts",
    "get_artifact",
    "get_artifact_for_update",
    "list_artifacts_referencing_source",
    "list_memories_referencing_source",
    "list_memories_referencing_sources",
    "list_beliefs",
    "get_belief",
    "list_projects",
    "get_project",
    "get_project_for_update",
}

GUARD_CALLS = {
    "LabelGuard",
    "effective_row",
    "effective_row_for_fence",
    "admit_rows",
    "admit_beliefs",
    "policy_labels",
}

# function -> helper that holds the guard call, or None when the function calls it
DOORS = {
    "routers/_vnext_shared.py:_vnext_authorized_artifact": None,
    "vnext_source_fence.py:resolve_attachable_memory_id": None,
    "mcp/evidence_artifacts.py:_authorize_explain_resource": None,
    "mcp/evidence_artifacts.py:_authorize_entity_explain_target": "_authorize_explain_resource",
    "mcp/evidence_artifacts.py:_entity_backing_is_fully_authorized": "_authorize_explain_resource",
    "mcp/evidence_artifacts.py:_handle_alice_vnext_memory_audit": "_authorize_explain_resource",
    "mcp/evidence_artifacts.py:_authorize_vnext_artifact_target": None,
    "mcp/review.py:_vnext_memory_review": None,
    "mcp/review.py:_vnext_memory_correct": None,
    "routers/vnext_memories.py:review_vnext_memory": None,
    "mcp/memories.py:redact_memory_flow": None,
    "routers/vnext_projects.py:review_vnext_open_loop": None,
    "mcp/retrieval.py:_handle_alice_open_loops": None,
    "vnext_memory_commit.py:VNextMemoryCommitService.authorize_memory_action": None,
    "vnext_memory_commit.py:VNextMemoryCommitService._write_policy_decision": None,
}

NOT_A_DOOR = {
    "routers/_vnext_shared.py:_vnext_load_source_trace": "operator trace stays label-agnostic until the screen change",
    "mcp/evidence_artifacts.py:_handle_alice_vnext_review_items": "legacy review list has no policy check",
    "routers/vnext_projects.py:list_vnext_projects": "operator project list stays label-agnostic until the screen change",
    "vnext_projects.py:VNextProjectService.generate_project_update_candidate": "producer input filter is the list-door change",
    "vnext_projects.py:VNextProjectService.review_project_update": "write path; the route authorizes before this mutation",
    "vnext_projects.py:VNextProjectService.review_open_loop": "write path; the route and the open-loop tool settle the loop first",
    "vnext_projects.py:VNextProjectService.project_dashboard": "operator dashboard lists are the list-door change",
    "vnext_projects.py:VNextProjectService._resolve_project": "operator project lookup is the list-door change",
    "vnext_memory_commit.py:VNextMemoryCommitService.confirm": "write path; _write_policy_decision settles the row",
    "vnext_memory_commit.py:VNextMemoryCommitService.undo": "write path; _write_policy_decision settles the row",
    "vnext_memory_commit.py:VNextMemoryCommitService.correct": "write path; _write_policy_decision settles the row",
    "vnext_memory_commit.py:VNextMemoryCommitService.forget": "write path; _write_policy_decision settles the row",
    "vnext_memory_commit.py:VNextMemoryCommitService.accept_consolidation_candidate": "write path; _write_policy_decision settles the row",
    "vnext_memory_commit.py:VNextMemoryCommitService.expire": "write path; _write_policy_decision settles the row",
    "vnext_memory_commit.py:VNextMemoryCommitService.unexpire": "write path; _write_policy_decision settles the row",
    "vnext_memory_commit.py:VNextMemoryCommitService.quarantine_by_agent_key": "write path; _write_policy_decision settles the row",
    "vnext_memory_commit.py:VNextMemoryCommitService.recent_commits": "owner list; the list-door change admits rows",
    "vnext_memory_commit.py:VNextMemoryCommitService.audit": "the authorize_memory callback settles each memory",
    "vnext_memory_commit.py:VNextMemoryCommitService._supersession_chain": "audit walks this after authorize_memory",
    "vnext_memory_commit.py:VNextMemoryCommitService.inline_confirmations": "owner list; the list-door change admits rows",
    "vnext_memory_commit.py:VNextMemoryCommitService._idempotent_memory": "write path replay of a row already authorized",
    "vnext_memory_commit.py:VNextMemoryCommitService._memory_by_confirmation_id": "write path replay of a row already authorized",
    "vnext_memory_commit.py:VNextMemoryCommitService._latest_agentic_commit": "owner list; the list-door change admits rows",
    "vnext_queue.py:VNextQueueService.review_artifact": "the HTTP review route authorizes through _vnext_authorized_artifact first",
    "vnext_queue.py:VNextQueueService._promote_artifact": "the HTTP review route authorizes through _vnext_authorized_artifact first",
    "vnext_queue.py:VNextQueueService.export_artifact_markdown": "the HTTP export route authorizes through _vnext_authorized_artifact first",
    "mcp/retrieval.py:_resume_event_honours_policy_fence": "list door; admit_rows lands with the list-door change",
    "mcp/retrieval.py:_vnext_recent_decisions": "list door; admit_rows lands with the list-door change",
    "mcp/retrieval.py:_vnext_resume": "list door; admit_rows lands with the list-door change",
}

SCAN_MODULES = (
    "alicebot_api/routers/_vnext_shared.py",
    "alicebot_api/mcp/evidence_artifacts.py",
    "alicebot_api/mcp/review.py",
    "alicebot_api/mcp/memories.py",
    "alicebot_api/routers/vnext_memories.py",
    "alicebot_api/routers/vnext_projects.py",
    "alicebot_api/vnext_projects.py",
    "alicebot_api/vnext_memory_commit.py",
    "alicebot_api/vnext_source_fence.py",
    "alicebot_api/vnext_open_loop_references.py",
    "alicebot_api/vnext_queue.py",
    "alicebot_api/mcp/retrieval.py",
)


def _functions(tree: ast.AST) -> list[tuple[str, ast.FunctionDef]]:
    found: list[tuple[str, ast.FunctionDef]] = []
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, ast.FunctionDef):
            found.append((node.name, node))
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef):
                    found.append((f"{node.name}.{child.name}", child))
    return found


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def _has_guard(node: ast.AST) -> bool:
    return bool(_called_names(node) & GUARD_CALLS)


def test_every_exact_door_calls_the_guard() -> None:
    modules: dict[str, ast.Module] = {}
    for key, helper in DOORS.items():
        path, name = key.split(":", 1)
        if path not in modules:
            modules[path] = ast.parse((SRC / "alicebot_api" / path).read_text(encoding="utf-8"))
        functions = dict(_functions(modules[path]))
        assert name in functions, key
        if helper is None:
            assert _has_guard(functions[name]), key
        else:
            assert helper in _called_names(functions[name]), key
            assert helper in functions, key
            assert _has_guard(functions[helper]), helper


def test_every_scanned_reader_is_classified() -> None:
    classified = set(DOORS) | set(NOT_A_DOOR)
    missing: list[str] = []
    for relative in SCAN_MODULES:
        tree = ast.parse((SRC / relative).read_text(encoding="utf-8"))
        short = relative.removeprefix("alicebot_api/")
        for name, node in _functions(tree):
            if _called_names(node) & READS:
                key = f"{short}:{name}"
                if key not in classified:
                    missing.append(key)
    assert missing == []
