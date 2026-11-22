"""Published lesson lookup through the recall memory fence."""

from __future__ import annotations

from datetime import datetime, timedelta
import fnmatch
from heapq import nlargest
from itertools import groupby
import json
import sqlite3
from collections.abc import Mapping

from alicebot_api.credential_floor import RATIONALE_WITHHELD_PLACEHOLDER, carries_credential_material
from alicebot_api.project_view import ProjectView
from alicebot_api.json_bounds import loads_stored
from alicebot_api.recall_framing import memory_writer, with_result_framing
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY, AgentIdentity
from alicebot_api.vnext_derived_labels import is_redacted_row
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_json import json_safe
from alicebot_api.vnext_label_guard import LabelGuard, admit_loaded, sensitivity_ceiling
from alicebot_api.vnext_lifecycle import default_memory_decision_reason
from alicebot_api.vnext_memory_commit import (
    VNextMemoryCommitValidationError,
    _lesson_block_from_payload,
    _lesson_paths,
    _utc_now,
)
from alicebot_api.vnext_retrieval import VNextRetrievalService, _parse_timestamp, reciprocal_rank_fusion
from alicebot_api.vnext_source_fence import SourceReadFence
from alicebot_api.vnext_stores.postgres import lessons as postgres_lessons
from alicebot_api.vnext_stores.sqlite import lessons as sqlite_lessons

from .arguments import _parse_int, _parse_required_text
from .policy import _agent_identity_from_arguments, _mcp_agent_policy_preflight
from .runtime import _vnext_store_context
from .types import MCPArgumentError, MCPRuntimeContext


LESSON_OUTCOME_WINDOW = timedelta(days=7)
LESSON_OUTCOME_UPDATED_MARGIN = timedelta(hours=1)
LESSON_OUTCOME_DETAIL_LIMIT = 3
LESSON_OUTCOME_APPLIES_WHEN_CHARS = 160
LESSON_OUTCOME_REASON_CHARS = 300
LESSON_OUTCOME_CUT_MARK = "…"

_UNPUBLISHED_DECISIONS = {
    "rejected": {
        ("rejected", "memory_correct_reject"),
        ("rejected", "memory_review_reject"),
        ("rejected", "agentic_memory_confirm_reject"),
        ("rejected", "agentic_memory_confirmation_expired"),
    },
    "private_only": {("edited", "memory_review_private")},
    "superseded": {
        ("archived", "agentic_memory_forget"),
        ("superseded", "agentic_memory_undo"),
        ("superseded", "agentic_memory_consolidation_supersede"),
    },
}


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


def _cut_outcome_note(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - len(LESSON_OUTCOME_CUT_MARK)] + LESSON_OUTCOME_CUT_MARK


def _outcome_reason(revision: Mapping[str, object]) -> str | None:
    reason = revision.get("reason")
    action = revision.get("action")
    if (
        not isinstance(reason, str) or not reason.strip()
        or reason == default_memory_decision_reason(str(action))
        # Consolidation writes its own per-member reason and takes no reason for this lesson.
        or action == "agentic_memory_consolidation_supersede"
        or reason == RATIONALE_WITHHELD_PLACEHOLDER
        or carries_credential_material(reason)
    ):
        return None
    return _cut_outcome_note(reason, LESSON_OUTCOME_REASON_CHARS)


def _lesson_outcomes(store, *, identity: AgentIdentity, domains, sensitivity, projects) -> dict[str, object] | None:
    now = _utc_now()
    cutoff = now - LESSON_OUTCOME_WINDOW
    backend = sqlite_lessons if isinstance(store.conn, sqlite3.Connection) else postgres_lessons
    rows = backend.select_lesson_outcome_rows(
        store, writer=identity.agent_id, projects=projects, updated_since=cutoff - LESSON_OUTCOME_UPDATED_MARGIN,
    )
    admitted = admit_loaded(
        store, kind="memory", rows=rows, domains=domains, sensitivity_allowed=sensitivity,
        projects=projects, identity=identity,
    )
    visible = [row for row in admitted if not is_redacted_row(row)]
    waiting = sum(row["status"] == "candidate" for row in visible)
    timed = {str(row["id"]): row for row in visible if row["status"] != "candidate"}
    published = 0
    unpublished: list[tuple[datetime, str, str, Mapping[str, object]]] = []
    if timed:
        batch = store.stream_lesson_revisions(list(timed), include_reason=True)
        for memory_id, group in groupby(batch, key=lambda revision: str(revision["memory_id"])):
            row = timed[memory_id]
            revisions = list(group)
            status = row["status"]
            if status == "active":
                publication = _publication(row, revisions)
                at = _parse_timestamp(publication["at"]) if publication else None
                published += int(at is not None and cutoff <= at <= now)
                continue
            actions = _UNPUBLISHED_DECISIONS.get(status, set())
            decision = next(
                (rev for rev in reversed(revisions) if (rev.get("revision_type"), rev.get("action")) in actions),
                None,
            )
            if decision is None:
                continue
            at = _parse_timestamp(decision.get("created_at"))
            applies_when = row["value"]["lesson"].get("applies_when")
            if at is not None and cutoff <= at <= now and isinstance(applies_when, str):
                unpublished.append((at, memory_id, applies_when, decision))
    if not (waiting or published or unpublished):
        return None
    result: dict[str, object] = {
        "waiting": waiting,
        "published_in_last_7_days": published,
        "not_published_in_last_7_days": len(unpublished),
    }
    if unpublished:
        newest = nlargest(LESSON_OUTCOME_DETAIL_LIMIT, unpublished, key=lambda item: (item[0], item[1]))
        result["not_published"] = [
            {
                "id": memory_id,
                "applies_when": _cut_outcome_note(applies_when, LESSON_OUTCOME_APPLIES_WHEN_CHARS),
                "reason": _outcome_reason(decision),
                "at": json_safe(decision["created_at"]),
            }
            for _at, memory_id, applies_when, decision in newest
        ]
    return result


def _select_published_lessons(store, *, domains, sensitivity, projects, limit, order, scan_state=None):
    """Select readable published lesson rows in the caller's requested order."""
    from itertools import islice
    from alicebot_api.sqlite_store import SQLiteVNextStore
    from alicebot_api.vnext_stores.postgres.lessons import stream_lesson_memories as stream_postgres_lessons
    from alicebot_api.vnext_stores.sqlite.lessons import stream_lesson_memories

    lazy_metadata = not LabelGuard.for_filters(store, domains, sensitivity, projects).active
    if order is None:
        if isinstance(store.conn, sqlite3.Connection):
            rows = stream_lesson_memories(
                store, domains=domains, sensitivity=sensitivity, projects=projects, lazy_metadata=lazy_metadata
            )
        else:
            rows = stream_postgres_lessons(
                store, domains=domains, sensitivity=sensitivity, projects=projects, lazy_metadata=lazy_metadata
            )
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
        response: dict[str, object] = {"count": len(result), "lessons": result}
        if identity is not None and identity.auth == "agent_api_key":
            yours = _lesson_outcomes(store, identity=identity, domains=domains, sensitivity=sensitivity, projects=projects)
            if yours is not None:
                response["yours"] = yours
        append_event(
            store,
            event_type="agent.lessons_requested",
            actor_type="agent" if identity is not None else "user",
            actor_id=identity.agent_id if identity is not None else None,
            payload={"returned": len(result), "limit": limit, "paths_given": len(paths)},
        )
    return with_result_framing(response)
