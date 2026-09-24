"""alice-memory install: the Claude Code SessionStart hook Claude Code can read.

Why this exists. Until 2026-09-22, ``alice-memory install`` wrote Claude
Code's SessionStart hook in Cursor's flat shape,
``{"hooks": {"SessionStart": [{"command": "..."}]}}``. Claude Code needs the
handler nested in a matcher group, ``{"hooks": [{"type": "command",
"command": "..."}]}``. Claude Code 2.1.270 ``claude doctor`` reports the flat
entry as ``hooks.SessionStart.0: Hook matcher "hooks" must be an array of
hook entries; received undefined; matcher ignored.`` The hook never ran, so
the brief was never injected on Claude Code.

How it escaped. The writer test compared the written file to the writer's
own helper output, and ``docs/examples/claude-code-session-start-hooks.json``
pinned the same flat shape, so the test and the example agreed with the bug.
Nothing checked the file against Claude Code's documented schema or ran the
real binary.

These tests check the written file against the documented shape, check that
re-running install repairs the flat entry it used to write without touching
anyone else's hooks, and, when ALICE_TEST_REAL_HOSTS=1 and ``claude`` is on
PATH, ask the real binary.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from alicebot_api.host_install import SESSION_START_COMMAND, host_file_map
from alicebot_api.onramp import main as onramp_main

pytestmark = pytest.mark.usefixtures("uvx_on_path")
REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_CODE_EXAMPLE = REPO_ROOT / "docs" / "examples" / "claude-code-session-start-hooks.json"

# Claude Code hook handler types, from the error text of claude 2.1.270:
# "Valid types: command, prompt, agent, http, mcp_tool".
_HANDLER_TYPES = frozenset({"command", "prompt", "agent", "http", "mcp_tool"})
_DOCTOR_TIMEOUT_SECONDS = 60
REAL_HOSTS_ENV = "ALICE_TEST_REAL_HOSTS"


def _claude_code_hook_errors(doc: object) -> list[str]:
    """Check ``doc`` against Claude Code's documented settings hook shape.

    ``hooks.<Event>`` is a list of matcher groups. A group is an object with
    an optional string ``matcher`` and a ``hooks`` list of handlers. Each
    handler has a ``type``; a ``command`` handler has a non-empty ``command``
    string. Returns one message per violation, empty when the shape is valid.
    """

    errors: list[str] = []
    if not isinstance(doc, Mapping):
        return ["settings is not an object"]
    hooks = doc.get("hooks")
    if hooks is None:
        return errors
    if not isinstance(hooks, Mapping):
        return ["hooks is not an object"]
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            errors.append(f"hooks.{event} is not a list")
            continue
        for g_index, group in enumerate(groups):
            where = f"hooks.{event}.{g_index}"
            if not isinstance(group, Mapping):
                errors.append(f"{where} is not an object")
                continue
            if "matcher" in group and not isinstance(group["matcher"], str):
                errors.append(f"{where}.matcher is not a string")
            handlers = group.get("hooks")
            if not isinstance(handlers, list) or not handlers:
                errors.append(f"{where}.hooks must be a non-empty array of hook entries")
                continue
            for h_index, handler in enumerate(handlers):
                h_where = f"{where}.hooks.{h_index}"
                if not isinstance(handler, Mapping):
                    errors.append(f"{h_where} is not an object")
                    continue
                handler_type = handler.get("type")
                if handler_type not in _HANDLER_TYPES:
                    errors.append(f"{h_where}.type is missing or unknown")
                    continue
                if handler_type == "command":
                    command = handler.get("command")
                    if not isinstance(command, str) or not command.strip():
                        errors.append(f"{h_where}.command is not a non-empty string")
    return errors


def _expected_command(data_dir: Path) -> str:
    return (
        "uvx --from alice-memory alice-memory-session-start "
        f"--data-dir {data_dir.resolve()}"
    )


def _legacy_flat_entry(data_dir: Path) -> dict[str, str]:
    """The exact SessionStart item v0.16.0 wrote for Claude Code."""

    return {"command": _expected_command(data_dir)}


def _alice_handlers(doc: Mapping[str, object]) -> list[object]:
    """Every SessionStart item or handler whose command runs Alice's hook."""

    found: list[object] = []
    hooks = doc.get("hooks")
    assert isinstance(hooks, Mapping)
    for group in hooks.get("SessionStart", []):
        if isinstance(group, Mapping) and SESSION_START_COMMAND in str(group.get("command", "")):
            found.append(group)
        handlers = group.get("hooks") if isinstance(group, Mapping) else None
        for handler in handlers if isinstance(handlers, list) else []:
            if isinstance(handler, Mapping) and SESSION_START_COMMAND in str(
                handler.get("command", "")
            ):
                found.append(handler)
    return found


def _dump_like_writer(doc: object) -> str:
    return json.dumps(doc, ensure_ascii=True, indent=2) + "\n"


def _embedded_block(value: object, depth: int) -> str:
    """``value`` as the writer lays it out ``depth`` levels deep in the file.

    The first line carries no pad: a value that follows ``"key": `` starts
    mid-line, and a list item's pad is still matched as a suffix.
    """

    pad = "  " * depth
    lines = json.dumps(value, ensure_ascii=True, indent=2).splitlines()
    return "\n".join([lines[0], *(pad + line for line in lines[1:])])


def _install(home: Path, vault: Path, capsys) -> tuple[int, str, str]:
    code = onramp_main(
        ["install", "--home", str(home), "--data-dir", str(vault), "--host", "claude-code"]
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_validator_rejects_the_shapes_claude_code_ignores() -> None:
    """Guards the guard: the schema check must fail the shapes Claude Code drops.

    If ``_claude_code_hook_errors`` returned ``[]`` for everything, the
    written-file test below would pass on the flat shape. Mutation: make the
    validator return ``[]``. This test fails.
    """

    vault = Path("/tmp/alice-validator-vault")
    flat = {"hooks": {"SessionStart": [_legacy_flat_entry(vault)]}}
    untyped = {"hooks": {"SessionStart": [{"hooks": [{"command": "echo hi"}]}]}}
    nested = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": _expected_command(vault)}]}
            ]
        }
    }
    assert _claude_code_hook_errors(flat) == [
        "hooks.SessionStart.0.hooks must be a non-empty array of hook entries"
    ]
    assert _claude_code_hook_errors(untyped) == [
        "hooks.SessionStart.0.hooks.0.type is missing or unknown"
    ]
    assert _claude_code_hook_errors(nested) == []


def test_written_claude_code_settings_match_the_documented_hook_shape(
    tmp_path: Path, capsys
) -> None:
    """The settings.json install writes, read back from disk, has the nested shape.

    Mutation: write ``{"command": ...}`` straight into ``SessionStart`` again
    (the v0.16.0 shape), or drop ``"type": "command"``. This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    settings_path = host_file_map(home.resolve())["claude-code"]["hooks"]
    written = json.loads(settings_path.read_text(encoding="utf-8"))

    assert _claude_code_hook_errors(written) == []
    assert written == {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": _expected_command(vault)}]}
            ]
        }
    }
    assert "session_start: added" in out


def test_reinstall_repairs_the_flat_entry_and_leaves_other_hooks_byte_identical(
    tmp_path: Path, capsys
) -> None:
    """A v0.16.0 settings.json is repaired in place. Other hooks keep their bytes.

    The seed has the flat entry v0.16.0 wrote, a second stale flat Alice
    entry, a third-party SessionStart group, a third-party PreToolUse hook,
    and unrelated settings. After install there is exactly one Alice handler,
    in the nested shape, where the first flat entry was; the third-party
    blocks are byte-identical; a second run changes nothing.

    Mutation: append the nested group without removing the flat entries, or
    rebuild the whole SessionStart list from scratch. This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    settings_path = host_file_map(home.resolve())["claude-code"]["hooks"]
    third_party_group = {
        "matcher": "startup",
        "hooks": [
            {"type": "command", "command": "~/bin/print-branch.sh", "timeout": 5}
        ],
    }
    pre_tool_use = [
        {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "~/bin/guard-rm.sh"}],
        }
    ]
    seed = {
        "model": "opus",
        "permissions": {"allow": ["Bash(git status)"]},
        "hooks": {
            "SessionStart": [
                _legacy_flat_entry(vault),
                third_party_group,
                {"command": SESSION_START_COMMAND},
            ],
            "PreToolUse": pre_tool_use,
        },
    }
    settings_path.parent.mkdir(parents=True)
    seed_text = _dump_like_writer(seed)
    settings_path.write_text(seed_text, encoding="utf-8")

    third_party_block = _embedded_block(third_party_group, 3)
    pre_tool_use_block = _embedded_block(pre_tool_use, 2)
    # Guards the guard: the seed really is the broken v0.16.0 shape, and the
    # byte-level needles really occur in it, so the assertions below cannot
    # pass because the fixture drifted.
    assert _claude_code_hook_errors(seed) == [
        "hooks.SessionStart.0.hooks must be a non-empty array of hook entries",
        "hooks.SessionStart.2.hooks must be a non-empty array of hook entries",
    ]
    assert third_party_block in seed_text
    assert pre_tool_use_block in seed_text

    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    after_text = settings_path.read_text(encoding="utf-8")
    after = json.loads(after_text)

    assert _claude_code_hook_errors(after) == []
    assert _alice_handlers(after) == [{"type": "command", "command": _expected_command(vault)}]
    assert after["hooks"]["SessionStart"] == [
        {"hooks": [{"type": "command", "command": _expected_command(vault)}]},
        third_party_group,
    ]
    assert after["hooks"]["PreToolUse"] == pre_tool_use
    assert after["model"] == "opus"
    assert after["permissions"] == {"allow": ["Bash(git status)"]}
    assert third_party_block in after_text
    assert pre_tool_use_block in after_text
    assert "session_start: updated" in out

    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert settings_path.read_text(encoding="utf-8") == after_text
    assert "session_start: already-present" in out


def test_reinstall_updates_alice_inside_a_shared_group_without_touching_the_neighbour(
    tmp_path: Path, capsys
) -> None:
    """An Alice handler someone put inside their own group is updated in place.

    The group keeps its matcher and its other handler; no second group is
    appended. The audit also found the old merge replaced the whole group.
    Mutation: replace the whole group that contains Alice, or append a new
    Alice group instead of updating the handler. This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    settings_path = host_file_map(home.resolve())["claude-code"]["hooks"]
    neighbour = {"type": "command", "command": "~/bin/print-branch.sh"}
    stale = {
        "type": "command",
        "command": "uvx --from alice-memory alice-memory-session-start --data-dir /old/vault",
        "timeout": 30,
    }
    seed = {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [neighbour, stale]}]}}
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(_dump_like_writer(seed), encoding="utf-8")

    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    after = json.loads(settings_path.read_text(encoding="utf-8"))
    assert _claude_code_hook_errors(after) == []
    assert after["hooks"]["SessionStart"] == [
        {
            "matcher": "startup",
            "hooks": [
                neighbour,
                {"type": "command", "command": _expected_command(vault), "timeout": 30},
            ],
        }
    ]
    assert "session_start: updated" in out


def test_claude_code_example_file_has_the_documented_shape() -> None:
    """The copy-paste example is the nested shape too.

    Mutation: restore the flat ``{"command": ...}`` example. This test fails.
    """

    example = json.loads(CLAUDE_CODE_EXAMPLE.read_text(encoding="utf-8"))
    assert _claude_code_hook_errors(example) == []
    assert len(_alice_handlers(example)) == 1


def _real_claude_env(tmp_path: Path) -> dict[str, str]:
    """Environment for the real binary: its own config dir, never the user's.

    CLAUDE_CONFIG_DIR moves Claude Code's user config into a temp dir. HOME is
    moved too, except on macOS, where a temp HOME makes Claude Code ask the
    user for the login Keychain.
    """

    env = dict(os.environ)
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    if sys.platform != "darwin":
        fake_home = tmp_path / "claude-home"
        fake_home.mkdir()
        env["HOME"] = str(fake_home)
    return env


def _run_claude(args: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    claude = shutil.which("claude")
    assert claude is not None
    proc = subprocess.run(
        [claude, *args],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_DOCTOR_TIMEOUT_SECONDS,
        check=False,
    )
    return proc.stdout + proc.stderr


def _invalid_settings_lines(report: str) -> list[str]:
    """The lines doctor prints under its "Invalid settings" heading."""

    found: list[str] = []
    inside = False
    for line in report.splitlines():
        if line.strip() == "Invalid settings":
            inside = True
            continue
        if inside:
            if not line.strip():
                inside = False
                continue
            found.append(line)
    return found


def _names_file(line: str, path: Path) -> bool:
    """True when a doctor line names ``path`` in either its given or resolved form."""

    return str(path) in line or str(path.resolve()) in line


@pytest.mark.skipif(
    os.environ.get(REAL_HOSTS_ENV) != "1",
    reason=f"set {REAL_HOSTS_ENV}=1 to run the real claude binary",
)
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude binary not on PATH")
def test_real_claude_doctor_accepts_the_written_settings(tmp_path: Path, capsys) -> None:
    """The real ``claude doctor`` lists no invalid setting in the written file.

    Opt-in: runs only with ALICE_TEST_REAL_HOSTS=1 and ``claude`` on PATH.
    The written file is copied to ``<project>/.claude/settings.json`` and
    ``claude doctor`` runs in ``<project>``, which reads it as project
    settings and makes no API call. Alice's own files go under ``--home``;
    Claude Code's user config goes to CLAUDE_CONFIG_DIR (see
    _real_claude_env). A control project with the v0.16.0 flat shape must
    be listed under "Invalid settings", which proves doctor read the file.
    The version is printed and put in every failure message. Mutation:
    write the flat shape. This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    code, _out, err = _install(home, vault, capsys)
    assert code == 0, err
    written = host_file_map(home.resolve())["claude-code"]["hooks"]
    env = _real_claude_env(tmp_path)
    version = _run_claude(["--version"], cwd=tmp_path, env=env).strip()
    print(f"claude version: {version}")

    control = tmp_path / "control-project"
    (control / ".claude").mkdir(parents=True)
    control_settings = control / ".claude" / "settings.json"
    control_settings.write_text(
        _dump_like_writer({"hooks": {"SessionStart": [_legacy_flat_entry(vault)]}}),
        encoding="utf-8",
    )
    control_report = _run_claude(["doctor"], cwd=control, env=env)
    control_lines = [
        line
        for line in _invalid_settings_lines(control_report)
        if _names_file(line, control_settings)
    ]
    assert control_lines, (version, control_report)

    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    project_settings = project / ".claude" / "settings.json"
    project_settings.write_bytes(written.read_bytes())
    report = _run_claude(["doctor"], cwd=project, env=env)
    offending = [
        line for line in _invalid_settings_lines(report) if _names_file(line, project_settings)
    ]
    assert offending == [], (version, report)


def test_fresh_install_and_rerun_keep_only_the_session_start_hook_key(
    tmp_path: Path, capsys
) -> None:
    """A fake home gets one hooks key per host, fresh and on the re-run.

    Claude Code's hooks object is exactly ``SessionStart``. Cursor's is
    exactly ``sessionStart``. The re-run goes through
    ``_merge_claude_code_session_start``. Mutation: add a SessionEnd group
    in that function. This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    for label in ("fresh install", "re-run"):
        code = onramp_main(
            [
                "install",
                "--home",
                str(home),
                "--data-dir",
                str(vault),
                "--host",
                "claude-code",
                "--host",
                "cursor",
            ]
        )
        captured = capsys.readouterr()
        assert code == 0, (label, captured.err)
        files = host_file_map(home.resolve())
        claude = json.loads(files["claude-code"]["hooks"].read_text(encoding="utf-8"))
        cursor = json.loads(files["cursor"]["hooks"].read_text(encoding="utf-8"))
        assert set(claude["hooks"]) == {"SessionStart"}, label
        assert set(cursor["hooks"]) == {"sessionStart"}, label
        assert claude["hooks"]["SessionStart"], label
        assert cursor["hooks"]["sessionStart"], label
