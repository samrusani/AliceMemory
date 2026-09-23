"""The SessionStart hook install writes runs exactly the argv install means.

Why this exists. Review round 3 of the S4 install work, 2026-09-23: the hook
command was an f-string, ``f"{script} --data-dir {data_dir}"``. A space
split the path into two words; a quote, ``$``, backtick, ``&``, ``;`` or a
parenthesis in the script path or data dir changed what the host's shell
ran, and could run something else. ``claude doctor`` cannot catch this:
it checks the settings shape, not what the command does.

The proof here is the shell itself. Each test installs with a script dir
and a data dir full of shell syntax, reads the written command back from
the host file, runs it with ``/bin/sh -c`` and ``bash -c`` against a stub
that records its argv, and checks the argv exactly, plus that nothing else
ran in the working directory. The stubs are two-line sh scripts in a temp
dir; no host binary and no uvx is run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from alicebot_api import host_launcher
from alicebot_api.host_install import host_file_map
from alicebot_api.onramp import _ERROR_CONTRACTS, main as onramp_main
from tests.unit.launcher_helpers import SH_ARGV_STUB, executable, make_scripts, pin_launcher_search

HOSTILE = "we 'q' $HOME &a ;touch pwned1 (p) $(touch pwned2) `touch pwned3` \"d\""
SHELLS = [shell for shell in ("/bin/sh", shutil.which("bash")) if shell and Path(shell).exists()]


def _install(capsys, home: Path, *extra: str) -> tuple[int, str, str]:
    code = onramp_main(["install", "--home", str(home), *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _hook_commands(home: Path) -> dict[str, list[str]]:
    files = host_file_map(home.resolve())
    claude = json.loads(files["claude-code"]["hooks"].read_text(encoding="utf-8"))
    cursor = json.loads(files["cursor"]["hooks"].read_text(encoding="utf-8"))
    return {
        "claude-code": [
            handler["command"]
            for group in claude["hooks"]["SessionStart"]
            for handler in group["hooks"]
        ],
        "cursor": [item["command"] for item in cursor["hooks"]["sessionStart"]],
    }


def _run(shell: str, command: str, sandbox: Path, argv_file: Path, path: str) -> list[str]:
    argv_file.unlink(missing_ok=True)
    subprocess.run(
        [shell, "-c", command],
        cwd=sandbox,
        env={"PATH": path, "ARGV_OUT": str(argv_file)},
        check=True,
        timeout=30,
    )
    return [part.decode("utf-8") for part in argv_file.read_bytes().split(b"\0")[:-1]]


@pytest.fixture
def hostile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """No uvx; the scripts live in a dir named with shell syntax."""

    bin_dir = make_scripts(tmp_path / f"bin {HOSTILE}", stub=SH_ARGV_STUB)
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=bin_dir)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    return bin_dir, tmp_path / f"data {HOSTILE}", sandbox


@pytest.mark.parametrize("shell", SHELLS)
def test_script_hooks_run_the_exact_argv_and_nothing_else(
    tmp_path: Path, capsys, hostile: tuple[Path, Path, Path], shell: str
) -> None:
    """Claude Code and Cursor hooks, script launcher, hostile paths.

    Mutation: build the hook with the old f-string. This test fails.
    """

    bin_dir, data, sandbox = hostile
    home = tmp_path / "home"
    code, out, err = _install(
        capsys, home, "--host", "claude-code", "--host", "cursor", "--data-dir", str(data)
    )
    assert code == 0, (out, err)
    expected = [str(bin_dir / "alice-memory-session-start"), "--data-dir", str(data.resolve())]
    commands = _hook_commands(home)
    assert len(commands["claude-code"]) == 1 and len(commands["cursor"]) == 1
    for host, [command] in commands.items():
        argv = _run(shell, command, sandbox, tmp_path / "argv.bin", "/usr/bin:/bin")
        assert argv == expected, (host, command)
    assert list(sandbox.iterdir()) == []


@pytest.mark.parametrize("shell", SHELLS)
def test_uvx_hook_runs_the_exact_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, shell: str
) -> None:
    """The uvx form with a hostile data dir. ``uvx`` here is a stub on PATH.

    Mutation: build the hook with the old f-string. This test fails.
    """

    stub_dir = tmp_path / "stub-bin"
    uvx = executable(stub_dir / "uvx", SH_ARGV_STUB)
    pin_launcher_search(monkeypatch, tmp_path, uvx=str(uvx))
    data = tmp_path / f"data {HOSTILE}"
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    home = tmp_path / "home"
    code, out, err = _install(capsys, home, "--host", "claude-code", "--data-dir", str(data))
    assert code == 0, (out, err)
    [command] = _hook_commands_claude(home)
    argv = _run(shell, command, sandbox, tmp_path / "argv.bin", f"{stub_dir}:/usr/bin:/bin")
    assert argv == [
        str(uvx),
        "--from",
        "alice-memory",
        "alice-memory-session-start",
        "--data-dir",
        str(data.resolve()),
    ]
    assert list(sandbox.iterdir()) == []


def _hook_commands_claude(home: Path) -> list[str]:
    doc = json.loads(host_file_map(home.resolve())["claude-code"]["hooks"].read_text(encoding="utf-8"))
    return [handler["command"] for group in doc["hooks"]["SessionStart"] for handler in group["hooks"]]


@pytest.mark.parametrize("shell", SHELLS)
def test_openclaw_add_line_runs_the_exact_argv(
    tmp_path: Path, capsys, hostile: tuple[Path, Path, Path], shell: str
) -> None:
    """The printed ``openclaw mcp add`` line is quoted the same way.

    Mutation: join the add line with spaces. This test fails.
    """

    bin_dir, data, sandbox = hostile
    stub_dir = tmp_path / "stub-bin"
    openclaw = executable(stub_dir / "openclaw", SH_ARGV_STUB)
    home = tmp_path / "home"
    code, out, err = _install(capsys, home, "--host", "openclaw", "--data-dir", str(data))
    assert code == 0, (out, err)
    line = next(line for line in out.splitlines() if line.startswith("openclaw mcp add"))
    argv = _run(shell, line, sandbox, tmp_path / "argv.bin", f"{stub_dir}:/usr/bin:/bin")
    assert argv == [
        str(openclaw),
        "mcp",
        "add",
        "alice",
        "--command",
        str(bin_dir / "alice-memory"),
        "--arg",
        "mcp",
        "--arg",
        "--data-dir",
        "--arg",
        str(data.resolve()),
    ]
    assert list(sandbox.iterdir()) == []


def test_quoted_hooks_stay_idempotent(
    tmp_path: Path, capsys, hostile: tuple[Path, Path, Path]
) -> None:
    """Two runs over hostile paths leave one Alice hook per host.

    A quoted command must still be recognised as Alice's, or the second run
    appends another. Mutation: recognise hooks with str.split only. This
    test fails.
    """

    _bin_dir, data, _sandbox = hostile
    home = tmp_path / "home"
    argv = ["--host", "claude-code", "--host", "cursor", "--data-dir", str(data)]
    assert _install(capsys, home, *argv)[0] == 0
    first = _hook_commands(home)
    code, out, err = _install(capsys, home, *argv)
    assert code == 0, (out, err)
    assert _hook_commands(home) == first
    assert out.count("session_start: already-present") == 2


def test_windows_refuses_a_hook_it_cannot_quote_for_every_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """On Windows, an apostrophe in the data dir refuses the hook, not the entry.

    The MCP entry is written; the hook is not, the receipt says why and
    prints the argv, and install exits 1 with install_refused. Mutation:
    drop ' from the refused set. This test fails.
    """

    bin_dir = make_scripts(tmp_path / "bin")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=bin_dir)
    monkeypatch.setattr(host_launcher, "WINDOWS_HOOKS", True)
    home = tmp_path / "home"
    data = tmp_path / "O'Brien" / ".alice"
    code, out, err = _install(capsys, home, "--host", "claude-code", "--data-dir", str(data))
    assert code == 1
    assert json.loads(err.splitlines()[-1]) == {
        "error": {"code": "install_refused", "message": _ERROR_CONTRACTS["install_refused"]}
    }
    files = host_file_map(home.resolve())["claude-code"]
    assert files["mcp"].is_file()
    assert not files["hooks"].exists()
    assert "session_start: refused" in out
    assert "session_start_reason:" in out and "'" in out
    assert "session_start_argv: " in out


def test_windows_quotes_a_data_dir_with_a_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Mutation: never quote on Windows. This test fails."""

    bin_dir = make_scripts(tmp_path / "bin")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=bin_dir)
    monkeypatch.setattr(host_launcher, "WINDOWS_HOOKS", True)
    home = tmp_path / "home"
    data = (tmp_path / "Sam Smith" / ".alice").resolve()
    code, out, err = _install(capsys, home, "--host", "claude-code", "--data-dir", str(data))
    assert code == 0, (out, err)
    assert _hook_commands_claude(home) == [
        f'{bin_dir / "alice-memory-session-start"} --data-dir "{data}"'
    ]
