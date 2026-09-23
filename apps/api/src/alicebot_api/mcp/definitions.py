"""Mechanical MCP definitions carrier."""

from __future__ import annotations

from alicebot_api.contracts import (
    CONTINUITY_CAPTURE_CANDIDATE_TYPES,
    CONTINUITY_CAPTURE_COMMIT_MODES,
    CONTINUITY_BRIEF_TYPE_ORDER,
    CONTRADICTION_RESOLUTION_ACTIONS,
    CONTINUITY_OBJECT_TYPES,
    MAX_CONTINUITY_BRIEF_CONFLICT_LIMIT,
    MAX_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT,
    MAX_CONTINUITY_BRIEF_TIMELINE_LIMIT,
    MAX_CONTINUITY_RECALL_LIMIT,
    MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
    MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
    MAX_TASK_BRIEF_TOKEN_BUDGET,
    MAX_CONTINUITY_REVIEW_LIMIT,
    MAX_TEMPORAL_TIMELINE_LIMIT,
    MEMORY_TRUST_CLASSES,
)
from alicebot_api.vnext_memory_commit import (
    VNEXT_DOMAINS,
    VNEXT_MEMORY_TYPES,
    VNEXT_SENSITIVITY_LEVELS,
)
from alicebot_api.vnext_retrieval import (
    BUDGET_STRATEGIES,
    CONTEXT_DEPTHS,
    MAX_CONTEXT_PACK_ITEMS,
    MAX_CONTEXT_PACK_TOKENS,
    MAX_CONTEXT_SCOPE_VALUES,
    MAX_TIME_WINDOW_DAYS,
)

from .memories import _MEMORY_MANAGE_ACTIONS
from .shared import (
    _MODEL_GENERATION_SCHEMA_PROPERTIES,
    _OPEN_LOOP_TOOL_ACTIONS,
    _PROVENANCE_EVIDENCE_ROLES,
    _RECALL_MAX_LIMIT,
    _REVIEW_APPLY_ACTION_CHOICES,
    _REVIEW_STATUS_CHOICES,
)

_VNEXT_AGENT_SCHEMA_PROPERTIES: dict[str, object] = {
    "agent_id": {"type": "string"},
    "agent_type": {"type": "string"},
    "agent_run_id": {"type": "string"},
    "task_id": {"type": "string"},
    "project_scope": {"type": "array", "items": {"type": "string"}},
    "permission_profile": {"type": "string"},
    "trace_id": {"type": "string"},
    "domains": {"type": "array", "items": {"type": "string"}},
    "sensitivity_allowed": {"type": "array", "items": {"type": "string"}},
}


def _vnext_agent_tool_schema(
    properties: dict[str, object] | None = None,
    *,
    required: list[str] | None = None,
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required or [],
        "properties": {**_VNEXT_AGENT_SCHEMA_PROPERTIES, **(properties or {})},
    }


_AGENT_IDENTITY_SCHEMA_PROPERTIES: dict[str, object] = {
    "agent_identity": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "agent_id": {"type": "string"},
            "agent_type": {"type": "string"},
            "agent_run_id": {"type": "string"},
            "task_id": {"type": "string"},
            "project_scope": {"type": "array", "items": {"type": "string"}},
            "permission_profile": {"type": "string"},
            "trace_id": {"type": "string"},
        },
        "description": "Optional nested caller identity; flat identity fields remain supported for compatibility.",
    },
    "agent_id": {
        "type": "string",
        "description": "Stable identifier of the calling agent, for example 'hermes'. Omit when a human calls directly.",
    },
    "agent_type": {
        "type": "string",
        "description": "Category of the calling agent, such as 'coding_agent' or 'personal_assistant'.",
    },
    "agent_run_id": {
        "type": "string",
        "description": "Identifier of the agent's current run, recorded in the audit log.",
    },
    "task_id": {
        "type": "string",
        "description": "Identifier of the task the agent is working on, recorded in the audit log.",
    },
    "project_scope": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Project names the agent may access; requests outside this scope are filtered or blocked.",
    },
    "permission_profile": {
        "type": "string",
        "description": (
            "Named permission level for the agent, such as 'trusted_local_agent' or "
            "'project_scoped_agent'. Unknown agents default to 'read_only_agent', which "
            "cannot write memories."
        ),
    },
    "trace_id": {
        "type": "string",
        "description": "Correlation id used to link this call with other logged events.",
    },
}


_DOMAINS_FILTER_SCHEMA: dict[str, object] = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Restrict to these life or work areas, such as 'project', 'professional', or 'personal'.",
}


_MEMORY_TYPES_FILTER_SCHEMA: dict[str, object] = {
    "type": "array",
    "items": {"type": "string", "enum": list(VNEXT_MEMORY_TYPES)},
    "description": "Restrict to these memory types, such as 'decision', 'preference', or 'procedure'. Empty means all types.",
}


_SENSITIVITY_ALLOWED_SCHEMA: dict[str, object] = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Sensitivity levels the caller may see. Defaults to public, internal, private, and unknown.",
}


_CORRECTION_BODY_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "minProperties": 1,
    "properties": {
        "text": {"type": "string"},
        "body": {"type": "string"},
        "fact_text": {"type": "string"},
        "decision_text": {"type": "string"},
        "commitment_text": {"type": "string"},
        "waiting_for_text": {"type": "string"},
        "blocking_reason": {"type": "string"},
        "action_text": {"type": "string"},
        "raw_content": {"type": "string"},
        "explicit_signal": {"type": ["string", "null"]},
    },
}


_REVIEW_PROVENANCE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["source_id"],
    "properties": {
        "source_id": {"type": "string", "format": "uuid"},
        "source_chunk_id": {"type": "string", "format": "uuid"},
        "evidence_role": {"type": "string", "enum": list(_PROVENANCE_EVIDENCE_ROLES)},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "quote": {"type": "string"},
    },
}


_CONTINUITY_PROVENANCE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "minProperties": 1,
    "properties": {
        "thread_id": {"type": "string", "format": "uuid"},
        "task_id": {"type": "string", "format": "uuid"},
        "project": {"type": "string"},
        "person": {"type": "string"},
        "source_event_ids": {"type": "array", "items": {"type": "string"}},
        "capture_event_id": {"type": "string"},
        "confirmation_status": {"type": "string"},
        "source_kind": {"type": "string"},
        "source_label": {"type": "string"},
        "artifact_id": {"type": "string"},
        "artifact_segment_id": {"type": "string"},
    },
}


_CONTINUITY_CAPTURE_CANDIDATE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "candidate_id",
        "candidate_type",
        "object_type",
        "normalized_text",
        "confidence",
        "trust_class",
        "evidence_snippet",
        "explicit",
        "source_role",
        "admission_reason",
        "proposed_action",
    ],
    "properties": {
        "candidate_id": {"type": "string"},
        "candidate_type": {"type": "string", "enum": list(CONTINUITY_CAPTURE_CANDIDATE_TYPES)},
        "object_type": {"type": ["string", "null"], "enum": [*CONTINUITY_OBJECT_TYPES, None]},
        "normalized_text": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "trust_class": {"type": "string", "enum": list(MEMORY_TRUST_CLASSES)},
        "evidence_snippet": {"type": "string"},
        "explicit": {"type": "boolean"},
        "source_role": {"type": "string"},
        "admission_reason": {"type": "string"},
        "proposed_action": {
            "type": "string",
            "enum": ["auto_save_candidate", "queue_for_review", "no_op"],
        },
    },
}


_CORE_TOOL_DEFINITIONS: list[dict[str, object]] = [
    {
        "name": "alice_capture",
        "description": (
            "Store a source document or raw note. The text is kept verbatim with provenance and "
            "split into searchable chunks, and alice_recall and alice_context_pack return its "
            "matching passages from the next call onward, under 'sources' and labelled as "
            "material to read and quote rather than as facts Alice asserts. Capture also proposes "
            "candidate memories; those stay unsearchable until a reviewer promotes them, so do "
            "not tell the user their import is unusable until they clear a queue, and do not "
            "re-capture the same document because recall returned no memories for it. Use this "
            "for transcripts, files, notes and pasted material. To assert a fact Alice should "
            "state in its own voice, use alice_memory_commit instead."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["raw_text"],
            "properties": {
                "raw_text": {
                    "type": "string",
                    "description": "The text to capture. Stored verbatim as source evidence.",
                },
                "title": {
                    "type": "string",
                    "description": "Short human-readable title for the captured text.",
                },
                "domain": {
                    "type": "string",
                    "enum": list(VNEXT_DOMAINS),
                    "description": "Life or work area this belongs to, such as 'project', 'professional', or 'personal'. Defaults to 'unknown'.",
                },
                "sensitivity": {
                    "type": "string",
                    "enum": list(VNEXT_SENSITIVITY_LEVELS),
                    "description": "How sensitive the content is: 'public', 'internal', 'private', or 'unknown' (default).",
                },
                **_AGENT_IDENTITY_SCHEMA_PROPERTIES,
            },
        },
    },
    {
        "name": "alice_memory_commit",
        "description": (
            "Record one fact as durable, immediately recallable memory. Use this whenever you learn something worth keeping, including when the user has not asked you to remember it. This is the write verb for ordinary memory. The write "
            "is policy-checked, never blind: the outcome is 'committed', 'confirmation_required', "
            "'review_required' (waits for human review), or 'rejected'. A write above this agent's "
            "sensitivity ceiling is rejected: This was not saved. Do not retry with a lower "
            "sensitivity label. Tell the user. The owner can raise this agent's clearance or "
            "store the memory themselves. A new write needs title "
            "and canonical_text. On 'confirmation_required' the fact is not stored yet: ask the "
            "user, showing them the proposed text. Then call this tool again with "
            "confirmation_id, confirmation_action ('confirm' if they agreed, 'reject' if they "
            "did not) and the same identity fields you sent with the write, and no memory "
            "fields. Alice cannot tell whether you asked, so never answer for the user. To "
            "change the text, reject it and commit the corrected text as a new write. After 24 "
            "hours a pending write can no longer be confirmed on this tool: the next confirm or "
            "reject here that passes the policy check resolves it to 'rejected'. A refusal writes "
            "agent.memory_commit_rejected and does not save a memory row, a revision, or provenance. "
            "An agent ceiling refusal also writes policy.decision and agent.policy_filtered, unless "
            "the policy decision is already blocked, which writes agent.policy_blocked instead. "
            "For source documents and raw notes use "
            "alice_capture instead."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short human-readable title for the memory. Required for a new write; leave it out when sending confirmation_id.",
                },
                "canonical_text": {
                    "type": "string",
                    "description": "The memory content, phrased as a standalone statement. Required for a new write; leave it out when sending confirmation_id.",
                },
                "memory_type": {
                    "type": "string",
                    "enum": list(VNEXT_MEMORY_TYPES),
                    "description": "What kind of memory this is, such as 'preference', 'decision', or 'procedure'. Defaults to 'semantic'.",
                },
                "domain": {
                    "type": "string",
                    "enum": list(VNEXT_DOMAINS),
                    "description": "Life or work area this belongs to. Sensitive domains such as 'health' require inline confirmation. Defaults to 'unknown'.",
                },
                "sensitivity": {
                    "type": "string",
                    "enum": list(VNEXT_SENSITIVITY_LEVELS),
                    "description": "How sensitive the content is. Above the caller's ceiling the commit is rejected and nothing is saved. The owner and an admin key still confirm levels above private. Defaults to 'unknown'.",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "How certain the caller is, 0 to 1. Below 0.5 routes to review; below 0.85 requires confirmation. Defaults to 0.9.",
                },
                "source_type": {
                    "type": "string",
                    "description": "Where the content came from. Defaults to 'direct_user_instruction'; external sources such as 'email' or 'web_page' route to review.",
                },
                "source_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ids or URLs of supporting sources, stored as provenance links.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why this memory is being committed, or why a pending write is being confirmed or rejected. Stored in the audit trail.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Unique key that makes retries safe; a replay returns the original result.",
                },
                "confirmation_id": {
                    "type": "string",
                    "description": (
                        "Finishes a pending write: the confirmation_id from an earlier "
                        "'confirmation_required' result. Send it with confirmation_action and "
                        "the same identity fields as the write, without title, canonical_text "
                        "or any other memory field. Ask the user first."
                    ),
                },
                "confirmation_action": {
                    "type": "string",
                    "enum": ["confirm", "reject"],
                    "description": (
                        "Required with confirmation_id, and it must be the user's answer. "
                        "'confirm' stores the pending text as a recallable fact; 'reject' "
                        "discards it. Both are policy-checked like a write: a read-only "
                        "identity or a key bound to another project is refused (a keyless server "
                        "does not check a declared project_scope). Only the agent that authored "
                        "the pending write, an admin_agent key, or the owner (a keyless call with "
                        "no agent identity) can confirm or reject it. On a keyless install that "
                        "limit is not protection: the caller can declare the author's agent_id. "
                        "The author can reject their own pending write even when it is above their "
                        "sensitivity ceiling. Confirming a write that is no longer pending is refused."
                    ),
                },
                **_AGENT_IDENTITY_SCHEMA_PROPERTIES,
            },
        },
    },
    {
        "name": "alice_recall",
        "description": (
            "Search Alice's memory. Runs full-text and semantic vector search over stored "
            "memories and merges both rankings (reciprocal-rank fusion); falls back to "
            "full-text only when no embedding endpoint is configured. Returns compact matches "
            "with relevance scores and provenance counts under 'results'. Also searches "
            "captured documents and returns their matching passages under 'sources', with "
            "an excerpt you can read and quote. The two are separate on purpose: 'results' "
            "are facts Alice asserts, 'sources' are material the user imported. An empty "
            "'results' with a non-empty 'sources' is a normal, useful answer, not a miss."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                **_AGENT_IDENTITY_SCHEMA_PROPERTIES,
                "query": {
                    "type": "string",
                    "description": "What to search for, in natural language or keywords.",
                },
                "include_sources": {
                    "type": "boolean",
                    "description": (
                        "Include matching passages from captured documents under "
                        "'sources'. Defaults to true; set false only to search "
                        "asserted memories alone."
                    ),
                },
                "domains": _DOMAINS_FILTER_SCHEMA,
                "memory_types": _MEMORY_TYPES_FILTER_SCHEMA,
                "projects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Restrict results to memories scoped to these project names.",
                },
                "project": {
                    "type": "string",
                    "description": "Restrict results to one project name; equivalent to a one-item projects array.",
                },
                "people": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Restrict results to memories linked to or scoped to these people.",
                },
                "person": {
                    "type": "string",
                    "description": "Restrict results to one person; equivalent to a one-item people array.",
                },
                "thread_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Restrict results to memories attributed to this conversation thread UUID.",
                },
                "task_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Restrict results to memories attributed to this task UUID.",
                },
                "since": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Only return memories created or observed at or after this ISO-8601 timestamp.",
                },
                "until": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Only return memories created or observed at or before this ISO-8601 timestamp.",
                },
                "created_by_agents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Restrict results to memories committed by these agent ids "
                        "(for example ['openclaw']). Omit to search memories from every writer."
                    ),
                },
                "sensitivity_allowed": _SENSITIVITY_ALLOWED_SCHEMA,
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _RECALL_MAX_LIMIT,
                    "description": "Maximum number of results to return. Defaults to 8.",
                },
                "context_depth": {
                    "type": "string",
                    "enum": list(CONTEXT_DEPTHS),
                    "description": "Cost/coverage tier: 'minimal' runs full-text search only and caps results at 4, 'low' (default) adds vector and entity-graph stages, 'medium' and 'high' match the context-pack tiers.",
                },
                "budget_strategy": {
                    "type": "string",
                    "enum": list(BUDGET_STRATEGIES),
                    "description": "How to order results: 'balanced' (default) keeps fused relevance order, 'facts_first' boosts semantic/decision/preference memories, 'recent_first' orders by recency; 'contradictions_first' and 'sources_first' match the context-pack strategies and keep fused order here.",
                },
                "debug": {
                    "type": "boolean",
                    "description": "When true, include a retrieval trace showing which search stages ran and why vector search was on or off.",
                },
            },
        },
    },
    {
        "name": "alice_resume",
        "description": (
            "Get a brief for picking work back up: the last recorded decision, the suggested "
            "next action, open loops, and recent changes, optionally scoped to a project, "
            "person, or conversation thread."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                **_AGENT_IDENTITY_SCHEMA_PROPERTIES,
                "query": {
                    "type": "string",
                    "description": "Free-text topic to focus the brief on.",
                },
                "thread_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Limit the brief to one conversation thread (UUID).",
                },
                "task_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Limit the brief to one task (UUID).",
                },
                "project": {
                    "type": "string",
                    "description": "Limit the brief to one project name.",
                },
                "person": {
                    "type": "string",
                    "description": "Limit the brief to one person's name.",
                },
                "since": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Only include items from at or after this ISO-8601 timestamp.",
                },
                "until": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Only include items from at or before this ISO-8601 timestamp.",
                },
                "max_recent_changes": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
                    "description": "Maximum number of recent changes to include.",
                },
                "max_open_loops": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
                    "description": "Maximum number of open loops to include.",
                },
                "include_non_promotable_facts": {
                    "type": "boolean",
                    "description": "When true, also include captured facts that were not approved for reuse.",
                },
                "debug": {
                    "type": "boolean",
                    "description": "When true, attach the underlying retrieval trace to the brief.",
                },
                "domains": _DOMAINS_FILTER_SCHEMA,
                "sensitivity_allowed": _SENSITIVITY_ALLOWED_SCHEMA,
            },
        },
    },
    {
        "name": "alice_context_pack",
        "description": (
            "Build a scoped context bundle for a task: the most relevant memories, open loops, "
            "and source documents for a query, with supporting evidence. Use this to brief an "
            "agent before it starts work."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The task or question the context should support.",
                },
                "domains": _DOMAINS_FILTER_SCHEMA,
                "memory_types": _MEMORY_TYPES_FILTER_SCHEMA,
                "projects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_CONTEXT_SCOPE_VALUES,
                    "description": "Restrict every context section to rows scoped to these project ids or names.",
                },
                "created_by_agents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Restrict the memory sections to memories committed by these agent ids. "
                        "Omit to build the pack from every writer's memories."
                    ),
                },
                "people": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_CONTEXT_SCOPE_VALUES,
                    "description": "Restrict every context section to these person ids or names.",
                },
                "time_window": {
                    "type": "string",
                    "pattern": r"^(all|(?:[1-9]|[1-9][0-9]{1,2}|[12][0-9]{3}|3[0-5][0-9]{2}|36[0-4][0-9]|3650)d)$",
                    "description": f"Restrict every context section to 'all' (default) or the last Nd, from 1d through {MAX_TIME_WINDOW_DAYS}d.",
                },
                "sensitivity_allowed": _SENSITIVITY_ALLOWED_SCHEMA,
                "include_sources": {
                    "type": "boolean",
                    "description": "Include matching source documents. Omit to let context_depth decide (on for low/medium/high, off for minimal); an explicit true or false always wins over the tier default.",
                },
                "include_contradictions": {
                    "type": "boolean",
                    "description": "Include known contradicting evidence when relevant. Omit to let context_depth decide (on for low/medium/high, off for minimal); an explicit true or false always wins over the tier default.",
                },
                "context_depth": {
                    "type": "string",
                    "enum": list(CONTEXT_DEPTHS),
                    "description": "Cost/coverage tier: 'minimal' runs full-text only with at most 4 items, 'low' (default) is the standard hybrid retrieval, 'medium' adds fuller sections, 'high' also walks supersession chains. No tier performs LLM synthesis.",
                },
                "budget_strategy": {
                    "type": "string",
                    "enum": list(BUDGET_STRATEGIES),
                    "description": "How the token budget is spent when max_tokens is tight: 'balanced' (default), 'facts_first' boosts semantic/decision/preference memories, 'recent_first' orders memories by recency, 'contradictions_first' packs contradicting evidence before memories, 'sources_first' packs source documents before memories.",
                },
                "max_items": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_CONTEXT_PACK_ITEMS,
                    "description": "Maximum number of memories to include. Defaults to 8.",
                },
                "max_tokens": {
                    "type": "integer",
                    "minimum": 500,
                    "maximum": MAX_CONTEXT_PACK_TOKENS,
                    "description": "Content-section token budget for the pack. Lowest-ranked content is dropped to fit; diagnostic/navigation envelope fields are excluded. token_report.serialized_token_estimate measures this compact MCP result, while full_pack_serialized_token_estimate preserves the compiler's complete-pack estimate. Defaults to 8000.",
                },
                "debug": {
                    "type": "boolean",
                    "description": "When true, include the full retrieval trace and query interpretation.",
                },
                **_AGENT_IDENTITY_SCHEMA_PROPERTIES,
            },
        },
    },
    {
        "name": "alice_open_loops",
        "description": (
            "List or manage open loops: unresolved tasks, blockers, and follow-ups. The default "
            "action 'list' returns current loops; 'close', 'snooze', 'edit', and 'reopen' "
            "update one loop by id."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_OPEN_LOOP_TOOL_ACTIONS),
                    "description": "What to do: 'list' (default) to read loops, or 'close', 'snooze', 'edit', 'reopen' to change one loop.",
                },
                "status": {
                    "type": "string",
                    "description": "For 'list': filter by loop status such as 'open' (default), 'resolved', or 'all'.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "For 'list': maximum number of loops to return. Defaults to 20.",
                },
                "domains": _DOMAINS_FILTER_SCHEMA,
                "sensitivity_allowed": _SENSITIVITY_ALLOWED_SCHEMA,
                "loop_id": {
                    "type": "string",
                    "description": "Id of the loop to change. Required for every action except 'list'.",
                },
                "title": {
                    "type": "string",
                    "description": "For 'edit': new title for the loop.",
                },
                "description": {
                    "type": "string",
                    "description": "For 'edit': new description for the loop.",
                },
                "due_at": {
                    "type": "string",
                    "description": "For 'snooze' (required) or 'edit': new due timestamp, ISO-8601.",
                },
                "priority": {
                    "type": "string",
                    "description": "For 'edit': new priority label, such as 'high'.",
                },
                "resolution_note": {
                    "type": "string",
                    "description": "For 'close': short note recording how the loop was resolved.",
                },
                **_AGENT_IDENTITY_SCHEMA_PROPERTIES,
            },
        },
    },
    {
        "name": "alice_recent_decisions",
        "description": (
            "List the most recent recorded decisions, newest first, optionally filtered by "
            "project, person, thread, or time window."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                **_AGENT_IDENTITY_SCHEMA_PROPERTIES,
                "query": {
                    "type": "string",
                    "description": "Free-text filter for which decisions to return.",
                },
                "thread_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Limit results to one conversation thread (UUID).",
                },
                "task_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Limit results to one task (UUID).",
                },
                "project": {
                    "type": "string",
                    "description": "Limit results to one project name.",
                },
                "person": {
                    "type": "string",
                    "description": "Limit results to one person's name.",
                },
                "since": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Only include decisions recorded at or after this ISO-8601 timestamp.",
                },
                "until": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Only include decisions recorded at or before this ISO-8601 timestamp.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_CONTINUITY_RECALL_LIMIT,
                    "description": "Maximum number of decisions to return.",
                },
                "domains": _DOMAINS_FILTER_SCHEMA,
                "sensitivity_allowed": _SENSITIVITY_ALLOWED_SCHEMA,
            },
        },
    },
    {
        "name": "alice_memory_review",
        "description": (
            "Inspect the memory review queue. Without an id it lists items awaiting human "
            "review; with review_item_id it returns full detail for one item, including why it "
            "was flagged. Use alice_memory_correct to act on an item."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "review_item_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "UUID of one review item to inspect in detail.",
                },
                "continuity_object_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Alias for review_item_id; both refer to the stored memory record's UUID.",
                },
                "status": {
                    "type": "string",
                    "enum": list(_REVIEW_STATUS_CHOICES),
                    "description": "Which queue slice to list, such as 'pending_review' or 'correction_ready' (default). Use 'all' for everything.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_CONTINUITY_REVIEW_LIMIT,
                    "description": "Maximum number of queue items to return.",
                },
                "domains": _DOMAINS_FILTER_SCHEMA,
                "projects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Restrict review items to these persisted project ids or names.",
                },
                "sensitivity_allowed": _SENSITIVITY_ALLOWED_SCHEMA,
                **_AGENT_IDENTITY_SCHEMA_PROPERTIES,
            },
        },
    },
    {
        "name": "alice_memory_correct",
        "description": (
            "Propose a correction to an existing memory: approve it as-is, edit and approve, "
            "reject it, or supersede it with a replacement. Every change keeps an audit trail."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["action"],
            "properties": {
                **_AGENT_IDENTITY_SCHEMA_PROPERTIES,
                "review_item_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "UUID of the memory record to act on. Provide this or continuity_object_id.",
                },
                "continuity_object_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Alias for review_item_id; both refer to the same memory record UUID.",
                },
                "action": {
                    "type": "string",
                    "enum": list(_REVIEW_APPLY_ACTION_CHOICES),
                    "description": "What to do with the memory: 'approve', 'edit-and-approve', 'reject', or 'supersede-existing'.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why the change is being made. Stored in the audit trail.",
                },
                "title": {
                    "type": "string",
                    "description": "For edit-and-approve: corrected title.",
                },
                "body": {
                    **_CORRECTION_BODY_SCHEMA,
                    "description": "For edit-and-approve: corrected structured content.",
                },
                "provenance": {
                    **_REVIEW_PROVENANCE_SCHEMA,
                    "description": "For edit-and-approve: corrected provenance details.",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "For edit-and-approve: corrected confidence, between 0 and 1.",
                },
                "replacement_title": {
                    "type": "string",
                    "description": "For supersede-existing: title of the replacement memory.",
                },
                "replacement_body": {
                    **_CORRECTION_BODY_SCHEMA,
                    "description": "For supersede-existing: structured content of the replacement memory.",
                },
                "replacement_provenance": {
                    **_REVIEW_PROVENANCE_SCHEMA,
                    "description": "For supersede-existing: provenance details of the replacement memory.",
                },
                "replacement_confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "For supersede-existing: confidence of the replacement memory, between 0 and 1.",
                },
            },
        },
    },
    {
        "name": "alice_memory_manage",
        "description": (
            "Manage a memory written through alice_memory_commit: confirm a pending "
            "confirmation, undo a commit, forget a memory, expire or unexpire its validity "
            "window, accept a consolidation candidate, or redact its content. Undo, forget, "
            "and expire hide the memory from recall but keep its revisions and audit events; "
            "redact permanently scrubs governed memory-lifecycle copies and any coupled "
            "terminal project-update artifact copies while keeping the audit skeleton. Alice "
            "source and source-chunk evidence is retained because it may be shared and requires "
            "separate source hygiene. A mutation of a memory above the caller's sensitivity "
            "ceiling is refused. Confirm and reject of a pending write are limited to its author, "
            "an admin_agent key, or the owner; on a keyless install that limit is not protection, "
            "because the caller can declare the author's agent_id. Redact is restricted to a human "
            "operator or an admin agent (as is accept_consolidation)."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_MEMORY_MANAGE_ACTIONS),
                    "description": (
                        "What to do: 'confirm' completes a pending confirmation by confirmation_id, "
                        "'undo' reverses a commit, 'forget' retires a memory from recall, 'expire' "
                        "closes the memory's validity window (valid_to) so recall stops returning it, "
                        "'unexpire' reopens that window, 'accept_consolidation' accepts a "
                        "consolidation candidate and supersedes the memories it merges, and 'redact' "
                        "permanently scrubs the governed memory row, revisions, matching event "
                        "payloads and quoted provenance, plus any coupled terminal project-update "
                        "artifact copies. It keeps the audit skeleton and Alice source/source-chunk "
                        "evidence."
                    ),
                },
                "confirmation_id": {
                    "type": "string",
                    "description": "For confirm: the confirmation id returned by alice_memory_commit.",
                },
                "memory_id": {
                    "type": "string",
                    "description": "The memory to act on. Required for forget, expire, unexpire, accept_consolidation, and redact; for undo it defaults to the calling agent's most recent commit.",
                },
                "canonical_text": {
                    "type": "string",
                    "description": "For confirm: corrected text to store instead of the proposed text.",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Why this change is being made. For expire, unexpire, and "
                        "accept_consolidation, the required reason is stored in the audit trail. "
                        "For redact, the reason is required for authorization and lifecycle intent "
                        "but intentionally not retained after successful true redaction. Required "
                        "for expire, unexpire, accept_consolidation, and redact."
                    ),
                },
                "valid_to": {
                    "type": "string",
                    "description": "For expire: ISO-8601 timestamp when the memory stops being valid. Defaults to now, which hides the memory from recall immediately.",
                },
                "superseded_by": {
                    "type": "string",
                    "description": "For undo: id of the memory that replaces the undone one. Links the two so alice_explain can show what changed and when.",
                },
                **_AGENT_IDENTITY_SCHEMA_PROPERTIES,
            },
        },
    },
    {
        "name": "alice_explain",
        "description": (
            "Explain where a memory came from, why it can be trusted, and how it changed "
            "over time: source evidence, revision history, corroborations, and contradiction "
            "signals, plus the memory's supersession chain (what it replaced and what "
            "replaced it, oldest to newest, each entry listing the people, projects, and "
            "other entities it is linked to) and a timeline that merges creations, edits, "
            "corrections, and replacements into one chronological story. "
            "Pass memory_id for a result from alice_recall, continuity_object_id for a "
            "reviewed record, or entity_id (optionally with 'at') for a point-in-time "
            "explanation."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                **_AGENT_IDENTITY_SCHEMA_PROPERTIES,
                "memory_id": {
                    "type": "string",
                    "description": "Id of a memory returned by alice_recall; returns its provenance links, revisions, event history, supersession_chain (each entry has id, title, status, created_at, its relation to this memory: predecessor, self, or successor, and the entities it is linked to when available), and a timeline: one chronological list of {at, kind, memory_id, summary} entries (kind is created, revised, corrected, or superseded_by) telling how this memory evolved.",
                },
                "continuity_object_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "UUID of a reviewed memory record; returns its evidence chain and trust signals.",
                },
                "entity_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "UUID of an entity; explains which facts were in effect for it and why.",
                },
                "at": {
                    "type": "string",
                    "format": "date-time",
                    "description": "With entity_id: the point in time to explain, ISO-8601. Defaults to now.",
                },
                "include_raw_content": {
                    "type": "boolean",
                    "description": "Include raw captured content in the explanation. Only allowed in development or test environments.",
                },
            },
        },
    },
]


_LEGACY_TOOL_DEFINITIONS: list[dict[str, object]] = [
    {
        "name": "alice_capture_candidates",
        "description": "Extract continuity candidates from one user/assistant turn without writing memory.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "user_content": {"type": "string"},
                "assistant_content": {"type": "string"},
                "session_id": {"type": "string"},
                "source_kind": {"type": "string"},
            },
        },
    },
    {
        "name": "alice_commit_captures",
        "description": "Commit extracted continuity candidates using manual/assist/auto bridge policy.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mode": {"type": "string", "enum": list(CONTINUITY_CAPTURE_COMMIT_MODES)},
                "sync_fingerprint": {"type": "string"},
                "source_kind": {"type": "string"},
                "candidates": {
                    "type": "array",
                    "items": _CONTINUITY_CAPTURE_CANDIDATE_SCHEMA,
                },
            },
        },
    },
    {
        "name": "alice_memory_mutations_generate",
        "description": "Generate explicit memory mutation candidates with ADD/UPDATE/SUPERSEDE/DELETE/NOOP classification.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "user_content": {"type": "string"},
                "assistant_content": {"type": "string"},
                "mode": {"type": "string", "enum": list(CONTINUITY_CAPTURE_COMMIT_MODES)},
                "sync_fingerprint": {"type": "string"},
                "source_kind": {"type": "string"},
                "session_id": {"type": "string"},
                "thread_id": {"type": "string", "format": "uuid"},
                "task_id": {"type": "string", "format": "uuid"},
                "project": {"type": "string"},
                "person": {"type": "string"},
                "target_continuity_object_id": {"type": "string", "format": "uuid"},
            },
        },
    },
    {
        "name": "alice_memory_mutations_list_candidates",
        "description": "Inspect generated explicit memory mutation candidates.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "policy_action": {"type": "string", "enum": ["auto_apply", "review_required", "skip"]},
                "operation_type": {"type": "string", "enum": ["ADD", "UPDATE", "SUPERSEDE", "DELETE", "NOOP"]},
                "sync_fingerprint": {"type": "string"},
            },
        },
    },
    {
        "name": "alice_memory_mutations_commit",
        "description": "Apply explicit memory mutation candidates with idempotent audit records.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "candidate_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                },
                "sync_fingerprint": {"type": "string"},
                "include_review_required": {"type": "boolean"},
            },
        },
    },
    {
        "name": "alice_memory_mutations_list_operations",
        "description": "Inspect committed explicit memory operations and their result links.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "sync_fingerprint": {"type": "string"},
            },
        },
    },
    {
        "name": "alice_recall_debug",
        "description": "Run hybrid continuity retrieval with per-candidate stage scores and exclusion reasons.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "thread_id": {"type": "string", "format": "uuid"},
                "task_id": {"type": "string", "format": "uuid"},
                "project": {"type": "string"},
                "person": {"type": "string"},
                "since": {"type": "string", "format": "date-time"},
                "until": {"type": "string", "format": "date-time"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_CONTINUITY_RECALL_LIMIT},
            },
        },
    },
    {
        "name": "alice_state_at",
        "description": "Show entity facts and edges that were effective at a specific point in time.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["entity_id"],
            "properties": {
                "entity_id": {"type": "string", "format": "uuid"},
                "at": {"type": "string", "format": "date-time"},
            },
        },
    },
    {
        "name": "alice_resume_debug",
        "description": "Compile a resumption brief with the underlying hybrid retrieval trace attached.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "thread_id": {"type": "string", "format": "uuid"},
                "task_id": {"type": "string", "format": "uuid"},
                "project": {"type": "string"},
                "person": {"type": "string"},
                "since": {"type": "string", "format": "date-time"},
                "until": {"type": "string", "format": "date-time"},
                "max_recent_changes": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
                },
                "max_open_loops": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
                },
                "include_non_promotable_facts": {"type": "boolean"},
            },
        },
    },
    {
        "name": "alice_brief",
        "description": "Compile the primary one-call continuity brief for general, resume, handoff, coding, or operator contexts.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "brief_type": {
                    "type": "string",
                    "enum": CONTINUITY_BRIEF_TYPE_ORDER,
                },
                "query": {"type": "string"},
                "thread_id": {"type": "string", "format": "uuid"},
                "task_id": {"type": "string", "format": "uuid"},
                "project": {"type": "string"},
                "person": {"type": "string"},
                "since": {"type": "string", "format": "date-time"},
                "until": {"type": "string", "format": "date-time"},
                "max_relevant_facts": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT,
                },
                "max_recent_changes": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
                },
                "max_open_loops": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
                },
                "max_conflicts": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_CONTINUITY_BRIEF_CONFLICT_LIMIT,
                },
                "max_timeline_highlights": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_CONTINUITY_BRIEF_TIMELINE_LIMIT,
                },
                "include_non_promotable_facts": {"type": "boolean"},
            },
        },
    },
    {
        "name": "alice_task_brief",
        "description": "Compile and persist one task-adaptive brief for recall, resume, worker, or handoff workloads.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["mode"],
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["user_recall", "resume", "worker_subtask", "agent_handoff"],
                },
                "query": {"type": "string"},
                "thread_id": {"type": "string", "format": "uuid"},
                "task_id": {"type": "string", "format": "uuid"},
                "project": {"type": "string"},
                "person": {"type": "string"},
                "since": {"type": "string", "format": "date-time"},
                "until": {"type": "string", "format": "date-time"},
                "include_non_promotable_facts": {"type": "boolean"},
                "provider_strategy": {"type": "string"},
                "briefing_strategy": {
                    "type": "string",
                    "enum": ["balanced", "compact", "detailed"],
                },
                "token_budget": {"type": "integer", "minimum": 1, "maximum": MAX_TASK_BRIEF_TOKEN_BUDGET},
            },
        },
    },
    {
        "name": "alice_task_brief_show",
        "description": "Load one persisted task-adaptive brief by id.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["task_brief_id"],
            "properties": {
                "task_brief_id": {"type": "string", "format": "uuid"},
            },
        },
    },
    {
        "name": "alice_task_brief_compare",
        "description": "Compare two task-brief modes for the same scope and show which one is smaller.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["mode", "compare_to_mode"],
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["user_recall", "resume", "worker_subtask", "agent_handoff"],
                },
                "compare_to_mode": {
                    "type": "string",
                    "enum": ["user_recall", "resume", "worker_subtask", "agent_handoff"],
                },
                "query": {"type": "string"},
                "thread_id": {"type": "string", "format": "uuid"},
                "task_id": {"type": "string", "format": "uuid"},
                "project": {"type": "string"},
                "person": {"type": "string"},
                "since": {"type": "string", "format": "date-time"},
                "until": {"type": "string", "format": "date-time"},
                "include_non_promotable_facts": {"type": "boolean"},
                "provider_strategy": {"type": "string"},
                "briefing_strategy": {
                    "type": "string",
                    "enum": ["balanced", "compact", "detailed"],
                },
                "compare_briefing_strategy": {
                    "type": "string",
                    "enum": ["balanced", "compact", "detailed"],
                },
                "token_budget": {"type": "integer", "minimum": 1, "maximum": MAX_TASK_BRIEF_TOKEN_BUDGET},
                "compare_token_budget": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TASK_BRIEF_TOKEN_BUDGET,
                },
            },
        },
    },
    {
        "name": "alice_retrieval_trace",
        "description": "Load one persisted retrieval trace by run id.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["retrieval_run_id"],
            "properties": {
                "retrieval_run_id": {"type": "string", "format": "uuid"},
            },
        },
    },
    {
        "name": "alice_prefetch_context",
        "description": "Assemble deterministic pre-turn prefetch context text from continuity resumption state.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "thread_id": {"type": "string", "format": "uuid"},
                "task_id": {"type": "string", "format": "uuid"},
                "project": {"type": "string"},
                "person": {"type": "string"},
                "since": {"type": "string", "format": "date-time"},
                "until": {"type": "string", "format": "date-time"},
                "max_recent_changes": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
                },
                "max_open_loops": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
                },
                "include_non_promotable_facts": {"type": "boolean"},
            },
        },
    },
    {
        "name": "alice_recent_changes",
        "description": "List recent continuity changes from the shipped resumption assembly logic.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "thread_id": {"type": "string", "format": "uuid"},
                "task_id": {"type": "string", "format": "uuid"},
                "project": {"type": "string"},
                "person": {"type": "string"},
                "since": {"type": "string", "format": "date-time"},
                "until": {"type": "string", "format": "date-time"},
                "limit": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
                },
            },
        },
    },
    {
        "name": "alice_timeline",
        "description": "List chronological temporal history for one entity.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["entity_id"],
            "properties": {
                "entity_id": {"type": "string", "format": "uuid"},
                "since": {"type": "string", "format": "date-time"},
                "until": {"type": "string", "format": "date-time"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_TEMPORAL_TIMELINE_LIMIT},
            },
        },
    },
    {
        "name": "alice_review_queue",
        "description": "List pending review queue items or fetch one review item detail with explanation metadata.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "review_item_id": {"type": "string", "format": "uuid"},
                "continuity_object_id": {"type": "string", "format": "uuid"},
                "status": {"type": "string", "enum": list(_REVIEW_STATUS_CHOICES)},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_CONTINUITY_REVIEW_LIMIT},
            },
        },
    },
    {
        "name": "alice_review_apply",
        "description": "Apply approve/reject/edit-and-approve/supersede-existing review actions deterministically.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["action"],
            "properties": {
                "review_item_id": {"type": "string", "format": "uuid"},
                "continuity_object_id": {"type": "string", "format": "uuid"},
                "action": {"type": "string", "enum": list(_REVIEW_APPLY_ACTION_CHOICES)},
                "reason": {"type": "string"},
                "title": {"type": "string"},
                "body": _CORRECTION_BODY_SCHEMA,
                "provenance": _CONTINUITY_PROVENANCE_SCHEMA,
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "replacement_title": {"type": "string"},
                "replacement_body": _CORRECTION_BODY_SCHEMA,
                "replacement_provenance": _CONTINUITY_PROVENANCE_SCHEMA,
                "replacement_confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
            },
        },
    },
    {
        "name": "alice_contradictions_detect",
        "description": "Run contradiction detection and persist current contradiction and trust state.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "continuity_object_id": {"type": "string", "format": "uuid"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_CONTINUITY_REVIEW_LIMIT},
            },
        },
    },
    {
        "name": "alice_contradictions_list",
        "description": "List contradiction cases or fetch one contradiction case detail.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "contradiction_case_id": {"type": "string", "format": "uuid"},
                "continuity_object_id": {"type": "string", "format": "uuid"},
                "status": {"type": "string", "enum": ["open", "resolved", "dismissed"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_CONTINUITY_REVIEW_LIMIT},
            },
        },
    },
    {
        "name": "alice_contradictions_resolve",
        "description": "Resolve one contradiction case with an explicit audit action.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["contradiction_case_id", "action"],
            "properties": {
                "contradiction_case_id": {"type": "string", "format": "uuid"},
                "action": {"type": "string", "enum": list(CONTRADICTION_RESOLUTION_ACTIONS)},
                "note": {"type": "string"},
            },
        },
    },
    {
        "name": "alice_trust_signals",
        "description": "Inspect current stored trust signals for continuity objects.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "continuity_object_id": {"type": "string", "format": "uuid"},
                "signal_state": {"type": "string", "enum": ["active", "inactive"]},
                "signal_type": {
                    "type": "string",
                    "enum": ["correction", "corroboration", "contradiction", "weak_inference"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_CONTINUITY_REVIEW_LIMIT},
            },
        },
    },
    {
        "name": "alice_artifact_inspect",
        "description": "Inspect one archived artifact with copies and extracted segments.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["artifact_id"],
            "properties": {
                "artifact_id": {"type": "string", "format": "uuid"},
                "include_raw_content": {"type": "boolean"},
            },
        },
    },
    {
        "name": "alice_vnext_context_pack",
        "description": "Compile a vNext provenance-aware context pack with retrieval trace metadata.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "domains": {"type": "array", "items": {"type": "string"}},
                "projects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_CONTEXT_SCOPE_VALUES,
                    "description": "Hard filter applied to every emitted context section.",
                },
                "people": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_CONTEXT_SCOPE_VALUES,
                    "description": "Hard filter applied to every emitted context section.",
                },
                "time_window": {
                    "type": "string",
                    "pattern": r"^(all|(?:[1-9]|[1-9][0-9]{1,2}|[12][0-9]{3}|3[0-5][0-9]{2}|36[0-4][0-9]|3650)d)$",
                    "description": f"Hard filter: 'all' or a rolling 1d through {MAX_TIME_WINDOW_DAYS}d window.",
                },
                "sensitivity_allowed": {"type": "array", "items": {"type": "string"}},
                "include_sources": {"type": "boolean"},
                "include_contradictions": {"type": "boolean"},
                "max_items": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_CONTEXT_PACK_ITEMS,
                },
                "max_tokens": {
                    "type": "integer",
                    "minimum": 500,
                    "maximum": MAX_CONTEXT_PACK_TOKENS,
                },
            },
        },
    },
    {
        "name": "alice_vnext_context_tree",
        "description": "Return a read-only agent-navigable tree over vNext projects, memories, sources, open loops, artifacts, and events.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "query": {"type": "string"},
                "projects": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "include_events": {"type": "boolean"},
                "trace_id": {"type": "string"},
            },
        ),
    },
    {
        "name": "alice_generate_daily_brief",
        "description": "Generate a vNext daily brief artifact with provenance and review status.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "generated_for": {"type": "string", "format": "date"},
                "domains": {"type": "array", "items": {"type": "string"}},
                "project_scope": {"type": "array", "items": {"type": "string"}},
                "projects": {"type": "array", "items": {"type": "string"}},
                "sensitivity_allowed": {"type": "array", "items": {"type": "string"}},
                "source_limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "memory_limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "open_loop_limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "artifact_limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "discover_open_loops": {"type": "boolean"},
                "create_candidate_memories": {"type": "boolean"},
                **_MODEL_GENERATION_SCHEMA_PROPERTIES,
            },
        },
    },
    {
        "name": "alice_generate_weekly_synthesis",
        "description": "Generate a vNext weekly synthesis artifact and candidate insight memories.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "generated_for": {"type": "string", "format": "date"},
                "domains": {"type": "array", "items": {"type": "string"}},
                "project_scope": {"type": "array", "items": {"type": "string"}},
                "projects": {"type": "array", "items": {"type": "string"}},
                "sensitivity_allowed": {"type": "array", "items": {"type": "string"}},
                "source_limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "memory_limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "open_loop_limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "artifact_limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "discover_open_loops": {"type": "boolean"},
                "create_candidate_memories": {"type": "boolean"},
                **_MODEL_GENERATION_SCHEMA_PROPERTIES,
            },
        },
    },
    {
        "name": "alice_generate_connections",
        "description": "Generate a vNext connection report and candidate graph edges.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "domains": {"type": "array", "items": {"type": "string"}},
                "project_scope": {"type": "array", "items": {"type": "string"}},
                "projects": {"type": "array", "items": {"type": "string"}},
                "sensitivity_allowed": {"type": "array", "items": {"type": "string"}},
                "max_connections": {"type": "integer", "minimum": 1, "maximum": 50},
                "auto_accept_threshold": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                **_MODEL_GENERATION_SCHEMA_PROPERTIES,
            },
        },
    },
    {
        "name": "alice_graph_edge_review",
        "description": "Review, accept, or reject a vNext candidate graph edge.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["edge_id", "action"],
            "properties": {
                "edge_id": {"type": "string"},
                "action": {"type": "string", "enum": ["review", "accept", "reject"]},
            },
        },
    },
    {
        "name": "alice_graph_neighborhood",
        "description": "Return active vNext graph edges around a target id.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["target_id"],
            "properties": {
                "target_id": {"type": "string"},
            },
        },
    },
    {
        "name": "alice_generate_contradictions",
        "description": "Generate a vNext contradiction report and candidate contradiction graph edges.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "domains": {"type": "array", "items": {"type": "string"}},
                "project_scope": {"type": "array", "items": {"type": "string"}},
                "projects": {"type": "array", "items": {"type": "string"}},
                "sensitivity_allowed": {"type": "array", "items": {"type": "string"}},
                "max_contradictions": {"type": "integer", "minimum": 1, "maximum": 50},
                **_MODEL_GENERATION_SCHEMA_PROPERTIES,
            },
        },
    },
    {
        "name": "alice_belief_review",
        "description": "Reinforce, challenge, supersede, or retire a vNext belief.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["belief_id", "action"],
            "properties": {
                "belief_id": {"type": "string"},
                "action": {"type": "string", "enum": ["reinforce", "challenge", "supersede", "retire"]},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "superseded_by": {"type": "string"},
            },
        },
    },
    {
        "name": "alice_belief_state",
        "description": "Return current and historical state for a vNext belief.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["belief_id"],
            "properties": {
                "belief_id": {"type": "string"},
            },
        },
    },
    {
        "name": "alice_project_update_candidate",
        "description": "Generate a vNext project update candidate artifact.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string"},
                "domains": {"type": "array", "items": {"type": "string"}},
                "sensitivity_allowed": {"type": "array", "items": {"type": "string"}},
                "max_items": {"type": "integer", "minimum": 1, "maximum": 50},
                **_MODEL_GENERATION_SCHEMA_PROPERTIES,
            },
        },
    },
    {
        "name": "alice_project_update_review",
        "description": "Accept, edit, or reject a vNext project update candidate artifact.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "artifact_id": {"type": "string"},
                "action": {"type": "string", "enum": ["accept", "edit", "reject"]},
                "edited_current_state": {"type": "string"},
            },
            required=["artifact_id", "action"],
        ),
    },
    {
        "name": "alice_project_dashboard",
        "description": "Return vNext project dashboard state, memories, open loops, and artifacts.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["project_id"],
            "properties": {
                "project_id": {"type": "string"},
                "sensitivity_allowed": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "alice_open_loop_extract",
        "description": "Extract vNext candidate open loops from selected sources.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string"},
                "person_id": {"type": "string"},
                "domains": {"type": "array", "items": {"type": "string"}},
                "sensitivity_allowed": {"type": "array", "items": {"type": "string"}},
                "max_items": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    },
    {
        "name": "alice_open_loop_review",
        "description": "Close, snooze, edit, or reopen a vNext open loop.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["loop_id", "action"],
            "properties": {
                "loop_id": {"type": "string"},
                "action": {"type": "string", "enum": ["close", "snooze", "edit", "reopen"]},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "due_at": {"type": "string"},
                "priority": {"type": "string"},
                "resolution_note": {"type": "string"},
            },
        },
    },
    {
        "name": "alice_vnext_capture",
        "description": "Capture a vNext source with optional agent identity and policy checks.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "raw_text": {"type": "string"},
                "title": {"type": "string"},
                "source_type": {"type": "string"},
                "domain": {"type": "string"},
                "sensitivity": {"type": "string"},
            },
            required=["raw_text"],
        ),
    },
    {
        "name": "alice_vnext_ingest_agent_output",
        "description": "Capture Hermes/OpenClaw agent output as source/artifact evidence with optional review-only memory proposal.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "title": {"type": "string"},
                "content": {"type": "string"},
                "output_type": {
                    "type": "string",
                    "enum": [
                        "sprint_summary",
                        "research_summary",
                        "code_review",
                        "project_update",
                        "decision",
                        "general",
                    ],
                },
                "domain": {"type": "string"},
                "sensitivity": {"type": "string"},
                "source_refs": {"type": "array", "items": {"type": "string"}},
                "rationale": {"type": "string"},
                "propose_memory": {"type": "boolean"},
            },
            required=["agent_id", "title", "content"],
        ),
    },
    {
        "name": "alice_vnext_queue_task",
        "description": "Create a vNext queue task with optional agent identity and policy checks.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "title": {"type": "string"},
                "task_type": {"type": "string"},
                "instructions": {"type": "string"},
                "domain": {"type": "string"},
                "sensitivity": {"type": "string"},
                "scheduled_for": {"type": "string"},
            },
            required=["title", "task_type", "instructions"],
        ),
    },
    {
        "name": "alice_vnext_generate_artifact",
        "description": "Generate a vNext artifact workflow such as daily_brief or weekly_synthesis.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "artifact_type": {"type": "string"},
                "workflow_type": {"type": "string"},
                "generated_for": {"type": "string"},
                "max_items": {"type": "integer", "minimum": 1, "maximum": 50},
                "source_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "memory_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "artifact_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "event_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "rating_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "create_candidate_memories": {"type": "boolean"},
                **_MODEL_GENERATION_SCHEMA_PROPERTIES,
            },
        ),
    },
    {
        "name": "alice_vnext_project_dashboard",
        "description": "Return vNext project dashboard state.",
        "inputSchema": _vnext_agent_tool_schema({"project_id": {"type": "string"}}, required=["project_id"]),
    },
    {
        "name": "alice_vnext_open_loops",
        "description": "List vNext open loops with domain and sensitivity filters.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "project_id": {"type": "string"},
                "source_id": {"type": "string"},
                "status": {"type": "string"},
                "priority": {"type": "string"},
                "due_at": {"type": "string"},
                "domain": {"type": "string"},
                "sensitivity": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        ),
    },
    {
        "name": "alice_vnext_recent_decisions",
        "description": "Return recent decision context through the existing continuity lookup.",
        "inputSchema": _vnext_agent_tool_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
    },
    {
        "name": "alice_vnext_recent_changes",
        "description": "Return recent change context through the existing continuity lookup.",
        "inputSchema": _vnext_agent_tool_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
    },
    {
        "name": "alice_vnext_find_connections",
        "description": "Generate a vNext connection report.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "max_connections": {"type": "integer", "minimum": 1, "maximum": 50},
                **_MODEL_GENERATION_SCHEMA_PROPERTIES,
            }
        ),
    },
    {
        "name": "alice_vnext_find_contradictions",
        "description": "Generate a vNext contradiction report.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "max_contradictions": {"type": "integer", "minimum": 1, "maximum": 50},
                **_MODEL_GENERATION_SCHEMA_PROPERTIES,
            }
        ),
    },
    {
        "name": "alice_vnext_propose_memory",
        "description": "Submit an agent memory proposal for human review.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "proposal_type": {"type": "string"},
                "title": {"type": "string"},
                "canonical_text": {"type": "string"},
                "source_refs": {"type": "array", "items": {"type": "string"}},
                "domain": {"type": "string"},
                "sensitivity": {"type": "string"},
                "confidence": {"type": "number"},
                "rationale": {"type": "string"},
            },
            required=["agent_id", "canonical_text"],
        ),
    },
    {
        "name": "alice_vnext_commit_memory",
        "description": "Commit an explicit trusted-agent memory write through Alice policy, or return confirmation/review/reject.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "intent": {"type": "string"},
                "title": {"type": "string"},
                "canonical_text": {"type": "string"},
                "memory_type": {"type": "string", "enum": list(VNEXT_MEMORY_TYPES)},
                "domain": {"type": "string", "enum": list(VNEXT_DOMAINS)},
                "sensitivity": {"type": "string", "enum": list(VNEXT_SENSITIVITY_LEVELS)},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "source_type": {"type": "string"},
                "source_refs": {"type": "array", "items": {"type": "string"}},
                "conversation_excerpt": {"type": "string"},
                "rationale": {"type": "string"},
                "idempotency_key": {"type": "string"},
                "contradiction_refs": {"type": "array", "items": {"type": "string"}},
            },
            required=["agent_id", "title", "canonical_text"],
        ),
    },
    {
        "name": "alice_vnext_confirm_memory",
        "description": "Confirm, reject, or edit a pending inline agentic memory confirmation.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "confirmation_id": {"type": "string"},
                "action": {"type": "string", "enum": ["confirm", "reject", "edit"]},
                "canonical_text": {"type": "string"},
                "rationale": {"type": "string"},
            },
            required=["confirmation_id"],
        ),
    },
    {
        "name": "alice_vnext_undo_memory",
        "description": "Undo an agentic memory commit without deleting the audit trail.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "memory_id": {"type": "string"},
                "reason": {"type": "string"},
                "superseded_by": {"type": "string"},
            },
        ),
    },
    {
        "name": "alice_vnext_correct_memory",
        "description": "Correct an agentic memory commit and append a revision.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "memory_id": {"type": "string"},
                "canonical_text": {"type": "string"},
                "reason": {"type": "string"},
            },
            required=["memory_id", "canonical_text"],
        ),
    },
    {
        "name": "alice_vnext_forget_memory",
        "description": "Forget an agentic memory commit while preserving audit history.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "memory_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            required=["memory_id"],
        ),
    },
    {
        "name": "alice_vnext_recent_memory_commits",
        "description": "List recent agentic memory commits, confirmations, corrections, undos, and forgets.",
        "inputSchema": _vnext_agent_tool_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
    },
    {
        "name": "alice_vnext_memory_audit",
        "description": "Return memory, revision, provenance, and event audit details for one memory.",
        "inputSchema": _vnext_agent_tool_schema({"memory_id": {"type": "string"}}, required=["memory_id"]),
    },
    {
        "name": "alice_vnext_review_items",
        "description": "List pending vNext memory review items.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "status": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        ),
    },
    {
        "name": "alice_vnext_artifact_get",
        "description": "Get one vNext generated artifact.",
        "inputSchema": _vnext_agent_tool_schema({"artifact_id": {"type": "string"}}, required=["artifact_id"]),
    },
    {
        "name": "alice_vnext_artifact_review",
        "description": "Review a vNext artifact; agent callers are policy checked.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "artifact_id": {"type": "string"},
                "action": {"type": "string"},
                "reason": {"type": "string"},
            },
            required=["artifact_id", "action"],
        ),
    },
    {
        "name": "alice_vnext_scheduler_status",
        "description": "Return governed local scheduler status.",
        "inputSchema": _vnext_agent_tool_schema(),
    },
    {
        "name": "alice_vnext_scheduler_run_now",
        "description": "Run a governed scheduler workflow now with policy checks.",
        "inputSchema": _vnext_agent_tool_schema(
            {
                "workflow_type": {"type": "string"},
                "generated_for": {"type": "string"},
                "source_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "memory_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "artifact_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "event_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "rating_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "create_candidate_memories": {"type": "boolean"},
                **_MODEL_GENERATION_SCHEMA_PROPERTIES,
            },
            required=["workflow_type"],
        ),
    },
    {
        "name": "alice_vnext_scheduler_run_due",
        "description": "Run enabled governed scheduler workflows whose next_run_at is due.",
        "inputSchema": _vnext_agent_tool_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
    },
    {
        "name": "alice_vnext_scheduler_pause",
        "description": "Pause all governed scheduler workflows.",
        "inputSchema": _vnext_agent_tool_schema(),
    },
    {
        "name": "alice_vnext_scheduler_resume",
        "description": "Resume all governed scheduler workflows.",
        "inputSchema": _vnext_agent_tool_schema(),
    },
]
