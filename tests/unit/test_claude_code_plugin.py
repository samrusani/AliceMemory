"""Claude Code plugin files, install's plugin check, and the duplicate brief line.

Real-host tests run only when ALICE_TEST_REAL_HOSTS=1.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from alicebot_api.host_install import CLAUDE_MARKETPLACE_NAME, CLAUDE_PLUGIN_ID
from alicebot_api.onramp import main as onramp_main
from alicebot_api.session_start_hook import main as hook_main
import scripts.release_check as release_check
from tests.unit.launcher_helpers import pin_launcher_search

pytestmark = pytest.mark.usefixtures("uvx_on_path")
REAL_HOSTS_ENV = "ALICE_TEST_REAL_HOSTS"
ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "alice-memory"


def _version() -> str:
    loaded = release_check.read_release_metadata(ROOT)
    return loaded.version


def _install(capsys, home: Path, *extra: str) -> tuple[int, str, str]:
    code = onramp_main(["install", "--home", str(home), "--host", "claude-code", *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _settings(home: Path, enabled: object | None) -> None:
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, object] = {}
    if enabled is not None:
        doc["enabledPlugins"] = enabled
    path.write_text(json.dumps(doc), encoding="utf-8")


def _seed_server(home: Path) -> None:
    path = home / ".claude.json"
    path.write_text(
        json.dumps({"mcpServers": {"alice": {"command": "uvx", "args": ["alice-memory", "mcp"]}}}),
        encoding="utf-8",
    )


def test_plugin_files_match_pyproject() -> None:
    version = _version()
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    mcp = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert plugin["name"] == "alice-memory"
    assert plugin["version"] == version
    assert CLAUDE_PLUGIN_ID == f"{plugin['name']}@{CLAUDE_MARKETPLACE_NAME}"
    option = plugin["userConfig"]["data_dir"]
    assert option["type"] == "string"
    assert option["default"] == "~/.alice"
    server = mcp["mcpServers"]["alice"]
    assert server["command"] == "uvx"
    assert "${user_config.data_dir}" in server["args"]
    assert f"alice-memory=={version}" in server["args"]
    handler = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    assert handler["type"] == "command"
    assert handler["command"] == "uvx"
    assert "command" in handler and isinstance(handler.get("args"), list)
    assert "${user_config.data_dir}" in handler["args"]
    assert f"alice-memory=={version}" in handler["args"]
    assert not (ROOT / ".claude-plugin" / "marketplace.json").exists()


def test_install_and_the_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    vault = tmp_path / "vault"

    home = tmp_path / "enabled-empty"
    _settings(home, {CLAUDE_PLUGIN_ID: True})
    code, out, err = _install(capsys, home, "--data-dir", str(vault))
    assert code == 0, err
    assert "skipped (the alice-memory plugin is enabled)" in out
    assert "session_start: skipped" in out
    assert not (home / ".claude.json").exists()

    home = tmp_path / "enabled-entry"
    _settings(home, {CLAUDE_PLUGIN_ID: True})
    _seed_server(home)
    before = (home / ".claude.json").read_bytes()
    code, out, err = _install(capsys, home, "--data-dir", str(vault))
    assert code == 1, out
    assert (home / ".claude.json").read_bytes() == before
    assert "claude mcp remove alice --scope user" in out
    assert "alice-memory-session-start" in out

    home = tmp_path / "disabled"
    _settings(home, {CLAUDE_PLUGIN_ID: False})
    code, out, err = _install(capsys, home, "--data-dir", str(vault))
    assert code == 0, err
    assert "Enabling it later sets Alice up twice" in out
    assert (home / ".claude.json").is_file()

    home = tmp_path / "missing"
    code, out, err = _install(capsys, home, "--data-dir", str(vault))
    assert code == 0, err
    assert "plugin is enabled" not in out
    assert (home / ".claude.json").is_file()

    home = tmp_path / "not-object"
    _settings(home, ["alice-memory"])
    code, out, err = _install(capsys, home, "--data-dir", str(vault))
    assert code == 0, err
    assert (home / ".claude.json").is_file()

    home = tmp_path / "other"
    _settings(home, {"other@other": True})
    code, out, err = _install(capsys, home, "--data-dir", str(vault))
    assert code == 0, err
    assert "skipped" not in out

    home = tmp_path / "dry"
    _settings(home, {CLAUDE_PLUGIN_ID: True})
    code, out, err = _install(capsys, home, "--data-dir", str(vault), "--dry-run")
    assert code == 0, err
    assert "skipped (the alice-memory plugin is enabled)" in out
    assert not (home / ".claude.json").exists()


def test_session_start_warns_about_a_double_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    vault = tmp_path / "vault"
    _seed_server(tmp_path)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    code = hook_main(["--data-dir", str(vault), "--format", "markdown"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "set up twice" not in captured.out

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin"))
    for flag in (["--format", "markdown"], []):
        code = hook_main(["--data-dir", str(vault), *flag])
        captured = capsys.readouterr()
        assert code == 0, captured.err
        assert "Alice is set up twice in Claude Code." in captured.out
        assert "claude mcp remove alice --scope user" in captured.out


def test_versions_agree_and_seeded_marketplace(tmp_path: Path) -> None:
    metadata, issues = release_check.validate_metadata(ROOT)
    assert issues == [] or all("marketplace" not in item for item in issues)
    assert metadata.version == _version()
    plugin_issues = release_check._plugin_metadata_issues(ROOT, metadata.version)
    assert plugin_issues == []

    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## v1.2.3 — 2026-01-02\n", encoding="utf-8")
    plugin_dir = tmp_path / "plugins" / "alice-memory" / ".claude-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "alice-memory", "version": "1.2.3"}),
        encoding="utf-8",
    )
    market = tmp_path / ".claude-plugin"
    market.mkdir()
    (market / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "alicememory",
                "description": "Alice Memory plugins",
                "owner": {"name": "Alice Memory"},
                "plugins": [
                    {
                        "name": "alice-memory",
                        "source": {
                            "source": "git-subdir",
                            "url": "samrusani/AliceMemory",
                            "path": "plugins/alice-memory",
                            "ref": "v1.2.3",
                            "sha": "a" * 40,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert release_check._marketplace_issues(tmp_path, market / "marketplace.json") == []


def test_plugin_pin_drift_is_an_issue(tmp_path: Path) -> None:
    from tests.unit.test_release_check import _seed_metadata_tree

    _seed_metadata_tree(tmp_path, python_version="1.2.3", web_version="1.2.3")
    mcp = tmp_path / "plugins" / "alice-memory" / ".mcp.json"
    mcp.write_text(mcp.read_text(encoding="utf-8").replace("alice-memory==1.2.3", "alice-memory==9.9.9"), encoding="utf-8")
    _metadata, issues = release_check.validate_metadata(tmp_path)
    assert any("does not pin alice-memory==1.2.3" in item for item in issues)


@pytest.mark.skipif(os.environ.get(REAL_HOSTS_ENV) != "1", reason="set ALICE_TEST_REAL_HOSTS=1")
@pytest.mark.skipif(sys.platform != "linux", reason="the Claude Code real-host check runs on Linux")
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not on PATH")
def test_real_claude_validates_the_plugin(tmp_path: Path) -> None:
    good = subprocess.run(
        ["claude", "plugin", "validate", str(PLUGIN), "--strict", "--json"],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    assert good.returncode == 0, (good.stdout, good.stderr)
    copy = tmp_path / "plugin"
    shutil.copytree(PLUGIN, copy)
    mcp_path = copy / ".mcp.json"
    mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    mcp["mcpServers"]["alice"]["args"].append("${user_config.nope}")
    mcp_path.write_text(json.dumps(mcp), encoding="utf-8")
    bad = subprocess.run(
        ["claude", "plugin", "validate", str(copy), "--strict", "--json"],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    assert bad.returncode == 1, (bad.stdout, bad.stderr)
    assert "nope" in (bad.stdout + bad.stderr)


