"""Helpers the Codex hook tests share. Nothing here imports a host module.

``commit_fact`` puts one known fact in a vault, so a session brief has a line a
test can look for. ``NUMBER_CASES`` is the table of hooks.json handlers whose
numbers and field spellings decide whether Codex loads the file, shared by the
install test and the real-Codex test that compares the two.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path

_USER_ID = "00000000-0000-0000-0000-000000000001"
_ENV_NAMES = (
    "ALICE_EMBEDDINGS_BASE_URL",
    "ALICE_EMBEDDINGS_MODEL",
    "ALICE_EMBEDDINGS_API_KEY",
    "ALICE_AGENT_API_KEY",
)


def commit_fact(vault: Path, monkeypatch: pytest.MonkeyPatch, title: str, text: str) -> None:
    """Commit ``text`` as a durable fact in ``vault``, through the same door an agent uses."""

    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp_tools import MCPRuntimeContext

    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    database = resolve_db_path(data_dir=str(vault), db=None)
    bootstrap_database(database, user_id=_USER_ID, user_email="local@alice")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=_USER_ID)
    payload = call_mcp_tool(
        context,
        name="alice_memory_commit",
        arguments={
            "title": title,
            "canonical_text": text,
            "memory_type": "decision",
            "domain": "personal",
            "sensitivity": "private",
            "confidence": 0.96,
            "rationale": "User said: remember this",
        },
    )
    assert payload["status"] == "committed", payload


def _mcp(literal: str) -> str:
    return '{"type":"mcp_tool","server":"s","tool":"t","input":{"a":%s}}' % literal


def _mcp_timeout(literal: str, *, server: str = "s", tool: str = "t") -> str:
    return '{"type":"mcp_tool","server":"%s","tool":"%s","timeout":%s}' % (server, tool, literal)


def _command(key: str, literal: str, command: str = "x") -> str:
    return '{"type":"command","command":"%s","%s":%s}' % (command, key, literal)


# (label, handler text, whether Codex uses the file and install accepts it, event the handler sits under).
# The verdicts are those of a Rust judge that copies HooksFile, MatcherGroup and
# HookHandlerConfig from Codex 0.158.0 (config/src/hook_config.rs), reads the text with
# serde_json, and then repeats the step in hooks/src/engine/discovery.rs that hashes each
# loaded handler as TOML for its trust record. That step is an unreachable!, so a handler
# it cannot serialize (a timeout or an additionalContextLimit above 2**63-1) makes Codex
# panic, which the pinned job saw as a dead ``codex app-server``. A refused case is one
# Codex skips or crashes on. The judge is built with serde_json's arbitrary_precision
# feature, as Codex is (codex-exec-server-protocol enables it). A build without it differs
# on the labels marked (plain): it skips an integer in [2**63, 2**64) in an mcp_tool input
# ("u64 value was too large") and one too large for a float ("number out of range"), and it
# skips a -0 timeout or limit. Only test_codex_hook_real_host.py, in the pinned job, runs
# the real Codex over this table.
_BIG = "1" + "0" * 399
NUMBER_CASES: list[tuple[str, str, bool, str]] = [
    ("mcp-int-2^63-1", _mcp(str(2**63 - 1)), True, "Stop"),
    ("mcp-int-2^63 (plain)", _mcp(str(2**63)), True, "Stop"),
    ("mcp-int-2^64-1 (plain)", _mcp(str(2**64 - 1)), True, "Stop"),
    ("mcp-int-2^64", _mcp(str(2**64)), True, "Stop"),
    ("mcp-int-minus-2^63", _mcp(str(-(2**63))), True, "Stop"),
    ("mcp-int-minus-2^63-1", _mcp(str(-(2**63) - 1)), True, "Stop"),
    ("mcp-int-1e30", _mcp(str(10**30)), True, "Stop"),
    ("mcp-int-400-digits (plain)", _mcp(_BIG), True, "Stop"),
    ("mcp-list-holding-2^63 (plain)", _mcp("[[1,%d]]" % 2**63), True, "Stop"),
    ("mcp-nested-holding-minus-2^63-1", _mcp('{"b":{"c":%d}}' % (-(2**63) - 1)), True, "Stop"),
    ("mcp-float", _mcp("1.5"), True, "Stop"),
    ("mcp-null", _mcp("null"), False, "Stop"),
    ("mcp-list-holding-null", _mcp("[1,null]"), False, "Stop"),
    ("timeout-0", _command("timeout", "0"), True, "Stop"),
    ("timeout-2^63-1", _command("timeout", str(2**63 - 1)), True, "Stop"),
    ("timeout-2^63 (crash)", _command("timeout", str(2**63)), False, "Stop"),
    ("timeout-2^64-1 (crash)", _command("timeout", str(2**64 - 1)), False, "Stop"),
    ("timeout-2^64", _command("timeout", str(2**64)), False, "Stop"),
    ("timeout-2^63-on-interrupt", _command("timeout", str(2**63)), True, "Interrupt"),
    ("timeout-2^63-on-session-end", _command("timeout", str(2**63)), True, "SessionEnd"),
    ("timeout-2^63-empty-command", _command("timeout", str(2**63), command=" "), True, "Stop"),
    ("timeout-minus-0 (plain)", _command("timeout", "-0"), True, "Stop"),
    ("timeout-5.0", _command("timeout", "5.0"), False, "Stop"),
    ("timeout-1e2", _command("timeout", "1e2"), False, "Stop"),
    ("timeout-minus-0.0", _command("timeout", "-0.0"), False, "Stop"),
    ("timeout-minus-1", _command("timeout", "-1"), False, "Stop"),
    ("mcp-timeout-2^63-1", _mcp_timeout(str(2**63 - 1)), True, "Stop"),
    ("mcp-timeout-2^63 (crash)", _mcp_timeout(str(2**63)), False, "Stop"),
    ("mcp-timeout-2^63-empty-tool", _mcp_timeout(str(2**63), tool=""), True, "Stop"),
    ("mcp-timeout-2^63-on-interrupt", _mcp_timeout(str(2**63)), True, "Interrupt"),
    ("mcp-timeout-2^63-on-session-end", _mcp_timeout(str(2**63)), True, "SessionEnd"),
    ("limit-minus-0 (plain)", _command("additionalContextLimit", "-0"), True, "Stop"),
    ("limit-2^64", _command("additionalContextLimit", str(2**64)), False, "Stop"),
    ("limit-2^63-on-an-event-that-drops-it", _command("additionalContextLimit", str(2**63)), True, "Stop"),
    ("limit-2^63-1-on-session-start", _command("additionalContextLimit", str(2**63 - 1)), True, "SessionStart"),
    ("limit-2^63-on-session-start (crash)", _command("additionalContextLimit", str(2**63)), False, "SessionStart"),
    ("limit-2^63-on-pre-tool-use (crash)", _command("additionalContextLimit", str(2**63)), False, "PreToolUse"),
    ("limit-2^63-on-post-tool-use (crash)", _command("additionalContextLimit", str(2**63)), False, "PostToolUse"),
    (
        "limit-2^63-on-user-prompt-submit (crash)",
        _command("additionalContextLimit", str(2**63)),
        False,
        "UserPromptSubmit",
    ),
    (
        "limit-2^63-on-subagent-start (crash)",
        _command("additionalContextLimit", str(2**63)),
        False,
        "SubagentStart",
    ),
    ("limit-2^63-on-subagent-stop", _command("additionalContextLimit", str(2**63)), True, "SubagentStop"),
    ("commandWindows-only", _command("commandWindows", '"w"'), True, "Stop"),
    ("command_windows-only", _command("command_windows", '"w"'), True, "Stop"),
    (
        "both-spellings-null",
        '{"type":"command","command":"x","commandWindows":null,"command_windows":null}',
        False,
        "Stop",
    ),
    (
        "both-spellings-strings",
        '{"type":"command","command":"x","commandWindows":"a","command_windows":"b"}',
        False,
        "Stop",
    ),
    (
        "both-spellings-same-string",
        '{"type":"command","command":"x","commandWindows":"a","command_windows":"a"}',
        False,
        "Stop",
    ),
    (
        "both-spellings-null-then-string",
        '{"type":"command","command":"x","commandWindows":null,"command_windows":"b"}',
        False,
        "Stop",
    ),
    (
        "both-spellings-string-then-null",
        '{"type":"command","command":"x","commandWindows":"a","command_windows":null}',
        False,
        "Stop",
    ),
]


def number_case_document(handler: str, event: str = "Stop") -> str:
    """A hooks.json text with ``handler`` under ``event`` and a plain command handler under SessionStart.

    The SessionStart handler is the probe: Codex lists it when it loads the file.
    """

    probe = '{"hooks":[{"type":"command","command":"echo probe-hook"}]}'
    events: dict[str, list[str]] = {"SessionStart": [probe]}
    events.setdefault(event, []).append('{"hooks":[%s]}' % handler)
    body = ",".join('"%s":[%s]' % (name, ",".join(groups)) for name, groups in events.items())
    return '{"hooks":{%s}}\n' % body
