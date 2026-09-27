"""OpenCode install writes strict JSON and refuses JSONC.

OpenCode v1.18.32 is MCP only and opt-in. These tests do not start
opencode unless ALICE_TEST_REAL_HOSTS=1.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from alicebot_api.host_install import BRIEF_HINT, host_file_map
from alicebot_api.onramp import main as onramp_main

from tests.unit.launcher_helpers import executable, make_scripts, pin_launcher_search


_SCHEMA = "https://opencode.ai/config.json"


def _install(home: Path, vault: Path, capsys, *extra: str, with_home: bool = True) -> tuple[int, str, str]:
    argv = ["install", "--data-dir", str(vault), "--host", "opencode", *extra]
    if with_home:
        argv[1:1] = ["--home", str(home)]
    code = onramp_main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _files(home: Path, config_home: Path | None = None) -> dict[str, Path]:
    return host_file_map(home, config_home=config_home)["opencode"]


def _pin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    scripts = make_scripts(tmp_path / "scripts")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=scripts)
    return scripts


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _backups(vault: Path) -> list[Path]:
    directory = vault / "backups" / "host-configs"
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.is_file())


def test_opencode_paths(tmp_path: Path) -> None:
    """OpenCode uses .config on every platform. XDG applies only without --home."""

    home = tmp_path / "home"
    for platform in ("linux", "darwin", "win32"):
        files = host_file_map(home, platform)
        assert files["opencode"]["mcp"] == home / ".config" / "opencode" / "opencode.json"
        assert files["opencode"]["jsonc"] == home / ".config" / "opencode" / "opencode.jsonc"
        assert files["opencode"]["legacy"] == home / ".config" / "opencode" / "config.json"
        assert files["opencode"]["home_json"] == home / ".opencode" / "opencode.json"
        assert files["opencode"]["home_jsonc"] == home / ".opencode" / "opencode.jsonc"
    custom = tmp_path / "xdg"
    mapped = _files(home, custom)
    assert mapped["mcp"] == custom / "opencode" / "opencode.json"


def test_opencode_is_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Omitted --host does not write OpenCode. The default hosts are unchanged."""

    _pin(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "proc-home"))
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    code = onramp_main(["install", "--home", str(home), "--data-dir", str(vault)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert not _files(home)["mcp"].exists()
    assert "host: opencode" not in captured.out


def test_opencode_new_file_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripts = _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert "format: json" in out
    assert "session_start: none" in out
    assert f"note: {BRIEF_HINT}" in out
    doc = json.loads(_files(home)["mcp"].read_text(encoding="utf-8"))
    assert doc["$schema"] == _SCHEMA
    entry = doc["mcp"]["alice"]
    assert entry["type"] == "local"
    assert "environment" not in entry
    assert entry["command"] == [str(scripts / "alice-memory"), "mcp", "--data-dir", str(vault.resolve())]


def test_opencode_json_rerun_keeps_user_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripts = _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    command = [str(scripts / "alice-memory"), "mcp", "--data-dir", str(vault.resolve())]
    seeded = {
        "$schema": _SCHEMA,
        "autoupdate": False,
        "mcp": {
            "other": {"type": "local", "command": ["node", "other.js"], "enabled": False},
            "alice": {
                "timeout": 5000,
                "type": "local",
                "command": command,
                "enabled": True,
                "cwd": "/work",
                "environment": {"FOO": "kept"},
            },
        },
    }
    path = _files(home)["mcp"]
    _write(path, json.dumps(seeded, indent=2) + "\n")
    before = path.read_bytes()
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert "action: unchanged" in out
    assert path.read_bytes() == before
    assert not _backups(vault)


def test_opencode_every_rewrite_backs_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripts = _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    first = tmp_path / "vault-a"
    second = tmp_path / "vault-b"
    path = _files(home)["mcp"]
    seed = {
        "mcp": {
            "alice": {
                "type": "local",
                "command": [str(scripts / "alice-memory"), "mcp", "--data-dir", str(first)],
            }
        }
    }
    _write(path, json.dumps(seed) + "\n")
    seed_bytes = path.read_bytes()
    code, _out, err = _install(home, second, capsys)
    assert code == 0, err
    after_first = path.read_bytes()
    code, _out, err = _install(home, tmp_path / "vault-c", capsys)
    assert code == 0, err
    backups = _backups(second) + _backups(tmp_path / "vault-c")
    # Each rewrite backs up into the data dir it is moving to.
    assert len(_backups(second)) == 1
    assert _backups(second)[0].read_bytes() == seed_bytes
    assert len(_backups(tmp_path / "vault-c")) == 1
    assert _backups(tmp_path / "vault-c")[0].read_bytes() == after_first
    assert backups


def test_opencode_dry_run_masks_command_array(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pin(monkeypatch, tmp_path)
    uvx = executable(tmp_path / "bin" / "uvx")
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    secret_url = "https://" + "u:" + "tok" + "@example.test/idx"
    sibling = "sibling-mcp"
    path = _files(home)["mcp"]
    _write(
        path,
        json.dumps(
            {
                "mcp": {
                    sibling: {"type": "local", "command": ["node", "s.js"]},
                    "alice": {
                        "type": "local",
                        "command": [
                            str(uvx),
                            "--index",
                            secret_url,
                            "alice-memory",
                            "mcp",
                            "--data-dir",
                            str(vault.resolve()),
                        ],
                    },
                }
            }
        ),
    )
    before = path.read_bytes()
    code, out, err = _install(home, vault, capsys, "--dry-run")
    assert code == 0, err
    assert "hidden:" in out
    assert "u:" not in out
    assert "tok" not in out
    assert sibling not in out
    assert path.read_bytes() == before
    assert not _backups(vault)


def test_opencode_uv_cache_launcher_replaced_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripts = _pin(monkeypatch, tmp_path)
    cached = executable(tmp_path / "cache" / "uv" / "archive-v0" / "A1b2" / "bin" / "uvx")
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    index = "https://" + "example.test/simple"
    path = _files(home)["mcp"]
    _write(
        path,
        json.dumps(
            {
                "mcp": {
                    "alice": {
                        "type": "local",
                        "command": [
                            str(cached),
                            "--with",
                            "x",
                            "--index",
                            index,
                            "alice-memory",
                            "mcp",
                            "--data-dir",
                            str(vault),
                        ],
                    }
                }
            }
        ),
    )
    code, _out, err = _install(home, vault, capsys)
    assert code == 0, err
    command = json.loads(path.read_text(encoding="utf-8"))["mcp"]["alice"]["command"]
    assert command == [str(scripts / "alice-memory"), "mcp", "--data-dir", str(vault.resolve())]
    assert "--with" not in command
    assert index not in command


def test_opencode_pinned_launcher_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pin(monkeypatch, tmp_path)
    uvx = executable(tmp_path / "bin" / "uvx")
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    command = [str(uvx), "--python", "3.12", "alice-memory", "mcp", "--data-dir", str(vault.resolve())]
    path = _files(home)["mcp"]
    _write(path, json.dumps({"mcp": {"alice": {"type": "local", "command": command}}}))
    before = path.read_bytes()
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert "action: unchanged" in out
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "remote", "command": ["uvx", "alice-memory", "mcp"]},
        {"enabled": False},
        {"type": "local", "command": ["{env:HOME}/uvx", "alice-memory", "mcp"]},
        {"type": "local", "command": ["uvx", "alice-memory", "mcp"], "args": ["--data-dir", "/x"]},
    ],
)
def test_opencode_foreign_entry_refused(
    entry: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _files(home)["mcp"]
    _write(path, json.dumps({"mcp": {"alice": entry}}))
    before = path.read_bytes()
    code, out, err = _install(home, vault, capsys)
    assert code == 1, out
    assert "action: refused" in out or "action: would-refuse" in out
    assert "it has no string command" not in out
    assert "command array" in out
    assert "next:" in out
    assert path.read_bytes() == before
    assert not _backups(vault)


def test_json_hosts_still_refuse_an_array_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = home / ".cursor" / "mcp.json"
    _write(
        path,
        json.dumps(
            {"mcpServers": {"alice": {"command": ["uvx", "alice-memory", "mcp"], "args": ["--data-dir", "/x"]}}}
        ),
    )
    before = path.read_bytes()
    code = onramp_main(
        ["install", "--home", str(home), "--data-dir", str(vault), "--host", "cursor"]
    )
    captured = capsys.readouterr()
    assert code == 1, captured.out
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "raw",
    [
        b'{ "mcp": { "alice": 1 } } // comment\n',
        b'{"mcp": {"alice": 1,}}\n',
        b'{"mcp": {"alice": 1, "alice": 2}}\n',
        b'{"mcp": {"n": NaN}}\n',
        b'{"mcp": {"n": 1e999}}\n',
        b'\xef\xbb\xbf{"mcp": {}}\n',
    ],
)
def test_opencode_format_detection(
    raw: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _files(home)["mcp"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    before = path.read_bytes()
    code, out, _err = _install(home, vault, capsys)
    assert code == 1
    assert "not strict JSON" in out
    assert "PR B will edit this file. Until then, add the snippet by hand." in out
    assert "Fix opencode.json" not in out
    assert path.read_bytes() == before
    assert not _backups(vault)


def test_opencode_jsonc_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Until the text writer lands, any opencode.jsonc refuses the host."""

    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _files(home)["jsonc"]
    _write(path, '{ "mcp": {} }\n')
    before = path.read_bytes()
    code, out, _err = _install(home, vault, capsys)
    assert code == 1
    assert "jsonc" in out.lower()
    assert "next:" in out
    assert path.read_bytes() == before
    assert not _files(home)["mcp"].exists()


def test_opencode_second_alice_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    files = _files(home)
    alice = {"type": "local", "command": ["uvx", "alice-memory", "mcp", "--data-dir", "/v"]}
    _write(files["mcp"], json.dumps({"mcp": {"alice": alice, "servers": {"alice": {"type": "local"}}}}))
    before = files["mcp"].read_bytes()
    code, out, _err = _install(home, vault, capsys)
    assert code == 1
    assert "Keep one alice entry" in out
    assert files["mcp"].read_bytes() == before

    files["mcp"].unlink()
    _write(files["mcp"], json.dumps({"mcp": {"alice": alice}}))
    _write(files["legacy"], json.dumps({"mcp": {"alice": alice}}))
    code, out, _err = _install(home, vault, capsys)
    assert code == 1
    assert "Keep one alice entry" in out

    files["legacy"].unlink()
    _write(files["home_json"], json.dumps({"mcp": {"alice": alice}}))
    code, out, _err = _install(home, vault, capsys)
    assert code == 1
    assert "Keep one alice entry" in out

    files["home_json"].unlink()
    files["mcp"].write_text("{", encoding="utf-8")
    broken = files["mcp"].read_bytes()
    code, out, _err = _install(home, vault, capsys)
    assert code == 1
    assert files["mcp"].read_bytes() == broken

    files["mcp"].write_text("{}\n", encoding="utf-8")
    legacy = files["mcp"].parent / "config"
    legacy.write_text("x = 1\n", encoding="utf-8")
    code, out, _err = _install(home, vault, capsys)
    assert code == 1
    assert (
        "next: nothing was written for opencode. "
        f"Remove or migrate {legacy} with OpenCode first, then run install again."
    ) in out
    assert "Keep one alice entry" not in out


def test_opencode_symlink_written_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    real = tmp_path / "real.json"
    real.write_text("{}\n", encoding="utf-8")
    link = _files(home)["mcp"]
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real)
    code, _out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert link.is_symlink()
    doc = json.loads(real.read_text(encoding="utf-8"))
    assert doc["mcp"]["alice"]["type"] == "local"


def test_opencode_xdg_only_without_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pin(monkeypatch, tmp_path)
    home = tmp_path / "proc-home"
    xdg = tmp_path / "xdg"
    vault = tmp_path / "vault"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    code, _out, err = _install(home, vault, capsys, with_home=False)
    assert code == 0, err
    assert (xdg / "opencode" / "opencode.json").is_file()
    assert not (home / ".config" / "opencode" / "opencode.json").exists()

    monkeypatch.setenv("XDG_CONFIG_HOME", "~/x")
    code, out, _err = _install(home, vault, capsys, with_home=False)
    assert code == 1
    assert "XDG_CONFIG_HOME" in out
    assert (
        "next: nothing was written for opencode. "
        "Set XDG_CONFIG_HOME to an absolute path, or pass --home, then run install again."
    ) in out
    monkeypatch.setenv("XDG_CONFIG_HOME", "C:foo")
    code, out, _err = _install(home, vault, capsys, with_home=False)
    assert code == 1
    monkeypatch.setenv("XDG_CONFIG_HOME", "\\foo")
    code, out, _err = _install(home, vault, capsys, with_home=False)
    assert code == 1

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ignored"))
    other = tmp_path / "flag-home"
    code, _out, err = _install(other, vault, capsys)
    assert code == 0, err
    assert _files(other)["mcp"].is_file()
    assert not (tmp_path / "ignored" / "opencode" / "opencode.json").exists()


def test_opencode_empty_xdg_config_home_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty XDG_CONFIG_HOME counts as unset."""

    _pin(monkeypatch, tmp_path)
    home = tmp_path / "proc-home"
    vault = tmp_path / "vault"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    code, _out, err = _install(home, vault, capsys, with_home=False)
    assert code == 0, err
    assert (home / ".config" / "opencode" / "opencode.json").is_file()


def test_opencode_config_env_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A set OPENCODE_CONFIG* variable is named in the receipt."""

    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    names = ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT")
    for name in names:
        monkeypatch.setenv(name, str(tmp_path / name))
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    for name in names:
        assert f"note: {name} is set, so OpenCode may load config that install did not write" in out


def test_opencode_string_command_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A string command is not an OpenCode array.

    Mutation: the parser accepts a string command. Install rewrites the
    file and exits 0. This test fails.
    """

    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _files(home)["mcp"]
    _write(path, json.dumps({"mcp": {"alice": {"type": "local", "command": "uvx alice-memory mcp"}}}))
    before = path.read_bytes()
    code, out, _err = _install(home, vault, capsys)
    assert code == 1, out
    assert "it has no command array" in out
    assert path.read_bytes() == before
    assert not _backups(vault)


def test_opencode_home_jsonc_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """~/.opencode/opencode.jsonc is part of the scan.

    Mutation: drop that path from the scan. Install writes opencode.json.
    This test fails.
    """

    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    files = _files(home)
    path = files["home_jsonc"]
    _write(path, '{ "mcp": {} }\n')
    before = path.read_bytes()
    code, out, _err = _install(home, vault, capsys)
    assert code == 1
    assert "jsonc" in out.lower()
    assert path.read_bytes() == before
    assert not files["mcp"].exists()
    assert not _backups(vault)


def test_opencode_unreadable_jsonc_refuses_only_that_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mode 000 on opencode.jsonc refuses OpenCode and still prints every receipt.

    Mutation: let the OSError escape the host. Claude Code is already
    written and the receipt is missing. This test fails.
    """

    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    jsonc = _files(home)["jsonc"]
    _write(jsonc, "{}\n")
    original = jsonc.read_bytes()
    jsonc.chmod(0)
    try:
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
                "opencode",
            ]
        )
        captured = capsys.readouterr()
    finally:
        jsonc.chmod(0o644)
    assert code == 1
    assert "alice_memory_failed" not in captured.err
    assert "install_refused" in captured.err
    assert "host: claude-code" in captured.out
    assert "action: written" in captured.out
    assert "host: opencode" in captured.out
    assert "unscannable" in captured.out
    assert "PermissionError" not in captured.out
    assert (home / ".claude.json").is_file()
    assert jsonc.read_bytes() == original
    assert not _files(home)["mcp"].exists()


@pytest.mark.parametrize("kind", ["dangling", "loop"])
def test_opencode_broken_symlink_refuses_the_host(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dangling or looping opencode.json is not replaced by a regular file.

    Mutation: follow the link and create a file. This test fails.
    """

    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _files(home)["mcp"]
    path.parent.mkdir(parents=True)
    if kind == "dangling":
        path.symlink_to(tmp_path / "nowhere.json")
    else:
        other = path.parent / "other.json"
        other.symlink_to(path)
        path.symlink_to(other)
    code, out, _err = _install(home, vault, capsys)
    assert code == 1
    assert "symbolic link whose target is missing or loops" in out
    assert path.is_symlink()
    assert not path.is_file()


def test_opencode_directory_refuses_dry_run_and_real_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A directory at opencode.json is refused before a dry run can succeed."""

    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _files(home)["mcp"]
    path.mkdir(parents=True)
    code, out, _err = _install(home, vault, capsys, "--dry-run")
    assert code == 1
    assert "not a regular file" in out
    assert path.is_dir()
    code, out, _err = _install(home, vault, capsys)
    assert code == 1
    assert "not a regular file" in out
    assert path.is_dir()


def test_opencode_servers_alice_refuses_on_the_first_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """mcp.servers.alice alone is a second alice. Install does not add mcp.alice."""

    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _files(home)["mcp"]
    _write(
        path,
        json.dumps({"mcp": {"servers": {"alice": {"type": "local", "command": ["uvx", "alice-memory", "mcp"]}}}}),
    )
    before = path.read_bytes()
    code, out, _err = _install(home, vault, capsys)
    assert code == 1
    assert "Keep one alice entry" in out
    assert path.read_bytes() == before
    assert "alice" not in json.loads(path.read_text(encoding="utf-8"))["mcp"]
    assert not _backups(vault)


@pytest.mark.parametrize("token", ["{env:" + "HOME}", "{file:" + "x}"])
def test_opencode_refuses_substitution_words_without_a_snippet(
    token: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A data dir holding {env: or {file: is not written, and no snippet is printed.

    Mutation: remove the writer's {env: and {file: check. Install exits 0
    and writes that word into command. This test fails.
    """

    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / token
    path = _files(home)["mcp"]
    code, out, _err = _install(home, vault, capsys)
    assert code == 1, out
    assert "snippet:" not in out
    assert token not in out
    assert not path.exists()
    assert not _backups(vault)


def test_opencode_relative_data_dir_snippet_shows_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The relative --data-dir refusal marks the spot in the snippet."""

    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    relative = "rel/vault"
    path = _files(home)["mcp"]
    _write(
        path,
        json.dumps(
            {
                "mcp": {
                    "alice": {
                        "type": "local",
                        "command": ["uvx", "alice-memory", "mcp", "--data-dir", relative],
                    }
                }
            }
        ),
    )
    before = path.read_bytes()
    code = onramp_main(["install", "--home", str(home), "--host", "opencode"])
    captured = capsys.readouterr()
    marker = f"<an absolute path for {relative}>"
    assert code == 1, captured.out
    assert path.read_bytes() == before
    assert "snippet:" in captured.out
    assert marker in captured.out.split("snippet:", 1)[1]
    assert "marks where" in captured.out


def test_opencode_first_rewrite_backs_up_a_file_without_alice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The first rewrite of an existing file backs up those bytes.

    Mutation: skip the backup when the file has no alice entry yet.
    This test fails.
    """

    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _files(home)["mcp"]
    _write(
        path,
        json.dumps(
            {
                "$schema": _SCHEMA,
                "autoupdate": False,
                "mcp": {"other": {"type": "local", "command": ["node", "s.js"]}},
            }
        )
        + "\n",
    )
    seed = path.read_bytes()
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    backups = _backups(vault)
    assert len(backups) == 1
    assert backups[0].read_bytes() == seed
    assert "backup:" in out


def test_opencode_rewrites_config_json_when_it_holds_alice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When alice already sits in config.json, that file is the one rewritten."""

    scripts = _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _files(home)["legacy"]
    _write(
        path,
        json.dumps(
            {
                "mcp": {
                    "alice": {
                        "type": "local",
                        "command": [
                            str(scripts / "alice-memory"),
                            "mcp",
                            "--data-dir",
                            str((tmp_path / "old").resolve()),
                        ],
                    }
                }
            }
        ),
    )
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert f"path: {path}" in out
    written = json.loads(path.read_text(encoding="utf-8"))
    assert str(vault.resolve()) in written["mcp"]["alice"]["command"]
    assert not _files(home)["mcp"].exists()


def test_readme_marks_opencode_unreleased_and_changelog_says_it_masks_command() -> None:
    """README describes the published package, which has no OpenCode host.

    The OpenCode detail lives in the changelog until a release ships it.
    Mutation: describe OpenCode in README outside the one marked line, or
    drop the masking sentence from the changelog. This test fails.
    """

    root = Path(__file__).resolve().parents[2]
    text = (root / "README.md").read_text(encoding="utf-8")
    marker = (
        "On main, not yet released: an opt-in OpenCode host (`--host opencode`). "
        "The published v0.17.0 does not have it."
    )
    assert [line for line in text.splitlines() if "opencode" in line.casefold()] == [marker]
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "When alice already sits in `config.json`, that file is the one" in changelog
    assert "A dry run masks the command array." in " ".join(changelog.split())


@pytest.mark.skipif(os.environ.get("ALICE_TEST_REAL_HOSTS") != "1", reason="set ALICE_TEST_REAL_HOSTS=1")
@pytest.mark.skipif(shutil.which("opencode") is None, reason="opencode is not on PATH")
@pytest.mark.skipif(sys.platform != "linux", reason="the OpenCode real-host check runs on Linux")
def test_real_opencode_reads_the_written_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """opencode debug config and mcp list see the strict JSON install wrote."""

    alice = shutil.which("alice-memory")
    scripts = Path(alice).resolve().parent if alice else Path(sys.executable).resolve().parent
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=scripts)
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    vault = tmp_path / "vault"
    moved = tmp_path / "vault-2"
    path = _files(home)["mcp"]
    sibling = "disabled-sibling"
    _write(
        path,
        json.dumps(
            {
                "$schema": _SCHEMA,
                "autoupdate": False,
                "mcp": {sibling: {"type": "local", "command": ["node", "s.js"], "enabled": False}},
            },
            indent=2,
        )
        + "\n",
    )
    seed = path.read_bytes()

    def run_opencode(*args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        for name in list(env):
            if name.startswith("OPENCODE_CONFIG") or name == "OPENCODE_TEST_HOME":
                env.pop(name, None)
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        return subprocess.run(
            ["opencode", *args],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    version_proc = run_opencode("--version")
    version = (version_proc.stdout + version_proc.stderr).strip()
    print(f"opencode version: {version}")
    shown = run_opencode("debug", "config")
    assert shown.returncode == 0, (version, shown.stderr)
    assert "autoupdate" in shown.stdout
    assert sibling in shown.stdout
    code, _out, err = _install(home, vault, capsys)
    assert code == 0, (version, err)
    backups = _backups(vault)
    assert len(backups) == 1
    assert backups[0].read_bytes() == seed
    shown = run_opencode("debug", "config")
    assert "alice" in shown.stdout, version
    listed = run_opencode("mcp", "list")
    listed_text = listed.stdout + listed.stderr
    assert listed.returncode == 0, (version, listed_text)
    alice_lines = [line for line in listed_text.splitlines() if "alice" in line]
    assert any("connected" in line for line in alice_lines), (version, listed_text)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["kept"] = True
    doc["mcp"]["extra"] = {"type": "local", "command": ["node", "e.js"], "enabled": False}
    doc["mcp"]["alice"]["timeout"] = 1500
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    code, _out, err = _install(home, moved, capsys)
    assert code == 0, err
    again = json.loads(path.read_text(encoding="utf-8"))
    assert again["kept"] is True
    assert again["mcp"]["extra"]["enabled"] is False
    assert again["mcp"]["alice"]["timeout"] == 1500
    assert str(moved.resolve()) in again["mcp"]["alice"]["command"]
    assert len(_backups(moved)) == 1
    secret = "sk-" + "test" + "tok" + "99"
    index = "https://user:" + secret + "@example.test/idx"
    again_doc = json.loads(path.read_text(encoding="utf-8"))
    again_doc["mcp"]["alice"]["command"] = [
        "uvx",
        "--index",
        index,
        "alice-memory",
        "mcp",
        "--data-dir",
        str(moved.resolve()),
    ]
    path.write_text(json.dumps(again_doc, indent=2) + "\n", encoding="utf-8")
    code, out, _err = _install(home, moved, capsys, "--dry-run")
    assert code == 0, (version, _err)
    assert "hidden:" in out
    assert secret not in out
    assert "action: dry-run" in out


@pytest.mark.skipif(os.environ.get("ALICE_TEST_REAL_HOSTS") != "1", reason="set ALICE_TEST_REAL_HOSTS=1")
@pytest.mark.skipif(shutil.which("opencode") is None, reason="opencode is not on PATH")
@pytest.mark.skipif(sys.platform != "linux", reason="the OpenCode real-host check runs on Linux")
def test_real_opencode_rejects_a_broken_alice_entry(tmp_path: Path) -> None:
    """A string command is invalid. Any other outcome fails this test."""

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    path = _files(home)["mcp"]
    _write(
        path,
        json.dumps(
            {
                "$schema": _SCHEMA,
                "mcp": {"alice": {"type": "local", "command": ["echo", "ok"]}},
            }
        ),
    )
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    for name in list(env):
        if name.startswith("OPENCODE_CONFIG") or name == "OPENCODE_TEST_HOME":
            env.pop(name, None)
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
    env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["opencode", *args],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    valid = run("debug", "config")
    assert valid.returncode == 0, valid.stderr
    assert "alice" in valid.stdout
    path.write_text(
        json.dumps({"$schema": _SCHEMA, "mcp": {"alice": {"type": "local", "command": "echo"}}}) + "\n",
        encoding="utf-8",
    )
    broken = run("debug", "config")
    combined = broken.stdout + broken.stderr
    assert broken.returncode == 1, combined
    assert f"Configuration is invalid at {path}" in combined
