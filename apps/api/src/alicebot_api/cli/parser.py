from __future__ import annotations

import argparse
from alicebot_api import __version__
from alicebot_api.contracts import (
    CONTINUITY_CAPTURE_EXPLICIT_SIGNALS,
    CONTINUITY_CORRECTION_ACTIONS,
    CONTRADICTION_RESOLUTION_ACTIONS,
    DEFAULT_CONTINUITY_CAPTURE_LIMIT,
    DEFAULT_CONTINUITY_LIFECYCLE_LIMIT,
    DEFAULT_CONTINUITY_OPEN_LOOP_LIMIT,
    DEFAULT_CONTINUITY_RECALL_LIMIT,
    DEFAULT_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
    DEFAULT_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
    DEFAULT_CONTINUITY_REVIEW_LIMIT,
    DEFAULT_TEMPORAL_TIMELINE_LIMIT,
    DEFAULT_TRUSTED_FACT_PROMOTION_LIMIT,
    MAX_CONTINUITY_REVIEW_LIMIT,
    MAX_CONTINUITY_OPEN_LOOP_LIMIT,
    MAX_CONTINUITY_RECALL_LIMIT,
    MAX_CONTINUITY_LIFECYCLE_LIMIT,
    MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
    MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
    MAX_TASK_BRIEF_TOKEN_BUDGET,
    MAX_TEMPORAL_TIMELINE_LIMIT,
    MAX_TRUSTED_FACT_PROMOTION_LIMIT,
)
# Underscore aliases: the CLI facade copies every name in these modules onto
# ``alicebot_api.cli``, and its public names are pinned.
from alicebot_api.importer_paths import (
    DEFAULT_MAX_CHATGPT_EXPORT_BYTES as _DEFAULT_MAX_CHATGPT_EXPORT_BYTES,
    DEFAULT_MAX_TEXT_FILE_BYTES as _DEFAULT_MAX_TEXT_FILE_BYTES,
    MIB as _MIB,
    parse_max_file_mib as _parse_max_file_mib,
)
from alicebot_api.surface_flags import legacy_surfaces_enabled
from alicebot_api.vnext_agent_control import PERMISSION_PROFILES
from alicebot_api.vnext_evals import VNEXT_EVAL_SUITE_ORDER
from alicebot_api.vnext_retrieval import BUDGET_STRATEGIES, CONTEXT_DEPTHS
from alicebot_api.vnext_scheduler_runtime import DEFAULT_LOG_FILE, DEFAULT_PID_FILE, DEFAULT_STATUS_FILE
from alicebot_api.vnext_embeddings import MAX_EMBEDDINGS_BATCH_SIZE
from .constants import DEFAULT_CLI_USER_ID, DEFAULT_VNEXT_DEMO_DATASET_PATH, REVIEW_STATUS_CHOICES
from .agents import _run_agent_keys_create, _run_agent_keys_list, _run_agent_keys_revoke
from .arguments import (
    _add_continuity_brief_arguments,
    _add_model_generation_arguments,
    _add_scope_filter_arguments,
    _add_task_brief_arguments,
    _add_vnext_agent_arguments,
    _parse_datetime,
    _parse_uuid,
)
from .automation import (
    _run_connections_generate,
    _run_vnext_artifact_export,
    _run_vnext_artifact_review,
    _run_vnext_belief_review,
    _run_vnext_belief_state,
    _run_vnext_contradictions_generate,
    _run_vnext_graph_neighborhood,
    _run_vnext_graph_review,
    _run_vnext_open_loop_review,
    _run_vnext_open_loops_extract,
    _run_vnext_project_dashboard,
    _run_vnext_project_update_candidate,
    _run_vnext_project_update_review,
    _run_vnext_quality_export,
    _run_vnext_quality_rate,
    _run_vnext_queue_add,
    _run_vnext_queue_process_next,
)
from .capture import (
    _run_vnext_agents_ingest_output,
    _run_vnext_artifact_insight_feedback,
    _run_vnext_browser_clip,
    _run_vnext_connectors_configure,
    _run_vnext_connectors_health,
    _run_vnext_connectors_ingest,
    _run_vnext_connectors_list,
    _run_vnext_connectors_status,
    _run_vnext_demo_load,
    _run_vnext_demo_reset,
    _run_vnext_doctor,
    _run_vnext_dogfooding_dashboard,
    _run_vnext_local_folder_sync,
    _run_vnext_local_folder_watch,
    _run_vnext_migrations_status,
    _run_vnext_sources_capture_file,
    _run_vnext_sources_capture_text,
    _run_vnext_sources_import_chatgpt,
    _run_vnext_sources_import_markdown,
)
from .context import _run_context_pack, _run_daily_brief, _run_vnext_context_tree, _run_weekly_synthesis
from .continuity import (
    _run_brief,
    _run_contradictions_detect,
    _run_contradictions_list,
    _run_contradictions_resolve,
    _run_contradictions_show,
    _run_evidence_artifact,
    _run_explain,
    _run_lifecycle_list,
    _run_lifecycle_show,
    _run_mutation_candidates,
    _run_mutation_commit,
    _run_mutation_generate,
    _run_mutation_operations,
    _run_open_loops,
    _run_pattern_explain,
    _run_pattern_list,
    _run_playbook_explain,
    _run_playbook_list,
    _run_recall,
    _run_resume,
    _run_review_apply,
    _run_review_queue,
    _run_review_show,
    _run_state_at,
    _run_status,
    _run_task_brief_compare,
    _run_task_brief_compile,
    _run_task_brief_show,
    _run_timeline,
    _run_trust_signals,
)
from .evals import (
    _run_eval_run,
    _run_eval_runs,
    _run_eval_show,
    _run_eval_suites,
    _run_vnext_eval_report,
    _run_vnext_eval_run,
    _run_vnext_eval_seed,
)
from .memories import (
    _run_maintenance_sync_contradictions,
    _run_vnext_agent_policy_telemetry,
    _run_vnext_agent_propose_memory,
    _run_vnext_memories_backfill_embeddings,
    _run_vnext_memory_accept_consolidation,
    _run_vnext_memory_audit,
    _run_vnext_memory_commit,
    _run_vnext_memory_confirm,
    _run_vnext_memory_correct,
    _run_vnext_memory_expire,
    _run_vnext_memory_forget,
    _run_vnext_memory_quarantine,
    _run_vnext_memory_recent,
    _run_vnext_memory_redact,
    _run_vnext_memory_undo,
    _run_vnext_memory_unexpire,
)
from .scheduler import (
    _run_vnext_scheduler_daemon_start,
    _run_vnext_scheduler_daemon_status,
    _run_vnext_scheduler_daemon_stop,
    _run_vnext_scheduler_failures,
    _run_vnext_scheduler_pause,
    _run_vnext_scheduler_resume,
    _run_vnext_scheduler_run_due,
    _run_vnext_scheduler_run_now,
    _run_vnext_scheduler_runs,
    _run_vnext_scheduler_status,
)
from .shared import _run_capture
from .smokes import (
    _run_vnext_alpha_check,
    _run_vnext_smoke_agent_integration_pack,
    _run_vnext_smoke_agentic_memory_commit,
    _run_vnext_smoke_agentic_scheduler,
    _run_vnext_smoke_capture_to_brief,
    _run_vnext_smoke_connector_hardening,
    _run_vnext_smoke_dogfood_doctor,
    _run_vnext_smoke_headless_ubuntu,
    _run_vnext_smoke_live_capture_connectors,
    _run_vnext_smoke_local_cors,
    _run_vnext_smoke_local_runtime,
    _run_vnext_smoke_model_backed,
    _run_vnext_smoke_operator_console,
    _run_vnext_smoke_secret_redaction,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alicebot",
        description="Deterministic local CLI for Alice continuity workflows.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"alicebot {__version__}",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override database URL. Defaults to settings/env DATABASE_URL.",
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help=(
            f"Override acting user UUID. Defaults to ALICEBOT_AUTH_USER_ID when set, otherwise {DEFAULT_CLI_USER_ID}."
        ),
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    capture_parser = subparsers.add_parser("capture", help="Capture continuity input.")
    capture_parser.add_argument("raw_content", nargs="+", help="Raw continuity text to capture.")
    capture_parser.add_argument(
        "--explicit-signal",
        choices=CONTINUITY_CAPTURE_EXPLICIT_SIGNALS,
        default=None,
        help="Optional explicit signal for deterministic derivation.",
    )
    capture_parser.set_defaults(handler=_run_capture)

    context_pack_parser = subparsers.add_parser("context-pack", help="Compile an Alice vNext context pack.")
    context_pack_parser.add_argument("query", nargs="+", help="Query to compile context for.")
    context_pack_parser.add_argument("--domain", action="append", default=[], help="Allowed domain. Repeatable.")
    context_pack_parser.add_argument("--project", action="append", default=[], help="Project scope. Repeatable.")
    context_pack_parser.add_argument("--person", action="append", default=[], help="People scope. Repeatable.")
    context_pack_parser.add_argument(
        "--sensitivity-allowed",
        action="append",
        default=None,
        help="Allowed sensitivity. Repeatable.",
    )
    context_pack_parser.add_argument("--max-items", type=int, default=8, help="Maximum selected memories.")
    context_pack_parser.add_argument("--max-tokens", type=int, default=8000, help="Approximate context token budget.")
    context_pack_parser.add_argument(
        "--sources",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Force source documents on (--sources) or off (--no-sources). Omit to let --context-depth decide.",
    )
    context_pack_parser.add_argument(
        "--contradictions",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Force contradicting evidence on (--contradictions) or off (--no-contradictions). Omit to let --context-depth decide.",
    )
    context_pack_parser.add_argument(
        "--context-depth",
        choices=CONTEXT_DEPTHS,
        default=None,
        help="Cost/coverage tier: minimal (FTS only, at most 4 items), low (default), medium, or high.",
    )
    context_pack_parser.add_argument(
        "--budget-strategy",
        choices=BUDGET_STRATEGIES,
        default=None,
        help="How the token budget is spent: balanced (default), facts_first, recent_first, contradictions_first, or sources_first.",
    )
    context_pack_parser.set_defaults(handler=_run_context_pack)

    context_tree_parser = subparsers.add_parser("context-tree", help="Compile a read-only Alice vNext context tree.")
    context_tree_parser.add_argument("query", nargs="*", help="Optional query to shape tree selection.")
    context_tree_parser.add_argument("--domain", action="append", default=[], help="Allowed domain. Repeatable.")
    context_tree_parser.add_argument(
        "--sensitivity-allowed",
        action="append",
        default=None,
        help="Allowed sensitivity. Repeatable.",
    )
    context_tree_parser.add_argument("--limit", type=int, default=12, help="Maximum items per tree group.")
    context_tree_parser.add_argument("--no-events", action="store_true", help="Exclude recent event nodes.")
    context_tree_parser.set_defaults(handler=_run_vnext_context_tree)

    daily_brief_parser = subparsers.add_parser("daily-brief", help="Generate a vNext daily brief artifact.")
    daily_brief_parser.add_argument(
        "--generate",
        action="store_true",
        help="Accepted for compatibility; the daily brief is always generated.",
    )
    daily_brief_parser.add_argument("--generated-for", default=None, help="ISO date for the brief.")
    daily_brief_parser.add_argument("--domain", action="append", default=[], help="Allowed domain. Repeatable.")
    daily_brief_parser.add_argument("--project", action="append", default=[], help="Project scope. Repeatable.")
    daily_brief_parser.add_argument(
        "--sensitivity-allowed",
        action="append",
        default=None,
        help="Allowed sensitivity. Repeatable.",
    )
    daily_brief_parser.add_argument("--source-limit", type=int, default=8, help="Maximum source inputs.")
    daily_brief_parser.add_argument("--memory-limit", type=int, default=8, help="Maximum memory inputs.")
    daily_brief_parser.add_argument("--open-loop-limit", type=int, default=8, help="Maximum open-loop inputs.")
    daily_brief_parser.add_argument("--artifact-limit", type=int, default=4, help="Maximum recent artifact inputs.")
    daily_brief_parser.add_argument(
        "--no-discover-open-loops",
        action="store_true",
        help="Skip candidate open-loop discovery from source text.",
    )
    daily_brief_parser.add_argument(
        "--no-candidate-memories",
        action="store_true",
        help="Do not create candidate memories for workflows that support them.",
    )
    _add_model_generation_arguments(daily_brief_parser)
    daily_brief_parser.set_defaults(handler=_run_daily_brief)

    weekly_synthesis_parser = subparsers.add_parser(
        "weekly-synthesis",
        help="Generate a vNext weekly synthesis artifact.",
    )
    weekly_synthesis_parser.add_argument(
        "--generate",
        action="store_true",
        help="Accepted for compatibility; the weekly synthesis is always generated.",
    )
    weekly_synthesis_parser.add_argument("--generated-for", default=None, help="ISO date inside the target week.")
    weekly_synthesis_parser.add_argument("--domain", action="append", default=[], help="Allowed domain. Repeatable.")
    weekly_synthesis_parser.add_argument("--project", action="append", default=[], help="Project scope. Repeatable.")
    weekly_synthesis_parser.add_argument(
        "--sensitivity-allowed",
        action="append",
        default=None,
        help="Allowed sensitivity. Repeatable.",
    )
    weekly_synthesis_parser.add_argument("--source-limit", type=int, default=8, help="Maximum source inputs.")
    weekly_synthesis_parser.add_argument("--memory-limit", type=int, default=8, help="Maximum memory inputs.")
    weekly_synthesis_parser.add_argument("--open-loop-limit", type=int, default=8, help="Maximum open-loop inputs.")
    weekly_synthesis_parser.add_argument(
        "--artifact-limit", type=int, default=4, help="Maximum recent artifact inputs."
    )
    weekly_synthesis_parser.add_argument(
        "--no-discover-open-loops",
        action="store_true",
        help="Skip candidate open-loop discovery from source text.",
    )
    weekly_synthesis_parser.add_argument(
        "--no-candidate-memories",
        action="store_true",
        help="Do not create candidate memories from weekly insights.",
    )
    _add_model_generation_arguments(weekly_synthesis_parser)
    weekly_synthesis_parser.set_defaults(handler=_run_weekly_synthesis)

    connections_parser = subparsers.add_parser("connections", help="Generate vNext connection reports.")
    connections_subparsers = connections_parser.add_subparsers(dest="connections_command", required=True)
    connections_generate_parser = connections_subparsers.add_parser(
        "generate",
        help="Generate a vNext connection report and candidate graph edges.",
    )
    connections_generate_parser.add_argument("--query", default="", help="Optional search query for candidate inputs.")
    connections_generate_parser.add_argument(
        "--domain", action="append", default=[], help="Allowed domain. Repeatable."
    )
    connections_generate_parser.add_argument(
        "--project", action="append", default=[], help="Project scope. Repeatable."
    )
    connections_generate_parser.add_argument(
        "--sensitivity-allowed",
        action="append",
        default=None,
        help="Allowed sensitivity. Repeatable.",
    )
    connections_generate_parser.add_argument(
        "--max-connections",
        type=int,
        default=8,
        help="Maximum candidate connections.",
    )
    connections_generate_parser.add_argument(
        "--auto-accept-threshold",
        type=float,
        default=None,
        help="Optional confidence threshold for auto-accepted edges.",
    )
    _add_model_generation_arguments(connections_generate_parser)
    connections_generate_parser.set_defaults(handler=_run_connections_generate)

    agent_parser = subparsers.add_parser("agent", help="Manage agent authentication.")
    agent_subparsers = agent_parser.add_subparsers(dest="agent_command", required=True)
    agent_keys_parser = agent_subparsers.add_parser("keys", help="Manage per-agent API keys.")
    agent_keys_subparsers = agent_keys_parser.add_subparsers(dest="agent_keys_command", required=True)
    agent_keys_create_parser = agent_keys_subparsers.add_parser(
        "create",
        help="Create a per-agent API key. The raw key is printed exactly once.",
    )
    agent_keys_create_parser.add_argument("--agent-id", required=True, help="Agent id the key authenticates.")
    agent_keys_create_parser.add_argument(
        "--profile",
        required=True,
        choices=PERMISSION_PROFILES,
        help="Permission profile granted to the key.",
    )
    agent_keys_create_parser.add_argument("--label", default=None, help="Optional human-readable key label.")
    agent_keys_create_parser.add_argument(
        "--project-scope",
        default=None,
        help=(
            "Optionally bind the key to one project. Identities resolved from a bound key "
            "carry that project scope (payloads may narrow it, never widen it) and write "
            "actions outside the scope are blocked by policy."
        ),
    )
    agent_keys_create_parser.set_defaults(handler=_run_agent_keys_create)
    agent_keys_list_parser = agent_keys_subparsers.add_parser(
        "list",
        help="List agent API keys. Shows prefixes only, never hashes or raw keys.",
    )
    agent_keys_list_parser.add_argument("--limit", type=int, default=50, help="Maximum keys to return.")
    agent_keys_list_parser.set_defaults(handler=_run_agent_keys_list)
    agent_keys_revoke_parser = agent_keys_subparsers.add_parser("revoke", help="Revoke an agent API key.")
    agent_keys_revoke_parser.add_argument("key", help="Key prefix or key id to revoke.")
    agent_keys_revoke_parser.set_defaults(handler=_run_agent_keys_revoke)

    vnext_parser = subparsers.add_parser("vnext", help="Alice vNext workflows.")
    vnext_subparsers = vnext_parser.add_subparsers(dest="vnext_command", required=True)

    vnext_connectors_parser = vnext_subparsers.add_parser("connectors", help="List and ingest vNext connectors.")
    vnext_connectors_subparsers = vnext_connectors_parser.add_subparsers(
        dest="vnext_connectors_command",
        required=True,
    )

    vnext_connectors_list_parser = vnext_connectors_subparsers.add_parser(
        "list",
        help="List deterministic vNext connector definitions and defaults.",
    )
    vnext_connectors_list_parser.set_defaults(handler=_run_vnext_connectors_list)

    vnext_connectors_configure_parser = vnext_connectors_subparsers.add_parser(
        "configure",
        help="Configure a vNext connector without storing raw secrets.",
    )
    vnext_connectors_configure_parser.add_argument("connector_name", help="Connector name.")
    vnext_connectors_configure_parser.add_argument(
        "--enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable the connector; omit to preserve its current state.",
    )
    vnext_connectors_configure_parser.add_argument(
        "--secret-ref", default=None, help="Connector secret reference such as env:CONNECTOR_TOKEN."
    )
    vnext_connectors_configure_parser.add_argument(
        "--sync-mode",
        choices=("manual", "polling", "watch", "on_demand", "disabled"),
        default=None,
        help="Connector sync mode.",
    )
    vnext_connectors_configure_parser.add_argument(
        "--poll-interval-seconds", type=int, default=None, help="Polling interval."
    )
    vnext_connectors_configure_parser.add_argument("--domain", default=None, help="Default domain.")
    vnext_connectors_configure_parser.add_argument("--sensitivity", default=None, help="Default sensitivity.")
    vnext_connectors_configure_parser.add_argument(
        "--allowed-chat-id", action="append", default=[], help="Allowed Telegram chat id. Repeatable."
    )
    vnext_connectors_configure_parser.add_argument(
        "--path", action="append", default=[], help="Watched local folder path. Repeatable."
    )
    vnext_connectors_configure_parser.add_argument(
        "--recursive", action="store_true", default=None, help="Enable recursive local folder scans."
    )
    vnext_connectors_configure_parser.add_argument(
        "--extension", action="append", default=[], help="Allowed local file extension. Repeatable."
    )
    vnext_connectors_configure_parser.add_argument(
        "--ignore-pattern", action="append", default=[], help="Local folder ignore glob. Repeatable."
    )
    vnext_connectors_configure_parser.set_defaults(handler=_run_vnext_connectors_configure)

    vnext_connectors_status_parser = vnext_connectors_subparsers.add_parser("status", help="Show connector status.")
    vnext_connectors_status_parser.add_argument(
        "connector_name", nargs="?", default=None, help="Optional connector name."
    )
    vnext_connectors_status_parser.set_defaults(handler=_run_vnext_connectors_status)

    vnext_connectors_health_parser = vnext_connectors_subparsers.add_parser(
        "health", help="Show connector health telemetry."
    )
    vnext_connectors_health_parser.set_defaults(handler=_run_vnext_connectors_health)

    vnext_connectors_ingest_parser = vnext_connectors_subparsers.add_parser(
        "ingest",
        help="Ingest already-exported connector payload items into vNext sources.",
    )
    vnext_connectors_ingest_parser.add_argument("connector_name", help="Connector name, such as browser_clipper.")
    vnext_connectors_ingest_parser.add_argument("payload_path", help="JSON payload file, or CSV for csv_table.")
    vnext_connectors_ingest_parser.add_argument("--domain", default=None, help="Connector default domain override.")
    vnext_connectors_ingest_parser.add_argument(
        "--sensitivity",
        default=None,
        help="Connector default sensitivity override.",
    )
    vnext_connectors_ingest_parser.set_defaults(handler=_run_vnext_connectors_ingest)

    vnext_connectors_local_parser = vnext_connectors_subparsers.add_parser(
        "local-folder", help="Local folder and Obsidian capture controls."
    )
    vnext_local_subparsers = vnext_connectors_local_parser.add_subparsers(
        dest="vnext_local_folder_command", required=True
    )
    vnext_local_add_parser = vnext_local_subparsers.add_parser("add-path", help="Configure a watched folder path.")
    vnext_local_add_parser.add_argument("path", action="append", help="Watched folder path.")
    vnext_local_add_parser.add_argument(
        "--enabled", action="store_true", default=True, help="Enable local folder connector."
    )
    vnext_local_add_parser.add_argument("--domain", default="project", help="Default domain.")
    vnext_local_add_parser.add_argument("--sensitivity", default="private", help="Default sensitivity.")
    vnext_local_add_parser.add_argument(
        "--extension", action="append", default=[".md", ".txt"], help="Allowed extension."
    )
    vnext_local_add_parser.add_argument("--ignore-pattern", action="append", default=[], help="Ignore glob.")
    vnext_local_add_parser.set_defaults(
        connector_name="local_folder",
        recursive=True,
        secret_ref=None,
        sync_mode="watch",
        poll_interval_seconds=30,
        merge_paths=True,
        remove_paths=False,
        handler=_run_vnext_connectors_configure,
    )
    vnext_local_remove_parser = vnext_local_subparsers.add_parser(
        "remove-path", help="Record a watched folder removal."
    )
    vnext_local_remove_parser.add_argument("path", action="append", help="Path to remove from config.")
    vnext_local_remove_parser.add_argument("--domain", default="project")
    vnext_local_remove_parser.add_argument("--sensitivity", default="private")
    vnext_local_remove_parser.set_defaults(
        connector_name="local_folder",
        enabled=None,
        secret_ref=None,
        recursive=True,
        extension=[],
        ignore_pattern=[],
        sync_mode="watch",
        poll_interval_seconds=30,
        merge_paths=False,
        remove_paths=True,
        handler=_run_vnext_connectors_configure,
    )
    vnext_local_sync_parser = vnext_local_subparsers.add_parser("sync", help="Scan watched folder paths now.")
    vnext_local_sync_parser.add_argument("--path", action="append", default=[], help="Folder path. Repeatable.")
    vnext_local_sync_parser.add_argument("--no-recursive", action="store_true", help="Disable recursive scan.")
    vnext_local_sync_parser.add_argument(
        "--extension", action="append", default=[".md", ".txt"], help="Allowed extension."
    )
    vnext_local_sync_parser.add_argument("--ignore-pattern", action="append", default=[], help="Ignore glob.")
    vnext_local_sync_parser.add_argument("--domain", default=None, help="Default domain.")
    vnext_local_sync_parser.add_argument("--sensitivity", default=None, help="Default sensitivity.")
    vnext_local_sync_parser.set_defaults(handler=_run_vnext_local_folder_sync)
    vnext_local_watch_parser = vnext_local_subparsers.add_parser("watch", help="Poll watched folders for changes.")
    vnext_local_watch_parser.add_argument("--path", action="append", default=[], help="Folder path. Repeatable.")
    vnext_local_watch_parser.add_argument("--once", action="store_true", help="Run one scan and exit.")
    vnext_local_watch_parser.add_argument(
        "--max-runs", type=int, default=1, help="Maximum polling scans for non-daemon use."
    )
    vnext_local_watch_parser.add_argument("--interval-seconds", type=float, default=2.0, help="Polling interval.")
    vnext_local_watch_parser.add_argument("--no-recursive", action="store_true", help="Disable recursive scan.")
    vnext_local_watch_parser.add_argument(
        "--extension", action="append", default=[".md", ".txt"], help="Allowed extension."
    )
    vnext_local_watch_parser.add_argument("--ignore-pattern", action="append", default=[], help="Ignore glob.")
    vnext_local_watch_parser.add_argument("--domain", default=None, help="Default domain.")
    vnext_local_watch_parser.add_argument("--sensitivity", default=None, help="Default sensitivity.")
    vnext_local_watch_parser.set_defaults(handler=_run_vnext_local_folder_watch)
    vnext_local_status_parser = vnext_local_subparsers.add_parser("status", help="Show local folder connector status.")
    vnext_local_status_parser.set_defaults(connector_name="local_folder", handler=_run_vnext_connectors_status)

    vnext_browser_parser = vnext_connectors_subparsers.add_parser(
        "browser-clipper", help="Browser clipper MVP controls."
    )
    vnext_browser_subparsers = vnext_browser_parser.add_subparsers(dest="vnext_browser_command", required=True)
    vnext_browser_capture_parser = vnext_browser_subparsers.add_parser("capture", help="Capture a browser clip.")
    vnext_browser_capture_parser.add_argument("--url", required=True, help="Page URL.")
    vnext_browser_capture_parser.add_argument("--title", default=None, help="Page title.")
    vnext_browser_capture_parser.add_argument("--selected-text", default=None, help="Selected text.")
    vnext_browser_capture_parser.add_argument("--page-text", default=None, help="Optional page text.")
    vnext_browser_capture_parser.add_argument("--user-note", default=None, help="User note.")
    vnext_browser_capture_parser.add_argument(
        "--capture-token", default=None, help="Optional local browser clipper capture token."
    )
    vnext_browser_capture_parser.add_argument("--file", default=None, help="Optional file containing page text.")
    vnext_browser_capture_parser.add_argument(
        "--max-file-mib",
        type=_parse_max_file_mib,
        default=None,
        help=(
            "Refuse the file, before reading it, when it is larger than this many MiB. "
            f"Defaults to {_DEFAULT_MAX_TEXT_FILE_BYTES // _MIB}. Applies to --file."
        ),
    )
    vnext_browser_capture_parser.add_argument("--domain", default="professional", help="Default domain.")
    vnext_browser_capture_parser.add_argument("--sensitivity", default="private", help="Default sensitivity.")
    vnext_browser_capture_parser.set_defaults(handler=_run_vnext_browser_clip)
    vnext_browser_status_parser = vnext_browser_subparsers.add_parser("status", help="Show browser clipper status.")
    vnext_browser_status_parser.set_defaults(connector_name="browser_clipper", handler=_run_vnext_connectors_status)

    vnext_doctor_parser = vnext_subparsers.add_parser("doctor", help="Check local vNext dogfooding readiness.")
    vnext_doctor_parser.add_argument(
        "--fix-safe", action="store_true", help="Initialize missing safe connector defaults."
    )
    vnext_doctor_parser.add_argument("--ci", action="store_true", help="Run in CI/smoke mode with non-secret checks.")
    vnext_doctor_parser.set_defaults(handler=_run_vnext_doctor)

    vnext_migrations_parser = vnext_subparsers.add_parser("migrations", help="Inspect vNext migration readiness.")
    vnext_migrations_subparsers = vnext_migrations_parser.add_subparsers(dest="vnext_migrations_command", required=True)
    vnext_migrations_status_parser = vnext_migrations_subparsers.add_parser(
        "status", help="Show vNext migration status."
    )
    vnext_migrations_status_parser.set_defaults(handler=_run_vnext_migrations_status)

    vnext_alpha_parser = vnext_subparsers.add_parser("alpha", help="Run public alpha readiness checks.")
    vnext_alpha_subparsers = vnext_alpha_parser.add_subparsers(dest="vnext_alpha_command", required=True)
    vnext_alpha_check_parser = vnext_alpha_subparsers.add_parser("check", help="Check public alpha local readiness.")
    vnext_alpha_check_parser.add_argument(
        "--skip-smokes", action="store_true", help="Only summarize storage, doctor, and scheduler posture."
    )
    vnext_alpha_check_parser.add_argument(
        "--headless",
        action="store_true",
        help="Include headless Ubuntu packaging and optional service reachability checks.",
    )
    vnext_alpha_check_parser.add_argument(
        "--api-url", default=None, help="Optional local API URL to check, for example http://127.0.0.1:8000/healthz."
    )
    vnext_alpha_check_parser.add_argument(
        "--web-url", default=None, help="Optional local web URL to check, for example http://127.0.0.1:3000/vnext."
    )
    vnext_alpha_check_parser.add_argument(
        "--demo-cycle", action="store_true", help="Run demo load/reset as part of the headless check."
    )
    vnext_alpha_check_parser.set_defaults(handler=_run_vnext_alpha_check)

    vnext_demo_parser = vnext_subparsers.add_parser(
        "demo", help="Load or reset the safe vNext public alpha demo dataset."
    )
    vnext_demo_subparsers = vnext_demo_parser.add_subparsers(dest="vnext_demo_command", required=True)
    vnext_demo_load_parser = vnext_demo_subparsers.add_parser("load", help="Load the synthetic vNext demo dataset.")
    vnext_demo_load_parser.add_argument(
        "--fixture",
        default=str(DEFAULT_VNEXT_DEMO_DATASET_PATH),
        help="Path to the synthetic vNext demo dataset JSON.",
    )
    vnext_demo_load_parser.add_argument(
        "--reset", action="store_true", help="Archive prior rows for this dataset before loading."
    )
    vnext_demo_load_parser.set_defaults(handler=_run_vnext_demo_load)
    vnext_demo_reset_parser = vnext_demo_subparsers.add_parser(
        "reset", help="Archive rows from a synthetic vNext demo dataset."
    )
    vnext_demo_reset_parser.add_argument(
        "--dataset-id", default=None, help="Dataset id to reset. Defaults to the fixture dataset id."
    )
    vnext_demo_reset_parser.add_argument(
        "--fixture",
        default=str(DEFAULT_VNEXT_DEMO_DATASET_PATH),
        help="Fixture used to infer dataset id when --dataset-id is omitted.",
    )
    vnext_demo_reset_parser.set_defaults(handler=_run_vnext_demo_reset)

    vnext_sources_parser = vnext_subparsers.add_parser("sources", help="Capture and import vNext sources.")
    vnext_sources_subparsers = vnext_sources_parser.add_subparsers(dest="vnext_sources_command", required=True)

    vnext_capture_text_parser = vnext_sources_subparsers.add_parser(
        "capture-text",
        help="Capture manual text into the vNext source pipeline.",
    )
    vnext_capture_text_parser.add_argument("raw_text", nargs="+", help="Raw text to capture.")
    vnext_capture_text_parser.add_argument("--title", default=None, help="Optional source title.")
    vnext_capture_text_parser.add_argument("--domain", default="unknown", help="Source domain.")
    vnext_capture_text_parser.add_argument("--sensitivity", default="unknown", help="Source sensitivity.")
    vnext_capture_text_parser.set_defaults(handler=_run_vnext_sources_capture_text)

    vnext_capture_file_parser = vnext_sources_subparsers.add_parser(
        "capture-file",
        help="Capture a local text or Markdown file into the vNext source pipeline.",
    )
    vnext_capture_file_parser.add_argument("path", help="Path to a text or Markdown file.")
    vnext_capture_file_parser.add_argument("--domain", default="unknown", help="Source domain.")
    vnext_capture_file_parser.add_argument("--sensitivity", default="unknown", help="Source sensitivity.")
    vnext_capture_file_parser.add_argument(
        "--max-file-mib",
        type=_parse_max_file_mib,
        default=None,
        help=(
            "Refuse the file, before reading it, when it is larger than this many MiB. "
            f"Defaults to {_DEFAULT_MAX_TEXT_FILE_BYTES // _MIB}."
        ),
    )
    vnext_capture_file_parser.set_defaults(handler=_run_vnext_sources_capture_file)

    vnext_import_markdown_parser = vnext_sources_subparsers.add_parser(
        "import-markdown",
        help="Import a Markdown/Obsidian folder into the vNext source pipeline.",
    )
    vnext_import_markdown_parser.add_argument("folder", help="Folder containing Markdown files.")
    vnext_import_markdown_parser.add_argument("--domain", default="unknown", help="Source domain.")
    vnext_import_markdown_parser.add_argument("--sensitivity", default="unknown", help="Source sensitivity.")
    vnext_import_markdown_parser.add_argument(
        "--max-file-mib",
        type=_parse_max_file_mib,
        default=None,
        help=(
            "Refuse the import, before reading, when any one Markdown file is larger than "
            f"this many MiB. Defaults to {_DEFAULT_MAX_TEXT_FILE_BYTES // _MIB}."
        ),
    )
    vnext_import_markdown_parser.set_defaults(handler=_run_vnext_sources_import_markdown)

    vnext_import_chatgpt_parser = vnext_sources_subparsers.add_parser(
        "import-chatgpt",
        help="Import a ChatGPT export JSON file into the vNext source pipeline.",
    )
    vnext_import_chatgpt_parser.add_argument("path", help="Path to a ChatGPT export JSON file.")
    vnext_import_chatgpt_parser.add_argument("--domain", default="personal", help="Source domain.")
    vnext_import_chatgpt_parser.add_argument("--sensitivity", default="private", help="Source sensitivity.")
    vnext_import_chatgpt_parser.add_argument(
        "--max-file-mib",
        type=_parse_max_file_mib,
        default=None,
        help=(
            "Refuse the import, before reading, when the export file is larger than this many "
            f"MiB. Defaults to {_DEFAULT_MAX_CHATGPT_EXPORT_BYTES // _MIB}. The whole export is "
            "held in memory, at about 6 to 9 times its size."
        ),
    )
    vnext_import_chatgpt_parser.set_defaults(handler=_run_vnext_sources_import_chatgpt)

    vnext_queue_parser = vnext_subparsers.add_parser("queue", help="Manage the vNext task queue.")
    vnext_queue_subparsers = vnext_queue_parser.add_subparsers(dest="vnext_queue_command", required=True)

    vnext_queue_add_parser = vnext_queue_subparsers.add_parser("add", help="Add a vNext queue task.")
    vnext_queue_add_parser.add_argument("--type", required=True, help="Task type, such as synthesize or draft.")
    vnext_queue_add_parser.add_argument("--title", required=True, help="Task title.")
    vnext_queue_add_parser.add_argument("--instructions", required=True, help="Task instructions.")
    vnext_queue_add_parser.add_argument("--domain", default="unknown", help="Task domain.")
    vnext_queue_add_parser.add_argument("--sensitivity", default="unknown", help="Task sensitivity.")
    vnext_queue_add_parser.add_argument("--write-policy", default="proposal_only", help="Task write policy.")
    vnext_queue_add_parser.set_defaults(handler=_run_vnext_queue_add)

    vnext_queue_process_parser = vnext_queue_subparsers.add_parser(
        "process-next",
        help="Claim and process the next pending vNext queue task.",
    )
    vnext_queue_process_parser.set_defaults(handler=_run_vnext_queue_process_next)

    vnext_artifacts_parser = vnext_subparsers.add_parser("artifacts", help="Review and export vNext artifacts.")
    vnext_artifacts_subparsers = vnext_artifacts_parser.add_subparsers(dest="vnext_artifacts_command", required=True)

    vnext_artifact_review_parser = vnext_artifacts_subparsers.add_parser("review", help="Review a vNext artifact.")
    vnext_artifact_review_parser.add_argument("artifact_id", help="Artifact id.")
    vnext_artifact_review_parser.add_argument(
        "--action",
        choices=("review", "accept", "reject", "promote", "archive"),
        required=True,
        help="Review action.",
    )
    vnext_artifact_review_parser.set_defaults(handler=_run_vnext_artifact_review)

    vnext_artifact_export_parser = vnext_artifacts_subparsers.add_parser(
        "export",
        help="Export a vNext artifact as Markdown.",
    )
    vnext_artifact_export_parser.add_argument("artifact_id", help="Artifact id.")
    vnext_artifact_export_parser.add_argument("--output-dir", required=True, help="Directory for the Markdown file.")
    vnext_artifact_export_parser.set_defaults(handler=_run_vnext_artifact_export)

    vnext_quality_parser = vnext_subparsers.add_parser("quality", help="Rate and export vNext artifact quality evals.")
    vnext_quality_subparsers = vnext_quality_parser.add_subparsers(dest="vnext_quality_command", required=True)
    vnext_quality_rate_parser = vnext_quality_subparsers.add_parser("rate", help="Rate a generated artifact.")
    vnext_quality_rate_parser.add_argument("artifact_id", help="Artifact id.")
    vnext_quality_rate_parser.add_argument("--reviewer-id", default=None, help="Optional reviewer id.")
    vnext_quality_rate_parser.add_argument("--usefulness", type=int, default=None, help="Usefulness rating 1-5.")
    vnext_quality_rate_parser.add_argument("--accuracy", type=int, default=None, help="Accuracy rating 1-5.")
    vnext_quality_rate_parser.add_argument(
        "--source-grounding", type=int, default=None, help="Source grounding rating 1-5."
    )
    vnext_quality_rate_parser.add_argument(
        "--novel-connections", type=int, default=None, help="Novel connections rating 1-5."
    )
    vnext_quality_rate_parser.add_argument("--actionability", type=int, default=None, help="Actionability rating 1-5.")
    vnext_quality_rate_parser.add_argument(
        "--hallucination-risk", type=int, default=None, help="Hallucination risk rating 1-5."
    )
    vnext_quality_rate_parser.add_argument(
        "--verbosity",
        choices=("too_shallow", "right_sized", "too_verbose", "unknown"),
        default="unknown",
        help="Verbosity judgment.",
    )
    vnext_quality_rate_parser.add_argument("--missed-context", default=None, help="Missing context note.")
    vnext_quality_rate_parser.add_argument("--comments", default=None, help="Reviewer comments.")
    vnext_quality_rate_parser.set_defaults(handler=_run_vnext_quality_rate)
    vnext_quality_export_parser = vnext_quality_subparsers.add_parser("export", help="Export quality evals as JSON.")
    vnext_quality_export_parser.add_argument("--artifact-id", default=None, help="Optional artifact id filter.")
    vnext_quality_export_parser.add_argument("--limit", type=int, default=100, help="Maximum ratings to export.")
    vnext_quality_export_parser.set_defaults(handler=_run_vnext_quality_export)
    vnext_quality_insight_parser = vnext_quality_subparsers.add_parser(
        "insight", help="Record a quick useful-insight signal."
    )
    vnext_quality_insight_parser.add_argument("artifact_id", help="Artifact id.")
    vnext_quality_insight_parser.add_argument(
        "--useful-insight",
        required=True,
        choices=("yes", "no", "not_sure"),
        help="Whether the artifact produced a useful insight.",
    )
    vnext_quality_insight_parser.add_argument(
        "--surfaced-missed",
        choices=("yes", "no", "not_sure"),
        default=None,
        help="Whether Alice surfaced something the user would have missed.",
    )
    vnext_quality_insight_parser.add_argument("--comments", default=None, help="Optional feedback comments.")
    vnext_quality_insight_parser.set_defaults(handler=_run_vnext_artifact_insight_feedback)

    vnext_dogfooding_parser = vnext_subparsers.add_parser("dogfooding", help="Show vNext dogfooding metrics.")
    vnext_dogfooding_subparsers = vnext_dogfooding_parser.add_subparsers(dest="vnext_dogfooding_command", required=True)
    vnext_dogfooding_dashboard_parser = vnext_dogfooding_subparsers.add_parser(
        "dashboard", help="Show capture and usefulness metrics."
    )
    vnext_dogfooding_dashboard_parser.set_defaults(handler=_run_vnext_dogfooding_dashboard)

    vnext_graph_parser = vnext_subparsers.add_parser("graph", help="Review and inspect vNext graph edges.")
    vnext_graph_subparsers = vnext_graph_parser.add_subparsers(dest="vnext_graph_command", required=True)

    vnext_graph_review_parser = vnext_graph_subparsers.add_parser("review", help="Review a candidate graph edge.")
    vnext_graph_review_parser.add_argument("edge_id", help="Graph edge id.")
    vnext_graph_review_parser.add_argument(
        "--action",
        required=True,
        choices=("review", "accept", "reject"),
        help="Review action.",
    )
    vnext_graph_review_parser.set_defaults(handler=_run_vnext_graph_review)

    vnext_graph_neighborhood_parser = vnext_graph_subparsers.add_parser(
        "neighborhood",
        help="Show active graph edges around a target id.",
    )
    vnext_graph_neighborhood_parser.add_argument("target_id", help="Source, memory, artifact, project, or person id.")
    vnext_graph_neighborhood_parser.set_defaults(handler=_run_vnext_graph_neighborhood)

    vnext_contradictions_parser = vnext_subparsers.add_parser(
        "contradictions",
        help="Generate vNext contradiction reports.",
    )
    vnext_contradictions_subparsers = vnext_contradictions_parser.add_subparsers(
        dest="vnext_contradictions_command",
        required=True,
    )
    vnext_contradictions_generate_parser = vnext_contradictions_subparsers.add_parser(
        "generate",
        help="Generate a vNext contradiction report and candidate contradiction edges.",
    )
    vnext_contradictions_generate_parser.add_argument("--query", default="", help="Optional search query.")
    vnext_contradictions_generate_parser.add_argument(
        "--domain",
        action="append",
        default=[],
        help="Allowed domain. Repeatable.",
    )
    vnext_contradictions_generate_parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="Project scope. Repeatable.",
    )
    vnext_contradictions_generate_parser.add_argument(
        "--sensitivity-allowed",
        action="append",
        default=None,
        help="Allowed sensitivity. Repeatable.",
    )
    vnext_contradictions_generate_parser.add_argument(
        "--max-contradictions",
        type=int,
        default=8,
        help="Maximum candidate contradictions.",
    )
    _add_model_generation_arguments(vnext_contradictions_generate_parser)
    vnext_contradictions_generate_parser.set_defaults(handler=_run_vnext_contradictions_generate)

    vnext_beliefs_parser = vnext_subparsers.add_parser("beliefs", help="Review and inspect vNext beliefs.")
    vnext_beliefs_subparsers = vnext_beliefs_parser.add_subparsers(dest="vnext_beliefs_command", required=True)

    vnext_belief_review_parser = vnext_beliefs_subparsers.add_parser("review", help="Review a vNext belief.")
    vnext_belief_review_parser.add_argument("belief_id", help="Belief id.")
    vnext_belief_review_parser.add_argument(
        "--action",
        required=True,
        choices=("reinforce", "challenge", "supersede", "retire"),
        help="Belief review action.",
    )
    vnext_belief_review_parser.add_argument("--confidence", type=float, default=None, help="Optional confidence.")
    vnext_belief_review_parser.add_argument("--superseded-by", default=None, help="Replacement belief id.")
    vnext_belief_review_parser.set_defaults(handler=_run_vnext_belief_review)

    vnext_belief_state_parser = vnext_beliefs_subparsers.add_parser(
        "state",
        help="Show current and historical state for a vNext belief.",
    )
    vnext_belief_state_parser.add_argument("belief_id", help="Belief id.")
    vnext_belief_state_parser.set_defaults(handler=_run_vnext_belief_state)

    vnext_projects_parser = vnext_subparsers.add_parser("projects", help="Generate and review vNext project updates.")
    vnext_projects_subparsers = vnext_projects_parser.add_subparsers(dest="vnext_projects_command", required=True)

    vnext_project_update_parser = vnext_projects_subparsers.add_parser(
        "update-candidate",
        help="Generate a project update candidate artifact.",
    )
    vnext_project_update_parser.add_argument("--project-id", default=None, help="Project id.")
    vnext_project_update_parser.add_argument(
        "--domain", action="append", default=[], help="Allowed domain. Repeatable."
    )
    vnext_project_update_parser.add_argument(
        "--sensitivity-allowed",
        action="append",
        default=None,
        help="Allowed sensitivity. Repeatable.",
    )
    vnext_project_update_parser.add_argument("--max-items", type=int, default=8, help="Maximum selected inputs.")
    _add_model_generation_arguments(vnext_project_update_parser)
    vnext_project_update_parser.set_defaults(handler=_run_vnext_project_update_candidate)

    vnext_project_review_parser = vnext_projects_subparsers.add_parser(
        "review-update",
        help="Accept, edit, or reject a project update candidate artifact.",
    )
    vnext_project_review_parser.add_argument("artifact_id", help="Project update artifact id.")
    vnext_project_review_parser.add_argument("--action", required=True, choices=("accept", "edit", "reject"))
    vnext_project_review_parser.add_argument("--edited-current-state", default=None, help="Edited current state.")
    vnext_project_review_parser.set_defaults(handler=_run_vnext_project_update_review)

    vnext_project_dashboard_parser = vnext_projects_subparsers.add_parser(
        "dashboard", help="Show project dashboard data."
    )
    vnext_project_dashboard_parser.add_argument("project_id", help="Project id.")
    vnext_project_dashboard_parser.add_argument(
        "--sensitivity-allowed",
        action="append",
        default=None,
        help="Allowed sensitivity. Repeatable.",
    )
    vnext_project_dashboard_parser.set_defaults(handler=_run_vnext_project_dashboard)

    vnext_open_loops_parser = vnext_subparsers.add_parser("open-loops", help="Extract and review vNext open loops.")
    vnext_open_loops_subparsers = vnext_open_loops_parser.add_subparsers(
        dest="vnext_open_loops_command",
        required=True,
    )
    vnext_open_loops_extract_parser = vnext_open_loops_subparsers.add_parser(
        "extract",
        help="Extract candidate open loops from selected sources.",
    )
    vnext_open_loops_extract_parser.add_argument("--project-id", default=None, help="Project id.")
    vnext_open_loops_extract_parser.add_argument("--person-id", default=None, help="Person id.")
    vnext_open_loops_extract_parser.add_argument(
        "--domain", action="append", default=[], help="Allowed domain. Repeatable."
    )
    vnext_open_loops_extract_parser.add_argument(
        "--sensitivity-allowed",
        action="append",
        default=None,
        help="Allowed sensitivity. Repeatable.",
    )
    vnext_open_loops_extract_parser.add_argument("--max-items", type=int, default=8, help="Maximum selected sources.")
    vnext_open_loops_extract_parser.set_defaults(handler=_run_vnext_open_loops_extract)

    vnext_open_loop_review_parser = vnext_open_loops_subparsers.add_parser(
        "review",
        help="Close, snooze, edit, or reopen a vNext open loop.",
    )
    vnext_open_loop_review_parser.add_argument("loop_id", help="Open loop id.")
    vnext_open_loop_review_parser.add_argument("--action", required=True, choices=("close", "snooze", "edit", "reopen"))
    vnext_open_loop_review_parser.add_argument("--title", default=None, help="Edited title.")
    vnext_open_loop_review_parser.add_argument("--description", default=None, help="Edited description.")
    vnext_open_loop_review_parser.add_argument("--due-at", default=None, help="ISO datetime for snooze/edit.")
    vnext_open_loop_review_parser.add_argument("--priority", default=None, help="Edited priority.")
    vnext_open_loop_review_parser.add_argument("--resolution-note", default=None, help="Resolution note for close.")
    vnext_open_loop_review_parser.set_defaults(handler=_run_vnext_open_loop_review)

    vnext_memories_parser = vnext_subparsers.add_parser(
        "memories",
        help="Commit, confirm, undo, correct, forget, expire, unexpire, redact, accept-consolidation, and audit vNext memories.",
    )
    vnext_memories_subparsers = vnext_memories_parser.add_subparsers(dest="vnext_memories_command", required=True)
    vnext_memory_commit_parser = vnext_memories_subparsers.add_parser(
        "commit",
        help="Commit an explicit trusted-agent memory write through Alice policy.",
    )
    _add_vnext_agent_arguments(vnext_memory_commit_parser)
    vnext_memory_commit_parser.add_argument("--intent", default="explicit_remember", help="Explicit memory intent.")
    vnext_memory_commit_parser.add_argument("--title", required=True, help="Memory title.")
    vnext_memory_commit_parser.add_argument("--text", required=True, help="Canonical memory text.")
    vnext_memory_commit_parser.add_argument("--memory-type", default="semantic", help="Memory type.")
    vnext_memory_commit_parser.add_argument("--domain", default="unknown", help="Domain label.")
    vnext_memory_commit_parser.add_argument("--sensitivity", default="unknown", help="Sensitivity label.")
    vnext_memory_commit_parser.add_argument("--confidence", type=float, default=0.9, help="Confidence from 0.0 to 1.0.")
    vnext_memory_commit_parser.add_argument("--source-type", default="direct_user_instruction", help="Source type.")
    vnext_memory_commit_parser.add_argument(
        "--source-ref", action="append", default=[], help="Source reference. Repeatable."
    )
    vnext_memory_commit_parser.add_argument(
        "--conversation-excerpt", default=None, help="Short user conversation excerpt."
    )
    vnext_memory_commit_parser.add_argument("--rationale", default=None, help="Agent rationale.")
    vnext_memory_commit_parser.add_argument("--idempotency-key", default=None, help="Idempotency key for retry safety.")
    vnext_memory_commit_parser.add_argument(
        "--contradiction-ref", action="append", default=[], help="Contradicted memory or edge id. Repeatable."
    )
    vnext_memory_commit_parser.set_defaults(handler=_run_vnext_memory_commit)

    vnext_memory_confirm_parser = vnext_memories_subparsers.add_parser(
        "confirm", help="Confirm, reject, or edit an inline memory confirmation."
    )
    _add_vnext_agent_arguments(vnext_memory_confirm_parser)
    vnext_memory_confirm_parser.add_argument("confirmation_id", help="Confirmation id.")
    vnext_memory_confirm_parser.add_argument(
        "--action", choices=("confirm", "reject", "edit"), default="confirm", help="Confirmation action."
    )
    vnext_memory_confirm_parser.add_argument("--text", default=None, help="Edited canonical memory text.")
    vnext_memory_confirm_parser.add_argument("--rationale", default=None, help="Confirmation rationale.")
    vnext_memory_confirm_parser.set_defaults(handler=_run_vnext_memory_confirm)

    vnext_memory_quarantine_parser = vnext_memories_subparsers.add_parser(
        "quarantine",
        help="Expire everything one agent key auto-promoted (operator action).",
    )
    _add_vnext_agent_arguments(vnext_memory_quarantine_parser)
    vnext_memory_quarantine_parser.add_argument(
        "--target-agent-id",
        required=True,
        help="The agent whose auto-promoted memories should be expired.",
    )
    vnext_memory_quarantine_parser.add_argument(
        "--reason", required=True, help="Why the sweep is being run. Recorded on every row."
    )
    vnext_memory_quarantine_parser.add_argument(
        "--since", default=None, help="ISO-8601 lower bound on the promotion time."
    )
    vnext_memory_quarantine_parser.add_argument(
        "--until", default=None, help="ISO-8601 upper bound on the promotion time."
    )
    vnext_memory_quarantine_parser.add_argument(
        "--dry-run", action="store_true", help="Report the set without expiring anything."
    )
    vnext_memory_quarantine_parser.set_defaults(handler=_run_vnext_memory_quarantine)

    vnext_memory_undo_parser = vnext_memories_subparsers.add_parser("undo", help="Undo an agentic memory commit.")
    _add_vnext_agent_arguments(vnext_memory_undo_parser)
    vnext_memory_undo_parser.add_argument(
        "--memory-id", default=None, help="Memory id. Defaults to the latest matching agentic commit."
    )
    vnext_memory_undo_parser.add_argument("--reason", default=None, help="Undo reason.")
    vnext_memory_undo_parser.set_defaults(handler=_run_vnext_memory_undo)

    vnext_memory_correct_parser = vnext_memories_subparsers.add_parser(
        "correct", help="Correct an agentic memory commit."
    )
    _add_vnext_agent_arguments(vnext_memory_correct_parser)
    vnext_memory_correct_parser.add_argument("memory_id", help="Memory id.")
    vnext_memory_correct_parser.add_argument("--text", required=True, help="Corrected canonical memory text.")
    vnext_memory_correct_parser.add_argument("--reason", default=None, help="Correction reason.")
    vnext_memory_correct_parser.set_defaults(handler=_run_vnext_memory_correct)

    vnext_memory_forget_parser = vnext_memories_subparsers.add_parser(
        "forget", help="Forget an agentic memory commit without deleting audit history."
    )
    _add_vnext_agent_arguments(vnext_memory_forget_parser)
    vnext_memory_forget_parser.add_argument("memory_id", help="Memory id.")
    vnext_memory_forget_parser.add_argument("--reason", default=None, help="Forget reason.")
    vnext_memory_forget_parser.set_defaults(handler=_run_vnext_memory_forget)

    vnext_memory_expire_parser = vnext_memories_subparsers.add_parser(
        "expire",
        help="Close a memory's validity window (valid_to) so recall stops returning it; the row stays active.",
    )
    _add_vnext_agent_arguments(vnext_memory_expire_parser)
    vnext_memory_expire_parser.add_argument("memory_id", help="Memory id.")
    vnext_memory_expire_parser.add_argument("--valid-to", default=None, help="ISO-8601 validity end. Defaults to now.")
    vnext_memory_expire_parser.add_argument("--reason", required=True, help="Expiry reason. Stored in the audit trail.")
    vnext_memory_expire_parser.set_defaults(handler=_run_vnext_memory_expire)

    vnext_memory_unexpire_parser = vnext_memories_subparsers.add_parser(
        "unexpire",
        help="Reopen an expired memory's validity window so recall returns it again.",
    )
    _add_vnext_agent_arguments(vnext_memory_unexpire_parser)
    vnext_memory_unexpire_parser.add_argument("memory_id", help="Memory id.")
    vnext_memory_unexpire_parser.add_argument(
        "--reason", required=True, help="Unexpire reason. Stored in the audit trail."
    )
    vnext_memory_unexpire_parser.set_defaults(handler=_run_vnext_memory_unexpire)

    vnext_memory_accept_consolidation_parser = vnext_memories_subparsers.add_parser(
        "accept-consolidation",
        help="Accept a consolidation candidate and supersede the memories it merges (human or admin agent only).",
    )
    _add_vnext_agent_arguments(vnext_memory_accept_consolidation_parser)
    vnext_memory_accept_consolidation_parser.add_argument("memory_id", help="Consolidation candidate memory id.")
    vnext_memory_accept_consolidation_parser.add_argument(
        "--reason", required=True, help="Acceptance reason. Stored in the audit trail."
    )
    vnext_memory_accept_consolidation_parser.set_defaults(handler=_run_vnext_memory_accept_consolidation)

    vnext_memory_redact_parser = vnext_memories_subparsers.add_parser(
        "redact",
        help=(
            "Permanently scrub governed memory-lifecycle copies and coupled project-update "
            "artifact copies, keeping the audit skeleton. Alice source/source-chunk evidence is "
            "retained (human or admin agent only)."
        ),
    )
    _add_vnext_agent_arguments(vnext_memory_redact_parser)
    vnext_memory_redact_parser.add_argument("memory_id", help="Memory id.")
    vnext_memory_redact_parser.add_argument(
        "--reason",
        required=True,
        help=(
            "Redaction reason. Required for authorization and lifecycle intent; intentionally "
            "not retained after successful true redaction."
        ),
    )
    vnext_memory_redact_parser.set_defaults(handler=_run_vnext_memory_redact)

    vnext_memory_recent_parser = vnext_memories_subparsers.add_parser(
        "recent", help="List recent agentic memory commits."
    )
    _add_vnext_agent_arguments(vnext_memory_recent_parser)
    vnext_memory_recent_parser.add_argument("--limit", type=int, default=20, help="Maximum commits to list.")
    vnext_memory_recent_parser.set_defaults(handler=_run_vnext_memory_recent)

    vnext_memory_audit_parser = vnext_memories_subparsers.add_parser("audit", help="Show memory audit details.")
    _add_vnext_agent_arguments(vnext_memory_audit_parser)
    vnext_memory_audit_parser.add_argument("memory_id", help="Memory id.")
    vnext_memory_audit_parser.set_defaults(handler=_run_vnext_memory_audit)

    vnext_memory_backfill_parser = vnext_memories_subparsers.add_parser(
        "backfill-embeddings",
        help=(
            "Embed active and accepted, unexpired memories with missing, unsigned, "
            "or provider/model-incompatible vectors."
        ),
    )
    vnext_memory_backfill_parser.add_argument(
        "--batch-size",
        type=int,
        default=32,
        help=f"Memories to embed per request (1-{MAX_EMBEDDINGS_BATCH_SIZE}).",
    )
    vnext_memory_backfill_parser.set_defaults(handler=_run_vnext_memories_backfill_embeddings)

    vnext_agents_parser = vnext_subparsers.add_parser("agents", help="Submit and inspect vNext agent proposals.")
    vnext_agents_subparsers = vnext_agents_parser.add_subparsers(dest="vnext_agents_command", required=True)
    vnext_agent_propose_parser = vnext_agents_subparsers.add_parser(
        "propose-memory",
        help="Submit an agent memory proposal for review.",
    )
    _add_vnext_agent_arguments(vnext_agent_propose_parser)
    vnext_agent_propose_parser.add_argument("--proposal-type", default="candidate_memory", help="Proposal type.")
    vnext_agent_propose_parser.add_argument("--memory-type", default="semantic", help="Stored candidate memory type.")
    vnext_agent_propose_parser.add_argument("--title", required=True, help="Proposal title.")
    vnext_agent_propose_parser.add_argument("--canonical-text", required=True, help="Canonical memory text.")
    vnext_agent_propose_parser.add_argument("--domain", default="unknown", help="Domain label.")
    vnext_agent_propose_parser.add_argument("--sensitivity", default="unknown", help="Sensitivity label.")
    vnext_agent_propose_parser.add_argument(
        "--sensitivity-allowed",
        action="append",
        default=None,
        help="Allowed sensitivity. Repeatable.",
    )
    vnext_agent_propose_parser.add_argument("--confidence", type=float, default=0.5, help="Proposal confidence.")
    vnext_agent_propose_parser.add_argument("--rationale", default=None, help="Proposal rationale.")
    vnext_agent_propose_parser.set_defaults(handler=_run_vnext_agent_propose_memory)
    vnext_agent_ingest_parser = vnext_agents_subparsers.add_parser(
        "ingest-output",
        help="Capture an agent output as source/artifact evidence.",
    )
    vnext_agent_ingest_parser.add_argument("--agent-id", required=True, help="Agent id.")
    vnext_agent_ingest_parser.add_argument("--agent-type", default="unknown", help="Agent type.")
    vnext_agent_ingest_parser.add_argument("--agent-run-id", default=None, help="Agent run id.")
    vnext_agent_ingest_parser.add_argument("--task-id", default=None, help="Task id.")
    vnext_agent_ingest_parser.add_argument(
        "--project-scope", action="append", default=[], help="Project scope. Repeatable."
    )
    vnext_agent_ingest_parser.add_argument(
        "--permission-profile", default="project_scoped_agent", help="Agent permission profile."
    )
    vnext_agent_ingest_parser.add_argument("--title", required=True, help="Output title.")
    vnext_agent_ingest_parser.add_argument("--file", default=None, help="File containing output content.")
    vnext_agent_ingest_parser.add_argument(
        "--max-file-mib",
        type=_parse_max_file_mib,
        default=None,
        help=(
            "Refuse the file, before reading it, when it is larger than this many MiB. "
            f"Defaults to {_DEFAULT_MAX_TEXT_FILE_BYTES // _MIB}. Applies to --file."
        ),
    )
    vnext_agent_ingest_parser.add_argument("content", nargs="*", help="Inline output content.")
    vnext_agent_ingest_parser.add_argument(
        "--output-type",
        choices=("sprint_summary", "research_summary", "code_review", "project_update", "decision", "general"),
        default="general",
        help="Agent output type.",
    )
    vnext_agent_ingest_parser.add_argument("--domain", default="project", help="Domain label.")
    vnext_agent_ingest_parser.add_argument("--sensitivity", default="private", help="Sensitivity label.")
    vnext_agent_ingest_parser.add_argument(
        "--source-ref", action="append", default=[], help="Source reference. Repeatable."
    )
    vnext_agent_ingest_parser.add_argument("--rationale", default=None, help="Optional rationale.")
    vnext_agent_ingest_parser.add_argument(
        "--propose-memory", action="store_true", help="Create review-only memory proposal."
    )
    vnext_agent_ingest_parser.set_defaults(handler=_run_vnext_agents_ingest_output)
    vnext_agent_telemetry_parser = vnext_agents_subparsers.add_parser(
        "policy-telemetry",
        help="Summarize vNext agent policy blocks, filters, reviews, workflows, and proposals.",
    )
    vnext_agent_telemetry_parser.add_argument("--agent-id", default=None, help="Optional agent id filter.")
    vnext_agent_telemetry_parser.add_argument(
        "--limit", type=int, default=200, help="Maximum agent events to summarize."
    )
    vnext_agent_telemetry_parser.set_defaults(handler=_run_vnext_agent_policy_telemetry)

    vnext_scheduler_parser = vnext_subparsers.add_parser("scheduler", help="Governed local vNext scheduler controls.")
    vnext_scheduler_subparsers = vnext_scheduler_parser.add_subparsers(dest="vnext_scheduler_command", required=True)
    vnext_scheduler_status_parser = vnext_scheduler_subparsers.add_parser("status", help="Show scheduler status.")
    vnext_scheduler_status_parser.set_defaults(handler=_run_vnext_scheduler_status)
    vnext_scheduler_runs_parser = vnext_scheduler_subparsers.add_parser("runs", help="List scheduler run history.")
    vnext_scheduler_runs_parser.add_argument("--workflow-type", default=None, help="Optional workflow type filter.")
    vnext_scheduler_runs_parser.add_argument("--limit", type=int, default=20, help="Maximum runs to return.")
    vnext_scheduler_runs_parser.set_defaults(handler=_run_vnext_scheduler_runs)
    vnext_scheduler_failures_parser = vnext_scheduler_subparsers.add_parser(
        "failures", help="List failed scheduler runs."
    )
    vnext_scheduler_failures_parser.add_argument("--workflow-type", default=None, help="Optional workflow type filter.")
    vnext_scheduler_failures_parser.add_argument("--limit", type=int, default=20, help="Maximum failed runs to return.")
    vnext_scheduler_failures_parser.set_defaults(handler=_run_vnext_scheduler_failures)
    vnext_scheduler_run_parser = vnext_scheduler_subparsers.add_parser("run-now", help="Run a workflow now.")
    _add_vnext_agent_arguments(vnext_scheduler_run_parser)
    vnext_scheduler_run_parser.add_argument("workflow_type", help="Workflow type, such as daily_brief.")
    vnext_scheduler_run_parser.add_argument("--generated-for", default=None, help="YYYY-MM-DD generation date.")
    vnext_scheduler_run_parser.add_argument("--domain", action="append", default=[], help="Allowed domain. Repeatable.")
    vnext_scheduler_run_parser.add_argument(
        "--sensitivity-allowed",
        action="append",
        default=None,
        help="Allowed sensitivity. Repeatable.",
    )
    _add_model_generation_arguments(vnext_scheduler_run_parser)
    vnext_scheduler_run_parser.set_defaults(handler=_run_vnext_scheduler_run_now)
    vnext_scheduler_run_due_parser = vnext_scheduler_subparsers.add_parser(
        "run-due", help="Run enabled workflows whose next_run_at is due."
    )
    _add_vnext_agent_arguments(vnext_scheduler_run_due_parser)
    vnext_scheduler_run_due_parser.add_argument("--limit", type=int, default=10, help="Maximum due workflows to run.")
    vnext_scheduler_run_due_parser.set_defaults(handler=_run_vnext_scheduler_run_due)
    vnext_scheduler_pause_parser = vnext_scheduler_subparsers.add_parser("pause", help="Pause all scheduler workflows.")
    _add_vnext_agent_arguments(vnext_scheduler_pause_parser)
    vnext_scheduler_pause_parser.set_defaults(handler=_run_vnext_scheduler_pause)
    vnext_scheduler_resume_parser = vnext_scheduler_subparsers.add_parser(
        "resume", help="Resume all scheduler workflows."
    )
    _add_vnext_agent_arguments(vnext_scheduler_resume_parser)
    vnext_scheduler_resume_parser.set_defaults(handler=_run_vnext_scheduler_resume)
    vnext_scheduler_daemon_parser = vnext_scheduler_subparsers.add_parser(
        "daemon", help="Run or inspect the local scheduler daemon."
    )
    vnext_scheduler_daemon_subparsers = vnext_scheduler_daemon_parser.add_subparsers(
        dest="vnext_scheduler_daemon_command", required=True
    )
    vnext_scheduler_daemon_start_parser = vnext_scheduler_daemon_subparsers.add_parser(
        "start", help="Start the local scheduler daemon."
    )
    vnext_scheduler_daemon_start_parser.add_argument(
        "--foreground", action="store_true", help="Run in the foreground instead of spawning a background process."
    )
    vnext_scheduler_daemon_start_parser.add_argument(
        "--once", action="store_true", help="Run one due scan, then exit. Useful for local smoke tests."
    )
    vnext_scheduler_daemon_start_parser.add_argument(
        "--interval-seconds", type=float, default=60.0, help="Due-scan polling interval."
    )
    vnext_scheduler_daemon_start_parser.add_argument(
        "--limit", type=int, default=10, help="Maximum due workflows per scan."
    )
    vnext_scheduler_daemon_start_parser.add_argument(
        "--pid-file", default=str(DEFAULT_PID_FILE), help="Daemon pid file."
    )
    vnext_scheduler_daemon_start_parser.add_argument(
        "--status-file", default=str(DEFAULT_STATUS_FILE), help="Daemon status JSON file."
    )
    vnext_scheduler_daemon_start_parser.add_argument(
        "--log-file", default=str(DEFAULT_LOG_FILE), help="Daemon log file."
    )
    vnext_scheduler_daemon_start_parser.set_defaults(handler=_run_vnext_scheduler_daemon_start)
    vnext_scheduler_daemon_status_parser = vnext_scheduler_daemon_subparsers.add_parser(
        "status", help="Show local scheduler daemon process status."
    )
    vnext_scheduler_daemon_status_parser.add_argument(
        "--pid-file", default=str(DEFAULT_PID_FILE), help="Daemon pid file."
    )
    vnext_scheduler_daemon_status_parser.add_argument(
        "--status-file", default=str(DEFAULT_STATUS_FILE), help="Daemon status JSON file."
    )
    vnext_scheduler_daemon_status_parser.set_defaults(handler=_run_vnext_scheduler_daemon_status)
    vnext_scheduler_daemon_stop_parser = vnext_scheduler_daemon_subparsers.add_parser(
        "stop", help="Stop the local scheduler daemon process."
    )
    vnext_scheduler_daemon_stop_parser.add_argument(
        "--pid-file", default=str(DEFAULT_PID_FILE), help="Daemon pid file."
    )
    vnext_scheduler_daemon_stop_parser.add_argument(
        "--status-file", default=str(DEFAULT_STATUS_FILE), help="Daemon status JSON file."
    )
    vnext_scheduler_daemon_stop_parser.set_defaults(handler=_run_vnext_scheduler_daemon_stop)

    vnext_smoke_parser = vnext_subparsers.add_parser("smoke", help="Run vNext smoke checks.")
    vnext_smoke_subparsers = vnext_smoke_parser.add_subparsers(dest="vnext_smoke_command", required=True)
    vnext_smoke_agentic_memory_parser = vnext_smoke_subparsers.add_parser(
        "agentic-memory-commit",
        help="Run the agentic memory commit, inline confirmation, undo, correction, and audit smoke.",
    )
    vnext_smoke_agentic_memory_parser.set_defaults(handler=_run_vnext_smoke_agentic_memory_commit)
    vnext_smoke_agentic_scheduler_parser = vnext_smoke_subparsers.add_parser(
        "agentic-scheduler",
        help="Run the agentic control-plane and governed scheduler smoke.",
    )
    vnext_smoke_agentic_scheduler_parser.set_defaults(handler=_run_vnext_smoke_agentic_scheduler)
    vnext_smoke_local_runtime_parser = vnext_smoke_subparsers.add_parser(
        "local-runtime",
        help="Run the local scheduler daemon and due-workflow smoke.",
    )
    vnext_smoke_local_runtime_parser.set_defaults(handler=_run_vnext_smoke_local_runtime)
    vnext_smoke_model_backed_parser = vnext_smoke_subparsers.add_parser(
        "model-backed",
        help="Run a Postgres-backed scheduled model-backed workflow smoke.",
    )
    vnext_smoke_model_backed_parser.set_defaults(handler=_run_vnext_smoke_model_backed)
    vnext_smoke_live_capture_parser = vnext_smoke_subparsers.add_parser(
        "live-capture-connectors",
        help="Run live connector capture framework smoke.",
    )
    vnext_smoke_live_capture_parser.set_defaults(handler=_run_vnext_smoke_live_capture_connectors)
    vnext_smoke_capture_to_brief_parser = vnext_smoke_subparsers.add_parser(
        "capture-to-brief",
        help="Run capture-to-context-to-artifact dogfooding smoke.",
    )
    vnext_smoke_capture_to_brief_parser.set_defaults(handler=_run_vnext_smoke_capture_to_brief)
    vnext_smoke_connector_hardening_parser = vnext_smoke_subparsers.add_parser(
        "connector-hardening",
        help="Run connector settings/state/cursor hardening smoke.",
    )
    vnext_smoke_connector_hardening_parser.set_defaults(handler=_run_vnext_smoke_connector_hardening)
    vnext_smoke_secret_redaction_parser = vnext_smoke_subparsers.add_parser(
        "secret-redaction",
        help="Run connector secret redaction smoke.",
    )
    vnext_smoke_secret_redaction_parser.set_defaults(handler=_run_vnext_smoke_secret_redaction)
    vnext_smoke_dogfood_doctor_parser = vnext_smoke_subparsers.add_parser(
        "dogfood-doctor",
        help="Run vNext dogfood doctor smoke.",
    )
    vnext_smoke_dogfood_doctor_parser.set_defaults(handler=_run_vnext_smoke_dogfood_doctor)
    vnext_smoke_local_cors_parser = vnext_smoke_subparsers.add_parser(
        "local-cors",
        help="Run local /vnext live CORS configuration smoke.",
    )
    vnext_smoke_local_cors_parser.set_defaults(handler=_run_vnext_smoke_local_cors)
    vnext_smoke_operator_console_parser = vnext_smoke_subparsers.add_parser(
        "operator-console",
        help="Run the live-backed /vnext operator console smoke.",
    )
    vnext_smoke_operator_console_parser.set_defaults(handler=_run_vnext_smoke_operator_console)
    vnext_smoke_agent_integration_pack_parser = vnext_smoke_subparsers.add_parser(
        "agent-integration-pack",
        help="Run the public alpha agent integration pack smoke.",
    )
    vnext_smoke_agent_integration_pack_parser.set_defaults(handler=_run_vnext_smoke_agent_integration_pack)
    vnext_smoke_headless_ubuntu_parser = vnext_smoke_subparsers.add_parser(
        "headless-ubuntu",
        help="Run the headless Ubuntu installer/docs/systemd packaging smoke.",
    )
    vnext_smoke_headless_ubuntu_parser.set_defaults(handler=_run_vnext_smoke_headless_ubuntu)

    mutations_parser = subparsers.add_parser("mutations", help="Generate, inspect, and apply memory operations.")
    mutations_subparsers = mutations_parser.add_subparsers(dest="mutations_command", required=True)

    mutation_generate_parser = mutations_subparsers.add_parser(
        "generate",
        help="Generate explicit mutation candidates from a turn pair.",
    )
    mutation_generate_parser.add_argument("--user-content", default="", help="User turn content.")
    mutation_generate_parser.add_argument("--assistant-content", default="", help="Assistant turn content.")
    mutation_generate_parser.add_argument(
        "--mode",
        choices=("manual", "assist", "auto"),
        default="assist",
        help="Mutation policy mode.",
    )
    mutation_generate_parser.add_argument("--sync-fingerprint", default=None, help="Optional sync fingerprint.")
    mutation_generate_parser.add_argument("--source-kind", default="sync_turn", help="Source kind label.")
    mutation_generate_parser.add_argument("--session-id", default=None, help="Optional session id.")
    mutation_generate_parser.add_argument("--thread-id", type=_parse_uuid, default=None, help="Optional thread UUID.")
    mutation_generate_parser.add_argument("--task-id", type=_parse_uuid, default=None, help="Optional task UUID.")
    mutation_generate_parser.add_argument("--project", default=None, help="Optional project scope.")
    mutation_generate_parser.add_argument("--person", default=None, help="Optional person scope.")
    mutation_generate_parser.add_argument(
        "--target-continuity-object-id",
        type=_parse_uuid,
        default=None,
        help="Optional explicit target continuity object UUID.",
    )
    mutation_generate_parser.set_defaults(handler=_run_mutation_generate)

    mutation_candidates_parser = mutations_subparsers.add_parser(
        "candidates",
        help="List generated mutation candidates.",
    )
    mutation_candidates_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CONTINUITY_CAPTURE_LIMIT,
        help="Max candidates (1-100).",
    )
    mutation_candidates_parser.add_argument(
        "--policy-action",
        choices=("auto_apply", "review_required", "skip"),
        default=None,
        help="Optional policy filter.",
    )
    mutation_candidates_parser.add_argument(
        "--operation-type",
        choices=("ADD", "UPDATE", "SUPERSEDE", "DELETE", "NOOP"),
        default=None,
        help="Optional operation filter.",
    )
    mutation_candidates_parser.add_argument("--sync-fingerprint", default=None, help="Optional sync fingerprint.")
    mutation_candidates_parser.set_defaults(handler=_run_mutation_candidates)

    mutation_commit_parser = mutations_subparsers.add_parser(
        "commit",
        help="Apply generated mutation candidates.",
    )
    mutation_commit_parser.add_argument(
        "candidate_ids",
        nargs="*",
        type=_parse_uuid,
        help="Candidate UUIDs to apply.",
    )
    mutation_commit_parser.add_argument("--sync-fingerprint", default=None, help="Optional sync fingerprint.")
    mutation_commit_parser.add_argument(
        "--include-review-required",
        action="store_true",
        help="Allow review-required candidates to apply.",
    )
    mutation_commit_parser.set_defaults(handler=_run_mutation_commit)

    mutation_operations_parser = mutations_subparsers.add_parser(
        "operations",
        help="List committed memory operations.",
    )
    mutation_operations_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CONTINUITY_CAPTURE_LIMIT,
        help="Max operations (1-100).",
    )
    mutation_operations_parser.add_argument("--sync-fingerprint", default=None, help="Optional sync fingerprint.")
    mutation_operations_parser.set_defaults(handler=_run_mutation_operations)

    brief_parser = subparsers.add_parser(
        "brief",
        help="Compile the primary one-call continuity brief.",
    )
    _add_continuity_brief_arguments(brief_parser)
    brief_parser.set_defaults(handler=_run_brief)

    recall_parser = subparsers.add_parser("recall", help="Recall continuity objects.")
    _add_scope_filter_arguments(recall_parser)
    recall_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CONTINUITY_RECALL_LIMIT,
        help=f"Max results (1-{MAX_CONTINUITY_RECALL_LIMIT}).",
    )
    recall_parser.add_argument(
        "--debug",
        action="store_true",
        help="Include hybrid retrieval stage scores and exclusion reasons.",
    )
    recall_parser.set_defaults(handler=_run_recall)

    state_at_parser = subparsers.add_parser(
        "state-at",
        help="Show entity state reconstructed at a specific point in time.",
    )
    state_at_parser.add_argument("entity_id", type=_parse_uuid, help="Entity UUID.")
    state_at_parser.add_argument("--at", type=_parse_datetime, default=None, help="As-of time (ISO-8601).")
    state_at_parser.set_defaults(handler=_run_state_at)

    timeline_parser = subparsers.add_parser(
        "timeline",
        help="Show chronological temporal history for one entity.",
    )
    timeline_parser.add_argument("entity_id", type=_parse_uuid, help="Entity UUID.")
    timeline_parser.add_argument("--since", type=_parse_datetime, default=None, help="Optional start time (ISO-8601).")
    timeline_parser.add_argument("--until", type=_parse_datetime, default=None, help="Optional end time (ISO-8601).")
    timeline_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_TEMPORAL_TIMELINE_LIMIT,
        help=f"Max timeline events (1-{MAX_TEMPORAL_TIMELINE_LIMIT}).",
    )
    timeline_parser.set_defaults(handler=_run_timeline)

    lifecycle_parser = subparsers.add_parser("lifecycle", help="Inspect continuity lifecycle state.")
    lifecycle_subparsers = lifecycle_parser.add_subparsers(dest="lifecycle_command", required=True)

    lifecycle_list_parser = lifecycle_subparsers.add_parser("list", help="List lifecycle states.")
    lifecycle_list_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CONTINUITY_LIFECYCLE_LIMIT,
        help=f"Max lifecycle results (1-{MAX_CONTINUITY_LIFECYCLE_LIMIT}).",
    )
    lifecycle_list_parser.set_defaults(handler=_run_lifecycle_list)

    lifecycle_show_parser = lifecycle_subparsers.add_parser("show", help="Show one lifecycle state.")
    lifecycle_show_parser.add_argument(
        "continuity_object_id",
        type=_parse_uuid,
        help="Continuity object UUID.",
    )
    lifecycle_show_parser.set_defaults(handler=_run_lifecycle_show)

    resume_parser = subparsers.add_parser("resume", help="Compile continuity resumption brief.")
    _add_scope_filter_arguments(resume_parser)
    resume_parser.add_argument(
        "--max-recent-changes",
        type=int,
        default=DEFAULT_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT,
        help=f"Recent change limit (0-{MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT}).",
    )
    resume_parser.add_argument(
        "--max-open-loops",
        type=int,
        default=DEFAULT_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT,
        help=f"Open loop limit (0-{MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT}).",
    )
    resume_parser.add_argument(
        "--include-non-promotable-facts",
        action="store_true",
        help="Include searchable but non-promotable facts in recent changes.",
    )
    resume_parser.add_argument(
        "--debug",
        action="store_true",
        help="Include the underlying hybrid retrieval trace.",
    )
    resume_parser.set_defaults(handler=_run_resume)

    if legacy_surfaces_enabled():
        task_briefs_parser = subparsers.add_parser(
            "task-briefs",
            help="Compile, compare, and inspect task-adaptive briefs.",
        )
        task_briefs_subparsers = task_briefs_parser.add_subparsers(dest="task_briefs_command", required=True)

        task_briefs_compile_parser = task_briefs_subparsers.add_parser(
            "compile",
            help="Compile and persist one task-adaptive brief.",
        )
        _add_task_brief_arguments(task_briefs_compile_parser)
        task_briefs_compile_parser.set_defaults(handler=_run_task_brief_compile)

        task_briefs_show_parser = task_briefs_subparsers.add_parser(
            "show",
            help="Load one persisted task brief.",
        )
        task_briefs_show_parser.add_argument("task_brief_id", type=_parse_uuid, help="Task brief UUID.")
        task_briefs_show_parser.set_defaults(handler=_run_task_brief_show)

        task_briefs_compare_parser = task_briefs_subparsers.add_parser(
            "compare",
            help="Compare two task brief modes for the same scope.",
        )
        _add_task_brief_arguments(task_briefs_compare_parser)
        task_briefs_compare_parser.add_argument(
            "--compare-to-mode",
            required=True,
            choices=("user_recall", "resume", "worker_subtask", "agent_handoff"),
            help="Secondary mode for comparison.",
        )
        task_briefs_compare_parser.add_argument(
            "--compare-briefing-strategy",
            choices=("balanced", "compact", "detailed"),
            default=None,
            help="Optional briefing strategy override for the comparison brief.",
        )
        task_briefs_compare_parser.add_argument(
            "--compare-token-budget",
            type=int,
            default=None,
            help=f"Optional comparison token budget (1-{MAX_TASK_BRIEF_TOKEN_BUDGET}).",
        )
        task_briefs_compare_parser.set_defaults(handler=_run_task_brief_compare)

    open_loops_parser = subparsers.add_parser(
        "open-loops",
        help="List open-loop dashboard grouped by posture.",
    )
    _add_scope_filter_arguments(open_loops_parser)
    open_loops_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CONTINUITY_OPEN_LOOP_LIMIT,
        help=f"Per-posture item limit (0-{MAX_CONTINUITY_OPEN_LOOP_LIMIT}).",
    )
    open_loops_parser.set_defaults(handler=_run_open_loops)

    review_parser = subparsers.add_parser("review", help="Review queue and correction commands.")
    review_subparsers = review_parser.add_subparsers(dest="review_command", required=True)

    review_queue_parser = review_subparsers.add_parser("queue", help="List review queue.")
    review_queue_parser.add_argument(
        "--status",
        choices=REVIEW_STATUS_CHOICES,
        default="correction_ready",
        help="Queue status filter.",
    )
    review_queue_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CONTINUITY_REVIEW_LIMIT,
        help=f"Max queue results (1-{MAX_CONTINUITY_REVIEW_LIMIT}).",
    )
    review_queue_parser.set_defaults(handler=_run_review_queue)

    review_show_parser = review_subparsers.add_parser("show", help="Show detail for one review object.")
    review_show_parser.add_argument("continuity_object_id", type=_parse_uuid, help="Continuity object UUID.")
    review_show_parser.set_defaults(handler=_run_review_show)

    review_apply_parser = review_subparsers.add_parser("apply", help="Apply a continuity correction.")
    review_apply_parser.add_argument("continuity_object_id", type=_parse_uuid, help="Continuity object UUID.")
    review_apply_parser.add_argument(
        "--action",
        required=True,
        choices=CONTINUITY_CORRECTION_ACTIONS,
        help="Correction action.",
    )
    review_apply_parser.add_argument("--reason", default=None, help="Optional correction reason.")
    review_apply_parser.add_argument("--title", default=None, help="Replacement title for edit.")
    review_apply_parser.add_argument(
        "--body-json",
        default=None,
        help="JSON object payload for body replacement on edit.",
    )
    review_apply_parser.add_argument(
        "--provenance-json",
        default=None,
        help="JSON object payload for provenance replacement on edit.",
    )
    review_apply_parser.add_argument(
        "--confidence",
        type=float,
        default=None,
        help="Updated confidence for edit/supersede.",
    )
    review_apply_parser.add_argument(
        "--replacement-title",
        default=None,
        help="Replacement title for supersede.",
    )
    review_apply_parser.add_argument(
        "--replacement-body-json",
        default=None,
        help="JSON object payload for supersede replacement body.",
    )
    review_apply_parser.add_argument(
        "--replacement-provenance-json",
        default=None,
        help="JSON object payload for supersede replacement provenance.",
    )
    review_apply_parser.add_argument(
        "--replacement-confidence",
        type=float,
        default=None,
        help="Replacement confidence for supersede.",
    )
    review_apply_parser.set_defaults(handler=_run_review_apply)

    contradictions_parser = subparsers.add_parser(
        "contradictions",
        help="Detect, inspect, and resolve continuity contradictions.",
    )
    contradictions_subparsers = contradictions_parser.add_subparsers(
        dest="contradictions_command",
        required=True,
    )

    contradictions_detect_parser = contradictions_subparsers.add_parser(
        "detect",
        help="Run contradiction detection and persist current cases.",
    )
    contradictions_detect_parser.add_argument(
        "--continuity-object-id",
        type=_parse_uuid,
        default=None,
        help="Optional continuity object UUID to scope detection.",
    )
    contradictions_detect_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CONTINUITY_REVIEW_LIMIT,
        help=f"Max contradiction rows to print (1-{MAX_CONTINUITY_REVIEW_LIMIT}).",
    )
    contradictions_detect_parser.set_defaults(handler=_run_contradictions_detect)

    contradictions_list_parser = contradictions_subparsers.add_parser(
        "list",
        help="List contradiction cases.",
    )
    contradictions_list_parser.add_argument(
        "--status",
        choices=("open", "resolved", "dismissed"),
        default="open",
        help="Case status filter.",
    )
    contradictions_list_parser.add_argument(
        "--continuity-object-id",
        type=_parse_uuid,
        default=None,
        help="Optional continuity object UUID filter.",
    )
    contradictions_list_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CONTINUITY_REVIEW_LIMIT,
        help=f"Max contradiction rows (1-{MAX_CONTINUITY_REVIEW_LIMIT}).",
    )
    contradictions_list_parser.set_defaults(handler=_run_contradictions_list)

    contradictions_show_parser = contradictions_subparsers.add_parser(
        "show",
        help="Show one contradiction case.",
    )
    contradictions_show_parser.add_argument(
        "contradiction_case_id",
        type=_parse_uuid,
        help="Contradiction case UUID.",
    )
    contradictions_show_parser.set_defaults(handler=_run_contradictions_show)

    contradictions_resolve_parser = contradictions_subparsers.add_parser(
        "resolve",
        help="Resolve one contradiction case.",
    )
    contradictions_resolve_parser.add_argument(
        "contradiction_case_id",
        type=_parse_uuid,
        help="Contradiction case UUID.",
    )
    contradictions_resolve_parser.add_argument(
        "--action",
        required=True,
        choices=CONTRADICTION_RESOLUTION_ACTIONS,
        help="Resolution action.",
    )
    contradictions_resolve_parser.add_argument(
        "--note",
        default=None,
        help="Optional operator note.",
    )
    contradictions_resolve_parser.set_defaults(handler=_run_contradictions_resolve)

    trust_parser = subparsers.add_parser(
        "trust",
        help="Inspect stored trust signals.",
    )
    trust_subparsers = trust_parser.add_subparsers(dest="trust_command", required=True)
    trust_signals_parser = trust_subparsers.add_parser("signals", help="List trust signals.")
    trust_signals_parser.add_argument(
        "--continuity-object-id",
        type=_parse_uuid,
        default=None,
        help="Optional continuity object UUID filter.",
    )
    trust_signals_parser.add_argument(
        "--signal-state",
        choices=("active", "inactive"),
        default="active",
        help="Signal state filter.",
    )
    trust_signals_parser.add_argument(
        "--signal-type",
        choices=("correction", "corroboration", "contradiction", "weak_inference"),
        default=None,
        help="Optional signal type filter.",
    )
    trust_signals_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CONTINUITY_REVIEW_LIMIT,
        help=f"Max trust signals (1-{MAX_CONTINUITY_REVIEW_LIMIT}).",
    )
    trust_signals_parser.set_defaults(handler=_run_trust_signals)

    explain_parser = subparsers.add_parser(
        "explain",
        help="Show continuity evidence or temporal explain output.",
    )
    explain_parser.add_argument(
        "continuity_object_id",
        nargs="?",
        type=_parse_uuid,
        help="Continuity object UUID.",
    )
    explain_parser.add_argument("--entity-id", type=_parse_uuid, default=None, help="Entity UUID.")
    explain_parser.add_argument("--at", type=_parse_datetime, default=None, help="As-of time (ISO-8601).")
    explain_parser.set_defaults(handler=_run_explain)

    evidence_parser = subparsers.add_parser("evidence", help="Inspect archived continuity artifacts.")
    evidence_subparsers = evidence_parser.add_subparsers(dest="evidence_command", required=True)
    evidence_artifact_parser = evidence_subparsers.add_parser("artifact", help="Show one archived artifact.")
    evidence_artifact_parser.add_argument("artifact_id", type=_parse_uuid, help="Continuity artifact UUID.")
    evidence_artifact_parser.set_defaults(handler=_run_evidence_artifact)

    patterns_parser = subparsers.add_parser("patterns", help="List and explain trusted fact patterns.")
    patterns_subparsers = patterns_parser.add_subparsers(dest="patterns_command", required=True)
    patterns_list_parser = patterns_subparsers.add_parser("list", help="List trusted fact patterns.")
    patterns_list_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_TRUSTED_FACT_PROMOTION_LIMIT,
        help=f"Max pattern results (1-{MAX_TRUSTED_FACT_PROMOTION_LIMIT}).",
    )
    patterns_list_parser.set_defaults(handler=_run_pattern_list)
    patterns_explain_parser = patterns_subparsers.add_parser("explain", help="Explain one trusted fact pattern.")
    patterns_explain_parser.add_argument("pattern_id", type=_parse_uuid, help="Pattern UUID.")
    patterns_explain_parser.set_defaults(handler=_run_pattern_explain)

    playbooks_parser = subparsers.add_parser("playbooks", help="List and explain trusted fact playbooks.")
    playbooks_subparsers = playbooks_parser.add_subparsers(dest="playbooks_command", required=True)
    playbooks_list_parser = playbooks_subparsers.add_parser("list", help="List trusted fact playbooks.")
    playbooks_list_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_TRUSTED_FACT_PROMOTION_LIMIT,
        help=f"Max playbook results (1-{MAX_TRUSTED_FACT_PROMOTION_LIMIT}).",
    )
    playbooks_list_parser.set_defaults(handler=_run_playbook_list)
    playbooks_explain_parser = playbooks_subparsers.add_parser("explain", help="Explain one trusted fact playbook.")
    playbooks_explain_parser.add_argument("playbook_id", type=_parse_uuid, help="Playbook UUID.")
    playbooks_explain_parser.set_defaults(handler=_run_playbook_explain)

    status_parser = subparsers.add_parser("status", help="Show local continuity runtime status.")
    status_parser.set_defaults(handler=_run_status)

    maintenance_parser = subparsers.add_parser("maintenance", help="Run explicit continuity maintenance jobs.")
    maintenance_subparsers = maintenance_parser.add_subparsers(dest="maintenance_command", required=True)
    maintenance_sync_contradictions_parser = maintenance_subparsers.add_parser(
        "sync-contradictions",
        help="Synchronize contradiction state across live continuity objects.",
    )
    maintenance_sync_contradictions_parser.add_argument(
        "--continuity-object-id",
        type=_parse_uuid,
        default=None,
        help="Optional continuity object UUID to scope the sync.",
    )
    maintenance_sync_contradictions_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CONTINUITY_REVIEW_LIMIT,
        help=f"Max contradiction rows to print (1-{MAX_CONTINUITY_REVIEW_LIMIT}).",
    )
    maintenance_sync_contradictions_parser.set_defaults(handler=_run_maintenance_sync_contradictions)

    vnext_eval_parser = subparsers.add_parser("eval", help="Run Alice vNext synthetic evals.")
    vnext_eval_subparsers = vnext_eval_parser.add_subparsers(dest="eval_command", required=True)

    vnext_eval_seed_parser = vnext_eval_subparsers.add_parser(
        "seed",
        help="Write the deterministic vNext synthetic benchmark corpus.",
    )
    vnext_eval_seed_parser.add_argument(
        "--output-path",
        default=None,
        help="Optional output path for the benchmark corpus JSON.",
    )
    vnext_eval_seed_parser.set_defaults(handler=_run_vnext_eval_seed)

    vnext_eval_run_parser = vnext_eval_subparsers.add_parser(
        "run",
        help="Run vNext eval suites against the synthetic corpus.",
    )
    vnext_eval_run_parser.add_argument(
        "--suite",
        choices=("all", *VNEXT_EVAL_SUITE_ORDER),
        default="all",
        help=(
            f"Suite key to run: all, {', '.join(VNEXT_EVAL_SUITE_ORDER)}. "
            "Live-store suites require ALICEBOT_EVAL_DATABASE_URL and report "
            "skipped without it."
        ),
    )
    vnext_eval_run_parser.add_argument(
        "--corpus-path",
        default=None,
        help="Optional benchmark corpus JSON path. Defaults to generated in-memory corpus when absent.",
    )
    vnext_eval_run_parser.add_argument(
        "--report-path",
        default=None,
        help="Optional output path for the vNext eval report JSON.",
    )
    vnext_eval_run_parser.add_argument(
        "--release-gate",
        action="store_true",
        help=(
            "Run as the canonical release gate: a run that never exercised the "
            "vector/paraphrase stage leaves its retrieval suite 'pass_fts_only', "
            "fails the aggregate case contract, and exits nonzero, so the gate "
            "cannot be green without measuring "
            "semantic retrieval quality (requires ALICE_EMBEDDINGS_* + pgvector)."
        ),
    )
    vnext_eval_run_parser.set_defaults(handler=_run_vnext_eval_run)

    vnext_eval_report_parser = vnext_eval_subparsers.add_parser(
        "report",
        help="Run vNext evals and write a canonical report artifact.",
    )
    vnext_eval_report_parser.add_argument(
        "--suite",
        choices=("all", *VNEXT_EVAL_SUITE_ORDER),
        default="all",
        help=(
            f"Suite key to report: all, {', '.join(VNEXT_EVAL_SUITE_ORDER)}. "
            "Live-store suites require ALICEBOT_EVAL_DATABASE_URL and report "
            "skipped without it."
        ),
    )
    vnext_eval_report_parser.add_argument(
        "--corpus-path",
        default=None,
        help="Optional benchmark corpus JSON path. Defaults to generated in-memory corpus when absent.",
    )
    vnext_eval_report_parser.add_argument(
        "--report-path",
        default=None,
        help="Optional output path for the vNext eval report JSON.",
    )
    vnext_eval_report_parser.set_defaults(handler=_run_vnext_eval_report)

    evals_parser = subparsers.add_parser("evals", help="Run and inspect public eval suites.")
    evals_subparsers = evals_parser.add_subparsers(dest="evals_command", required=True)

    evals_suites_parser = evals_subparsers.add_parser("suites", help="List public eval suites.")
    evals_suites_parser.set_defaults(handler=_run_eval_suites)

    evals_run_parser = evals_subparsers.add_parser("run", help="Run the public eval harness.")
    evals_run_parser.add_argument(
        "--suite-key",
        action="append",
        default=None,
        help="Optional suite key filter. Repeat to run multiple suites.",
    )
    evals_run_parser.add_argument(
        "--report-path",
        default=None,
        help="Optional output path for the canonical JSON report artifact.",
    )
    evals_run_parser.set_defaults(handler=_run_eval_run)

    evals_runs_parser = evals_subparsers.add_parser("runs", help="List persisted public eval runs.")
    evals_runs_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of eval runs to list.",
    )
    evals_runs_parser.set_defaults(handler=_run_eval_runs)

    evals_show_parser = evals_subparsers.add_parser("show", help="Show one persisted public eval run.")
    evals_show_parser.add_argument("eval_run_id", type=_parse_uuid, help="Eval run UUID.")
    evals_show_parser.set_defaults(handler=_run_eval_show)

    return parser
