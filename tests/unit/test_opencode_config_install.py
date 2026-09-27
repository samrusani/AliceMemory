"""OpenCode install writes strict JSON and edits JSONC as text.

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

from tests.unit.jsonc_judge import parse as parse_jsonc
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


def test_opencode_format_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A loose opencode.json is edited as text. A file the scanner cannot read stays put.

    Mutation: refuse every non-strict file with the old hand-edit line.
    The 1e999 file is not edited, or a comment is dropped. This test fails.
    """

    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _files(home)["mcp"]
    refused = [
        (b'{ "mcp": { "alice": 1 } } // comment\n', "alice is not an object"),
        (b'{"mcp": {"alice": 1,}}\n', "alice is not an object"),
        (b'{"mcp": {"alice": 1, "alice": 2}}\n', "Keep one alice entry"),
        (b'{"mcp": {"n": NaN}}\n', "a token error"),
        (b"\xef\xbb\xbf" + b'{"mcp": {}}\n', "a byte order mark"),
    ]
    for raw, reason in refused:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        before = path.read_bytes()
        code, out, _err = _install(home, vault, capsys)
        assert code == 1, raw
        assert reason in out
        assert "PR B will edit" not in out
        assert path.read_bytes() == before
        assert not _backups(vault)
        path.unlink()

    path.write_bytes(b'{"mcp": {"n": 1e999}}\n')
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    written = path.read_text(encoding="utf-8")
    assert "1e999" in written
    assert "format: jsonc, edited as text" in out
    assert parse_jsonc(written)["mcp"]["alice"]["type"] == "local"
    assert parse_jsonc(written)["mcp"]["n"] == float("inf")
    path.unlink()

    jsonc = _files(home)["jsonc"]
    _write(jsonc, '{ /* kept */ "mcp": {} }\n')
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert "/* kept */" in jsonc.read_text(encoding="utf-8")
    assert "format: jsonc, edited as text" in out


def test_opencode_jsonc_is_edited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A global opencode.jsonc is the file install edits. It does not write opencode.json.

    Mutation: refuse every jsonc file. Nothing is written. This test fails.
    """

    scripts = _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _files(home)["jsonc"]
    _write(path, '{ "mcp": {} }\n')
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert "format: jsonc, edited as text" in out
    assert "session_start: none" in out
    doc = parse_jsonc(path.read_text(encoding="utf-8"))
    assert doc["mcp"]["alice"]["command"][0] == str(scripts / "alice-memory")
    assert "hooks" not in doc
    assert "hook" not in doc["mcp"]["alice"]
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


def test_opencode_home_jsonc_is_part_of_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """~/.opencode/opencode.jsonc is part of the scan. Alice there is the target.

    Mutation: drop that path from the scan. Install writes opencode.json.
    This test fails.
    """

    scripts = _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    files = _files(home)
    path = files["home_jsonc"]
    _write(
        path,
        json.dumps(
            {
                "mcp": {
                    "alice": {
                        "type": "local",
                        "command": [str(scripts / "alice-memory"), "mcp", "--data-dir", str(tmp_path / "old")],
                    }
                }
            }
        )
        + "\n",
    )
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert f"path: {path}" in out
    assert str(vault.resolve()) in path.read_text(encoding="utf-8")
    assert not files["mcp"].exists()
    assert not files["jsonc"].exists()


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


def test_opencode_json_and_jsonc_are_a_second_alice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Alice in opencode.json and opencode.jsonc is a second alice. Nothing is written.

    Mutation: skip the jsonc file while scanning. Install edits opencode.json.
    This test fails.
    """

    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    files = _files(home)
    alice = {"type": "local", "command": ["uvx", "alice-memory", "mcp", "--data-dir", "/v"]}
    _write(files["mcp"], json.dumps({"mcp": {"alice": alice}}))
    _write(files["jsonc"], '{ "mcp": { "alice": { "type": "local", "command": ["uvx", "alice-memory", "mcp"] } } }\n')
    before_json = files["mcp"].read_bytes()
    before_jsonc = files["jsonc"].read_bytes()
    code, out, _err = _install(home, vault, capsys)
    assert code == 1
    assert "Keep one alice entry" in out
    assert files["mcp"].read_bytes() == before_json
    assert files["jsonc"].read_bytes() == before_jsonc
    assert not _backups(vault)


def test_opencode_jsonc_insert_keeps_every_other_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Inserting mcp.alice leaves every other byte, including a CRLF file's schema line."""

    scripts = _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _files(home)["jsonc"]
    schema_line = '"$schema": "https://opencode.ai/config.json",'
    original = "{\r\n  " + schema_line + "\r\n  " + '"note": "keep me"\r\n}\r\n'
    _write(path, original)
    code, _out, err = _install(home, vault, capsys)
    assert code == 0, err
    written = path.read_bytes().decode("utf-8")
    assert schema_line in written
    assert '"note": "keep me"' in written
    assert "\r\n" in written
    assert written.split(schema_line, 1)[1].startswith("\r\n  " + '"note": "keep me"\r\n}\r\n')
    doc = parse_jsonc(written)
    assert doc["note"] == "keep me"
    assert doc["mcp"]["alice"]["command"][0] == str(scripts / "alice-memory")
    assert "hooks" not in doc


def test_opencode_jsonc_replaces_only_alice_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A data-dir change replaces the alice value and leaves a sibling backslash path."""

    scripts = _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _files(home)["jsonc"]
    sibling = '"other": { "command": ["C:\\\\tools\\\\node"] }'
    original = (
        "{\n"
        '  "$schema": "https://opencode.ai/config.json",\n'
        '  "mcp": {\n'
        f"    {sibling},\n"
        '    "alice": { "type": "local", "command": ["uvx", "alice-memory", "mcp", "--data-dir", "/old"] }\n'
        "  }\n"
        "}\n"
    )
    _write(path, original)
    code, _out, err = _install(home, vault, capsys)
    assert code == 0, err
    written = path.read_text(encoding="utf-8")
    assert sibling in written
    assert '"$schema": "https://opencode.ai/config.json",' in written
    assert "/old" not in written
    assert str(vault.resolve()) in written
    assert written.split(sibling, 1)[0].endswith("    ")
    doc = parse_jsonc(written)
    assert doc["mcp"]["alice"]["command"][0] == str(scripts / "alice-memory")
    assert doc["mcp"]["other"]["command"] == ["C:\\tools\\node"]


def test_opencode_jsonc_carries_documented_env_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A documented environment literal is copied raw, escapes included.

    Mutation: re-encode the value with json.dumps. The ``\\u0041`` escape is gone.
    This test fails.
    """

    scripts = _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _files(home)["jsonc"]
    secret = '"' + "sk-" + "\\u0041" + "b\\\"c" + '"'
    file_ref = '"{file:~/k}"'
    original = (
        "{\n"
        '  "mcp": {\n'
        '    "alice": {\n'
        '      "type": "local",\n'
        '      "command": ['
        + json.dumps(str(scripts / "alice-memory"))
        + ', "mcp", "--data-dir", "/old"],\n'
        '      "environment": {\n'
        f"        \"ALICE_AGENT_API_KEY\": {secret},\n"
        f"        \"ALICE_EMBEDDINGS_BASE_URL\": {file_ref}\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    _write(path, original)
    code, out, err = _install(home, vault, capsys)
    assert code == 0, (out, err)
    written = path.read_text(encoding="utf-8")
    assert secret in written
    assert file_ref in written
    assert "sk-Ab" not in written
    assert str(vault.resolve()) in written


def test_opencode_jsonc_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unsafe JSONC is left byte for byte. The receipt does not print the file.

    Mutation: drop the extra-key refusal and rewrite the file. This test fails.
    """

    _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _files(home)["jsonc"]
    marker = "KEEP" + "TEXT" + "91"
    command = '["uvx", "alice-memory", "mcp", "--data-dir", "/old"]'
    cases = [
        (
            '{ "note": "' + marker + '", "mcp": { "alice": { "type": "local", "command": '
            + command
            + ', "environment": { "FOO": "nope" } } } }\n',
            "those keys are present",
        ),
        (
            '{ "note": "' + marker + '", "mcp": { "alice": { "type": "local", "command": '
            + command
            + ', "timeout": 1 } } }\n',
            "those keys are present",
        ),
        (
            '{ "note": "' + marker + '", "mcp": { "alice": { "type": "local", "command": '
            + command
            + ', "enabled": false } } }\n',
            "those keys are present",
        ),
        (
            '{ "note": "' + marker + '", "mcp": { "alice": { "type": "local", "command": '
            + command
            + ', "cwd": "/tmp" } } }\n',
            "those keys are present",
        ),
        (
            '{ "note": "' + marker + '", "mcp": { "alice": { "type": "local", /* no */ "command": '
            + command
            + " } } }\n",
            "a comment in alice",
        ),
        (
            '{ "note": "' + marker + '", "mcp": { "alice": 1 } }\n',
            "alice is not an object",
        ),
        (
            '{ "note": "' + marker + '", "mcp": { "alice": { "type": "local" } /* never closed',
            "an unterminated comment",
        ),
        (
            '{ "note": "' + marker + '", "mcp": { "alice": {env:HOME} } }\n',
            "an unquoted substitution",
        ),
        (
            '{ "note": "'
            + marker
            + '", "mcp": { "alice": { "type": "local", "command": '
            + command
            + ' }, "alice": { "type": "local", "command": '
            + command
            + " } } }\n",
            "Keep one alice entry",
        ),
        ("   \n\n", "whitespace-only text"),
    ]
    for raw, reason in cases:
        _write(path, raw)
        before = path.read_bytes()
        code, out, err = _install(home, vault, capsys)
        assert code == 1, (reason, out, err)
        assert reason in out
        assert marker not in out + err
        assert path.read_bytes() == before
        assert not _backups(vault)
        if reason in {"an unterminated comment", "whitespace-only text", "an unquoted substitution"}:
            assert "<the data dir your existing alice entry uses>" in out


def test_opencode_jsonc_every_rewrite_backs_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two JSONC rewrites leave two backups, each equal to the prior bytes."""

    scripts = _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    first = tmp_path / "vault-a"
    second = tmp_path / "vault-b"
    third = tmp_path / "vault-c"
    path = _files(home)["jsonc"]
    seed = (
        "{\n"
        '  "mcp": {\n'
        '    "alice": { "type": "local", "command": ['
        + json.dumps(str(scripts / "alice-memory"))
        + ', "mcp", "--data-dir", '
        + json.dumps(str(first))
        + "] }\n"
        "  }\n"
        "}\n"
    )
    _write(path, seed)
    seed_bytes = path.read_bytes()
    code, _out, err = _install(home, second, capsys)
    assert code == 0, err
    after_first = path.read_bytes()
    code, _out, err = _install(home, third, capsys)
    assert code == 0, err
    assert len(_backups(second)) == 1
    assert _backups(second)[0].read_bytes() == seed_bytes
    assert len(_backups(third)) == 1
    assert _backups(third)[0].read_bytes() == after_first


def test_opencode_unreadable_directory_fails_only_that_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mode 000 on an OpenCode directory fails that host and still prints every receipt.

    Mutation: let the PermissionError escape. The other host's receipt is missing.
    This test fails.
    """

    _pin(monkeypatch, tmp_path)
    targets = ("config", "opencode", "dot-opencode")
    for label in targets:
        home = tmp_path / label / "home"
        vault = tmp_path / label / "vault"
        if label == "config":
            directory = home / ".config"
        elif label == "opencode":
            directory = home / ".config" / "opencode"
        else:
            directory = home / ".opencode"
        directory.mkdir(parents=True)
        directory.chmod(0)
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
            assert code == 1, label
            assert "PermissionError" not in captured.out + captured.err
            assert "host: claude-code" in captured.out
            assert "action: written" in captured.out
            assert "host: opencode" in captured.out
            assert "action: failed" in captured.out
            assert "the file could not be read or written" in captured.out
            if label == "opencode":
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
                        "--dry-run",
                    ]
                )
                captured = capsys.readouterr()
                assert code == 1
                assert "action: failed" in captured.out
                assert "PermissionError" not in captured.out + captured.err
        finally:
            directory.chmod(0o755)


def test_opencode_bad_args_snippet_uses_the_entry_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Args that would not start still paste the entry's own data dir.

    The warning does not mention a SessionStart hook. Mutation: build the
    snippet on ~/.alice. This test fails.
    """

    scripts = _pin(monkeypatch, tmp_path)
    home = tmp_path / "home"
    own = (tmp_path / "real-vault").resolve()
    path = _files(home)["mcp"]
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
                            str(own),
                            "--bogus",
                        ],
                    }
                }
            }
        ),
    )
    before = path.read_bytes()
    code = onramp_main(["install", "--home", str(home), "--host", "opencode"])
    captured = capsys.readouterr()
    assert code == 1, captured.out
    assert path.read_bytes() == before
    assert "would not start" in captured.out
    assert "SessionStart" not in captured.out
    snippet = captured.out.split("snippet:", 1)[1]
    assert str(own) in snippet
    assert str((home / ".alice").resolve()) not in snippet


def test_readme_says_opencode_masks_command() -> None:
    root = Path(__file__).resolve().parents[2]
    text = (root / "README.md").read_text(encoding="utf-8")
    assert "--host opencode" in text
    assert "masks that array" in text
    assert "When alice already sits in `config.json`, that file is the one rewritten." in text
    assert "edited as text" in text
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "When alice already sits in `config.json`, that file is the one" in changelog


@pytest.mark.skipif(os.environ.get("ALICE_TEST_REAL_HOSTS") != "1", reason="set ALICE_TEST_REAL_HOSTS=1")
@pytest.mark.skipif(shutil.which("opencode") is None, reason="opencode is not on PATH")
@pytest.mark.skipif(sys.platform != "linux", reason="the OpenCode real-host check runs on Linux")
@pytest.mark.parametrize("kind", ["json", "jsonc"])
def test_real_opencode_reads_the_written_config(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """opencode debug config and mcp list see the config install wrote."""

    alice = shutil.which("alice-memory")
    scripts = Path(alice).resolve().parent if alice else Path(sys.executable).resolve().parent
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=scripts)
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    vault = tmp_path / "vault"
    moved = tmp_path / "vault-2"
    path = _files(home)["jsonc"] if kind == "jsonc" else _files(home)["mcp"]
    sibling = "disabled-sibling"
    if kind == "json":
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
    else:
        _write(
            path,
            "{\n"
            f'  "$schema": "{_SCHEMA}",\n'
            "  // kept comment\n"
            '  "autoupdate": false,\n'
            '  "mcp": {\n'
            f'    "{sibling}": {{"type": "local", "command": ["node", "s.js"], "enabled": false}}\n'
            "  }\n"
            "}\n",
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
    if kind == "jsonc":
        assert "// kept comment" in path.read_text(encoding="utf-8")
        assert not _files(home)["mcp"].exists()
    doc = json.loads(path.read_text(encoding="utf-8")) if kind == "json" else None
    if kind == "json":
        assert doc is not None
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
        return
    text = path.read_text(encoding="utf-8")
    text = text.replace("{", '{\n  "kept": true,', 1)
    text = text.replace(
        '"mcp": {',
        '"mcp": {\n    "extra": {"type": "local", "command": ["node", "e.js"], "enabled": false},',
        1,
    )
    path.write_text(text, encoding="utf-8")
    code, _out, err = _install(home, moved, capsys)
    assert code == 0, err
    again_text = path.read_text(encoding="utf-8")
    again = parse_jsonc(again_text)
    assert isinstance(again, dict)
    assert again["kept"] is True
    assert again["mcp"]["extra"]["enabled"] is False
    assert "// kept comment" in again_text
    assert str(moved.resolve()) in again_text
    assert len(_backups(moved)) == 1
    secret = "sk-" + "test" + "tok" + "99"
    index = "https://user:" + secret + "@example.test/idx"
    command0 = json.dumps(str(Path(alice).resolve())) if alice else json.dumps(str(scripts / "alice-memory"))
    secret_text = again_text.replace(
        command0,
        json.dumps("uvx") + ", " + json.dumps("--index") + ", " + json.dumps(index) + ", " + json.dumps("alice-memory"),
        1,
    )
    path.write_text(secret_text, encoding="utf-8")
    secret_bytes = path.read_bytes()
    code, out, _err = _install(home, moved, capsys, "--dry-run")
    assert code == 0, (version, _err)
    assert "hidden:" in out
    assert secret not in out
    assert "action: dry-run" in out
    assert path.read_bytes() == secret_bytes
    at = secret_text.index('"alice"')
    brace = secret_text.index("{", at)
    refused_text = secret_text[: brace + 1] + ' "timeout": 1500,' + secret_text[brace + 1 :]
    path.write_text(refused_text, encoding="utf-8")
    before = path.read_bytes()
    code, out, _err = _install(home, moved, capsys)
    assert code == 1, out
    assert "those keys are present" in out
    assert path.read_bytes() == before


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
