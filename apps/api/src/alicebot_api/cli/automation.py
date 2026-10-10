from __future__ import annotations

import argparse
from alicebot_api.vnext_artifact_review import dispatch_vnext_artifact_review
from alicebot_api.vnext_connections import ConnectionFinderRequest, VNextConnectionService
from alicebot_api.vnext_contradictions import ContradictionFinderRequest, VNextContradictionService
from alicebot_api.vnext_projects import ProjectAutomationRequest, VNextProjectService
from alicebot_api.vnext_queue import QueueTaskRequest, VNextQueueService
from .models import CLIContext
from .context import _model_generation_kwargs_from_args
from .shared import (
    _checked_batch_output,
    _json_dumps,
    _persist_deferred_embedding_inputs,
    _vnext_sensitivity_allowed,
    _vnext_store_context,
)


def _connection_finder_request_from_args(args: argparse.Namespace) -> ConnectionFinderRequest:
    return ConnectionFinderRequest(agent_identity=None,
        query=getattr(args, "query", "") or "",
        domains=tuple(args.domain),
        projects=tuple(getattr(args, "project", ())),
        sensitivity_allowed=_vnext_sensitivity_allowed(args),
        max_connections=args.max_connections,
        auto_accept_threshold=args.auto_accept_threshold,
        **_model_generation_kwargs_from_args(args),
    )


def _run_connections_generate(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        artifact = VNextConnectionService(store).generate_connection_report(_connection_finder_request_from_args(args))
    return _json_dumps(artifact)


def _run_vnext_graph_review(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        edge = VNextConnectionService(store).review_edge(edge_id=args.edge_id, action=args.action)
    return _json_dumps(edge)


def _run_vnext_graph_neighborhood(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        payload = VNextConnectionService(store).graph_neighborhood(target_id=args.target_id)
    return _json_dumps(payload)


def _contradiction_finder_request_from_args(args: argparse.Namespace) -> ContradictionFinderRequest:
    return ContradictionFinderRequest(agent_identity=None,
        query=getattr(args, "query", "") or "",
        domains=tuple(args.domain),
        projects=tuple(getattr(args, "project", ())),
        sensitivity_allowed=_vnext_sensitivity_allowed(args),
        max_contradictions=args.max_contradictions,
        **_model_generation_kwargs_from_args(args),
    )


def _run_vnext_contradictions_generate(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        artifact = VNextContradictionService(store).generate_contradiction_report(
            _contradiction_finder_request_from_args(args)
        )
    return _json_dumps(artifact)


def _run_vnext_belief_review(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        belief = VNextContradictionService(store).review_belief(
            belief_id=args.belief_id,
            action=args.action,
            confidence=args.confidence,
            superseded_by=args.superseded_by,
        )
    return _json_dumps(belief)


def _run_vnext_belief_state(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        payload = VNextContradictionService(store).belief_state(belief_id=args.belief_id)
    return _json_dumps(payload)


def _project_automation_request_from_args(args: argparse.Namespace) -> ProjectAutomationRequest:
    return ProjectAutomationRequest(agent_identity=None,
        domains=tuple(args.domain),
        sensitivity_allowed=_vnext_sensitivity_allowed(args),
        project_id=getattr(args, "project_id", None),
        person_id=getattr(args, "person_id", None),
        max_items=args.max_items,
        **_model_generation_kwargs_from_args(args),
    )


def _run_vnext_project_update_candidate(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        artifact = VNextProjectService(store).generate_project_update_candidate(
            _project_automation_request_from_args(args)
        )
    return _json_dumps(artifact)


def _run_vnext_project_update_review(ctx: CLIContext, args: argparse.Namespace) -> str:
    actor_id = str(ctx.user_id)
    with _vnext_store_context(ctx) as store:
        service = VNextProjectService(store, defer_embeddings=True)
        artifact = service.review_project_update(
            artifact_id=args.artifact_id,
            action=args.action,
            edited_current_state=args.edited_current_state,
            actor_type="user",
            actor_id=actor_id,
        )
    _persist_deferred_embedding_inputs(
        ctx,
        service.deferred_embedding_inputs,
        actor_type="user",
        actor_id=actor_id,
    )
    return _json_dumps(artifact)


def _run_vnext_project_dashboard(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        payload = VNextProjectService(store).project_dashboard(
            project_id=args.project_id,
            sensitivity_allowed=_vnext_sensitivity_allowed(args),
        )
    return _json_dumps(payload)


def _run_vnext_open_loops_extract(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        from alicebot_api.vnext_label_guard import LabelGuard

        # The command takes no agent identity: it runs for the owner of the vault, who has no limits.
        loops = VNextProjectService(store).extract_open_loops(
            _project_automation_request_from_args(args), guard=LabelGuard.unlimited(store)
        )
    return _json_dumps({"open_loops": loops, "created_count": len(loops)})


def _run_vnext_open_loop_review(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        loop = VNextProjectService(store).review_open_loop(
            loop_id=args.loop_id,
            action=args.action,
            title=args.title,
            description=args.description,
            due_at=args.due_at,
            priority=args.priority,
            resolution_note=args.resolution_note,
        )
    return _json_dumps(loop)


def _run_vnext_queue_add(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        task = VNextQueueService(store).enqueue_task(
            QueueTaskRequest(
                title=args.title,
                task_type=args.type,
                instructions=args.instructions,
                requested_by="cli",
                domain=args.domain,
                sensitivity=args.sensitivity,
                write_policy=args.write_policy,
            )
        )
    return _json_dumps(task)


def _run_vnext_queue_process_next(ctx: CLIContext, _args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        result = VNextQueueService(store).process_next_task()
    return _checked_batch_output(result.to_record())


def _run_vnext_artifact_review(ctx: CLIContext, args: argparse.Namespace) -> str:
    actor_id = str(ctx.user_id)
    with _vnext_store_context(ctx) as store:
        result = dispatch_vnext_artifact_review(
            store,
            artifact_id=args.artifact_id,
            action=args.action,
            actor_type="user",
            actor_id=actor_id,
        )
    _persist_deferred_embedding_inputs(
        ctx,
        result.deferred_embedding_inputs,
        actor_type="user",
        actor_id=actor_id,
    )
    return _json_dumps(result.artifact)


def _run_vnext_artifact_export(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        output_path = VNextQueueService(store).export_artifact_markdown(
            artifact_id=args.artifact_id,
            output_dir=args.output_dir,
        )
    return _json_dumps({"artifact_id": args.artifact_id, "output_path": str(output_path)})


def _run_vnext_quality_rate(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        if store.get_artifact(args.artifact_id) is None:
            raise ValueError(f"artifact {args.artifact_id} was not found")
        rating = store.create_artifact_quality_rating(
            {
                "artifact_id": args.artifact_id,
                "reviewer_id": args.reviewer_id,
                "usefulness": args.usefulness,
                "accuracy": args.accuracy,
                "source_grounding": args.source_grounding,
                "novel_connections": args.novel_connections,
                "actionability": args.actionability,
                "hallucination_risk": args.hallucination_risk,
                "verbosity": args.verbosity,
                "missed_context": args.missed_context,
                "comments": args.comments,
                "metadata_json": {"created_from": "cli"},
            },
            actor_type="user",
        )
    return _json_dumps(rating)


def _run_vnext_quality_export(ctx: CLIContext, args: argparse.Namespace) -> str:
    limit = max(1, min(args.limit, 500))
    with _vnext_store_context(ctx) as store:
        rows = store.list_artifact_quality_ratings(
            artifact_id=args.artifact_id,
            limit=limit,
        )
    return _json_dumps(
        {
            "items": rows,
            "count": len(rows),
            "export": {
                "format": "json",
                "rating_fields": [
                    "usefulness",
                    "accuracy",
                    "source_grounding",
                    "novel_connections",
                    "actionability",
                    "hallucination_risk",
                    "verbosity",
                    "missed_context",
                ],
            },
        }
    )
