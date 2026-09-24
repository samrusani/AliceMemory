"""Mechanical MCP context carrier."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict
from alicebot_api.recall_framing import (
    present_model_item,
    present_model_items,
    with_result_framing,
    writer_for_recent_change,
    writer_for_returned_item,
)
from alicebot_api.store import JsonObject
from alicebot_api.vnext_agent_control import PolicyDecision
from alicebot_api.vnext_context_tree import (
    ContextTreeRequest,
    VNextContextTreeService,
)
from alicebot_api.vnext_repositories import JsonObject as VNextJsonObject
from alicebot_api.vnext_retrieval import (
    MAX_CONTEXT_PACK_ITEMS,
    MAX_CONTEXT_PACK_TOKENS,
    VNextRetrievalRequest,
    VNextRetrievalService,
    estimate_item_tokens,
)
from alicebot_api.vnext_scheduler import SchedulerRunRequest

from .shared import (
    MCPRuntimeContext,
    MCPToolError,
    _agent_identity_from_arguments,
    _json_object,
    _parse_bool,
    _parse_context_pack_tuning,
    _parse_int,
    _parse_memory_types,
    _parse_optional_bool,
    _parse_optional_text,
    _parse_required_text,
    _parse_string_list,
    _policy_checked,
    _raise_mcp_policy_blocked,
    _vnext_store_context,
)

_COMPACT_MEMORY_FIELDS = (
    "id",
    "memory_type",
    "title",
    "canonical_text",
    "summary",
    "status",
    "confidence",
    "domain",
    "sensitivity",
    "project_id",
    "last_seen_at",
    "event_time",
    "supersedes",
    "superseded_by",
    "validity",
    "currency",
    # Attached by the compiler when last_confirmed_at is older than the
    # staleness threshold; agents should weigh flagged memories accordingly.
    "staleness",
)


_COMPACT_OPEN_LOOP_FIELDS = (
    "id",
    "title",
    "description",
    "status",
    "priority",
    "due_at",
    "domain",
    "project_id",
)


# "excerpt" is the payload an agent actually needs: the best-matching chunk of an
# imported document. Without it this list is a bibliography, and the agent can see
# that a relevant document exists while never reading a word of it.
#
# "excerpt_kind" labels it as imported source material rather than as an
# established fact, which is the distinction that lets captured text be useful
# without being believed. Committed memories remain the only channel for facts.
_COMPACT_SOURCE_FIELDS = (
    "id",
    "source_type",
    "title",
    "captured_at",
    "domain",
    "sensitivity",
    "excerpt",
    "excerpt_kind",
)


def _compact_fields(item: object, fields: tuple[str, ...]) -> JsonObject:
    if not isinstance(item, Mapping):
        return {}
    return _json_object({key: item[key] for key in fields if item.get(key) is not None})


def _compact_items(items: object, fields: tuple[str, ...]) -> list[JsonObject]:
    if not isinstance(items, list):
        return []
    return [_compact_fields(item, fields) for item in items]


def _present_stored_rows(items: object) -> list[object]:
    """Quote a debug section of stored rows and attach each writer."""

    if not isinstance(items, list):
        return []
    presented: list[object] = []
    for item in items:
        if not isinstance(item, Mapping):
            presented.append(item)
            continue
        presented.append(present_model_item(item, source=item, writer=_stamped_writer(item)))
    return presented


def _present_compact_items(items: object, fields: tuple[str, ...]) -> list[JsonObject]:
    """Compact a pack section, then quote its text and attach writer.

    Writer is read from the pre-compact row. For a memory, that is the
    latest text-changing revision, not the identity left on the original
    commit. The compact copy is what the tool returns.
    """

    if not isinstance(items, list):
        return []
    presented: list[JsonObject] = []
    for item in items:
        if not isinstance(item, Mapping):
            presented.append(present_model_item(_compact_fields(item, fields)))
            continue
        presented.append(
            present_model_item(
                _compact_fields(item, fields),
                source=item,
                writer=_stamped_writer(item),
            )
        )
    return presented


def _stamped_writer(item: Mapping[str, object]) -> dict[str, str] | None:
    writer = item.get("writer")
    if not isinstance(writer, Mapping):
        return None
    writer_id = writer.get("id")
    established = writer.get("established")
    if not isinstance(writer_id, str) or not isinstance(established, str):
        return None
    return {"id": writer_id, "established": established}


def _stamp_pack_writers(store: object, pack: Mapping[str, object]) -> None:
    """Attach writer on the compiled pack after the budget has already run."""

    for section in (
        "relevant_memories",
        "decisions",
        "procedures",
        "relevant_beliefs",
        "current_known_state",
        "open_loops",
        "sources",
    ):
        rows = pack.get(section)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and "writer" not in row:
                row["writer"] = writer_for_returned_item(store, row)
    changes = pack.get("recent_changes")
    if isinstance(changes, list):
        for change in changes:
            if isinstance(change, dict) and "writer" not in change:
                change["writer"] = writer_for_recent_change(store, change)


def _handle_alice_context_pack(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    debug = _parse_bool(arguments, key="debug", default=False)
    pack = _vnext_context_pack_payload(context, arguments)
    interpretation = pack.get("query_interpretation")
    if not isinstance(interpretation, Mapping):
        interpretation = {}
    return _frame_context_pack_tool(pack, interpretation, debug=debug)


def _frame_context_pack_tool(
    pack: Mapping[str, object],
    interpretation: Mapping[str, object],
    *,
    debug: bool,
) -> JsonObject:
    payload: dict[str, object] = {
        "context_pack_id": pack.get("context_pack_id"),
        "query": interpretation.get("query"),
        "query_type": interpretation.get("query_type"),
        "memories": _present_compact_items(pack.get("relevant_memories"), _COMPACT_MEMORY_FIELDS),
        "open_loops": _present_compact_items(pack.get("open_loops"), _COMPACT_OPEN_LOOP_FIELDS),
        "sources": _present_compact_items(pack.get("sources"), _COMPACT_SOURCE_FIELDS),
        "supporting_evidence": present_model_items(pack.get("supporting_evidence", [])),
        "missing_information": pack.get("missing_information", []),
        "warnings": pack.get("warnings", []),
        "trace_id": pack.get("trace_id"),
    }
    # Sections that cannot be reconstructed from the memory rows themselves;
    # typed groupings (procedures/decisions/beliefs) are omitted here because
    # every compact memory row already carries memory_type.
    entities = pack.get("entities")
    if isinstance(entities, list) and entities:
        # Already compact ({id, name, entity_type, mention_count}): the
        # entities the query resolved to, i.e. who the pack is about.
        payload["entities"] = entities
    contradictions = pack.get("contradicting_evidence")
    if isinstance(contradictions, list) and contradictions:
        payload["contradicting_evidence"] = present_model_items(contradictions)
    recent_changes = pack.get("recent_changes")
    if isinstance(recent_changes, list) and recent_changes:
        payload["recent_changes"] = [
            present_model_item(
                change,
                source=change,
                writer=_stamped_writer(change),
            )
            if isinstance(change, Mapping)
            else change
            for change in recent_changes
        ]
    supersession_context = pack.get("supersession_context")
    if isinstance(supersession_context, list) and supersession_context:
        payload["supersession_context"] = present_model_items(supersession_context)
    derived_values = pack.get("derived_values")
    if isinstance(derived_values, Mapping) and derived_values:
        # Deterministic temporal computations are not reconstructable from the
        # compact memory rows alone (sources can also contribute event times),
        # so keep the compiler's machine-readable result in the compact view.
        payload["derived_values"] = dict(derived_values)
    # -- entity grounding passthrough (vnext_grounding; single block) -------
    # pack["grounding"] exists only when a salient query entity has ZERO
    # corpus support (see vnext_grounding); the compact view must not
    # silently drop it or the honesty statistic never reaches the tool
    # caller. Absent for every ungated query, so ordinary responses are
    # byte-identical.
    grounding = pack.get("grounding")
    if isinstance(grounding, Mapping) and grounding:
        payload["grounding"] = dict(grounding)
    # -- end entity grounding passthrough ------------------------------------
    if debug:
        payload["query_interpretation"] = dict(interpretation)
        payload["trace"] = pack.get("trace")
        for section in ("procedures", "decisions", "relevant_beliefs", "current_known_state"):
            payload[section] = _present_stored_rows(pack.get(section, []))
    framed = with_result_framing(payload)
    _attach_compact_context_pack_token_report(framed, pack)
    return _json_object(framed)


_TOKEN_REPORT_FIELDS = (
    "token_budget",
    "token_estimate",
    "truncated",
    "dropped_item_count",
    "scope",
    "serialized_token_estimate",
    "excluded_token_estimate",
    "excluded_sections",
    "is_transport_cap",
)


def _context_pack_token_report(pack: Mapping[str, object]) -> JsonObject:
    """Extract the compiler's token-budget report from a context pack.

    Accepts either a nested ``token_report`` object or the report fields at
    the top level of the pack, and returns ``{}`` when the compiler did not
    report a budget.
    """
    nested = pack.get("token_report")
    if not isinstance(nested, Mapping):
        nested = pack.get("budget")
    if isinstance(nested, Mapping):
        return _json_object({key: nested[key] for key in _TOKEN_REPORT_FIELDS if key in nested})
    return _json_object({key: pack[key] for key in _TOKEN_REPORT_FIELDS if key in pack})


def _attach_compact_context_pack_token_report(
    payload: dict[str, object],
    pack: Mapping[str, object],
) -> None:
    """Attach estimates whose names match the payload they measure.

    The compiler estimates its full context-pack envelope.  ``alice_context_pack``
    returns a smaller, compact projection, so forwarding that estimate as
    ``serialized_token_estimate`` misdescribes the tool result.  Preserve the
    compiler figures under explicit ``full_pack_*`` names and calculate the
    compact result's estimate after every optional response section is present.
    """

    report = _context_pack_token_report(pack)
    if not report:
        return

    full_pack_estimate = report.pop("serialized_token_estimate", None)
    if full_pack_estimate is not None:
        report["full_pack_serialized_token_estimate"] = full_pack_estimate
    full_pack_excluded_estimate = report.pop("excluded_token_estimate", None)
    if full_pack_excluded_estimate is not None:
        report["full_pack_excluded_token_estimate"] = full_pack_excluded_estimate

    report["serialized_token_estimate_scope"] = "compact_mcp_tool_payload"
    report["serialized_token_estimate"] = 0
    payload["token_report"] = report
    # The estimate is part of the object being estimated. Iterate to a fixed
    # point so crossing an integer digit boundary cannot leave a stale value.
    for _attempt in range(32):
        compact_estimate = estimate_item_tokens(payload)
        if report["serialized_token_estimate"] == compact_estimate:
            return
        report["serialized_token_estimate"] = compact_estimate
    raise RuntimeError("compact MCP context-pack token estimate did not converge")


class _ContextPackRequestKwargs(TypedDict, total=False):
    memory_types: tuple[str, ...]
    created_by_agent_ids: tuple[str, ...]


def _vnext_context_pack_payload(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    max_items = _parse_int(
        arguments,
        key="max_items",
        default=8,
        minimum=1,
        maximum=MAX_CONTEXT_PACK_ITEMS,
    )
    max_tokens = _parse_int(
        arguments,
        key="max_tokens",
        default=8000,
        minimum=500,
        maximum=MAX_CONTEXT_PACK_TOKENS,
    )
    sensitivity_allowed = _parse_string_list(arguments, "sensitivity_allowed") or (
        "public",
        "internal",
        "private",
        "unknown",
    )
    identity = _agent_identity_from_arguments(context, arguments)

    blocked_decision: PolicyDecision | None = None
    scheduler_request: SchedulerRunRequest | None = None
    with _vnext_store_context(context) as store:
        actor_type, actor_id, decision = _policy_checked(
            store,
            identity=identity,
            action="context_pack.request",
            domains=_parse_string_list(arguments, "domains"),
            sensitivity_allowed=sensitivity_allowed,
            project_scope=_parse_string_list(arguments, "project_scope") or _parse_string_list(arguments, "projects"),
        )
        if decision.decision == "blocked":
            blocked_decision = decision
        else:
            request_kwargs: _ContextPackRequestKwargs = {}
            memory_types = _parse_memory_types(arguments)
            if memory_types:
                # Forwarded only when requested so the retrieval request
                # dataclass stays the source of truth for the default ().
                request_kwargs["memory_types"] = memory_types
            created_by_agents = _parse_string_list(arguments, "created_by_agents")
            if created_by_agents:
                request_kwargs["created_by_agent_ids"] = created_by_agents
            context_depth, budget_strategy = _parse_context_pack_tuning(arguments)
            payload = VNextRetrievalService(store).compile_context_pack(
                VNextRetrievalRequest(
                    query=_parse_required_text(arguments, "query"),
                    domains=decision.effective_domains,
                    projects=decision.effective_project_scope,
                    people=_parse_string_list(arguments, "people"),
                    time_window=_parse_optional_text(arguments, "time_window") or "all",
                    sensitivity_allowed=decision.effective_sensitivity_allowed,
                    # Tri-state: absent means "let the context_depth tier
                    # decide"; an explicit true/false always wins.
                    include_sources=_parse_optional_bool(arguments, key="include_sources"),
                    include_contradictions=_parse_optional_bool(arguments, key="include_contradictions"),
                    context_depth=context_depth,
                    budget_strategy=budget_strategy,
                    max_items=max_items,
                    max_tokens=max_tokens,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    agent_identity=identity.to_record() if identity is not None else None,
                    policy_decision=decision.to_record(),
                    trace_id=_parse_optional_text(arguments, "trace_id") or decision.trace_id,
                    run_id=identity.agent_run_id if identity is not None else None,
                    **request_kwargs,
                )
            )
            _stamp_pack_writers(store, payload)
    if blocked_decision is not None:
        _raise_mcp_policy_blocked(blocked_decision)
    if payload is None:
        raise MCPToolError("vNext context-pack request did not complete")
    return _json_object(payload)


def _handle_alice_vnext_context_pack(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    return _vnext_context_pack_payload(context, arguments)


def _handle_alice_vnext_context_tree(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> JsonObject:
    limit = _parse_int(arguments, key="limit", default=12, minimum=1, maximum=50)
    sensitivity_allowed = _parse_string_list(arguments, "sensitivity_allowed") or (
        "public",
        "internal",
        "private",
        "unknown",
    )
    identity = _agent_identity_from_arguments(context, arguments)

    blocked_decision: PolicyDecision | None = None
    payload: VNextJsonObject | None = None
    with _vnext_store_context(context) as store:
        actor_type, _actor_id, decision = _policy_checked(
            store,
            identity=identity,
            action="context_pack.request",
            domains=_parse_string_list(arguments, "domains"),
            sensitivity_allowed=sensitivity_allowed,
            project_scope=_parse_string_list(arguments, "project_scope") or _parse_string_list(arguments, "projects"),
        )
        if decision.decision == "blocked":
            blocked_decision = decision
        else:
            payload = VNextContextTreeService(store).build_tree(
                ContextTreeRequest(
                    query=_parse_optional_text(arguments, "query") or "",
                    domains=decision.effective_domains,
                    projects=decision.effective_project_scope,
                    sensitivity_allowed=decision.effective_sensitivity_allowed,
                    limit=limit,
                    include_events=_parse_bool(arguments, key="include_events", default=True),
                    generated_by=actor_type,
                    agent_identity=identity.to_record() if identity is not None else None,
                    policy_decision=decision.to_record(),
                    trace_id=_parse_optional_text(arguments, "trace_id") or decision.trace_id,
                )
            )
    if blocked_decision is not None:
        _raise_mcp_policy_blocked(blocked_decision)
    if payload is None:
        raise MCPToolError("vNext context-tree request did not complete")
    return _json_object(payload)
