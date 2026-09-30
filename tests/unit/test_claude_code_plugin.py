"""Claude Code plugin files, install's plugin check, and the duplicate brief line.

Real-host tests run only when ALICE_TEST_REAL_HOSTS=1.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

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


def _expected_plugin_documents(version: str) -> tuple[dict, dict, dict]:
    plugin = {
        "name": "alice-memory",
        "version": version,
        "description": (
            "Local-first memory for Claude Code: an MCP server and a SessionStart "
            "brief on one SQLite file."
        ),
        "author": {"name": "Alice Memory", "url": "https://alicememory.com"},
        "homepage": "https://alicememory.com",
        "repository": "https://github.com/samrusani/AliceMemory",
        "license": "MIT",
        "userConfig": {
            "data_dir": {
                "type": "string",
                "title": "Alice data directory",
                "description": (
                    "Absolute path, or ~/..., of the folder holding memory.db. "
                    "Use the same folder as your other hosts."
                ),
                "default": "~/.alice",
                "required": False,
            }
        },
    }
    pin = f"alice-memory=={version}"
    mcp = {
        "mcpServers": {
            "alice": {
                "command": "uvx",
                "args": ["--from", pin, "alice-memory", "mcp", "--data-dir", "${user_config.data_dir}"],
            }
        }
    }
    hooks = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "uvx",
                            "args": ["--from", pin, "alice-memory-session-start"],
                        }
                    ]
                }
            ]
        }
    }
    return plugin, mcp, hooks


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
    assert handler["args"] == ["--from", f"alice-memory=={version}", "alice-memory-session-start"]
    assert "user_config" not in json.dumps(hooks)
    assert f"alice-memory=={version}" in handler["args"]
    expected_plugin, expected_mcp, expected_hooks = _expected_plugin_documents(version)
    assert plugin == expected_plugin
    assert mcp == expected_mcp
    assert hooks == expected_hooks
    market = ROOT / ".claude-plugin" / "marketplace.json"
    if market.is_file():
        assert release_check._marketplace_issues(ROOT, market) == []


def test_the_plugin_hook_trial_uses_install_ids() -> None:
    """The dispatch-only trial writes its own marketplace, so its ids must be install's.

    Mutation: change the marketplace name or the plugin id in
    ``scripts/real_host_plugin_hook_trial.py``. This test fails.
    """

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "real_host_plugin_hook_trial", ROOT / "scripts" / "real_host_plugin_hook_trial.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._MARKETPLACE == CLAUDE_MARKETPLACE_NAME
    assert module._PLUGIN_ID == CLAUDE_PLUGIN_ID
    assert module._PROBE_ID == f"probe-plugin@{CLAUDE_MARKETPLACE_NAME}"


def test_install_and_the_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    vault = tmp_path / "vault"

    home = tmp_path / "enabled-empty"
    _settings(home, {CLAUDE_PLUGIN_ID: True})
    code, out, err = _install(capsys, home, "--data-dir", str(vault))
    assert code == 0, err
    resolved = home.resolve()
    skip_lines = [
        "host: claude-code",
        f"path: {resolved / '.claude.json'}",
        "action: skipped (the alice-memory plugin is enabled)",
        "session_start: skipped",
        f"session_start_path: {resolved / '.claude' / 'settings.json'}",
    ]
    assert "\n".join(skip_lines) in out
    assert "launcher:" not in out
    assert "data_dir:" not in out
    assert not (home / ".claude.json").exists()

    home = tmp_path / "enabled-entry"
    _settings(home, {CLAUDE_PLUGIN_ID: True})
    _seed_server(home)
    before = (home / ".claude.json").read_bytes()
    code, out, err = _install(capsys, home, "--data-dir", str(vault))
    assert code == 1, out
    assert (home / ".claude.json").read_bytes() == before
    resolved = home.resolve()
    refusal_lines = [
        "host: claude-code",
        f"path: {resolved / '.claude.json'}",
        "action: refused",
        "reason: the alice-memory plugin is enabled and install's entries exist",
        "session_start: none",
        f"session_start_path: {resolved / '.claude' / 'settings.json'}",
        "next: run `claude mcp remove alice --scope user`, remove the "
        "alice-memory-session-start hook from ~/.claude/settings.json, or disable the plugin.",
    ]
    assert "\n".join(refusal_lines) in out
    assert "launcher:" not in out
    assert "data_dir:" not in out

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
    good_payload = json.loads(good.stdout)
    assert good_payload.get("success") is True
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
    bad_payload = json.loads(bad.stdout)
    assert bad_payload.get("success") is False
    assert "user_config.nope" in (bad.stdout + bad.stderr)


def test_install_plugin_edge_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    vault = tmp_path / "vault"

    home = tmp_path / "other-market"
    _settings(home, {"alice-memory@othermarket": True})
    code, out, err = _install(capsys, home, "--data-dir", str(vault))
    assert code == 0, err
    assert "skipped" not in out
    assert (home / ".claude.json").is_file()

    home = tmp_path / "string-true"
    _settings(home, {CLAUDE_PLUGIN_ID: "true"})
    code, out, err = _install(capsys, home, "--data-dir", str(vault))
    assert code == 0, err
    assert "skipped" not in out
    assert (home / ".claude.json").is_file()

    home = tmp_path / "hook-only"
    settings = {
        "enabledPlugins": {CLAUDE_PLUGIN_ID: True},
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "uvx alice-memory-session-start --data-dir /v",
                        }
                    ]
                }
            ]
        },
    }
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(settings), encoding="utf-8")
    claude_json = home / ".claude.json"
    claude_json.write_text(json.dumps({"userID": "kept"}), encoding="utf-8")
    before_settings = path.read_bytes()
    before_claude = claude_json.read_bytes()
    code, out, err = _install(capsys, home, "--data-dir", str(vault))
    assert code == 1, out
    assert path.read_bytes() == before_settings
    assert claude_json.read_bytes() == before_claude
    assert "install_refused_plugin" in err
    assert "add the printed snippet" not in err

    home = tmp_path / "dry-present"
    _settings(home, {CLAUDE_PLUGIN_ID: True})
    _seed_server(home)
    before = (home / ".claude.json").read_bytes()
    code, out, err = _install(capsys, home, "--data-dir", str(vault), "--dry-run")
    assert code == 1, out
    resolved = home.resolve()
    dry_lines = [
        "host: claude-code",
        f"path: {resolved / '.claude.json'}",
        "action: would-refuse",
        "reason: the alice-memory plugin is enabled and install's entries exist",
        "session_start: none",
        f"session_start_path: {resolved / '.claude' / 'settings.json'}",
        "next: run `claude mcp remove alice --scope user`, remove the "
        "alice-memory-session-start hook from ~/.claude/settings.json, or disable the plugin.",
        "dry run: install would refuse this file; nothing was attempted",
    ]
    assert "\n".join(dry_lines) in out
    assert (home / ".claude.json").read_bytes() == before
    assert "launcher:" not in out
    assert "data_dir:" not in out


def test_duplicate_line_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    _seed_server(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "")
    code = hook_main(["--data-dir", str(vault), "--format", "json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "set up twice" not in captured.out

    (tmp_path / ".claude.json").unlink()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin"))
    code = hook_main(["--data-dir", str(vault), "--format", "markdown"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "set up twice" not in captured.out

    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "uvx alice-memory-session-start --data-dir /v",
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    code = hook_main(["--data-dir", str(vault), "--format", "json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("Alice is set up twice in Claude Code.")
    assert not context.lstrip().startswith("{")
    assert not context.lstrip().startswith("[")

    settings.write_text(
        json.dumps({"permissions": {"allow": ["Bash(alice-memory-session-start)"]}}),
        encoding="utf-8",
    )
    code = hook_main(["--data-dir", str(vault), "--format", "markdown"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "set up twice" not in captured.out


def test_bad_claude_config_keeps_the_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin"))
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / ".claude.json").write_bytes(b"\xff")
    code = hook_main(["--data-dir", str(vault), "--format", "markdown"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "Nothing stored yet." in captured.out

    (tmp_path / ".claude.json").unlink()
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b"\xff")
    code = hook_main(["--data-dir", str(vault), "--format", "markdown"])
    captured = capsys.readouterr()
    assert code == 0
    assert "Nothing stored yet." in captured.out
    assert "set up twice" not in captured.out

    nested = "[" * 100_000 + "]" * 100_000
    with pytest.raises(RecursionError):
        json.loads(nested)
    settings.write_text(nested, encoding="utf-8")
    code = hook_main(["--data-dir", str(vault), "--format", "markdown"])
    captured = capsys.readouterr()
    assert code == 0
    assert "Nothing stored yet." in captured.out
    (tmp_path / ".claude.json").write_text(nested, encoding="utf-8")
    code = hook_main(["--data-dir", str(vault), "--format", "markdown"])
    captured = capsys.readouterr()
    assert code == 0
    assert "Nothing stored yet." in captured.out

    real_is_file = Path.is_file

    def unreadable(self: Path) -> bool:
        if self.name == "settings.json" and ".claude" in self.parts:
            raise PermissionError("unreadable")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", unreadable)
    code = hook_main(["--data-dir", str(vault), "--format", "markdown"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "Nothing stored yet." in captured.out


def _good_market(tmp_path: Path) -> Path:
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
    path = market / "marketplace.json"
    path.write_text(
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
    return path


def test_marketplace_rules_each_fail_on_their_own(tmp_path: Path) -> None:
    path = _good_market(tmp_path)
    base = json.loads(path.read_text(encoding="utf-8"))

    def issues_for(mutator) -> list[str]:
        doc = json.loads(json.dumps(base))
        mutator(doc)
        path.write_text(json.dumps(doc), encoding="utf-8")
        return release_check._marketplace_issues(tmp_path, path)

    assert any("description is missing" in item for item in issues_for(lambda doc: doc.pop("description")))
    assert any("name is not alicememory" in item for item in issues_for(lambda doc: doc.__setitem__("name", "other")))
    assert any(
        "url is not samrusani/AliceMemory" in item
        for item in issues_for(lambda doc: doc["plugins"][0]["source"].__setitem__("url", "other/repo"))
    )
    assert any(
        "path is not plugins/alice-memory" in item
        for item in issues_for(lambda doc: doc["plugins"][0]["source"].__setitem__("path", "other"))
    )
    assert any(
        "ref is not a v tag" in item
        for item in issues_for(lambda doc: doc["plugins"][0]["source"].__setitem__("ref", "main"))
    )
    assert any(
        "no dated CHANGELOG heading" in item
        for item in issues_for(lambda doc: doc["plugins"][0]["source"].__setitem__("ref", "v9.9.9"))
    )
    assert any(
        "sha is not 40 lowercase hex characters" in item
        for item in issues_for(lambda doc: doc["plugins"][0]["source"].__setitem__("sha", "A" * 40))
    )
    assert any(
        "sha is not 40 lowercase hex characters" in item
        for item in issues_for(lambda doc: doc["plugins"][0]["source"].__setitem__("sha", "a" * 39))
    )
    assert any(
        "source is not git-subdir" in item
        for item in issues_for(lambda doc: doc["plugins"][0]["source"].__setitem__("source", "github"))
    )
    assert any(
        "entry name does not match" in item
        for item in issues_for(lambda doc: doc["plugins"][0].__setitem__("name", "other"))
    )
    bad = json.loads(json.dumps(base))
    bad["name"] = "other"
    path.write_text(json.dumps(bad), encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "alice-memory"\nversion = "1.2.3"\nreadme = "docs/pypi-description.md"\n',
        encoding="utf-8",
    )
    description = tmp_path / "docs" / "pypi-description.md"
    description.parent.mkdir(parents=True, exist_ok=True)
    description.write_text("# Alice Memory\n\nEvergreen package description.\n", encoding="utf-8")
    web_dir = tmp_path / "apps" / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    (web_dir / "package.json").write_text(
        '{"name":"@alicebot/web","private":true,"version":"1.2.3"}\n',
        encoding="utf-8",
    )
    package_dir = tmp_path / "apps" / "api" / "src" / "alicebot_api"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "main.py").write_text(
        'app = FastAPI(title="AliceBot API", version=__version__)\n',
        encoding="utf-8",
    )
    (package_dir / "__init__.py").write_text(
        "from importlib.metadata import version as _distribution_version\n"
        '__version__ = _distribution_version("alice-memory")\n',
        encoding="utf-8",
    )
    manifest_dir = tmp_path / "packaging" / "mcpb"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "manifest.json").write_text('{"version": "1.2.3"}\n', encoding="utf-8")
    _metadata, issues = release_check.validate_metadata(tmp_path)
    assert any("name is not alicememory" in item for item in issues)


def test_plugin_pin_rules_reject_drift_and_a_longer_version(tmp_path: Path) -> None:
    from tests.unit.test_release_check import _seed_metadata_tree

    _seed_metadata_tree(tmp_path, python_version="1.2.3", web_version="1.2.3")
    plugin = tmp_path / "plugins" / "alice-memory" / ".claude-plugin" / "plugin.json"
    hooks = tmp_path / "plugins" / "alice-memory" / "hooks" / "hooks.json"
    mcp = tmp_path / "plugins" / "alice-memory" / ".mcp.json"
    saved = {path: path.read_text(encoding="utf-8") for path in (plugin, hooks, mcp)}

    loaded = json.loads(saved[plugin])
    loaded["version"] = "9.9.9"
    plugin.write_text(json.dumps(loaded), encoding="utf-8")
    _metadata, issues = release_check.validate_metadata(tmp_path)
    assert any("plugin.json version does not match" in item for item in issues)

    for path, text in saved.items():
        path.write_text(text, encoding="utf-8")
    hook_doc = json.loads(saved[hooks])
    hook_doc["hooks"]["SessionStart"][0]["hooks"][0]["args"][1] = "alice-memory==9.9.9"
    hooks.write_text(json.dumps(hook_doc), encoding="utf-8")
    _metadata, issues = release_check.validate_metadata(tmp_path)
    assert any("hook command does not pin alice-memory==1.2.3" in item for item in issues)

    for path, text in saved.items():
        path.write_text(text, encoding="utf-8")
    mcp_doc = json.loads(saved[mcp])
    mcp_doc["mcpServers"]["alice"]["args"][1] = "alice-memory==1.2.30"
    mcp.write_text(json.dumps(mcp_doc), encoding="utf-8")
    _metadata, issues = release_check.validate_metadata(tmp_path)
    assert any("mcp command does not pin alice-memory==1.2.3" in item for item in issues)

    for path, text in saved.items():
        path.write_text(text, encoding="utf-8")
    mcp_doc = json.loads(saved[mcp])
    mcp_doc["mcpServers"]["alice"]["args"][0] = "--with"
    mcp.write_text(json.dumps(mcp_doc), encoding="utf-8")
    _metadata, issues = release_check.validate_metadata(tmp_path)
    assert any("mcp command does not pin alice-memory==1.2.3" in item for item in issues)

    for path, text in saved.items():
        path.write_text(text, encoding="utf-8")
    hook_doc = json.loads(saved[hooks])
    hook_doc["hooks"]["SessionStart"][0]["hooks"][0]["args"][0] = "--with"
    hooks.write_text(json.dumps(hook_doc), encoding="utf-8")
    _metadata, issues = release_check.validate_metadata(tmp_path)
    assert any("hook command does not pin alice-memory==1.2.3" in item for item in issues)


def test_plugin_hook_args_must_be_exactly_the_pinned_command(tmp_path: Path) -> None:
    """The hook's args are exactly ``--from``, the pin and the script, and nothing else.

    A ``--data-dir`` with ``${user_config.data_dir}`` is the shape Claude Code
    refuses to run when the option is unset, so it must be an issue. The
    server's rule is unchanged: it only has to start with the pin.

    Mutations: go back to the prefix rule ``args[:2] == ["--from", pin]`` for
    the hook (every extra-arg case below returns no issue), drop the script
    name from the required tail (the missing and wrong script cases), or apply
    the exact rule to the server too (the clean tree and the long server args
    are then reported).
    """

    from tests.unit.test_release_check import _seed_metadata_tree

    _seed_metadata_tree(tmp_path, python_version="1.2.3", web_version="1.2.3")
    hooks = tmp_path / "plugins" / "alice-memory" / "hooks" / "hooks.json"
    mcp = tmp_path / "plugins" / "alice-memory" / ".mcp.json"
    good = json.loads(hooks.read_text(encoding="utf-8"))
    pin = "alice-memory==1.2.3"
    assert good["hooks"]["SessionStart"][0]["hooks"][0]["args"] == [
        "--from",
        pin,
        "alice-memory-session-start",
    ]
    assert release_check._plugin_metadata_issues(tmp_path, "1.2.3") == []
    exact = ["--from", pin, "alice-memory-session-start"]
    not_exact = [f"hook command args are not exactly {json.dumps(exact)}"]

    def issues_for(args: list[object]) -> list[str]:
        doc = json.loads(json.dumps(good))
        doc["hooks"]["SessionStart"][0]["hooks"][0]["args"] = args
        hooks.write_text(json.dumps(doc), encoding="utf-8")
        return release_check._plugin_metadata_issues(tmp_path, "1.2.3")

    for args in (
        [*exact, "--data-dir", "${user_config.data_dir}"],
        [*exact, "--data-dir", "~/.alice"],
        [*exact, "extra"],
        ["--from", pin],
        ["--from", pin, "alice-memory"],
        ["--from", pin, "alice-memory-session-start-x"],
        ["--from", pin, "--data-dir", "/v", "alice-memory-session-start"],
        ["--from", pin, "Alice-Memory-Session-Start"],
    ):
        assert issues_for(args) == not_exact, args
    assert issues_for(["--with", pin, "alice-memory-session-start"]) == [f"hook command does not pin {pin}"]
    assert issues_for(["--from", "alice-memory==9.9.9", "alice-memory-session-start"]) == [
        f"hook command does not pin {pin}"
    ]
    assert issues_for(exact) == []

    long_server = json.loads(mcp.read_text(encoding="utf-8"))
    long_server["mcpServers"]["alice"]["args"] = [
        "--from",
        pin,
        "alice-memory",
        "mcp",
        "--data-dir",
        "${user_config.data_dir}",
    ]
    mcp.write_text(json.dumps(long_server), encoding="utf-8")
    assert release_check._plugin_metadata_issues(tmp_path, "1.2.3") == []


def test_marketplace_owner_and_plugin_name_are_checked(tmp_path: Path) -> None:
    path = _good_market(tmp_path)
    base = json.loads(path.read_text(encoding="utf-8"))
    missing = json.loads(json.dumps(base))
    missing.pop("owner")
    path.write_text(json.dumps(missing), encoding="utf-8")
    assert any("owner is missing" in item for item in release_check._marketplace_issues(tmp_path, path))

    for owner in ("Alice Memory", ["Alice Memory"], None, {}, {"name": ""}, {"name": "  "}, {"name": 7}):
        shaped = json.loads(json.dumps(base))
        shaped["owner"] = owner
        path.write_text(json.dumps(shaped), encoding="utf-8")
        issues = release_check._marketplace_issues(tmp_path, path)
        assert any("owner is missing" in item for item in issues), (owner, issues)

    path.write_text(json.dumps(base), encoding="utf-8")
    assert release_check._marketplace_issues(tmp_path, path) == []
    plugin = tmp_path / "plugins" / "alice-memory" / ".claude-plugin" / "plugin.json"
    loaded = json.loads(plugin.read_text(encoding="utf-8"))
    loaded["name"] = "other-plugin"
    plugin.write_text(json.dumps(loaded), encoding="utf-8")
    issues = release_check._marketplace_issues(tmp_path, path)
    assert any("entry name does not match plugin.json" in item for item in issues)

    plugin.write_bytes(b"\xff")
    issues = release_check._marketplace_issues(tmp_path, path)
    assert isinstance(issues, list)
    path.write_bytes(b"\xff")
    assert release_check._marketplace_issues(tmp_path, path) == [
        ".claude-plugin/marketplace.json is missing or unreadable"
    ]


def test_marketplace_changelog_read_failure_is_an_issue_not_a_crash(tmp_path: Path) -> None:
    path = _good_market(tmp_path)
    assert release_check._marketplace_issues(tmp_path, path) == []
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_bytes(b"\xff")
    issues = release_check._marketplace_issues(tmp_path, path)
    assert [item for item in issues if "CHANGELOG" in item] == [
        ".claude-plugin/marketplace.json ref has no dated CHANGELOG heading"
    ]
    changelog.unlink()
    issues = release_check._marketplace_issues(tmp_path, path)
    assert ".claude-plugin/marketplace.json ref has no dated CHANGELOG heading" in issues


def test_plugin_reads_treat_non_utf8_as_an_issue(tmp_path: Path) -> None:
    from tests.unit.test_release_check import _seed_metadata_tree

    _seed_metadata_tree(tmp_path, python_version="1.2.3", web_version="1.2.3")
    plugin = tmp_path / "plugins" / "alice-memory" / ".claude-plugin" / "plugin.json"
    plugin_bytes = plugin.read_bytes()
    plugin.write_bytes(b"\xff")
    issues = release_check._plugin_metadata_issues(tmp_path, "1.2.3")
    assert any("plugin.json is missing or unreadable" in item for item in issues)
    plugin.write_bytes(plugin_bytes)
    assert release_check._plugin_metadata_issues(tmp_path, "1.2.3") == []
    for relative in ("plugins/alice-memory/.mcp.json", "plugins/alice-memory/hooks/hooks.json"):
        path = tmp_path / relative
        saved = path.read_bytes()
        path.write_bytes(b"\xff")
        issues = release_check._plugin_metadata_issues(tmp_path, "1.2.3")
        assert any(f"{relative} is missing or unreadable" in item for item in issues), (relative, issues)
        path.write_bytes(saved)


def test_a_cursor_refusal_is_not_hidden_by_the_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    home = tmp_path / "both"
    vault = tmp_path / "vault"
    _settings(home, {CLAUDE_PLUGIN_ID: True})
    _seed_server(home)
    cursor = home / ".cursor" / "mcp.json"
    cursor.parent.mkdir(parents=True)
    cursor.write_text("{", encoding="utf-8")
    code = onramp_main(
        [
            "install",
            "--home",
            str(home),
            "--host",
            "claude-code",
            "--host",
            "cursor",
            "--data-dir",
            str(vault),
        ]
    )
    captured = capsys.readouterr()
    assert code == 1, captured.out
    assert "install_refused_plugin" not in captured.err
    assert '"code":"install_refused"' in captured.err.replace(" ", "")
    assert "host: claude-code" in captured.out
    assert "host: cursor" in captured.out


def test_duplicate_prefix_is_reserved_and_the_final_fit_cuts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Seven facts reach the cap, the reserve keeps the prefix inside it, and the fit cuts nothing.

    The first fact is short and the six after it are 20,000 characters each,
    so the brief fills every slot. Without the reserve the compiled brief plus
    the prefix passes 9,500. Mutation: drop ``reserve=`` from the hook's
    compile call. ``additionalContext`` no longer matches the reserved brief,
    and this test fails.
    """

    import io

    from alicebot_api.onramp import resolve_db_path
    from alicebot_api.session_briefing import brief_char_len, compile_local_session_brief
    from tests.unit.test_session_brief import USER_ID, _commit, _context

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin"))
    _seed_server(tmp_path)
    vault = tmp_path / "vault"
    context = _context(vault, monkeypatch)
    _commit(
        context,
        title="Short fact",
        text="short" + "s" * 250,
        sensitivity="public",
        project="acme",
        domain="project",
    )
    for index in range(6):
        _commit(
            context,
            title=f"Long fact {index}",
            text=f"n{index}" + chr(ord("a") + index) * 20000,
            sensitivity="public",
            project="acme",
            domain="project",
        )
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code = hook_main(["--data-dir", str(vault), "--user-id", USER_ID, "--format", "json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    additional = payload["hookSpecificOutput"]["additionalContext"]
    assert additional == payload["additional_context"]
    assert additional.startswith("Alice is set up twice in Claude Code.")
    prefix, _sep, _rest = additional.partition("\n")
    prefix = prefix + "\n"
    database = resolve_db_path(data_dir=str(vault), db=None)
    unreserved = compile_local_session_brief(database, user_id=USER_ID, query=None, reserve=0)
    assert brief_char_len(prefix + unreserved) >= 9_500
    assert brief_char_len(additional) < 9_500
    expected = compile_local_session_brief(
        database,
        user_id=USER_ID,
        query=None,
        reserve=brief_char_len(prefix),
    )
    assert additional[len(prefix) :] == expected


def test_the_final_fit_covers_the_duplicate_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A compiled brief that ignores its reserve is still cut with the prefix in place.

    The compile call is patched to return 20,000 characters whatever the
    reserve says. Mutation: fit the brief before adding the prefix
    (``prefix + fit(markdown)``). The emitted context passes 9,500 and this
    test fails.
    """

    import io

    from alicebot_api import session_start_hook as hook_module
    from alicebot_api.session_briefing import brief_char_len
    from tests.unit.test_session_brief import USER_ID

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin"))
    _seed_server(tmp_path)
    vault = tmp_path / "vault"
    monkeypatch.setattr(hook_module, "compile_local_session_brief", lambda *args, **kwargs: "z" * 20000)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code = hook_main(["--data-dir", str(vault), "--user-id", USER_ID, "--format", "json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    additional = json.loads(captured.out)["hookSpecificOutput"]["additionalContext"]
    assert additional.startswith("Alice is set up twice in Claude Code.")
    assert brief_char_len(additional + "\n") <= 9_500


_PLUGIN_ENV_NAMES = ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_OPTION_DATA_DIR", "ALICE_MEMORY_DATA_DIR")


def _hook_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh HOME with none of the plugin or data dir variables set."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)
    for name in _PLUGIN_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return home


def _run_hook(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *args: str,
    root: str | None = None,
    option: str | None = None,
    env_dir: str | None = None,
) -> tuple[int, str, str]:
    """Run the session-start hook once with exactly the given plugin variables set."""

    for name in _PLUGIN_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in (
        ("CLAUDE_PLUGIN_ROOT", root),
        ("CLAUDE_PLUGIN_OPTION_DATA_DIR", option),
        ("ALICE_MEMORY_DATA_DIR", env_dir),
    ):
        if value is not None:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code = hook_main([*args, "--format", "markdown"])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _vaults(tmp_path: Path, home: Path) -> set[str]:
    """Every folder under the test's tree that now holds a memory.db."""

    return {
        path.parent.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("memory.db")
    } | ({"home/.alice"} if (home / ".alice" / "memory.db").is_file() else set())


def test_plugin_hook_reads_the_option_and_otherwise_uses_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no --data-dir the plugin hook opens the option's folder, else ~/.alice.

    The plugin's hook carries no ``--data-dir``, because Claude Code does not
    run a hook that references an unset option. An empty option counts as
    unset, and ``ALICE_MEMORY_DATA_DIR`` is set in the empty case so a fall
    through to it shows.

    Mutations: ignore ``CLAUDE_PLUGIN_OPTION_DATA_DIR`` (the set case fails);
    read it with ``os.environ.get(name, default)`` so an empty value passes
    through (the empty case opens ``from-env``); drop the ``~/.alice`` default
    from plugin mode (the unset case fails once ``ALICE_MEMORY_DATA_DIR`` is
    set, see the ignored-variable test).
    """

    home = _hook_home(tmp_path, monkeypatch)
    vault = tmp_path / "option-vault"
    code, out, err = _run_hook(
        monkeypatch, capsys, root=str(tmp_path / "plugin"), option=str(vault)
    )
    assert code == 0, err
    assert "is not an absolute path" not in out
    assert _vaults(tmp_path, home) == {"option-vault"}

    code, out, err = _run_hook(monkeypatch, capsys, root=str(tmp_path / "plugin"))
    assert code == 0, err
    assert _vaults(tmp_path, home) == {"option-vault", "home/.alice"}

    (home / ".alice" / "memory.db").unlink()
    code, out, err = _run_hook(
        monkeypatch,
        capsys,
        root=str(tmp_path / "plugin"),
        option="",
        env_dir=str(tmp_path / "from-env"),
    )
    assert code == 0, err
    assert _vaults(tmp_path, home) == {"option-vault", "home/.alice"}


def test_plugin_hook_refuses_a_relative_option_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A relative option prints the existing one-line refusal, exits 0 and opens nothing.

    Mutation: check only ``--data-dir`` for the absolute rule and let the option
    value through. The refusal line is missing, a vault is created, and this
    test fails.
    """

    home = _hook_home(tmp_path, monkeypatch)
    for value in ("relative/dir", "alice", "${user_config.data_dir}", "$HOME/.alice"):
        expected = f'Alice: the data directory "{value}" is not an absolute path; set an absolute path.'
        code, out, err = _run_hook(
            monkeypatch, capsys, root=str(tmp_path / "plugin"), option=value
        )
        assert code == 0, err
        assert out == expected + "\n", (value, out)
        assert expected in err
        assert _vaults(tmp_path, home) == set(), value

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code = hook_main([])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["hookSpecificOutput"]["additionalContext"] == expected
    assert _vaults(tmp_path, home) == set()


def test_plugin_hook_ignores_alice_memory_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """In plugin mode ``ALICE_MEMORY_DATA_DIR`` never picks the folder.

    The server reads only the plugin option, so the hook must not read the
    variable either, or the two open different vaults.

    Mutations: fall back to ``ALICE_MEMORY_DATA_DIR`` before the default when
    the option is unset (the unset case opens ``from-env``); consult it before
    the option (the set case opens ``from-env``).
    """

    home = _hook_home(tmp_path, monkeypatch)
    env_dir = tmp_path / "from-env"
    option = tmp_path / "option-vault"
    code, out, err = _run_hook(
        monkeypatch, capsys, root=str(tmp_path / "plugin"), env_dir=str(env_dir)
    )
    assert code == 0, err
    assert _vaults(tmp_path, home) == {"home/.alice"}

    (home / ".alice" / "memory.db").unlink()
    code, out, err = _run_hook(
        monkeypatch,
        capsys,
        root=str(tmp_path / "plugin"),
        option=str(option),
        env_dir=str(env_dir),
    )
    assert code == 0, err
    assert _vaults(tmp_path, home) == {"option-vault"}


def test_outside_plugin_mode_the_hook_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without ``CLAUDE_PLUGIN_ROOT`` the option is ignored and the old chain runs.

    ``ALICE_MEMORY_DATA_DIR`` is used, and ``~/.alice`` when it is unset. An
    empty ``CLAUDE_PLUGIN_ROOT`` is not plugin mode either, and a relative
    option is then not refused.

    Mutations: treat the mere presence of ``CLAUDE_PLUGIN_OPTION_DATA_DIR`` as
    plugin mode, or drop the root check (the first two cases open
    ``option-vault``); test ``CLAUDE_PLUGIN_ROOT is not None`` so an empty root
    counts (the last two cases open ``option-vault`` or print the refusal).
    """

    home = _hook_home(tmp_path, monkeypatch)
    env_dir = tmp_path / "from-env"
    option = tmp_path / "option-vault"
    code, out, err = _run_hook(monkeypatch, capsys, env_dir=str(env_dir), option=str(option))
    assert code == 0, err
    assert _vaults(tmp_path, home) == {"from-env"}

    (env_dir / "memory.db").unlink()
    code, out, err = _run_hook(monkeypatch, capsys, option=str(option))
    assert code == 0, err
    assert _vaults(tmp_path, home) == {"home/.alice"}

    (home / ".alice" / "memory.db").unlink()
    code, out, err = _run_hook(
        monkeypatch, capsys, root="", env_dir=str(env_dir), option=str(option)
    )
    assert code == 0, err
    assert _vaults(tmp_path, home) == {"from-env"}

    (env_dir / "memory.db").unlink()
    code, out, err = _run_hook(monkeypatch, capsys, root="", option="relative")
    assert code == 0, err
    assert "is not an absolute path" not in out
    assert _vaults(tmp_path, home) == {"home/.alice"}


def test_an_explicit_data_dir_wins_in_plugin_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-empty ``--data-dir`` beats the option and the environment.

    An explicit relative value is refused by name even when the option is a
    good path. An empty ``--data-dir`` is the same as none, so the option
    applies.

    Mutations: read the option before ``--data-dir`` (the first case opens
    ``option-vault``); validate the option instead of the explicit value, or
    as well as it (the refusal case names the wrong value, or the explicit
    vault with a relative option is refused); treat an empty
    ``--data-dir`` as given (the last case opens no vault or the wrong one).
    """

    home = _hook_home(tmp_path, monkeypatch)
    explicit = tmp_path / "explicit-vault"
    option = tmp_path / "option-vault"
    code, out, err = _run_hook(
        monkeypatch,
        capsys,
        "--data-dir",
        str(explicit),
        root=str(tmp_path / "plugin"),
        option=str(option),
        env_dir=str(tmp_path / "from-env"),
    )
    assert code == 0, err
    assert _vaults(tmp_path, home) == {"explicit-vault"}

    code, out, err = _run_hook(
        monkeypatch, capsys, "--data-dir", "rel", root=str(tmp_path / "plugin"), option=str(option)
    )
    assert code == 0, err
    assert out == 'Alice: the data directory "rel" is not an absolute path; set an absolute path.\n'
    assert _vaults(tmp_path, home) == {"explicit-vault"}

    code, out, err = _run_hook(
        monkeypatch, capsys, "--data-dir", str(explicit), root=str(tmp_path / "plugin"), option="relative"
    )
    assert code == 0, err
    assert "is not an absolute path" not in out
    assert _vaults(tmp_path, home) == {"explicit-vault"}

    code, out, err = _run_hook(
        monkeypatch, capsys, "--data-dir", "", root=str(tmp_path / "plugin"), option=str(option)
    )
    assert code == 0, err
    assert _vaults(tmp_path, home) == {"explicit-vault", "option-vault"}


class _AnthropicStub:
    """Loopback stand-in for the Anthropic API. Records method and path only."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def _reply(handler, method: str) -> None:
                self.records.append((method, handler.path))
                length = int(handler.headers.get("Content-Length") or 0)
                if length:
                    handler.rfile.read(length)
                body = (
                    b'{"type":"error","error":{"type":"invalid_request_error",'
                    b'"message":"alice test stub"}}'
                )
                handler.send_response(400)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

            def do_POST(handler) -> None:  # noqa: N802
                handler._reply("POST")

            def do_GET(handler) -> None:  # noqa: N802
                handler._reply("GET")

            def log_message(handler, _format: str, *_args: object) -> None:  # noqa: N802
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _claude_env(base: dict[str, str], stub: _AnthropicStub) -> dict[str, str]:
    env = dict(base)
    env["ANTHROPIC_BASE_URL"] = stub.base_url
    env["ANTHROPIC_API_KEY"] = "alice-test-" + secrets.token_hex(8)
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"] = "1"
    return env


def _uvx_records(log_path: Path) -> list[str]:
    """Every line the stub uvx wrote, raw, so a failure shows all of them."""

    if not log_path.is_file():
        return []
    return [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _claude_detail(
    result: subprocess.CompletedProcess[str], stub: _AnthropicStub, log_path: Path
) -> str:
    """The return code, the first stderr line, the stub's requests and every uvx record."""

    lines = (result.stderr or "").splitlines()
    first = lines[0] if lines else ""
    out = (result.stdout or "").splitlines()
    return (
        f"returncode={result.returncode} stderr={first!r} stdout={out[:3]!r} "
        f"stub={stub.records} uvx={_uvx_records(log_path)}"
    )


def _messages_posts(records: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """POSTs to the Messages endpoint. Claude Code sends ``/v1/messages?beta=true``."""

    return [
        item
        for item in records
        if item[0] == "POST" and urlsplit(item[1]).path.endswith("/v1/messages")
    ]


def _check_plugin_rows(
    records: list[dict],
    pin: str,
    *,
    expected_option: str | None,
    server_data_dir: str,
    detail: str,
) -> None:
    """Assert the hook and server rows one plugin session wrote.

    The hook argv is exactly ``--from``, the pin and the script, with no
    ``--data-dir``. ``CLAUDE_PLUGIN_ROOT`` is non-empty in the hook. With
    ``expected_option`` None the plugin option is unset and
    ``CLAUDE_PLUGIN_OPTION_DATA_DIR`` is absent or empty in the hook. Otherwise
    it equals ``expected_option``. The server argv ends with
    ``--data-dir <server_data_dir>``, and at least one server row exists.
    """

    for row in records:
        if any("${user_config" in str(item) for item in row["argv"]):
            raise AssertionError("unsubstituted; stop and report. " + detail)
    hook_rows = [row for row in records if "alice-memory-session-start" in row["argv"]]
    assert [row["argv"] for row in hook_rows] == [
        ["--from", pin, "alice-memory-session-start"]
    ], detail
    assert hook_rows[0].get("CLAUDE_PLUGIN_ROOT"), detail
    option = hook_rows[0].get("CLAUDE_PLUGIN_OPTION_DATA_DIR")
    if expected_option is None:
        assert not option, detail
    else:
        assert option == expected_option, detail
    server_rows = [
        row
        for row in records
        if "alice-memory" in row["argv"]
        and "mcp" in row["argv"]
        and "alice-memory-session-start" not in row["argv"]
    ]
    assert server_rows, detail
    for row in server_rows:
        assert row["argv"] == ["--from", pin, "alice-memory", "mcp", "--data-dir", server_data_dir], detail


def _claude(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["claude", *args],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError("claude blocked; stop and report") from exc


def test_stub_records_the_query_string_and_the_filter_strips_it() -> None:
    """Claude Code posts to ``/v1/messages?beta=true``, and that still counts as a Messages POST.

    Mutation: match ``item[1].endswith("/v1/messages")`` on the raw path. The
    query string hides the POST and this test fails.
    """

    import urllib.error
    import urllib.request

    api = _AnthropicStub()
    try:
        for path in ("/v1/messages?beta=true", "/v1/messages", "/v1/other?next=/v1/messages"):
            request = urllib.request.Request(api.base_url + path, data=b"{}", method="POST")
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=10)  # noqa: S310
            assert caught.value.code == 400
        request = urllib.request.Request(api.base_url + "/v1/messages?beta=true", method="GET")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(request, timeout=10)  # noqa: S310
        records = list(api.records)
    finally:
        api.close()
    assert records == [
        ("POST", "/v1/messages?beta=true"),
        ("POST", "/v1/messages"),
        ("POST", "/v1/other?next=/v1/messages"),
        ("GET", "/v1/messages?beta=true"),
    ]
    assert _messages_posts(records) == [
        ("POST", "/v1/messages?beta=true"),
        ("POST", "/v1/messages"),
    ]


def test_claude_failure_detail_carries_the_code_stderr_stub_and_uvx_records(tmp_path: Path) -> None:
    """A failed step prints why: return code, first stderr line, the first three stdout lines, every stub and uvx record.

    Mutation: build the detail from stderr alone, or from the first uvx row,
    keep one stdout line instead of three, or let a blank line into the uvx
    list. This test fails.
    """

    log_path = tmp_path / "uvx.jsonl"
    log_path.write_text('{"argv": ["one"]}\n\n{"argv": ["two"]}\n', encoding="utf-8")
    api = _AnthropicStub()
    try:
        api.records.extend([("POST", "/v1/messages?beta=true"), ("GET", "/v1/models")])
        result = subprocess.CompletedProcess(
            ["claude"],
            7,
            stdout="out one\nout two\nout three\nout four\n",
            stderr="first line\nsecond line\n",
        )
        detail = _claude_detail(result, api, log_path)
        empty = _claude_detail(subprocess.CompletedProcess(["claude"], 0, "", ""), api, tmp_path / "none")
    finally:
        api.close()
    assert "returncode=7" in detail
    assert "stderr='first line'" in detail
    assert "second line" not in detail
    assert "stdout=['out one', 'out two', 'out three']" in detail
    assert "out four" not in detail
    assert _uvx_records(log_path) == ['{"argv": ["one"]}', '{"argv": ["two"]}']
    assert "uvx=['{\"argv\": [\"one\"]}', '{\"argv\": [\"two\"]}']" in detail
    assert "('POST', '/v1/messages?beta=true')" in detail and "('GET', '/v1/models')" in detail
    assert '{"argv": ["one"]}' in detail and '{"argv": ["two"]}' in detail
    assert "returncode=0 stderr='' stdout=[]" in empty and "uvx=[]" in empty


def _plugin_row_set(
    *, option: str | None, data_dir: str = "~/.alice", root: str | None = "/plugins/alice-memory"
) -> list[dict]:
    pin = "alice-memory==1.2.3"
    return [
        {
            "argv": ["--from", pin, "alice-memory-session-start"],
            "CLAUDE_PLUGIN_ROOT": root,
            "CLAUDE_PLUGIN_OPTION_DATA_DIR": option,
        },
        {
            "argv": ["--from", pin, "alice-memory", "mcp", "--data-dir", data_dir],
            "CLAUDE_PLUGIN_ROOT": root,
            "CLAUDE_PLUGIN_OPTION_DATA_DIR": None,
        },
    ]


def test_the_plugin_row_check_accepts_the_two_real_shapes_and_nothing_near_them() -> None:
    """``_check_plugin_rows`` is what the real-host test asserts for both sessions.

    Unset option: the hook argv is exactly the pinned command, the option is
    absent or empty, and the server row carries ``~/.alice``. Set option: the
    option equals the vault and the server row carries the vault.

    Mutations, each one alone: allow ``--data-dir`` in the hook argv; drop the
    ``CLAUDE_PLUGIN_ROOT`` check; accept a set option when it should be unset;
    accept any option when a value is expected; drop the non-empty server row
    check; compare the server row to ``~/.alice`` whatever the vault is; drop
    the unsubstituted check. Each case below then stops raising.
    """

    pin = "alice-memory==1.2.3"
    vault = "/case/vault"

    def passes(rows: list[dict], *, option: str | None, data_dir: str = "~/.alice") -> None:
        _check_plugin_rows(rows, pin, expected_option=option, server_data_dir=data_dir, detail="d")

    def fails(rows: list[dict], *, option: str | None, data_dir: str = "~/.alice") -> str:
        with pytest.raises(AssertionError) as caught:
            passes(rows, option=option, data_dir=data_dir)
        return str(caught.value)

    passes(_plugin_row_set(option=None), option=None)
    passes(_plugin_row_set(option=""), option=None)
    passes(_plugin_row_set(option=vault, data_dir=vault), option=vault, data_dir=vault)

    fails(_plugin_row_set(option=vault), option=None)
    fails(_plugin_row_set(option=None, data_dir=vault), option=vault, data_dir=vault)
    fails(_plugin_row_set(option="/other", data_dir=vault), option=vault, data_dir=vault)
    fails(_plugin_row_set(option="", data_dir=vault), option=vault, data_dir=vault)
    fails(_plugin_row_set(option=vault, data_dir="~/.alice"), option=vault, data_dir=vault)
    fails(_plugin_row_set(option=None, data_dir=vault), option=None)

    for root in (None, ""):
        fails(_plugin_row_set(option=None, root=root), option=None)

    extra = _plugin_row_set(option=None)
    extra[0]["argv"] = [*extra[0]["argv"], "--data-dir", "~/.alice"]
    fails(extra, option=None)
    with_placeholder = _plugin_row_set(option=None)
    with_placeholder[0]["argv"] = [*with_placeholder[0]["argv"], "--data-dir", "${user_config.data_dir}"]
    assert "unsubstituted" in fails(with_placeholder, option=None)
    server_placeholder = _plugin_row_set(option=None, data_dir="${user_config.data_dir}")
    assert "unsubstituted" in fails(server_placeholder, option=None, data_dir="${user_config.data_dir}")

    hook_only = _plugin_row_set(option=None)[:1]
    fails(hook_only, option=None)
    server_only = _plugin_row_set(option=None)[1:]
    fails(server_only, option=None)
    fails([], option=None)
    twice = _plugin_row_set(option=None)
    fails([twice[0], *twice], option=None)
    wrong_pin = _plugin_row_set(option=None)
    wrong_pin[0]["argv"][1] = "alice-memory==9.9.9"
    fails(wrong_pin, option=None)


def test_the_real_host_test_wires_the_plugin_row_check_for_both_sessions() -> None:
    """The real-host test calls the row check with the unset option, then with the vault.

    The real-host test cannot run in the unit job, so this reads its source. The
    first session expects no option and ``~/.alice`` on the server. The second
    session, installed with ``--config data_dir=<vault>``, expects the vault
    as the option and on the server.

    Mutation: pass ``expected_option=None`` or ``server_data_dir="~/.alice"`` in
    the second call, swap the two calls, or drop either call. This test fails.
    """

    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "test_real_claude_plugin_install_and_run"
    ]
    assert len(functions) == 1
    calls = [
        node
        for node in ast.walk(functions[0])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_check_plugin_rows"
    ]
    calls.sort(key=lambda call: call.lineno)
    shown = [
        (
            [ast.unparse(arg) for arg in call.args],
            {keyword.arg: ast.unparse(keyword.value) for keyword in call.keywords},
        )
        for call in calls
    ]
    assert shown == [
        (
            ["records", "pin"],
            {"expected_option": "None", "server_data_dir": "'~/.alice'", "detail": "detail"},
        ),
        (
            ["records", "pin"],
            {
                "expected_option": "str(vault_b)",
                "server_data_dir": "str(vault_b)",
                "detail": "detail",
            },
        ),
    ]


def test_the_real_host_test_writes_only_under_tmp_path() -> None:
    """The real-host test leaves nothing at a fixed path outside ``tmp_path``.

    Mutation: write the no-mock note to a fixed path under the system temp
    directory again. This test fails.
    """

    source = Path(__file__).read_text(encoding="utf-8")
    fixed = "/" + "tmp/"
    assert fixed not in source
    assert "tempfile." + "gettempdir" not in source


@pytest.mark.skipif(os.environ.get(REAL_HOSTS_ENV) != "1", reason="set ALICE_TEST_REAL_HOSTS=1")
@pytest.mark.skipif(sys.platform != "linux", reason="the Claude Code real-host check runs on Linux")
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not on PATH")
def test_real_claude_plugin_install_and_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Install the plugin from a marketplace whose file is under .claude-plugin.

    The marketplace file is `<root>/.claude-plugin/marketplace.json`. A relative
    source resolves from that root. This test does not accept a marketplace
    file at the directory root.
    """

    version = _version()
    pin = f"alice-memory=={version}"
    home = tmp_path / "home"
    project = tmp_path / "project"
    market = tmp_path / "market"
    vault_b = tmp_path / "vault-b"
    log_path = tmp_path / "uvx.jsonl"
    (home / ".claude").mkdir(parents=True)
    project.mkdir()
    vault_b.mkdir()
    plugin_copy = market / "plugins" / "alice-memory"
    shutil.copytree(PLUGIN, plugin_copy)
    market_dir = market / ".claude-plugin"
    market_dir.mkdir()
    (market_dir / "marketplace.json").write_text(
        json.dumps(
            {
                "name": CLAUDE_MARKETPLACE_NAME,
                "owner": {"name": "Alice Memory"},
                "description": "Alice Memory plugins",
                "plugins": [{"name": "alice-memory", "source": "./plugins/alice-memory"}],
            }
        ),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uvx"
    stub.write_text(
        "\n".join(
            (
                f"#!{sys.executable}",
                "import json, os, sys",
                "row = {",
                '    "argv": sys.argv[1:],',
                '    "CLAUDE_PLUGIN_ROOT": os.environ.get("CLAUDE_PLUGIN_ROOT"),',
                '    "CLAUDE_PLUGIN_OPTION_DATA_DIR": os.environ.get("CLAUDE_PLUGIN_OPTION_DATA_DIR"),',
                "}",
                f"path = {str(log_path)!r}",
                "with open(path, 'a', encoding='utf-8') as handle:",
                "    handle.write(json.dumps(row) + '\\n')",
                "",
            )
        ),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env = os.environ.copy()
    for name in (
        "CLAUDE_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_PLUGIN_ROOT",
    ):
        env.pop(name, None)
    env["HOME"] = str(home)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"] = "1"
    api = _AnthropicStub()
    try:
        claude_env = _claude_env(env, api)
        bare = _claude(["-p", "ok"], cwd=project, env=env)
        bare_line = (bare.stderr or "").splitlines()
        note = f"{bare.returncode}\n{bare_line[0] if bare_line else ''}\n"
        (tmp_path / "nomock.txt").write_text(note, encoding="utf-8")
        print("no-mock run:", note)

        added = _claude(["plugin", "marketplace", "add", str(market)], cwd=project, env=claude_env)
        assert added.returncode == 0, _claude_detail(added, api, log_path)
        installed = _claude(["plugin", "install", CLAUDE_PLUGIN_ID], cwd=project, env=claude_env)
        assert installed.returncode == 0, _claude_detail(installed, api, log_path)
        settings_path = home / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert settings["enabledPlugins"][CLAUDE_PLUGIN_ID] is True
        listed = _claude(["plugin", "list", "--json"], cwd=project, env=claude_env)
        assert listed.returncode == 0, _claude_detail(listed, api, log_path)
        payload = json.loads(listed.stdout)
        rows = payload if isinstance(payload, list) else payload.get("plugins", [])
        matched = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("id") == CLAUDE_PLUGIN_ID
            and row.get("enabled") is True
            and row.get("scope") == "user"
        ]
        assert len(rows) == 1 and len(matched) == 1, payload
        assert matched[0]["version"] == version
        claude_json = home / ".claude.json"
        if claude_json.is_file():
            loaded = json.loads(claude_json.read_text(encoding="utf-8"))
            servers = loaded.get("mcpServers") if isinstance(loaded, dict) else None
            assert not (isinstance(servers, dict) and "alice" in servers)
        assert "alice-memory-session-start" not in json.dumps(settings.get("hooks", {}))

        log_path.write_text("", encoding="utf-8")
        api.records.clear()
        prompted = _claude(["-p", "ok"], cwd=project, env=claude_env)
        detail = _claude_detail(prompted, api, log_path)
        posts = _messages_posts(api.records)
        assert posts, detail
        records = [
            json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        hook_rows = [row for row in records if "alice-memory-session-start" in row["argv"]]
        if posts and not hook_rows:
            raise AssertionError(
                "stub saw a request and the hook row is missing; stop and report. " + detail
            )
        _check_plugin_rows(
            records,
            pin,
            expected_option=None,
            server_data_dir="~/.alice",
            detail=detail,
        )
        option = hook_rows[0].get("CLAUDE_PLUGIN_OPTION_DATA_DIR")
        note = (tmp_path / "nomock.txt").read_text(encoding="utf-8")
        note = note + f"option={option!r}\n"
        (tmp_path / "nomock.txt").write_text(note, encoding="utf-8")
        print("no-mock run and option:", note)

        removed = _claude(["plugin", "uninstall", CLAUDE_PLUGIN_ID], cwd=project, env=claude_env)
        assert removed.returncode == 0, _claude_detail(removed, api, log_path)
        configured = _claude(
            ["plugin", "install", CLAUDE_PLUGIN_ID, "--config", f"data_dir={vault_b}"],
            cwd=project,
            env=claude_env,
        )
        assert configured.returncode == 0, _claude_detail(configured, api, log_path)
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert settings["pluginConfigs"][CLAUDE_PLUGIN_ID]["options"]["data_dir"] == str(vault_b)
        log_path.write_text("", encoding="utf-8")
        api.records.clear()
        prompted = _claude(["-p", "ok"], cwd=project, env=claude_env)
        detail = _claude_detail(prompted, api, log_path)
        records = [
            json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        hook_rows = [row for row in records if "alice-memory-session-start" in row["argv"]]
        posts = _messages_posts(api.records)
        if posts and not hook_rows:
            raise AssertionError(
                "stub saw a request and the hook row is missing; stop and report. " + detail
            )
        assert hook_rows, detail
        _check_plugin_rows(
            records,
            pin,
            expected_option=str(vault_b),
            server_data_dir=str(vault_b),
            detail=detail,
        )
    finally:
        api.close()

    before_settings = settings_path.read_bytes()
    before_claude = claude_json.read_bytes() if claude_json.is_file() else None
    code = onramp_main(["install", "--host", "claude-code", "--home", str(home)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "skipped (the alice-memory plugin is enabled)" in captured.out
    assert "session_start: skipped" in captured.out
    assert settings_path.read_bytes() == before_settings
    if before_claude is None:
        assert not claude_json.is_file() or claude_json.read_bytes() == b""
    else:
        assert claude_json.read_bytes() == before_claude
    assert not (home / ".alice" / "backups").exists()

    loaded = json.loads(claude_json.read_text(encoding="utf-8")) if claude_json.is_file() else {}
    servers = loaded.setdefault("mcpServers", {})
    servers["alice"] = {"command": "uvx", "args": ["alice-memory", "mcp"]}
    claude_json.write_text(json.dumps(loaded), encoding="utf-8")
    before_settings = settings_path.read_bytes()
    before_claude = claude_json.read_bytes()
    code = onramp_main(["install", "--host", "claude-code", "--home", str(home)])
    captured = capsys.readouterr()
    assert code == 1, captured.out
    assert "claude mcp remove alice --scope user" in captured.out
    assert settings_path.read_bytes() == before_settings
    assert claude_json.read_bytes() == before_claude


