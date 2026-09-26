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

from alicebot_api.host_install import host_file_map
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
    assert "config" in out


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


def test_readme_says_opencode_masks_command() -> None:
    readme = Path(__file__).resolve().parents[2] / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert "--host opencode" in text
    assert "masks that array" in text


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

    version = run_opencode("--version")
    assert "1.18.32" in (version.stdout + version.stderr)
    shown = run_opencode("debug", "config")
    assert shown.returncode == 0, shown.stderr
    assert "autoupdate" in shown.stdout
    assert sibling in shown.stdout
    code, _out, err = _install(home, vault, capsys)
    assert code == 0, err
    backups = _backups(vault)
    assert len(backups) == 1
    assert backups[0].read_bytes() == seed
    shown = run_opencode("debug", "config")
    assert "alice" in shown.stdout
    listed = run_opencode("mcp", "list")
    assert listed.returncode == 0, listed.stderr
    assert "alice" in listed.stdout
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
    code, out, _err = _install(home, moved, capsys, "--dry-run")
    assert code == 0
    assert "hidden:" in out or "action: dry-run" in out
    assert "tok" not in out


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
