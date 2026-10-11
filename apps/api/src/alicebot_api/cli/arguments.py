from __future__ import annotations

import argparse
from datetime import datetime
import json
from uuid import UUID
from alicebot_api.contracts import (
    CONTINUITY_BRIEF_TYPE_ORDER,
    DEFAULT_CONTINUITY_BRIEF_CONFLICT_LIMIT,
    DEFAULT_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT,
    DEFAULT_CONTINUITY_BRIEF_TIMELINE_LIMIT,
    DEFAULT_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
    DEFAULT_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
    MAX_CONTINUITY_REVIEW_LIMIT,
    MAX_CONTINUITY_OPEN_LOOP_LIMIT,
    MAX_CONTINUITY_RECALL_LIMIT,
    MAX_CONTINUITY_BRIEF_CONFLICT_LIMIT,
    MAX_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT,
    MAX_CONTINUITY_BRIEF_TIMELINE_LIMIT,
    MAX_CONTINUITY_LIFECYCLE_LIMIT,
    MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
    MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
    MAX_TASK_BRIEF_TOKEN_BUDGET,
    MAX_TEMPORAL_TIMELINE_LIMIT,
    MAX_TRUSTED_FACT_PROMOTION_LIMIT,
)
from alicebot_api.store import JsonObject as ContinuityJsonObject

from .constants import DEFAULT_VNEXT_SENSITIVITY_ALLOWED, REVIEW_STATUS_CHOICES


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid UUID value: {value}") from exc


def _parse_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid datetime value '{value}'. Use ISO-8601 format.") from exc


def _parse_optional_json_object(raw_value: str | None, *, option_name: str) -> ContinuityJsonObject | None:
    if raw_value is None:
        return None
    # Imported here so the module keeps the names the CLI facade re-exports.
    from alicebot_api.json_bounds import JsonShapeError, loads_caller_json

    try:
        payload = loads_caller_json(raw_value, name=option_name)
    except JsonShapeError as exc:
        raise ValueError(str(exc)) from exc
    except ValueError as exc:
        raise ValueError(f"{option_name} must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{option_name} must be a JSON object")
    return payload


def _object_dict(value: object) -> dict[str, object]:
    """Return a JSON-like object only after a runtime shape check."""

    return value if isinstance(value, dict) else {}


def _object_list(value: object) -> list[object]:
    """Return a JSON-like list only after a runtime shape check."""

    return value if isinstance(value, list) else []


def _object_int(value: object, *, default: int = 0) -> int:
    """Read an integer-valued payload field without accepting bools."""

    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _add_scope_filter_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--query", default=None, help="Optional query text.")
    parser.add_argument("--thread-id", type=_parse_uuid, default=None, help="Optional thread UUID scope.")
    parser.add_argument("--task-id", type=_parse_uuid, default=None, help="Optional task UUID scope.")
    parser.add_argument("--project", default=None, help="Optional project scope.")
    parser.add_argument("--person", default=None, help="Optional person scope.")
    parser.add_argument("--since", type=_parse_datetime, default=None, help="Optional start time (ISO-8601).")
    parser.add_argument("--until", type=_parse_datetime, default=None, help="Optional end time (ISO-8601).")


def _add_vnext_agent_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--agent-id", default=None, help="Agent id for agent-originated vNext actions.")
    parser.add_argument("--agent-type", default="unknown", help="Agent type.")
    parser.add_argument("--agent-run-id", default=None, help="Agent run id.")
    parser.add_argument("--agent-task-id", default=None, help="Agent task id.")
    parser.add_argument("--project-scope", action="append", default=[], help="Allowed project scope. Repeatable.")
    parser.add_argument("--permission-profile", default="read_only_agent", help="Agent permission profile.")


def _add_model_generation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--generation-mode",
        choices=("deterministic", "model_backed"),
        default="deterministic",
        help="Generation mode for reviewable vNext artifacts.",
    )
    parser.add_argument(
        "--model-route-mode",
        choices=("local_only", "cloud_allowed", "cloud_requires_approval", "model_disabled"),
        default=None,
        help="Model routing policy mode for model-backed generation.",
    )
    parser.add_argument("--model-provider", default=None, help="Optional model provider id.")
    parser.add_argument("--model", default=None, help="Optional model id.")
    parser.add_argument(
        "--model-temperature", type=float, default=0.2, help="Model temperature for model-backed generation."
    )
    parser.add_argument(
        "--allow-cloud-private",
        action="store_true",
        help="Allow explicit cloud routing for private/restricted scopes.",
    )


def _add_task_brief_arguments(parser: argparse.ArgumentParser) -> None:
    _add_scope_filter_arguments(parser)
    parser.add_argument(
        "--mode",
        required=True,
        choices=("user_recall", "resume", "worker_subtask", "agent_handoff"),
        help="Task brief mode.",
    )
    parser.add_argument(
        "--include-non-promotable-facts",
        action="store_true",
        help="Include searchable but non-promotable facts where the mode allows it.",
    )
    parser.add_argument(
        "--provider-strategy",
        default=None,
        help="Optional provider briefing strategy label.",
    )
    parser.add_argument(
        "--briefing-strategy",
        choices=("balanced", "compact", "detailed"),
        default=None,
        help="Optional briefing strategy override.",
    )
    parser.add_argument(
        "--token-budget",
        type=int,
        default=None,
        help=f"Optional explicit token budget (1-{MAX_TASK_BRIEF_TOKEN_BUDGET}).",
    )


def _add_continuity_brief_arguments(parser: argparse.ArgumentParser) -> None:
    _add_scope_filter_arguments(parser)
    parser.add_argument(
        "--brief-type",
        choices=CONTINUITY_BRIEF_TYPE_ORDER,
        default="general",
        help="One-call continuity brief type.",
    )
    parser.add_argument(
        "--max-relevant-facts",
        type=int,
        default=DEFAULT_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT,
        help=f"Maximum relevant facts ({0}-{MAX_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT}).",
    )
    parser.add_argument(
        "--max-recent-changes",
        type=int,
        default=DEFAULT_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
        help=f"Maximum recent changes ({0}-{MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT}).",
    )
    parser.add_argument(
        "--max-open-loops",
        type=int,
        default=DEFAULT_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
        help=f"Maximum open loops ({0}-{MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT}).",
    )
    parser.add_argument(
        "--max-conflicts",
        type=int,
        default=DEFAULT_CONTINUITY_BRIEF_CONFLICT_LIMIT,
        help=f"Maximum open conflicts ({0}-{MAX_CONTINUITY_BRIEF_CONFLICT_LIMIT}).",
    )
    parser.add_argument(
        "--max-timeline-highlights",
        type=int,
        default=DEFAULT_CONTINUITY_BRIEF_TIMELINE_LIMIT,
        help=f"Maximum timeline highlights ({0}-{MAX_CONTINUITY_BRIEF_TIMELINE_LIMIT}).",
    )
    parser.add_argument(
        "--include-non-promotable-facts",
        action="store_true",
        help="Include searchable but non-promotable facts where the brief type allows it.",
    )


def _validate_limit(value: int, *, option_name: str, minimum: int, maximum: int) -> None:
    if value < minimum or value > maximum:
        raise ValueError(f"{option_name} must be between {minimum} and {maximum}")


def _validate_arguments(args: argparse.Namespace) -> None:
    if args.command == "mutations" and args.mutations_command in {"candidates", "operations"}:
        _validate_limit(
            args.limit,
            option_name="--limit",
            minimum=1,
            maximum=100,
        )
    elif args.command == "recall":
        _validate_limit(
            args.limit,
            option_name="--limit",
            minimum=1,
            maximum=MAX_CONTINUITY_RECALL_LIMIT,
        )
    elif args.command == "context-pack":
        _validate_limit(
            args.max_items,
            option_name="--max-items",
            minimum=1,
            maximum=50,
        )
        _validate_limit(
            args.max_tokens,
            option_name="--max-tokens",
            minimum=500,
            maximum=50_000,
        )
    elif args.command == "context-tree":
        _validate_limit(
            args.limit,
            option_name="--limit",
            minimum=1,
            maximum=50,
        )
    elif args.command == "contradictions" and args.contradictions_command in {"detect", "list"}:
        _validate_limit(
            args.limit,
            option_name="--limit",
            minimum=1,
            maximum=MAX_CONTINUITY_REVIEW_LIMIT,
        )
    elif args.command == "trust" and args.trust_command == "signals":
        _validate_limit(
            args.limit,
            option_name="--limit",
            minimum=1,
            maximum=MAX_CONTINUITY_REVIEW_LIMIT,
        )
    elif args.command == "evals" and args.evals_command == "runs":
        _validate_limit(
            args.limit,
            option_name="--limit",
            minimum=1,
            maximum=100,
        )
    elif args.command == "timeline":
        _validate_limit(
            args.limit,
            option_name="--limit",
            minimum=1,
            maximum=MAX_TEMPORAL_TIMELINE_LIMIT,
        )
    elif args.command == "brief":
        _validate_limit(
            args.max_relevant_facts,
            option_name="--max-relevant-facts",
            minimum=0,
            maximum=MAX_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT,
        )
        _validate_limit(
            args.max_recent_changes,
            option_name="--max-recent-changes",
            minimum=0,
            maximum=MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
        )
        _validate_limit(
            args.max_open_loops,
            option_name="--max-open-loops",
            minimum=0,
            maximum=MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
        )
        _validate_limit(
            args.max_conflicts,
            option_name="--max-conflicts",
            minimum=0,
            maximum=MAX_CONTINUITY_BRIEF_CONFLICT_LIMIT,
        )
        _validate_limit(
            args.max_timeline_highlights,
            option_name="--max-timeline-highlights",
            minimum=0,
            maximum=MAX_CONTINUITY_BRIEF_TIMELINE_LIMIT,
        )
    elif args.command == "lifecycle" and args.lifecycle_command == "list":
        _validate_limit(
            args.limit,
            option_name="--limit",
            minimum=1,
            maximum=MAX_CONTINUITY_LIFECYCLE_LIMIT,
        )
    elif args.command == "resume":
        _validate_limit(
            args.max_recent_changes,
            option_name="--max-recent-changes",
            minimum=0,
            maximum=MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
        )
        _validate_limit(
            args.max_open_loops,
            option_name="--max-open-loops",
            minimum=0,
            maximum=MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
        )
    elif args.command == "task-briefs" and args.task_briefs_command in {"compile", "compare"}:
        if args.token_budget is not None:
            _validate_limit(
                args.token_budget,
                option_name="--token-budget",
                minimum=1,
                maximum=MAX_TASK_BRIEF_TOKEN_BUDGET,
            )
        if args.task_briefs_command == "compare" and args.compare_token_budget is not None:
            _validate_limit(
                args.compare_token_budget,
                option_name="--compare-token-budget",
                minimum=1,
                maximum=MAX_TASK_BRIEF_TOKEN_BUDGET,
            )
    elif args.command == "open-loops":
        _validate_limit(
            args.limit,
            option_name="--limit",
            minimum=0,
            maximum=MAX_CONTINUITY_OPEN_LOOP_LIMIT,
        )
    elif args.command == "review" and args.review_command == "queue":
        _validate_limit(
            args.limit,
            option_name="--limit",
            minimum=1,
            maximum=MAX_CONTINUITY_REVIEW_LIMIT,
        )
    elif args.command == "patterns" and args.patterns_command == "list":
        _validate_limit(
            args.limit,
            option_name="--limit",
            minimum=1,
            maximum=MAX_TRUSTED_FACT_PROMOTION_LIMIT,
        )
    elif args.command == "playbooks" and args.playbooks_command == "list":
        _validate_limit(
            args.limit,
            option_name="--limit",
            minimum=1,
            maximum=MAX_TRUSTED_FACT_PROMOTION_LIMIT,
        )
