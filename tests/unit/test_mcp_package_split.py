from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import alicebot_api.mcp as mcp_package
import alicebot_api.mcp.memories as mcp_memories
import alicebot_api.mcp.registry as mcp_registry
import alicebot_api.mcp.shared as mcp_shared
import alicebot_api.mcp_tools as mcp_tools


REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_PACKAGE = Path(mcp_package.__file__).resolve().parent
EXPECTED_MODULES = {
    "__init__.py",
    "arguments.py",
    "capture_automation.py",
    "capture_mutations.py",
    "context.py",
    "definitions.py",
    "evidence_artifacts.py",
    "memories.py",
    "policy.py",
    "projects.py",
    "registry.py",
    "retrieval.py",
    "retrieval_shared.py",
    "review.py",
    "runtime.py",
    "scheduler.py",
    "shared.py",
    "synthesis.py",
    "types.py",
}
EXPECTED_PUBLIC_EXPORTS = [
    "MCP_FULL_TOOLS_ENV",
    "MCP_LEGACY_TOOLS_ENV",
    "MCPRuntimeContext",
    "MCPToolError",
    "MCPToolNotFoundError",
    "call_mcp_tool",
    "list_mcp_tools",
    "redact_memory_flow",
]
EXPECTED_ALIAS_PAIRS = (
    ("alice_capture", "alice_vnext_capture"),
    ("alice_memory_commit", "alice_vnext_commit_memory"),
    ("alice_recent_decisions", "alice_vnext_recent_decisions"),
    ("alice_recent_changes", "alice_vnext_recent_changes"),
    ("alice_generate_connections", "alice_vnext_find_connections"),
    ("alice_generate_contradictions", "alice_vnext_find_contradictions"),
    ("alice_project_dashboard", "alice_vnext_project_dashboard"),
)
EXPECTED_FACADE_ANNOTATIONS = {
    "_MODEL_GENERATION_SCHEMA_PROPERTIES": "dict[str, object]",
    "_VNEXT_AGENT_SCHEMA_PROPERTIES": "dict[str, object]",
    "_AGENT_IDENTITY_SCHEMA_PROPERTIES": "dict[str, object]",
    "_DOMAINS_FILTER_SCHEMA": "dict[str, object]",
    "_MEMORY_TYPES_FILTER_SCHEMA": "dict[str, object]",
    "_SENSITIVITY_ALLOWED_SCHEMA": "dict[str, object]",
    "_CORRECTION_BODY_SCHEMA": "dict[str, object]",
    "_REVIEW_PROVENANCE_SCHEMA": "dict[str, object]",
    "_CONTINUITY_PROVENANCE_SCHEMA": "dict[str, object]",
    "_CONTINUITY_CAPTURE_CANDIDATE_SCHEMA": "dict[str, object]",
    "_CORE_TOOL_DEFINITIONS": "list[dict[str, object]]",
    "_LEGACY_TOOL_DEFINITIONS": "list[dict[str, object]]",
}


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _tool_names() -> list[str]:
    return [str(tool["name"]) for tool in mcp_tools.list_mcp_tools()]


def test_mcp_monolith_is_a_thin_identity_preserving_facade() -> None:
    facade_path = Path(mcp_tools.__file__).resolve()
    assert len(facade_path.read_text(encoding="utf-8").splitlines()) < 1_500
    assert mcp_tools.__all__ == EXPECTED_PUBLIC_EXPORTS
    assert mcp_tools.MCPRuntimeContext is mcp_shared.MCPRuntimeContext
    assert mcp_tools.MCPToolError is mcp_shared.MCPToolError
    assert mcp_tools.MCPToolNotFoundError is mcp_shared.MCPToolNotFoundError
    assert mcp_tools.call_mcp_tool is mcp_registry.call_mcp_tool
    assert mcp_tools.list_mcp_tools is mcp_registry.list_mcp_tools
    assert mcp_tools.redact_memory_flow is mcp_memories.redact_memory_flow
    assert mcp_tools._TOOL_HANDLERS is mcp_registry._TOOL_HANDLERS
    assert mcp_tools._TOOL_DEFINITIONS_BY_NAME is mcp_registry._TOOL_DEFINITIONS_BY_NAME
    assert len(vars(mcp_tools)) == 439
    assert mcp_tools.__annotations__ == EXPECTED_FACADE_ANNOTATIONS


def test_mcp_carriers_are_complete_bounded_and_do_not_import_onramps() -> None:
    module_paths = sorted(MCP_PACKAGE.glob("*.py"))
    assert {path.name for path in module_paths} == EXPECTED_MODULES
    for path in module_paths:
        assert len(path.read_text(encoding="utf-8").splitlines()) < 4_000, path.name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_modules = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported_modules.update(
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        )
        assert not imported_modules.intersection(
            {
                "alicebot_api.cli",
                "alicebot_api.main",
                "alicebot_api.mcp_tools",
                "alicebot_api.onramp",
            }
        ), path.name


def test_mcp_registry_order_definitions_and_alias_identity_are_frozen() -> None:
    core_definitions = mcp_registry._CORE_TOOL_DEFINITIONS
    legacy_definitions = mcp_registry._LEGACY_TOOL_DEFINITIONS
    handlers = mcp_registry._TOOL_HANDLERS

    assert (len(core_definitions), len(legacy_definitions), len(handlers)) == (11, 65, 76)
    # Moved 2026-08-17 (D7). The tool COUNT is unchanged and asserted above.
    # What moved is two optional properties, domains and sensitivity_allowed,
    # on alice_resume and alice_recent_decisions. Those arguments already
    # existed on the handlers; additionalProperties is false, so the schema
    # had to name them before a caller could pass the fence. No tool added,
    # removed, or renamed.
    # Moved again 2026-09-22 (D8). Counts unchanged, legacy digest and handler
    # map digest unchanged. Per-tool digests against the previous pin differ
    # for alice_memory_commit only: it gained confirmation_id and
    # confirmation_action, dropped the top-level "required" list (a
    # confirmation call carries no title or canonical_text; the handler still
    # requires both for a new write), and its description and the title,
    # canonical_text and rationale descriptions now say how to finish a
    # pending write on the same tool instead of naming alice_memory_manage.
    # Moved once more the same day after review: only alice_memory_commit's
    # description and confirmation_action description, which now say reject
    # is allowed past the sensitivity ceiling and describe expiry as applied
    # on the next confirm or reject rather than in the background.
    # And once more after the second review, wording only: the expiry sentence
    # now says "on this tool" (the review approve path does not read the 24
    # hours), and confirmation_action says a keyless server does not check a
    # declared project_scope.
    # Moved 2026-09-22 (S4.4). alice_memory_commit's source_refs gained
    # maxItems 64 and an item maxLength of 4000, mirroring the service bound
    # (MAX_COMMIT_SOURCE_REFS, MAX_COMMIT_SOURCE_REF_CHARS). No tool added,
    # removed, or renamed.
    # Moved 2026-09-23 (S4.4 round 2, review finding 8). Correction titles
    # gained maxLength 280 and correction body strings maxLength 20000 on
    # alice_memory_correct and alice_review_apply, and alice_commit_captures'
    # candidates gained maxItems 100 (write_bounds). No tool added, removed,
    # or renamed.
    # Moved 2026-09-23 (S4.4 round 2, owner ruling R4). The legacy alias
    # alice_vnext_commit_memory's source_refs gained the same maxItems 64
    # and item maxLength 4000 as alice_memory_commit, both now read from
    # write_bounds. Legacy only; the core digest is unchanged. No tool added,
    # removed, or renamed.
    # Moved 2026-09-23 (Sprint 4 integration, s4-sprint). The S4.3 (D8) and
    # S4.4 pins above were each minted on their own branch from 880915a, so
    # neither holds for the combined tree. Core: alice_memory_commit carries
    # both changes, S4.3's confirmation_id, confirmation_action, dropped
    # "required" list and new descriptions, and S4.4's source_refs maxItems 64
    # and item maxLength 4000; alice_memory_correct carries S4.4's correction
    # bounds. Every other core tool matches 880915a. Legacy: S4.3 changed no
    # legacy tool, so the legacy pin is S4.4's. Checked by comparing each tool
    # of the combined tree against both branches, not only by re-hashing. No
    # tool added, removed, or renamed.
    # Moved 2026-09-23 (Sprint 4 integration follow-up, text only). The combined
    # tree runs S4.4's credential check on S4.3's confirmation route, so
    # alice_memory_commit's description, and its rationale and
    # confirmation_action descriptions, now say that a confirm is refused when
    # the pending text or the rationale carries credential material, and that a
    # reject still completes and stores such a rationale as a fixed placeholder
    # (rationale_withheld). No type, enum, bound or required list moved; no
    # other tool changed; legacy and handler map digests unchanged. No tool
    # added, removed, or renamed.
    assert _digest(core_definitions) == "dbfbf6bbb02d24aca02f94d94aff1fb3970aa6a1c08ecb03a3812ba72f9f58e4"
    assert _digest(legacy_definitions) == "2c21d4d624da448969554137e0b9cbae14c34cfaa0454e76d22ae480a6a29a58"
    ordered_handler_map = [(name, handler.__name__) for name, handler in handlers.items()]
    assert _digest(ordered_handler_map) == "d864c98bb914bbc6ace464fa8020b3ed264f17f2061a6101aae677d801032ae5"
    for first, second in EXPECTED_ALIAS_PAIRS:
        assert handlers[first] is handlers[second]
        assert (
            mcp_registry._TOOL_DEFINITIONS_BY_NAME[first]["inputSchema"]
            != mcp_registry._TOOL_DEFINITIONS_BY_NAME[second]["inputSchema"]
        )


def test_mcp_gates_remain_dynamic_and_agent_keys_suppress_legacy(monkeypatch) -> None:
    for name in (
        mcp_tools.MCP_FULL_TOOLS_ENV,
        mcp_tools.MCP_LEGACY_TOOLS_ENV,
        mcp_tools.LEGACY_SURFACES_ENV,
        mcp_tools.AGENT_API_KEY_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    assert _tool_names() == ["alice_memory_commit", "alice_recall", "alice_resume"]

    monkeypatch.setenv(mcp_tools.MCP_FULL_TOOLS_ENV, "1")
    assert len(_tool_names()) == 11

    monkeypatch.setenv(mcp_tools.MCP_LEGACY_TOOLS_ENV, "1")
    assert len(_tool_names()) == 73
    monkeypatch.setenv(mcp_tools.LEGACY_SURFACES_ENV, "1")
    assert len(_tool_names()) == 76
    monkeypatch.delenv(mcp_tools.MCP_LEGACY_TOOLS_ENV)
    assert len(_tool_names()) == 11
    monkeypatch.setenv(mcp_tools.MCP_LEGACY_TOOLS_ENV, "1")
    monkeypatch.setenv(mcp_tools.AGENT_API_KEY_ENV, " configured ")
    assert len(_tool_names()) == 11

    monkeypatch.delenv(mcp_tools.MCP_FULL_TOOLS_ENV)
    assert _tool_names() == ["alice_memory_commit", "alice_recall", "alice_resume"]


def test_registry_import_does_not_pull_in_application_onramps() -> None:
    source_root = REPO_ROOT / "apps" / "api" / "src"
    command = (
        "import json, sys; import alicebot_api.mcp.registry; "
        "print(json.dumps(sorted(name for name in sys.modules "
        "if name in {'alicebot_api.cli','alicebot_api.main','alicebot_api.onramp','alicebot_api.mcp_tools'})))"
    )
    env = dict(os.environ)
    if env.get("ALICE_TEST_INSTALLED_WHEEL") == "1":
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = str(source_root)
    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == []
