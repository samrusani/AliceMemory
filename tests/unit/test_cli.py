from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import alicebot_api.cli as cli_module
from alicebot_api.config import Settings
from alicebot_api.contracts import ContinuityRecallResponse
from alicebot_api.vnext_embeddings import VNextEmbeddingProviderError
from alicebot_api.vnext_event_log import build_event_log_record


def _assert_cli_error(stderr: str, *, code: str, message: str) -> None:
    records = [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]
    assert records == [{"error": {"code": code, "message": message}}]
    assert "Traceback" not in stderr


def _nested_subcommand_parser(
    parser: argparse.ArgumentParser,
    *commands: str,
) -> argparse.ArgumentParser:
    current = parser
    for command in commands:
        subparser_actions = [
            action for action in current._actions if isinstance(action, argparse._SubParsersAction)
        ]
        assert len(subparser_actions) == 1
        current = subparser_actions[0].choices[command]
    return current


def _subcommand_names(parser: argparse.ArgumentParser) -> set[str]:
    subparser_actions = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]
    assert len(subparser_actions) == 1
    return set(subparser_actions[0].choices)


def _cli_source_text() -> str:
    package_init = Path(cli_module.__file__).resolve()
    assert package_init.name == "__init__.py"
    assert package_init.parent.name == "cli"
    source_paths = sorted(package_init.parent.rglob("*.py"))
    assert source_paths
    return "\n".join(path.read_text(encoding="utf-8") for path in source_paths)


def test_parser_routes_required_commands() -> None:
    parser = cli_module.build_parser()
    continuity_object_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    cases = [
        (["capture", "Decision: Keep rollout phased"], "_run_capture"),
        (["context-pack", "What should Alice remember?"], "_run_context_pack"),
        (["daily-brief", "--generate"], "_run_daily_brief"),
        (["weekly-synthesis", "--generate"], "_run_weekly_synthesis"),
        (["connections", "generate"], "_run_connections_generate"),
        (["vnext", "connectors", "list"], "_run_vnext_connectors_list"),
        (
            ["vnext", "connectors", "ingest", "browser_clipper", "clips.json"],
            "_run_vnext_connectors_ingest",
        ),
        (["vnext", "sources", "capture-text", "Fact: Keep vNext provenance-first"], "_run_vnext_sources_capture_text"),
        (["vnext", "sources", "capture-file", "notes.md"], "_run_vnext_sources_capture_file"),
        (["vnext", "sources", "import-markdown", "notes"], "_run_vnext_sources_import_markdown"),
        (["vnext", "sources", "import-chatgpt", "conversations.json"], "_run_vnext_sources_import_chatgpt"),
        (
            ["vnext", "queue", "add", "--type", "synthesize", "--title", "T", "--instructions", "Do it"],
            "_run_vnext_queue_add",
        ),
        (["vnext", "queue", "process-next"], "_run_vnext_queue_process_next"),
        (["vnext", "artifacts", "review", "artifact-1", "--action", "accept"], "_run_vnext_artifact_review"),
        (["vnext", "artifacts", "export", "artifact-1", "--output-dir", "."], "_run_vnext_artifact_export"),
        (["vnext", "graph", "review", "edge-1", "--action", "accept"], "_run_vnext_graph_review"),
        (["vnext", "graph", "neighborhood", "source-1"], "_run_vnext_graph_neighborhood"),
        (["vnext", "contradictions", "generate"], "_run_vnext_contradictions_generate"),
        (["vnext", "beliefs", "review", "belief-1", "--action", "challenge"], "_run_vnext_belief_review"),
        (["vnext", "beliefs", "state", "belief-1"], "_run_vnext_belief_state"),
        (["vnext", "projects", "update-candidate"], "_run_vnext_project_update_candidate"),
        (
            ["vnext", "projects", "review-update", "artifact-1", "--action", "accept"],
            "_run_vnext_project_update_review",
        ),
        (["vnext", "projects", "dashboard", "project-1"], "_run_vnext_project_dashboard"),
        (["vnext", "open-loops", "extract"], "_run_vnext_open_loops_extract"),
        (["vnext", "open-loops", "review", "loop-1", "--action", "close"], "_run_vnext_open_loop_review"),
        (
            ["vnext", "agents", "propose-memory", "--agent-id", "hermes", "--title", "T", "--canonical-text", "Fact"],
            "_run_vnext_agent_propose_memory",
        ),
        (
            ["agent", "keys", "create", "--agent-id", "hermes", "--profile", "trusted_local_agent"],
            "_run_agent_keys_create",
        ),
        (["agent", "keys", "list"], "_run_agent_keys_list"),
        (["agent", "keys", "revoke", "alice_sk_abc1"], "_run_agent_keys_revoke"),
        (
            ["vnext", "memories", "commit", "--agent-id", "hermes", "--title", "T", "--text", "Fact"],
            "_run_vnext_memory_commit",
        ),
        (["vnext", "memories", "confirm", "confirm-1"], "_run_vnext_memory_confirm"),
        (["vnext", "memories", "undo"], "_run_vnext_memory_undo"),
        (["vnext", "memories", "correct", "memory-1", "--text", "Corrected"], "_run_vnext_memory_correct"),
        (["vnext", "memories", "forget", "memory-1"], "_run_vnext_memory_forget"),
        (["vnext", "memories", "expire", "memory-1", "--reason", "Window closed"], "_run_vnext_memory_expire"),
        (["vnext", "memories", "unexpire", "memory-1", "--reason", "Extended"], "_run_vnext_memory_unexpire"),
        (["vnext", "memories", "redact", "memory-1", "--reason", "Erasure"], "_run_vnext_memory_redact"),
        (
            ["vnext", "memories", "accept-consolidation", "memory-1", "--reason", "Merge duplicates"],
            "_run_vnext_memory_accept_consolidation",
        ),
        (["vnext", "memories", "recent"], "_run_vnext_memory_recent"),
        (["vnext", "memories", "audit", "memory-1"], "_run_vnext_memory_audit"),
        (["vnext", "memories", "backfill-embeddings"], "_run_vnext_memories_backfill_embeddings"),
        (["maintenance", "sync-contradictions"], "_run_maintenance_sync_contradictions"),
        (["vnext", "scheduler", "status"], "_run_vnext_scheduler_status"),
        (["vnext", "scheduler", "run-now", "daily_brief"], "_run_vnext_scheduler_run_now"),
        (["vnext", "scheduler", "run-due"], "_run_vnext_scheduler_run_due"),
        (["vnext", "scheduler", "pause"], "_run_vnext_scheduler_pause"),
        (["vnext", "scheduler", "resume"], "_run_vnext_scheduler_resume"),
        (["vnext", "smoke", "agentic-memory-commit"], "_run_vnext_smoke_agentic_memory_commit"),
        (["vnext", "smoke", "agentic-scheduler"], "_run_vnext_smoke_agentic_scheduler"),
        (["vnext", "doctor"], "_run_vnext_doctor"),
        (["vnext", "migrations", "status"], "_run_vnext_migrations_status"),
        (["vnext", "smoke", "connector-hardening"], "_run_vnext_smoke_connector_hardening"),
        (["vnext", "smoke", "local-cors"], "_run_vnext_smoke_local_cors"),
        (["vnext", "smoke", "secret-redaction"], "_run_vnext_smoke_secret_redaction"),
        (["vnext", "smoke", "dogfood-doctor"], "_run_vnext_smoke_dogfood_doctor"),
        (["mutations", "generate"], "_run_mutation_generate"),
        (["mutations", "candidates"], "_run_mutation_candidates"),
        (["mutations", "commit"], "_run_mutation_commit"),
        (["mutations", "operations"], "_run_mutation_operations"),
        (["brief"], "_run_brief"),
        (["recall"], "_run_recall"),
        (["state-at", continuity_object_id], "_run_state_at"),
        (["timeline", continuity_object_id], "_run_timeline"),
        (["lifecycle", "list"], "_run_lifecycle_list"),
        (["lifecycle", "show", continuity_object_id], "_run_lifecycle_show"),
        (["resume"], "_run_resume"),
        (["open-loops"], "_run_open_loops"),
        (["review", "queue"], "_run_review_queue"),
        (["review", "show", continuity_object_id], "_run_review_show"),
        (["review", "apply", continuity_object_id, "--action", "confirm"], "_run_review_apply"),
        (["contradictions", "detect"], "_run_contradictions_detect"),
        (["contradictions", "list"], "_run_contradictions_list"),
        (["contradictions", "show", continuity_object_id], "_run_contradictions_show"),
        (
            ["contradictions", "resolve", continuity_object_id, "--action", "confirm_primary"],
            "_run_contradictions_resolve",
        ),
        (["trust", "signals"], "_run_trust_signals"),
        (["explain", continuity_object_id], "_run_explain"),
        (["explain", "--entity-id", continuity_object_id], "_run_explain"),
        (["evidence", "artifact", continuity_object_id], "_run_evidence_artifact"),
        (["patterns", "list"], "_run_pattern_list"),
        (["patterns", "explain", continuity_object_id], "_run_pattern_explain"),
        (["playbooks", "list"], "_run_playbook_list"),
        (["playbooks", "explain", continuity_object_id], "_run_playbook_explain"),
        (["status"], "_run_status"),
        (["eval", "seed"], "_run_vnext_eval_seed"),
        (["eval", "run", "--suite", "all"], "_run_vnext_eval_run"),
        (["eval", "report"], "_run_vnext_eval_report"),
        (["evals", "suites"], "_run_eval_suites"),
        (["evals", "run"], "_run_eval_run"),
        (["evals", "runs"], "_run_eval_runs"),
        (["evals", "show", continuity_object_id], "_run_eval_show"),
    ]

    for argv, expected_handler_name in cases:
        parsed = parser.parse_args(argv)
        assert parsed.handler.__name__ == expected_handler_name


@pytest.mark.parametrize("flag_value", [None, "", "0", "true", " 1", "1 "])
def test_task_brief_cli_is_absent_unless_legacy_surfaces_is_exactly_one(
    monkeypatch,
    flag_value: str | None,
) -> None:
    if flag_value is None:
        monkeypatch.delenv("ALICE_LEGACY_SURFACES", raising=False)
    else:
        monkeypatch.setenv("ALICE_LEGACY_SURFACES", flag_value)

    assert "task-briefs" not in _subcommand_names(cli_module.build_parser())


def test_task_brief_cli_is_flagged_and_uses_neutral_strategy_options(monkeypatch) -> None:
    monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")
    parser = cli_module.build_parser()
    continuity_object_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    cases = [
        (["task-briefs", "compile", "--mode", "resume"], "_run_task_brief_compile"),
        (["task-briefs", "show", continuity_object_id], "_run_task_brief_show"),
        (
            [
                "task-briefs",
                "compare",
                "--mode",
                "worker_subtask",
                "--compare-to-mode",
                "user_recall",
            ],
            "_run_task_brief_compare",
        ),
    ]
    for argv, expected_handler_name in cases:
        assert parser.parse_args(argv).handler.__name__ == expected_handler_name

    parsed = parser.parse_args(
        [
            "task-briefs",
            "compare",
            "--mode",
            "resume",
            "--briefing-strategy",
            "compact",
            "--compare-to-mode",
            "user_recall",
            "--compare-briefing-strategy",
            "detailed",
        ]
    )
    assert parsed.briefing_strategy == "compact"
    assert parsed.compare_briefing_strategy == "detailed"

    compile_parser = _nested_subcommand_parser(parser, "task-briefs", "compile")
    compare_parser = _nested_subcommand_parser(parser, "task-briefs", "compare")
    option_strings = {
        option
        for command_parser in (compile_parser, compare_parser)
        for action in command_parser._actions
        for option in action.option_strings
    }
    assert {
        "--workspace-id",
        "--pack-id",
        "--pack-version",
        "--model-pack-strategy",
        "--compare-model-pack-strategy",
    }.isdisjoint(option_strings)


def test_dedicated_telegram_polling_cli_is_absent() -> None:
    parser = cli_module.build_parser()
    connectors_parser = _nested_subcommand_parser(parser, "vnext", "connectors")

    assert "telegram" not in _subcommand_names(connectors_parser)
    assert not hasattr(cli_module, "_run_vnext_telegram_configure")
    assert not hasattr(cli_module, "_run_vnext_telegram_test")
    assert not hasattr(cli_module, "_run_vnext_telegram_sync")


def test_cli_contains_no_telegram_polling_or_bot_token_residue() -> None:
    source = _cli_source_text()

    for forbidden in (
        "TelegramPollContext",
        "poll_telegram_updates",
        "TELEGRAM_BOT_TOKEN",
        "telegram.bot_token",
        "telegram_token_absent",
    ):
        assert forbidden not in source


def test_continuity_brief_formatter_uses_neutral_briefing_strategy() -> None:
    empty_section = {
        "items": [],
        "summary": {"limit": 0, "total_count": 0, "order": "created_at_desc"},
        "empty_state": {"message": "none"},
    }
    payload = {
        "brief": {
            "brief_type": "general",
            "assembly_version": "continuity_brief_v0",
            "scope": {},
            "summary": "No current context.",
            "selection_strategy": {
                "task_brief_mode": "user_recall",
                "provider_strategy": "continuity_brief.general",
                "briefing_strategy": "balanced",
                "token_budget": 320,
                "budget_source": "mode_default",
            },
            "trust_posture": {
                "confidence_posture": "low",
                "average_confidence": 0.0,
                "strongest_trust_class": "unknown",
                "weakest_provenance_posture": "unknown",
                "active_signal_count": 0,
                "open_conflict_count": 0,
                "rationale": "No trusted context.",
            },
            "provenance_bundle": {
                "summary": {
                    "source_object_count": 0,
                    "reference_count": 0,
                    "reference_kind_count": 0,
                }
            },
            "sources": [],
            "relevant_facts": empty_section,
            "recent_changes": empty_section,
            "open_loops": empty_section,
            "conflicts": {"items": [], "empty_state": {"message": "none"}},
            "timeline_highlights": {"items": [], "empty_state": {"message": "none"}},
            "next_suggested_action": {
                "title": "Review current sources",
                "object_type": "source",
                "continuity_object_id": None,
                "confidence_posture": "low",
                "reason": "No context is available.",
                "provenance_references": [],
            },
        }
    }

    rendered = cli_module.format_continuity_brief_output(payload)

    assert "provider=continuity_brief.general briefing=balanced" in rendered
    assert "model_pack" not in rendered


def test_context_pack_parser_tuning_and_tri_state_flags() -> None:
    parser = cli_module.build_parser()

    omitted = parser.parse_args(["context-pack", "coffee"])
    # Tri-state: omitted flags stay None so the context_depth tier decides.
    assert omitted.sources is None
    assert omitted.contradictions is None
    assert omitted.context_depth is None
    assert omitted.budget_strategy is None

    explicit = parser.parse_args(
        [
            "context-pack",
            "coffee",
            "--no-sources",
            "--contradictions",
            "--context-depth",
            "minimal",
            "--budget-strategy",
            "facts_first",
        ]
    )
    assert explicit.sources is False
    assert explicit.contradictions is True
    assert explicit.context_depth == "minimal"
    assert explicit.budget_strategy == "facts_first"


def test_new_memory_lifecycle_subcommands_require_a_reason() -> None:
    parser = cli_module.build_parser()
    for subcommand in ("expire", "unexpire", "redact", "accept-consolidation"):
        with pytest.raises(SystemExit):
            parser.parse_args(["vnext", "memories", subcommand, "memory-1"])


def test_memory_redact_help_is_truthful_about_reason_retention() -> None:
    parser = cli_module.build_parser()
    memories_parser = _nested_subcommand_parser(parser, "vnext", "memories")
    redact_parser = _nested_subcommand_parser(
        parser,
        "vnext",
        "memories",
        "redact",
    )
    rendered_help = " ".join(
        f"{memories_parser.format_help()} {redact_parser.format_help()}".split()
    )

    assert "governed memory-lifecycle copies" in rendered_help
    assert "Alice source/source-chunk evidence is retained" in rendered_help
    assert "content everywhere" not in rendered_help
    assert "Required for authorization and lifecycle intent" in rendered_help
    assert "intentionally not retained after successful true redaction" in rendered_help
    assert "Redaction reason. Stored in the audit trail." not in rendered_help


def test_parser_preserves_explicit_vnext_sensitivity_filter() -> None:
    parser = cli_module.build_parser()
    explicit = parser.parse_args(["context-pack", "coffee", "--sensitivity-allowed", "public"])
    omitted = parser.parse_args(["context-pack", "coffee"])

    assert explicit.sensitivity_allowed == ["public"]
    assert cli_module._vnext_sensitivity_allowed(explicit) == ("public",)
    assert omitted.sensitivity_allowed is None
    assert cli_module._vnext_sensitivity_allowed(omitted) == ("public", "internal", "private", "unknown")


class FakeVNextCliStore:
    def __init__(self) -> None:
        self.sources: list[dict[str, object]] = []
        self.chunks: list[dict[str, object]] = []
        self.memories: list[dict[str, object]] = []
        self.open_loops: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []
        self.provenance_links: list[dict[str, object]] = []
        self.source_by_hash: dict[str, dict[str, object]] = {}
        self.tasks: list[dict[str, object]] = []
        self.artifacts: dict[str, dict[str, object]] = {}
        self.edges: dict[str, dict[str, object]] = {}
        self.beliefs: dict[str, dict[str, object]] = {}
        self.projects: dict[str, dict[str, object]] = {}
        self.revisions: list[dict[str, object]] = []
        self.agent_identities: dict[str, dict[str, object]] = {}
        self.agent_api_keys: list[dict[str, object]] = []
        self.scheduler_workflows: dict[str, dict[str, object]] = {}
        self.scheduler_runs: list[dict[str, object]] = []

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event

    def read_label_rows(self, kind: str, ids: list[str]) -> list[dict[str, object]]:
        """Mirror narrow dependency reads for this fixture's stored rows."""

        from alicebot_api.vnext_derived_labels import identifier

        rows = {
            "source": self.sources,
            "memory": self.memories,
            "open_loop": self.open_loops,
            "artifact": list(self.artifacts.values()),
            "project": list(self.projects.values()),
        }.get(kind, [])
        wanted = {identifier(value) for value in ids}
        fields = ("id", "user_id", "domain", "sensitivity", "metadata_json", "value", "project_id", "source_id", "memory_id", "status", "memory_type", "artifact_type")
        return [
            {field: row[field] for field in fields if field in row}
            for row in rows if identifier(row.get("id")) in wanted
        ]

    def upsert_agent_identity(self, identity: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {
            **identity,
            "id": self.agent_identities.get(str(identity["agent_id"]), {}).get("id")
            or f"agent-{len(self.agent_identities) + 1}",
        }
        self.agent_identities[str(identity["agent_id"])] = row
        return row

    def create_agent_api_key(self, key: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {
            **key,
            "id": str(uuid4()),
            "created_at": "now",
            "revoked_at": None,
            "last_used_at": None,
        }
        self.agent_api_keys.append(row)
        return row

    def get_agent_api_key_by_hash(self, key_hash: str) -> dict[str, object] | None:
        for row in self.agent_api_keys:
            if row.get("key_hash") == key_hash:
                return row
        return None

    def list_agent_api_keys(self, *, limit: int = 50) -> list[dict[str, object]]:
        return self.agent_api_keys[:limit]

    def revoke_agent_api_key(self, *, key_id: str, **_kwargs) -> dict[str, object] | None:
        for row in self.agent_api_keys:
            if row["id"] == key_id and row.get("revoked_at") is None:
                row["revoked_at"] = "now"
                return row
        return None

    def touch_agent_api_key(self, *, key_id: str) -> dict[str, object]:
        for row in self.agent_api_keys:
            if row["id"] == key_id:
                row["last_used_at"] = "now"
                return row
        raise AssertionError(key_id)

    def count_active_agent_api_keys(self) -> int:
        return len([row for row in self.agent_api_keys if row.get("revoked_at") is None])

    def get_source_by_content_hash(self, content_hash: str) -> dict[str, object] | None:
        return self.source_by_hash.get(content_hash)

    def create_source(self, source: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**source, "id": f"source-{len(self.sources) + 1}"}
        self.sources.append(row)
        self.source_by_hash[str(source["content_hash"])] = row
        return row

    def create_source_chunk(self, chunk: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**chunk, "id": f"chunk-{len(self.chunks) + 1}"}
        self.chunks.append(row)
        return row

    def create_memory(self, memory: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**memory, "id": f"memory-{len(self.memories) + 1}"}
        self.memories.append(row)
        return row

    def get_memory_for_update(self, memory_id: str) -> dict[str, object] | None:
        """Mirror the production locking read for single-threaded CLI tests."""

        return self.get_memory(memory_id)

    def get_memory_for_redaction(self, memory_id: str) -> dict[str, object] | None:
        return self.get_memory(memory_id)

    def lock_project_update_artifacts_for_redaction(self, memory_id: str) -> list[dict[str, object]]:
        return sorted(
            [
                artifact
                for artifact in self.artifacts.values()
                if isinstance(artifact.get("metadata_json"), dict)
                and artifact["metadata_json"].get("candidate_memory_id") == memory_id
            ],
            key=lambda artifact: str(artifact.get("id") or ""),
        )

    def redact_memory_bundle(
        self,
        *,
        memory_id: str,
        project_update_artifacts: list[dict[str, object]],
        actor_type: str = "user",
    ) -> dict[str, object]:
        memory = self.get_memory(memory_id)
        assert memory is not None, memory_id
        metadata = memory.get("metadata_json")
        redacted_at = (
            str(metadata.get("redacted_at") or "now") if isinstance(metadata, dict) else "now"
        )
        structural = {
            key: metadata[key]
            for key in (
                "project_id",
                "project_scope",
                "superseded_by",
                "supersedes",
                "run_id",
                "agent_id",
                "created_by_agent_id",
            )
            if isinstance(metadata, dict) and key in metadata
        }
        desired_memory = {
            "memory_key": f"redacted.{memory_id}",
            "title": None if memory.get("title") is None else "[REDACTED]",
            "canonical_text": "[REDACTED]",
            "summary": None if memory.get("summary") is None else "[REDACTED]",
            "trust_reason": None if memory.get("trust_reason") is None else "[REDACTED]",
            "value": {"redacted": True},
            "source_event_ids": [],
            "metadata_json": {**structural, "redacted": True, "redacted_at": redacted_at},
            "commit_digest": None,
            "confirmation_id": None,
            "embedding_vector": None,
            "fact_keys": None,
            "status": "archived",
            "deleted_at": memory.get("deleted_at") or "now",
        }
        memory_changed = any(memory.get(key) != value for key, value in desired_memory.items())
        memory.update(desired_memory)

        redacted_revisions = 0
        for revision in self.revisions:
            if str(revision.get("memory_id")) != memory_id:
                continue
            desired_revision = {
                "memory_key": f"redacted.{memory_id}",
                "previous_value": None if revision.get("previous_value") is None else {"redacted": True},
                "new_value": None if revision.get("new_value") is None else {"redacted": True},
                "source_event_ids": [],
                "candidate": {"redacted": True},
                "text_before": None if revision.get("text_before") is None else "[REDACTED]",
                "text_after": "[REDACTED]",
                "reason": None if revision.get("reason") is None else "[REDACTED]",
                "metadata_json": {"redacted": True},
            }
            if any(revision.get(key) != value for key, value in desired_revision.items()):
                revision.update(desired_revision)
                redacted_revisions += 1

        coupled_artifact_ids = [str(artifact["id"]) for artifact in project_update_artifacts]
        changed_artifact_ids: list[str] = []
        for artifact in project_update_artifacts:
            artifact_id = str(artifact["id"])
            old_metadata = artifact.get("metadata_json")
            assert isinstance(old_metadata, dict)
            desired_artifact = {
                "title": "[REDACTED]",
                "content_markdown": "[REDACTED]",
                "prompt_hash": None,
                "model_info_json": {"redacted": True},
                "metadata_json": {
                    "redacted": True,
                    "redacted_at": redacted_at,
                    "workflow": "project_auto_update",
                    "project_id": old_metadata["project_id"],
                    "project_scope": [old_metadata["project_id"]],
                    "candidate_memory_id": memory_id,
                    "review_action": old_metadata["review_action"],
                },
            }
            if any(artifact.get(key) != value for key, value in desired_artifact.items()):
                artifact.update(desired_artifact)
                changed_artifact_ids.append(artifact_id)

        redacted_events = 0
        for event in self.events:
            payload = event.get("payload_json")
            coupled = str(event.get("target_id")) in {memory_id, *coupled_artifact_ids} or (
                isinstance(payload, dict)
                and any(
                    str(payload.get(key)) in {memory_id, *coupled_artifact_ids}
                    for key in ("memory_id", "candidate_memory_id", "artifact_id")
                )
            )
            if not coupled:
                continue
            desired_payload = {
                "redacted": True,
                "memory_id": memory_id,
                "event_type": event.get("event_type"),
            }
            if event.get("payload_json") != desired_payload or event.get("integrity_hash") is not None:
                event["payload_json"] = desired_payload
                event["integrity_hash"] = None
                redacted_events += 1

        changed = bool(memory_changed or changed_artifact_ids or redacted_revisions or redacted_events)
        if changed:
            self.append_event(
                {
                    "event_type": "memory.redacted",
                    "actor_type": actor_type,
                    "target_type": "memory",
                    "target_id": memory_id,
                    "payload_json": {
                        "redacted": True,
                        "memory_id": memory_id,
                        "event_type": "memory.redacted",
                    },
                    "integrity_hash": None,
                }
            )
        return {
            "memory": memory,
            "redacted_revisions": redacted_revisions,
            "redacted_events": redacted_events,
            "redacted_artifacts": len(changed_artifact_ids),
            "redacted_artifact_ids": changed_artifact_ids,
            "redacted_quality_ratings": 0,
            "redacted_provenance_links": 0,
            "idempotent_replay": not changed,
        }

    def get_memory(self, memory_id: str) -> dict[str, object] | None:
        return next(
            (memory for memory in self.memories if memory.get("id") == memory_id),
            None,
        )

    def update_memory(self, *, memory_id: str, patch: dict[str, object], **_kwargs) -> dict[str, object]:
        for memory in self.memories:
            if memory["id"] == memory_id:
                memory.update(patch)
                return memory
        raise AssertionError(memory_id)

    def append_revision(self, revision: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**revision, "id": f"revision-{len(self.revisions) + 1}"}
        self.revisions.append(row)
        return row

    def list_revisions(self, memory_id: str) -> list[dict[str, object]]:
        return [revision for revision in self.revisions if revision.get("memory_id") == memory_id]

    def create_provenance_link(self, link: dict[str, object], **_kwargs) -> dict[str, object]:
        return {**link, "id": "provenance-1"}

    def create_open_loop(self, loop: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**loop, "id": f"loop-{len(self.open_loops) + 1}", "status": loop.get("status", "open")}
        self.open_loops.append(row)
        return row

    def search_memories(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
        **_filters: object,
    ) -> list[dict[str, object]]:
        del query, domains, sensitivity_allowed
        return self.memories[:limit]

    def search_sources(
        self,
        *,
        query: str,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
        **_filters: object,
    ) -> list[dict[str, object]]:
        del query, domains, sensitivity_allowed
        return self.sources[:limit]

    def list_open_loops(
        self,
        *,
        status: str | None = "open",
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        project_id: str | None = None,
        person_id: str | None = None,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        del domains, sensitivity_allowed
        rows = [
            row
            for row in self.open_loops
            if (status is None or row.get("status") == status)
            and (project_id is None or row.get("project_id") == project_id)
            and (person_id is None or row.get("person_id") == person_id)
        ]
        return rows[:limit]

    def get_open_loop(self, loop_id: str) -> dict[str, object] | None:
        for loop in self.open_loops:
            if loop["id"] == loop_id:
                return loop
        return None

    def update_open_loop(self, *, loop_id: str, patch: dict[str, object], **_kwargs) -> dict[str, object]:
        loop = self.get_open_loop(loop_id)
        if loop is None:
            raise AssertionError(loop_id)
        loop.update(patch)
        return loop

    def update_open_loop_status(
        self,
        *,
        loop_id: str,
        status: str,
        resolution_note: str | None = None,
        **_kwargs,
    ) -> dict[str, object]:
        loop = self.update_open_loop(loop_id=loop_id, patch={"status": status})
        if resolution_note is not None:
            loop["resolution_note"] = resolution_note
        return loop

    def list_provenance_links(self, *, target_type: str, target_id: str) -> list[dict[str, object]]:
        del target_type, target_id
        return []

    def create_edge(self, edge: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**edge, "id": f"edge-{len(self.edges) + 1}"}
        self.edges[str(row["id"])] = row
        return row

    def update_edge_status(self, *, edge_id: str, status: str, **_kwargs) -> dict[str, object]:
        edge = self.edges[edge_id]
        metadata = edge.get("metadata_json")
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.update({"status": status, "candidate": status != "accepted"})
        edge["metadata_json"] = metadata
        if status == "rejected":
            edge["valid_to"] = "now"
        return edge

    def list_edges(self, *, from_id: str | None = None, to_id: str | None = None) -> list[dict[str, object]]:
        return [
            edge
            for edge in self.edges.values()
            if (from_id is None or edge.get("from_id") == from_id)
            and (to_id is None or edge.get("to_id") == to_id)
            and edge.get("valid_to") is None
        ]

    def create_belief(self, belief: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**belief, "id": f"belief-{len(self.beliefs) + 1}"}
        self.beliefs[str(row["id"])] = row
        return row

    def get_belief(self, belief_id: str) -> dict[str, object] | None:
        return self.beliefs.get(belief_id)

    def list_beliefs(
        self,
        *,
        status: str | None = "active",
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        del domains, sensitivity_allowed
        rows = [row for row in self.beliefs.values() if status is None or row.get("status") == status]
        return rows[:limit]

    def update_belief_status(
        self,
        *,
        belief_id: str,
        status: str,
        confidence: float | None = None,
        superseded_by: str | None = None,
        **_kwargs,
    ) -> dict[str, object]:
        belief = self.beliefs[belief_id]
        belief["status"] = status
        if confidence is not None:
            belief["confidence"] = confidence
        if superseded_by is not None:
            belief["superseded_by"] = superseded_by
        self.append_event(
            {
                "event_type": "belief.updated",
                "target_type": "belief",
                "target_id": belief_id,
                "payload_json": {"status": status},
            }
        )
        return belief

    def list_events(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        rows = [
            event
            for event in self.events
            if (target_type is None or event.get("target_type") == target_type)
            and (target_id is None or event.get("target_id") == target_id)
        ]
        rows = list(reversed(rows))
        return rows[:limit] if limit is not None else rows

    def list_project_update_events(
        self,
        *,
        artifact_id: str,
        candidate_memory_id: str,
    ) -> list[dict[str, object]]:
        event_types = {
            "project.update_candidate_created",
            "project.update_candidate_accepted",
            "project.update_candidate_rejected",
        }
        rows: list[dict[str, object]] = []
        for event in self.events:
            if event.get("event_type") not in event_types:
                continue
            payload_value = event.get("payload_json")
            payload = payload_value if isinstance(payload_value, dict) else {}
            if (
                (event.get("target_type") == "artifact" and str(event.get("target_id") or "") == artifact_id)
                or (
                    event.get("target_type") == "memory"
                    and str(event.get("target_id") or "") == candidate_memory_id
                )
                or str(payload.get("artifact_id") or "") == artifact_id
                or str(payload.get("candidate_memory_id") or "") == candidate_memory_id
                or str(payload.get("memory_id") or "") == candidate_memory_id
            ):
                rows.append(event)
        return rows

    def list_agent_events(self, *, agent_id: str | None = None, limit: int = 50) -> list[dict[str, object]]:
        rows = [
            event
            for event in reversed(self.events)
            if event.get("actor_type") == "agent" and (agent_id is None or event.get("actor_id") == agent_id)
        ]
        return rows[:limit]

    def list_memories(self, *, status: str | None = None) -> list[dict[str, object]]:
        return [memory for memory in self.memories if status is None or memory.get("status") == status]

    def create_task(self, task: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**task, "id": f"task-{len(self.tasks) + 1}", "status": "pending"}
        self.tasks.append(row)
        return row

    def claim_next_task(self) -> dict[str, object] | None:
        for task in self.tasks:
            if task["status"] == "pending":
                task["status"] = "running"
                return task
        return None

    def update_task_status(
        self,
        *,
        task_id: str,
        status: str,
        details: dict[str, object] | None = None,
        **_kwargs,
    ) -> dict[str, object]:
        for task in self.tasks:
            if task["id"] == task_id:
                task["status"] = status
                if details:
                    task.update(details)
                return task
        raise AssertionError(task_id)

    def create_artifact(self, artifact: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**artifact, "id": f"artifact-{len(self.artifacts) + 1}"}
        self.artifacts[str(row["id"])] = row
        return row

    def get_artifact(self, artifact_id: str) -> dict[str, object] | None:
        return self.artifacts.get(artifact_id)

    def get_artifact_for_update(self, artifact_id: str) -> dict[str, object] | None:
        """Mirror the production locking read for single-threaded CLI tests."""

        return self.artifacts.get(artifact_id)

    def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 4,
        **_filters: object,
    ) -> list[dict[str, object]]:
        del domains, sensitivity_allowed
        rows = [
            row for row in self.artifacts.values() if artifact_type is None or row.get("artifact_type") == artifact_type
        ]
        return rows[:limit]

    def get_project(self, project_id: str) -> dict[str, object] | None:
        return self.projects.get(project_id)

    def get_project_for_update(self, project_id: str) -> dict[str, object] | None:
        """Mirror the production locking read for single-threaded CLI tests."""

        return self.projects.get(project_id)

    def list_projects(
        self,
        *,
        status: str | None = "active",
        domains: list[str] | None = None,
        sensitivity_allowed: list[str] | None = None,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        del domains, sensitivity_allowed
        rows = [row for row in self.projects.values() if status is None or row.get("status") == status]
        return rows[:limit]

    def update_project(self, *, project_id: str, patch: dict[str, object], **_kwargs) -> dict[str, object]:
        project = self.projects[project_id]
        project.update(patch)
        return project

    def update_artifact_status(
        self,
        *,
        artifact_id: str,
        status: str,
        expected_status: str | None = None,
        metadata_json: dict[str, object] | None = None,
        **_kwargs,
    ) -> dict[str, object] | None:
        artifact = self.artifacts[artifact_id]
        if expected_status is not None and artifact.get("status") != expected_status:
            return None
        artifact["status"] = status
        if metadata_json is not None:
            metadata = artifact.setdefault("metadata_json", {})
            assert isinstance(metadata, dict)
            metadata.update(metadata_json)
        return artifact

    def upsert_scheduler_workflow(self, workflow: dict[str, object], **_kwargs) -> dict[str, object]:
        workflow_type = str(workflow["workflow_type"])
        existing = self.scheduler_workflows.get(workflow_type, {})
        row = {
            **existing,
            **workflow,
            "id": existing.get("id") or f"workflow-{len(self.scheduler_workflows) + 1}",
        }
        self.scheduler_workflows[workflow_type] = row
        return row

    def update_scheduler_workflow(
        self,
        *,
        workflow_type: str,
        patch: dict[str, object],
        **_kwargs,
    ) -> dict[str, object]:
        workflow = self.get_scheduler_workflow(workflow_type)
        if workflow is None:
            workflow = self.upsert_scheduler_workflow(
                {
                    "workflow_type": workflow_type,
                    "enabled": False,
                    "paused": False,
                    "schedule_json": {"kind": "manual"},
                    "timezone": "UTC",
                    "next_run_at": None,
                    "metadata_json": {},
                }
            )
        workflow.update(patch)
        return workflow

    def get_scheduler_workflow(self, workflow_type: str) -> dict[str, object] | None:
        return self.scheduler_workflows.get(workflow_type)

    def list_scheduler_workflows(self) -> list[dict[str, object]]:
        return list(self.scheduler_workflows.values())

    def create_scheduler_run(self, run: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {
            **run,
            "id": f"scheduler-run-{len(self.scheduler_runs) + 1}",
            "started_at": datetime.now(UTC).isoformat(),
        }
        self.scheduler_runs.append(row)
        self.append_event(
            {
                "event_type": "scheduler.run_started",
                "target_type": "scheduler_run",
                "target_id": row["id"],
                "payload_json": {"workflow_type": row["workflow_type"]},
            }
        )
        return row

    def update_scheduler_run(
        self,
        *,
        run_id: str,
        patch: dict[str, object],
        **_kwargs,
    ) -> dict[str, object]:
        for run in self.scheduler_runs:
            if run["id"] == run_id:
                run.update(patch)
                run["finished_at"] = datetime.now(UTC).isoformat()
                self.append_event(
                    {
                        "event_type": "scheduler.run_succeeded"
                        if run.get("status") == "succeeded"
                        else "scheduler.run_failed",
                        "target_type": "scheduler_run",
                        "target_id": run_id,
                        "payload_json": {"workflow_type": run["workflow_type"]},
                    }
                )
                return run
        raise AssertionError(run_id)

    def list_scheduler_runs(self, *, workflow_type: str | None = None, limit: int = 20) -> list[dict[str, object]]:
        rows = [
            run
            for run in reversed(self.scheduler_runs)
            if workflow_type is None or run.get("workflow_type") == workflow_type
        ]
        return rows[:limit]

    def try_scheduler_workflow_lock(self, workflow_type: str) -> bool:
        del workflow_type
        return True


def test_vnext_capture_text_cli_uses_vnext_capture_service(monkeypatch) -> None:
    store = FakeVNextCliStore()

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)

    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    args = cli_module.build_parser().parse_args(
        [
            "vnext",
            "sources",
            "capture-text",
            "Fact: Alice vNext captures sources with provenance.",
            "--domain",
            "project",
            "--sensitivity",
            "private",
        ]
    )

    output = args.handler(ctx, args)

    payload = json.loads(output)
    assert payload["status"] == "imported"
    assert payload["chunk_count"] == 1
    assert payload["candidate_memory_count"] == 1
    assert store.sources[0]["domain"] == "project"
    assert store.sources[0]["metadata_json"]["raw_text"] == "Fact: Alice vNext captures sources with provenance."
    assert store.memories[0]["memory_type"] == "semantic"


def test_agent_keys_cli_create_list_revoke_flow(monkeypatch) -> None:
    store = FakeVNextCliStore()

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    parser = cli_module.build_parser()

    create_args = parser.parse_args(
        [
            "agent",
            "keys",
            "create",
            "--agent-id",
            "hermes",
            "--profile",
            "trusted_local_agent",
            "--label",
            "Hermes local",
        ]
    )
    create_payload = json.loads(create_args.handler(ctx, create_args))

    assert create_payload["status"] == "created"
    raw_key = create_payload["raw_key"]
    assert raw_key.startswith("alice_sk_")
    assert "shown exactly once" in create_payload["warning"]
    assert create_payload["key"]["agent_id"] == "hermes"
    assert create_payload["key"]["permission_profile"] == "trusted_local_agent"
    assert create_payload["key"]["key_prefix"] == raw_key[:12]
    assert "key_hash" not in create_payload["key"]
    assert store.agent_api_keys[0]["key_hash"] != raw_key

    list_args = parser.parse_args(["agent", "keys", "list"])
    list_payload = json.loads(list_args.handler(ctx, list_args))

    assert list_payload["count"] == 1
    listed = list_payload["items"][0]
    assert listed["key_prefix"] == raw_key[:12]
    assert listed["revoked"] is False
    assert "key_hash" not in listed
    assert raw_key not in json.dumps(list_payload)

    revoke_args = parser.parse_args(["agent", "keys", "revoke", raw_key[:12]])
    revoke_payload = json.loads(revoke_args.handler(ctx, revoke_args))

    assert revoke_payload["status"] == "revoked"
    assert revoke_payload["key"]["revoked"] is True
    assert store.agent_api_keys[0]["revoked_at"] is not None

    relist_payload = json.loads(list_args.handler(ctx, list_args))
    assert relist_payload["items"][0]["revoked"] is True


def test_agent_keys_cli_create_supports_project_scope_binding(monkeypatch) -> None:
    store = FakeVNextCliStore()

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    parser = cli_module.build_parser()

    bound_args = parser.parse_args(
        [
            "agent",
            "keys",
            "create",
            "--agent-id",
            "openclaw",
            "--profile",
            "project_scoped_agent",
            "--project-scope",
            "alicebot",
        ]
    )
    bound_payload = json.loads(bound_args.handler(ctx, bound_args))

    assert bound_payload["status"] == "created"
    assert bound_payload["key"]["project_scope"] == "alicebot"
    assert store.agent_api_keys[0]["project_scope"] == "alicebot"

    # Without the flag, keys stay unbound (NULL project_scope).
    unbound_args = parser.parse_args(
        ["agent", "keys", "create", "--agent-id", "hermes", "--profile", "trusted_local_agent"]
    )
    unbound_payload = json.loads(unbound_args.handler(ctx, unbound_args))
    assert unbound_payload["key"]["project_scope"] is None
    assert store.agent_api_keys[1]["project_scope"] is None

    # The binding is visible (prefixes only) in list output.
    list_args = parser.parse_args(["agent", "keys", "list"])
    list_payload = json.loads(list_args.handler(ctx, list_args))
    scopes = {item["agent_id"]: item["project_scope"] for item in list_payload["items"]}
    assert scopes == {"openclaw": "alicebot", "hermes": None}


def test_agent_keys_cli_revoke_rejects_unknown_and_already_revoked_keys(monkeypatch, capsys) -> None:
    store = FakeVNextCliStore()

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)

    exit_code = cli_module.main(["agent", "keys", "revoke", "alice_sk_none"])
    assert exit_code == 1
    _assert_cli_error(
        capsys.readouterr().err,
        code="invalid_request",
        message="The command request is invalid",
    )

    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    parser = cli_module.build_parser()
    create_args = parser.parse_args(["agent", "keys", "create", "--agent-id", "hermes", "--profile", "read_only_agent"])
    create_payload = json.loads(create_args.handler(ctx, create_args))
    prefix = create_payload["key"]["key_prefix"]
    revoke_args = parser.parse_args(["agent", "keys", "revoke", prefix])
    revoke_args.handler(ctx, revoke_args)

    exit_code = cli_module.main(["agent", "keys", "revoke", prefix])
    assert exit_code == 1
    _assert_cli_error(
        capsys.readouterr().err,
        code="invalid_request",
        message="The command request is invalid",
    )


def _stub_eval_report(status: str) -> dict[str, object]:
    return {
        "schema_version": "vnext_eval_report_v1",
        "status": status,
        "suites": [],
        "summary": {"status": status},
    }


def test_eval_run_cli_exits_nonzero_when_report_status_is_fail(monkeypatch, capsys) -> None:
    # Reproduction for audit P1 #8: `alice eval run` must not exit 0 when the
    # emitted report status is "fail".
    monkeypatch.setattr(cli_module, "run_vnext_evals", lambda **kwargs: _stub_eval_report("fail"))

    exit_code = cli_module.main(["eval", "run", "--suite", "all"])

    assert exit_code == 1
    # JSON output contract is preserved: the report still prints to stdout.
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"]["status"] == "fail"


def test_eval_run_cli_release_gate_fts_only_exits_nonzero(monkeypatch, capsys) -> None:
    def _fake_run(**kwargs):
        assert kwargs.get("release_gate") is True
        return _stub_eval_report("pass_fts_only")

    monkeypatch.setattr(cli_module, "run_vnext_evals", _fake_run)

    exit_code = cli_module.main(["eval", "run", "--suite", "all", "--release-gate"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"]["status"] == "pass_fts_only"


def test_eval_run_cli_release_gate_all_skipped_exits_nonzero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli_module,
        "run_vnext_evals",
        lambda **kwargs: _stub_eval_report("skipped"),
    )

    exit_code = cli_module.main(["eval", "run", "--suite", "all", "--release-gate"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"]["status"] == "skipped"


def test_eval_run_cli_release_gate_partial_skip_exits_nonzero(monkeypatch, capsys) -> None:
    report = _stub_eval_report("pass")
    report["summary"] = {
        "status": "pass",
        "suite_count": 6,
        "executed_suite_count": 5,
        "skipped_suite_count": 1,
    }
    monkeypatch.setattr(cli_module, "run_vnext_evals", lambda **kwargs: report)

    exit_code = cli_module.main(["eval", "run", "--suite", "all", "--release-gate"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"]["summary"]["skipped_suite_count"] == 1


def test_eval_run_cli_release_gate_accepts_threshold_pass_with_case_misses(monkeypatch, capsys) -> None:
    report = _stub_eval_report("pass")
    report["summary"] = {
        "status": "pass",
        "suite_count": 6,
        "executed_suite_count": 6,
        "skipped_suite_count": 0,
        "case_count": 78,
        "passed_case_count": 75,
        "failed_case_count": 3,
        "pass_rate": 75 / 78,
    }
    monkeypatch.setattr(cli_module, "run_vnext_evals", lambda **kwargs: report)

    exit_code = cli_module.main(["eval", "run", "--suite", "all", "--release-gate"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"]["summary"]["failed_case_count"] == 3


def test_eval_run_cli_exits_zero_on_pass(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_module, "run_vnext_evals", lambda **kwargs: _stub_eval_report("pass"))

    exit_code = cli_module.main(["eval", "run", "--suite", "all"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"]["status"] == "pass"


def test_vnext_connector_cli_lists_and_ingests_payload_file(monkeypatch, tmp_path: Path) -> None:
    store = FakeVNextCliStore()
    payload_path = tmp_path / "clips.json"
    payload_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "external_id": "clip-1",
                        "cursor": "1",
                        "title": "Connector clip",
                        "url": "https://example.test/clip",
                        "text": "Fact: Connector CLI preserves raw evidence.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    parser = cli_module.build_parser()

    list_args = parser.parse_args(["vnext", "connectors", "list"])
    ingest_args = parser.parse_args(
        [
            "vnext",
            "connectors",
            "ingest",
            "browser_clipper",
            str(payload_path),
            "--domain",
            "learning",
            "--sensitivity",
            "private",
        ]
    )

    list_payload = json.loads(list_args.handler(ctx, list_args))
    ingest_payload = json.loads(ingest_args.handler(ctx, ingest_args))

    assert "browser_clipper" in list_payload["order"]
    assert ingest_payload["status"] == "ok"
    assert ingest_payload["imported_count"] == 1
    assert store.sources[0]["connector_name"] == "browser_clipper"
    assert store.sources[0]["metadata_json"]["raw_payload"]["external_id"] == "clip-1"
    assert store.sources[0]["domain"] == "learning"


def test_context_pack_cli_returns_structured_vnext_pack(monkeypatch) -> None:
    store = FakeVNextCliStore()
    memory_id = uuid4()
    source_id = uuid4()
    captured_at = datetime(2026, 5, 10, 0, 0, tzinfo=UTC)
    store.memories.append(
        {
            "id": memory_id,
            "memory_type": "semantic",
            "canonical_text": "Alice context packs need provenance.",
            "status": "active",
            "confidence": 0.8,
            "domain": "project",
            "sensitivity": "private",
            "first_seen_at": captured_at,
            "last_seen_at": captured_at,
        }
    )
    store.sources.append(
        {
            "id": source_id,
            "source_type": "manual_text",
            "title": "Alice context source",
            "content_hash": "sha256:abc",
            "captured_at": captured_at,
            "domain": "project",
            "sensitivity": "private",
        }
    )

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    args = cli_module.build_parser().parse_args(
        ["context-pack", "Alice context provenance", "--domain", "project", "--max-items", "4"]
    )

    output = args.handler(ctx, args)

    payload = json.loads(output)
    assert payload["query_interpretation"]["query_type"] == "strategic_synthesis"
    assert payload["relevant_memories"][0]["id"] == str(memory_id)
    assert payload["relevant_memories"][0]["first_seen_at"] == "2026-05-10T00:00:00+00:00"
    assert payload["sources"][0]["id"] == str(source_id)
    assert payload["sources"][0]["captured_at"] == "2026-05-10T00:00:00+00:00"
    assert payload["trace"]["selected_count"] == 2


def test_context_pack_cli_forwards_depth_strategy_and_tri_state_flags(monkeypatch) -> None:
    store = FakeVNextCliStore()
    store.memories.append(
        {
            "id": uuid4(),
            "memory_type": "semantic",
            "canonical_text": "Quarterly budget lives in the finance folder.",
            "status": "active",
            "confidence": 0.8,
            "domain": "project",
            "sensitivity": "private",
        }
    )

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    parser = cli_module.build_parser()

    tuned_args = parser.parse_args(
        [
            "context-pack",
            "quarterly budget",
            "--context-depth",
            "minimal",
            "--budget-strategy",
            "facts_first",
            "--no-sources",
        ]
    )
    tuned = json.loads(tuned_args.handler(ctx, tuned_args))
    assert tuned["trace"]["context_depth"] == "minimal"
    assert tuned["trace"]["budget_strategy"] == "facts_first"
    # Explicit --no-sources wins over the tier default, with the honest status.
    assert tuned["trace"]["stages"]["sources"]["status"] == "disabled: include_sources=false"

    default_args = parser.parse_args(["context-pack", "quarterly budget"])
    default = json.loads(default_args.handler(ctx, default_args))
    # Omitted flags fall back to the request dataclass tier defaults.
    assert default["trace"]["context_depth"] == "low"
    assert default["trace"]["budget_strategy"] == "balanced"
    assert "status" not in default["trace"]["stages"]["sources"]


def test_vnext_brain_cli_generates_daily_and_weekly_artifacts(monkeypatch) -> None:
    store = FakeVNextCliStore()
    store.sources.append(
        {
            "id": "source-1",
            "source_type": "manual_text",
            "title": "Alice daily note",
            "content_hash": "sha256:abc",
            "captured_at": "2026-05-10T00:00:00Z",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {"raw_text": "TODO: validate daily brief CLI"},
        }
    )
    store.memories.append(
        {
            "id": "memory-1",
            "memory_type": "project_state",
            "canonical_text": "Alice vNext CLI generates brain artifacts.",
            "status": "active",
            "domain": "project",
            "sensitivity": "private",
        }
    )

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    parser = cli_module.build_parser()

    daily_args = parser.parse_args(
        ["daily-brief", "--generate", "--generated-for", "2026-05-10", "--domain", "project"]
    )
    weekly_args = parser.parse_args(
        ["weekly-synthesis", "--generate", "--generated-for", "2026-05-10", "--domain", "project"]
    )

    daily_payload = json.loads(daily_args.handler(ctx, daily_args))
    weekly_payload = json.loads(weekly_args.handler(ctx, weekly_args))

    assert daily_payload["artifact_type"] == "daily_brief"
    assert daily_payload["metadata_json"]["candidate_open_loop_ids"] == ["loop-1"]
    assert weekly_payload["artifact_type"] == "weekly_synthesis"
    assert weekly_payload["metadata_json"]["candidate_memory_ids"] == ["memory-2"]
    assert store.events[-1]["event_type"] == "artifact.generated"


def test_vnext_agentic_scheduler_smoke_cli_runs_required_gates(monkeypatch) -> None:
    store = FakeVNextCliStore()

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)

    def fake_run_due_workflows_durable(**kwargs):
        return cli_module._scheduler_service(store).run_due_workflows(
            limit=int(kwargs.get("limit", 10)),
            triggered_by=str(kwargs.get("triggered_by", "scheduler")),
            agent_identity=kwargs.get("agent_identity"),
            policy_decision=kwargs.get("policy_decision"),
        )

    monkeypatch.setattr(
        cli_module,
        "run_due_workflows_durable",
        fake_run_due_workflows_durable,
    )
    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    args = cli_module.build_parser().parse_args(["vnext", "smoke", "agentic-scheduler"])

    output = args.handler(ctx, args)

    payload = json.loads(output)
    assert payload["status"] == "passed"
    assert all(payload["gates"].values())
    assert payload["policy_decisions"]["blocked"]["decision"] == "blocked"
    assert store.memories[-1]["status"] == "candidate"
    assert len(store.scheduler_runs) == 3
    assert {run["status"] for run in store.scheduler_runs} == {"succeeded"}
    assert any(event["event_type"] == "agent.policy_blocked" for event in store.events)


def test_vnext_connection_cli_generates_reviews_and_lists_neighborhood(monkeypatch) -> None:
    store = FakeVNextCliStore()
    store.sources.append(
        {
            "id": "source-1",
            "source_type": "manual_text",
            "title": "Queue retrieval pattern note",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {"raw_text": "Queue retrieval provenance trace review."},
        }
    )
    store.memories.append(
        {
            "id": "memory-1",
            "memory_type": "semantic",
            "canonical_text": "Retrieval provenance trace review improves queue artifacts.",
            "status": "active",
            "domain": "project",
            "sensitivity": "private",
        }
    )

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    parser = cli_module.build_parser()

    generate_args = parser.parse_args(["connections", "generate", "--domain", "project", "--max-connections", "1"])
    review_args = parser.parse_args(["vnext", "graph", "review", "edge-1", "--action", "accept"])
    neighborhood_args = parser.parse_args(["vnext", "graph", "neighborhood", "source-1"])

    generate_payload = json.loads(generate_args.handler(ctx, generate_args))
    review_payload = json.loads(review_args.handler(ctx, review_args))
    neighborhood_payload = json.loads(neighborhood_args.handler(ctx, neighborhood_args))

    assert generate_payload["artifact_type"] == "connection_report"
    assert generate_payload["metadata_json"]["candidate_edge_ids"] == ["edge-1"]
    assert review_payload["metadata_json"]["status"] == "accepted"
    assert neighborhood_payload["edge_count"] == 1
    assert neighborhood_payload["from_edges"][0]["id"] == "edge-1"


def test_vnext_contradiction_and_belief_cli(monkeypatch) -> None:
    store = FakeVNextCliStore()
    store.sources.append(
        {
            "id": "source-1",
            "source_type": "manual_text",
            "title": "Artifact policy note",
            "content_hash": "sha256:abc",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {"raw_text": "Alice should not auto-promote generated artifacts into memory."},
        }
    )
    store.beliefs["belief-1"] = {
        "id": "belief-1",
        "memory_id": "memory-belief-1",
        "claim": "Alice should auto-promote generated artifacts into memory.",
        "status": "active",
        "confidence": 0.8,
        "domain": "project",
        "sensitivity": "private",
        "memory_type": "belief",
    }

    store.memories.append({
        "id": "memory-belief-1",
        "canonical_text": "Alice should auto-promote generated artifacts into memory.",
        "memory_type": "belief",
        "status": "active",
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {},
    })

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    parser = cli_module.build_parser()

    generate_args = parser.parse_args(
        ["vnext", "contradictions", "generate", "--domain", "project", "--max-contradictions", "1"]
    )
    review_args = parser.parse_args(
        ["vnext", "beliefs", "review", "belief-1", "--action", "challenge", "--confidence", "0.3"]
    )
    state_args = parser.parse_args(["vnext", "beliefs", "state", "belief-1"])

    generate_payload = json.loads(generate_args.handler(ctx, generate_args))
    review_payload = json.loads(review_args.handler(ctx, review_args))
    state_payload = json.loads(state_args.handler(ctx, state_args))

    assert generate_payload["artifact_type"] == "contradiction_report"
    assert generate_payload["metadata_json"]["candidate_edge_ids"] == ["edge-1"]
    assert review_payload["status"] == "challenged"
    assert review_payload["confidence"] == 0.3
    assert state_payload["current"]["status"] == "challenged"
    assert "challenged" in state_payload["previous_statuses"]


def test_vnext_project_and_open_loop_cli(monkeypatch) -> None:
    store = FakeVNextCliStore()
    store.projects["project-1"] = {
        "id": "project-1",
        "name": "Alice vNext",
        "slug": "alice-vnext",
        "status": "active",
        "current_state": "Sprint 7 complete.",
        "domain": "project",
        "sensitivity": "private",
    }
    store.sources.append(
        {
            "id": "source-1",
            "source_type": "manual_text",
            "title": "Alice project note",
            "content_hash": "sha256:abc",
            "captured_at": "2026-05-10T00:00:00Z",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {
                "project_scope": ["project-1"],
                "raw_text": "Project: Alice vNext needs project automation.\nTODO: validate dashboard Owner: Jordan",
            },
        }
    )

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    parser = cli_module.build_parser()

    update_args = parser.parse_args(
        ["vnext", "projects", "update-candidate", "--project-id", "project-1", "--domain", "project"]
    )
    extract_args = parser.parse_args(
        ["vnext", "open-loops", "extract", "--project-id", "project-1", "--domain", "project"]
    )
    review_update_args = parser.parse_args(
        [
            "vnext",
            "projects",
            "review-update",
            "artifact-1",
            "--action",
            "edit",
            "--edited-current-state",
            "Project automation reviewed.",
        ]
    )
    review_loop_args = parser.parse_args(
        ["vnext", "open-loops", "review", "loop-1", "--action", "snooze", "--due-at", "2026-05-12T09:00:00Z"]
    )
    dashboard_args = parser.parse_args(["vnext", "projects", "dashboard", "project-1"])

    update_payload = json.loads(update_args.handler(ctx, update_args))
    extract_payload = json.loads(extract_args.handler(ctx, extract_args))
    review_update_payload = json.loads(review_update_args.handler(ctx, review_update_args))
    review_loop_payload = json.loads(review_loop_args.handler(ctx, review_loop_args))
    dashboard_payload = json.loads(dashboard_args.handler(ctx, dashboard_args))

    assert update_payload["artifact_type"] == "project_update"
    assert update_payload["metadata_json"]["candidate_memory_id"] == "memory-1"
    assert extract_payload["created_count"] == 1
    assert extract_payload["open_loops"][0]["metadata_json"]["owner"] == "Jordan"
    assert review_update_payload["status"] == "accepted"
    assert store.projects["project-1"]["current_state"] == "Project automation reviewed."
    assert review_loop_payload["due_at"] == "2026-05-12T09:00:00Z"
    assert dashboard_payload["counts"]["open_loops"] == 1

    # The CLI must keep the dashboard's current-input admission checks.
    store.sources[0]["domain"] = "health"
    store.sources[0]["sensitivity"] = "regulated"
    restricted = json.loads(dashboard_args.handler(ctx, dashboard_args))
    assert restricted["counts"]["open_loops"] == 0
    store.sources.clear()
    missing = json.loads(dashboard_args.handler(ctx, dashboard_args))
    assert missing["counts"]["open_loops"] == 0


def test_vnext_queue_cli_add_process_review_and_export(monkeypatch, tmp_path: Path) -> None:
    store = FakeVNextCliStore()

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    parser = cli_module.build_parser()

    add_args = parser.parse_args(
        [
            "vnext",
            "queue",
            "add",
            "--type",
            "draft",
            "--title",
            "Draft launch note",
            "--instructions",
            "Draft it.",
        ]
    )
    added = json.loads(add_args.handler(ctx, add_args))
    process_args = parser.parse_args(["vnext", "queue", "process-next"])
    processed = json.loads(process_args.handler(ctx, process_args))
    review_args = parser.parse_args(["vnext", "artifacts", "review", "artifact-1", "--action", "accept"])
    reviewed = json.loads(review_args.handler(ctx, review_args))
    export_args = parser.parse_args(["vnext", "artifacts", "export", "artifact-1", "--output-dir", str(tmp_path)])
    exported = json.loads(export_args.handler(ctx, export_args))

    assert added["id"] == "task-1"
    assert processed["status"] == "completed"
    assert processed["artifact_id"] == "artifact-1"
    assert reviewed["status"] == "accepted"
    assert Path(exported["output_path"]).exists()


def test_vnext_eval_cli_seed_run_and_report(tmp_path: Path, monkeypatch) -> None:
    # Without a live eval store the retrieval-quality suite must report
    # skipped -- never a fabricated pass.
    monkeypatch.delenv("ALICEBOT_EVAL_DATABASE_URL", raising=False)
    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    parser = cli_module.build_parser()
    corpus_path = tmp_path / "vnext_corpus.json"
    report_path = tmp_path / "vnext_report.json"

    seed_args = parser.parse_args(["eval", "seed", "--output-path", str(corpus_path)])
    seed_payload = json.loads(seed_args.handler(ctx, seed_args))
    run_args = parser.parse_args(["eval", "run", "--suite", "all", "--corpus-path", str(corpus_path)])
    run_payload = json.loads(run_args.handler(ctx, run_args))
    report_args = parser.parse_args(
        [
            "eval",
            "report",
            "--suite",
            "retrieval_quality",
            "--corpus-path",
            str(corpus_path),
            "--report-path",
            str(report_path),
        ]
    )
    report_payload = json.loads(report_args.handler(ctx, report_args))

    assert Path(seed_payload["written_corpus_path"]) == corpus_path.resolve()
    assert run_payload["report"]["status"] == "skipped"
    assert run_payload["report"]["summary"]["executed_suite_count"] == 0
    from alicebot_api.vnext_evals import VNEXT_EVAL_SUITE_ORDER

    assert [entry["suite_key"] for entry in run_payload["report"]["skipped_suites"]] == list(VNEXT_EVAL_SUITE_ORDER)
    assert Path(report_payload["written_report_path"]) == report_path.resolve()
    assert report_payload["report"]["suite"] == "retrieval_quality"
    assert json.loads(report_path.read_text(encoding="utf-8")) == report_payload["report"]


def test_resolve_user_id_prefers_flag_then_settings_then_env_then_default(monkeypatch) -> None:
    flag_user_id = UUID("11111111-1111-4111-8111-111111111111")
    configured_user_id = UUID("22222222-2222-4222-8222-222222222222")
    env_user_id = UUID("33333333-3333-4333-8333-333333333333")

    settings_without_auth = Settings(auth_user_id="")
    settings_with_auth = Settings(auth_user_id=str(configured_user_id))

    monkeypatch.setenv("ALICEBOT_AUTH_USER_ID", str(env_user_id))
    assert cli_module._resolve_user_id(settings_without_auth, str(flag_user_id)) == flag_user_id
    assert cli_module._resolve_user_id(settings_with_auth, None) == configured_user_id
    assert cli_module._resolve_user_id(settings_without_auth, None) == env_user_id

    monkeypatch.delenv("ALICEBOT_AUTH_USER_ID")
    assert cli_module._resolve_user_id(settings_without_auth, None) == UUID(cli_module.DEFAULT_CLI_USER_ID)


def test_build_context_applies_production_flags_before_scoped_validation(monkeypatch) -> None:
    user_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: (_ for _ in ()).throw(AssertionError("cached full settings must not load")),
    )
    args = cli_module.build_parser().parse_args(
        [
            "--database-url",
            "postgresql://runtime:secret@db/alice",
            "--user-id",
            user_id,
            "status",
        ]
    )

    context = cli_module._build_context(args)

    assert context.database_url == "postgresql://runtime:secret@db/alice"
    assert context.user_id == UUID(user_id)
    assert context.settings.app_env == "production"


def test_build_context_accepts_environment_only_production_runtime(monkeypatch) -> None:
    user_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://runtime:secret@db/alice")
    monkeypatch.setenv("ALICEBOT_AUTH_USER_ID", user_id)
    for key in (
        "DATABASE_ADMIN_URL",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "TELEGRAM_WEBHOOK_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: (_ for _ in ()).throw(AssertionError("hosted settings must not load")),
    )

    context = cli_module._build_context(cli_module.build_parser().parse_args(["status"]))

    assert context.database_url == "postgresql://runtime:secret@db/alice"
    assert context.user_id == UUID(user_id)


def test_connector_config_enablement_is_tri_state() -> None:
    parser = cli_module.build_parser()

    preserved = parser.parse_args(["vnext", "connectors", "configure", "telegram"])
    disabled = parser.parse_args(["vnext", "connectors", "configure", "telegram", "--no-enabled"])

    assert preserved.enabled is None
    assert disabled.enabled is False


def test_checked_batch_output_keeps_json_and_signals_failure() -> None:
    with pytest.raises(cli_module.PartialCommandFailure) as exc_info:
        cli_module._checked_batch_output({"status": "partial", "imported_count": 1, "failed_count": 1})

    assert json.loads(exc_info.value.output) == {
        "status": "partial",
        "imported_count": 1,
        "failed_count": 1,
    }


def test_failed_queue_task_signals_cli_failure(monkeypatch) -> None:
    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield object()

    class Result:
        def to_record(self):
            return {"status": "failed", "failed_count": 1, "task": {"status": "failed"}}

    class FakeQueueService:
        def __init__(self, _store):
            pass

        def process_next_task(self):
            return Result()

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    monkeypatch.setattr(cli_module, "VNextQueueService", FakeQueueService)
    context = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    args = cli_module.build_parser().parse_args(["vnext", "queue", "process-next"])

    with pytest.raises(cli_module.PartialCommandFailure):
        args.handler(context, args)


def test_failed_due_scan_signals_cli_failure(monkeypatch) -> None:
    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield object()

    class Decision:
        decision = "allowed"
        effective_domains = ()
        effective_project_scope = ()
        effective_sensitivity_allowed = ("private",)

        def to_record(self):
            return {"decision": "allowed"}

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    monkeypatch.setattr(
        cli_module,
        "_vnext_policy_checked_for_args",
        lambda *_args, **_kwargs: (None, "scheduler", None, Decision()),
    )
    monkeypatch.setattr(
        cli_module,
        "run_due_workflows_durable",
        lambda **_kwargs: {"due_count": 1, "failed_count": 1, "runs": []},
    )
    context = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    args = cli_module.build_parser().parse_args(["vnext", "scheduler", "run-due"])

    with pytest.raises(cli_module.PartialCommandFailure):
        args.handler(context, args)


def test_foreground_once_scheduler_failure_signals_cli_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "run_foreground_daemon",
        lambda _config: {
            "running": False,
            "exit_code": 1,
            "last_error": "1 scheduled workflow(s) failed",
        },
    )
    context = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    args = cli_module.build_parser().parse_args(
        [
            "vnext",
            "scheduler",
            "daemon",
            "start",
            "--foreground",
            "--once",
        ]
    )

    with pytest.raises(cli_module.PartialCommandFailure):
        args.handler(context, args)


def test_deferred_embedding_provider_call_happens_between_transactions(monkeypatch) -> None:
    transaction_open = False
    calls: list[str] = []

    class Result:
        deferred_embedding_inputs = (object(),)

    @contextmanager
    def fake_vnext_store_context(_ctx):
        nonlocal transaction_open
        transaction_open = True
        calls.append("transaction_open")
        try:
            yield object()
        finally:
            transaction_open = False
            calls.append("transaction_closed")

    def fake_best_effort(_inputs, *, store_context, **_kwargs):
        assert transaction_open is False
        calls.append("provider")
        with store_context():
            assert transaction_open is True
            calls.append("persist")
        return 1

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    monkeypatch.setattr(
        cli_module,
        "persist_deferred_memory_embeddings_best_effort",
        fake_best_effort,
    )
    context = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )

    cli_module._persist_deferred_capture_embeddings(context, Result())

    assert calls == ["provider", "transaction_open", "persist", "transaction_closed"]


def test_local_folder_scan_happens_before_cli_transaction(monkeypatch) -> None:
    from alicebot_api.vnext_connectors import LocalFolderScan

    transaction_depth = 0
    calls: list[str] = []
    # A real scan: the command prints its refused_count and truncated after the sync.
    scan_result = LocalFolderScan(
        items=(), path_count=1, ignored_count=0, recursive=True, extensions=(".md",), refused_count=2, truncated=True
    )

    @contextmanager
    def fake_vnext_store_context(_ctx):
        nonlocal transaction_depth
        transaction_depth += 1
        calls.append("transaction_open")
        try:
            yield object()
        finally:
            transaction_depth -= 1
            calls.append("transaction_closed")

    class Result:
        deferred_embedding_inputs = ()

        def to_record(self):
            return {"status": "ok", "failed_count": 0}

    class FakeConnectorService:
        def __init__(self, _store, *, defer_embeddings=False):
            assert transaction_depth == 1

        def sync_local_folder_scan(self, scan, **_kwargs):
            assert transaction_depth == 1
            assert scan is scan_result
            calls.append("persist")
            return Result()

    def fake_scan(paths, **_kwargs):
        assert transaction_depth == 0
        assert paths == ["/tmp/notes"]
        calls.append("scan")
        return scan_result

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    monkeypatch.setattr(cli_module, "VNextConnectorService", FakeConnectorService)
    monkeypatch.setattr(cli_module, "scan_local_folder", fake_scan)
    context = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    args = cli_module.build_parser().parse_args(["vnext", "connectors", "local-folder", "sync", "--path", "/tmp/notes"])

    payload = json.loads(args.handler(context, args))

    assert payload["status"] == "ok"
    assert payload["refused_count"] == 2
    assert payload["truncated"] is True
    assert calls == ["scan", "transaction_open", "persist", "transaction_closed"]


def test_scheduler_cli_run_now_starts_after_policy_transaction(monkeypatch) -> None:
    transaction_open = False
    calls: list[str] = []

    @contextmanager
    def fake_vnext_store_context(_ctx):
        nonlocal transaction_open
        transaction_open = True
        calls.append("policy_open")
        try:
            yield object()
        finally:
            transaction_open = False
            calls.append("policy_closed")

    class Decision:
        decision = "allowed"
        effective_domains = ("project",)
        effective_project_scope = ("alice",)
        effective_sensitivity_allowed = ("private",)

        def to_record(self):
            return {"decision": "allowed"}

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    monkeypatch.setattr(
        cli_module,
        "_vnext_policy_checked_for_args",
        lambda *_args, **_kwargs: (None, "user", None, Decision()),
    )

    def fake_run_now(**kwargs):
        assert transaction_open is False
        calls.append("durable_execute")
        assert kwargs["request"].projects == ("alice",)
        return {"run": {"status": "succeeded"}, "artifact": {"id": "artifact-1"}}

    monkeypatch.setattr(cli_module, "run_now_durable", fake_run_now)
    context = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    args = cli_module.build_parser().parse_args(["vnext", "scheduler", "run-now", "daily_brief"])

    payload = json.loads(args.handler(context, args))

    assert payload["run"]["status"] == "succeeded"
    assert calls == ["policy_open", "policy_closed", "durable_execute"]


def test_cli_policy_telemetry_uses_bounded_agent_scoped_readers(monkeypatch) -> None:
    calls: list[tuple[str, str | None, int]] = []

    class Store:
        def list_agent_events(self, *, agent_id, limit):
            calls.append(("events", agent_id, limit))
            return []

        def list_agent_policy_artifacts(self, *, agent_id, limit):
            calls.append(("artifacts", agent_id, limit))
            return []

        def list_agent_policy_memories(self, *, agent_id, limit):
            calls.append(("memories", agent_id, limit))
            return []

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield Store()

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    context = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    args = cli_module.build_parser().parse_args(
        ["vnext", "agents", "policy-telemetry", "--agent-id", "hermes", "--limit", "999"]
    )

    payload = json.loads(args.handler(context, args))

    assert "summary" in payload
    assert calls == [
        ("events", "hermes", 200),
        ("artifacts", "hermes", 200),
        ("memories", "hermes", 200),
    ]


def test_main_normalizes_expected_runtime_errors_without_traceback(monkeypatch, capsys) -> None:
    class Parser:
        def parse_args(self, _argv):
            def fail(_ctx, _args):
                raise RuntimeError("expected command failure")

            return type("Args", (), {"handler": staticmethod(fail)})()

    monkeypatch.setattr(cli_module, "build_parser", lambda: Parser())
    monkeypatch.setattr(cli_module, "_validate_arguments", lambda _args: None)
    monkeypatch.setattr(cli_module, "_build_context", lambda _args: object())

    exit_code = cli_module.main(["anything"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    _assert_cli_error(
        captured.err,
        code="command_failed",
        message="The command could not be completed",
    )


@pytest.mark.parametrize(
    "handler_name",
    ["_run_vnext_smoke_live_capture_connectors", "_run_vnext_smoke_connector_hardening"],
)
def test_connector_smoke_folder_scan_starts_outside_transaction(monkeypatch, handler_name) -> None:
    transaction_depth = 0
    calls: list[str] = []

    class ScanObserved(RuntimeError):
        pass

    @contextmanager
    def fake_vnext_store_context(_ctx):
        nonlocal transaction_depth
        transaction_depth += 1
        calls.append("transaction_open")
        try:
            yield object()
        finally:
            transaction_depth -= 1
            calls.append("transaction_closed")

    def stop_after_scan(_paths, **_kwargs):
        assert transaction_depth == 0
        calls.append("scan")
        raise ScanObserved

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    monkeypatch.setattr(cli_module, "scan_local_folder", stop_after_scan)
    context = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )

    with pytest.raises(ScanObserved):
        getattr(cli_module, handler_name)(context, object())

    assert calls == ["scan"]


def test_memory_commit_defers_embedding_until_after_primary_transaction(monkeypatch) -> None:
    transaction_open = False
    calls: list[str] = []
    deferred_input = object()

    @contextmanager
    def fake_vnext_store_context(_ctx):
        nonlocal transaction_open
        transaction_open = True
        calls.append("primary_open")
        try:
            yield object()
        finally:
            transaction_open = False
            calls.append("primary_closed")

    class FakeMemoryCommitService:
        def __init__(self, _store, *, defer_embeddings: bool = False) -> None:
            assert transaction_open is True
            assert defer_embeddings is True
            self.deferred_embedding_inputs = (deferred_input,)

        def commit(self, *, identity, request):
            assert transaction_open is True
            assert identity is None
            assert request.canonical_text == "Embedding work happens after commit."
            calls.append("commit")
            return {"status": "committed"}

    def fake_persist(_ctx, inputs, **_kwargs) -> None:
        assert transaction_open is False
        assert inputs == (deferred_input,)
        calls.append("embedding")

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    monkeypatch.setattr(cli_module, "VNextMemoryCommitService", FakeMemoryCommitService)
    monkeypatch.setattr(cli_module, "_persist_deferred_embedding_inputs", fake_persist)
    context = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    args = cli_module.build_parser().parse_args(
        [
            "vnext",
            "memories",
            "commit",
            "--title",
            "Transaction boundary",
            "--text",
            "Embedding work happens after commit.",
        ]
    )

    payload = json.loads(cli_module._run_vnext_memory_commit(context, args))

    assert payload == {"status": "committed"}
    assert calls == ["primary_open", "commit", "primary_closed", "embedding"]


def test_memory_consolidation_defers_embedding_until_primary_transaction_closes(monkeypatch) -> None:
    transaction_depth = 0
    calls: list[str] = []
    deferred_input = object()

    @contextmanager
    def fake_vnext_store_context(_ctx):
        nonlocal transaction_depth
        transaction_depth += 1
        try:
            yield object()
        finally:
            transaction_depth -= 1

    class FakeMemoryService:
        def __init__(self, _store, *, defer_embeddings: bool = False) -> None:
            assert transaction_depth == 1
            assert defer_embeddings is True
            self.deferred_embedding_inputs = (deferred_input,)

        def accept_consolidation_candidate(self, memory_id: str, **_kwargs):
            assert transaction_depth == 1
            calls.append("accept")
            return {"memory": {"id": memory_id, "status": "active"}}

    def fake_persist(_ctx, deferred_inputs, **_kwargs) -> None:
        assert transaction_depth == 0
        assert deferred_inputs == (deferred_input,)
        calls.append("embedding")

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    monkeypatch.setattr(cli_module, "VNextMemoryCommitService", FakeMemoryService)
    monkeypatch.setattr(cli_module, "_persist_deferred_embedding_inputs", fake_persist)
    context = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    args = cli_module.build_parser().parse_args(
        ["vnext", "memories", "accept-consolidation", "memory-1", "--reason", "Merge duplicates."]
    )

    payload = json.loads(cli_module._run_vnext_memory_accept_consolidation(context, args))

    assert payload["memory"]["status"] == "active"
    assert calls == ["accept", "embedding"]


def test_cli_memory_redact_is_positive_and_strictly_idempotent(monkeypatch) -> None:
    store = FakeVNextCliStore()
    memory = store.create_memory(
        {
            "memory_key": "private.cli.redaction",
            "value": {"text": "CLI secret"},
            "status": "active",
            "source_event_ids": [],
            "memory_type": "fact",
            "confidence": 0.9,
            "title": "CLI secret",
            "canonical_text": "CLI secret",
            "summary": "CLI secret",
            "trust_reason": "operator supplied",
            "domain": "personal",
            "sensitivity": "private",
            "metadata_json": {},
            "commit_digest": "digest",
            "confirmation_id": None,
            "deleted_at": None,
        }
    )

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    context = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    args = cli_module.build_parser().parse_args(
        ["vnext", "memories", "redact", str(memory["id"]), "--reason", "Operator erasure"]
    )

    first = json.loads(cli_module._run_vnext_memory_redact(context, args))
    assert first["status"] == "redacted"
    assert first["forgotten_first"] is True
    assert first["idempotent_replay"] is False
    frozen = deepcopy((store.memories, store.artifacts, store.revisions, store.events))

    second = json.loads(cli_module._run_vnext_memory_redact(context, args))
    assert second["status"] == "redacted"
    assert second["forgotten_first"] is False
    assert second["idempotent_replay"] is True
    assert second["redacted_revisions"] == 0
    assert second["redacted_events"] == 0
    assert (store.memories, store.artifacts, store.revisions, store.events) == frozen


def test_project_review_defers_embedding_until_primary_transaction_closes(monkeypatch) -> None:
    transaction_depth = 0
    calls: list[str] = []
    deferred_input = object()

    @contextmanager
    def fake_vnext_store_context(_ctx):
        nonlocal transaction_depth
        transaction_depth += 1
        try:
            yield object()
        finally:
            transaction_depth -= 1

    class FakeProjectService:
        def __init__(self, _store, *, defer_embeddings: bool = False) -> None:
            assert transaction_depth == 1
            assert defer_embeddings is True
            self.deferred_embedding_inputs = (deferred_input,)

        def review_project_update(self, **kwargs):
            assert transaction_depth == 1
            assert kwargs["actor_type"] == "user"
            assert kwargs["actor_id"] == str(context.user_id)
            calls.append("review")
            return {"id": "artifact-1", "status": "accepted"}

    def fake_persist(_ctx, deferred_inputs, **kwargs) -> None:
        assert transaction_depth == 0
        assert deferred_inputs == (deferred_input,)
        assert kwargs["actor_type"] == "user"
        assert kwargs["actor_id"] == str(context.user_id)
        calls.append("embedding")

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    monkeypatch.setattr(cli_module, "VNextProjectService", FakeProjectService)
    monkeypatch.setattr(cli_module, "_persist_deferred_embedding_inputs", fake_persist)
    context = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    args = cli_module.build_parser().parse_args(
        ["vnext", "projects", "review-update", "artifact-1", "--action", "accept"]
    )

    payload = json.loads(cli_module._run_vnext_project_update_review(context, args))

    assert payload["status"] == "accepted"
    assert calls == ["review", "embedding"]


@pytest.mark.parametrize("action", ["accept", "reject", "promote"])
def test_generic_cli_artifact_review_uses_central_dispatch_with_reviewer_attribution(monkeypatch, action: str) -> None:
    transaction_depth = 0
    calls: list[str] = []
    deferred_input = object()

    @contextmanager
    def fake_vnext_store_context(_ctx):
        nonlocal transaction_depth
        transaction_depth += 1
        try:
            yield object()
        finally:
            transaction_depth -= 1

    class DispatchResult:
        artifact = {"id": "artifact-1", "status": action}
        deferred_embedding_inputs = (deferred_input,)

    def fake_dispatch(_store, **kwargs):
        assert transaction_depth == 1
        assert kwargs["artifact_id"] == "artifact-1"
        assert kwargs["action"] == action
        assert kwargs["actor_type"] == "user"
        assert kwargs["actor_id"] == str(context.user_id)
        calls.append("dispatch")
        return DispatchResult()

    def fake_persist(_ctx, deferred_inputs, **kwargs) -> None:
        assert transaction_depth == 0
        assert deferred_inputs == (deferred_input,)
        assert kwargs["actor_type"] == "user"
        assert kwargs["actor_id"] == str(context.user_id)
        calls.append("embedding")

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    monkeypatch.setattr(cli_module, "dispatch_vnext_artifact_review", fake_dispatch)
    monkeypatch.setattr(cli_module, "_persist_deferred_embedding_inputs", fake_persist)
    context = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    args = cli_module.build_parser().parse_args(["vnext", "artifacts", "review", "artifact-1", "--action", action])

    payload = json.loads(cli_module._run_vnext_artifact_review(context, args))

    assert payload["status"] == action
    assert calls == ["dispatch", "embedding"]


def _cli_project_update_review_fixture() -> tuple[FakeVNextCliStore, dict[str, object]]:
    store = FakeVNextCliStore()
    store.projects["project-1"] = {
        "id": "project-1",
        "name": "Alice vNext",
        "slug": "alice-vnext",
        "status": "active",
        "current_state": "Sprint 7 complete.",
        "domain": "project",
        "sensitivity": "private",
    }
    store.sources.append(
        {
            "id": "source-1",
            "source_type": "manual_text",
            "title": "Alice project note",
            "content_hash": "sha256:terminal-consistency-cli",
            "captured_at": "2026-05-10T00:00:00Z",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {
                "project_scope": ["project-1"],
                "raw_text": "Alice vNext is ready for terminal consistency review.",
            },
        }
    )
    artifact = cli_module.VNextProjectService(store).generate_project_update_candidate(
        cli_module.ProjectAutomationRequest(project_id="project-1", domains=("project",))
    )
    return store, artifact


def _cli_project_update_review_argv(*, adapter: str, artifact_id: str, action: str) -> list[str]:
    if adapter == "generic":
        return ["vnext", "artifacts", "review", artifact_id, "--action", action]
    return ["vnext", "projects", "review-update", artifact_id, "--action", action]


def _install_cli_project_update_store(monkeypatch, store: FakeVNextCliStore) -> None:
    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    monkeypatch.setattr(
        cli_module,
        "_build_context",
        lambda _args: cli_module.CLIContext(
            settings=Settings(database_url="postgresql://db"),
            database_url="postgresql://db",
            user_id=UUID("11111111-1111-4111-8111-111111111111"),
        ),
    )
    monkeypatch.setattr(cli_module, "_persist_deferred_embedding_inputs", lambda *_args, **_kwargs: None)


@pytest.mark.parametrize("marker", ["workflow", "memory_key"])
@pytest.mark.parametrize("operation", ["correct", "forget", "undo", "redact"])
def test_cli_generic_memory_mutations_cannot_strand_pending_project_update_candidate(
    monkeypatch,
    capsys,
    marker: str,
    operation: str,
) -> None:
    store, artifact = _cli_project_update_review_fixture()
    metadata = artifact["metadata_json"]
    assert isinstance(metadata, dict)
    candidate_memory_id = str(metadata["candidate_memory_id"])
    candidate = store.get_memory(candidate_memory_id)
    assert candidate is not None
    candidate_metadata = candidate["metadata_json"]
    assert isinstance(candidate_metadata, dict)
    if marker == "workflow":
        candidate["memory_key"] = "ordinary.pending.candidate"
    else:
        candidate_metadata.pop("workflow", None)
        candidate["memory_key"] = "  project_update.alice.digest  "
    _install_cli_project_update_store(monkeypatch, store)
    state_before = deepcopy((store.projects, store.memories, store.artifacts, store.revisions))
    event_types_before = [event.get("event_type") for event in store.events]

    argv = ["vnext", "memories", operation]
    if operation == "undo":
        argv.extend(["--memory-id", candidate_memory_id, "--reason", "Generic undo must not apply."])
    else:
        argv.append(candidate_memory_id)
        if operation == "correct":
            argv.extend(["--text", "Generic correction must not apply."])
        argv.extend(["--reason", f"Generic {operation} must not apply."])

    exit_code = cli_module.main(argv)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    _assert_cli_error(
        captured.err,
        code="invalid_request",
        message="The command request is invalid",
    )
    assert (store.projects, store.memories, store.artifacts, store.revisions) == state_before
    assert [event.get("event_type") for event in store.events] == event_types_before


def _apply_supported_cli_memory_lifecycle(
    store: FakeVNextCliStore,
    *,
    artifact: dict[str, object],
    operation: str,
) -> None:
    metadata = artifact["metadata_json"]
    assert isinstance(metadata, dict)
    memory_id = str(metadata["candidate_memory_id"])
    service = cli_module.VNextMemoryCommitService(store)
    if operation == "correct":
        service.correct(
            identity=None,
            memory_id=memory_id,
            canonical_text="Later corrected CLI project-update memory.",
            reason="Exercise a supported post-review correction.",
        )
    elif operation == "undo":
        service.undo(
            identity=None,
            memory_id=memory_id,
            reason="Exercise a supported post-review undo.",
        )
    else:
        service.forget(
            identity=None,
            memory_id=memory_id,
            reason="Exercise a supported post-review forget.",
        )


def _accept_later_cli_project_update(store: FakeVNextCliStore, *, first_artifact_id: str) -> None:
    service = cli_module.VNextProjectService(store)
    later = service.generate_project_update_candidate(
        cli_module.ProjectAutomationRequest(project_id="project-1", domains=("project",))
    )
    assert later["id"] != first_artifact_id
    service.review_project_update(
        artifact_id=str(later["id"]),
        action="edit",
        edited_current_state="Later accepted CLI project state B.",
    )


def _append_conflicting_cli_project_update_decision(
    store: FakeVNextCliStore,
    *,
    artifact: dict[str, object],
    conflict: str,
) -> None:
    metadata = artifact["metadata_json"]
    assert isinstance(metadata, dict)
    artifact_id = str(artifact["id"])
    candidate_memory_id = str(metadata["candidate_memory_id"])
    project_id = str(metadata["project_id"])
    review_event = next(
        event for event in store.events if event.get("event_type") == f"project.update_candidate_{artifact['status']}"
    )
    event_type: str
    target_type: str
    target_id: str
    payload: dict[str, object]
    if conflict == "accepted_plus_rejected":
        event_type = "project.update_candidate_rejected"
        target_type = "artifact"
        target_id = artifact_id
        payload = {"project_id": project_id, "source_ids": list(metadata["source_ids"])}
    elif conflict == "candidate_linked_accepted_wrong_action":
        event_type = "project.update_candidate_accepted"
        target_type = "project"
        target_id = project_id
        payload = {"candidate_memory_id": candidate_memory_id, "action": "reject"}
    elif conflict == "rejected_plus_conflicting_rejection":
        event_type = "project.update_candidate_rejected"
        target_type = "artifact"
        target_id = artifact_id
        payload = {"project_id": project_id, "source_ids": ["conflicting-source"]}
    else:  # pragma: no cover - exhaustive parameter list
        raise AssertionError(conflict)
    store.append_event(
        build_event_log_record(
            event_type=event_type,
            actor_type=str(review_event["actor_type"]),
            actor_id=str(review_event["actor_id"]) if review_event.get("actor_id") is not None else None,
            target_type=target_type,
            target_id=target_id,
            trace_id=str(review_event["trace_id"]) if review_event.get("trace_id") is not None else None,
            run_id=str(review_event["run_id"]) if review_event.get("run_id") is not None else None,
            payload=payload,
        )
    )


def _redact_and_clone_cli_project_update_terminal(
    store: FakeVNextCliStore,
    *,
    terminal: dict[str, object],
) -> str:
    metadata = terminal["metadata_json"]
    assert isinstance(metadata, dict)
    candidate_memory_id = str(metadata["candidate_memory_id"])
    for revision in store.revisions:
        if (
            str(revision.get("memory_id") or "") == candidate_memory_id
            and revision.get("action") == "project_update_review"
        ):
            revision.update(
                {
                    "metadata_json": {"redacted": True},
                    "text_before": "[REDACTED]",
                    "text_after": "[REDACTED]",
                    "reason": "[REDACTED]",
                }
            )
    for event in store.events:
        payload = event.get("payload_json")
        if not isinstance(payload, dict):
            continue
        if (
            str(payload.get("candidate_memory_id") or "") != candidate_memory_id
            and str(payload.get("memory_id") or "") != candidate_memory_id
            and not (event.get("target_type") == "memory" and str(event.get("target_id") or "") == candidate_memory_id)
        ):
            continue
        event["payload_json"] = {
            "redacted": True,
            "memory_id": candidate_memory_id,
            "event_type": event["event_type"],
        }
        event["integrity_hash"] = None
    clone_id = "artifact-terminal-clone"
    clone = deepcopy(terminal)
    clone["id"] = clone_id
    store.artifacts[clone_id] = clone
    return clone_id


@pytest.mark.parametrize("adapter", ["generic", "dedicated"])
@pytest.mark.parametrize(
    ("forced_status", "retry_action"),
    [("accepted", "accept"), ("rejected", "reject")],
)
def test_cli_project_update_review_rejects_forced_terminal_status_without_mutation(
    monkeypatch,
    capsys,
    adapter: str,
    forced_status: str,
    retry_action: str,
) -> None:
    store, artifact = _cli_project_update_review_fixture()
    artifact["status"] = forced_status
    artifact_id = str(artifact["id"])
    _install_cli_project_update_store(monkeypatch, store)
    state_before = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    exit_code = cli_module.main(
        _cli_project_update_review_argv(adapter=adapter, artifact_id=artifact_id, action=retry_action)
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    _assert_cli_error(
        captured.err,
        code="invalid_request",
        message="The command request is invalid",
    )
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before


@pytest.mark.parametrize("adapter", ["generic", "dedicated"])
def test_cli_project_update_review_rejects_terminal_clone_after_true_redaction_without_mutation(
    monkeypatch,
    capsys,
    adapter: str,
) -> None:
    store, artifact = _cli_project_update_review_fixture()
    original_id = str(artifact["id"])
    _install_cli_project_update_store(monkeypatch, store)
    original_argv = _cli_project_update_review_argv(adapter=adapter, artifact_id=original_id, action="accept")
    assert cli_module.main(original_argv) == 0
    capsys.readouterr()
    clone_id = _redact_and_clone_cli_project_update_terminal(store, terminal=artifact)
    state_before_retry = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    exit_code = cli_module.main(_cli_project_update_review_argv(adapter=adapter, artifact_id=clone_id, action="accept"))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    _assert_cli_error(
        captured.err,
        code="invalid_request",
        message="The command request is invalid",
    )
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before_retry


@pytest.mark.parametrize("adapter", ["generic", "dedicated"])
@pytest.mark.parametrize("action", ["accept", "reject"])
def test_cli_project_update_review_keeps_consistent_terminal_outcomes_idempotent(
    monkeypatch,
    capsys,
    adapter: str,
    action: str,
) -> None:
    store, artifact = _cli_project_update_review_fixture()
    artifact_id = str(artifact["id"])
    _install_cli_project_update_store(monkeypatch, store)
    argv = _cli_project_update_review_argv(adapter=adapter, artifact_id=artifact_id, action=action)
    first_exit = cli_module.main(argv)
    first_output = capsys.readouterr()
    state_before_retry = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))
    second_exit = cli_module.main(argv)

    second_output = capsys.readouterr()
    assert first_exit == second_exit == 0
    assert json.loads(first_output.out) == json.loads(second_output.out)
    assert first_output.err == second_output.err == ""
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before_retry


@pytest.mark.parametrize("adapter", ["generic", "dedicated"])
@pytest.mark.parametrize(
    ("action", "conflict"),
    [
        ("accept", "accepted_plus_rejected"),
        ("accept", "candidate_linked_accepted_wrong_action"),
        ("reject", "rejected_plus_conflicting_rejection"),
    ],
)
def test_cli_project_update_terminal_replay_rejects_every_coupled_competing_decision(
    monkeypatch,
    capsys,
    adapter: str,
    action: str,
    conflict: str,
) -> None:
    store, artifact = _cli_project_update_review_fixture()
    artifact_id = str(artifact["id"])
    _install_cli_project_update_store(monkeypatch, store)
    argv = _cli_project_update_review_argv(adapter=adapter, artifact_id=artifact_id, action=action)
    assert cli_module.main(argv) == 0
    capsys.readouterr()
    _append_conflicting_cli_project_update_decision(store, artifact=artifact, conflict=conflict)
    state_before_retry = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    exit_code = cli_module.main(argv)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    _assert_cli_error(
        captured.err,
        code="invalid_request",
        message="The command request is invalid",
    )
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before_retry


@pytest.mark.parametrize("adapter", ["generic", "dedicated"])
@pytest.mark.parametrize("operation", ["correct", "undo", "forget"])
def test_cli_accepted_project_update_replay_survives_supported_memory_lifecycle(
    monkeypatch,
    capsys,
    adapter: str,
    operation: str,
) -> None:
    store, artifact = _cli_project_update_review_fixture()
    artifact_id = str(artifact["id"])
    _install_cli_project_update_store(monkeypatch, store)
    argv = _cli_project_update_review_argv(adapter=adapter, artifact_id=artifact_id, action="accept")
    first_exit = cli_module.main(argv)
    first_output = capsys.readouterr()
    _apply_supported_cli_memory_lifecycle(store, artifact=artifact, operation=operation)
    state_before_retry = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    second_exit = cli_module.main(argv)

    second_output = capsys.readouterr()
    assert first_exit == second_exit == 0
    assert json.loads(first_output.out) == json.loads(second_output.out)
    assert first_output.err == second_output.err == ""
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before_retry


@pytest.mark.parametrize("adapter", ["generic", "dedicated"])
def test_cli_accepted_project_update_replay_preserves_a_genuine_later_project_update(
    monkeypatch,
    capsys,
    adapter: str,
) -> None:
    store, artifact = _cli_project_update_review_fixture()
    artifact_id = str(artifact["id"])
    _install_cli_project_update_store(monkeypatch, store)
    argv = _cli_project_update_review_argv(adapter=adapter, artifact_id=artifact_id, action="accept")
    first_exit = cli_module.main(argv)
    first_output = capsys.readouterr()
    _accept_later_cli_project_update(store, first_artifact_id=artifact_id)
    state_before_retry = deepcopy((store.projects, store.memories, store.artifacts, store.revisions, store.events))

    second_exit = cli_module.main(argv)

    second_output = capsys.readouterr()
    assert first_exit == second_exit == 0
    assert json.loads(first_output.out) == json.loads(second_output.out)
    assert first_output.err == second_output.err == ""
    assert store.projects["project-1"]["current_state"] == "Later accepted CLI project state B."
    assert (store.projects, store.memories, store.artifacts, store.revisions, store.events) == state_before_retry


def test_generic_telegram_ingest_fails_before_reading_payload(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: Settings(database_url="postgresql://db", auth_user_id=str(uuid4())),
    )

    exit_code = cli_module.main(["vnext", "connectors", "ingest", "telegram", str(tmp_path / "missing.json")])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    _assert_cli_error(
        captured.err,
        code="invalid_request",
        message="The command request is invalid",
    )
    assert "missing.json" not in captured.err


def test_main_returns_error_for_non_object_json_on_review_apply(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: Settings(database_url="postgresql://db", auth_user_id=str(uuid4())),
    )

    exit_code = cli_module.main(
        [
            "review",
            "apply",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "--action",
            "edit",
            "--body-json",
            "[]",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    _assert_cli_error(
        captured.err,
        code="invalid_request",
        message="The command request is invalid",
    )


def test_recall_formatting_is_deterministic() -> None:
    payload: ContinuityRecallResponse = {
        "items": [
            {
                "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "capture_event_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "object_type": "Decision",
                "status": "active",
                "lifecycle": {
                    "is_preserved": True,
                    "preservation_status": "preserved",
                    "is_searchable": True,
                    "searchability_status": "searchable",
                    "is_promotable": True,
                    "promotion_status": "promotable",
                },
                "title": "Decision: Keep rollout phased",
                "body": {"decision_text": "Keep rollout phased"},
                "provenance": {"thread_id": "thread-1"},
                "confirmation_status": "confirmed",
                "admission_posture": "DERIVED",
                "confidence": 0.95,
                "relevance": 1.0,
                "last_confirmed_at": "2026-03-30T10:00:00+00:00",
                "supersedes_object_id": None,
                "superseded_by_object_id": None,
                "scope_matches": [{"kind": "thread", "value": "thread-1"}],
                "provenance_references": [
                    {"source_kind": "continuity_capture_event", "source_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
                    {"source_kind": "thread", "source_id": "thread-1"},
                ],
                "ordering": {
                    "scope_match_count": 1,
                    "query_term_match_count": 2,
                    "confirmation_rank": 3,
                    "freshness_posture": "fresh",
                    "freshness_rank": 4,
                    "provenance_posture": "strong",
                    "provenance_rank": 3,
                    "supersession_posture": "current",
                    "supersession_rank": 3,
                    "posture_rank": 2,
                    "lifecycle_rank": 4,
                    "open_contradiction_count": 0,
                    "contradiction_penalty_score": 0.0,
                    "confidence": 0.95,
                },
                "explanation": {
                    "source_facts": [
                        {"kind": "capture_event", "label": "raw_content", "value": "Decision: Keep rollout phased"},
                        {"kind": "body", "label": "decision_text", "value": "Keep rollout phased"},
                    ],
                    "trust": {
                        "trust_class": "human_curated",
                        "trust_reason": "Inferred from confirmation or correction history.",
                        "confirmation_status": "confirmed",
                        "confidence": 0.95,
                        "provenance_posture": "strong",
                        "evidence_segment_count": 1,
                        "correction_count": 0,
                        "active_signal_count": 0,
                    },
                    "contradictions": {
                        "open_case_count": 0,
                        "resolved_case_count": 0,
                        "open_case_ids": [],
                        "kinds": [],
                        "counterpart_object_ids": [],
                        "penalty_score": 0.0,
                    },
                    "evidence_segments": [
                        {
                            "relationship": "captured_from",
                            "source_kind": "continuity_capture_event",
                            "source_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                            "display_name": "capture event",
                            "relative_path": None,
                            "segment_kind": "capture_event",
                            "locator": None,
                            "snippet": "Decision: Keep rollout phased",
                            "created_at": "2026-03-30T09:58:00+00:00",
                        }
                    ],
                    "supersession_notes": [],
                    "timestamps": {
                        "capture_created_at": "2026-03-30T09:58:00+00:00",
                        "created_at": "2026-03-30T09:59:00+00:00",
                        "updated_at": "2026-03-30T10:00:00+00:00",
                        "last_confirmed_at": "2026-03-30T10:00:00+00:00",
                    },
                },
                "created_at": "2026-03-30T09:59:00+00:00",
                "updated_at": "2026-03-30T10:00:00+00:00",
            }
        ],
        "summary": {
            "query": "rollout",
            "filters": {"thread_id": "thread-1", "since": None, "until": None},
            "limit": 20,
            "returned_count": 1,
            "total_count": 1,
            "order": ["relevance_desc", "created_at_desc", "id_desc"],
        },
    }

    rendered = cli_module.format_recall_output(payload)

    assert rendered == (
        "recall summary\n"
        "query: rollout\n"
        "filters: thread_id=thread-1\n"
        "returned: 1/1 (limit=20)\n"
        "order: relevance_desc, created_at_desc, id_desc\n"
        "items:\n"
        "  1. [Decision|active] Decision: Keep rollout phased\n"
        "    id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa capture_event_id=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb\n"
        "    lifecycle=preserved:True searchable:True promotable:True\n"
        "    confidence=0.950 relevance=1.000 confirmation=confirmed\n"
        "    freshness=fresh provenance=strong supersession=current\n"
        "    contradictions=0 penalty=0.000\n"
        "    source=(unknown)\n"
        "    provenance_refs=continuity_capture_event:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb; thread:thread-1\n"
        "    trust=human_curated reason=Inferred from confirmation or correction history. evidence_segments=1 corrections=0 active_signals=0\n"
        "    contradiction_summary=open=0 resolved=0 kinds= penalty=0.000\n"
        "    timestamps=capture_created_at=2026-03-30T09:58:00+00:00 created_at=2026-03-30T09:59:00+00:00 updated_at=2026-03-30T10:00:00+00:00 last_confirmed_at=2026-03-30T10:00:00+00:00\n"
        "    source_facts=raw_content=Decision: Keep rollout phased | decision_text=Keep rollout phased\n"
        '    evidence_segments=continuity_capture_event:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb "Decision: Keep rollout phased"\n'
        "    supersession_notes=(none)"
    )


def test_status_command_returns_unreachable_without_db_connection(monkeypatch, capsys) -> None:
    user_id = UUID("44444444-4444-4444-8444-444444444444")
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: Settings(
            database_url="postgresql://db",
            healthcheck_timeout_seconds=2,
            auth_user_id=str(user_id),
        ),
    )
    monkeypatch.setattr(cli_module, "ping_database", lambda *_args, **_kwargs: False)

    exit_code = cli_module.main(["status"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "database: unreachable" in captured.out
    assert f"user_id: {user_id}" in captured.out


def test_status_command_surfaces_latest_maintenance_snapshot(monkeypatch, capsys, tmp_path: Path) -> None:
    user_id = UUID("44444444-4444-4444-8444-444444444444")
    maintenance_report_path = tmp_path / "maintenance_status_latest.json"
    maintenance_report_path.write_text(
        json.dumps(
            {
                "summary": {
                    "status": "warn",
                    "schedule": "nightly",
                    "run_completed_at": "2026-04-11T01:00:00Z",
                    "failure_count": 0,
                    "warning_count": 2,
                },
                "jobs": [
                    {
                        "job_key": "stale_fact_marking",
                        "details": {"stale_fact_count": 3},
                    },
                    {
                        "job_key": "reembed_missing_segments",
                        "details": {"reembedded_segment_count": 5},
                    },
                    {
                        "job_key": "pattern_candidate_recompute",
                        "details": {"pattern_candidate_count": 8},
                    },
                    {
                        "job_key": "benchmark_regeneration",
                        "details": {"benchmark_status": "pass"},
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv(cli_module.MAINTENANCE_REPORT_PATH_ENV, str(maintenance_report_path))
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: Settings(
            database_url="postgresql://db",
            healthcheck_timeout_seconds=2,
            auth_user_id=str(user_id),
        ),
    )
    monkeypatch.setattr(cli_module, "ping_database", lambda *_args, **_kwargs: False)

    exit_code = cli_module.main(["status"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "maintenance: status=warn schedule=nightly" in captured.out
    assert "last_run=2026-04-11T01:00:00Z" in captured.out
    assert (
        "failures=0 warnings=2 stale_facts=3 reembedded_segments=5 pattern_candidates=8 benchmark=pass" in captured.out
    )


def test_status_command_reports_memory_hygiene_and_thread_health_when_database_is_reachable(
    monkeypatch,
    capsys,
) -> None:
    user_id = UUID("44444444-4444-4444-8444-444444444444")

    class FakeStatusStore:
        def count_continuity_review_queue(self, *, statuses: list[str]) -> int:
            return {
                ("active",): 2,
                ("stale",): 1,
                ("superseded",): 0,
                ("deleted",): 0,
            }[tuple(statuses)]

        def list_continuity_recall_candidates(self) -> list[dict[str, object]]:
            return [
                {
                    "id": uuid4(),
                    "status": "active",
                    "object_type": "Decision",
                    "is_searchable": True,
                    "is_promotable": True,
                },
                {
                    "id": uuid4(),
                    "status": "stale",
                    "object_type": "WaitingFor",
                    "is_searchable": True,
                    "is_promotable": False,
                },
            ]

        def count_continuity_capture_events(self) -> int:
            return 7

    @contextmanager
    def fake_store_context(_ctx):
        yield FakeStatusStore()

    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: Settings(
            database_url="postgresql://db",
            healthcheck_timeout_seconds=2,
            auth_user_id=str(user_id),
        ),
    )
    monkeypatch.setattr(cli_module, "ping_database", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(cli_module, "_store_context", fake_store_context)
    monkeypatch.setattr(
        cli_module,
        "compile_continuity_open_loop_dashboard",
        lambda *_args, **_kwargs: {
            "dashboard": {
                "summary": {"total_count": 3},
                "waiting_for": {"summary": {"total_count": 1}},
                "blocker": {"summary": {"total_count": 1}},
                "stale": {"summary": {"total_count": 1}},
                "next_action": {"summary": {"total_count": 0}},
            }
        },
    )
    monkeypatch.setattr(
        cli_module,
        "get_retrieval_evaluation_summary",
        lambda *_args, **_kwargs: {
            "summary": {
                "status": "healthy",
                "precision_at_k_mean": 0.875,
                "precision_at_1_mean": 1.0,
            }
        },
    )
    monkeypatch.setattr(
        cli_module,
        "get_memory_hygiene_dashboard_summary",
        lambda *_args, **_kwargs: {
            "dashboard": {
                "posture": "watch",
                "duplicate_group_count": 2,
                "stale_fact_count": 1,
                "unresolved_contradiction_count": 1,
                "weak_trust_count": 3,
                "review_queue_pressure": {"posture": "critical"},
            }
        },
    )
    monkeypatch.setattr(
        cli_module,
        "get_thread_health_dashboard",
        lambda *_args, **_kwargs: {
            "dashboard": {
                "posture": "critical",
                "recent_thread_count": 4,
                "stale_thread_count": 2,
                "risky_thread_count": 1,
                "watch_thread_count": 3,
            }
        },
    )

    exit_code = cli_module.main(["status"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "database: reachable" in captured.out
    assert (
        "memory_hygiene: posture=watch duplicate_groups=2 stale_facts=1 "
        "open_contradictions=1 weak_trust=3 queue_pressure=critical"
    ) in captured.out
    assert "thread_health: posture=critical recent=4 stale=2 risky=1 watch=3" in captured.out


def test_recall_formatting_renders_provenance_source_label_when_present() -> None:
    payload: ContinuityRecallResponse = {
        "items": [
            {
                "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "capture_event_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "object_type": "Decision",
                "status": "active",
                "lifecycle": {
                    "is_preserved": True,
                    "preservation_status": "preserved",
                    "is_searchable": True,
                    "searchability_status": "searchable",
                    "is_promotable": True,
                    "promotion_status": "promotable",
                },
                "title": "Decision: Keep rollout phased",
                "body": {"decision_text": "Keep rollout phased"},
                "provenance": {"source_kind": "openclaw_import", "source_label": "OpenClaw"},
                "confirmation_status": "confirmed",
                "admission_posture": "DERIVED",
                "confidence": 0.95,
                "relevance": 1.0,
                "last_confirmed_at": "2026-03-30T10:00:00+00:00",
                "supersedes_object_id": None,
                "superseded_by_object_id": None,
                "scope_matches": [],
                "provenance_references": [
                    {"source_kind": "continuity_capture_event", "source_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}
                ],
                "ordering": {
                    "scope_match_count": 0,
                    "query_term_match_count": 0,
                    "confirmation_rank": 3,
                    "freshness_posture": "fresh",
                    "freshness_rank": 4,
                    "provenance_posture": "strong",
                    "provenance_rank": 3,
                    "supersession_posture": "current",
                    "supersession_rank": 3,
                    "posture_rank": 2,
                    "lifecycle_rank": 4,
                    "confidence": 0.95,
                },
                "explanation": {
                    "source_facts": [],
                    "trust": {
                        "trust_class": "llm_single_source",
                        "trust_reason": "Inferred from a single capture or provenance chain.",
                        "confirmation_status": "confirmed",
                        "confidence": 0.95,
                        "provenance_posture": "strong",
                        "evidence_segment_count": 1,
                        "correction_count": 0,
                    },
                    "evidence_segments": [
                        {
                            "relationship": "captured_from",
                            "source_kind": "continuity_capture_event",
                            "source_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                            "display_name": "capture event",
                            "relative_path": None,
                            "segment_kind": "capture_event",
                            "locator": None,
                            "snippet": "Decision: Keep rollout phased",
                            "created_at": "2026-03-30T09:58:00+00:00",
                        }
                    ],
                    "supersession_notes": [],
                    "timestamps": {
                        "capture_created_at": "2026-03-30T09:58:00+00:00",
                        "created_at": "2026-03-30T09:59:00+00:00",
                        "updated_at": "2026-03-30T10:00:00+00:00",
                        "last_confirmed_at": "2026-03-30T10:00:00+00:00",
                    },
                },
                "created_at": "2026-03-30T09:59:00+00:00",
                "updated_at": "2026-03-30T10:00:00+00:00",
            }
        ],
        "summary": {
            "query": None,
            "filters": {"since": None, "until": None},
            "limit": 20,
            "returned_count": 1,
            "total_count": 1,
            "order": ["relevance_desc", "created_at_desc", "id_desc"],
        },
    }

    rendered = cli_module.format_recall_output(payload)
    assert "source=OpenClaw (openclaw_import)" in rendered


def test_backfill_embeddings_cli_exits_nonzero_when_provider_unconfigured(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)

    exit_code = cli_module.main(["vnext", "memories", "backfill-embeddings"])

    captured = capsys.readouterr()
    assert exit_code == 1
    _assert_cli_error(
        captured.err,
        code="invalid_request",
        message="The command request is invalid",
    )


def test_backfill_embeddings_cli_embeds_missing_memories_in_batches(monkeypatch) -> None:
    transaction_depth = 0

    class BackfillStore(FakeVNextCliStore):
        def __init__(self) -> None:
            super().__init__()
            self.embedding_updates: list[tuple[str, list[float]]] = []
            self.embedding_signatures: list[dict[str, object]] = []
            self.missing = [
                {"status": "active", "id": "00000000-0000-4000-8000-000000000001", "title": "One", "canonical_text": "First fact."},
                {"status": "active", "id": "00000000-0000-4000-8000-000000000002", "title": "Two", "canonical_text": "Second fact."},
                {"status": "active", "id": "00000000-0000-4000-8000-000000000003", "title": "", "canonical_text": "  "},
            ]

        def list_memories_missing_embeddings(
            self,
            *,
            limit: int = 100,
            after_id: str | None = None,
            **_signature: object,
        ):
            assert transaction_depth == 1
            rows = [row for row in self.missing if after_id is None or str(row["id"]) > after_id]
            return rows[:limit]

        def update_memory_embedding(
            self,
            *,
            memory_id: str,
            vector: list[float],
            **signature: object,
        ):
            assert transaction_depth == 1
            self.embedding_updates.append((memory_id, vector))
            self.embedding_signatures.append(signature)
            return {"id": memory_id}

    class StubProvider:
        provider = "stub"
        model = "stub-embedding"
        base_url = "https://Embed.Example:443/Case/V1"

        def embed_batch(self, texts):
            assert transaction_depth == 0
            return [[0.5] * 4 for _text in texts]

        def embed_text(self, text):
            return self.embed_batch([text])[0]

    store = BackfillStore()

    @contextmanager
    def fake_vnext_store_context(_ctx):
        nonlocal transaction_depth
        transaction_depth += 1
        try:
            yield store
        finally:
            transaction_depth -= 1

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    monkeypatch.setattr(cli_module, "get_embedding_provider", lambda: StubProvider())
    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"),
        database_url="postgresql://db",
        user_id=uuid4(),
    )
    args = cli_module.build_parser().parse_args(["vnext", "memories", "backfill-embeddings", "--batch-size", "2"])

    output = args.handler(ctx, args)

    payload = json.loads(output)
    assert transaction_depth == 0
    assert payload["embedded"] == 2
    assert payload["skipped"] == 1
    assert payload["failed"] == 0
    assert payload["reindexed_incompatible"] == 0
    assert payload["batches"] == 2
    assert [memory_id for memory_id, _vector in store.embedding_updates] == [
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
    ]
    assert all(signature["signature_version"] == 2 for signature in store.embedding_signatures)
    assert all(signature["endpoint"] for signature in store.embedding_signatures)


def test_backfill_embeddings_cli_exits_nonzero_when_any_batch_fails(monkeypatch, capsys) -> None:
    class BackfillStore(FakeVNextCliStore):
        def list_memories_missing_embeddings(
            self,
            *,
            limit: int = 100,
            after_id: str | None = None,
            **_signature: object,
        ):
            del limit
            if after_id is not None:
                return []
            return [
                {
                    "id": "00000000-0000-4000-8000-000000000001",
                    "status": "active",
                    "canonical_text": "Embedding request will fail.",
                }
            ]

    class FailingProvider:
        provider = "stub"
        model = "stub-embedding"
        base_url = "http://127.0.0.1:9999/v1"

        def embed_batch(self, texts):
            del texts
            raise VNextEmbeddingProviderError("connection refused")

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield BackfillStore()

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    monkeypatch.setattr(cli_module, "get_embedding_provider", lambda: FailingProvider())
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: Settings(database_url="postgresql://db", auth_user_id=str(uuid4())),
    )

    exit_code = cli_module.main(["vnext", "memories", "backfill-embeddings"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.out)["failed"] == 1
    _assert_cli_error(
        captured.err,
        code="embedding_batch_failed",
        message="An embedding batch failed",
    )


def test_backfill_embeddings_cli_names_the_refused_memory_and_embeds_its_neighbours(
    monkeypatch, capsys
) -> None:
    """One text the endpoint refuses is named with the reason; the other two get vectors.

    The list query also gets the provider's input cap, so a cap change finds the
    rows to embed again on Postgres.

    Mutations: drop ``**summarize_embedding_failures(failures)`` from the output of
    ``_run_vnext_memories_backfill_embeddings`` (the failed id and reason are then
    missing), or embed the whole batch without isolation in
    ``prepare_memory_embeddings`` (the two good memories are then counted as
    failed). Or remove ``embedding_input_cap=input_cap`` from the
    ``list_memories_missing_embeddings`` call there: the recorded call then has
    no cap and the cap assertion fails. Each makes this test fail.
    """

    refused_id = "00000000-0000-4000-8000-000000000002"

    class BackfillStore(FakeVNextCliStore):
        def __init__(self) -> None:
            super().__init__()
            self.stored: list[tuple[str, dict[str, object]]] = []
            self.list_calls: list[dict[str, object]] = []

        def list_memories_missing_embeddings(self, *, limit: int = 100, after_id: str | None = None, **signature):
            del limit
            self.list_calls.append(dict(signature))
            if after_id is not None:
                return []
            return [
                {"status": "active", "id": "00000000-0000-4000-8000-000000000001", "canonical_text": "First fact."},
                {"status": "active", "id": refused_id, "canonical_text": "REFUSED " + "x" * 50},
                {"status": "active", "id": "00000000-0000-4000-8000-000000000003", "canonical_text": "Third fact."},
            ]

        def update_memory_embedding(self, *, memory_id: str, vector: list[float], **signature: object):
            self.stored.append((memory_id, signature))
            return {"id": memory_id}

    class RefusingProvider:
        provider = "stub"
        model = "stub-embedding"
        base_url = "http://127.0.0.1:9/v1"
        max_input_chars = 1234

        def embed_batch(self, texts):
            if any("REFUSED" in text for text in texts):
                raise VNextEmbeddingProviderError(
                    "embeddings endpoint returned HTTP 400: input is too long", status=400
                )
            return [[0.5] * 4 for _text in texts]

    store = BackfillStore()

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    monkeypatch.setattr(cli_module, "get_embedding_provider", lambda: RefusingProvider())
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: Settings(database_url="postgresql://db", auth_user_id=str(uuid4())),
    )

    exit_code = cli_module.main(["vnext", "memories", "backfill-embeddings"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload["embedded"] == 2
    assert payload["failed"] == 1
    assert payload["failed_ids"] == [refused_id]
    assert payload["failed_ids_omitted"] == 0
    assert payload["failure_reasons"] == [
        {"count": 1, "reason": "embeddings endpoint returned HTTP 400: input is too long"}
    ]
    assert payload["truncated_inputs"] == 0
    assert payload["input_cap_chars"] == 1234
    # the cap reaches the list query, next to the rest of the signature, on every page
    assert store.list_calls
    assert all(call.get("embedding_input_cap") == 1234 for call in store.list_calls)
    assert store.list_calls[0]["embedding_model"] == "stub-embedding"
    assert [memory_id for memory_id, _signature in store.stored] == [
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000003",
    ]
    _assert_cli_error(
        captured.err,
        code="embedding_batch_failed",
        message="An embedding batch failed",
    )


def test_main_rejects_sqlite_database_url_with_onramp_pointer(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: Settings(database_url="postgresql://db", auth_user_id=str(uuid4())),
    )

    exit_code = cli_module.main(["--database-url", "sqlite:///alice.db", "status"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    _assert_cli_error(
        captured.err,
        code="invalid_request",
        message="The command request is invalid",
    )


def test_main_rejects_non_uuid_user_id_without_traceback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: Settings(database_url="postgresql://db", auth_user_id=str(uuid4())),
    )

    exit_code = cli_module.main(["--user-id", "not-a-uuid", "status"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    _assert_cli_error(
        captured.err,
        code="invalid_request",
        message="The command request is invalid",
    )


def test_version_flag_reports_distribution_version(capsys) -> None:
    from alicebot_api import __version__

    with pytest.raises(SystemExit) as excinfo:
        cli_module.main(["--version"])

    captured = capsys.readouterr()
    assert excinfo.value.code == 0
    assert captured.out.strip() == f"alicebot {__version__}"


def test_eval_suite_choices_derive_from_canonical_registry() -> None:
    from alicebot_api.vnext_evals import VNEXT_EVAL_SUITE_ORDER

    parser = cli_module.build_parser()

    for subcommand in ("run", "report"):
        args = parser.parse_args(["eval", subcommand, "--suite", "graph_hop_retrieval"])
        assert args.suite == "graph_hop_retrieval"
        for suite_key in VNEXT_EVAL_SUITE_ORDER:
            parsed = parser.parse_args(["eval", subcommand, "--suite", suite_key])
            assert parsed.suite == suite_key
        with pytest.raises(SystemExit):
            parser.parse_args(["eval", subcommand, "--suite", "not_a_suite"])
