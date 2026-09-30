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
import shlex
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from alicebot_api import host_install, host_launcher, session_start_hook
from alicebot_api.host_install import host_file_map
from alicebot_api.host_launcher import hook_output_format, split_command
from alicebot_api.onramp import main as onramp_main
from tests.unit.codex_hook_helpers import commit_fact
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
    "raw",
    [
        b"\xef\xbb\xbf{}",
        b'{"hooks": {}, "hooks": {}}',
        b'{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x", "timeout": NaN}]}]}}',
        b"[]",
        b"not json",
        b"\xff\xfe",
        b'{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x", "timeout": 1e999}]}]}}',
    ],
    ids=["bom", "duplicate-key", "nan", "top-level-list", "not-json", "not-utf8", "infinity"],
)
def test_codex_hooks_file_must_be_strict_json(
    raw: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A BOM, a duplicate key, NaN, a non-object top level and non-JSON are refused.

    Mutation: parse with plain ``json.loads``. The duplicate key, NaN and
    BOM cases fail.
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
    not created. Mutation: write the hook into config.toml, or into hooks.json
    anyway. This test fails.
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
    ``--format`` in ``shown_hook_words``. This test fails.
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
