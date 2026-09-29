"""alice-memory install --host codex edits config.toml as text.

Codex 0.158.0 is MCP only in this change and opt-in. The producer bytes
below were captured from ``codex mcp remove`` with ``@openai/codex@0.158.0``
(``codex-cli 0.158.0``), which rewrites ``[mcp_servers]`` through toml_edit
0.24. The import-shaped bytes follow Codex's Claude import
(``toml`` 0.9 ``to_string_pretty``: sorted keys, trailing commas, literal
strings, a one-line triple-quoted value) and were checked with
``codex mcp get alice --json`` on that same binary. Real-host tests run
only when ALICE_TEST_REAL_HOSTS=1.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import scripts.fuzz_codex_config_writer as fuzz
from alicebot_api import host_install
from alicebot_api.host_install import CodexConfigRefused, host_file_map, plan_codex_config
from alicebot_api.onramp import _ERROR_CONTRACTS, main as onramp_main
from tests.unit.launcher_helpers import executable, make_scripts, pin_launcher_search

pytestmark = pytest.mark.usefixtures("uvx_on_path")
REAL_HOSTS_ENV = "ALICE_TEST_REAL_HOSTS"
INSTALL_REFUSED = {
    "error": {
        "code": "install_refused",
        "message": _ERROR_CONTRACTS["install_refused"],
    }
}

# Exact bytes from `codex mcp remove other` on @openai/codex@0.158.0.
MCP_REMOVE_PRODUCER = (
    "# header comment\n"
    "check_for_update_on_startup = false\n"
    "\n"
    "[mcp_servers.alice]\n"
    'command = "uvx"\n'
    "args = [\"alice-memory\", \"mcp\", \"--data-dir\", 'C:\\Users\\Alex\\Alice Vault']\n"
    "enabled = false\n"
    "startup_timeout_sec = 60.0\n"
    'default_tools_approval_mode = "approve"\n'
    "\n"
    "[mcp_servers.alice.env]\n"
    "ALICE_AGENT_API_KEY = 'a\"b'\n"
    'ALICE_MCP_FULL_TOOLS = "1"\n'
    "\n"
    "[mcp_servers.alice.tools.alice_recall]\n"
    'approval_mode = "approve"\n'
    "\n"
    '[projects."/tmp/proj"]\n'
    'trust_level = "trusted"\n'
)
IMPORT_PRODUCER = (
    "[mcp_servers.alice]\n"
    "args = [\n"
    '    "alice-memory",\n'
    '    "mcp",\n'
    '    "--data-dir",\n'
    "    'C:\\Users\\Alex\\Alice Vault',\n"
    "]\n"
    'command = "uvx"\n'
    "env_vars = [\n"
    '    "HTTPS_PROXY",\n'
    '    "UV_INDEX_PRIVATE_USERNAME",\n'
    "]\n"
    "\n"
    "[mcp_servers.alice.env]\n"
    'ALICE_AGENT_API_KEY = """say "hi" and \'bye\'"""\n'
)


def _install(home: Path, vault: Path, capsys, *extra: str) -> tuple[int, str, str]:
    code = onramp_main(
        ["install", "--home", str(home), "--data-dir", str(vault), "--host", "codex", *extra]
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _config(home: Path) -> Path:
    return host_file_map(home.resolve())["codex"]["mcp"]


def _seed(home: Path, content: str | bytes) -> Path:
    path = _config(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_bytes(content.encode("utf-8"))
    return path


def _backups(vault: Path) -> list[Path]:
    directory = vault / "backups" / "host-configs"
    if not directory.is_dir():
        return []
    return sorted(directory.glob("codex-config.toml.alice-backup-*"))


def _error_records(err: str) -> list[dict[str, object]]:
    records = []
    for line in err.splitlines():
        if line.startswith("{"):
            records.append(json.loads(line))
    return records


def test_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--home, and CODEX_HOME unset, empty, relative, missing, and a file, on three platforms."""

    home = tmp_path / "home"
    for platform in ("linux", "darwin", "win32"):
        monkeypatch.delenv("CODEX_HOME", raising=False)
        directory, problem = host_install._codex_directory(home, explicit_home=True, platform=platform)
        assert problem is None
        assert directory == home / ".codex"
        directory, problem = host_install._codex_directory(home, explicit_home=False, platform=platform)
        assert problem is None
        assert directory == home / ".codex"
    monkeypatch.setenv("CODEX_HOME", "")
    directory, problem = host_install._codex_directory(home, explicit_home=False, platform="linux")
    assert problem is None and directory == home / ".codex"
    monkeypatch.setenv("CODEX_HOME", "relative/codex")
    _directory, problem = host_install._codex_directory(home, explicit_home=False, platform="linux")
    assert problem == "CODEX_HOME relative/codex is not an absolute path"
    missing = tmp_path / "missing-codex"
    monkeypatch.setenv("CODEX_HOME", str(missing))
    _directory, problem = host_install._codex_directory(home, explicit_home=False, platform="linux")
    assert problem is not None and "does not exist" in problem and str(missing) in problem
    file_path = tmp_path / "not-a-dir"
    file_path.write_text("x", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(file_path))
    _directory, problem = host_install._codex_directory(home, explicit_home=False, platform="linux")
    assert problem is not None and "is not a directory" in problem


def test_codex_is_opt_in(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    code = onramp_main(["install", "--home", str(home), "--data-dir", str(vault)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "host: codex" not in captured.out
    assert not (home / ".codex").exists()


def test_codex_new_file_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A missing file is only Alice's table. Empty and comment-only files are appended."""

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    path = _config(home)
    text = path.read_text(encoding="utf-8")
    loaded = tomllib.loads(text)
    assert loaded["mcp_servers"]["alice"]["command"] == "uvx"
    assert loaded["mcp_servers"]["alice"]["args"][-1] == str(vault.resolve())
    assert text.startswith("[mcp_servers.alice]\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert "session_start: none" in out
    assert "format: toml, edited as text" in out

    empty_home = tmp_path / "empty-home"
    empty = _seed(empty_home, "")
    code, _out, err = _install(empty_home, tmp_path / "vault-empty", capsys)
    assert code == 0, err
    assert empty.read_text(encoding="utf-8").startswith("\n\n[mcp_servers.alice]\n")

    comment_home = tmp_path / "comment-home"
    comment = _seed(comment_home, "# keep me\n")
    code, _out, err = _install(comment_home, tmp_path / "vault-comment", capsys)
    assert code == 0, err
    written = comment.read_text(encoding="utf-8")
    assert written.startswith("# keep me\n\n[mcp_servers.alice]\n")


def test_codex_rerun_is_unchanged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    _install(home, vault, capsys)
    path = _config(home)
    before = path.read_bytes()
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert "action: unchanged" in out
    assert path.read_bytes() == before
    assert _backups(vault) == []


def test_codex_reruns_over_the_producer_shapes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both producer shapes re-run to unchanged or an update, never a refusal."""

    for label, original in (("mcp-remove", MCP_REMOVE_PRODUCER), ("import", IMPORT_PRODUCER)):
        home = tmp_path / label
        vault = tmp_path / f"vault-{label}"
        path = _seed(home, original)
        code, out, err = _install(home, vault, capsys)
        assert code == 0, (label, out, err)
        assert "action: refused" not in out
        assert "action: written" in out or "action: unchanged" in out
        code, out, err = _install(home, vault, capsys)
        assert code == 0, (label, out, err)
        assert "action: unchanged" in out
        assert "mcp_servers.alice.tools.alice_recall" not in original or (
            'approval_mode = "approve"' in path.read_text(encoding="utf-8")
        )


def test_codex_keeps_tools_tables(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    original = (
        "[mcp_servers.alice.tools.before]\n"
        'approval_mode = "prompt"\n'
        "\n"
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        'tools.alice_recall = { approval_mode = "approve" }\n'
        "\n"
        "[mcp_servers.alice.tools.after]\n"
        'approval_mode = "approve"\n'
    )
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _seed(home, original)
    code, _out, err = _install(home, vault, capsys)
    assert code == 0, err
    written = path.read_text(encoding="utf-8")
    assert '[mcp_servers.alice.tools.before]\napproval_mode = "prompt"\n' in written
    assert '[mcp_servers.alice.tools.after]\napproval_mode = "approve"\n' in written
    assert 'tools.alice_recall = { approval_mode = "approve" }' in written
    loaded = tomllib.loads(written)
    assert loaded["mcp_servers"]["alice"]["tools"]["before"]["approval_mode"] == "prompt"
    assert loaded["mcp_servers"]["alice"]["tools"]["alice_recall"]["approval_mode"] == "approve"


def test_codex_keeps_a_comment_before_the_next_header(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "\n"
        "# c7f3a1 before the next header\n"
        "[other]\n"
        "k = 1\n"
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    code, _out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, err
    written = path.read_text(encoding="utf-8")
    assert "# c7f3a1 before the next header\n" in written
    assert written.index("# c7f3a1") < written.index("[other]")


def test_codex_carries_documented_env_byte_for_byte(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "sk-" + 'Ab\\"c'
    literal = "ALICE_MCP_FULL_TOOLS = 'yes'"
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        f'env = {{ ALICE_AGENT_API_KEY = "{secret}", {literal[0:0]}ALICE_MCP_LEGACY_TOOLS = "1" }}\n'
        "\n"
        "[mcp_servers.other]\n"
        'command = "true"\n'
    )
    # The inline form and the sub-table form are separate files.
    inline = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        f'env = {{ ALICE_AGENT_API_KEY = "{secret}", ALICE_MCP_FULL_TOOLS = \'yes\' }}\n'
    )
    table = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "\n"
        "[mcp_servers.alice.env]\n"
        f'ALICE_AGENT_API_KEY = "{secret}"\n'
        "ALICE_MCP_FULL_TOOLS = 'yes'\n"
        'ALICE_MEMORY_DATA_DIR = "/hand/vault"\n'
    )
    for label, source, needle in (
        ("inline", inline, f'ALICE_AGENT_API_KEY = "{secret}"'),
        ("table", table, 'ALICE_MEMORY_DATA_DIR = "/hand/vault"'),
    ):
        home = tmp_path / label
        path = _seed(home, source)
        code, out, err = _install(home, tmp_path / f"vault-{label}", capsys)
        assert code == 0, (label, out, err)
        written = path.read_text(encoding="utf-8")
        assert needle in written
        assert "ALICE_MCP_FULL_TOOLS = 'yes'" in written
        if label == "table":
            assert "reads --data-dir" in out


def test_codex_carries_scalar_keys(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    original = (
        "[mcp_servers.alice]\n"
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        'command = "uvx"\n'
        "startup_timeout_sec = 60.0\n"
        "tool_timeout_sec = 10\n"
        "startup_timeout_ms = 60000\n"
        "enabled = false\n"
        'default_tools_approval_mode = "writes"\n'
        'env_vars = ["HTTPS_PROXY", "UV_INDEX_PRIVATE_USERNAME"]\n'
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, err
    written = path.read_text(encoding="utf-8")
    for line in (
        "startup_timeout_sec = 60.0",
        "tool_timeout_sec = 10",
        "startup_timeout_ms = 60000",
        "enabled = false",
        'default_tools_approval_mode = "writes"',
        'env_vars = ["HTTPS_PROXY", "UV_INDEX_PRIVATE_USERNAME"]',
    ):
        assert line in written
    assert "Codex will not start alice" in out
    code, out, _err = _install(home, tmp_path / "vault", capsys)
    assert code == 0
    assert "action: unchanged" in out


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ("startup_timeout_sec = -1", "startup_timeout_sec"),
        ("startup_timeout_sec = nan", "startup_timeout_sec"),
        ("startup_timeout_sec = inf", "startup_timeout_sec"),
        ('startup_timeout_sec = "60"', "startup_timeout_sec"),
        ("startup_timeout_sec = 1e1_000", "startup_timeout_sec"),
        ("startup_timeout_ms = 60000.0", "startup_timeout_ms"),
        ("startup_timeout_ms = -1", "startup_timeout_ms"),
        ("startup_timeout_ms = 9223372036854775808", "startup_timeout_ms"),
        ("enabled = 0", "enabled"),
        ('enabled = "false"', "enabled"),
        ('default_tools_approval_mode = "nope"', "default_tools_approval_mode"),
        ("env_vars = { name = \"A\" }", "env_vars"),
    ],
)
def test_codex_type_checks_carried_keys(
    line: str, key: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        f"{line}\n"
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1
    assert path.read_bytes() == before
    assert _backups(tmp_path / "vault") == []
    assert key in out
    assert "Codex would refuse to load this value" in out
    assert _error_records(err) == [INSTALL_REFUSED]


@pytest.mark.parametrize(
    ("label", "original", "needle"),
    [
        (
            "inline alice",
            "[mcp_servers]\nalice = { command = \"uvx\", args = [\"a\"] }\n",
            "inline table",
        ),
        (
            "dotted root",
            'mcp_servers.alice.command = "uvx"\n',
            "dotted keys",
        ),
        (
            "dotted inside",
            "[mcp_servers]\nalice.command = \"uvx\"\n",
            "dotted keys",
        ),
        (
            "other table",
            "[mcp_servers.alice]\ncommand = \"uvx\"\nargs = [\"a\"]\n\n[mcp_servers.alice.other]\nk = 1\n",
            "holds other",
        ),
        (
            "cwd",
            "[mcp_servers.alice]\ncommand = \"uvx\"\nargs = [\"a\"]\ncwd = \"/tmp\"\n",
            "holds cwd",
        ),
        (
            "url",
            "[mcp_servers.alice]\ncommand = \"uvx\"\nargs = [\"a\"]\nurl = \"https://example.test\"\n",
            "holds url",
        ),
        (
            "enabled_tools",
            "[mcp_servers.alice]\ncommand = \"uvx\"\nargs = [\"a\"]\nenabled_tools = [\"a\"]\n",
            "holds enabled_tools",
        ),
        (
            "undocumented env",
            "[mcp_servers.alice]\ncommand = \"uvx\"\nargs = [\"a\"]\n\n[mcp_servers.alice.env]\nNOPE = \"1\"\n",
            "holds env.NOPE",
        ),
        (
            "comment between",
            "[mcp_servers.alice]\ncommand = \"uvx\"\n# inside\nargs = [\"a\"]\n",
            "comment between",
        ),
        (
            "trailing comment",
            "[mcp_servers.alice]\ncommand = \"uvx\" # note\nargs = [\"a\"]\n",
            "trailing comment",
        ),
        ("bom", "\ufeffmodel = 1\n", "BOM"),
        ("mixed", "a = 1\r\nb = 2\n", "mixed line endings"),
        ("cr", "a = 1\rb = 2\n", "lone CR"),
        ("newline inline", "t = { a = 1,\n b = 2 }\n", "newline inside an inline table"),
        ("trailing comma", "t = { a = 1, }\n", "trailing comma inside an inline table"),
        ("escape e", 't = "\\e"\n', "\\e escape"),
        ("escape x", 't = "\\x41"\n', "\\x escape"),
        ("time", "t = 07:32\n", "without seconds"),
        ("datetime", "t = 1979-05-27T07:32\n", "without seconds"),
        ("inline mcp", 'mcp_servers = { alice = { command = "uvx" } }\n', "inline table"),
    ],
)
def test_codex_refusals(
    label: str, original: str, needle: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bytes stay, there is no backup, the next line is present, and the file text is not printed."""

    home = tmp_path / "home"
    path = _seed(home, original)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (label, out, err)
    assert path.read_bytes() == before
    assert _backups(tmp_path / "vault") == []
    assert needle in out, (label, out)
    assert "next:" in out
    assert original.strip() not in out or needle in ("BOM",)
    assert _error_records(err) == [INSTALL_REFUSED]


def test_codex_toml_11_refused_before_tomllib(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lexer refuses TOML 1.1 forms without asking tomllib."""

    def boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("tomllib.loads was called")

    monkeypatch.setattr(host_install.tomllib, "loads", boom)
    for original in ('t = "\\e"\n', "t = { a = 1, }\n", "t = 07:32\n", "t = { a = 1,\n b = 2 }\n"):
        with pytest.raises(CodexConfigRefused):
            plan_codex_config(original, "/new")


def test_codex_accepts_a_hash_inside_a_string(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "vault#dir"
    original = (
        '[note = "keep # hash"]\n' if False else 'note = "keep # hash"\n'
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old#vault"]\n'
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    code, _out, err = _install(home, vault, capsys)
    assert code == 0, err
    written = path.read_text(encoding="utf-8")
    assert 'note = "keep # hash"' in written
    assert "vault#dir" in written or str(vault.resolve()) in written
    assert tomllib.loads(written)["mcp_servers"]["alice"]["args"][-1] == str(vault.resolve())


@pytest.mark.parametrize(
    "original",
    [
        'model = "x"',
        'model = "x"\r\n',
        "[[array]]\nname = \"x\"\n",
        '[projects."/abs/path"]\ntrust_level = "trusted"\n',
        's = """\n[mcp_servers.alice]\n"""\nmodel = "x"\n',
        "items = [\n[\"element\"],\n]\n",
    ],
)
def test_codex_insert_edges(original: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    path = _seed(home, original)
    code, _out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, err
    written = path.read_text(encoding="utf-8")
    loaded = tomllib.loads(written)
    assert "alice" in loaded["mcp_servers"]
    if "\r" in original:
        assert b"\r\n" in path.read_bytes()
    if "[[array]]" in original:
        assert "[[array]]" in written
    if 'projects."/abs/path"' in original:
        assert written.index('[projects."/abs/path"]') < written.index("[mcp_servers.alice]")


def test_codex_other_launcher_note(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "\n"
        "[mcp_servers.second]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/other"]\n'
    )
    home = tmp_path / "home"
    _seed(home, original)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, err
    assert "Codex will run both" in out


def test_codex_other_layer_note(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "work.config.toml").write_text(
        "[mcp_servers.alice]\ncommand = \"uvx\"\nargs = [\"a\"]\n",
        encoding="utf-8",
    )
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, err
    assert "work.config.toml defines mcp_servers.alice" in out


def test_codex_json_mode_hook_note(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    hooks = home / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(
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
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, err
    assert "Codex rejects its output. Remove it from hooks.json" in out


def test_codex_changed_while_running_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    path = _seed(home, 'model = "x"\n')
    seed = path.read_bytes()
    real = host_install._plan_codex_text

    def wrapped(*args: object, **kwargs: object) -> object:
        result = real(*args, **kwargs)
        path.write_bytes(seed + b"# changed\n")
        return result

    monkeypatch.setattr(host_install, "_plan_codex_text", wrapped)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (out, err)
    assert path.read_bytes() == seed + b"# changed\n"
    assert "config.toml changed while install ran; run install again." in out
    assert _backups(tmp_path / "vault") == []


def test_codex_every_rewrite_backs_up(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    other = tmp_path / "other"
    path = _seed(home, 'model = "x"\n')
    seed = path.read_bytes()
    code, _out, err = _install(home, vault, capsys)
    assert code == 0, err
    backups = _backups(vault)
    assert len(backups) == 1
    assert backups[0].read_bytes() == seed
    code, _out, err = _install(home, other, capsys)
    assert code == 0, err
    assert len(_backups(other)) == 1


def test_codex_symlink_written_through(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    target = tmp_path / "real-config.toml"
    target.write_text('model = "x"\n', encoding="utf-8")
    path = _config(home)
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    code, _out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, err
    assert path.is_symlink()
    assert "alice" in target.read_text(encoding="utf-8")


def test_codex_dry_run_masks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secret = "sk-" + "Ab" + "secret"
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "\n"
        "[mcp_servers.alice.env]\n"
        f'ALICE_AGENT_API_KEY = "{secret}"\n'
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys, "--dry-run")
    assert code == 0, err
    assert path.read_bytes() == before
    assert secret not in out and secret not in err
    assert "<hidden>" in out
    assert "format: toml, edited as text" in out


def test_codex_receipts_never_say_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    code, out, err = _install(home, tmp_path / "vault", capsys, "--dry-run")
    assert code == 0, err
    assert "format: toml, edited as text" in out
    assert "format: json" not in out
    assert "session_start: none" in out
    snippet = out.split("snippet:", 1)[1]
    assert not snippet.lstrip().startswith("{")


def test_codex_uv_cache_launcher_replaced_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripts = make_scripts(tmp_path / "scripts", session_start=False)
    pin_launcher_search(monkeypatch, tmp_path, uvx=str(executable(tmp_path / "bin" / "uvx")))
    cached = executable(tmp_path / "cache" / "uv" / "archive-v0" / "A1b2" / "bin" / "uvx")
    index = "https://" + "example.test/simple"
    original = (
        "[mcp_servers.alice]\n"
        f'command = "{cached}"\n'
        f'args = ["--with", "x", "--index-url", "{index}", "alice-memory", "mcp", "--data-dir", "/old"]\n'
    )
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _seed(home, original)
    code, _out, err = _install(home, vault, capsys)
    assert code == 0, err
    loaded = tomllib.loads(path.read_text(encoding="utf-8"))
    args = loaded["mcp_servers"]["alice"]["args"]
    assert "--with" not in args
    assert index not in args
    assert args[-1] == str(vault.resolve())
    assert loaded["mcp_servers"]["alice"]["command"] == "uvx"
    assert scripts.is_dir()


def test_codex_pinned_launcher_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pin_launcher_search(monkeypatch, tmp_path, uvx=str(executable(tmp_path / "bin" / "uvx")))
    uvx = executable(tmp_path / "tools" / "uvx")
    vault = tmp_path / "vault"
    original = (
        "[mcp_servers.alice]\n"
        f'command = "{uvx}"\n'
        f'args = ["--python", "3.12", "alice-memory", "mcp", "--data-dir", "{vault.resolve()}"]\n'
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    before = path.read_bytes()
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    written = path.read_text(encoding="utf-8")
    assert "--python" in written and "3.12" in written
    if "action: unchanged" in out:
        assert path.read_bytes() == before


def test_codex_foreign_entry_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    original = '[mcp_servers.alice]\ncommand = "node"\nargs = ["server.js"]\n'
    home = tmp_path / "home"
    path = _seed(home, original)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1
    assert path.read_bytes() == before
    assert "did not write it" in out
    assert _error_records(err) == [INSTALL_REFUSED]


def test_codex_guard_rejects_an_outside_byte_change() -> None:
    """The guard refuses when a byte outside alice changes.

    Mutation: skip the guard. This test fails.
    """

    original = 'model = "x"\n'
    edited = (
        'model = "y"\n\n'
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/v"]\n'
    )
    with pytest.raises(CodexConfigRefused, match="outside alice"):
        host_install._codex_guard(
            original,
            edited,
            "\n",
            {"command": "uvx", "args": ["alice-memory", "mcp", "--data-dir", "/v"]},
            (),
            (len(original), len(edited)),
            {},
        )


def test_seeded_fuzz_run_finds_no_meaning_change() -> None:
    """A short seeded pass. A lexer that treats ''' inside a basic string as an opener fails it.

    A writer that refuses everything fails it too: every label has to be written.
    """

    counts = fuzz.run(range(3), configs_per_seed=7, mutants_per_config=2)
    assert counts.generated_written == 21
    for label in fuzz.LABELS:
        assert counts.by_label.get(label, 0) >= 3, counts.by_label
    assert counts.by_mutation
    assert sum(item.get("refused", 0) + item.get("written", 0) for item in counts.by_mutation.values()) > 0


def _codex_bin() -> str | None:
    return shutil.which("codex")


def _run_codex(home: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CODEX_HOME"] = str(home / ".codex")
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        ["codex", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(os.environ.get(REAL_HOSTS_ENV) != "1", reason="set ALICE_TEST_REAL_HOSTS=1")
@pytest.mark.skipif(sys.platform != "linux", reason="the Codex real-host check runs on Linux")
@pytest.mark.skipif(_codex_bin() is None, reason="codex is not on PATH")
def test_real_codex_reads_the_written_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """codex mcp get and mcp list read the file install wrote. The PATH-alias warning is ignored."""

    scripts = make_scripts(tmp_path / "scripts", session_start=False)
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=scripts)
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (home / ".codex").mkdir(parents=True)
    vault = tmp_path / "vault"
    moved = tmp_path / "vault-2"
    project = tmp_path / "project"
    seed = (
        "# kept comment\n"
        "check_for_update_on_startup = false\n"
        "\n"
        "[mcp_servers.other]\n"
        'command = "true"\n'
        "enabled = false\n"
        "\n"
        "# trusted projects\n"
        f'[projects."{project}"]\n'
        'trust_level = "trusted"\n'
        "\n"
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "\n"
        "[mcp_servers.alice.env]\n"
        'ALICE_MEMORY_DATA_DIR = "/hand"\n'
    )
    path = _seed(home, seed)
    original = path.read_bytes()

    def codex(*args: str) -> subprocess.CompletedProcess[str]:
        return _run_codex(home, cwd, *args)

    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    backups = _backups(vault)
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    got = codex("mcp", "get", "alice", "--json")
    assert got.returncode == 0, got.stderr
    payload = json.loads(got.stdout)
    assert payload["transport"]["type"] == "stdio"
    written = tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"]["alice"]
    assert payload["transport"]["command"] == written["command"]
    assert payload["transport"]["args"] == written["args"]
    listed = codex("mcp", "list", "--json")
    assert listed.returncode == 0, listed.stderr
    names = {item["name"] for item in json.loads(listed.stdout)}
    assert {"alice", "other"} <= names

    current = path.read_text(encoding="utf-8")
    path.write_text(
        current.replace(
            "[mcp_servers.alice]\n",
            "[mcp_servers.alice]\nstartup_timeout_sec = 60\n",
            1,
        )
        + '\n[mcp_servers.alice.tools.alice_recall]\napproval_mode = "approve"\n',
        encoding="utf-8",
    )
    code, _out, err = _install(home, moved, capsys)
    assert code == 0, err
    rewritten = path.read_text(encoding="utf-8")
    assert "startup_timeout_sec = 60" in rewritten
    assert 'approval_mode = "approve"' in rewritten
    assert "# kept comment" in rewritten
    assert len(_backups(moved)) == 1
    got = codex("mcp", "get", "alice", "--json")
    assert got.returncode == 0, got.stderr
    payload = json.loads(got.stdout)
    assert payload["startup_timeout_sec"] == 60.0
    assert payload["transport"]["args"][-1] == str(moved.resolve())

    removed = codex("mcp", "remove", "other")
    assert removed.returncode == 0, removed.stderr
    code, out, err = _install(home, moved, capsys)
    assert code == 0, err
    assert "action: refused" not in out
    got = codex("mcp", "get", "alice", "--json")
    assert got.returncode == 0, got.stderr
    assert json.loads(got.stdout)["transport"]["args"][-1] == str(moved.resolve())

    code, out, err = _install(home, moved, capsys, "--dry-run")
    assert code == 0, err
    assert "<hidden>" in out or "action: dry-run" in out

    for case in fuzz.codex_bases(50, seed=4):
        path.write_text(case, encoding="utf-8")
        got = codex("mcp", "get", "alice", "--json")
        assert got.returncode == 0, (got.stderr, case)
        payload = json.loads(got.stdout)
        expected = tomllib.loads(case)["mcp_servers"]["alice"]
        assert payload["transport"]["command"] == expected["command"]
        assert payload["transport"]["args"] == expected["args"]


@pytest.mark.skipif(os.environ.get(REAL_HOSTS_ENV) != "1", reason="set ALICE_TEST_REAL_HOSTS=1")
@pytest.mark.skipif(sys.platform != "linux", reason="the Codex real-host check runs on Linux")
@pytest.mark.skipif(_codex_bin() is None, reason="codex is not on PATH")
def test_real_codex_rejects_a_broken_alice_entry(tmp_path: Path) -> None:
    """A string args value fails config load. Any other outcome fails this test."""

    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    path = _seed(
        home,
        '[mcp_servers.alice]\ncommand = "uvx"\nargs = ["alice-memory", "mcp", "--data-dir", "/old"]\n',
    )
    got = _run_codex(home, cwd, "mcp", "get", "alice", "--json")
    assert got.returncode == 0, got.stderr
    path.write_text('[mcp_servers.alice]\ncommand = "uvx"\nargs = "x"\n', encoding="utf-8")
    broken = _run_codex(home, cwd, "mcp", "list", "--json")
    assert broken.returncode == 1, broken.stderr
    assert "failed to load bootstrap configuration" in broken.stderr
