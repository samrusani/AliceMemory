#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
from uuid import UUID, uuid4

from alicebot_api.config import DEFAULT_DATABASE_URL
from alicebot_api.db import user_connection
from alicebot_api.mcp.types import MCP_CODED_ERROR_CODES, MCPCodedToolError
from alicebot_api.mcp_tools import (
    MCPRuntimeContext,
    MCPToolError,
    MCPToolNotFoundError,
    call_mcp_tool,
    list_mcp_tools,
)
from alicebot_api.store import ContinuityStore


THREAD_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
REQUIRED_HERMES_TOOL_NAMES = (
    "mcp_alice_core_alice_recall_debug",
    "mcp_alice_core_alice_resume",
    "mcp_alice_core_alice_open_loops",
    "mcp_alice_core_alice_capture_candidates",
    "mcp_alice_core_alice_commit_captures",
    "mcp_alice_core_alice_review_queue",
    "mcp_alice_core_alice_review_apply",
)
_TOOL_NOT_FOUND_CODE = "tool_not_found"
_TOOL_NOT_FOUND_MESSAGE = "The requested tool is not available"
_TOOL_REQUEST_FAILED_CODE = "tool_request_failed"
_TOOL_REQUEST_FAILED_MESSAGE = "The tool request could not be processed"
_TOOL_EXECUTION_FAILED_CODE = "tool_execution_failed"
_TOOL_EXECUTION_FAILED_MESSAGE = "The tool could not be executed"

logger = logging.getLogger(__name__)


def _stable_error_json(*, code: str, message: str) -> str:
    return json.dumps(
        {"error": {"code": code, "message": message}},
        separators=(",", ":"),
        sort_keys=True,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_hermes_mcp_smoke.py",
        description=(
            "Verify Hermes MCP runtime can discover and call Alice MCP tools "
            "(recall/resume/open-loops plus B2 capture and B3 review flows)."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
        help="Database URL for seeding and runtime calls.",
    )
    parser.add_argument(
        "--python-command",
        default=sys.executable,
        help="Python executable Hermes should use for the Alice MCP server.",
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Alice repository root used to compose PYTHONPATH for the MCP server.",
    )
    return parser


def _dispatch_mcp_tool(registry, *, tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
    payload = json.loads(registry.dispatch(tool_name, arguments))
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        stable_code = str(code) if isinstance(code, str) else _TOOL_EXECUTION_FAILED_CODE
        raise RuntimeError(f"{tool_name} failed with error code {stable_code}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"{tool_name} returned unexpected payload: {payload}")
    return result


def _build_local_mcp_compat_runtime():
    routes: dict[str, tuple[MCPRuntimeContext, str]] = {}

    class _Registry:
        def dispatch(self, tool_name: str, arguments: dict[str, object]) -> str:
            route = routes.get(tool_name)
            if route is None:
                return _stable_error_json(
                    code=_TOOL_NOT_FOUND_CODE,
                    message=_TOOL_NOT_FOUND_MESSAGE,
                )
            context, raw_tool_name = route
            try:
                result = call_mcp_tool(context, name=raw_tool_name, arguments=arguments)
            except MCPToolNotFoundError:
                logger.info("Hermes compatibility tool was not found", exc_info=True)
                return _stable_error_json(
                    code=_TOOL_NOT_FOUND_CODE,
                    message=_TOOL_NOT_FOUND_MESSAGE,
                )
            except MCPCodedToolError as exc:
                # The same closed set the stdio server sends: the code says what
                # kind of refusal it was, the message stays the fixed sentence.
                logger.warning("Hermes compatibility tool request was refused code=%s", exc.code, exc_info=True)
                return _stable_error_json(
                    code=exc.code if exc.code in MCP_CODED_ERROR_CODES else _TOOL_REQUEST_FAILED_CODE,
                    message=_TOOL_REQUEST_FAILED_MESSAGE,
                )
            except (MCPToolError, ValueError, TypeError):
                logger.warning("Hermes compatibility tool request failed", exc_info=True)
                return _stable_error_json(
                    code=_TOOL_REQUEST_FAILED_CODE,
                    message=_TOOL_REQUEST_FAILED_MESSAGE,
                )
            except Exception:
                logger.exception("Hermes compatibility tool execution failed")
                return _stable_error_json(
                    code=_TOOL_EXECUTION_FAILED_CODE,
                    message=_TOOL_EXECUTION_FAILED_MESSAGE,
                )
            return json.dumps({"result": result}, separators=(",", ":"), sort_keys=True)

    registry = _Registry()

    def register_mcp_servers(server_config: dict[str, object]) -> list[str]:
        routes.clear()
        registered: list[str] = []

        for server_name, server_value in server_config.items():
            if not isinstance(server_value, dict):
                continue
            env = server_value.get("env")
            if not isinstance(env, dict):
                continue

            database_url = str(env.get("DATABASE_URL", "")).strip()
            auth_user_id = str(env.get("ALICEBOT_AUTH_USER_ID", "")).strip()
            if database_url == "" or auth_user_id == "":
                continue

            context = MCPRuntimeContext(database_url=database_url, user_id=UUID(auth_user_id))

            tools_cfg = server_value.get("tools")
            includes: list[str] = []
            if isinstance(tools_cfg, dict):
                raw_include = tools_cfg.get("include", [])
                if isinstance(raw_include, list):
                    includes = [str(name).strip() for name in raw_include if str(name).strip() != ""]

            available = {
                str(tool.get("name", "")).strip()
                for tool in list_mcp_tools()
                if isinstance(tool, dict)
            }

            for tool_name in includes:
                if tool_name not in available:
                    continue
                registered_name = f"mcp_{server_name}_{tool_name}"
                routes[registered_name] = (context, tool_name)
                registered.append(registered_name)

        return registered

    def shutdown_mcp_servers() -> None:
        routes.clear()

    return register_mcp_servers, shutdown_mcp_servers, registry


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # This smoke exercises the legacy capture/review flows alongside a
    # full-surface core tool (alice_open_loops). LEGACY does not imply the
    # eight extra core tools, so both flags must be set for in-process listing.
    os.environ.setdefault("ALICE_MCP_LEGACY_TOOLS", "1")
    os.environ.setdefault("ALICE_MCP_FULL_TOOLS", "1")

    # Import Hermes MCP runtime lazily so the script can print a clear error
    # when Hermes dependencies are not installed in this Python environment.
    runtime_mode = "hermes_runtime"
    try:
        from tools.mcp_tool import register_mcp_servers, shutdown_mcp_servers
        from tools.registry import registry
    except ModuleNotFoundError:
        register_mcp_servers, shutdown_mcp_servers, registry = _build_local_mcp_compat_runtime()
        runtime_mode = "compat_shim"

    user_id = uuid4()
    email = f"hermes-smoke-{user_id}@example.com"
    pythonpath = f"{args.repo_root}/apps/api/src:{args.repo_root}/workers"

    with user_connection(args.database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, email, "Hermes Smoke")

        decision_capture = store.create_continuity_capture_event(
            raw_content="Decision: Keep Alice MCP local-first for Hermes verification.",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        decision = store.create_continuity_object(
            capture_event_id=decision_capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: Keep Alice MCP local-first for Hermes verification.",
            body={"decision_text": "Keep Alice MCP local-first for Hermes verification."},
            provenance={"thread_id": str(THREAD_ID), "source_event_ids": ["hermes-smoke-1"]},
            confidence=0.95,
        )

        waiting_capture = store.create_continuity_capture_event(
            raw_content="Waiting For: Hermes docs sign-off",
            explicit_signal="waiting_for",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_waiting_for",
        )
        waiting_for = store.create_continuity_object(
            capture_event_id=waiting_capture["id"],
            object_type="WaitingFor",
            status="active",
            title="Waiting For: Hermes docs sign-off",
            body={"waiting_for_text": "Hermes docs sign-off"},
            provenance={"thread_id": str(THREAD_ID), "source_event_ids": ["hermes-smoke-2"]},
            confidence=0.93,
        )

    server_config = {
        "alice_core": {
            "command": args.python_command,
            "args": ["-m", "alicebot_api.mcp_server"],
            "env": {
                "DATABASE_URL": args.database_url,
                "ALICEBOT_AUTH_USER_ID": str(user_id),
                "PYTHONPATH": pythonpath,
                # Legacy capture/review flows plus the debug recall view.
                "ALICE_MCP_LEGACY_TOOLS": "1",
                # alice_open_loops is full-surface; LEGACY does not imply it.
                "ALICE_MCP_FULL_TOOLS": "1",
            },
            "tools": {
                "include": [
                    "alice_recall_debug",
                    "alice_resume",
                    "alice_open_loops",
                    "alice_capture_candidates",
                    "alice_commit_captures",
                    "alice_review_queue",
                    "alice_review_apply",
                ],
                "resources": False,
                "prompts": False,
            },
        }
    }

    try:
        registered_tools = set(register_mcp_servers(server_config))
        required_tools = set(REQUIRED_HERMES_TOOL_NAMES)
        if not required_tools.issubset(registered_tools):
            missing = sorted(required_tools - registered_tools)
            raise RuntimeError(f"Hermes did not register expected tools: {missing}")

        recall = _dispatch_mcp_tool(
            registry,
            tool_name="mcp_alice_core_alice_recall_debug",
            arguments={"thread_id": str(THREAD_ID), "query": "Hermes", "limit": 5},
        )
        resume = _dispatch_mcp_tool(
            registry,
            tool_name="mcp_alice_core_alice_resume",
            arguments={"thread_id": str(THREAD_ID), "max_recent_changes": 5, "max_open_loops": 5},
        )
        open_loops = _dispatch_mcp_tool(
            registry,
            tool_name="mcp_alice_core_alice_open_loops",
            arguments={"thread_id": str(THREAD_ID), "limit": 5},
        )
        capture_candidates = _dispatch_mcp_tool(
            registry,
            tool_name="mcp_alice_core_alice_capture_candidates",
            arguments={
                "session_id": "hermes-smoke-session",
                "source_kind": "sync_turn",
                "user_content": "Decision: Keep provider plus MCP as the default Hermes deployment shape.",
                "assistant_content": "Note: Track a short migration runbook for MCP-only users.",
            },
        )

        candidate_list = capture_candidates.get("candidates")
        if not isinstance(candidate_list, list):
            raise RuntimeError("Capture candidates payload missing candidates list.")

        commit_captures = _dispatch_mcp_tool(
            registry,
            tool_name="mcp_alice_core_alice_commit_captures",
            arguments={
                "mode": "assist",
                "source_kind": "sync_turn",
                "sync_fingerprint": "hermes-smoke-sync-001",
                "candidates": candidate_list,
            },
        )
        commit_summary = commit_captures.get("summary")
        if not isinstance(commit_summary, dict):
            raise RuntimeError("Commit captures payload missing summary.")

        review_queue_before = _dispatch_mcp_tool(
            registry,
            tool_name="mcp_alice_core_alice_review_queue",
            arguments={"status": "pending_review", "limit": 10},
        )
        review_items = review_queue_before.get("items")
        if not isinstance(review_items, list):
            raise RuntimeError("Review queue payload missing items list.")

        review_item_id: str | None = None
        for item in review_items:
            if not isinstance(item, dict):
                continue
            if item.get("object_type") == "Note":
                item_id = item.get("id")
                if isinstance(item_id, str):
                    review_item_id = item_id
                    break
        if review_item_id is None:
            raise RuntimeError("Review queue did not contain a queued Note item from capture commit.")

        review_apply = _dispatch_mcp_tool(
            registry,
            tool_name="mcp_alice_core_alice_review_apply",
            arguments={
                "review_item_id": review_item_id,
                "action": "approve",
                "reason": "Hermes smoke validation approved queued note.",
            },
        )
        review_queue_after = _dispatch_mcp_tool(
            registry,
            tool_name="mcp_alice_core_alice_review_queue",
            arguments={"status": "pending_review", "limit": 10},
        )
        review_summary_before = review_queue_before.get("summary")
        review_summary_after = review_queue_after.get("summary")
        if not isinstance(review_summary_before, dict) or not isinstance(review_summary_after, dict):
            raise RuntimeError("Review queue payload missing summary.")
        review_action = review_apply.get("review_action")
        if not isinstance(review_action, dict):
            raise RuntimeError("Review apply payload missing review_action.")

        if recall["summary"]["returned_count"] < 1:
            raise RuntimeError("Recall returned no continuity items.")
        if resume["brief"]["last_decision"]["item"]["id"] != str(decision["id"]):
            raise RuntimeError("Resume did not surface the seeded decision.")
        # Core alice_open_loops lists vNext open loops ({items, count}); the
        # seeded legacy WaitingFor stays visible through the debug recall view.
        if not isinstance(open_loops.get("items"), list) or "count" not in open_loops:
            raise RuntimeError("Open loops did not return the core items/count shape.")
        recall_ids = {item["id"] for item in recall.get("items", [])}
        if str(waiting_for["id"]) not in recall_ids:
            raise RuntimeError("Debug recall did not surface the seeded waiting-for item.")
        if commit_summary.get("auto_saved_count", 0) < 1:
            raise RuntimeError("Commit captures did not auto-save any candidate.")
        if commit_summary.get("review_queued_count", 0) < 1:
            raise RuntimeError("Commit captures did not queue any candidate for review.")
        if review_summary_before.get("total_count", 0) < 1:
            raise RuntimeError("Review queue did not contain pending_review items after commit.")
        if review_action.get("resolved_action") != "confirm":
            raise RuntimeError("Review apply did not resolve action to confirm.")
        if review_summary_after.get("total_count", 0) >= review_summary_before.get("total_count", 0):
            raise RuntimeError("Review queue count did not drop after approval.")

        summary = {
            "runtime_mode": runtime_mode,
            "registered_tools": sorted(required_tools),
            "recall_items": recall["summary"]["returned_count"],
            "resume_last_decision_title": resume["brief"]["last_decision"]["item"]["title"],
            "open_loop_count": open_loops["count"],
            "capture_candidate_count": capture_candidates["summary"]["candidate_count"],
            "capture_auto_saved_count": commit_summary["auto_saved_count"],
            "capture_review_queued_count": commit_summary["review_queued_count"],
            "review_queue_pending_before_apply": review_summary_before["total_count"],
            "review_apply_resolved_action": review_action["resolved_action"],
            "review_queue_pending_after_apply": review_summary_after["total_count"],
        }
        print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    finally:
        shutdown_mcp_servers()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
