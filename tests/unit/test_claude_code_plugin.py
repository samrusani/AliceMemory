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
                            "args": [
                                "--from",
                                pin,
                                "alice-memory-session-start",
                                "--data-dir",
                                "${user_config.data_dir}",
                            ],
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
    assert "${user_config.data_dir}" in handler["args"]
    assert f"alice-memory=={version}" in handler["args"]
    expected_plugin, expected_mcp, expected_hooks = _expected_plugin_documents(version)
    assert plugin == expected_plugin
    assert mcp == expected_mcp
    assert hooks == expected_hooks
    market = ROOT / ".claude-plugin" / "marketplace.json"
    if market.is_file():
        assert release_check._marketplace_issues(ROOT, market) == []


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
    assert "would-refuse" in out
    assert "next:" in out
    assert "claude mcp remove alice --scope user" in out
    assert (home / ".claude.json").read_bytes() == before
    assert "session_start_launcher:" not in out


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
    assert "set up twice" not in captured.out

    settings.write_text("[" * 3000 + "]" * 3000, encoding="utf-8")
    code = hook_main(["--data-dir", str(vault), "--format", "markdown"])
    captured = capsys.readouterr()
    assert code == 0
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

    added = _claude(["plugin", "marketplace", "add", str(market)], cwd=project, env=env)
    assert added.returncode == 0, (added.stdout, added.stderr)
    installed = _claude(["plugin", "install", CLAUDE_PLUGIN_ID], cwd=project, env=env)
    assert installed.returncode == 0, (installed.stdout, installed.stderr)
    settings_path = home / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["enabledPlugins"][CLAUDE_PLUGIN_ID] is True
    listed = _claude(["plugin", "list", "--json"], cwd=project, env=env)
    assert listed.returncode == 0, (listed.stdout, listed.stderr)
    payload = json.loads(listed.stdout)
    rows = payload if isinstance(payload, list) else payload.get("plugins", [])
    matched = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("id") == CLAUDE_PLUGIN_ID and row.get("enabled") is True and row.get("scope") == "user"
    ]
    assert len(rows) == 1 and len(matched) == 1, payload
    claude_json = home / ".claude.json"
    if claude_json.is_file():
        loaded = json.loads(claude_json.read_text(encoding="utf-8"))
        servers = loaded.get("mcpServers") if isinstance(loaded, dict) else None
        assert not (isinstance(servers, dict) and "alice" in servers)
    assert "alice-memory-session-start" not in json.dumps(settings.get("hooks", {}))

    log_path.write_text("", encoding="utf-8")
    _claude(["-p", "ok"], cwd=project, env=env)
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in records:
        if any("${user_config" in str(item) for item in row["argv"]):
            raise AssertionError("unsubstituted; stop and report")
    hook_rows = [row for row in records if "alice-memory-session-start" in row["argv"]]
    assert [row["argv"] for row in hook_rows] == [
        ["--from", pin, "alice-memory-session-start", "--data-dir", "~/.alice"]
    ]
    assert hook_rows and hook_rows[0]["CLAUDE_PLUGIN_ROOT"]
    server_rows = [
        row
        for row in records
        if "alice-memory" in row["argv"] and "mcp" in row["argv"] and "alice-memory-session-start" not in row["argv"]
    ]
    for row in server_rows:
        assert row["argv"] == ["--from", pin, "alice-memory", "mcp", "--data-dir", "~/.alice"]

    removed = _claude(["plugin", "uninstall", CLAUDE_PLUGIN_ID], cwd=project, env=env)
    assert removed.returncode == 0, (removed.stdout, removed.stderr)
    configured = _claude(
        ["plugin", "install", CLAUDE_PLUGIN_ID, "--config", f"data_dir={vault_b}"],
        cwd=project,
        env=env,
    )
    assert configured.returncode == 0, (configured.stdout, configured.stderr)
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["pluginConfigs"][CLAUDE_PLUGIN_ID]["options"]["data_dir"] == str(vault_b)
    log_path.write_text("", encoding="utf-8")
    _claude(["-p", "ok"], cwd=project, env=env)
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    hook_rows = [row for row in records if "alice-memory-session-start" in row["argv"]]
    assert hook_rows
    assert str(vault_b) in hook_rows[0]["argv"]

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


