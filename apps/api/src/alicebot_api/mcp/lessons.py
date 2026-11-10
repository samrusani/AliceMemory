"""Published lesson lookup through the recall memory fence."""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Mapping

from alicebot_api.project_view import ProjectView
from alicebot_api.recall_framing import memory_writer, with_result_framing
from alicebot_api.sqlite_store import SQLiteVNextStore
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_json import json_safe
from alicebot_api.vnext_label_guard import admit_loaded
from alicebot_api.vnext_memory_commit import (
    VNextMemoryCommitValidationError,
    _lesson_block_from_payload,
    _lesson_paths,
)
from alicebot_api.vnext_retrieval import VNextRetrievalService, reciprocal_rank_fusion
from alicebot_api.vnext_source_fence import SourceReadFence
from alicebot_api.vnext_stores.memory_lifecycle_common import is_redacted_memory

from .arguments import _parse_int, _parse_required_text
from .policy import _agent_identity_from_arguments, _mcp_agent_policy_preflight
from .runtime import _vnext_store_context
from .types import MCPArgumentError, MCPRuntimeContext


def _revisions_for_candidates(store):
    """Read the acts for all potential lessons in one query."""
    columns = "r.memory_id, r.revision_number, r.sequence_no, r.id, r.action, r.revision_type, r.actor_type, r.actor_id, r.created_at, r.text_before, r.text_after"
    if isinstance(store, SQLiteVNextStore):
        return store._fetch_all(
            f"SELECT {columns}, json_extract(r.new_value, '$.lesson') AS publication_lesson FROM memory_revisions r JOIN memories m ON m.id = r.memory_id AND m.user_id = r.user_id "
            "WHERE r.user_id = ? AND m.status = 'active' AND m.memory_type = 'procedure' "
            "AND m.deleted_at IS NULL AND json_type(m.value, '$.lesson') = 'object' "
            "ORDER BY r.memory_id, r.revision_number, r.sequence_no, r.id",
            (store.user_id,),
        )
    return store._fetch_all(
        f"SELECT {columns}, r.new_value -> 'lesson' AS publication_lesson FROM memory_revisions r JOIN memories m ON m.id = r.memory_id AND m.user_id = r.user_id "
        "WHERE r.user_id = app.current_user_id() AND m.status = 'active' AND m.memory_type = 'procedure' "
        "AND m.deleted_at IS NULL AND jsonb_typeof(m.value -> 'lesson') = 'object' "
        "ORDER BY r.memory_id, r.revision_number, r.sequence_no, r.id",
        (),
    )


def _candidate_memories(store, ids):
    """Hydrate a candidate set in one query without a variable-count limit."""
    if isinstance(store, SQLiteVNextStore):
        from alicebot_api.vnext_stores.sqlite.columns import MEMORY_COLUMNS

        return store._fetch_all(
            f"SELECT {', '.join(MEMORY_COLUMNS)} FROM memories "
            "WHERE user_id = ? AND deleted_at IS NULL AND id IN (SELECT value FROM json_each(?))",
            (store.user_id, json.dumps(ids)),
        )
    from alicebot_api.vnext_stores.postgres.columns import MEMORY_COLUMNS

    return store._fetch_all(
        f"SELECT {MEMORY_COLUMNS} FROM memories WHERE user_id = app.current_user_id() "
        "AND deleted_at IS NULL AND id = ANY(%s::uuid[])",
        (ids,),
    )


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
    metadata = row.get("metadata_json")
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
    )
    domains = list(decision.effective_domains)
    sensitivity = list(decision.effective_sensitivity_allowed)
    projects = tuple(decision.effective_project_scope)
    fence = SourceReadFence.for_identity(identity)
    with _vnext_store_context(context) as store:
        lock = getattr(store, "lock_label_writes", None)
        if callable(lock):
            lock()
        revisions = _revisions_for_candidates(store)
        grouped: dict[str, list[dict[str, object]]] = {}
        for rev in revisions:
            grouped.setdefault(str(rev.get("memory_id")), []).append(rev)
        service = VNextRetrievalService(store)
        # When the lexical stage is exhausted, no later text hit can outrank
        # its results. A full page of path hits is also the complete first group.
        if service.embedding_provider is None:
            first, _ = service._memory_fts_rows(
                query=task,
                domains=domains,
                sensitivity_allowed=sensitivity,
                limit=limit,
                memory_types=("procedure",),
                projects=projects,
                lessons_only=True,
                read_fence=fence,
            )
            first_items = []
            for row in first:
                try:
                    value = row.get("value")
                    lesson = _lesson_block_from_payload(value.get("lesson") if isinstance(value, Mapping) else None)
                except VNextMemoryCommitValidationError:
                    continue
                publication = _publication(row, grouped.get(str(row.get("id")), []))
                if is_redacted_memory(row) or publication is None:
                    continue
                match = _path_matches(paths, lesson.paths)
                first_items.append((row, lesson, publication, match))
            complete_first_group = len(first_items) == limit and (not paths or all(item[3] for item in first_items))
            exhausted = len(first) < limit
            if complete_first_group or exhausted:
                both = [item[:3] for item in first_items if item[3]]
                text_only = [item[:3] for item in first_items if not item[3]]
                path_only = []
                if exhausted and paths and len(both) < limit:
                    projected = store.list_memories(
                        status="active",
                        memory_types=("procedure",),
                        domains=domains,
                        sensitivity_allowed=sensitivity,
                        projects=projects,
                        include_expired=False,
                        lessons_only=True,
                        lesson_projection=True,
                        order_by_created_at=True,
                    )
                    first_ids = {str(row.get("id")) for row in first}

                    def select_candidates(items, max_count=None):
                        selected = []
                        position = 0
                        for position, row in enumerate(items, start=1):
                            if str(row.get("id")) in first_ids:
                                continue
                            value = row.get("value")
                            block = value.get("lesson") if isinstance(value, Mapping) else None
                            entries = block.get("paths") if isinstance(block, Mapping) else None
                            if not isinstance(entries, list) or not _path_matches(
                                paths, [" ".join(entry.split()) for entry in entries if isinstance(entry, str)]
                            ):
                                continue
                            try:
                                lesson = _lesson_block_from_payload(block)
                            except VNextMemoryCommitValidationError:
                                continue
                            if not _path_matches(paths, lesson.paths):
                                continue
                            publication = _publication(row, grouped.get(str(row.get("id")), []))
                            if publication is not None:
                                selected.append((row, lesson, publication))
                            if max_count is not None and len(selected) == max_count:
                                break
                        return selected, position

                    def admit_candidates(candidates):
                        hydrated = _candidate_memories(store, [str(row["id"]) for row, _, _ in candidates])
                        readable = admit_loaded(
                            store,
                            kind="memory",
                            rows=hydrated,
                            domains=domains,
                            sensitivity_allowed=sensitivity,
                            projects=projects,
                        )
                        indexed = {str(row["id"]): row for row in readable if not is_redacted_memory(row)}
                        return [
                            (indexed[str(row["id"])], lesson, publication)
                            for row, lesson, publication in candidates
                            if str(row["id"]) in indexed
                        ]

                    candidates, position = select_candidates(projected, 128)
                    path_only = admit_candidates(candidates)
                    # A bounded prefix is enough in the usual case. If its
                    # readable rows do not fill the answer, read the remaining
                    # candidates as one complete batch, rather than paging.
                    remaining = []
                    if len(both) + len(path_only) < limit:
                        remaining, _ = select_candidates(projected[position:])
                    path_only.extend(admit_candidates(remaining))
                chosen = (both + path_only + text_only)[:limit]
                view = _RevisionView(store, grouped)
                result = [
                    {
                        "id": str(row["id"]),
                        "applies_when": lesson.applies_when,
                        "what_happened": lesson.what_happened,
                        "do_instead": lesson.do_instead,
                        "paths": list(lesson.paths),
                        "writer": memory_writer(view, row),
                        "published": publication,
                    }
                    for row, lesson, publication in chosen
                ]
                append_event(
                    store,
                    event_type="agent.lessons_requested",
                    actor_type="agent" if identity is not None else "user",
                    actor_id=identity.agent_id if identity is not None else None,
                    payload={"returned": len(result), "limit": limit, "paths_given": len(paths)},
                )
                return with_result_framing({"count": len(result), "lessons": result})
        rows = store.list_memories(
            status="active",
            memory_types=("procedure",),
            domains=domains,
            sensitivity_allowed=sensitivity,
            projects=projects,
            include_expired=False,
            lessons_only=True,
        )
        rows = admit_loaded(
            store, kind="memory", rows=rows, domains=domains, sensitivity_allowed=sensitivity, projects=projects
        )
        valid = []
        for row in rows:
            if is_redacted_memory(row):
                continue
            value = row.get("value")
            block = value.get("lesson") if isinstance(value, Mapping) else None
            try:
                lesson = _lesson_block_from_payload(block)
            except VNextMemoryCommitValidationError:
                continue
            valid.append((row, lesson))
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
        )
        lists = {"fts": fts}
        if status == "enabled":
            lists["vector"] = vector
        if status == "enabled":
            ranks = {str(row.get("id")): score for row, score, _ in reciprocal_rank_fusion(lists)}
        else:
            ranks = {
                str(row.get("id")): float(score) if isinstance(score, (int, float)) else 0.0
                for row in fts
                for score in (row.get("fts_score"),)
            }
        choices = []
        for row, lesson in valid:
            mid = str(row["id"])
            pub = _publication(row, grouped.get(mid, []))
            if pub is None:
                continue
            matched_path = _path_matches(paths, lesson.paths)
            matched_text = mid in ranks
            if not matched_path and not matched_text:
                continue
            choices.append((row, lesson, pub, matched_path, matched_text, ranks.get(mid, 0.0)))
        choices.sort(key=lambda item: str(item[0]["id"]))
        choices.sort(key=lambda item: str(item[0].get("created_at") or ""), reverse=True)
        choices.sort(key=lambda item: item[5], reverse=True)
        choices.sort(key=lambda item: (not item[3], not item[4]))
        revision_view = _RevisionView(store, grouped)
        result = [
            {
                "id": str(row["id"]),
                "applies_when": lesson.applies_when,
                "what_happened": lesson.what_happened,
                "do_instead": lesson.do_instead,
                "paths": list(lesson.paths),
                "writer": memory_writer(revision_view, row),
                "published": publication,
            }
            for row, lesson, publication, _path, _text, _score in choices[:limit]
        ]
        append_event(
            store,
            event_type="agent.lessons_requested",
            actor_type="agent" if identity is not None else "user",
            actor_id=identity.agent_id if identity is not None else None,
            payload={"returned": len(result), "limit": limit, "paths_given": len(paths)},
        )
    return with_result_framing({"count": len(result), "lessons": result})
