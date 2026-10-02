"""Mechanical MCP capture mutations carrier."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID
from alicebot_api.continuity_capture import (
    capture_continuity_candidates,
    commit_continuity_captures,
    withhold_capture_candidates_echo,
)
from alicebot_api.memory_mutations import (
    commit_memory_operations,
    generate_memory_operation_candidates,
    list_memory_operation_candidates,
    list_memory_operations,
)
from alicebot_api.contracts import (
    CONTINUITY_CAPTURE_COMMIT_MODES,
    ContinuityCaptureCandidatesInput,
    ContinuityCaptureCommitInput,
    MemoryOperationCommitInput,
    MemoryOperationGenerateInput,
    MemoryOperationListInput,
)
from alicebot_api.store import JsonObject

from .shared import (
    MCPRuntimeContext,
    _json_object,
    _parse_bool,
    _parse_int,
    _parse_optional_text,
    _parse_optional_uuid,
    _store_context,
)
from .types import MCPArgumentError


def _handle_alice_capture_candidates(
    context: MCPRuntimeContext,
    arguments: Mapping[str, object],
) -> JsonObject:
    with _store_context(context) as store:
        return _json_object(
            withhold_capture_candidates_echo(
                capture_continuity_candidates(
                    store,
                    user_id=context.user_id,
                    request=ContinuityCaptureCandidatesInput(
                        user_content=_parse_optional_text(arguments, "user_content") or "",
                        assistant_content=_parse_optional_text(arguments, "assistant_content") or "",
                        session_id=_parse_optional_text(arguments, "session_id"),
                        source_kind=_parse_optional_text(arguments, "source_kind") or "sync_turn",
                    ),
                )
            ),
        )


def _handle_alice_commit_captures(
    context: MCPRuntimeContext,
    arguments: Mapping[str, object],
) -> JsonObject:
    raw_mode = _parse_optional_text(arguments, "mode") or "assist"
    mode = raw_mode.lower()
    if mode not in CONTINUITY_CAPTURE_COMMIT_MODES:
        allowed = ", ".join(CONTINUITY_CAPTURE_COMMIT_MODES)
        raise MCPArgumentError(f"mode must be one of: {allowed}")

    raw_candidates = arguments.get("candidates", [])
    if not isinstance(raw_candidates, list):
        raise MCPArgumentError("candidates must be a JSON array")
    for item in raw_candidates:
        if not isinstance(item, dict):
            raise MCPArgumentError("each candidate must be a JSON object")

    with _store_context(context) as store:
        return _json_object(
            commit_continuity_captures(
                store,
                user_id=context.user_id,
                request=ContinuityCaptureCommitInput(
                    mode=mode,  # type: ignore[arg-type]
                    candidates=list(raw_candidates),
                    sync_fingerprint=_parse_optional_text(arguments, "sync_fingerprint"),
                    source_kind=_parse_optional_text(arguments, "source_kind") or "sync_turn",
                ),
            ),
        )


def _handle_alice_memory_mutations_generate(
    context: MCPRuntimeContext,
    arguments: Mapping[str, object],
) -> JsonObject:
    raw_mode = _parse_optional_text(arguments, "mode") or "assist"
    mode = raw_mode.lower()
    if mode not in CONTINUITY_CAPTURE_COMMIT_MODES:
        allowed = ", ".join(CONTINUITY_CAPTURE_COMMIT_MODES)
        raise MCPArgumentError(f"mode must be one of: {allowed}")

    with _store_context(context) as store:
        return _json_object(
            generate_memory_operation_candidates(
                store,
                user_id=context.user_id,
                request=MemoryOperationGenerateInput(
                    user_content=_parse_optional_text(arguments, "user_content") or "",
                    assistant_content=_parse_optional_text(arguments, "assistant_content") or "",
                    mode=mode,  # type: ignore[arg-type]
                    sync_fingerprint=_parse_optional_text(arguments, "sync_fingerprint"),
                    source_kind=_parse_optional_text(arguments, "source_kind") or "sync_turn",
                    session_id=_parse_optional_text(arguments, "session_id"),
                    thread_id=_parse_optional_uuid(arguments, "thread_id"),
                    task_id=_parse_optional_uuid(arguments, "task_id"),
                    project=_parse_optional_text(arguments, "project"),
                    person=_parse_optional_text(arguments, "person"),
                    target_continuity_object_id=_parse_optional_uuid(arguments, "target_continuity_object_id"),
                ),
            ),
        )


def _handle_alice_memory_mutations_list_candidates(
    context: MCPRuntimeContext,
    arguments: Mapping[str, object],
) -> JsonObject:
    limit = _parse_int(arguments, key="limit", default=20, minimum=1, maximum=100)
    with _store_context(context) as store:
        return _json_object(
            list_memory_operation_candidates(
                store,
                user_id=context.user_id,
                request=MemoryOperationListInput(
                    limit=limit,
                    policy_action=_parse_optional_text(arguments, "policy_action"),  # type: ignore[arg-type]
                    operation_type=_parse_optional_text(arguments, "operation_type"),  # type: ignore[arg-type]
                    sync_fingerprint=_parse_optional_text(arguments, "sync_fingerprint"),
                ),
            ),
        )


def _handle_alice_memory_mutations_commit(
    context: MCPRuntimeContext,
    arguments: Mapping[str, object],
) -> JsonObject:
    raw_candidate_ids = arguments.get("candidate_ids", [])
    if not isinstance(raw_candidate_ids, list):
        raise MCPArgumentError("candidate_ids must be a JSON array")
    candidate_ids: list[UUID] = []
    for item in raw_candidate_ids:
        if not isinstance(item, str):
            raise MCPArgumentError("candidate_ids must contain UUID strings")
        try:
            candidate_ids.append(UUID(item))
        except ValueError as exc:
            raise MCPArgumentError("candidate_ids must contain UUID strings") from exc

    with _store_context(context) as store:
        return _json_object(
            commit_memory_operations(
                store,
                user_id=context.user_id,
                request=MemoryOperationCommitInput(
                    candidate_ids=candidate_ids,
                    sync_fingerprint=_parse_optional_text(arguments, "sync_fingerprint"),
                    include_review_required=_parse_bool(arguments, key="include_review_required", default=False),
                ),
            ),
        )


def _handle_alice_memory_mutations_list_operations(
    context: MCPRuntimeContext,
    arguments: Mapping[str, object],
) -> JsonObject:
    limit = _parse_int(arguments, key="limit", default=20, minimum=1, maximum=100)
    with _store_context(context) as store:
        return _json_object(
            list_memory_operations(
                store,
                user_id=context.user_id,
                request=MemoryOperationListInput(
                    limit=limit,
                    sync_fingerprint=_parse_optional_text(arguments, "sync_fingerprint"),
                ),
            ),
        )
