"""alice-memory install picks a launcher per host, and the hook follows the entry.

Why this exists. S4.6 (2026-09-23) made install write the installed
``alice-memory`` scripts when uvx is missing. The round-3 review found the
launcher was decided once for the whole run, from install's own PATH:

- an existing entry pointing at a deleted alice-memory was kept forever;
- an absolute uvx path that worked was warned about as "not on PATH";
- the hook was written from install's PATH even when the kept MCP entry
  ran something else, so the two files could start different programs;
- the receipt named scripts that were never written into a kept entry.

The rules now (ruling A3): the MCP entry as it will be written decides,
and the hook always derives from it. A live launcher is kept. A dead one is
swapped for the working launcher install found, keeping every other key,
the data dir and a backup, unless it is pinned or customised. With nothing
working, the entry is kept and a warning printed.

Review round 4, 2026-09-23. P1, reproduced by the tower: an entry whose
alice-memory sat in a uv cache was kept, and got a hook into the cache,
because a cached program only counted as dead when install had something
better to offer. A launcher in a uv cache is now dead with no exceptions,
pinned or not, and is rewritten like a new entry. S4: a script launcher is
alive only with its alice-memory-session-start beside it, so the entry and
the hook can no longer end on two versions and two vaults.

No binary is run. shutil.which and the script dirs are pinned per test
(tests/unit/launcher_helpers.py), and every "installed program" is an empty
0755 file in a temp dir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from alicebot_api.host_install import HERMES_BACKUP_MARKER, host_file_map
from alicebot_api.host_launcher import UVX_MISSING_WARNING_PREFIX
from alicebot_api.onramp import main as onramp_main
from tests.unit.launcher_helpers import executable, make_scripts, pin_launcher_search


def _install(capsys, home: Path, *extra: str) -> tuple[int, str, str]:
    code = onramp_main(["install", "--home", str(home), *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _block(out: str, host: str) -> str:
    for block in out.split("\n\n"):
        if block.startswith(f"host: {host}\n"):
            return block
    raise AssertionError(f"no receipt for {host}:\n{out}")


def _alice(host: str, path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if host == "openclaw":
        return doc["mcp"]["servers"]["alice"]
    return doc["mcpServers"]["alice"]


def _seed_entry(home: Path, host: str, entry: dict) -> Path:
    path = host_file_map(home.resolve())[host]["mcp"]
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"mcp": {"servers": {"alice": entry}}} if host == "openclaw" else {"mcpServers": {"alice": entry}}
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def _vault(tmp_path: Path) -> str:
    """A data dir install can back up into (backups live under the data dir)."""

    return str((tmp_path / "vault").resolve())


def _backups(data_dir: str, host: str, name: str) -> list[Path]:
    return list((Path(data_dir) / "backups" / "host-configs").glob(f"{host}-{name}{HERMES_BACKUP_MARKER}*"))


def _claude_hook(home: Path) -> list[str]:
    doc = json.loads(host_file_map(home.resolve())["claude-code"]["hooks"].read_text(encoding="utf-8"))
    return [handler["command"] for group in doc["hooks"]["SessionStart"] for handler in group["hooks"]]


def test_new_entries_use_the_installed_scripts_when_uvx_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """All four JSON hosts and both hooks: absolute script paths from one bin dir.

    Mutation: keep writing uvx, or write only the script's name. This test
    fails.
    """

    bin_dir = make_scripts(tmp_path / "venv" / "bin")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=bin_dir)
    home = tmp_path / "home"
    vault = (tmp_path / "vault").resolve()
    code, out, err = _install(capsys, home, "--data-dir", str(vault))
    assert code == 0, (out, err)
    assert UVX_MISSING_WARNING_PREFIX not in out
    script = str(bin_dir / "alice-memory")
    hook = str(bin_dir / "alice-memory-session-start")
    files = host_file_map(home.resolve())
    for host in ("claude-desktop", "claude-code", "cursor", "openclaw"):
        assert _alice(host, files[host]["mcp"]) == {
            "command": script,
            "args": ["mcp", "--data-dir", str(vault)],
        }
        assert f"launcher: {script} mcp" in _block(out, host)
    assert _claude_hook(home) == [f"{hook} --data-dir {vault}"]
    cursor = json.loads(files["cursor"]["hooks"].read_text(encoding="utf-8"))
    assert cursor["hooks"]["sessionStart"] == [{"command": f"{hook} --data-dir {vault}"}]
    assert f"session_start_launcher: {hook}" in _block(out, "cursor")


def test_hermes_new_entry_uses_the_installed_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Mutation: keep writing uvx for Hermes. This test fails."""

    bin_dir = make_scripts(tmp_path / "venv" / "bin")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=bin_dir)
    home = tmp_path / "home"
    vault = (tmp_path / "vault").resolve()
    code, out, err = _install(capsys, home, "--host", "hermes", "--data-dir", str(vault))
    assert code == 0, (out, err)
    config = home.resolve() / ".hermes" / "config.yaml"
    assert yaml.safe_load(config.read_text(encoding="utf-8"))["mcp_servers"]["alice"] == {
        "command": str(bin_dir / "alice-memory"),
        "args": ["mcp", "--data-dir", str(vault)],
        "env": {"ALICE_MEMORY_DATA_DIR": str(vault)},
    }


def test_uvx_on_path_keeps_writing_uvx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """uvx on PATH: today's entries, even with both scripts installed here.

    Mutation: prefer the scripts over uvx. This test fails.
    """

    bin_dir = make_scripts(tmp_path / "venv" / "bin")
    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx", interpreter=bin_dir)
    home = tmp_path / "home"
    code, out, err = _install(capsys, home, "--host", "claude-code", "--data-dir", str(tmp_path / "v"))
    assert code == 0, (out, err)
    alice = _alice("claude-code", host_file_map(home.resolve())["claude-code"]["mcp"])
    assert alice["command"] == "uvx"
    assert "launcher: uvx alice-memory mcp" in out
    assert "session_start_launcher: uvx --from alice-memory alice-memory-session-start" in out


def test_dead_script_entry_is_rewritten_to_the_working_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Review round 3 finding 2: a deleted alice-memory was kept forever.

    The entry's launcher fields change; env, timeout and the data dir stay;
    the old file is backed up; the receipt prints old -> new; the hook
    follows the new launcher. Mutation: keep a dead launcher. This test
    fails.
    """

    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    home = tmp_path / "home"
    vault = _vault(tmp_path)
    dead = str(tmp_path / "deleted-venv" / "bin" / "alice-memory")
    entry = {"command": dead, "args": ["mcp", "--data-dir", vault], "env": {"K": "1"}, "timeout": 9}
    path = _seed_entry(home, "claude-code", entry)

    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert _alice("claude-code", path) == {
        "command": "uvx",
        "args": ["alice-memory", "mcp", "--data-dir", vault],
        "env": {"K": "1"},
        "timeout": 9,
    }
    block = _block(out, "claude-code")
    assert f"launcher: {dead} mcp -> uvx alice-memory mcp" in block
    assert len(_backups(vault, "claude-code", ".claude.json")) == 1
    assert _claude_hook(home) == [
        f"uvx --from alice-memory alice-memory-session-start --data-dir {vault}"
    ]


def test_dead_pinned_entry_is_kept_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A pinned or customised launcher is the user's choice: kept, and warned.

    The hook follows the entry, pin included. Mutation: rewrite customised
    launchers too. This test fails.
    """

    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    home = tmp_path / "home"
    gone_uvx = str(tmp_path / "gone" / "uvx")
    entry = {"command": gone_uvx, "args": ["alice-memory==0.16.0", "mcp", "--data-dir", "/v"]}
    path = _seed_entry(home, "claude-code", entry)
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert _alice("claude-code", path) == entry
    assert "because it asks for alice-memory==0.16.0" in out
    assert _claude_hook(home) == [
        f"{gone_uvx} --from alice-memory==0.16.0 alice-memory-session-start --data-dir /v"
    ]


def test_no_working_launcher_keeps_the_entry_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Nothing runs here: the dead entry stays, and install says so.

    Mutation: rewrite to uvx by name anyway, or drop the warning. This test
    fails.
    """

    pin_launcher_search(monkeypatch, tmp_path, uvx=None)
    home = tmp_path / "home"
    dead = str(tmp_path / "deleted-venv" / "bin" / "alice-memory")
    entry = {"command": dead, "args": ["mcp", "--data-dir", "/v"]}
    path = _seed_entry(home, "claude-desktop", entry)
    code, out, err = _install(capsys, home, "--host", "claude-desktop")
    assert code == 0, (out, err)
    assert _alice("claude-desktop", path) == entry
    assert "install found no working launcher" in out
    assert out.startswith(UVX_MISSING_WARNING_PREFIX)


def test_live_absolute_uvx_is_kept_without_a_path_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Review round 3 finding 12: an absolute uvx that exists is live.

    uvx is not on PATH, the scripts are installed, and the entry's absolute
    uvx still works: kept, hook on the same uvx, no warning. Mutation: judge
    uvx only by PATH. This test fails.
    """

    bin_dir = make_scripts(tmp_path / "venv" / "bin")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=bin_dir)
    uvx = executable(tmp_path / "homebrew" / "bin" / "uvx")
    home = tmp_path / "home"
    entry = {"command": str(uvx), "args": ["alice-memory", "mcp", "--data-dir", "/v"]}
    path = _seed_entry(home, "claude-code", entry)
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert _alice("claude-code", path) == entry
    assert "not on PATH" not in out and "warning" not in out
    assert f"launcher: kept {uvx} alice-memory mcp" in out
    assert _claude_hook(home) == [
        f"{uvx} --from alice-memory alice-memory-session-start --data-dir /v"
    ]


def test_bare_uvx_entry_without_uvx_moves_to_the_scripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """``uvx`` by name, uvx gone, scripts installed: the entry moves to them.

    Mutation: keep a launcher that is not on PATH. This test fails.
    """

    bin_dir = make_scripts(tmp_path / "venv" / "bin")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=bin_dir)
    home = tmp_path / "home"
    vault = _vault(tmp_path)
    entry = {"command": "uvx", "args": ["alice-memory", "mcp", "--data-dir", vault]}
    path = _seed_entry(home, "cursor", entry)
    code, out, err = _install(capsys, home, "--host", "cursor")
    assert code == 0, (out, err)
    script = str(bin_dir / "alice-memory")
    assert _alice("cursor", path) == {"command": script, "args": ["mcp", "--data-dir", vault]}
    assert f"launcher: uvx alice-memory mcp -> {script} mcp (uvx is not on PATH)" in out


def test_script_entry_without_its_session_start_is_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Review round 4 S4: a script without its sibling is dead, so both files move.

    Round 3 kept the entry as live and skipped the hook, so --data-dir moved
    the entry to a new vault while the old hook stayed on the old one. Now
    the entry moves to the working launcher and the hook follows it.
    Mutation: judge a script launcher by alice-memory alone. This test fails.
    """

    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    lonely = make_scripts(tmp_path / "old-venv" / "bin", session_start=False)
    home = tmp_path / "home"
    old_vault, new_vault = _vault(tmp_path), str((tmp_path / "new").resolve())
    entry = {"command": str(lonely / "alice-memory"), "args": ["mcp", "--data-dir", old_vault]}
    path = _seed_entry(home, "claude-code", entry)
    code, out, err = _install(capsys, home, "--host", "claude-code", "--data-dir", new_vault)
    assert code == 0, (out, err)
    assert _alice("claude-code", path) == {
        "command": "uvx",
        "args": ["alice-memory", "mcp", "--data-dir", new_vault],
    }
    assert _claude_hook(home) == [
        f"uvx --from alice-memory alice-memory-session-start --data-dir {new_vault}"
    ]
    assert "alice-memory-session-start is missing or not executable" in out


def test_script_entry_without_its_session_start_and_nothing_working_skips_the_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """With nothing working, the dead entry stays and the hook is not pointed at a missing file.

    Mutation: write the hook onto the missing sibling. This test fails.
    """

    pin_launcher_search(monkeypatch, tmp_path, uvx=None)
    lonely = make_scripts(tmp_path / "old-venv" / "bin", session_start=False)
    home = tmp_path / "home"
    entry = {"command": str(lonely / "alice-memory"), "args": ["mcp", "--data-dir", _vault(tmp_path)]}
    path = _seed_entry(home, "claude-code", entry)
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert _alice("claude-code", path) == entry
    assert "session_start: skipped" in out
    assert not host_file_map(home.resolve())["claude-code"]["hooks"].exists()


def test_db_script_entry_without_its_session_start_moves_with_its_hook_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A --db entry whose script lost its sibling moves to the working launcher.

    Its hook takes the same launcher and keeps its own store. Mutation:
    judge the script by alice-memory alone. This test fails.
    """

    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    lonely = make_scripts(tmp_path / "old-venv" / "bin", session_start=False)
    home = tmp_path / "home"
    entry = {"command": str(lonely / "alice-memory"), "args": ["mcp", "--db", "/x.db"]}
    path = _seed_entry(home, "claude-code", entry)
    hooks = host_file_map(home.resolve())["claude-code"]["hooks"]
    hooks.parent.mkdir(parents=True, exist_ok=True)
    old = f"{lonely / 'alice-memory-session-start'} --data-dir /hook/store"
    hooks.write_text(json.dumps({"hooks": {"SessionStart": [{"command": old}]}}), encoding="utf-8")
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert _alice("claude-code", path) == {"command": "uvx", "args": ["alice-memory", "mcp", "--db", "/x.db"]}
    assert _claude_hook(home) == [
        "uvx --from alice-memory alice-memory-session-start --data-dir /hook/store"
    ]


def test_db_script_entry_with_nothing_working_keeps_the_hook_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A --db entry only repairs its hook, and never onto a missing script.

    Found while writing the round-3 docs, 2026-09-23. With nothing working
    the dead entry stays, and the flat v0.16.0 item is still repaired into
    Claude Code's shape, keeping its command. Mutation: skip the sibling
    check on the --db path. This test fails.
    """

    pin_launcher_search(monkeypatch, tmp_path, uvx=None)
    lonely = make_scripts(tmp_path / "old-venv" / "bin", session_start=False)
    home = tmp_path / "home"
    entry = {"command": str(lonely / "alice-memory"), "args": ["mcp", "--db", "/x.db"]}
    _seed_entry(home, "claude-code", entry)
    hooks = host_file_map(home.resolve())["claude-code"]["hooks"]
    hooks.parent.mkdir(parents=True, exist_ok=True)
    old = "uvx --from alice-memory alice-memory-session-start --data-dir /hook/store"
    hooks.write_text(json.dumps({"hooks": {"SessionStart": [{"command": old}]}}), encoding="utf-8")
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert _claude_hook(home) == [old]
    assert "so install kept the SessionStart hook's command" in out


# --- P1: a launcher in a uv cache is dead, with no exceptions ---------------------------


def _cached_scripts(tmp_path: Path) -> Path:
    return make_scripts(tmp_path / "cache" / "uv" / "archive-v0" / "K3y" / "bin")


def test_cached_script_entry_is_rewritten_to_uvx_by_name_with_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The tower's P1 setup: cached entry, no uvx, no scripts found.

    Round 3 kept the cache entry, added a hook into the cache, and printed
    a warning that the entries run uvx when they did not. Now the entry is
    rewritten to uvx by name, exactly as a new entry would be, the hook runs
    uvx, and nothing points into the cache. Mutation: treat a cached
    launcher like any dead one (kept when nothing works). This test fails.
    """

    cached = _cached_scripts(tmp_path)
    pin_launcher_search(monkeypatch, tmp_path, uvx=None)
    home = tmp_path / "home"
    vault = _vault(tmp_path)
    entry = {"command": str(cached / "alice-memory"), "args": ["mcp", "--data-dir", vault], "timeout": 5}
    path = _seed_entry(home, "claude-code", entry)
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert _alice("claude-code", path) == {
        "command": "uvx",
        "args": ["alice-memory", "mcp", "--data-dir", vault],
        "timeout": 5,
    }
    hook = _claude_hook(home)
    assert hook == [f"uvx --from alice-memory alice-memory-session-start --data-dir {vault}"]
    assert str(tmp_path / "cache") not in "".join(hook)
    block = _block(out, "claude-code")
    assert (
        f"launcher: {cached / 'alice-memory'} mcp -> uvx alice-memory mcp "
        f"({cached / 'alice-memory'} is inside a uv cache, which uv may delete; uvx is not on "
        "PATH here)"
    ) in block
    assert "session_start_launcher: uvx --from alice-memory alice-memory-session-start" in block
    assert out.startswith(UVX_MISSING_WARNING_PREFIX)


def test_cached_pinned_uvx_is_rewritten_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The pinned-or-customised exception does not apply to a cache path.

    Mutation: check customisation before the cache. This test fails.
    """

    cached_uvx = executable(tmp_path / "c" / "archive-v1" / "Id" / "bin" / "uvx")
    (tmp_path / "c" / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n", encoding="utf-8")
    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    home = tmp_path / "home"
    vault = _vault(tmp_path)
    entry = {"command": str(cached_uvx), "args": ["alice-memory==0.16.0", "mcp", "--data-dir", vault]}
    path = _seed_entry(home, "cursor", entry)
    code, out, err = _install(capsys, home, "--host", "cursor")
    assert code == 0, (out, err)
    assert _alice("cursor", path) == {"command": "uvx", "args": ["alice-memory", "mcp", "--data-dir", vault]}
    assert "is inside a uv cache" in out and "because it asks for" not in out


def test_cached_script_entry_moves_to_the_scripts_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """With a working pair outside the cache, the entry takes it, as a new entry would."""

    cached = _cached_scripts(tmp_path)
    good = make_scripts(tmp_path / "venv" / "bin")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=good)
    home = tmp_path / "home"
    vault = _vault(tmp_path)
    path = _seed_entry(home, "claude-code", {"command": str(cached / "alice-memory"), "args": ["mcp", "--data-dir", vault]})
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert _alice("claude-code", path)["command"] == str(good / "alice-memory")
    assert _claude_hook(home) == [f"{good / 'alice-memory-session-start'} --data-dir {vault}"]
    assert UVX_MISSING_WARNING_PREFIX not in out


@pytest.mark.parametrize(
    "command",
    ["/opt/py/bin/alice-memory", "C:\\Python312\\Scripts\\alice-memory.exe"],
    ids=["posix", "windows"],
)
def test_script_entry_is_install_shaped_on_a_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, command: str
) -> None:
    """An entry running the installed script is install's own: never refused.

    Neither path exists here, so each is rewritten to uvx, with its env
    kept. Mutation: accept only uvx as install's shape. This test fails.
    """

    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    home = tmp_path / "home"
    entry = {"command": command, "args": ["mcp", "--data-dir", _vault(tmp_path)], "env": {"X": "1"}}
    path = _seed_entry(home, "claude-desktop", entry)
    code, out, err = _install(capsys, home, "--host", "claude-desktop")
    assert code == 0, (out, err)
    assert _alice("claude-desktop", path)["env"] == {"X": "1"}
    assert f"launcher: {command} mcp -> uvx alice-memory mcp" in out


def test_mcpb_bundle_warns_when_uvx_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Review round 3 finding 11: the bundle runs uvx; say so when uvx is gone.

    Mutation: drop the bundle warning. This test fails.
    """

    bin_dir = make_scripts(tmp_path / "venv" / "bin")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=bin_dir)
    bundle = tmp_path / "alice.mcpb"
    code, out, err = _install(
        capsys, tmp_path / "home", "--host", "cursor", "--write-mcpb", str(bundle)
    )
    assert code == 0, (out, err)
    mcpb_block = next(block for block in out.split("\n\n") if block.startswith("mcpb: "))
    assert "warning: the bundle runs uvx, which is not on PATH here" in mcpb_block

    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    code, out, err = _install(
        capsys, tmp_path / "home2", "--host", "cursor", "--write-mcpb", str(tmp_path / "b.mcpb")
    )
    assert code == 0, (out, err)
    assert "the bundle runs uvx" not in out
