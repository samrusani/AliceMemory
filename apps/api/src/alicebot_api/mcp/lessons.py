"""Published lesson lookup through the recall memory fence."""

from __future__ import annotations

import fnmatch
from itertools import groupby
import json
from collections.abc import Mapping

from alicebot_api.project_view import ProjectView
from alicebot_api.json_bounds import loads_stored
from alicebot_api.recall_framing import memory_writer, with_result_framing
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_json import json_safe
from alicebot_api.vnext_label_guard import LabelGuard, admit_loaded, sensitivity_ceiling
from alicebot_api.vnext_memory_commit import (
    VNextMemoryCommitValidationError,
    _lesson_block_from_payload,
    _lesson_paths,
)
from alicebot_api.vnext_retrieval import VNextRetrievalService, reciprocal_rank_fusion
from alicebot_api.vnext_source_fence import SourceReadFence

from .arguments import _parse_int, _parse_required_text
from .policy import _agent_identity_from_arguments, _mcp_agent_policy_preflight
from .runtime import _vnext_store_context
from .types import MCPArgumentError, MCPRuntimeContext


class _RevisionView:
    def __init__(self, store, grouped):
        self.store, self.grouped = store, grouped

    def list_revisions(self, memory_id):
        return self.grouped.get(str(memory_id), [])

    def list_events(self, **kwargs):
        return self.store.list_events(**kwargs)


def _path_matches(paths, entries):
    return any(
        fnmatch.fnmatchcase(path.removeprefix("./"), entry.removeprefix("./")) for path in paths for entry in entries
    )


def _writer_can_publish(row):
    metadata = row.get("metadata_json", row.get("lesson_metadata_raw"))
    if isinstance(metadata, str):
        metadata = loads_stored(metadata)
    agentic = metadata.get("agentic_memory") if isinstance(metadata, Mapping) else None
    identity = agentic.get("agent_identity") if isinstance(agentic, Mapping) else None
    if identity is None:
        return row.get("created_by_agent_id") is None and not (
            isinstance(metadata, Mapping) and metadata.get("agent_id")
        )
    return isinstance(identity, Mapping) and identity.get("permission_profile") == "admin_agent"


def _publication(row, revisions):
    value = row.get("value")
    lesson = value.get("lesson") if isinstance(value, Mapping) else None
    for revision in revisions:
        published_lesson = revision.get("publication_lesson")
        if isinstance(published_lesson, str):
            try:
                published_lesson = json.loads(published_lesson)
            except ValueError:
                continue
        if not isinstance(lesson, Mapping) or published_lesson != lesson:
            continue
        action, kind = revision.get("action"), revision.get("revision_type")
        if action == "agentic_memory_commit" and kind == "created" and _writer_can_publish(row):
            how = "committed"
        elif action == "agentic_memory_confirm_confirm" and kind == "promoted" and _writer_can_publish(row):
            how = "confirmed"
        elif (
            action in {"memory_correct_approve", "memory_review_accept", "memory_review_promote"} and kind == "promoted"
        ):
            how = "approved"
        else:
            continue
        actor_type = revision.get("actor_type")
        if actor_type not in {"agent", "user"}:
            continue
        actor = revision.get("actor_id") if actor_type == "agent" else "owner"
        if not actor or not revision.get("created_at"):
            continue
        return {"by": str(actor), "at": json_safe(revision["created_at"]), "how": how}
    return None


def _select_published_lessons(store, *, domains, sensitivity, projects, limit, order, scan_state=None):
    """Select readable published lesson rows in the caller's requested order."""
    from itertools import islice
    from alicebot_api.sqlite_store import SQLiteVNextStore
    from alicebot_api.vnext_stores.sqlite.lessons import stream_lesson_memories

    lazy_metadata = not LabelGuard.for_filters(store, domains, sensitivity, projects).active
    if order is None:
        if isinstance(store, SQLiteVNextStore):
            rows = stream_lesson_memories(
                store, domains=domains, sensitivity=sensitivity, projects=projects, lazy_metadata=lazy_metadata
            )
        else:
            rows = iter(store.list_memories(
                status="active", memory_types=("procedure",), domains=domains,
                sensitivity_allowed=sensitivity, projects=projects, include_expired=False,
                lessons_only=True, lesson_projection=True, lesson_lazy_metadata=lazy_metadata,
                lesson_publication_candidate=True,
            ))
        batch_size = 64
    else:
        global_option = {"exclude_global_domains": ()} if "~global" in projects and isinstance(store, SQLiteVNextStore) else {}
        rows = iter(store.list_memories(
            status="active", memory_types=("procedure",), domains=domains,
            sensitivity_allowed=sensitivity, projects=projects, include_expired=False,
            lessons_only=True, lesson_projection=True, lesson_lazy_metadata=lazy_metadata,
            **global_option,
        ))
        batch_size = None
    grouped: dict[str, list[dict[str, object]]] = {}
    choices = []
    examined = 0
    limit_reached = False
    if scan_state is not None:
        scan_state["limit_reached"] = False
    try:
        while True:
            batch = list(islice(rows, batch_size)) if batch_size is not None else list(rows)
            if not batch:
                break
            batch = admit_loaded(
                store, kind="memory", rows=batch, domains=domains, sensitivity_allowed=sensitivity, projects=projects
            )
            if order is None:
                remaining = 256 - examined
                if len(batch) > remaining:
                    limit_reached = True
                    if scan_state is not None:
                        scan_state["limit_reached"] = True
                    batch = batch[:remaining]
                examined += len(batch)
                candidates = []
                for row in batch:
                    value = row.get("value")
                    block = value.get("lesson") if isinstance(value, Mapping) else None
                    if isinstance(block, Mapping):
                        candidates.append((row, block, False, False, 0.0))
            else:
                candidates = order(batch)
            indexed = {str(row["id"]): (row, block, matched_path, matched_text, score)
                       for row, block, matched_path, matched_text, score in candidates}
            revisions = store.stream_lesson_revisions(list(indexed))
            try:
                for mid, group in groupby(revisions, key=lambda rev: str(rev.get("memory_id"))):
                    records = list(group)
                    row, block, matched_path, matched_text, score = indexed[mid]
                    try:
                        lesson = _lesson_block_from_payload(block)
                    except VNextMemoryCommitValidationError:
                        continue
                    publication = _publication(row, records)
                    if publication is not None:
                        grouped[mid] = records
                        choices.append((row, lesson, publication, matched_path, matched_text, score))
                        if len(choices) == limit:
                            break
            finally:
                close = getattr(revisions, "close", None)
                if callable(close):
                    close()
            if len(choices) == limit or batch_size is None or limit_reached:
                break
    finally:
        close = getattr(rows, "close", None)
        if callable(close):
            close()
    return choices, grouped


def _handle_alice_lessons(context: MCPRuntimeContext, arguments: Mapping[str, object]):
    task = _parse_required_text(arguments, "task")
    if len(task) > 1000:
        raise MCPArgumentError("task must be at most 1000 characters")
    try:
        paths = _lesson_paths(arguments.get("paths"))
    except VNextMemoryCommitValidationError as exc:
        raise MCPArgumentError(str(exc)) from None
    limit = _parse_int(arguments, key="limit", default=5, minimum=1, maximum=10)
    identity = _agent_identity_from_arguments(context, arguments)
    decision = _mcp_agent_policy_preflight(
        context,
        arguments,
        action="memory.recall",
        project_view=ProjectView.unscoped(),
        sensitivity_allowed=sensitivity_ceiling(identity) or ALL_SENSITIVITY,
    )
    domains = list(decision.effective_domains)
    sensitivity = list(decision.effective_sensitivity_allowed)
    projects = tuple(decision.effective_project_scope)
    fence = SourceReadFence.for_identity(identity)
    with _vnext_store_context(context) as store:
        lock = getattr(store, "lock_label_writes", None)
        if callable(lock):
            lock()
        def order(rows):
            service = VNextRetrievalService(store)
            amount = max(1, len(rows))
            fts, _ = service._memory_fts_rows(
                query=task,
                domains=domains,
                sensitivity_allowed=sensitivity,
                limit=amount,
                memory_types=("procedure",),
                projects=projects,
                lessons_only=True,
                read_fence=fence,
                lesson_admitted_ids=frozenset(str(row["id"]) for row in rows),
            )
            vector, status = service._memory_vector_rows(
                query=task,
                domains=domains,
                sensitivity_allowed=sensitivity,
                limit=amount,
                memory_types=("procedure",),
                projects=projects,
                lessons_only=True,
                label_refill=fence.entity_read_fenced,
                lesson_admitted_ids=frozenset(str(row["id"]) for row in rows),
            )
            if status == "enabled":
                ranks = {
                    str(row.get("id")): score
                    for row, score, _ in reciprocal_rank_fusion({"fts": fts, "vector": vector})
                }
            else:
                ranks = {
                    str(row.get("id")): float(score) if isinstance(score, (int, float)) else 0.0
                    for row in fts
                    for score in (row.get("fts_score"),)
                }
            candidates = []
            for row in rows:
                value = row.get("value")
                block = value.get("lesson") if isinstance(value, Mapping) else None
                if not isinstance(block, Mapping):
                    continue
                mid = str(row["id"])
                entries = block.get("paths")
                path_entries = [" ".join(entry.split()) for entry in entries if isinstance(entry, str)] if isinstance(entries, list) else []
                matched_path = _path_matches(paths, path_entries)
                matched_text = mid in ranks
                if matched_path or matched_text:
                    candidates.append((row, block, matched_path, matched_text, ranks.get(mid, 0.0)))
            candidates.sort(key=lambda item: str(item[0]["id"]))
            candidates.sort(key=lambda item: str(item[0].get("created_at") or ""), reverse=True)
            candidates.sort(key=lambda item: item[4], reverse=True)
            candidates.sort(key=lambda item: (not item[2], not item[3]))
            return candidates

        choices, grouped = _select_published_lessons(
            store, domains=domains, sensitivity=sensitivity, projects=projects, limit=limit, order=order
        )
        full_rows = store.get_memories_by_ids([str(row["id"]) for row, *_ in choices])
        full_rows = admit_loaded(
            store, kind="memory", rows=full_rows, domains=domains, sensitivity_allowed=sensitivity, projects=projects
        )
        full_by_id = {str(row["id"]): row for row in full_rows}
        view = _RevisionView(store, grouped)
        result = [
            {
                "id": str(row["id"]),
                "applies_when": lesson.applies_when,
                "what_happened": lesson.what_happened,
                "do_instead": lesson.do_instead,
                "paths": list(lesson.paths),
                "writer": memory_writer(view, full_by_id[str(row["id"])]),
                "published": publication,
            }
            for row, lesson, publication, _path, _text, _score in choices
            if str(row["id"]) in full_by_id
        ]
        append_event(
            store,
            event_type="agent.lessons_requested",
            actor_type="agent" if identity is not None else "user",
            actor_id=identity.agent_id if identity is not None else None,
            payload={"returned": len(result), "limit": limit, "paths_given": len(paths)},
        )
    return with_result_framing({"count": len(result), "lessons": result})
