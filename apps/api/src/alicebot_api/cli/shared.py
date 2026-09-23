from __future__ import annotations

import argparse
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
import json
import os
from pathlib import Path
from typing import cast
from uuid import UUID
from alicebot_api.cli_formatting import format_capture_output
from alicebot_api.config import Settings, get_runtime_settings, get_settings
from alicebot_api.continuity_capture import capture_continuity_input
from alicebot_api.contracts import (
    DEFAULT_TASK_BRIEF_TOKEN_BUDGET as DEFAULT_TASK_BRIEF_TOKEN_BUDGET,
    ContinuityCaptureCreateInput,
)
from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_control import AgentIdentity, PolicyDecision, append_policy_events, evaluate_agent_policy
from alicebot_api.vnext_context_tree import VNextContextTreeService, VNextContextTreeStore
from alicebot_api.vnext_retrieval import VNextRetrievalService, VNextRetrievalStore
from alicebot_api.vnext_scheduler import VNextSchedulerService, VNextSchedulerStore
from alicebot_api.vnext_embeddings import DeferredMemoryEmbedding, persist_deferred_memory_embeddings_best_effort
from alicebot_api.vnext_json import json_safe
from alicebot_api.vnext_memory_commit import (
    VNextMemoryCommitValidationError as VNextMemoryCommitValidationError,
)
from alicebot_api.vnext_store import PostgresVNextStore
from .constants import (
    DEFAULT_CLI_USER_ID,
    DEFAULT_MAINTENANCE_REPORT_PATH,
    DEFAULT_VNEXT_SENSITIVITY_ALLOWED,
    MAINTENANCE_REPORT_PATH_ENV,
)
from .errors import PartialCommandFailure
from .models import CLIContext


def _resolve_user_id(settings: Settings, user_id_flag: str | None) -> UUID:
    if user_id_flag is not None:
        try:
            return UUID(user_id_flag)
        except ValueError as exc:
            raise ValueError(f"--user-id must be a UUID, got: {user_id_flag}") from exc
    if settings.auth_user_id != "":
        return UUID(settings.auth_user_id)
    return UUID(os.getenv("ALICEBOT_AUTH_USER_ID", DEFAULT_CLI_USER_ID))


def _settings_with_command_overrides(args: argparse.Namespace) -> Settings:
    """Load CLI settings after applying its explicit database/user overrides.

    The API server validates every hosted production dependency. Local CLI
    commands only require their database and acting-user context, so explicit
    flags must be usable even when unrelated hosted services are not configured.
    """

    if args.database_url is None and args.user_id is None:
        if os.getenv("APP_ENV", "development") in {"development", "test"}:
            return get_settings()
        return get_runtime_settings()
    effective_env = dict(os.environ)
    if args.database_url is not None:
        effective_env["DATABASE_URL"] = args.database_url
    if args.user_id is not None:
        try:
            UUID(args.user_id)
        except ValueError as exc:
            raise ValueError(f"--user-id must be a UUID, got: {args.user_id}") from exc
        effective_env["ALICEBOT_AUTH_USER_ID"] = args.user_id
    return Settings.from_env(
        effective_env,
        require_production_services=False,
    )


def _build_context(args: argparse.Namespace) -> CLIContext:
    settings = _settings_with_command_overrides(args)
    database_url = settings.database_url
    if database_url.startswith("sqlite:"):
        raise ValueError(
            "the 'alicebot' CLI requires a Postgres DATABASE_URL, got a SQLite URL. "
            "For local SQLite memory, use the 'alice-memory' CLI instead."
        )
    user_id = _resolve_user_id(settings, args.user_id)
    return CLIContext(settings=settings, database_url=database_url, user_id=user_id)


@contextmanager
def _store_context(ctx: CLIContext) -> Iterator[ContinuityStore]:
    with user_connection(ctx.database_url, ctx.user_id) as conn:
        yield ContinuityStore(conn)


@contextmanager
def _vnext_store_context(ctx: CLIContext) -> Iterator[PostgresVNextStore]:
    with user_connection(ctx.database_url, ctx.user_id) as conn:
        yield PostgresVNextStore(conn)


def _parse_maintenance_status_payload(payload: object) -> dict[str, object]:
    default_snapshot: dict[str, object] = {
        "maintenance_status": "unknown",
        "maintenance_schedule": "unknown",
        "maintenance_last_run_at": "unknown",
        "maintenance_failure_count": 0,
        "maintenance_warning_count": 0,
        "maintenance_stale_fact_count": 0,
        "maintenance_reembedded_segment_count": 0,
        "maintenance_pattern_candidate_count": 0,
        "maintenance_benchmark_status": "unknown",
    }

    if not isinstance(payload, dict):
        return default_snapshot

    summary = payload.get("summary")
    if isinstance(summary, dict):
        status = summary.get("status")
        if isinstance(status, str):
            default_snapshot["maintenance_status"] = status
        schedule = summary.get("schedule")
        if isinstance(schedule, str):
            default_snapshot["maintenance_schedule"] = schedule
        completed_at = summary.get("run_completed_at")
        if isinstance(completed_at, str):
            default_snapshot["maintenance_last_run_at"] = completed_at
        failure_count = summary.get("failure_count")
        if isinstance(failure_count, int):
            default_snapshot["maintenance_failure_count"] = failure_count
        warning_count = summary.get("warning_count")
        if isinstance(warning_count, int):
            default_snapshot["maintenance_warning_count"] = warning_count

    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return default_snapshot

    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_key = job.get("job_key")
        details = job.get("details")
        if not isinstance(job_key, str) or not isinstance(details, dict):
            continue
        if job_key == "stale_fact_marking":
            stale_fact_count = details.get("stale_fact_count")
            if isinstance(stale_fact_count, int):
                default_snapshot["maintenance_stale_fact_count"] = stale_fact_count
        elif job_key == "reembed_missing_segments":
            reembedded_segment_count = details.get("reembedded_segment_count")
            if isinstance(reembedded_segment_count, int):
                default_snapshot["maintenance_reembedded_segment_count"] = reembedded_segment_count
        elif job_key == "pattern_candidate_recompute":
            pattern_candidate_count = details.get("pattern_candidate_count")
            if isinstance(pattern_candidate_count, int):
                default_snapshot["maintenance_pattern_candidate_count"] = pattern_candidate_count
        elif job_key == "benchmark_regeneration":
            benchmark_status = details.get("benchmark_status")
            if isinstance(benchmark_status, str):
                default_snapshot["maintenance_benchmark_status"] = benchmark_status

    return default_snapshot


def _load_maintenance_status_snapshot() -> dict[str, object]:
    raw_path = os.getenv(MAINTENANCE_REPORT_PATH_ENV)
    if raw_path is None or raw_path.strip() == "":
        report_path = DEFAULT_MAINTENANCE_REPORT_PATH
    else:
        candidate = Path(raw_path.strip()).expanduser()
        report_path = candidate if candidate.is_absolute() else (Path.cwd() / candidate).resolve()

    if not report_path.exists():
        return _parse_maintenance_status_payload({})

    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _parse_maintenance_status_payload({})

    return _parse_maintenance_status_payload(payload)


def _run_capture(ctx: CLIContext, args: argparse.Namespace) -> str:
    raw_content = " ".join(args.raw_content).strip()
    with _store_context(ctx) as store:
        payload = capture_continuity_input(
            store,
            user_id=ctx.user_id,
            request=ContinuityCaptureCreateInput(
                raw_content=raw_content,
                explicit_signal=args.explicit_signal,
            ),
        )
    return format_capture_output(payload)


def _json_dumps(value: object) -> str:
    return json.dumps(json_safe(value), indent=2, sort_keys=True)


def _checked_batch_output(record: dict[str, object]) -> str:
    output = _json_dumps(record)
    if record.get("status") in {"partial", "failed"}:
        raise PartialCommandFailure(output)
    return output


def _vnext_sensitivity_allowed(args: argparse.Namespace) -> tuple[str, ...]:
    values = getattr(args, "sensitivity_allowed", None)
    return tuple(values) if values else DEFAULT_VNEXT_SENSITIVITY_ALLOWED


_AGENT_API_KEY_ENV = "ALICE_AGENT_API_KEY"


def _vnext_agent_identity_from_args(args: argparse.Namespace) -> AgentIdentity | None:
    agent_id = getattr(args, "agent_id", None)
    if not agent_id:
        return None
    return AgentIdentity(
        agent_id=agent_id,
        agent_type=getattr(args, "agent_type", None) or "unknown",
        agent_run_id=getattr(args, "agent_run_id", None),
        task_id=getattr(args, "agent_task_id", None),
        project_scope=tuple(getattr(args, "project_scope", None) or ()),
        permission_profile=getattr(args, "permission_profile", None) or "read_only_agent",
    )


def _vnext_authenticated_agent_identity_from_args(
    store: PostgresVNextStore,
    args: argparse.Namespace,
) -> AgentIdentity | None:
    """Resolve a CLI agent identity, against an issued key when one is given.

    Without this the CLI had no key path at all, so every ``--agent-id`` was
    self-asserted and could never be trusted, while omitting ``--agent-id``
    produced no identity. That inverted the intended order: the
    unauthenticated invocation outranked the authenticated one.

    With ``ALICE_AGENT_API_KEY`` set, identity is resolved and enforced
    against the key record exactly as the HTTP and MCP surfaces do, so a CLI
    agent can hold a server-enforced profile. Without it, the payload
    identity is honoured for authorization as before and stays marked
    unauthenticated, which is what keeps it out of promotion.

    Imported inside the function on purpose: tests/unit/test_cli_package_split.py
    pins the public names the CLI facade re-exports.
    """

    import os

    from alicebot_api.vnext_agent_keys import (
        AgentKeyAuthenticationError,
        resolve_agent_identity,
    )

    claimed = _vnext_agent_identity_from_args(args)
    raw_key = (os.environ.get(_AGENT_API_KEY_ENV) or "").strip() or None
    if raw_key is None or claimed is None:
        return claimed
    try:
        return resolve_agent_identity(
            store,
            user_id=getattr(args, "user_id", None) or getattr(store, "user_id", None),
            raw_key=raw_key,
            payload=claimed.to_record(),
        )
    except AgentKeyAuthenticationError as exc:
        raise ValueError(str(exc)) from exc


# Both helpers below import inside the function body and carry leading
# underscores on purpose. tests/unit/test_cli_package_split.py pins the exact
# set of PUBLIC names the CLI facade re-exports, so a module-level import or a
# public helper name here would fail a guard that exists to keep this package
# thin. Respecting it costs two local imports.
def _vnext_proposal_from_args(args: argparse.Namespace) -> object:
    """The MemoryProposal a CLI ``memory.propose`` call describes. Typed as
    object because a module-level import would add a public CLI name."""

    from alicebot_api.vnext_memory_propose import MemoryProposal

    canonical_text = getattr(args, "canonical_text", "") or ""
    return MemoryProposal(
        proposal_type=getattr(args, "proposal_type", None) or "candidate_memory",
        title=getattr(args, "title", None) or canonical_text[:120],
        canonical_text=canonical_text,
        memory_type=getattr(args, "memory_type", "semantic") or "semantic",
        domain=getattr(args, "domain", "unknown") or "unknown",
        sensitivity=getattr(args, "sensitivity", "unknown") or "unknown",
        confidence=getattr(args, "confidence", None),
        rationale=getattr(args, "rationale", None),
        source_refs=tuple(getattr(args, "source_ref", None) or ()),
        project_scope=tuple(getattr(args, "project_scope", None) or ()),
        source_type=getattr(args, "source_type", None) or "trusted_agent",
        contradiction_refs=tuple(getattr(args, "contradiction_ref", None) or ()),
    )


def _vnext_proposal_promotion_candidate(args: argparse.Namespace) -> object:
    """Build the promotion candidate for a CLI ``memory.propose`` call."""

    from alicebot_api.vnext_memory_propose import MemoryProposal

    return cast(MemoryProposal, _vnext_proposal_from_args(args)).promotion_candidate()


def _vnext_append_promotion_event(
    store: PostgresVNextStore,
    *,
    identity: AgentIdentity | None,
    decision: PolicyDecision,
    target_type: str,
    target_id: str,
    trace_id: str | None = None,
) -> bool:
    """Record a CLI proposal that promotion wrote instead of gating."""

    from alicebot_api.vnext_agent_control import append_promotion_event

    return append_promotion_event(
        store,
        identity=identity,
        decision=decision,
        target_type=target_type,
        target_id=target_id,
        trace_id=trace_id,
    )


def _vnext_policy_checked_for_args(
    store: PostgresVNextStore,
    args: argparse.Namespace,
    *,
    action: str,
    domains: tuple[str, ...] = (),
    workflow_type: str | None = None,
    write_policy: str | None = None,
    promotion_candidate: object | None = None,
) -> tuple[AgentIdentity | None, str, str | None, PolicyDecision]:
    # Imported inside the function on purpose. tests/unit/test_cli_package_split.py
    # pins the exact set of public names the CLI facade re-exports, and a
    # module-level import here would add two of them. That guard exists to
    # keep the CLI package thin, so it is respected rather than retargeted.
    from alicebot_api.vnext_memory_commit import _brain_charter_row, load_promotion_settings
    from alicebot_api.vnext_promotion_policy import PromotionCandidate

    if promotion_candidate is not None and not isinstance(promotion_candidate, PromotionCandidate):
        raise TypeError("promotion_candidate must be a PromotionCandidate")

    identity = _vnext_authenticated_agent_identity_from_args(store, args)
    if identity is not None:
        store.upsert_agent_identity(
            {
                "agent_id": identity.agent_id,
                "agent_type": identity.agent_type,
                "permission_profile": identity.permission_profile,
                "project_scope_json": list(identity.project_scope),
                "metadata_json": {"last_agent_run_id": identity.agent_run_id, "last_task_id": identity.task_id},
            },
            actor_type="agent",
        )
    promotion_settings = (
        load_promotion_settings(brain_charter=_brain_charter_row(store))
        if promotion_candidate is not None
        else None
    )
    decision = evaluate_agent_policy(
        identity=identity,
        action=action,
        domains=domains,
        sensitivity_allowed=_vnext_sensitivity_allowed(args),
        project_scope=tuple(getattr(args, "project_scope", None) or ()),
        workflow_type=workflow_type,
        write_policy=write_policy,
        promotion_settings=promotion_settings,
        promotion_candidate=promotion_candidate,
        # The CLI enforces no credential on an identity-less invocation, so
        # it can never vouch that the caller is the owner. An agent that
        # wants promotion here presents ALICE_AGENT_API_KEY and --agent-id.
        owner_verified=False,
    )
    append_policy_events(store, identity=identity, decision=decision)
    return (
        identity,
        ("agent" if identity is not None else "user"),
        identity.agent_id if identity is not None else None,
        decision,
    )


def _scheduler_service(store: PostgresVNextStore) -> VNextSchedulerService:
    """Bridge the concrete store to the scheduler's deliberately broad protocol."""

    return VNextSchedulerService(cast(VNextSchedulerStore, store))


def _context_tree_service(store: PostgresVNextStore) -> VNextContextTreeService:
    """Bridge the concrete store to the context-tree read protocol."""

    return VNextContextTreeService(cast(VNextContextTreeStore, store))


def _retrieval_service(store: PostgresVNextStore) -> VNextRetrievalService:
    """Bridge the concrete store to the retrieval read protocol."""

    return VNextRetrievalService(cast(VNextRetrievalStore, store))


def _persist_deferred_capture_embeddings(
    ctx: CLIContext,
    result: object,
    *,
    actor_type: str = "system",
    actor_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    """Call the embedding provider between capture and persistence transactions."""

    deferred_inputs = getattr(result, "deferred_embedding_inputs", ())
    _persist_deferred_embedding_inputs(
        ctx,
        deferred_inputs,
        actor_type=actor_type,
        actor_id=actor_id,
        trace_id=trace_id,
    )


def _persist_deferred_embedding_inputs(
    ctx: CLIContext,
    deferred_inputs: Sequence[DeferredMemoryEmbedding],
    *,
    actor_type: str = "system",
    actor_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    """Prepare a collected embedding batch, then persist it separately."""

    persist_deferred_memory_embeddings_best_effort(
        deferred_inputs,
        store_context=lambda: _vnext_store_context(ctx),
        actor_type=actor_type,
        actor_id=actor_id,
        trace_id=trace_id,
    )
