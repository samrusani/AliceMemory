"""alice-memory install --host codex writes a SessionStart hook to hooks.json.

Codex 0.158.0 (``codex-cli 0.158.0``) reads ``<CODEX_HOME>/hooks.json``. Install
appends one group at the end of ``hooks.SessionStart``, replaces its own handler
in place on a re-run, prints ``--format markdown`` in the command, never
writes a ``trusted_hash``, and writes no hook when ``config.toml`` holds TOML
hooks. Each test names the edit that makes it fail. The real-host run that
proves Codex injects the brief is in ``test_codex_hook_real_host.py``.

The imported JSON-mode item below has the shape ``codex`` writes when its
Claude Code import copies install's SessionStart hook: ``serde_json``
``to_string_pretty`` plus a newline (external-agent-migration
``hooks_common.rs`` and ``hooks_cla.rs``), one group with no matcher and one
``{"type", "command"}`` handler.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from alicebot_api import host_install, host_launcher, session_start_hook
from alicebot_api.host_install import host_file_map
from alicebot_api.host_launcher import hook_output_format, split_command
from alicebot_api.onramp import _ERROR_CONTRACTS
from alicebot_api.onramp import main as onramp_main
from tests.unit.codex_hook_helpers import NUMBER_CASES, commit_fact, number_case_document
from tests.unit.launcher_helpers import make_scripts, pin_launcher_search

pytestmark = pytest.mark.usefixtures("uvx_on_path")

TRUST_NEXT = (
    'next: open Codex. At "Hooks need review", choose Review hooks and trust the '
    "alice-memory-session-start hook, or use /hooks. Until then Codex skips it "
    "without a message."
)
MODIFIED = "The hook changed, so Codex will skip it until you trust it again."
WRITTEN = "session_start: written to hooks.json, not trusted yet"
UNCHANGED = (
    "session_start: unchanged in hooks.json (Codex runs it only if you have trusted it)"
)
CHECK_NEXT = "next: check it with: codex mcp get alice"
CANARY = "sk-" + "hookfile" + "canary"

# Exact bytes of the hooks.json Codex's Claude Code import writes for install's hook.
IMPORTED_JSON_MODE = (
    "{\n"
    '  "hooks": {\n'
    '    "SessionStart": [\n'
    "      {\n"
    '        "hooks": [\n'
    "          {\n"
    '            "type": "command",\n'
    '            "command": "uvx --from alice-memory alice-memory-session-start --data-dir /old/vault"\n'
    "          }\n"
    "        ]\n"
    "      }\n"
    "    ]\n"
    "  }\n"
    "}\n"
)


def _install(home: Path, vault: Path | None, capsys, *extra: str) -> tuple[int, str, str]:
    argv = ["install", "--home", str(home), "--host", "codex"]
    if vault is not None:
        argv += ["--data-dir", str(vault)]
    code = onramp_main([*argv, *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _files(home: Path) -> dict[str, Path]:
    return host_file_map(home.resolve())["codex"]


def _config(home: Path) -> Path:
    return _files(home)["mcp"]


def _hooks(home: Path) -> Path:
    return _files(home)["hooks"]


def _seed(path: Path, content: str | bytes | dict | list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, dict | list):
        content = json.dumps(content, indent=2) + "\n"
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _command(vault: Path) -> str:
    return (
        "uvx --from alice-memory alice-memory-session-start --data-dir "
        f"{shlex.quote(str(vault.resolve()))} --format markdown"
    )


def _handler(vault: Path) -> dict:
    return {
        "type": "command",
        "command": _command(vault),
        "timeout": 120,
        "additionalContextLimit": 0,
    }


def _user_group(command: str, **extra: object) -> dict:
    return {**extra, "hooks": [{"type": "command", "command": command}]}


def _backups(vault: Path, name: str) -> list[Path]:
    directory = vault / "backups" / "host-configs"
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"codex-{name}.alice-backup-*"))


def test_codex_hook_new_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A new hooks.json holds one group with the exact handler, private, and the receipt says so.

    Mutations: write ``--format json`` (drop the format), a matcher, or
    another handler key. The exact-equality assertions fail.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    hooks = _hooks(home)
    expected = {"hooks": {"SessionStart": [{"hooks": [_handler(vault)]}]}}
    assert hooks.read_text(encoding="utf-8") == json.dumps(expected, indent=2) + "\n"
    assert stat.S_IMODE(hooks.stat().st_mode) == 0o600
    lines = out.splitlines()
    assert WRITTEN in lines
    assert f"session_start_path: {hooks}" in lines
    assert TRUST_NEXT in lines
    assert lines[-1] == CHECK_NEXT
    assert MODIFIED not in out
    assert "note: Run alice-memory brief" not in out
    assert not _backups(vault, "hooks.json")


def test_codex_hook_merges_with_siblings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Other events, other groups and the description stay exactly as they were.

    Mutation: rebuild the document from Alice's group alone. This test fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    before = {
        "description": "my hooks",
        "hooks": {
            "PreToolUse": [_user_group("echo pre", matcher="shell")],
            "SessionStart": [_user_group("echo one", matcher="startup"), _user_group("echo two")],
            "Stop": [_user_group("echo stop")],
        },
    }
    _seed(_hooks(home), before)
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    after = _read(_hooks(home))
    assert list(after) == ["description", "hooks"]
    assert after["description"] == "my hooks"
    assert after["hooks"]["PreToolUse"] == before["hooks"]["PreToolUse"]
    assert after["hooks"]["Stop"] == before["hooks"]["Stop"]
    assert after["hooks"]["SessionStart"][:2] == before["hooks"]["SessionStart"]
    assert after["hooks"]["SessionStart"][2] == {"hooks": [_handler(vault)]}
    assert len(after["hooks"]["SessionStart"]) == 3
    assert WRITTEN in out.splitlines()


def test_codex_hook_is_appended_after_existing_groups(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Codex keys trust by group index, so Alice's group goes last and theirs keep their indexes.

    Mutation: insert the group at index 0. The user's groups move and the
    index assertions fail. Mutation: drop a user group. This test fails too.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    groups = [_user_group(f"echo user-{index}") for index in range(3)]
    _seed(_hooks(home), {"hooks": {"SessionStart": groups}})
    code, _out, err = _install(home, vault, capsys)
    assert code == 0, err
    written = _read(_hooks(home))["hooks"]["SessionStart"]
    assert written[:3] == groups
    assert written[3] == {"hooks": [_handler(vault)]}
    assert len(written) == 4
    for index, group in enumerate(groups):
        assert written[index]["hooks"][0]["command"] == f"echo user-{index}"


def test_codex_hook_rerun_is_unchanged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A re-run leaves both files as they were, prints the unchanged wording, and makes no backup.

    Mutation: rewrite hooks.json on every run. The backup assertion fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    _install(home, vault, capsys)
    hooks_before = _hooks(home).read_bytes()
    config_before = _config(home).read_bytes()
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert _hooks(home).read_bytes() == hooks_before
    assert _config(home).read_bytes() == config_before
    lines = out.splitlines()
    assert UNCHANGED in lines
    assert "action: unchanged" in lines
    assert TRUST_NEXT not in lines
    assert MODIFIED not in out
    assert lines[-1] == CHECK_NEXT
    assert not _backups(vault, "hooks.json")


def test_codex_hook_rerun_replaces_in_place(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A changed data dir replaces Alice's handler at its own group and handler index.

    Alice's handler shares a group with a user handler, and a user group
    follows. The group's matcher stays, the receipt says the hook changed,
    and the old file is backed up in the new vault. Mutation: append a second
    Alice group instead of replacing, or move the handler. This test fails.
    Mutation: leave the ``session_start_backup:`` line out of the receipt. The
    ``session_start_backup`` assertion fails.
    """

    home = tmp_path / "home"
    first, second = tmp_path / "vault-one", tmp_path / "vault-two"
    _install(home, first, capsys)
    document = _read(_hooks(home))
    alice = document["hooks"]["SessionStart"].pop()
    document["hooks"]["SessionStart"] = [
        _user_group("echo before"),
        {"matcher": "startup", "hooks": [{"type": "command", "command": "echo mine"}, alice["hooks"][0]]},
        _user_group("echo after"),
    ]
    _seed(_hooks(home), document)
    seeded = _hooks(home).read_bytes()

    code, out, err = _install(home, second, capsys)
    assert code == 0, err
    groups = _read(_hooks(home))["hooks"]["SessionStart"]
    assert len(groups) == 3
    assert groups[0] == _user_group("echo before")
    assert groups[2] == _user_group("echo after")
    assert groups[1]["matcher"] == "startup"
    assert groups[1]["hooks"][0] == {"type": "command", "command": "echo mine"}
    assert groups[1]["hooks"][1] == _handler(second)
    lines = out.splitlines()
    assert WRITTEN in lines and MODIFIED in lines and TRUST_NEXT in lines
    backups = _backups(second, "hooks.json")
    assert len(backups) == 1
    assert backups[0].read_bytes() == seeded
    assert f"session_start_backup: {backups[0]}" in lines


def test_codex_hook_replaces_the_imported_json_mode_item(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The item Codex's import copies is replaced in place, not left beside a second one.

    The import writes the flat JSON-mode command with no format, timeout or
    limit. Install replaces the whole handler, so a copied ``timeout`` and
    ``statusMessage`` do not survive, and the item keeps its index behind a
    user group. Mutation: leave the imported item and append another. The
    length assertion fails. Mutation: keep the imported handler's extra keys.
    The equality assertion fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    seeded = _seed(_hooks(home), IMPORTED_JSON_MODE)
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    groups = _read(_hooks(home))["hooks"]["SessionStart"]
    assert groups == [{"hooks": [_handler(vault)]}]
    lines = out.splitlines()
    assert WRITTEN in lines and MODIFIED in lines
    backups = _backups(vault, "hooks.json")
    assert [path.read_bytes() for path in backups] == [IMPORTED_JSON_MODE.encode()]
    assert seeded.exists()

    home_two = tmp_path / "home-two"
    imported = json.loads(IMPORTED_JSON_MODE)
    imported_handler = imported["hooks"]["SessionStart"][0]["hooks"][0]
    imported_handler["timeout"] = 30
    imported_handler["statusMessage"] = "Loading Alice"
    imported["hooks"]["SessionStart"].insert(0, _user_group("echo mine"))
    _seed(_hooks(home_two), json.dumps(imported, indent=2) + "\n")
    code, out, err = _install(home_two, vault, capsys)
    assert code == 0, err
    groups = _read(_hooks(home_two))["hooks"]["SessionStart"]
    assert groups == [_user_group("echo mine"), {"hooks": [_handler(vault)]}]


def test_codex_second_alice_item_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two Alice handlers in hooks.SessionStart refuse the host, and nothing is written.

    Mutation: replace the first and ignore the second. This test fails.
    """

    alice = "uvx --from alice-memory alice-memory-session-start --data-dir /v"
    shapes = {
        "one-group": {"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": alice},
            {"type": "command", "command": alice},
        ]}]}},
        "two-groups": {"hooks": {"SessionStart": [_user_group(alice), _user_group(alice)]}},
    }
    for label, document in shapes.items():
        home = tmp_path / label
        seeded = _seed(_hooks(home), document)
        before = seeded.read_bytes()
        code, out, err = _install(home, tmp_path / f"vault-{label}", capsys)
        assert code == 1, (label, out, err)
        assert seeded.read_bytes() == before
        assert not _config(home).exists()
        assert not _backups(tmp_path / f"vault-{label}", "hooks.json")
        assert "more than one alice-memory-session-start hook" in out
        assert "action: refused" in out
        assert "next: nothing was written for codex. Fix hooks.json, then run install again." in out

    other = tmp_path / "other-event"
    _seed(_hooks(other), {"hooks": {"Stop": [_user_group(alice)], "SessionStart": [_user_group(alice)]}})
    code, out, err = _install(other, tmp_path / "vault-other", capsys)
    assert code == 0, (out, err)


_CANARY_URL = f"https://user:{CANARY}@h.example/x"
_INVALID_HOOKS = {
    "top-level-key": {CANARY: 1, "hooks": {}},
    "hooks-not-object": {"hooks": [CANARY]},
    "description-not-string": {"description": 5, "hooks": {}},
    "event-not-list": {"hooks": {"SessionStart": {CANARY: 1}}},
    "group-not-object": {"hooks": {"Stop": [CANARY]}},
    "group-hooks-not-list": {"hooks": {"SessionStart": [{"hooks": CANARY}]}},
    "group-hooks-null": {"hooks": {"SessionStart": [{"hooks": None}]}},
    "handler-not-object": {"hooks": {"SessionStart": [{"hooks": [CANARY]}]}},
    "handler-without-type": {"hooks": {"SessionStart": [{"hooks": [{"command": CANARY}]}]}},
    "handler-type-http": {
        "hooks": {"SessionStart": [{"hooks": [{"type": "http", "url": _CANARY_URL}]}]}
    },
    "handler-type-not-string": {"hooks": {"Stop": [{"hooks": [{"type": 7, "command": CANARY}]}]}},
    "command-handler-without-command": {
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "timeout": 5}]}]}
    },
    "command-not-string": {
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": ["a", CANARY]}]}]}
    },
    "command-windows-not-string": {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x", "commandWindows": 1}]}]}
    },
    "status-message-not-string": {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x", "statusMessage": [CANARY]}]}]}
    },
    "timeout-float": {
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "x", "timeout": 5.5}]}]}
    },
    "timeout-integral-float": {
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "x", "timeout": 5.0}]}]}
    },
    "timeout-negative": {
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "x", "timeout": -1}]}]}
    },
    "timeout-bool": {
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "x", "timeout": True}]}]}
    },
    "timeout-too-large": {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x", "timeout": 2**64}]}]}
    },
    "limit-string": {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x", "additionalContextLimit": CANARY}]}]}
    },
    "limit-negative": {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x", "additionalContextLimit": -1}]}]}
    },
    "async-string": {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x", "async": "yes"}]}]}
    },
    "matcher-numeric": {"hooks": {"SessionStart": [{"matcher": 3, "hooks": []}]}},
    "matcher-list": {"hooks": {"PreToolUse": [{"matcher": [CANARY], "hooks": []}]}},
    "mcp-tool-without-server": {
        "hooks": {"Stop": [{"hooks": [{"type": "mcp_tool", "tool": "t"}]}]}
    },
    "mcp-tool-input-null": {
        "hooks": {"Stop": [{"hooks": [{"type": "mcp_tool", "server": "s", "tool": "t", "input": {"a": None}}]}]}
    },
    "mcp-tool-input-not-object": {
        "hooks": {"Stop": [{"hooks": [{"type": "mcp_tool", "server": "s", "tool": "t", "input": [CANARY]}]}]}
    },
    "mcp-tool-timeout-float": {
        "hooks": {"Stop": [{"hooks": [{"type": "mcp_tool", "server": "s", "tool": "t", "timeout": 1.5}]}]}
    },
    "command-windows-snake-case-not-string": {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x", "command_windows": 1}]}]}
    },
    "command-windows-both-spellings-null": {
        "hooks": {"Stop": [{"hooks": [{
            "type": "command", "command": "x", "commandWindows": None, "command_windows": None,
        }]}]}
    },
    "command-windows-both-spellings-strings": {
        "hooks": {"Stop": [{"hooks": [{
            "type": "command", "command": "x", "commandWindows": "a", "command_windows": "b",
        }]}]}
    },
    "command-windows-both-spellings-mixed": {
        "hooks": {"Stop": [{"hooks": [{
            "type": "command", "command": "x", "commandWindows": None, "command_windows": "b",
        }]}]}
    },
    "mcp-tool-status-message-not-string": {
        "hooks": {"Stop": [{"hooks": [{"type": "mcp_tool", "server": "s", "tool": "t", "statusMessage": 7}]}]}
    },
    "mcp-tool-input-list-holding-null": {
        "hooks": {"Stop": [{"hooks": [{"type": "mcp_tool", "server": "s", "tool": "t", "input": {"a": [1, None]}}]}]}
    },
}


@pytest.mark.parametrize("label", sorted(_INVALID_HOOKS))
def test_codex_hooks_file_codex_would_skip_is_refused(
    label: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Codex skips the whole hooks.json on any type error, so install refuses to add to it.

    Each case is one rule from Codex's ``HookHandlerConfig``. The host is
    refused, nothing is written for it (config.toml included), no backup is
    made, and the reason quotes no value from the file. Mutation: accept an
    ``http`` handler, or drop any one check. The matching case fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    seeded = _seed(_hooks(home), _INVALID_HOOKS[label])
    before = seeded.read_bytes()
    code, out, err = _install(home, vault, capsys)
    assert code == 1, (label, out, err)
    assert seeded.read_bytes() == before
    assert not _config(home).exists()
    assert not vault.exists()
    lines = out.splitlines()
    assert "action: refused" in lines
    assert any(
        line.startswith("reason: Codex would skip this hooks.json: ") for line in lines
    ), out
    assert "next: nothing was written for codex. Fix hooks.json, then run install again." in lines
    assert "session_start: none" in lines
    assert CANARY not in out + err


def test_codex_hooks_file_dry_run_refuses_the_same_way(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dry run reports the refusal, exits 1, and writes nothing.

    Mutation: skip the hooks.json check in a dry run. This test fails.
    """

    home = tmp_path / "home"
    seeded = _seed(_hooks(home), _INVALID_HOOKS["handler-type-http"])
    before = seeded.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys, "--dry-run")
    assert code == 1, (out, err)
    assert "action: would-refuse" in out.splitlines()
    assert "dry run: install would refuse this file; nothing was attempted" in out
    assert seeded.read_bytes() == before
    assert not _config(home).exists()


_ACCEPTED_HOOKS = {
    "matcher-null": {"hooks": {"Stop": [{"matcher": None, "hooks": [{"type": "command", "command": "x"}]}]}},
    "timeout-null": {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x", "timeout": None}]}]}
    },
    "unknown-event": {"hooks": {"FutureEvent": ["anything", 1]}},
    "prompt-handler": {"hooks": {"Stop": [{"hooks": [{"type": "prompt", "text": "hi"}]}]}},
    "agent-handler": {"hooks": {"Stop": [{"hooks": [{"type": "agent"}]}]}},
    "unknown-handler-key": {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x", "shell": "bash"}]}]}
    },
    "group-without-hooks": {"hooks": {"Stop": [{"matcher": "x"}]}},
    "description-null": {"description": None, "hooks": {}},
    "only-description": {"description": "just this"},
    "mcp-tool": {
        "hooks": {"Stop": [{"hooks": [{"type": "mcp_tool", "server": "s", "tool": "t", "input": {"a": [1, "b"]}}]}]}
    },
    "mcp-tool-input-i64-bounds": {
        "hooks": {"Stop": [{"hooks": [{
            "type": "mcp_tool", "server": "s", "tool": "t",
            "input": {"low": -(2**63), "high": 2**63 - 1, "nested": [{"n": 2**63 - 1}]},
        }]}]}
    },
    "mcp-tool-input-integer-above-i64": {
        "hooks": {"Stop": [{"hooks": [{"type": "mcp_tool", "server": "s", "tool": "t", "input": {"a": 2**63}}]}]}
    },
    "mcp-tool-input-integer-below-i64": {
        "hooks": {"Stop": [{"hooks": [{"type": "mcp_tool", "server": "s", "tool": "t", "input": {"a": -(2**63) - 1}}]}]}
    },
    "mcp-tool-input-list-holding-huge-integer": {
        "hooks": {"Stop": [{"hooks": [{"type": "mcp_tool", "server": "s", "tool": "t", "input": {"a": [{"b": 2**64}]}}]}]}
    },
    "command-windows-one-spelling": {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x", "commandWindows": None}]}]}
    },
    "character-outside-the-bmp": {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo \U0001f600"}]}]}
    },
}


@pytest.mark.parametrize("label", sorted(_ACCEPTED_HOOKS))
def test_codex_hooks_file_codex_loads_is_accepted(
    label: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A field typed as an option accepts null, and Codex ignores what it does not read.

    Refusing these would say Codex skips a file it loads. Mutation: require a
    matcher string, or reject an unknown event. The matching case fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    document = _ACCEPTED_HOOKS[label]
    _seed(_hooks(home), document)
    code, out, err = _install(home, vault, capsys)
    assert code == 0, (label, out, err)
    written = _read(_hooks(home))
    assert written["hooks"]["SessionStart"] == [{"hooks": [_handler(vault)]}]
    for key, value in document.get("hooks", {}).items():
        assert written["hooks"][key] == value
    if "description" in document:
        assert written["description"] == document["description"]


@pytest.mark.parametrize(
    ("label", "handler", "accepted", "event"), NUMBER_CASES, ids=[case[0] for case in NUMBER_CASES]
)
def test_codex_hooks_file_number_and_spelling_rules_match_the_serde_judge(
    label: str,
    handler: str,
    accepted: bool,
    event: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Install agrees with Codex on which numbers and field spellings make it skip hooks.json.

    ``NUMBER_CASES`` holds each verdict from a Rust judge built from Codex
    0.158.0's own ``HookHandlerConfig``, serde_json, and the step that hashes
    each handler as TOML (see the table's comment for the build, and for the
    labels a plain serde_json build would call differently). An ``mcp_tool``
    input refuses only a null: integers at and beyond the i64 and u64 limits
    load. A ``timeout`` must be a whole number from 0 to 2**63-1, because
    Codex reads a u64 and then panics hashing one above i64, except where
    it hashes none (a clamped event, an empty command, an empty ``mcp_tool``
    server or tool); ``additionalContextLimit`` follows the same rule on the
    five events that keep it. A handler that spells both ``commandWindows``
    and ``command_windows`` is skipped, null included. An accepted file gets
    Alice's group appended after the user's, and their handler is kept as
    written. A refused one is left as it was and nothing else is written.
    Mutations: restore the i64 range check in ``_codex_toml_representable``
    (every integer case above 2**63-1 or below -2**63 fails); refuse only
    [2**63, 2**64) there (the ``2^63`` and ``2^64-1`` cases fail); drop the
    both-spellings check (the five ``both-spellings`` cases fail); accept a
    ``timeout`` up to 2**64-1 again (the ``crash`` cases fail); hash a
    timeout on a clamped event, on an empty command or on an empty tool (the
    matching accepted case fails); apply ``additionalContextLimit`` to every
    event (``limit-2^63-on-an-event-that-drops-it`` fails).
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    seeded = _seed(_hooks(home), number_case_document(handler, event))
    before = seeded.read_bytes()
    code, out, err = _install(home, vault, capsys)
    if not accepted:
        assert code == 1, (label, out, err)
        assert seeded.read_bytes() == before
        assert not _config(home).exists() and not vault.exists()
        lines = out.splitlines()
        assert "action: refused" in lines
        assert any(
            line.startswith("reason: Codex would skip this hooks.json: ") for line in lines
        ), out
        assert "session_start: none" in lines
        return
    assert code == 0, (label, out, err)
    written = _read(_hooks(home))["hooks"]
    probe = {"hooks": [{"type": "command", "command": "echo probe-hook"}]}
    case = {"hooks": [json.loads(handler)]}
    alice = {"hooks": [_handler(vault)]}
    if event == "SessionStart":
        assert written["SessionStart"] == [probe, case, alice]
    else:
        assert written[event] == [case]
        assert written["SessionStart"] == [probe, alice]


@pytest.mark.parametrize(
    "raw",
    [
        b"\xef\xbb\xbf{}",
        b'{"hooks": {}, "hooks": {}}',
        b'{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x", "timeout": NaN}]}]}}',
        b"[]",
        b"not json",
        b"\xff\xfe",
        b'{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x", "timeout": 1e999}]}]}}',
        b'{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "\\ud800"}]}]}}',
        b'{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "\\udc00"}]}]}}',
        b'{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "\\ud83d x"}]}]}}',
        b'{"\\ud800": {}}',
    ],
    ids=[
        "bom",
        "duplicate-key",
        "nan",
        "top-level-list",
        "not-json",
        "not-utf8",
        "infinity",
        "lone-leading-surrogate",
        "lone-trailing-surrogate",
        "leading-surrogate-then-text",
        "lone-surrogate-in-a-key",
    ],
)
def test_codex_hooks_file_must_be_strict_json(
    raw: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A BOM, a duplicate key, NaN, a non-object top level, non-JSON and a lone surrogate are refused.

    serde_json, which Codex reads with, rejects a lone surrogate escape that
    Python turns into a string, so Codex would skip such a file. Mutation:
    parse with plain ``json.loads``. The duplicate key, NaN and BOM cases
    fail. Mutation: drop the UTF-8 encode check in ``_strict_hooks_json``, or
    run it with ``ensure_ascii=True`` so nothing can fail to encode. The
    surrogate cases fail.
    """

    home = tmp_path / "home"
    seeded = _seed(_hooks(home), raw)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (out, err)
    assert seeded.read_bytes() == raw
    assert not _config(home).exists()
    assert "reason: hooks.json is not strict JSON" in out


@pytest.mark.parametrize("raw", [b"", b"  \n"])
def test_codex_hooks_file_blank_counts_as_new(
    raw: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty hooks.json holds nothing of the user's, so install writes the hook into it.

    Mutation: refuse an empty file as invalid JSON. This test fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    _seed(_hooks(home), raw)
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert _read(_hooks(home)) == {"hooks": {"SessionStart": [{"hooks": [_handler(vault)]}]}}
    assert WRITTEN in out.splitlines()
    assert [path.read_bytes() for path in _backups(vault, "hooks.json")] == [raw]


@pytest.mark.parametrize("kind", ["list", "dict"])
@pytest.mark.parametrize("depth", [100_000, 5_000])
@pytest.mark.parametrize("dry_run", [False, True], ids=["written", "dry-run"])
def test_codex_deeply_nested_hooks_file_is_refused_not_a_crash(
    kind: str, depth: int, dry_run: bool, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hooks.json nested too deep is a refusal, not ``unexpected RecursionError``.

    At 100,000 levels ``json.loads`` raises RecursionError. At 5,000 levels
    Python 3.12 parses a dict and a later walk overflows instead. Mutation:
    drop RecursionError from the catch around the hooks planning in
    ``_install_codex_host``. The dict cases fail.
    """

    home = tmp_path / "home"
    deep = "[" * depth + "]" * depth if kind == "list" else '{"a":' * depth + "1" + "}" * depth
    seeded = _seed(_hooks(home), deep)
    before = seeded.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys, *(("--dry-run",) if dry_run else ()))
    lines = out.splitlines()
    assert "reason: unexpected RecursionError" not in lines, (out, err)
    assert code == 1
    assert seeded.read_bytes() == before
    assert not _config(home).exists()
    assert ("action: would-refuse" if dry_run else "action: refused") in lines


def test_codex_hooks_file_refusal_comes_after_a_config_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A config.toml that install refuses leaves hooks.json alone, even an imported JSON-mode item.

    Mutation: plan the hook before config.toml, or write it on a refused
    host. The bytes of hooks.json change and this test fails.
    """

    home = tmp_path / "home"
    hooks = _seed(_hooks(home), IMPORTED_JSON_MODE)
    config = _seed(
        _config(home),
        '[mcp_servers.alice]\ncommand = "uvx"\nargs = ["alice-memory", "mcp"]\ncwd = "/tmp/work"\n',
    )
    hooks_before, config_before = hooks.read_bytes(), config.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (out, err)
    assert hooks.read_bytes() == hooks_before
    assert config.read_bytes() == config_before
    assert "holds cwd" in out
    assert "session_start: none" in out.splitlines()
    assert "session_start_path:" not in out
    assert not _backups(tmp_path / "vault", "hooks.json")


def _toml_hook(event: str = "SessionStart") -> str:
    return (
        f"[[hooks.{event}]]\n"
        'matcher = "startup"\n'
        "\n"
        f"[[hooks.{event}.hooks]]\n"
        'type = "command"\n'
        'command = "echo from-toml"\n'
    )


def test_codex_toml_hooks_get_no_hook_and_a_snippet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With hooks in config.toml install writes the MCP entry, no hook, and prints the TOML hook.

    Two SessionStart sources would inject the brief twice. The receipt is a
    refusal for the hook (exit 1) with the handler as ``[[hooks.SessionStart]]``
    TOML, ``--format markdown`` in its command, and a next line. hooks.json is
    not created, so the receipt names no hooks.json path. Mutation: write the
    hook into config.toml, or into hooks.json anyway. This test fails.
    Mutation: name hooks.json (``hooks_named = True``) on this path. The
    ``session_start_path`` assertion fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    seed = "# mine\n" + _toml_hook()
    config = _seed(_config(home), seed)
    code, out, err = _install(home, vault, capsys)
    assert code == 1, (out, err)
    written = config.read_text(encoding="utf-8")
    assert written.startswith(seed)
    assert "[mcp_servers.alice]" in written
    assert written.count("[[hooks.SessionStart]]") == 1
    assert "alice-memory-session-start" not in written
    assert not _hooks(home).exists()
    lines = out.splitlines()
    assert "session_start: refused" in lines
    assert any(line.startswith("session_start_reason: config.toml already holds hooks") for line in lines)
    assert "action: written" in lines
    assert "session_start_path:" not in out
    snippet = out.split("snippet:", 1)[1]
    assert "[[hooks.SessionStart]]" in snippet and "[[hooks.SessionStart.hooks]]" in snippet
    assert 'type = "command"' in snippet
    assert "--format markdown" in snippet
    assert "timeout = 120" in snippet and "additionalContextLimit = 0" in snippet
    assert any(line.startswith("next: install did not write hooks.json") for line in lines)
    assert lines[-1] == CHECK_NEXT
    assert TRUST_NEXT not in lines
    # And the snippet is TOML Codex can read.
    parsed = tomllib.loads(snippet.split("\nnext:", 1)[0])
    handler = parsed["hooks"]["SessionStart"][0]["hooks"][0]
    assert handler["timeout"] == 120 and handler["additionalContextLimit"] == 0
    assert handler["command"].endswith("--format markdown")


@pytest.mark.parametrize("event", list(host_install._CODEX_HOOK_EVENTS))
def test_codex_toml_hooks_rule_counts_each_of_the_twelve_event_keys(
    event: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-empty array under any of the 12 event keys is TOML hooks; an empty one is not.

    Mutation: look at SessionStart only. Every other event case fails.
    """

    home = tmp_path / "home"
    _seed(_config(home), _toml_hook(event))
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (event, out, err)
    assert not _hooks(home).exists()
    assert "session_start: refused" in out.splitlines()

    empty_home = tmp_path / "empty"
    _seed(_config(empty_home), f"[hooks]\n{event} = []\n")
    code, out, err = _install(empty_home, tmp_path / "vault-empty", capsys)
    assert code == 0, (event, out, err)
    assert _hooks(empty_home).is_file()


@pytest.mark.parametrize(
    "body",
    [
        '[hooks.foo]\nx = 1\n',
        "[hooks]\nSessionStart = []\n",
        '[hooks.state."/x/hooks.json:session_start:0:0"]\ntrusted_hash = "sha256:aa"\n',
        "[features]\nhooks = true\n",
    ],
    ids=["unknown-key", "empty-array", "state-only", "no-hooks"],
)
def test_codex_toml_hooks_rule_accepts_the_inert_shapes(
    body: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``hooks.foo``, an empty ``SessionStart`` and ``hooks.state`` alone are not TOML hooks.

    Mutation: treat any ``hooks`` table as TOML hooks. This test fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    config = _seed(_config(home), body)
    code, out, err = _install(home, vault, capsys)
    assert code == 0, (out, err)
    assert _read(_hooks(home)) == {"hooks": {"SessionStart": [{"hooks": [_handler(vault)]}]}}
    assert config.read_text(encoding="utf-8").startswith(body)


def test_codex_hook_never_writes_trust(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Install writes no ``trusted_hash`` and no ``hooks.state``, and keeps the user's.

    A hook Codex has not been told to trust is skipped until the user reviews
    it, which is the review Codex added so that no program can quietly add a
    command that runs on every session. Mutation: write a ``trusted_hash``
    for Alice's key into config.toml or hooks.json. This test fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    code, _out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert "trusted_hash" not in _config(home).read_text(encoding="utf-8")
    assert "trusted_hash" not in _hooks(home).read_text(encoding="utf-8")
    assert "hooks" not in tomllib.loads(_config(home).read_text(encoding="utf-8"))

    other = tmp_path / "other"
    key = f"{_hooks(other.resolve())}:session_start:0:0"
    state = f'[hooks.state."{key}"]\ntrusted_hash = "sha256:' + "ab" * 32 + '"\n'
    _seed(_hooks(other), {"hooks": {"SessionStart": [_user_group("echo mine")]}})
    config = _seed(_config(other), state)
    code, _out, err = _install(other, vault, capsys)
    assert code == 0, err
    parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    assert list(parsed["hooks"]["state"]) == [key]
    assert config.read_text(encoding="utf-8").count("trusted_hash") == 1
    assert "trusted_hash" not in _hooks(other).read_text(encoding="utf-8")
    assert config.read_text(encoding="utf-8").startswith(state)


def test_codex_hook_has_no_context_limit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The handler's keys are spelled exactly, ``additionalContextLimit`` is 0, and there is no matcher.

    ``0`` means Codex never spills the brief to a file. Any other number has
    to track the budget and the frame, and a real one-line brief at the
    budget measures 16,010 by Codex's count, so a fixed limit would cut it.
    Mutation: write a limit other than 0, drop the key, or spell it
    ``additional_context_limit``. This test fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    code, _out, err = _install(home, vault, capsys)
    assert code == 0, err
    group = _read(_hooks(home))["hooks"]["SessionStart"][-1]
    assert set(group) == {"hooks"}
    handler = group["hooks"][0]
    assert set(handler) == {"type", "command", "timeout", "additionalContextLimit"}
    assert handler["additionalContextLimit"] == 0
    assert type(handler["additionalContextLimit"]) is int
    assert handler["timeout"] == 120 and type(handler["timeout"]) is int
    assert handler["type"] == "command"


def test_codex_hook_command_carries_format_markdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every written and printed command ends in ``--format markdown``, and the dry run hides no word.

    Codex rejects the JSON Cursor and Claude Code read, and injects nothing.
    Mutation: build the command without ``output_format``, or hide
    ``--format`` in ``shown_hook_words``. This test fails. A dry run writes
    nothing, so it must not ask the user to trust a hook that is not there.
    Mutation: print the trust line on a dry run (``if hook_written:``). The
    ``TRUST_NEXT`` assertion fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    code, out, err = _install(home, vault, capsys, "--dry-run")
    assert code == 0, err
    assert not _hooks(home).exists()
    hook_part = out.split("\n---\n", 1)[1]
    shown = json.loads(hook_part.split("\nnext:", 1)[0].split("\nhidden:", 1)[0])
    command = shown["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command == _command(vault)
    assert "<hidden>" not in hook_part
    assert shown["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] == 120
    assert shown["hooks"]["SessionStart"][0]["hooks"][0]["additionalContextLimit"] == 0
    assert "session_start: planned (written to hooks.json, not trusted yet)" in out.splitlines()
    assert TRUST_NEXT not in out.splitlines()
    assert "hidden:" not in out

    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    written = _read(_hooks(home))["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert hook_output_format(written) == "markdown"
    assert split_command(written)[-2:] == ["--format", "markdown"]


def test_codex_hook_receipt_wordings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """New, changed and unchanged each print their own lines, and only a change asks for trust.

    Mutation: print the trust line on an unchanged run, drop the Modified
    line, or print it for a new hook. This test fails.
    """

    home = tmp_path / "home"
    first, second = tmp_path / "vault-one", tmp_path / "vault-two"
    _, new_out, _ = _install(home, first, capsys)
    _, same_out, _ = _install(home, first, capsys)
    _, changed_out, _ = _install(home, second, capsys)
    new_lines, same_lines, changed_lines = (
        new_out.splitlines(),
        same_out.splitlines(),
        changed_out.splitlines(),
    )
    assert WRITTEN in new_lines and TRUST_NEXT in new_lines and MODIFIED not in new_lines
    assert UNCHANGED in same_lines and TRUST_NEXT not in same_lines and MODIFIED not in same_lines
    assert WRITTEN in changed_lines and MODIFIED in changed_lines and TRUST_NEXT in changed_lines
    for lines in (new_lines, same_lines, changed_lines):
        assert lines[-1] == CHECK_NEXT
        assert "note: Run alice-memory brief or alice-memory-session-start --format markdown" not in lines


@pytest.mark.parametrize(
    ("body", "noted"),
    [
        ("[features]\nhooks = false\n", True),
        ("[features]\nhooks = true\n", False),
        ("[features]\ncodex_hooks = false\n", True),
        ("[features]\nhooks = true\ncodex_hooks = false\n", False),
        ("[features]\nother = false\n", False),
        ("", False),
    ],
    ids=["off", "on", "old-name-off", "new-name-wins", "other-feature", "absent"],
)
def test_codex_hooks_feature_off_is_a_note(
    body: str, noted: bool, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``features.hooks = false`` gets a note that Codex will run no hook; anything else does not.

    Mutation: never print it, or print it for ``hooks = true``. This test fails.
    """

    home = tmp_path / "home"
    _seed(_config(home), body)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, err
    note = "note: features.hooks is false in config.toml, so Codex will not run any hook"
    assert (note in out.splitlines()) is noted
    assert _hooks(home).is_file()


def test_codex_hook_follows_the_entry_data_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without ``--data-dir`` the hook reads the vault the existing alice entry opens.

    Mutation: build the hook on ~/.alice. This test fails.
    """

    home = tmp_path / "home"
    vault = (tmp_path / "mine").resolve()
    _seed(
        _config(home),
        f'[mcp_servers.alice]\ncommand = "uvx"\nargs = ["alice-memory", "mcp", "--data-dir", "{vault}"]\n',
    )
    code, out, err = _install(home, None, capsys)
    assert code == 0, (out, err)
    handler = _read(_hooks(home))["hooks"]["SessionStart"][0]["hooks"][0]
    assert handler == _handler(vault)


def test_codex_hook_uses_the_script_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A script launcher runs its sibling ``alice-memory-session-start``, and needs it to count alive.

    Codex now runs a hook, so a script launcher without its pair is not a
    working launcher (``needs_hook``). Mutation: keep ``needs_hook=False`` for
    Codex. The second half fails.
    """

    scripts = make_scripts(tmp_path / "scripts", session_start=True)
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=scripts)
    home, vault = tmp_path / "home", tmp_path / "vault"
    code, out, err = _install(home, vault, capsys)
    assert code == 0, (out, err)
    command = _read(_hooks(home))["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command == (
        f"{scripts / 'alice-memory-session-start'} --data-dir {shlex.quote(str(vault.resolve()))} "
        "--format markdown"
    )
    entry = tomllib.loads(_config(home).read_text(encoding="utf-8"))["mcp_servers"]["alice"]
    assert entry["command"] == str(scripts / "alice-memory")

    lonely = make_scripts(tmp_path / "lonely", session_start=False)
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=lonely)
    other_home = tmp_path / "other-home"
    _seed(
        _config(other_home),
        "[mcp_servers.alice]\n"
        f'command = "{lonely / "alice-memory"}"\n'
        'args = ["mcp", "--data-dir", "/old"]\n',
    )
    code, out, err = _install(other_home, tmp_path / "vault-two", capsys)
    assert "alice-memory-session-start is missing or not executable" in out, out


def test_codex_hook_keeps_a_kept_command_only_when_it_prints_markdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hook install must keep still has to print what Codex reads.

    The entry's uvx options include ``--with``, which install does not carry
    into a hook, so it writes no new hook command and keeps the existing one.
    A kept JSON-mode command would inject nothing, so install refuses to
    keep it, prints the argv to add, and leaves hooks.json alone. A kept
    markdown command is accepted with Alice's timeout and limit. Mutation:
    drop the format check on a kept command. The first half fails.
    """

    vault = (tmp_path / "vault").resolve()
    entry = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        f'args = ["--with", "requests", "alice-memory", "mcp", "--data-dir", "{vault}"]\n'
    )
    json_mode = f"uvx --from alice-memory alice-memory-session-start --data-dir {vault}"
    home = tmp_path / "home"
    _seed(_config(home), entry)
    hooks = _seed(_hooks(home), {"hooks": {"SessionStart": [_user_group(json_mode)]}})
    before = hooks.read_bytes()
    code, out, err = _install(home, None, capsys)
    assert code == 1, (out, err)
    assert hooks.read_bytes() == before
    lines = out.splitlines()
    assert "session_start: refused" in lines
    assert any(
        line.startswith("session_start_reason: the SessionStart hook would keep printing")
        for line in lines
    )
    argv_lines = [line for line in lines if line.startswith("session_start_argv: ")]
    assert argv_lines
    assert json.loads(argv_lines[0].split(": ", 1)[1])[-2:] == ["--format", "markdown"]

    kept = json_mode + " --format markdown"
    other = tmp_path / "other"
    _seed(_config(other), entry)
    _seed(_hooks(other), {"hooks": {"SessionStart": [_user_group(kept)]}})
    code, out, err = _install(other, None, capsys)
    assert code == 0, (out, err)
    handler = _read(_hooks(other))["hooks"]["SessionStart"][0]["hooks"][0]
    assert handler["command"] == kept
    assert handler["timeout"] == 120 and handler["additionalContextLimit"] == 0


def test_codex_hook_never_writes_a_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An entry whose uvx options name an index, or whose spec is a URL, gets no hook.

    Install never writes a URL into a hook file. The MCP entry is untouched
    by that, the receipt says why, and hooks.json is not created. Mutation:
    let ``_hook_block`` return None. The hook carries the URL and this test
    fails.
    """

    secret = "tok-" + "urlcanary"
    entries = {
        "index": f'["--index-url", "https://user:{secret}@example.invalid/simple", "alice-memory", "mcp", "--data-dir", "{{vault}}"]',
        "spec": f'["alice-memory@https://user:{secret}@example.invalid/x.whl", "mcp", "--data-dir", "{{vault}}"]',
    }
    for label, args in entries.items():
        home = tmp_path / label
        vault = (tmp_path / f"vault-{label}").resolve()
        _seed(_config(home), f'[mcp_servers.alice]\ncommand = "uvx"\nargs = {args.format(vault=vault)}\n')
        code, out, err = _install(home, None, capsys)
        assert code == 0, (label, out, err)
        assert not _hooks(home).exists(), label
        lines = out.splitlines()
        assert "session_start: skipped" in lines
        assert any(
            line.startswith("warning: install added no SessionStart hook:")
            and "install never writes a URL into a hook file" in line
            for line in lines
        ), out
        assert secret not in out + err
        if label == "index":
            after = [line for line in lines if line.startswith("session_start_argv_after_change: ")]
            assert after, out
            argv = json.loads(after[0].split(": ", 1)[1])
            assert argv[-4:] == ["--data-dir", str(vault), "--format", "markdown"]


def test_codex_hook_carries_the_allowlisted_uvx_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The uvx options install carries into a hook come along, before ``--from``.

    Mutation: drop the launcher's options from the hook argv. The hook
    resolves a different alice-memory than the server, and this test fails.
    """

    home = tmp_path / "home"
    vault = (tmp_path / "vault").resolve()
    _seed(
        _config(home),
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        f'args = ["--python", "3.12", "--offline", "alice-memory", "mcp", "--data-dir", "{vault}"]\n',
    )
    code, out, err = _install(home, None, capsys)
    assert code == 0, (out, err)
    command = _read(_hooks(home))["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command == (
        "uvx --python 3.12 --offline --from alice-memory alice-memory-session-start "
        f"--data-dir {shlex.quote(str(vault))} --format markdown"
    )


def test_codex_hook_dry_run_masks_a_kept_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hook install keeps is printed with every word it did not write hidden.

    The entry's ``--with`` keeps install from rebuilding the hook, so the
    user's own command, with a secret assignment in front, is what the dry
    run would print. Mutation: print the command without ``shown_hook_words``.
    The secret appears and this test fails.
    """

    secret = "tok-" + "keptcanary"
    home = tmp_path / "home"
    vault = (tmp_path / "vault").resolve()
    _seed(
        _config(home),
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        f'args = ["--with", "requests", "alice-memory", "mcp", "--data-dir", "{vault}"]\n',
    )
    kept = (
        f"TOKEN={secret} uvx --from alice-memory alice-memory-session-start "
        f"--data-dir {vault} --format markdown"
    )
    hooks = _seed(_hooks(home), {"hooks": {"SessionStart": [_user_group(kept)]}})
    before = hooks.read_bytes()
    code, out, err = _install(home, None, capsys, "--dry-run")
    assert code == 0, (out, err)
    assert hooks.read_bytes() == before
    assert secret not in out + err
    assert "<hidden> uvx --from alice-memory alice-memory-session-start" in out
    assert "hook command (1 word install does not print)" in out


def test_codex_hook_refuses_a_kept_hook_it_cannot_move_and_prints_markdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A kept hook whose data dir install cannot rely on is refused, with the argv to add by hand.

    ``--data-dir`` is passed, the entry's ``--with`` stops install from
    rebuilding the hook, and the old hook's relative data dir cannot be
    trusted. The printed argv has the new data dir and ends in
    ``--format markdown``. Mutation: leave ``output_format`` out of the
    ``_refuse_kept_hook`` call. This test fails.
    """

    old_vault = (tmp_path / "old").resolve()
    new_vault = (tmp_path / "new").resolve()
    home = tmp_path / "home"
    _seed(
        _config(home),
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        f'args = ["--with", "requests", "alice-memory", "mcp", "--data-dir", "{old_vault}"]\n',
    )
    hooks = _seed(
        _hooks(home),
        {
            "hooks": {
                "SessionStart": [
                    _user_group(
                        "uvx --from alice-memory alice-memory-session-start --data-dir relative-vault"
                    )
                ]
            }
        },
    )
    before = hooks.read_bytes()
    code, out, err = _install(home, new_vault, capsys)
    assert code == 1, (out, err)
    assert hooks.read_bytes() == before
    lines = out.splitlines()
    assert "session_start: refused" in lines
    argv_lines = [line for line in lines if line.startswith("session_start_argv: ")]
    assert argv_lines, out
    argv = json.loads(argv_lines[0].split(": ", 1)[1])
    assert argv[-4:] == ["--data-dir", str(new_vault), "--format", "markdown"]


def test_codex_hook_dry_run_hides_group_keys_from_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Alice's group is printed with every key install did not write hidden.

    A ``matcher`` on the group stays in place on a re-run, so the dry run
    prints it as ``<hidden>`` and names it. Mutation: print the group's
    other keys as they are. The secret appears and this test fails.
    """

    secret = "startup|" + "matchercanary"
    home, vault = tmp_path / "home", tmp_path / "vault"
    _install(home, vault, capsys)
    document = _read(_hooks(home))
    document["hooks"]["SessionStart"][0]["matcher"] = secret
    hooks = _seed(_hooks(home), document)
    before = hooks.read_bytes()
    code, out, err = _install(home, vault, capsys, "--dry-run")
    assert code == 0, (out, err)
    assert hooks.read_bytes() == before
    assert secret not in out + err
    assert '"matcher": "<hidden>"' in out
    assert "hook matcher" in out


def test_codex_hook_of_a_db_entry_keeps_its_own_data_dir_and_prints_markdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``--db`` entry keeps the hook on the hook's own store, rebuilt with ``--format markdown``.

    ``--data-dir`` does not move a ``--db`` entry. The imported JSON-mode
    hook keeps its data dir and gets the launcher and the markdown format.
    Mutation: leave ``output_format`` out of the own-dir rebuild. This test
    fails.
    """

    home = tmp_path / "home"
    own = (tmp_path / "own-store").resolve()
    _seed(
        _config(home),
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        f'args = ["alice-memory", "mcp", "--db", "{tmp_path / "alice.sqlite"}"]\n',
    )
    _seed(
        _hooks(home),
        {
            "hooks": {
                "SessionStart": [
                    _user_group(
                        f"uvx --from alice-memory alice-memory-session-start --data-dir {own}"
                    )
                ]
            }
        },
    )
    code, out, err = _install(home, None, capsys)
    assert code == 0, (out, err)
    handler = _read(_hooks(home))["hooks"]["SessionStart"][0]["hooks"][0]
    assert handler == _handler(own)


def test_codex_hook_only_command_handlers_count_as_alices(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A handler of another type that names the script is not Alice's, so it is left alone.

    Mutation: match on the command text alone. Install would replace the
    user's ``mcp_tool`` handler, and this test fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    theirs = {
        "type": "mcp_tool",
        "server": "s",
        "tool": "t",
        "command": "uvx --from alice-memory alice-memory-session-start --data-dir /v",
    }
    _seed(_hooks(home), {"hooks": {"SessionStart": [{"hooks": [theirs]}]}})
    code, out, err = _install(home, vault, capsys)
    assert code == 0, (out, err)
    groups = _read(_hooks(home))["hooks"]["SessionStart"]
    assert groups[0] == {"hooks": [theirs]}
    assert groups[1] == {"hooks": [_handler(vault)]}


def test_codex_hook_windows_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On Windows the command is PowerShell-safe: first token bare, a spaced value double-quoted.

    ``'`` and ``$`` are refused, so the hook is not written, the MCP entry is,
    and the receipt prints the argv with ``--format markdown``. Mutation:
    single-quote the value, or drop ``--format`` from the printed argv. This
    test fails.
    """

    monkeypatch.setattr(host_launcher, "WINDOWS_HOOKS", True)
    home = tmp_path / "home"
    spaced = tmp_path / "vault with space"
    code, out, err = _install(home, spaced, capsys)
    assert code == 0, (out, err)
    command = _read(_hooks(home))["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command == (
        "uvx --from alice-memory alice-memory-session-start --data-dir "
        f'"{spaced.resolve().as_posix()}" --format markdown'
    )
    assert command.split()[0] == "uvx"

    other = tmp_path / "other"
    dollar = tmp_path / "v$ault"
    code, out, err = _install(other, dollar, capsys)
    assert code == 1, (out, err)
    assert not _hooks(other).exists()
    assert _config(other).is_file()
    lines = out.splitlines()
    assert "session_start: refused" in lines
    argv_lines = [line for line in lines if line.startswith("session_start_argv: ")]
    assert argv_lines and json.loads(argv_lines[0].split(": ", 1)[1])[-2:] == ["--format", "markdown"]
    assert any("$" in line and line.startswith("session_start_reason:") for line in lines)


def test_codex_hook_every_rewrite_backs_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The first write and a later rewrite each back the file up in that run's vault.

    Mutation: skip the hooks.json backup. This test fails.
    """

    home = tmp_path / "home"
    first, second = tmp_path / "vault-one", tmp_path / "vault-two"
    seed = {"hooks": {"Stop": [_user_group("echo bye")]}}
    seeded = _seed(_hooks(home), seed)
    original = seeded.read_bytes()
    _install(home, first, capsys)
    assert [path.read_bytes() for path in _backups(first, "hooks.json")] == [original]
    after_first = _hooks(home).read_bytes()
    _install(home, second, capsys)
    assert [path.read_bytes() for path in _backups(second, "hooks.json")] == [after_first]
    assert len(_backups(first, "hooks.json")) == 1
    for path in _backups(first, "hooks.json") + _backups(second, "hooks.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_codex_hook_symlink_is_written_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A symlinked hooks.json stays a link and the target is edited, like every host file.

    Mutation: replace the link with a regular file. This test fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    target = tmp_path / "dotfiles" / "hooks.json"
    _seed(target, {"hooks": {"Stop": [_user_group("echo bye")]}})
    link = _hooks(home)
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert link.is_symlink()
    assert _read(target)["hooks"]["SessionStart"] == [{"hooks": [_handler(vault)]}]
    assert f"session_start_target: {target.resolve()}" in out.splitlines()


def test_codex_hook_changed_while_running_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hooks.json edited between planning and the write is left as the user made it.

    Install refuses the hook with a run-again line, removes its backup, and
    exits 1; config.toml was already written. Mutation: drop ``expected`` on
    the hooks write. The edit is overwritten and this test fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    hooks = _seed(_hooks(home), {"hooks": {"Stop": [_user_group("echo bye")]}})
    real = host_install._write_text

    def racing(path: Path, text: str, **kwargs: object) -> None:
        if path.name == "hooks.json":
            path.write_text('{"hooks": {}}\n', encoding="utf-8")
        real(path, text, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(host_install, "_write_text", racing)
    code, out, err = _install(home, vault, capsys)
    assert code == 1, (out, err)
    assert hooks.read_text(encoding="utf-8") == '{"hooks": {}}\n'
    lines = out.splitlines()
    assert "session_start: refused" in lines
    assert "session_start_reason: hooks.json changed while install ran" in lines
    assert "next: hooks.json was not changed. Run install again." in lines
    assert not _backups(vault, "hooks.json")
    assert _config(home).is_file()


def test_codex_hook_write_failure_is_a_failed_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An OSError writing hooks.json fails the host and names the file, after config.toml is written.

    Mutation: let the OSError escape. The receipt says ``unexpected OSError``.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    real = host_install._write_text

    def failing(path: Path, text: str, **kwargs: object) -> None:
        if path.name == "hooks.json":
            raise OSError("disk full")
        real(path, text, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(host_install, "_write_text", failing)
    code, out, err = _install(home, vault, capsys)
    assert code != 0
    lines = out.splitlines()
    assert "session_start: failed" in lines
    assert any(line.startswith("session_start_file: ") and "hooks.json" in line for line in lines)
    assert "reason: unexpected" not in out
    assert _config(home).is_file()
    assert not _hooks(home).exists()


# --- rules a mutation once slipped past -----------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["h", "--format", "json"], ["h", "--format", "markdown"]),
        (["h", "--format=json"], ["h", "--format=markdown"]),
        (["h", "--format", "json", "--format=json"], ["h", "--format", "json", "--format=markdown"]),
        (["h", "--format=json", "--format", "json"], ["h", "--format=json", "--format", "markdown"]),
        (["h"], ["h", "--format", "markdown"]),
        (["h", "--format"], ["h", "--format", "markdown"]),
    ],
    ids=["space", "equals", "last-is-equals", "last-is-space", "none", "dangling"],
)
def test_with_output_format_sets_the_last_format_in_its_own_spelling(
    argv: list[str], expected: list[str]
) -> None:
    """The word Codex reads is the last ``--format``, and it keeps the spelling it was written in.

    Mutation: replace the value with ``output_format`` even for the equals
    spelling (``out[index] = output_format``). The equals cases become a bare
    ``markdown`` word and fail. Mutation: drop the ``--format=`` branch. The
    equals cases append a second ``--format`` and fail.
    """

    assert host_install._with_output_format(argv, "markdown") == expected


@pytest.mark.parametrize(
    ("spelling", "expected_tail"),
    [
        ("--format=json", ["--format=markdown"]),
        ("--format json", ["--format", "markdown"]),
    ],
    ids=["equals", "space"],
)
def test_codex_hook_kept_json_mode_command_prints_the_argv_to_add(
    spelling: str, expected_tail: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A kept hook that ends in ``--format=json`` is refused with the argv fixed in place.

    The receipt argv is what the user pastes into hooks.json, so it changes
    the word that is there and adds no second ``--format``. Mutation: rewrite
    the equals spelling as a bare ``markdown`` word (the argv line vanishes,
    because a bare word is hidden), or append a second ``--format``. This
    test fails.
    """

    vault = (tmp_path / "vault").resolve()
    home = tmp_path / "home"
    _seed(
        _config(home),
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        f'args = ["--with", "requests", "alice-memory", "mcp", "--data-dir", "{vault}"]\n',
    )
    kept = f"uvx --from alice-memory alice-memory-session-start --data-dir {vault} {spelling}"
    hooks = _seed(_hooks(home), {"hooks": {"SessionStart": [_user_group(kept)]}})
    before = hooks.read_bytes()
    code, out, err = _install(home, None, capsys)
    assert code == 1, (out, err)
    assert hooks.read_bytes() == before
    argv_lines = [line for line in out.splitlines() if line.startswith("session_start_argv: ")]
    assert len(argv_lines) == 1, out
    argv = json.loads(argv_lines[0].split(": ", 1)[1])
    assert argv[-len(expected_tail) :] == expected_tail
    assert len([word for word in argv if word.startswith("--format")]) == 1
    assert "json" not in argv and "--format=json" not in argv


def test_codex_hook_created_while_running_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hooks.json the user creates between install's read and its write is left as they made it.

    There was no file when install read, so the write expects none. Mutation:
    write with ``expect_absent=False``. The user's file is overwritten and
    this test fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    mine = '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}\n'
    real = host_install._write_text

    def racing(path: Path, text: str, **kwargs: object) -> None:
        if path.name == "hooks.json":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(mine, encoding="utf-8")
        real(path, text, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(host_install, "_write_text", racing)
    code, out, err = _install(home, vault, capsys)
    assert code == 1, (out, err)
    assert _hooks(home).read_text(encoding="utf-8") == mine
    lines = out.splitlines()
    assert "session_start: refused" in lines
    assert "session_start_reason: hooks.json changed while install ran" in lines
    assert MODIFIED not in lines and TRUST_NEXT not in lines
    assert not list(_hooks(home).parent.glob(".hooks.json.*"))


@pytest.mark.parametrize(
    "variant",
    ["no-limit", "limit-16000", "async-true", "status-message", "command-windows", "timeout-30"],
)
def test_codex_hook_handler_that_differs_in_any_key_is_replaced(
    variant: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Alice's handler with the right command but any other key is not ``unchanged``.

    A missing ``additionalContextLimit`` lets Codex spill the brief to a
    file, ``async`` changes when it runs, and ``commandWindows`` changes what
    runs. Install replaces the whole handler and asks for trust again.
    Mutation: compare only type, command and timeout in
    ``_merge_codex_session_start``. Every case but ``timeout-30`` becomes
    ``unchanged`` and fails; ``timeout-30`` fails on any comparison that
    leaves timeout out.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    handler = _handler(vault)
    if variant == "no-limit":
        del handler["additionalContextLimit"]
    elif variant == "limit-16000":
        handler["additionalContextLimit"] = 16000
    elif variant == "async-true":
        handler["async"] = True
    elif variant == "status-message":
        handler["statusMessage"] = "Loading Alice"
    elif variant == "command-windows":
        handler["commandWindows"] = "echo replaced"
    else:
        handler["timeout"] = 30
    _seed(_hooks(home), {"hooks": {"SessionStart": [{"hooks": [handler]}]}})
    code, out, err = _install(home, vault, capsys)
    assert code == 0, (out, err)
    assert _read(_hooks(home))["hooks"]["SessionStart"] == [{"hooks": [_handler(vault)]}]
    lines = out.splitlines()
    assert WRITTEN in lines and MODIFIED in lines and TRUST_NEXT in lines
    assert UNCHANGED not in lines


def test_codex_toml_hooks_rerun_prints_the_hook_again_and_still_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Until the user adds the printed hook, every run prints it again and exits 1.

    The second run changes nothing, so its action is ``unchanged``, but the
    hook the user has to add is still missing, so the receipt still carries
    the snippet. Mutation: drop the snippet from the unchanged receipt
    (``snippet=None``). The second run loses it and this test fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    _seed(_config(home), _toml_hook())
    first_code, first_out, first_err = _install(home, vault, capsys)
    second_code, second_out, second_err = _install(home, vault, capsys)
    assert (first_code, second_code) == (1, 1), (first_err, second_err)
    lines = second_out.splitlines()
    assert "action: unchanged" in lines
    assert "session_start: refused" in lines
    assert "snippet:" in lines
    second = second_out.split("snippet:\n", 1)[1].split("\nnext:", 1)[0]
    first = first_out.split("snippet:\n", 1)[1].split("\nnext:", 1)[0]
    assert second == first
    assert "[[hooks.SessionStart.hooks]]" in second and "--format markdown" in second
    assert any(line.startswith("next: install did not write hooks.json") for line in lines)


def _toml_alice_hook(vault: Path, *, drop: str | None = None, **override: object) -> str:
    """The TOML hook install prints for ``vault``, with a key dropped or a value changed."""

    values: dict[str, object] = {
        "type": "command",
        "command": _command(vault),
        "timeout": 120,
        "additionalContextLimit": 0,
        **override,
    }
    if drop is not None:
        del values[drop]
    body = "".join(f"{key} = {json.dumps(value)}\n" for key, value in values.items())
    return "[[hooks.SessionStart]]\n\n[[hooks.SessionStart.hooks]]\n" + body


def test_codex_toml_hook_the_user_already_added_is_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Once the printed hook is in config.toml, install says so and exits 0.

    The user did what the first run asked, so the next run has nothing to
    refuse and nothing to print. Mutation: never recognise it (compare
    against nothing). The second run exits 1 with the snippet and this test
    fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    config = _seed(_config(home), "# mine\n" + _toml_hook())
    code, out, err = _install(home, vault, capsys)
    assert code == 1, (out, err)
    printed = out.split("snippet:\n", 1)[1].split("\nnext:", 1)[0]
    config.write_text(config.read_text(encoding="utf-8") + "\n" + printed, encoding="utf-8")
    before = config.read_bytes()
    for _ in range(2):
        code, out, err = _install(home, vault, capsys)
        assert code == 0, (out, err)
        lines = out.splitlines()
        assert "session_start: unchanged in config.toml (Codex runs it only if you have trusted it)" in lines
        assert "action: unchanged" in lines
        assert "snippet:" not in lines
        assert not any(line.startswith("session_start_reason:") for line in lines)
        assert "session_start_path:" not in out
        assert TRUST_NEXT not in lines and MODIFIED not in lines
        assert lines[-1] == CHECK_NEXT
        assert config.read_bytes() == before
        assert not _hooks(home).exists()


@pytest.mark.parametrize(
    "hook",
    [
        "json-mode-command",
        "no-limit",
        "limit-16000",
        "timeout-30",
        "other-vault",
    ],
)
def test_codex_toml_hook_that_is_not_the_one_install_would_write_is_refused(
    hook: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An Alice hook in config.toml that differs is refused with a replace-by-hand line.

    Install never edits TOML hooks. Telling the user to add the printed hook
    would leave two Alice hooks and the brief twice, so the reason and the
    next line say to replace the one that is there. Mutation: recognise any
    Alice hook in config.toml as current (``plan.toml_alice_handler is not
    None``). Every case exits 0 and fails. Mutation: compare only the
    command. The ``no-limit``, ``limit-16000`` and ``timeout-30`` cases fail.
    Mutation: use the add-it-by-hand reason for a hook that is there. The
    reason assertion fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    other = tmp_path / "other"
    if hook == "json-mode-command":
        text = _toml_alice_hook(
            vault,
            command="uvx --from alice-memory alice-memory-session-start --data-dir "
            f"{shlex.quote(str(vault.resolve()))}",
        )
    elif hook == "no-limit":
        text = _toml_alice_hook(vault, drop="additionalContextLimit")
    elif hook == "limit-16000":
        text = _toml_alice_hook(vault, additionalContextLimit=16000)
    elif hook == "timeout-30":
        text = _toml_alice_hook(vault, timeout=30)
    else:
        text = _toml_alice_hook(other)
    config = _seed(_config(home), text)
    code, out, err = _install(home, vault, capsys)
    assert code == 1, (out, err)
    lines = out.splitlines()
    assert "session_start: refused" in lines
    assert (
        "session_start_reason: the alice-memory-session-start hook in config.toml is not the "
        "one install would write, and install does not edit hooks in config.toml"
    ) in lines
    assert not any(line.startswith("session_start_reason: config.toml already holds hooks") for line in lines)
    assert any(
        line.startswith("next: install did not change the hook in config.toml. Replace the "
                        "alice-memory-session-start hook there")
        for line in lines
    )
    assert not any(line.startswith("next: install did not write hooks.json") for line in lines)
    snippet = out.split("snippet:\n", 1)[1].split("\nnext:", 1)[0]
    parsed = tomllib.loads(snippet[snippet.index("[[hooks.SessionStart]]") :])
    assert parsed["hooks"]["SessionStart"][0]["hooks"][0]["command"] == _command(vault)
    assert config.read_text(encoding="utf-8").count("[[hooks.SessionStart]]") == 1
    assert not _hooks(home).exists()


def test_codex_hook_with_a_shell_read_data_dir_is_kept_unless_data_dir_is_passed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hook whose ``--data-dir`` the shell expands is the user's to move, so ``--data-dir`` decides.

    Without ``--data-dir`` install keeps the command as it is (an entry
    install rebuilds nothing for, and one it blocks). With ``--data-dir``, a
    rebuildable hook moves to it, and a hook install must keep is refused.
    Mutation: treat every run as if ``--data-dir`` were passed
    (``explicit = True``). The kept command is rebuilt in the first case and
    refused in the third, and this test fails.
    """

    kept = (
        "uvx --from alice-memory alice-memory-session-start "
        '--data-dir "$HOME/vault" --format markdown'
    )
    vault = (tmp_path / "vault").resolve()
    document = {"hooks": {"SessionStart": [_user_group(kept)]}}

    plain = tmp_path / "plain"
    _seed(_hooks(plain), document)
    code, out, err = _install(plain, None, capsys)
    assert code == 0, (out, err)
    assert _read(_hooks(plain))["hooks"]["SessionStart"][0]["hooks"][0] == {
        "type": "command",
        "command": kept,
        "timeout": 120,
        "additionalContextLimit": 0,
    }
    assert any(
        line.startswith("warning: the SessionStart hook's --data-dir $HOME/vault is not read literally")
        for line in out.splitlines()
    ), out

    moved = tmp_path / "moved"
    _seed(_hooks(moved), document)
    code, out, err = _install(moved, vault, capsys)
    assert code == 0, (out, err)
    assert _read(_hooks(moved))["hooks"]["SessionStart"][0]["hooks"][0] == _handler(vault)

    entry = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        f'args = ["--with", "requests", "alice-memory", "mcp", "--data-dir", "{vault}"]\n'
    )
    blocked = tmp_path / "blocked"
    _seed(_config(blocked), entry)
    _seed(_hooks(blocked), document)
    code, out, err = _install(blocked, None, capsys)
    assert code == 0, (out, err)
    assert "session_start: refused" not in out.splitlines()
    assert _read(_hooks(blocked))["hooks"]["SessionStart"][0]["hooks"][0]["command"] == kept

    refused = tmp_path / "refused"
    _seed(_config(refused), entry)
    seeded = _seed(_hooks(refused), document)
    before = seeded.read_bytes()
    code, out, err = _install(refused, tmp_path / "elsewhere", capsys)
    assert code == 1, (out, err)
    assert "session_start: refused" in out.splitlines()
    assert seeded.read_bytes() == before


def test_codex_hook_dry_run_hides_a_group_key_that_holds_a_number(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only ``timeout``, ``additionalContextLimit`` and ``async`` are printed as they are.

    A number under any other key of Alice's group is the user's data. It is
    printed as ``<hidden>`` and named. Mutation: print every int-valued key
    (``isinstance(value, bool | int)`` alone). The number appears and this
    test fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    _install(home, vault, capsys)
    document = _read(_hooks(home))
    document["hooks"]["SessionStart"][0]["priority"] = 6431907
    hooks = _seed(_hooks(home), document)
    before = hooks.read_bytes()
    code, out, err = _install(home, vault, capsys, "--dry-run")
    assert code == 0, (out, err)
    assert hooks.read_bytes() == before
    assert "6431907" not in out + err
    hook_part = out.split("\n---\n", 1)[1]
    shown = json.loads(hook_part.split("\nnext:", 1)[0].split("\nhidden:", 1)[0])
    group = shown["hooks"]["SessionStart"][0]
    assert group["priority"] == "<hidden>"
    assert group["hooks"][0]["timeout"] == 120
    assert group["hooks"][0]["additionalContextLimit"] == 0
    assert "hook priority" in out


def test_codex_hook_dry_run_prints_only_alices_command_handlers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A user handler of another type that names the script is not printed as Alice's.

    Mutation: judge a printed handler by its command text alone
    (``_is_alice_hook_item`` for every host). The user's ``mcp_tool`` group
    is printed and this test fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    theirs = {
        "type": "mcp_tool",
        "server": "s",
        "tool": "t",
        "command": "uvx --from alice-memory alice-memory-session-start --data-dir /v",
    }
    hooks = _seed(_hooks(home), {"hooks": {"SessionStart": [{"hooks": [theirs]}]}})
    before = hooks.read_bytes()
    code, out, err = _install(home, vault, capsys, "--dry-run")
    assert code == 0, (out, err)
    assert hooks.read_bytes() == before
    assert "mcp_tool" not in out
    hook_part = out.split("\n---\n", 1)[1]
    shown = json.loads(hook_part.split("\nnext:", 1)[0].split("\nhidden:", 1)[0])
    assert len(shown["hooks"]["SessionStart"]) == 1
    assert shown["hooks"]["SessionStart"][0]["hooks"][0]["command"] == _command(vault)


@pytest.mark.parametrize(
    ("body", "noted"),
    [("[features]\nhooks = false\n", True), ("[features]\nhooks = true\n", False)],
    ids=["off", "on"],
)
def test_codex_hooks_feature_off_note_is_in_a_dry_run(
    body: str, noted: bool, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dry run tells the user Codex will run no hook, as a real run does.

    Mutation: print the note only when not a dry run
    (``plan.hooks_disabled and not dry_run``). This test fails.
    """

    home = tmp_path / "home"
    config = _seed(_config(home), body)
    before = config.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys, "--dry-run")
    assert code == 0, (out, err)
    note = "note: features.hooks is false in config.toml, so Codex will not run any hook"
    assert (note in out.splitlines()) is noted
    assert config.read_bytes() == before
    assert not _hooks(home).exists()


def test_codex_hook_windows_kept_hook_refusal_prints_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A kept hook whose data dir cannot be moved on Windows is refused with a markdown argv.

    The entry's ``--with`` keeps install from rebuilding the hook, the hook's
    ``C:/`` data dir is one install can rely on, and the new ``--data-dir``
    holds a ``$`` that PowerShell, cmd and Git Bash do not all keep literal.
    The hook has no ``--format``. Mutation: leave ``output_format`` out of
    the first ``_refuse_kept_hook`` call in ``_plan_hook``. The printed argv
    has no ``--format markdown`` and this test fails.
    """

    monkeypatch.setattr(host_launcher, "WINDOWS_HOOKS", True)
    home = tmp_path / "home"
    _seed(
        _config(home),
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["--with", "requests", "alice-memory", "mcp", "--data-dir", "C:/old/vault"]\n',
    )
    old = "uvx --from alice-memory alice-memory-session-start --data-dir C:/old/vault"
    hooks = _seed(_hooks(home), {"hooks": {"SessionStart": [_user_group(old)]}})
    before = hooks.read_bytes()
    new_vault = tmp_path / "v$ault"
    code, out, err = _install(home, new_vault, capsys)
    assert code == 1, (out, err)
    assert hooks.read_bytes() == before
    lines = out.splitlines()
    assert "session_start: refused" in lines
    assert any(
        line.startswith("session_start_reason: the SessionStart hook still points at C:/old/vault")
        for line in lines
    ), out
    argv_lines = [line for line in lines if line.startswith("session_start_argv: ")]
    assert len(argv_lines) == 1, out
    argv = json.loads(argv_lines[0].split(": ", 1)[1])
    assert argv[-4:] == ["--data-dir", str(new_vault.resolve()), "--format", "markdown"]


@pytest.mark.parametrize("scenario", ["hooks-write-fails", "hooks-changed", "config-write-fails"])
def test_codex_hook_modified_line_is_only_printed_once_the_hook_is_written(
    scenario: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``The hook changed`` and the trust line say nothing when hooks.json was not written.

    A hook install moved to a new vault is only changed once the file holds
    the new command. When the hooks write fails, hooks.json changes under
    install, or config.toml cannot be written first, the old hook is still
    what Codex trusts. Mutation: add the Modified line when the hook is
    planned, before any write. Every scenario fails; the last control still
    prints it.
    """

    home = tmp_path / "home"
    first, second = tmp_path / "vault-one", tmp_path / "vault-two"
    _install(home, first, capsys)
    real = host_install._write_text

    def wrapped(path: Path, text: str, **kwargs: object) -> None:
        if scenario == "hooks-write-fails" and path.name == "hooks.json":
            raise OSError("disk full")
        if scenario == "config-write-fails" and path.name == "config.toml":
            raise OSError("disk full")
        if scenario == "hooks-changed" and path.name == "hooks.json":
            path.write_text('{"hooks": {}}\n', encoding="utf-8")
        real(path, text, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(host_install, "_write_text", wrapped)
    code, out, err = _install(home, second, capsys)
    assert code == 1, (out, err)
    lines = out.splitlines()
    assert MODIFIED not in lines
    assert TRUST_NEXT not in lines
    assert WRITTEN not in lines or scenario == "config-write-fails"
    if scenario == "hooks-changed":
        assert _hooks(home).read_text(encoding="utf-8") == '{"hooks": {}}\n'
    else:
        assert _read(_hooks(home))["hooks"]["SessionStart"][0]["hooks"][0] == _handler(first)

    monkeypatch.setattr(host_install, "_write_text", real)
    _seed(_hooks(home), {"hooks": {"SessionStart": [{"hooks": [_handler(first)]}]}})
    code, out, err = _install(home, second, capsys)
    assert code == 0, (out, err)
    assert MODIFIED in out.splitlines()


@pytest.mark.parametrize("source", ["no-files", "config-without-entry", "config-with-toml-hooks"])
def test_codex_new_entry_opens_the_data_dir_of_the_hook_already_there(
    source: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A new MCP entry follows the Alice hook that is already installed, as on Claude Code and Cursor.

    Without ``--data-dir``, the entry and the hook stay on the hook's vault
    instead of both moving to ``~/.alice`` with a note. The hook is the one in
    hooks.json, or the one in config.toml when config.toml holds TOML hooks.
    Mutation: give the new-file branch ``default_dir``, leave ``hook_dir`` out
    of the ``_plan_codex_text`` call, or read the dir from hooks.json when
    config.toml holds TOML hooks. The matching case fails.
    """

    hook_vault = (tmp_path / "hook-vault").resolve()
    home = tmp_path / "home"
    if source == "config-with-toml-hooks":
        _seed(_config(home), _toml_alice_hook(hook_vault))
        _seed(
            _hooks(home),
            {"hooks": {"SessionStart": [{"hooks": [_handler((tmp_path / "ignored").resolve())]}]}},
        )
    else:
        _seed(_hooks(home), {"hooks": {"SessionStart": [_user_group("echo mine"), {"hooks": [_handler(hook_vault)]}]}})
        if source == "config-without-entry":
            _seed(_config(home), "[features]\nhooks = true\n")
    hooks_before = _hooks(home).read_bytes() if _hooks(home).exists() else None
    code, out, err = _install(home, None, capsys)
    assert code == 0, (out, err)
    entry = tomllib.loads(_config(home).read_text(encoding="utf-8"))["mcp_servers"]["alice"]
    assert entry["args"][-2:] == ["--data-dir", str(hook_vault)]
    lines = out.splitlines()
    assert not any(line.startswith("session_start_data_dir:") for line in lines)
    assert not any("cannot be relied on" in line for line in lines)
    if source == "config-with-toml-hooks":
        assert "session_start: unchanged in config.toml (Codex runs it only if you have trusted it)" in lines
        assert _hooks(home).read_bytes() == hooks_before
    else:
        assert UNCHANGED in lines
        assert _hooks(home).read_bytes() == hooks_before
        assert _read(_hooks(home))["hooks"]["SessionStart"][1] == {"hooks": [_handler(hook_vault)]}


def test_codex_new_entry_takes_an_explicit_data_dir_over_the_hooks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--data-dir`` still decides: the entry and the hook both move to it, and the receipt says so.

    Mutation: let the hook's vault win over ``--data-dir`` for a new entry.
    This test fails.
    """

    hook_vault = (tmp_path / "hook-vault").resolve()
    explicit = (tmp_path / "explicit").resolve()
    home = tmp_path / "home"
    _seed(_hooks(home), {"hooks": {"SessionStart": [{"hooks": [_handler(hook_vault)]}]}})
    code, out, err = _install(home, explicit, capsys)
    assert code == 0, (out, err)
    entry = tomllib.loads(_config(home).read_text(encoding="utf-8"))["mcp_servers"]["alice"]
    assert entry["args"][-2:] == ["--data-dir", str(explicit)]
    assert _read(_hooks(home))["hooks"]["SessionStart"] == [{"hooks": [_handler(explicit)]}]
    assert f"session_start_data_dir: {hook_vault} -> {explicit}" in out.splitlines()


def test_codex_new_entry_ignores_a_hook_it_cannot_rely_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hook whose ``--data-dir`` is relative gives the new entry no vault, so it uses ``~/.alice``.

    Mutation: trust any hook data dir for a new entry. The entry opens a
    relative path and this test fails.
    """

    home = tmp_path / "home"
    relative = (
        "uvx --from alice-memory alice-memory-session-start --data-dir relative-vault --format markdown"
    )
    _seed(_hooks(home), {"hooks": {"SessionStart": [_user_group(relative)]}})
    code, out, err = _install(home, None, capsys)
    assert code == 0, (out, err)
    entry = tomllib.loads(_config(home).read_text(encoding="utf-8"))["mcp_servers"]["alice"]
    default = str((home.resolve() / ".alice"))
    assert entry["args"][-2:] == ["--data-dir", default]
    assert _read(_hooks(home))["hooks"]["SessionStart"][0]["hooks"][0] == _handler(Path(default))


# --- test_markdown_brief_never_starts_with_a_bracket ---------------------------------------


def _hook_output(monkeypatch: pytest.MonkeyPatch, capsys, *argv: str) -> str:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert session_start_hook.main(list(argv)) == 0
    return capsys.readouterr().out


def test_markdown_brief_never_starts_with_a_bracket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Codex reads stdout that starts with ``{`` or ``[`` as JSON and fails the hook on a bad parse.

    Every markdown form the hook prints must start with something else: the
    empty vault line, the framed brief (even when a stored fact starts with
    ``[`` or ``{``), the not-absolute data dir line, and both fail-open
    outputs (a blank line). Mutation: print the brief without its frame, or
    fail open with ``{}``. This test fails.
    """

    def first(text: str) -> str:
        stripped = text.lstrip()
        return stripped[:1]

    empty = tmp_path / "empty"
    forms = {"empty": _hook_output(monkeypatch, capsys, "--format", "markdown", "--data-dir", str(empty))}
    assert forms["empty"].strip() == "Nothing stored yet."

    vault = tmp_path / "vault"
    commit_fact(vault, monkeypatch, "Bracket fact", "[bracket] opens this fact")
    commit_fact(vault, monkeypatch, "Brace fact", '{"json": "like"} opens this fact')
    forms["framed"] = _hook_output(monkeypatch, capsys, "--format", "markdown", "--data-dir", str(vault))
    assert "opens this fact" in forms["framed"]
    assert forms["framed"].startswith("Stored notes from Alice memory")

    forms["relative"] = _hook_output(monkeypatch, capsys, "--format", "markdown", "--data-dir", "relative/dir")
    assert forms["relative"].startswith("Alice: the data directory")

    jsonrpc = tmp_path / "jsonrpc"
    commit_fact(jsonrpc, monkeypatch, "Wire text", "jsonrpc 2.0 appears in this fact")
    forms["fail-open-jsonrpc"] = _hook_output(
        monkeypatch, capsys, "--format", "markdown", "--data-dir", str(jsonrpc)
    )
    assert forms["fail-open-jsonrpc"] == "\n"

    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x", encoding="utf-8")
    forms["fail-open-error"] = _hook_output(
        monkeypatch, capsys, "--format", "markdown", "--data-dir", str(not_a_dir / "vault")
    )
    assert forms["fail-open-error"] == "\n"

    for label, text in forms.items():
        assert first(text) not in {"{", "["}, (label, text[:80])
    # The JSON form is what Codex rejects, so the hook must not use it.
    as_json = _hook_output(monkeypatch, capsys, "--format", "json", "--data-dir", str(vault))
    assert first(as_json) == "{"


# --- round 3: paths the round 2 recheck found untested --------------------------------------

DRY_RUN_REFUSAL = "dry run: install would refuse this file; nothing was attempted"
_STOP_HOOK = (
    "[[hooks.Stop]]\n\n[[hooks.Stop.hooks]]\n"
    'type = "command"\n'
    'command = "echo stop"\n'
)


@pytest.mark.parametrize("state", ["no-alice-hook", "matching-alice-hook", "stale-alice-hook"])
def test_codex_toml_hooks_dry_run_prints_the_hook_and_says_it_would_refuse(
    state: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dry run with a ``[[hooks.Stop]]`` command hook in config.toml changes nothing and says what a run would do.

    With no Alice hook there, or a stale one, the receipt prints the TOML hook
    to add after the ``---`` line, adds the ``dry run: install would refuse``
    line, and exits 1. With Alice's matching hook already there it exits 0
    and prints neither. config.toml keeps its bytes, and no hooks.json,
    vault or backup appears. Mutation: replace the ``if hook_snippet is not
    None:`` block of the dry run with ``if False:``. The ``no-alice-hook``
    and ``stale-alice-hook`` cases lose the snippet and fail. Mutation:
    replace ``if hook_problem is not None: dry_trailer.append(_DRY_RUN_REFUSAL)``
    with ``if False:``. The same two cases lose the refusal line and fail.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    alice = {
        "no-alice-hook": "",
        "matching-alice-hook": "\n" + _toml_alice_hook(vault),
        "stale-alice-hook": "\n" + _toml_alice_hook(vault, timeout=30),
    }[state]
    config = _seed(_config(home), _STOP_HOOK + alice)
    before = config.read_bytes()
    code, out, err = _install(home, vault, capsys, "--dry-run")
    lines = out.splitlines()
    assert config.read_bytes() == before
    assert not _hooks(home).exists()
    assert not vault.exists()
    assert "action: dry-run" in lines
    snippet = out.split("snippet:\n", 1)[1]
    if state == "matching-alice-hook":
        assert code == 0, (out, err)
        assert "\n---\n" not in snippet
        assert DRY_RUN_REFUSAL not in lines
        assert "session_start: unchanged in config.toml (Codex runs it only if you have trusted it)" in lines
        return
    assert code == 1, (out, err)
    assert "\n---\n" in snippet
    hook_part = snippet.split("\n---\n", 1)[1].split("\nnext:", 1)[0]
    parsed = tomllib.loads(hook_part[hook_part.index("[[hooks.SessionStart]]") :])
    handler = parsed["hooks"]["SessionStart"][0]["hooks"][0]
    assert handler["command"] == _command(vault)
    assert handler["timeout"] == 120 and handler["additionalContextLimit"] == 0
    assert DRY_RUN_REFUSAL in lines
    assert "session_start: refused" in lines


@pytest.mark.parametrize("kind", ["directory", "mode-0"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["written", "dry-run"])
def test_codex_hooks_file_that_cannot_be_read_is_a_failed_host_and_writes_nothing(
    kind: str, dry_run: bool, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hooks.json that is a directory, or a file nobody can read, fails the host before any write.

    Exit 1, ``action: failed`` and the generic file reason. config.toml, the
    vault and the backups do not exist afterwards. Mutation: change the
    ``except OSError`` after the hook-load ``try`` block to ``except
    ZeroDivisionError``. The OSError escapes to the host's catch-all, whose
    reason is ``unexpected ...Error``, and the reason assertion fails.
    """

    if kind == "mode-0" and os.geteuid() == 0:
        pytest.skip("root reads a file with mode 0")
    home, vault = tmp_path / "home", tmp_path / "vault"
    hooks = _hooks(home)
    if kind == "directory":
        hooks.mkdir(parents=True)
    else:
        _seed(hooks, {"hooks": {}})
        hooks.chmod(0)
    try:
        code, out, err = _install(home, vault, capsys, *(["--dry-run"] if dry_run else []))
    finally:
        if kind == "mode-0":
            hooks.chmod(0o600)
    assert code == 1, (out, err)
    lines = out.splitlines()
    assert "action: failed" in lines
    assert "reason: the file could not be read or written" in lines
    assert f"file: {hooks}" in lines
    assert not _config(home).exists()
    assert not vault.exists()
    assert not _backups(vault, "hooks.json") and not _backups(vault, "config.toml")


def test_codex_hook_refusal_prints_no_argv_and_no_secret_for_a_kept_json_mode_hook(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A kept JSON-mode hook with an ``API_KEY=...`` prefix is refused without printing its argv.

    The entry's ``--index-url`` keeps install from rebuilding the hook, and
    the kept command prints JSON, which Codex rejects. Any argv the receipt
    printed would have the prefix hidden, so it could only be used by pasting
    the secret back: the receipt prints none, and neither the prefix value
    nor the index credential shows in stdout or stderr. Mutation: drop the
    ``if not hidden:`` guard in ``_plan_hook``'s kept-command format refusal.
    A masked ``session_start_argv:`` line appears and this test fails.
    """

    vault = (tmp_path / "vault").resolve()
    home = tmp_path / "home"
    _seed(
        _config(home),
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        f'args = ["--index-url", "https://user:{CANARY}@h.example/simple", "alice-memory", "mcp", "--data-dir", "{vault}"]\n',
    )
    kept = (
        f"API_KEY={CANARY}-key uvx --from alice-memory alice-memory-session-start "
        f"--data-dir {shlex.quote(str(vault))} --format json"
    )
    hooks = _seed(_hooks(home), {"hooks": {"SessionStart": [_user_group(kept)]}})
    before = hooks.read_bytes()
    code, out, err = _install(home, None, capsys)
    assert code == 1, (out, err)
    assert hooks.read_bytes() == before
    lines = out.splitlines()
    assert "session_start: refused" in lines
    assert any(
        "does not print --format markdown, which Codex needs" in line for line in lines
    ), out
    assert not any(line.startswith("session_start_argv: ") for line in lines), out
    assert CANARY not in out + err
    assert "API_KEY" not in out + err


def test_codex_group_without_a_hooks_list_is_kept_and_alices_group_is_appended(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``{"matcher": "x"}`` in ``hooks.SessionStart`` has no ``hooks`` list, and Codex loads it.

    Install keeps it first, byte for byte, and appends Alice's group, so
    Codex's trust keys for the user's group do not move. Mutation: read the
    group's list with ``group["hooks"]`` in ``_codex_alice_handlers`` (assume
    it exists). A ``KeyError`` ends the run and this test fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    bare = {"matcher": "x"}
    _seed(_hooks(home), {"hooks": {"SessionStart": [bare]}})
    code, out, err = _install(home, vault, capsys)
    assert code == 0, (out, err)
    written = _read(_hooks(home))["hooks"]["SessionStart"]
    assert written == [bare, {"hooks": [_handler(vault)]}]
    assert WRITTEN in out.splitlines()
    again_code, again_out, again_err = _install(home, vault, capsys)
    assert again_code == 0, (again_out, again_err)
    assert _read(_hooks(home))["hooks"]["SessionStart"] == written
    assert UNCHANGED in again_out.splitlines()


# --- round 3: Alice's hook in hooks.json while config.toml holds hooks ----------------------


def _alice_hooks_json(vault: Path) -> dict:
    """The hooks.json install writes for ``vault``: Alice's handler in its own group."""

    return {"hooks": {"SessionStart": [{"hooks": [_handler(vault)]}]}}


def _twice_next(home: Path) -> str:
    return (
        f"next: add the hook above to {_config(home)} by hand, then remove Alice's "
        f"alice-memory-session-start hook from {_hooks(home)}. Codex runs the hooks in both "
        "files, so the brief would be injected twice. Then trust the new hook: open Codex, "
        'choose Review hooks at "Hooks need review", or use /hooks.'
    )


@pytest.mark.parametrize("dry_run", [False, True], ids=["written", "dry-run"])
def test_codex_toml_hooks_and_an_alice_hook_in_hooks_json_say_to_move_it(
    dry_run: bool, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With hooks in config.toml and Alice's hook already in hooks.json, the next line names both files.

    Codex runs the hooks in both, so adding the printed hook to config.toml
    alone would inject the brief twice. The next line says to add it to
    config.toml and then remove Alice's from hooks.json, with both paths.
    Install still never edits hooks.json: its bytes stay, and no backup is
    made. Mutation: keep the plain ``_CODEX_TOML_HOOKS_NEXT`` line on this
    path. The exact-line assertion fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    _seed(_config(home), _toml_hook())
    hooks = _seed(_hooks(home), _alice_hooks_json(vault))
    before = hooks.read_bytes()
    code, out, err = _install(home, vault, capsys, *(["--dry-run"] if dry_run else []))
    assert code == 1, (out, err)
    lines = out.splitlines()
    assert _twice_next(home) in lines
    assert not any(line.startswith("next: install did not write hooks.json") for line in lines)
    assert hooks.read_bytes() == before
    assert not _backups(vault, "hooks.json")
    snippet = out.split("snippet:\n", 1)[1]
    assert "[[hooks.SessionStart.hooks]]" in snippet and "--format markdown" in snippet


@pytest.mark.parametrize(
    "state",
    ["no-alice-hook-in-hooks-json", "hooks-json-codex-skips", "hooks-json-unreadable", "hooks-off"],
)
def test_codex_toml_hooks_keep_the_plain_next_line_when_nothing_runs_twice(
    state: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The move-it line is only for a hooks.json Codex loads with Alice's hook in it, with hooks on.

    A hooks.json with only the user's own hook, one Codex would skip (an
    ``http`` sibling), one that is a directory, or Alice's hook with
    ``features.hooks = false`` runs nothing twice, so the plain line stays
    and the run does not fail. Mutation: read hooks.json without the
    tolerant catch (``except (_MalformedHostFile, RecursionError, OSError)``
    becomes ``except ZeroDivisionError``). The ``hooks-json-codex-skips``
    and ``hooks-json-unreadable`` cases fail. Mutation: drop ``not
    plan.hooks_disabled``. The ``hooks-off`` case fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    config_text = _toml_hook()
    if state == "hooks-off":
        config_text += "\n[features]\nhooks = false\n"
    _seed(_config(home), config_text)
    alice = _alice_hooks_json(vault)
    if state == "no-alice-hook-in-hooks-json":
        _seed(_hooks(home), {"hooks": {"SessionStart": [_user_group("echo mine")]}})
    elif state == "hooks-json-codex-skips":
        skipped = json.loads(json.dumps(alice))
        skipped["hooks"]["Stop"] = [{"hooks": [{"type": "http", "url": "http://127.0.0.1:9/x"}]}]
        _seed(_hooks(home), skipped)
    elif state == "hooks-json-unreadable":
        _hooks(home).mkdir(parents=True)
    else:
        _seed(_hooks(home), alice)
    code, out, err = _install(home, vault, capsys)
    assert code == 1, (state, out, err)
    lines = out.splitlines()
    assert "action: written" in lines
    assert any(line.startswith("next: install did not write hooks.json") for line in lines)
    assert _twice_next(home) not in lines
    assert not any("injected twice" in line for line in lines), out


@pytest.mark.parametrize("hook", ["matching", "stale"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["written", "dry-run"])
def test_codex_toml_alice_hook_and_one_in_hooks_json_get_a_note(
    hook: str, dry_run: bool, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When config.toml already holds Alice's hook and hooks.json does too, a note says Codex runs both.

    The matching hook in config.toml is the unchanged case (exit 0); a stale
    one is refused (exit 1) with its own next line. Either way the note names
    hooks.json and says to remove the one there, and install does not touch
    hooks.json. With no Alice hook in hooks.json there is no note. Mutation:
    never add the note (``if json_alice and plan.toml_alice_handler is not
    None`` becomes ``if False``). Both hook cases fail.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    toml_hook = _toml_alice_hook(vault, timeout=30) if hook == "stale" else _toml_alice_hook(vault)
    _seed(_config(home), _STOP_HOOK + "\n" + toml_hook)
    alice_json = _seed(_hooks(home), _alice_hooks_json(vault))
    before = alice_json.read_bytes()
    extra = ["--dry-run"] if dry_run else []
    code, out, err = _install(home, vault, capsys, *extra)
    lines = out.splitlines()
    note = (
        f"note: {_hooks(home)} also holds the alice-memory-session-start hook, and Codex runs "
        "the hooks in config.toml and in hooks.json, so the brief is injected twice. Remove the "
        "one in hooks.json; install does not edit hooks.json when config.toml holds hooks"
    )
    assert note in lines
    assert code == (1 if hook == "stale" else 0), (out, err)
    assert alice_json.read_bytes() == before
    assert not _backups(vault, "hooks.json")

    # The control: the same config.toml with no Alice hook in hooks.json has no note.
    other = tmp_path / "other"
    _seed(_config(other), _STOP_HOOK + "\n" + toml_hook)
    _seed(_hooks(other), {"hooks": {"SessionStart": [_user_group("echo mine")]}})
    _, control_out, _ = _install(other, vault, capsys, *extra)
    assert "also holds the alice-memory-session-start hook" not in control_out


# --- round 3: the error code when config.toml holds hooks -----------------------------------

HOOK_BY_HAND_MESSAGE = (
    "Install wrote the MCP entry to config.toml but could not add the SessionStart hook, "
    "because config.toml already defines hooks; add the printed hook to config.toml by hand"
)


def _error_record(err: str) -> dict:
    return json.loads(err.strip().splitlines()[-1])


def _error(code: str) -> dict:
    return {"error": {"code": code, "message": _ERROR_CONTRACTS[code]}}


@pytest.mark.parametrize("hook", ["none", "stale"])
def test_codex_toml_hooks_refusal_has_its_own_error_code(
    hook: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Install wrote config.toml and refused only the hook: exit 1 with ``install_hook_by_hand``.

    The generic ``install_refused`` says the host config was left unchanged,
    which is not true here. The new message says the MCP entry was written,
    the hook was not, and why. It is used only on this path, for a config.toml
    with no Alice hook and for one with a stale hook. Mutation: emit
    ``install_refused`` on this path (drop the ``hook_by_hand`` branch in
    ``_run_install``). The record assertion fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    text = _toml_hook() if hook == "none" else _STOP_HOOK + "\n" + _toml_alice_hook(vault, timeout=30)
    _seed(_config(home), text)
    code, out, err = _install(home, vault, capsys)
    assert code == 1, (out, err)
    assert "action: written" in out.splitlines()
    assert _ERROR_CONTRACTS["install_hook_by_hand"] == HOOK_BY_HAND_MESSAGE
    assert _error_record(err) == {
        "error": {"code": "install_hook_by_hand", "message": HOOK_BY_HAND_MESSAGE}
    }
    assert "install_refused" not in err


def _scenario_unchanged(home: Path, vault: Path, capsys, monkeypatch) -> tuple[int, str, str]:
    _seed(_config(home), _toml_hook())
    _install(home, vault, capsys)
    return _install(home, vault, capsys)


def _scenario_dry_run(home: Path, vault: Path, capsys, monkeypatch) -> tuple[int, str, str]:
    _seed(_config(home), _toml_hook())
    return _install(home, vault, capsys, "--dry-run")


def _scenario_config_refused(home: Path, vault: Path, capsys, monkeypatch) -> tuple[int, str, str]:
    _seed(
        _config(home),
        '[mcp_servers.alice]\ncommand = "uvx"\nargs = ["alice-memory", "mcp"]\ncwd = "/tmp/work"\n',
    )
    return _install(home, vault, capsys)


def _scenario_hooks_json_skipped(home: Path, vault: Path, capsys, monkeypatch) -> tuple[int, str, str]:
    _seed(_hooks(home), _INVALID_HOOKS["handler-type-http"])
    return _install(home, vault, capsys)


def _scenario_hook_cannot_be_quoted(home: Path, vault: Path, capsys, monkeypatch) -> tuple[int, str, str]:
    # config.toml holds hooks, but the hook install would print cannot be written for a Windows
    # shell, so there is no hook to add and "config.toml already defines hooks" would be wrong.
    monkeypatch.setattr(host_launcher, "WINDOWS_HOOKS", True)
    _seed(_config(home), _toml_hook())
    return _install(home, vault.parent / "O'Brien" / ".alice", capsys)


def _scenario_two_hosts(home: Path, vault: Path, capsys, monkeypatch) -> tuple[int, str, str]:
    _seed(_config(home), _toml_hook())
    cursor = home / ".cursor" / "mcp.json"
    cursor.parent.mkdir(parents=True)
    cursor.write_text("{", encoding="utf-8")
    code = onramp_main(
        ["install", "--home", str(home), "--host", "codex", "--host", "cursor", "--data-dir", str(vault)]
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize(
    "scenario",
    [
        _scenario_unchanged,
        _scenario_dry_run,
        _scenario_config_refused,
        _scenario_hooks_json_skipped,
        _scenario_hook_cannot_be_quoted,
        _scenario_two_hosts,
    ],
    ids=lambda fn: fn.__name__.removeprefix("_scenario_"),
)
def test_every_other_codex_refusal_still_emits_install_refused(
    scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only a written config.toml with a refused hook has the new code; every other refusal is unchanged.

    A run that changed nothing, a dry run, a config.toml install cannot edit, a
    hooks.json Codex would skip, a hook that cannot be quoted for a Windows
    shell (no hook to add by hand), and a second refused host all keep
    ``install_refused``, exit 1. Mutation: give ``_HostResult`` the
    ``hook_by_hand`` kind whenever ``hook_problem`` is set (test
    ``hook_snippet is not None`` as ``hook_problem is not None``). The
    ``hook_cannot_be_quoted`` case fails. Mutation: set the kind on every
    refusal. Every case fails.
    """

    home, vault = tmp_path / "home", tmp_path / "vault"
    code, out, err = scenario(home, vault, capsys, monkeypatch)
    assert code == 1, (out, err)
    assert _error_record(err) == _error("install_refused"), err
