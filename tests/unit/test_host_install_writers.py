"""alice-memory install: host MCP writers. No live-home write.

Each test names the edit that makes it fail. --home / HOME / --data-dir
are tmp_path. Do not call the live openclaw binary.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import yaml

from alicebot_api.host_install import (
    ALICE_MEMORY_DATA_DIR_ENV,
    DEFAULT_INSTALL_HOSTS,
    SESSION_START_COMMAND,
    build_mcpb_manifest,
    committed_mcpb_manifest_path,
    host_file_map,
    openclaw_add_line,
)
from alicebot_api.onramp import (
    _ERROR_CONTRACTS,
    _KNOWN_COMMANDS,
    _normalized_argv,
    build_parser,
    main as onramp_main,
)

USER_ID = "00000000-0000-0000-0000-000000000001"
REPO_ROOT = Path(__file__).resolve().parents[2]
README_PATH = REPO_ROOT / "README.md"
INSTALL_FAILED = {
    "error": {
        "code": "install_failed",
        "message": "The host install could not complete",
    }
}
INSTALL_REFUSED = {
    "error": {
        "code": "install_refused",
        "message": _ERROR_CONTRACTS["install_refused"],
    }
}
PASTE_MARKERS = ("mcpServers", "mcp_servers", "mcp.servers")


def _install(argv: list[str], capsys) -> tuple[int, str, str]:
    code = onramp_main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _base_argv(home: Path, data_dir: Path, *extra: str) -> list[str]:
    return [
        "install",
        "--home",
        str(home),
        "--data-dir",
        str(data_dir),
        *extra,
    ]


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _error_records(stderr: str) -> list[object]:
    return [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]


def _alice_payload(doc: dict, host: str) -> dict:
    if host == "openclaw":
        return doc["mcp"]["servers"]["alice"]
    if host == "hermes":
        return doc["mcp_servers"]["alice"]
    return doc["mcpServers"]["alice"]


def _flag_value(parts: list[str], flag: str) -> str:
    return parts[parts.index(flag) + 1]


def _expected_session_start_hook(data_dir: Path) -> dict[str, str]:
    resolved = str(data_dir.resolve())
    return {
        "command": (
            "uvx --from alice-memory alice-memory-session-start "
            f"--data-dir {resolved}"
        )
    }


def _expected_claude_code_session_start_group(data_dir: Path) -> dict[str, object]:
    """Claude Code nests handlers under ``hooks`` and needs ``type``.

    Until 2026-09-22 this helper pinned Cursor's flat shape for Claude Code
    too, which Claude Code ignores. See test_claude_code_session_start_hook.
    """

    return {"hooks": [{"type": "command", **_expected_session_start_hook(data_dir)}]}


def _assert_alice_payload(payload: dict, data_dir: Path, *, with_env: bool) -> None:
    assert payload["command"] == "uvx"
    assert payload["args"] == [
        "alice-memory",
        "mcp",
        "--data-dir",
        str(data_dir.resolve()),
    ]
    if with_env:
        assert payload["env"][ALICE_MEMORY_DATA_DIR_ENV] == str(data_dir.resolve())
    else:
        assert "env" not in payload


def _snapshot(path: Path) -> tuple[bool, int | None, str | None]:
    if not path.exists():
        return (False, None, None)
    digest = None
    if path.is_file():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return (True, path.stat().st_mtime_ns, digest)


def test_install_missing_from_known_commands_becomes_mcp() -> None:
    """If install is missing from _KNOWN_COMMANDS, argv handling fails like doctor.

    Mutation: drop ``install`` from ``_KNOWN_COMMANDS``. This test fails.
    """

    assert "install" in _KNOWN_COMMANDS
    assert _normalized_argv(["install", "--home", "/tmp/x"]) == [
        "install",
        "--home",
        "/tmp/x",
    ]
    assert _normalized_argv(["install", "--home", "/tmp/x"])[0] != "mcp"


def test_host_cursor_writes_mcp_and_session_start_hook(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """--home tmp --host cursor writes mcp.json and a SessionStart hook.

    Mutation: skip the hook write, or write the bare
    ``alice-memory-session-start``. This test fails.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    code, out, err = _install(
        _base_argv(home, vault, "--host", "cursor"),
        capsys,
    )
    assert code == 0, err
    files = host_file_map(home.resolve())
    mcp = _load_json(files["cursor"]["mcp"])
    hooks = _load_json(files["cursor"]["hooks"])
    _assert_alice_payload(mcp["mcpServers"]["alice"], vault, with_env=False)
    assert hooks["hooks"]["sessionStart"] == [_expected_session_start_hook(vault)]
    assert "session_start: added" in out
    assert not vault.exists()


def test_host_claude_code_writes_mcp_and_session_start(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """--host claude-code writes MCP plus hooks.SessionStart.

    Mutation: write MCP only, write the bare ``alice-memory-session-start``,
    or write Cursor's flat entry. This test fails.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    code, out, err = _install(
        _base_argv(home, vault, "--host", "claude-code"),
        capsys,
    )
    assert code == 0, err
    files = host_file_map(home.resolve())
    mcp = _load_json(files["claude-code"]["mcp"])
    hooks = _load_json(files["claude-code"]["hooks"])
    _assert_alice_payload(mcp["mcpServers"]["alice"], vault, with_env=False)
    assert hooks["hooks"]["SessionStart"] == [
        _expected_claude_code_session_start_group(vault)
    ]
    assert "session_start: added" in out


def test_session_start_hook_data_dir_matches_mcp(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Hook --data-dir is the same resolved vault MCP uses.

    Mutation: omit --data-dir on the hook while MCP has a custom vault.
    This test fails.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "home"
    vault = tmp_path / "custom-vault"
    code, _out, err = _install(
        _base_argv(home, vault, "--host", "cursor"),
        capsys,
    )
    assert code == 0, err
    files = host_file_map(home.resolve())
    mcp = _load_json(files["cursor"]["mcp"])
    hooks = _load_json(files["cursor"]["hooks"])
    hook_command = hooks["hooks"]["sessionStart"][0]["command"]
    hook_dir = _flag_value(hook_command.split(), "--data-dir")
    mcp_dir = _flag_value(mcp["mcpServers"]["alice"]["args"], "--data-dir")
    assert hook_dir == mcp_dir
    assert hook_dir == str(vault.resolve())


def test_second_install_replaces_bare_session_start_hook(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A second install replaces a bare SessionStart hook. One hook remains.

    Mutation: leave the bare command in place. This test fails.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    files = host_file_map(home.resolve())
    hooks_path = files["cursor"]["hooks"]
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {"sessionStart": [{"command": SESSION_START_COMMAND}]},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    argv = _base_argv(home, vault, "--host", "cursor")
    assert _install(argv, capsys)[0] == 0
    hooks = _load_json(hooks_path)
    assert hooks["hooks"]["sessionStart"] == [_expected_session_start_hook(vault)]
    assert _install(argv, capsys)[0] == 0
    hooks = _load_json(hooks_path)
    assert hooks["hooks"]["sessionStart"] == [_expected_session_start_hook(vault)]


def test_install_replaces_session_start_hook_missing_or_wrong_data_dir(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A hook that omits --data-dir or points at another vault is replaced.

    Mutation: leave the stale hook in place. This test fails.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    files = host_file_map(home.resolve())
    hooks_path = files["cursor"]["hooks"]
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "sessionStart": [
                        {
                            "command": (
                                "uvx --from alice-memory "
                                "alice-memory-session-start"
                            )
                        },
                        {
                            "command": (
                                "uvx --from alice-memory "
                                "alice-memory-session-start "
                                "--data-dir /other/vault"
                            )
                        },
                    ]
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    code, _out, err = _install(_base_argv(home, vault, "--host", "cursor"), capsys)
    assert code == 0, err
    hooks = _load_json(hooks_path)
    assert hooks["hooks"]["sessionStart"] == [_expected_session_start_hook(vault)]


def test_host_claude_desktop_writes_mcp_servers_alice(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """--host claude-desktop writes mcpServers.alice.

    Mutation: write a different key. This test fails.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    code, _out, err = _install(
        _base_argv(home, vault, "--host", "claude-desktop"),
        capsys,
    )
    assert code == 0, err
    files = host_file_map(home.resolve())
    mcp = _load_json(files["claude-desktop"]["mcp"])
    _assert_alice_payload(mcp["mcpServers"]["alice"], vault, with_env=False)
    assert list(mcp["mcpServers"]) == ["alice"]


def test_host_openclaw_writes_nested_server_and_prints_add_line(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """--host openclaw writes mcp.servers.alice and prints the add line.

    Mutation: drop the print. This test fails.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    code, out, err = _install(
        _base_argv(home, vault, "--host", "openclaw"),
        capsys,
    )
    assert code == 0, err
    files = host_file_map(home.resolve())
    mcp = _load_json(files["openclaw"]["mcp"])
    _assert_alice_payload(mcp["mcp"]["servers"]["alice"], vault, with_env=False)
    expected = openclaw_add_line(str(vault.resolve()))
    assert expected in out
    assert "alice-memory brief" in out
    assert "session_start: none" in out


def test_host_hermes_writes_yaml_env_map(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """--host hermes writes YAML mcp_servers.alice with an env map.

    Mutation: write command/args only. This test fails.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    code, out, err = _install(
        _base_argv(home, vault, "--host", "hermes"),
        capsys,
    )
    assert code == 0, err
    files = host_file_map(home.resolve())
    # PyYAML is how Hermes reads config.yaml. Until 2026-09-22 these tests read
    # it back with the writer's own hand parser, which hid its corruption.
    loaded = yaml.safe_load(files["hermes"]["mcp"].read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    _assert_alice_payload(loaded["mcp_servers"]["alice"], vault, with_env=True)
    assert ALICE_MEMORY_DATA_DIR_ENV in out or "env" in files["hermes"]["mcp"].read_text(
        encoding="utf-8"
    )
    assert "session_start: none" in out


def test_hermes_flow_style_foreign_server_is_not_stringified(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A flow-style Hermes ``other`` server is not written back as a string.

    Refuse and keep the file, or keep ``other`` as a mapping. Mutation:
    stringify ``other``. This test fails.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    files = host_file_map(home.resolve())
    hermes_mcp = files["hermes"]["mcp"]
    hermes_mcp.parent.mkdir(parents=True)
    flow = "mcp_servers:\n  other: {command: node, args: [other.js]}\n"
    hermes_mcp.write_text(flow, encoding="utf-8")

    code, _out, err = _install(_base_argv(home, vault, "--host", "hermes"), capsys)
    raw = hermes_mcp.read_text(encoding="utf-8")
    if code == 0:
        loaded = yaml.safe_load(raw)
        assert isinstance(loaded, dict)
        other = loaded["mcp_servers"]["other"]
        assert isinstance(other, dict), raw
        assert other.get("command") == "node"
        _assert_alice_payload(loaded["mcp_servers"]["alice"], vault, with_env=True)
    else:
        # A refusal is the other safe outcome: the file keeps its bytes and
        # install exits with install_refused. Until 2026-09-22 this branch
        # expected install_failed, which the writer no longer produces here.
        assert _error_records(err) == [INSTALL_REFUSED]
        assert raw == flow


def test_existing_foreign_server_stays_and_second_run_does_not_duplicate_alice(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A foreign server stays. Second run does not duplicate alice.

    Mutation: replace the whole file or append a second alice. This test fails.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    files = host_file_map(home.resolve())
    cursor_mcp = files["cursor"]["mcp"]
    cursor_mcp.parent.mkdir(parents=True)
    cursor_mcp.write_text(
        json.dumps(
            {
                "theme": "keep-me",
                "mcpServers": {
                    "other": {"command": "node", "args": ["other.js"]},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    openclaw_mcp = files["openclaw"]["mcp"]
    openclaw_mcp.parent.mkdir(parents=True)
    openclaw_mcp.write_text(
        json.dumps(
            {
                "mcp": {
                    "servers": {
                        "other": {"command": "node", "args": ["other.js"]},
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    hermes_mcp = files["hermes"]["mcp"]
    hermes_mcp.parent.mkdir(parents=True)
    hermes_mcp.write_text(
        "theme: keep-me\nmcp_servers:\n  other:\n    command: node\n    args:\n      - other.js\n",
        encoding="utf-8",
    )

    argv = _base_argv(
        home,
        vault,
        "--host",
        "cursor",
        "--host",
        "openclaw",
        "--host",
        "hermes",
    )
    assert _install(argv, capsys)[0] == 0
    assert _install(argv, capsys)[0] == 0

    cursor = _load_json(cursor_mcp)
    assert cursor["theme"] == "keep-me"
    assert cursor["mcpServers"]["other"] == {"command": "node", "args": ["other.js"]}
    assert list(cursor["mcpServers"]).count("alice") == 1
    _assert_alice_payload(cursor["mcpServers"]["alice"], vault, with_env=False)

    openclaw = _load_json(openclaw_mcp)
    assert openclaw["mcp"]["servers"]["other"] == {"command": "node", "args": ["other.js"]}
    assert list(openclaw["mcp"]["servers"]).count("alice") == 1

    hermes = yaml.safe_load(hermes_mcp.read_text(encoding="utf-8"))
    assert isinstance(hermes, dict)
    assert hermes["theme"] == "keep-me"
    assert hermes["mcp_servers"]["other"]["command"] == "node"
    assert list(hermes["mcp_servers"]).count("alice") == 1
    _assert_alice_payload(hermes["mcp_servers"]["alice"], vault, with_env=True)

    hooks = _load_json(files["cursor"]["hooks"])
    assert hooks["hooks"]["sessionStart"] == [_expected_session_start_hook(vault)]


def test_dry_run_writes_no_files(tmp_path: Path, monkeypatch, capsys) -> None:
    """--dry-run prints the plan and writes nothing.

    Mutation: write files during dry-run. This test fails.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    mcpb = tmp_path / "alice-memory.mcpb"
    before = {path: path.exists() for path in home.rglob("*")} if home.exists() else {}
    code, out, err = _install(
        _base_argv(home, vault, "--dry-run", "--write-mcpb", str(mcpb)),
        capsys,
    )
    assert code == 0, err
    assert "action: dry-run" in out
    assert "snippet:" in out
    assert openclaw_add_line(str(vault.resolve())) in out
    assert not home.exists()
    assert not vault.exists()
    assert not mcpb.exists()
    assert before == {}


def test_written_paths_stay_under_home_and_do_not_touch_real_host_dirs(
    tmp_path: Path, capsys
) -> None:
    """Written paths stay under --home. Real ~/.hermes and ~/.openclaw stay put.

    Mutation: resolve host files against the process home. This test fails.
    HOME is not patched here so Path.home() is the unpatched process home.
    """

    real_home = Path.home()
    watched = (
        real_home / ".hermes",
        real_home / ".hermes" / "config.yaml",
        real_home / ".openclaw",
        real_home / ".openclaw" / "openclaw.json",
    )
    before = {path: _snapshot(path) for path in watched}
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    code, out, err = _install(_base_argv(home, vault, "--host", "hermes", "--host", "openclaw"), capsys)
    assert code == 0, err
    resolved_home = home.resolve()
    files = host_file_map(resolved_home)
    written = (files["hermes"]["mcp"], files["openclaw"]["mcp"])
    for path in written:
        assert path.is_file()
        assert str(path).startswith(str(resolved_home) + "/")
        assert path != real_home / ".hermes" / "config.yaml"
        assert path != real_home / ".openclaw" / "openclaw.json"
    assert str(resolved_home) in out
    assert str(real_home / ".hermes" / "config.yaml") not in out
    assert str(real_home / ".openclaw" / "openclaw.json") not in out
    after = {path: _snapshot(path) for path in watched}
    assert after == before


def test_mcpb_zip_manifest_launches_alice_memory(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A built .mcpb zip contains a manifest that launches alice-memory.

    Mutation: omit uvx or alice-memory from the manifest. This test fails.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    mcpb = tmp_path / "alice-memory.mcpb"
    code, _out, err = _install(
        _base_argv(home, vault, "--host", "cursor", "--write-mcpb", str(mcpb)),
        capsys,
    )
    assert code == 0, err
    assert mcpb.is_file()
    with zipfile.ZipFile(mcpb) as archive:
        assert "manifest.json" in archive.namelist()
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    dumped = json.dumps(manifest)
    assert "uvx" in dumped
    assert "alice-memory" in dumped
    assert manifest["server"]["mcp_config"]["command"] == "uvx"
    assert "alice-memory" in manifest["server"]["mcp_config"]["args"]
    assert manifest["author"]["name"] == "Sami Rusani"
    committed = committed_mcpb_manifest_path()
    assert committed is not None
    committed_doc = json.loads(committed.read_text(encoding="utf-8"))
    generated = build_mcpb_manifest(version=str(committed_doc["version"]))
    assert committed_doc["name"] == generated["name"]
    assert committed_doc["server"]["mcp_config"]["command"] == "uvx"


def test_readme_first_twenty_lines_have_no_mcp_paste() -> None:
    """README first twenty lines have no JSON/YAML MCP paste.

    Mutation: restore the old Quickstart JSON block as the first config
    the reader sees above line 20. This test fails only if that block
    sits in the first twenty lines.
    """

    lines = README_PATH.read_text(encoding="utf-8").splitlines()
    lead = "\n".join(lines[:20])
    for marker in PASTE_MARKERS:
        assert marker not in lead
    quickstart = README_PATH.read_text(encoding="utf-8").split("## Quickstart", 1)[1]
    first_block = quickstart.split("```", 2)[1]
    assert "uvx alice-memory install --data-dir ~/.alice" in first_block


def test_default_hosts_write_session_start_and_openclaw_line(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Omitted --host writes the default four hosts, not Hermes.

    Mutation: include hermes by default or skip SessionStart. This test fails.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    code, out, err = _install(_base_argv(home, vault), capsys)
    assert code == 0, err
    files = host_file_map(home.resolve())
    assert DEFAULT_INSTALL_HOSTS == (
        "claude-desktop",
        "claude-code",
        "cursor",
        "openclaw",
    )
    for host in DEFAULT_INSTALL_HOSTS:
        assert files[host]["mcp"].is_file()
    assert not files["hermes"]["mcp"].exists()
    cursor_hooks = _load_json(files["cursor"]["hooks"])
    claude_hooks = _load_json(files["claude-code"]["hooks"])
    assert cursor_hooks["hooks"]["sessionStart"][0] == _expected_session_start_hook(vault)
    assert claude_hooks["hooks"]["SessionStart"][0] == (
        _expected_claude_code_session_start_group(vault)
    )
    assert openclaw_add_line(str(vault.resolve())) in out
    assert not vault.exists()


def test_install_keeps_db_and_user_id_flags() -> None:
    """--db and --user-id stay available on install."""

    parser = build_parser()
    args = parser.parse_args(
        [
            "install",
            "--data-dir",
            "/tmp/vault",
            "--db",
            "/tmp/vault/memory.db",
            "--user-id",
            USER_ID,
            "--home",
            "/tmp/home",
        ]
    )
    assert args.command == "install"
    assert args.db == "/tmp/vault/memory.db"
    assert str(args.user_id) == USER_ID
    assert args.home == "/tmp/home"


def test_write_failure_emits_install_failed_without_exception_text(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A write failure is non-zero and a static code. No {exc} on stderr.

    Mutation: print the exception or skip the error contract. This test fails.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    blocked = tmp_path / "blocked.txt"
    code, _out, err = _install(
        _base_argv(home, vault, "--host", "cursor", "--write-mcpb", str(blocked)),
        capsys,
    )
    assert code != 0
    assert _error_records(err) == [INSTALL_FAILED]
    assert "{exc}" not in err
    assert "Traceback" not in err
    assert _ERROR_CONTRACTS["install_failed"] == INSTALL_FAILED["error"]["message"]
