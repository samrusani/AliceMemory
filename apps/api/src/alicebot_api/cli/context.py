from __future__ import annotations

import argparse
from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from alicebot_api.vnext_context_tree import ContextTreeRequest
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_retrieval import VNextRetrievalRequest
from .models import CLIContext, ModelGenerationKwargs
from .shared import (
    _context_tree_service,
    _json_dumps,
    _retrieval_service,
    _vnext_sensitivity_allowed,
    _vnext_store_context,
)
from alicebot_api.vnext_source_fence import SourceReadFence as _SourceReadFence


def _run_context_pack(ctx: CLIContext, args: argparse.Namespace) -> str:
    query = " ".join(args.query).strip()
    # Only forwarded when the caller sets them, so the retrieval request
    # dataclass stays the source of truth for the tier defaults.
    tuning_kwargs: dict[str, object] = {}
    if args.context_depth is not None:
        tuning_kwargs["context_depth"] = args.context_depth
    if args.budget_strategy is not None:
        tuning_kwargs["budget_strategy"] = args.budget_strategy
    with _vnext_store_context(ctx) as store:
        payload = _retrieval_service(store).compile_context_pack(
            VNextRetrievalRequest(
                query=query,
                domains=tuple(args.domain),
                projects=tuple(args.project),
                people=tuple(args.person),
                sensitivity_allowed=_vnext_sensitivity_allowed(args),
                # Tri-state flags: omitted means "let the context_depth tier
                # decide"; --sources/--no-sources force an explicit value.
                include_sources=args.sources,
                include_contradictions=args.contradictions,
                max_items=args.max_items,
                max_tokens=args.max_tokens,
                **tuning_kwargs,  # type: ignore[arg-type]
            ),
            source_fence=_SourceReadFence.unfenced()
        )
    return _json_dumps(payload)


def _run_vnext_context_tree(ctx: CLIContext, args: argparse.Namespace) -> str:
    query = " ".join(args.query).strip()
    with _vnext_store_context(ctx) as store:
        payload = _context_tree_service(store).build_tree(
            ContextTreeRequest(
                query=query,
                domains=tuple(args.domain),
                sensitivity_allowed=_vnext_sensitivity_allowed(args),
                limit=args.limit,
                include_events=not args.no_events,
                generated_by="cli",
                caller_limited=False,
            )
        )
    return _json_dumps(payload)


def _brain_artifact_request_from_args(args: argparse.Namespace) -> BrainArtifactRequest:
    return BrainArtifactRequest(agent_identity=None,
        domains=tuple(args.domain),
        projects=tuple(getattr(args, "project", ())),
        sensitivity_allowed=_vnext_sensitivity_allowed(args),
        generated_for=args.generated_for,
        source_limit=args.source_limit,
        memory_limit=args.memory_limit,
        open_loop_limit=args.open_loop_limit,
        artifact_limit=args.artifact_limit,
        discover_open_loops=not args.no_discover_open_loops,
        create_candidate_memories=not args.no_candidate_memories,
        **_model_generation_kwargs_from_args(args),
    )


def _model_generation_kwargs_from_args(args: argparse.Namespace) -> ModelGenerationKwargs:
    return {
        "generation_mode": getattr(args, "generation_mode", "deterministic"),
        "model_route_mode": getattr(args, "model_route_mode", None),
        "model_provider": getattr(args, "model_provider", None),
        "model": getattr(args, "model", None),
        "model_temperature": getattr(args, "model_temperature", 0.2),
        "allow_cloud_private": getattr(args, "allow_cloud_private", False),
    }


def _model_generation_options_from_args(args: argparse.Namespace) -> JsonObject:
    return {
        "generation_mode": getattr(args, "generation_mode", "deterministic"),
        "model_route_mode": getattr(args, "model_route_mode", None),
        "model_provider": getattr(args, "model_provider", None),
        "model": getattr(args, "model", None),
        "model_temperature": getattr(args, "model_temperature", 0.2),
        "allow_cloud_private": getattr(args, "allow_cloud_private", False),
    }


def _run_daily_brief(ctx: CLIContext, args: argparse.Namespace) -> str:
    del args.generate
    with _vnext_store_context(ctx) as store:
        artifact = VNextBrainService(store).generate_daily_brief(_brain_artifact_request_from_args(args))
    return _json_dumps(artifact)


def _run_weekly_synthesis(ctx: CLIContext, args: argparse.Namespace) -> str:
    del args.generate
    with _vnext_store_context(ctx) as store:
        artifact = VNextBrainService(store).generate_weekly_synthesis(_brain_artifact_request_from_args(args))
    return _json_dumps(artifact)
