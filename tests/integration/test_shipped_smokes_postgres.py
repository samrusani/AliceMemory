"""Shipped local smokes and the alpha check pass on fresh PostgreSQL users."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore

ROOT = Path(__file__).resolve().parents[2]

GATES = {
    "connector-hardening": "settings_rows_available telegram_cursor_persisted telegram_rejected_chat_logged local_folder_ignores_generated local_folder_dedupes_unchanged_restart health_counts_present",
    "local-cors": "strict_default_empty_allowlist root_env_template_has_explicit_local_origins ubuntu_env_template_has_explicit_local_origins web_env_template_points_to_local_api active_live_cors_valid_when_configured active_cors_does_not_use_wildcard",
    "secret-redaction": "clip_imported_or_deduped browser_token_absent capture_token_redacted",
    "dogfood-doctor": "doctor_ran no_blocking_failures migration_status_present connector_settings_checked",
    "live-capture-connectors": "telegram_imported_allowlisted local_folder_imported_and_ignored_generated browser_clip_imported agent_output_review_only health_telemetry_present",
    "capture-to-brief": "source_captured context_pack_includes_source daily_brief_created artifact_has_source_reference rating_recorded dogfooding_reflects_rating",
    "agentic-memory-commit": "trusted_hermes_commit_active committed_memory_enters_context sensitive_memory_requires_confirmation inline_confirmation_commits confidential_hermes_commit_refused external_source_review_required read_only_rejected project_scoped_commit_allowed project_scoped_out_of_scope_rejected correction_revises_memory forget_preserves_audit_and_excludes_context undo_supersedes_committed_memory undone_memory_leaves_context audit_includes_revision_and_undo_event recent_commits_visible",
    "agentic-scheduler": "scheduler_defaults_exist scheduler_disabled_by_default daily_workflow_enabled weekly_workflow_enabled memory_proposal_candidate daily_run_succeeded weekly_run_succeeded due_scan_executed scheduler_artifacts_reviewable blocked_policy_recorded pause_resume_completed run_history_visible",
    "operator-console": "source_review_action_persisted memory_review_action_persisted artifact_review_and_rating_persisted open_loop_created_from_source scheduler_run_now_created_artifact connector_health_visible doctor_readiness_available capture_to_brief_trace_exists event_log_records_actions",
    "agent-integration-pack": "agent_identified_as_openclaw agent_requested_project_context_pack scoped_context_pack_returned agent_output_stored_as_reviewable_source_or_artifact memory_proposal_in_review_queue no_trusted_memory_auto_promoted event_log_records_full_flow policy_blocks_restricted_domain_request vnext_agent_activity_visible",
    "headless-ubuntu": "installer_exists_and_is_executable installer_supports_required_flags uninstaller_exists_and_confirms_destructive_actions systemd_templates_exist systemd_runs_as_non_root systemd_binds_localhost_by_default env_template_documents_config_layout headless_install_doc_covers_ssh_tunnel hermes_guide_covers_identity_and_policy rc_release_notes_exist cto_summary_exists",
    "local-runtime": "daemon_once_completed due_scan_executed_all_workflows daily_brief_scheduled weekly_synthesis_scheduled connection_report_scheduled contradiction_report_scheduled open_loop_review_scheduled project_update_scan_scheduled scheduled_artifacts_reviewable scheduled_artifact_metadata_complete",
    "model-backed": "due_scan_ran_one_workflow run_succeeded artifact_reviewable artifact_model_backed local_route_enforced provider_metadata_present source_grounded_sections_present source_refs_present",
}

ALPHA_SMOKES = (
    "connector-hardening", "local-cors", "secret-redaction", "dogfood-doctor", "live-capture-connectors",
    "capture-to-brief", "agentic-memory-commit", "agentic-scheduler", "operator-console",
    "agent-integration-pack", "headless-ubuntu",
)


def _run(urls: dict[str, str], args: list[str], tmp_path: Path) -> dict:
    uid = uuid4()
    with user_connection(urls["app"], uid) as conn:
        ContinuityStore(conn).create_user(uid, f"fixture-{uid}@example.invalid", "Fixture")
    env = os.environ.copy()
    for key in (
        "ALICE_AGENT_API_KEY", "ALICE_LEGACY_SURFACES", "ALICE_PROJECT_SCOPING",
        "ALICE_EMBEDDINGS_BASE_URL", "ALICE_EMBEDDINGS_MODEL", "ALICE_EMBEDDINGS_API_KEY",
        "ALICE_RERANKER_BASE_URL", "ALICE_RERANKER_MODEL", "ALICE_RERANKER_API_KEY",
        "ALICE_SEARCH_QUALITY",
    ):
        env.pop(key, None)
    env.update({
        "DATABASE_URL": urls["app"], "DATABASE_ADMIN_URL": urls["admin"],
        "ALICE_SOURCE_EMBEDDINGS": "off", "ALICE_PROJECT_DIR": str(tmp_path),
        "PYTHONPATH": os.pathsep.join((str(ROOT / "apps/api/src"), str(ROOT))),
    })
    result = subprocess.run(
        [sys.executable, "-m", "alicebot_api", "--user-id", str(uid), "vnext", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return json.loads(result.stdout)


def _assert_smoke(result: dict, name: str) -> None:
    assert result["status"] == "passed"
    assert result["smoke"] == name
    assert set(result["gates"]) == set(GATES[name].split())
    assert all(value is True for value in result["gates"].values())


@pytest.mark.parametrize("name", tuple(GATES))
def test_each_shipped_smoke_passes_with_all_gates(migrated_database_urls, tmp_path, name):
    """Guard: force connector-hardening's status to failed and its case fails."""
    _assert_smoke(_run(migrated_database_urls, ["smoke", name], tmp_path), name)


def test_agent_pack_seed_matches_the_requested_project(migrated_database_urls, tmp_path):
    """Guard: remove the seed's project scope and both scoped-pack gates fail."""
    _assert_smoke(_run(migrated_database_urls, ["smoke", "agent-integration-pack"], tmp_path), "agent-integration-pack")


def test_capture_to_brief_uses_the_capture_day(migrated_database_urls, tmp_path):
    """Guard: restore the fixed 2026-05-11 report date and the source-reference gate fails."""
    _assert_smoke(_run(migrated_database_urls, ["smoke", "capture-to-brief"], tmp_path), "capture-to-brief")


def test_alpha_check_runs_all_shipped_smokes(migrated_database_urls, tmp_path):
    """Guard: skip the smoke loop and the expected smoke names are absent."""
    result = _run(migrated_database_urls, ["alpha", "check", "--headless"], tmp_path)
    assert result["status"] == "passed"
    assert [item["name"] for item in result["smokes"]] == list(ALPHA_SMOKES)
    for item in result["smokes"]:
        assert item["status"] == "passed"
        _assert_smoke(item["result"], item["name"])
    assert result["headless"]["api_reachability"]["status"] == "skipped"
    assert result["headless"]["web_reachability"]["status"] == "skipped"
