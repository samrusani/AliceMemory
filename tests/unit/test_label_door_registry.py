"""Every exact reader is classified, and each exact door calls the guard."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "apps/api/src"

READS = {
    "get_source", "list_sources", "get_sources_by_ids", "get_edge",
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
    "list_open_loops_referencing_source", "list_events", "list_memory_events", "list_open_loop_events",
    "list_events_for_source_trace", "list_recent_agentic_commits", "list_pending_inline_confirmations",
    "count_sources", "count_artifacts", "count_artifacts_by_status", "count_projects", "count_memories_by_status",
    "count_open_loops", "count_open_loops_by_status", "count_events", "iter_label_rows", "iter_label_events", "iter_label_ratings",
    "list_agent_events", "list_agent_policy_artifacts", "list_agent_policy_memories",
    "list_artifact_quality_ratings", "count_artifact_quality_ratings",
}

GUARD_CALLS = {
    "LabelGuard",
    "effective_row",
    "effective_row_for_fence",
    "admit_rows",
    "admit_beliefs",
    "policy_labels",
    "admit_loaded",
    "apply_sensitivity_ceiling",
    "sensitivity_ceiling",
    "admit_events", "admit_related_rows", "readable_status_counts", "readable_event_count",
}

# function -> helper that holds the guard call, or None when the function calls it
DOORS = {
    "routers/vnext_memories.py:get_vnext_source": "_vnext_readable_source",
    "routers/vnext_memories.py:review_vnext_source": "_vnext_readable_source",
    "routers/vnext_memories.py:delete_vnext_source": "_vnext_readable_source",
    "routers/vnext_memories.py:_vnext_readable_source": None,
    "routers/_vnext_shared.py:_vnext_readable_memory": None,
    "routers/_vnext_shared.py:_vnext_readable_belief": "_vnext_readable_memory",
    "routers/_vnext_shared.py:_vnext_readable_edge": "_vnext_readable_memory",
    "routers/vnext_review.py:review_vnext_belief": "routers/_vnext_shared.py:_vnext_readable_belief",
    "routers/vnext_review.py:review_vnext_graph_edge": "routers/_vnext_shared.py:_vnext_readable_edge",
    "mcp/evidence_artifacts.py:_handle_alice_vnext_review_items": None,
    "routers/_vnext_shared.py:_vnext_authorized_artifact": None,
    "vnext_source_fence.py:resolve_attachable_memory_id": None,
    "vnext_open_loop_references.py:withhold_unreadable_references": None,
    "mcp/evidence_artifacts.py:_authorize_explain_resource": None,
    "mcp/evidence_artifacts.py:_authorize_entity_explain_target": "_authorize_explain_resource",
    "mcp/evidence_artifacts.py:_entity_backing_is_fully_authorized": "_authorize_explain_resource",
    "mcp/evidence_artifacts.py:_handle_alice_vnext_memory_audit": "_authorize_explain_resource",
    "mcp/evidence_artifacts.py:_authorize_vnext_artifact_target": None,
    "mcp/review.py:_vnext_memory_review": None,
    "mcp/review.py:_vnext_memory_correct": None,
    "routers/vnext_memories.py:review_vnext_memory": None,
    "routers/vnext_memories.py:get_vnext_memory_audit": None,
    "mcp/memories.py:redact_memory_flow": None,
    "routers/vnext_projects.py:review_vnext_open_loop": None,
    "mcp/retrieval.py:_handle_alice_open_loops": None,
    "vnext_memory_commit.py:VNextMemoryCommitService.authorize_memory_action": None,
    "vnext_memory_commit.py:VNextMemoryCommitService._write_policy_decision": None,
    "routers/_vnext_shared.py:_vnext_load_source_trace": None,
    "routers/vnext_projects.py:list_vnext_projects": None,
    "routers/vnext_review.py:list_vnext_artifacts": None,
    "routers/vnext_review.py:list_vnext_quality_evals": None,
    "routers/vnext_projects.py:get_vnext_agent_policy_telemetry": None,
    "routers/vnext_review.py:get_vnext_belief_state": None,
    "routers/vnext_memories.py:get_vnext_dogfooding_dashboard": None,
    "vnext_projects.py:VNextProjectService.project_dashboard": None,
    "vnext_projects.py:VNextProjectService._resolve_project": None,
    "vnext_projects.py:VNextProjectService.generate_project_update_candidate": None,
    "mcp/retrieval.py:_resume_event_honours_policy_fence": None,
    "mcp/retrieval.py:_vnext_recent_decisions": None,
    "mcp/retrieval.py:_vnext_resume": None,
    "mcp/projects.py:_handle_alice_vnext_open_loops": None,
    "session_briefing.py:compile_session_brief": None,
    "session_briefing.py:_event_target_honours_fence": None,
    "session_briefing.py:_memory_honours_fence": None,
    "routers/workspaces.py:_vnext_workspace_payload": None,
    "routers/workspaces.py:_workspace_event_visible": "_workspace_rows",
    "vnext_context_tree.py:VNextContextTreeService.build_tree": None,
    "vnext_dogfooding.py:VNextDogfoodingService.dashboard": None,
    "vnext_contradictions.py:VNextContradictionService.belief_state": None,
    "vnext_retrieval.py:expand_provenance_once": None,
    "vnext_retrieval.py:VNextRetrievalService._memories_by_ids": None,
    "vnext_retrieval.py:VNextRetrievalService._memory_fts_rows": None,
    "vnext_retrieval.py:VNextRetrievalService._memory_vector_rows": None,
    "vnext_retrieval.py:VNextRetrievalService._memory_graph_rows": None,
    "vnext_retrieval.py:VNextRetrievalService._memory_temporal_rows": None,
    "vnext_retrieval.py:VNextRetrievalService.compile_context_pack": None,
    "vnext_retrieval.py:VNextRetrievalService._contradicting_evidence": None,
    "vnext_retrieval.py:VNextRetrievalService._recent_changes": None,
    "vnext_retrieval.py:VNextRetrievalService.memory_visibility": None,
    "vnext_brain.py:VNextBrainService._load_inputs": None,
    "vnext_connections.py:VNextConnectionService.generate_connection_report": None,
    "vnext_contradictions.py:VNextContradictionService.generate_contradiction_report": None,
    "vnext_consolidation.py:VNextConsolidationService._cluster_memories": None,
    "vnext_consolidation.py:VNextConsolidationService.generate_memory_consolidation": None,
    "vnext_rollups.py:VNextRollupService._collect_rows": None,
    "vnext_rollups.py:VNextRollupService._existing_rollup_state": None,
    "vnext_scheduler.py:VNextSchedulerService._run_staleness_sweep": None,
    "vnext_scheduler.py:VNextSchedulerService._generate_open_loop_review_artifact": None,
    "vnext_context_tree.py:_tree_event_visible": None,
    "routers/vnext_retrieval.py:get_vnext_source_trace": "routers/_vnext_shared.py:_vnext_load_source_trace",
    "routers/vnext_retrieval.py:get_vnext_artifact_trace": "routers/_vnext_shared.py:_vnext_authorized_artifact",
}

NOT_A_DOOR = {
    "vnext_artifact_review.py:lock_artifact_review_labels": "write lock classification only; the adapter authorizes before the dispatcher mutates",
    "routers/vnext_memories.py:regenerate_vnext_source": "operator-only regeneration rejects every profile except owner and unbound admin before the source lookup; test_source_routes_limits_postgres.py pins that gate for every profile",
    "vnext_projects.py:VNextProjectService.review_project_update": "write path; the route authorizes before this mutation",
    "vnext_projects.py:VNextProjectService.review_open_loop": "write path; the route and the open-loop tool settle the loop first",
    "vnext_memory_commit.py:VNextMemoryCommitService.confirm": "write path; _write_policy_decision settles the row",
    "vnext_memory_commit.py:VNextMemoryCommitService.undo": "write path; _write_policy_decision settles the row",
    "vnext_memory_commit.py:VNextMemoryCommitService.correct": "write path; _write_policy_decision settles the row",
    "vnext_memory_commit.py:VNextMemoryCommitService.forget": "write path; _write_policy_decision settles the row",
    "vnext_memory_commit.py:VNextMemoryCommitService.accept_consolidation_candidate": "write path; _write_policy_decision settles the row",
    "vnext_memory_commit.py:VNextMemoryCommitService.expire": "write path; _write_policy_decision settles the row",
    "vnext_memory_commit.py:VNextMemoryCommitService.unexpire": "write path; _write_policy_decision settles the row",
    "vnext_memory_commit.py:VNextMemoryCommitService.quarantine_by_agent_key": "write path; _write_policy_decision settles the row",
    "vnext_memory_commit.py:VNextMemoryCommitService.recent_commits": "owner list; stays label-agnostic",
    "vnext_memory_commit.py:VNextMemoryCommitService.audit": "the authorize_memory callback settles each memory",
    "vnext_memory_commit.py:VNextMemoryCommitService._supersession_chain": "audit walks this after authorize_memory",
    "vnext_memory_commit.py:VNextMemoryCommitService.inline_confirmations": "owner list; stays label-agnostic",
    "vnext_memory_commit.py:VNextMemoryCommitService._idempotent_memory": "write path replay of a row already authorized",
    "vnext_memory_commit.py:VNextMemoryCommitService._memory_by_confirmation_id": "write path replay of a row already authorized",
    "vnext_memory_commit.py:VNextMemoryCommitService._latest_agentic_commit": "owner list; stays label-agnostic",
    "vnext_queue.py:VNextQueueService.review_artifact": "the HTTP review route authorizes through _vnext_authorized_artifact first",
    "vnext_queue.py:VNextQueueService._promote_artifact": "the HTTP review route authorizes through _vnext_authorized_artifact first",
    "vnext_queue.py:VNextQueueService.export_artifact_markdown": "the HTTP export route authorizes through _vnext_authorized_artifact first",
    "mcp/evidence_artifacts.py:_authorize_memory_audit_provenance": "original source pointers use SourceReadFence.admits before disclosure",
    "routers/vnext_memories.py:get_vnext_connector_status": "operator connector telemetry; original-source labels retain existing behavior",
    "vnext_memory_commit.py:VNextMemoryCommitService.auto_promoted_by_agent": "write sweep; every target is authorized by expire before mutation",
    "vnext_memory_commit.py:VNextMemoryCommitService._transition_memory": "writer checks source validity; no new read response",
    "vnext_source_fence.py:_rows_by_id": "narrow loader; SavedProvenanceReader applies effective labels before presenting",
    "vnext_source_fence.py:source_rows_including_archived": "original-source loader for provenance and producer label computation",
    "vnext_source_fence.py:SavedProvenanceReader._row_for": "private loader; SavedProvenanceReader._admits settles each row",
    "vnext_retrieval.py:_current_memory_id": "pointer loader; caller memory_visibility settles before exposing the pointer",
    "vnext_retrieval.py:_memories_referencing_sources": "internal loader; expand_provenance_once applies effective admission",
    "vnext_retrieval.py:VNextRetrievalService._sources_by_ids": "original-source lookup; SourceReadFence admits before formatting",
    "vnext_retrieval.py:VNextRetrievalService._query_embedding": "store capability discovery only; does not execute a row reader",
    "vnext_retrieval.py:VNextRetrievalService._source_stage_lists": "original-source stage; existing source admission remains",
    "vnext_retrieval.py:VNextRetrievalService._supersession_context": "pointer metadata is fenced through memory_visibility at output",
    "session_briefing.py:_merge_recent_change_targets": "private loader checks _event_target_honours_fence before adding a target",
    "session_briefing.py:_resolve_excerpt_query": "original source lookup; source fence and withheld event targets constrain excerpts",
    "vnext_context_tree.py:VNextContextTreeService._scoped_events": "build_tree filters every resulting event through _tree_event_visible",
    "vnext_dogfooding.py:VNextDogfoodingService.record_insight_feedback": "write route authorizes artifact through _vnext_authorized_artifact",
    "vnext_contradictions.py:_project_scoped_beliefs": "internal loader; generate_contradiction_report admits through backing memories",
    "vnext_consolidation.py:_list_memories_bounded": "private loader; _cluster_memories applies effective admission",
    "vnext_consolidation.py:_existing_cluster_candidates": "owner acceptance/idempotency lookup; group-scope consumers retain existing behavior",
    "vnext_scheduler.py:_StagedSchedulerStore.update_memory": "staged write replay; returned row is not a disclosure door",
    "vnext_scheduler.py:VNextSchedulerService.status": "scheduler telemetry; events are constrained to scheduler targets",
    "vnext_scheduler.py:VNextSchedulerService._generate_project_update_scan_artifact": "internal project scan; generate_project_update_candidate applies effective admission",
    "vnext_connectors.py:VNextConnectorService.get_cursor": "connector cursor events only; no labelled targets",
    "vnext_connectors.py:VNextConnectorService.get_config": "connector configuration events only; no labelled targets",
    "vnext_connectors.py:VNextConnectorService.connector_health": "connector state telemetry only; no labels_raised events",
    "vnext_connectors.py:VNextConnectorService._connector_events": "raw connector event cache; the query is constrained to connector targets and preserves the existing configuration/cursor telemetry readers",
    "vnext_dogfooding.py:VNextDogfoodingStore.list_artifact_quality_ratings": "store protocol declaration; no execution or response",
    "vnext_artifact_review.py:dispatch_vnext_artifact_review": "writer entry; calling route or MCP authorizes the artifact before dispatch",
    "vnext_memory_commit.py:VNextMemoryCommitService._guard_supersession_acyclic": "write validation traverses pointers without exposing their content",
    "routers/vnext_projects.py:_vnext_project_id_within_binding": "a project has no labels; the open-loop create route holds the id to the key's project binding, and a project outside it reads as a missing one",
}

SCAN_MODULES = tuple(sorted({
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
    "alicebot_api/vnext_retrieval.py", "alicebot_api/session_briefing.py",
    "alicebot_api/routers/workspaces.py", "alicebot_api/vnext_context_tree.py", "alicebot_api/vnext_dogfooding.py",
    "alicebot_api/vnext_brain.py", "alicebot_api/vnext_connections.py", "alicebot_api/vnext_contradictions.py",
    "alicebot_api/vnext_consolidation.py", "alicebot_api/vnext_rollups.py", "alicebot_api/vnext_scheduler.py",
    "alicebot_api/vnext_connectors.py", "alicebot_api/vnext_artifact_review.py",
    *(str(path.relative_to(SRC)) for directory in ("routers", "mcp") for path in (SRC / "alicebot_api" / directory).glob("*.py")),
}))


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


def _reader_names(node: ast.AST) -> set[str]:
    """Actual AST calls, bound-method callbacks, and dynamic reader lookup."""

    names = _called_names(node) & READS
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr in READS:
            names.add(child.attr)
        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "getattr":
            if len(child.args) > 1 and isinstance(child.args[1], ast.Constant) and child.args[1].value in READS:
                names.add(child.args[1].value)
            elif len(child.args) > 1 and not isinstance(child.args[1], ast.Constant):
                # Target-kind dispatch maps choose a method name dynamically.
                # Resolve their possible reader names from this function's AST.
                names.update(value.value for value in ast.walk(node) if isinstance(value, ast.Constant)
                             and isinstance(value.value, str) and value.value in READS)
    return names


def _has_guard(node: ast.AST) -> bool:
    return bool(_called_names(node) & GUARD_CALLS)


def _delegates_to_a_registered_door(node: ast.AST) -> bool:
    """A helper that composes the doors of other kinds (an edge is read through its ends) may call them instead."""

    registered = {key.split(":", 1)[1].rsplit(".", 1)[-1] for key, helper in DOORS.items() if helper is None}
    return bool(_called_names(node) & registered)


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
            helper_path, helper_name = helper.split(":", 1) if ":" in helper else (path, helper)
            assert helper_name in _called_names(functions[name]), key
            if helper_path not in modules:
                modules[helper_path] = ast.parse((SRC / "alicebot_api" / helper_path).read_text(encoding="utf-8"))
            helper_functions = dict(_functions(modules[helper_path]))
            assert helper_name in helper_functions, key
            assert _has_guard(helper_functions[helper_name]) or _delegates_to_a_registered_door(
                helper_functions[helper_name]
            ), helper


def test_every_scanned_reader_is_classified() -> None:
    classified = set(DOORS) | set(NOT_A_DOOR)
    missing: list[str] = []
    for relative in SCAN_MODULES:
        tree = ast.parse((SRC / relative).read_text(encoding="utf-8"))
        short = relative.removeprefix("alicebot_api/")
        for name, node in _functions(tree):
            if _reader_names(node):
                key = f"{short}:{name}"
                if key not in classified:
                    missing.append(key)
    assert missing == []


def test_discovery_catches_new_direct_and_dynamic_readers() -> None:
    for source in ("def added(store): return store.list_events()", "def added(store): return getattr(store, 'list_memories')()", "def added(store): return invoke(store.list_beliefs)",
                   "def added(store, kind):\n methods = {'memory': 'get_memory', 'artifact': 'get_artifact'}\n return getattr(store, methods[kind])()"):
        node = ast.parse(source).body[0]
        assert _reader_names(node)
        assert "added" not in DOORS and "added" not in NOT_A_DOOR
