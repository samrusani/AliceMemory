"""alice-memory install --host codex edits config.toml as text.

Codex 0.158.0 is MCP only in this change and opt-in. The producer bytes
below were captured from ``codex mcp remove`` with ``@openai/codex@0.158.0``
(``codex-cli 0.158.0``), which rewrites ``[mcp_servers]`` through toml_edit
0.24. The import-shaped bytes are the output of ``toml`` 0.9.11
``to_string_pretty`` (sorted keys, trailing commas, literal strings, a
one-line triple-quoted value). Produced with rustc 1.85.0, which builds
that crate's edition 2024 package: a throwaway crate depending on
``toml = "=0.9.11"``, then ``cargo +1.85.0 run``. The CRLF producer is
``toml_edit`` 0.24.0 ``DocumentMut::to_string`` on a CRLF file whose
multi-line string holds a CRLF; every other break comes out as LF.
Real-host tests run only when ALICE_TEST_REAL_HOSTS=1.
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


@pytest.fixture(autouse=True)
def _codex_output_has_no_unexpected_failure(request, capsys, monkeypatch):
    """A writer crash must not be reported as an ordinary Codex refusal.

    ``test_codex_one_host_crash_does_not_hide_the_other`` raises on purpose.
    """

    seen: list[str] = []
    real = capsys.readouterr

    def wrapped():
        result = real()
        seen.append(result.out)
        seen.append(result.err)
        return result

    monkeypatch.setattr(capsys, "readouterr", wrapped)
    yield
    if request.node.name in {
        "test_codex_one_host_crash_does_not_hide_the_other",
        "test_codex_failed_host_receipt_names_the_path",
    }:
        return
    leftover = real()
    seen.append(leftover.out)
    seen.append(leftover.err)
    assert "reason: unexpected" not in "".join(seen)
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
# toml_edit 0.24.0 keeps CRLF inside the multi-line string and writes LF elsewhere.
TOML_EDIT_CRLF_PRODUCER = (
    'title = """line\r\nmore"""\n'
    "\n"
    "[mcp_servers.alice]\n"
    'command = "uvx"\n'
    'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
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

    producers = (
        ("mcp-remove", MCP_REMOVE_PRODUCER),
        ("import", IMPORT_PRODUCER),
        ("toml-edit-crlf", TOML_EDIT_CRLF_PRODUCER),
    )
    for label, original in producers:
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
    """Tables before and after Alice stay. A dotted tools key alone stays. Both together are refused."""

    tables = (
        "[mcp_servers.alice.tools.before]\n"
        'approval_mode = "prompt"\n'
        "\n"
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "\n"
        "[mcp_servers.alice.tools.after]\n"
        'approval_mode = "approve"\n'
    )
    dotted = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        'tools.alice_recall = { approval_mode = "approve" }\n'
    )
    both_before = (
        "[mcp_servers.alice.tools.before]\n"
        'approval_mode = "prompt"\n'
        "\n"
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        'tools.alice_recall = { approval_mode = "approve" }\n'
    )
    both_after = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        'tools.alice_recall = { approval_mode = "approve" }\n'
        "\n"
        "[mcp_servers.alice.tools.after]\n"
        'approval_mode = "approve"\n'
    )
    for label, original in (("tables", tables), ("dotted", dotted)):
        home = tmp_path / label
        path = _seed(home, original)
        code, out, err = _install(home, tmp_path / f"vault-{label}", capsys)
        assert code == 0, (label, out, err)
        written = path.read_text(encoding="utf-8")
        if label == "tables":
            assert '[mcp_servers.alice.tools.before]\napproval_mode = "prompt"\n' in written
            assert '[mcp_servers.alice.tools.after]\napproval_mode = "approve"\n' in written
        else:
            assert 'tools.alice_recall = { approval_mode = "approve" }' in written
    for label, original in (("before", both_before), ("after", both_after)):
        home = tmp_path / f"both-{label}"
        path = _seed(home, original)
        before = path.read_bytes()
        code, out, err = _install(home, tmp_path / f"vault-both-{label}", capsys)
        assert code == 1, (label, out, err)
        assert path.read_bytes() == before
        assert _backups(tmp_path / f"vault-both-{label}") == []
        assert (
            "next: config.toml was not changed. Move each tools.<name> key into its own "
            "[mcp_servers.alice.tools.<name>] table, then run install again."
        ) in out


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
        ("startup_timeout_ms = 60000.0", "startup_timeout_ms"),
        ("startup_timeout_ms = -1", "startup_timeout_ms"),
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


_HAND_NEXT = (
    "next: config.toml was not changed. Edit the alice entry by hand instead, "
    "then check it with: codex mcp get alice"
)
_ADD_NEXT = (
    "next: config.toml was not changed. Add the alice entry above under "
    "[mcp_servers] by hand, then check it with: codex mcp get alice"
)
_COMMENT_NEXT = "next: config.toml was not changed. Remove the comment, then run install again."


def _remove_next(key: str) -> str:
    return (
        "next: install will not edit an alice entry that holds "
        f"{key}. Keep editing it by hand, or remove {key} for good and run install again."
    )


@pytest.mark.parametrize(
    ("label", "original", "needle", "nxt"),
    [
        (
            "inline alice",
            "[mcp_servers]\nalice = { command = \"uvx\", args = [\"a\"] }\n",
            "inline table",
            _HAND_NEXT,
        ),
        (
            "dotted root",
            'mcp_servers.alice.command = "uvx"\n',
            "dotted keys",
            _HAND_NEXT,
        ),
        (
            "dotted inside",
            "[mcp_servers]\nalice.command = \"uvx\"\n",
            "dotted keys",
            _HAND_NEXT,
        ),
        (
            "other table",
            "[mcp_servers.alice]\ncommand = \"uvx\"\nargs = [\"a\"]\n\n[mcp_servers.alice.other]\nk = 1\n",
            "holds other",
            _remove_next("other"),
        ),
        (
            "cwd",
            "[mcp_servers.alice]\ncommand = \"uvx\"\nargs = [\"a\"]\ncwd = \"/tmp\"\n",
            "holds cwd",
            _remove_next("cwd"),
        ),
        (
            "url",
            "[mcp_servers.alice]\ncommand = \"uvx\"\nargs = [\"a\"]\nurl = \"https://example.test\"\n",
            "holds url",
            _remove_next("url"),
        ),
        (
            "enabled_tools",
            "[mcp_servers.alice]\ncommand = \"uvx\"\nargs = [\"a\"]\nenabled_tools = [\"a\"]\n",
            "holds enabled_tools",
            _remove_next("enabled_tools"),
        ),
        (
            "undocumented env",
            "[mcp_servers.alice]\ncommand = \"uvx\"\nargs = [\"a\"]\n\n[mcp_servers.alice.env]\nNOPE = \"1\"\n",
            "holds env.NOPE",
            _remove_next("env.NOPE"),
        ),
        (
            "comment between",
            "[mcp_servers.alice]\ncommand = \"uvx\"\n# inside\nargs = [\"a\"]\n",
            "comment between",
            _COMMENT_NEXT,
        ),
        (
            "trailing comment",
            "[mcp_servers.alice]\ncommand = \"uvx\" # note\nargs = [\"a\"]\n",
            "trailing comment",
            _COMMENT_NEXT,
        ),
        ("bom", "\ufeffmodel = 1\n", "BOM", _ADD_NEXT),
        ("mixed", "a = 1\r\nb = 2\n", "mixed line endings", _ADD_NEXT),
        ("cr", "a = 1\rb = 2\n", "lone CR", _ADD_NEXT),
        ("newline inline", "t = { a = 1,\n b = 2 }\n", "newline inside an inline table", _ADD_NEXT),
        ("trailing comma", "t = { a = 1, }\n", "trailing comma inside an inline table", _ADD_NEXT),
        ("escape e", 't = "\\e"\n', "\\e escape", _ADD_NEXT),
        ("escape x", 't = "\\x41"\n', "\\x escape", _ADD_NEXT),
        ("time", "t = 07:32\n", "without seconds", _ADD_NEXT),
        ("datetime", "t = 1979-05-27T07:32\n", "without seconds", _ADD_NEXT),
        ("inline mcp", 'mcp_servers = { alice = { command = "uvx" } }\n', "inline table", _HAND_NEXT),
    ],
)
def test_codex_refusals(
    label: str,
    original: str,
    needle: str,
    nxt: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bytes stay, there is no backup, and the next line is the one for this case."""

    home = tmp_path / "home"
    path = _seed(home, original)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (label, out, err)
    assert path.read_bytes() == before
    assert _backups(tmp_path / "vault") == []
    assert needle in out, (label, out)
    assert nxt in out, (label, out)
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
    assert "note: Codex will run both alice and second" in out


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
    layer = codex / "work.config.toml"
    assert f"note: {layer} defines mcp_servers.alice, and Codex merges it" in out


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
    assert (
        "note: Codex rejects the output of alice-memory-session-start. "
        "Remove it from hooks.json"
    ) in out
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "uvx alice-memory-session-start --format json "
                                        "--format markdown"
                                    ),
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    code, out, err = _install(home, tmp_path / "vault-markdown", capsys)
    assert code == 0, err
    assert "Codex rejects the output" not in out
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "uvx alice-memory-session-start --format markdown "
                                        "--format json"
                                    ),
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    code, out, err = _install(home, tmp_path / "vault-json", capsys)
    assert code == 0, err
    assert "note: Codex rejects the output of alice-memory-session-start" in out


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
    assert "config.toml changed while install ran" in out
    assert "next: config.toml was not changed. Run install again." in out
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

    import inspect

    assert inspect.signature(fuzz.run).parameters["configs_per_seed"].default == len(fuzz.LABELS)
    literal = "quoted = \"x ''' y\"\nticks = '''a # not a comment'''\n# cabcde after\n"
    assert host_install._codex_comments_outside(literal, "\n", ()) == ["# cabcde after"]
    counts = fuzz.run(range(3), configs_per_seed=len(fuzz.LABELS), mutants_per_config=2)
    refusable = {"comment-in-args", "tools-key-and-table", "out-of-range"}
    writable = [label for label in fuzz.LABELS if label not in refusable]
    assert counts.generated_written == 3 * len(writable)
    for label in fuzz.LABELS:
        if label in refusable:
            assert counts.by_label.get(label, 0) == 0
            assert counts.refused_labels.get(label, 0) >= 3, counts.refused_labels
            continue
        assert counts.by_label.get(label, 0) >= 3, counts.by_label
    for kind in fuzz.MUTATIONS:
        assert kind in counts.by_mutation, counts.by_mutation
        assert sum(counts.by_mutation[kind].values()) > 0
    bases = fuzz.codex_bases(20, seed=1)
    assert len({item[0] for item in bases}) > 7


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
    secret = "sk-" + "realhost" + "value"
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
        f'ALICE_AGENT_API_KEY = "{secret}"\n'
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
    assert secret not in out and secret not in err
    assert "<hidden>" in out
    assert "action: dry-run" in out

    for case, data_dir in fuzz.codex_bases(50, seed=4):
        path.write_text(case, encoding="utf-8")
        got = codex("mcp", "get", "alice", "--json")
        assert got.returncode == 0, (got.stderr, case)
        payload = json.loads(got.stdout)
        planned = host_install._plan_codex_text(
            case,
            data_dir,
            data_dir,
            home=home,
            search=host_install.LauncherSearch(host_install.UVX_LAUNCHER, None, True),
            problem_of=lambda _launcher: None,
        )
        assert payload["transport"]["command"] == planned.payload["command"]
        assert payload["transport"]["args"] == planned.payload["args"]


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


def test_codex_refuses_a_comment_inside_args(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A comment inside multi-line args is kept, and install does not report written."""

    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        "args = [\n"
        '  "alice-memory", # pinned by hand, see ticket 42\n'
        '  "mcp",\n'
        '  "--data-dir",\n'
        '  "/old",\n'
        "]\n"
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (out, err)
    assert path.read_bytes() == before
    assert _backups(tmp_path / "vault") == []
    assert "# pinned by hand, see ticket 42" in path.read_text(encoding="utf-8")
    assert "next: config.toml was not changed. Remove the comment, then run install again." in out


def test_codex_keeps_a_datetime(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    original = "last_seen = 1979-05-27T07:32:00Z\n"
    home = tmp_path / "home"
    path = _seed(home, original)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, (out, err)
    written = path.read_text(encoding="utf-8")
    assert "last_seen = 1979-05-27T07:32:00Z" in written
    assert "action: written" in out
    assert "next: check it with: codex mcp get alice" in out


def test_codex_refuses_numbers_codex_cannot_load(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = (
        ("n = 9223372036854775808\n", "integer outside the i64 range"),
        ("n = 0x8000000000000000\n", "integer outside the i64 range"),
        ("n = 0o1000000000000000000000\n", "integer outside the i64 range"),
        ("n = 0b1" + "0" * 63 + "\n", "integer outside the i64 range"),
        ("n = 9_223_372_036_854_775_808\n", "integer outside the i64 range"),
        ("n = 1e400\n", "float that is not finite"),
        ("n = 1e1_000\n", "float that is not finite"),
    )
    for original, needle in cases:
        home = tmp_path / "home"
        path = _seed(home, original)
        before = path.read_bytes()
        code, out, err = _install(home, tmp_path / "vault", capsys)
        assert code == 1, (original, out, err)
        assert path.read_bytes() == before
        assert needle in out
        assert "next:" in out


def test_codex_allows_inf_nan_and_in_range_integers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = "n = inf\nflag = nan\nbig = 9223372036854775807\n"
    home = tmp_path / "home"
    path = _seed(home, original)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, (out, err)
    written = path.read_text(encoding="utf-8")
    assert "n = inf" in written
    assert "flag = nan" in written
    assert "big = 9223372036854775807" in written


def test_codex_does_not_carry_forms_codex_loads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = (
        (
            "startup_timeout_sec = 1e19\n",
            "install does not carry this form of startup_timeout_sec",
        ),
        (
            'env_vars = [{ name = "X" }]\n',
            "install does not carry this form of env_vars",
        ),
    )
    header = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
    )
    for line, needle in cases:
        home = tmp_path / "home"
        path = _seed(home, header + line)
        before = path.read_bytes()
        code, out, err = _install(home, tmp_path / "vault", capsys)
        assert code == 1, (line, out, err)
        assert path.read_bytes() == before
        assert needle in out
        assert "Codex would refuse to load this value" not in out


def test_codex_inserts_under_a_dotted_other_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = 'mcp_servers.other.command = "true"\n'
    home = tmp_path / "home"
    path = _seed(home, original)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, (out, err)
    loaded = tomllib.loads(path.read_text(encoding="utf-8"))
    assert loaded["mcp_servers"]["other"]["command"] == "true"
    assert loaded["mcp_servers"]["alice"]["command"] == "uvx"


def test_codex_tools_table_without_alice_has_its_own_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = '[mcp_servers.alice.tools.alice_recall]\napproval_mode = "approve"\n'
    home = tmp_path / "home"
    path = _seed(home, original)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (out, err)
    assert path.read_bytes() == before
    assert "a tools table exists and [mcp_servers.alice] does not" in out
    assert "dotted keys" not in out


def test_codex_located_refusal_keeps_the_entry_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = "/tmp/my-real-vault"
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx" # note\n'
        f'args = ["alice-memory", "mcp", "--data-dir", "{vault}"]\n'
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    before = path.read_bytes()
    code = onramp_main(["install", "--home", str(home), "--host", "codex"])
    captured = capsys.readouterr()
    assert code == 1, captured.err
    assert path.read_bytes() == before
    assert vault in captured.out
    assert (
        "next: config.toml was not changed. Remove the comment, then run install again."
    ) in captured.out
    assert "Add the alice entry above" not in captured.out


def test_codex_env_comment_and_unquoted_value_name_the_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    comment = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "\n"
        "[mcp_servers.alice.env]\n"
        'ALICE_MCP_FULL_TOOLS = "1" # kept\n'
    )
    unquoted = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "\n"
        "[mcp_servers.alice.env]\n"
        "ALICE_MCP_FULL_TOOLS = 1\n"
    )
    home = tmp_path / "comment"
    path = _seed(home, comment)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault-comment", capsys)
    assert code == 1, (out, err)
    assert path.read_bytes() == before
    assert "next: config.toml was not changed. Remove the comment, then run install again." in out
    assert "remove env for good" not in out
    home = tmp_path / "unquoted"
    path = _seed(home, unquoted)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault-unquoted", capsys)
    assert code == 1, (out, err)
    assert path.read_bytes() == before
    assert "next: config.toml was not changed. Quote the value, then run install again." in out
    assert "remove env.ALICE_MCP_FULL_TOOLS for good" not in out


def test_codex_empty_env_is_absent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "env = {}\n"
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, (out, err)
    assert "changed a carried value" not in out
    loaded = tomllib.loads(path.read_text(encoding="utf-8"))
    assert "env" not in loaded["mcp_servers"]["alice"]


def test_codex_one_host_crash_does_not_hide_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    _seed(home, 'model = "x"\n')

    def boom(*_args: object, **_kwargs: object) -> object:
        raise TypeError("datetime")

    monkeypatch.setattr(host_install, "_plan_codex_text", boom)
    code = onramp_main(
        ["install", "--home", str(home), "--data-dir", str(vault), "--host", "cursor", "--host", "codex"]
    )
    captured = capsys.readouterr()
    assert code != 0
    assert "host: cursor" in captured.out
    assert "host: codex" in captured.out
    assert "action: failed" in captured.out
    assert "unexpected TypeError" in captured.out
    assert (home / ".cursor" / "mcp.json").is_file()


def test_codex_new_file_bytes_are_exact(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    shown = str(vault.resolve())
    expected = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "' + shown + '"]\n'
    )
    assert _config(home).read_bytes() == expected.encode()
    assert "next: check it with: codex mcp get alice" in out


def test_codex_refusal_hides_a_secret_env_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "sk-" + "refusal" + "value"
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx" # note\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "\n"
        "[mcp_servers.alice.env]\n"
        f'ALICE_AGENT_API_KEY = "{secret}"\n'
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1
    assert path.read_bytes() == before
    assert secret not in out and secret not in err
    assert "<hidden>" in out


def test_codex_guard_failures_leave_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each guard failure is exit 1, unchanged bytes, and no backup."""

    original = (
        'model = "x"\n'
        "# coutside keep\n"
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
    )
    home = tmp_path / "home"
    real_splice = host_install._codex_splice
    real_render = host_install._codex_render_block

    def unparseable(*args: object, **kwargs: object) -> object:
        text, span = real_splice(*args, **kwargs)  # type: ignore[misc]
        return text + "\n=\n", span

    def outside_byte(*args: object, **kwargs: object) -> object:
        text, span = real_splice(*args, **kwargs)  # type: ignore[misc]
        return text.replace('model = "x"', 'model = "y"', 1), span

    def drop_comment(*args: object, **kwargs: object) -> object:
        text, span = real_splice(*args, **kwargs)  # type: ignore[misc]
        return text.replace("# coutside keep\n", "", 1), span

    def wrong_args(*args: object, **kwargs: object) -> str:
        entry = dict(args[0])  # type: ignore[index]
        entry["args"] = ["alice-memory", "mcp", "--data-dir", "/wrong"]
        return real_render(entry, *args[1:], **kwargs)  # type: ignore[misc]

    def drop_carried(*args: object, **kwargs: object) -> str:
        return real_render(args[0], (), *args[2:], **kwargs)  # type: ignore[misc]

    mutations = {
        "unparseable": ("_codex_splice", unparseable),
        "outside": ("_codex_splice", outside_byte),
        "comment": ("_codex_splice", drop_comment),
        "args": ("_codex_render_block", wrong_args),
        "carried": ("_codex_render_block", drop_carried),
    }
    for label, (target_name, fn) in mutations.items():
        if label == "carried":
            content = (
                'outside = "x"\n'
                "[mcp_servers.alice]\n"
                'command = "uvx"\n'
                'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
                "startup_timeout_sec = 60.0\n"
            )
        else:
            content = original
        path = _seed(home, content)
        before = path.read_bytes()
        monkeypatch.setattr(host_install, target_name, fn)
        code, out, err = _install(home, tmp_path / f"vault-{label}", capsys)
        monkeypatch.undo()
        assert code == 1, (label, out, err)
        assert "action: refused" in out, (label, out)
        assert "reason: unexpected" not in out, (label, out)
        assert "reason:" in out, (label, out)
        assert path.read_bytes() == before, label
        assert _backups(tmp_path / f"vault-{label}") == [], label


def test_codex_new_file_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    path = _config(home)
    real = host_install._codex_render_block

    def plant(*args: object, **kwargs: object) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('other = 1\n', encoding="utf-8")
        return real(*args, **kwargs)  # type: ignore[misc]

    monkeypatch.setattr(host_install, "_codex_render_block", plant)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (out, err)
    assert path.read_text(encoding="utf-8") == "other = 1\n"
    assert "next: config.toml was not changed. Run install again." in out
    assert _backups(tmp_path / "vault") == []


def test_codex_backup_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    path = _seed(home, 'model = "x"\n')
    seed = path.read_bytes()
    real = host_install._backup_host_file

    def change(*args: object, **kwargs: object) -> Path:
        path.write_bytes(seed + b"# changed\n")
        return real(*args, **kwargs)  # type: ignore[misc]

    monkeypatch.setattr(host_install, "_backup_host_file", change)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (out, err)
    assert path.read_bytes() == seed + b"# changed\n"
    assert "next: config.toml was not changed. Run install again." in out
    assert _backups(tmp_path / "vault") == []


def test_codex_home_through_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    codex_home = tmp_path / "elsewhere"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    code = onramp_main(["install", "--data-dir", str(tmp_path / "vault"), "--host", "codex"])
    captured = capsys.readouterr()
    assert code == 1, captured.err
    assert "does not exist" in captured.out
    assert (
        "next: config.toml was not changed. Point CODEX_HOME at an existing directory, "
        "or pass --home, then run install again."
    ) in captured.out
    code = onramp_main(
        ["install", "--data-dir", str(tmp_path / "vault-dry"), "--host", "codex", "--dry-run"]
    )
    captured = capsys.readouterr()
    assert code == 1, captured.err
    assert (
        "next: config.toml was not changed. Point CODEX_HOME at an existing directory, "
        "or pass --home, then run install again."
    ) in captured.out
    assert "dry run: install would refuse this file; nothing was attempted" in captured.out
    monkeypatch.setenv("CODEX_HOME", "relative/codex")
    code = onramp_main(
        ["install", "--home", str(tmp_path / "home"), "--data-dir", str(tmp_path / "vault"), "--host", "codex"]
    )
    captured = capsys.readouterr()
    # --home wins, and the note says CODEX_HOME points elsewhere.
    assert code == 0, captured.err
    assert (tmp_path / "home" / ".codex" / "config.toml").is_file()
    assert "CODEX_HOME points elsewhere" in captured.out
    code = onramp_main(["install", "--data-dir", str(tmp_path / "vault2"), "--host", "codex"])
    captured = capsys.readouterr()
    assert code == 1
    assert "next: Set CODEX_HOME to an absolute path, or pass --home, then run install again." in captured.out


def test_codex_refuses_non_utf8(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    path = _seed(home, b"\xff\xfe not utf8")
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1
    assert path.read_bytes() == before
    assert "not UTF-8" in out
    assert _backups(tmp_path / "vault") == []


def test_codex_unreadable_layer_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    layer = home / ".codex" / "work.config.toml"
    layer.parent.mkdir(parents=True)
    layer.write_text("[mcp_servers.alice]\ncommand = \"uvx\"\n", encoding="utf-8")
    real = Path.read_text

    def unreadable(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "work.config.toml":
            raise OSError("unreadable")
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", unreadable)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, err
    assert f"note: {layer} could not be read" in out


def test_codex_enabled_false_note_on_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "' + str((tmp_path / "vault").resolve()) + '"]\n'
        "enabled = false\n"
    )
    home = tmp_path / "home"
    _seed(home, original)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, err
    assert "action: unchanged" in out
    assert "Codex will not start alice" in out


def test_codex_dry_run_shows_carried_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "startup_timeout_sec = 60.0\n"
        'tools.alice_recall = { approval_mode = "approve" }\n'
    )
    home = tmp_path / "home"
    _seed(home, original)
    code, out, err = _install(home, tmp_path / "vault", capsys, "--dry-run")
    assert code == 0, err
    assert "startup_timeout_sec = 60.0" in out
    assert 'tools.alice_recall = { approval_mode = "approve" }' in out


def test_codex_rewrite_makes_the_file_private(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    path = _seed(home, 'model = "x"\n')
    path.chmod(0o644)
    code, _out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, err
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_codex_dry_run_hides_carried_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Carried strings are rendered from the parse and masked like args.

    Mutation: print the raw env_vars line. The token or the URL is in the
    receipt. This test fails.
    """

    token = "gh" + "p_" + "Q7xK9mN2pL4a"
    user = "us" + "er"
    password = "pa" + "ss"
    url = "https://" + user + ":" + password + "@example.test/hook"
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "env_vars = [\n"
        f'  "{token}",\n'
        f'  "{url}",\n'
        "]\n"
    )
    home = tmp_path / "home"
    _seed(home, original)
    code, out, err = _install(home, tmp_path / "vault", capsys, "--dry-run")
    assert code == 0, err
    assert token not in out and token not in err
    assert password not in out and user + ":" + password not in out
    assert "<hidden>" in out
    assert "\\u000a" not in out
    assert "env_vars = [" in out
    assert out.count("env_vars = [") == 1


def test_codex_refuses_tools_values_codex_cannot_load(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = (
        (
            'tools.alice_recall = "approve"\n',
            "tools.alice_recall",
        ),
        (
            '[mcp_servers.alice.tools]\nalice_recall = "approve"\n',
            "tools.alice_recall",
        ),
        (
            '[mcp_servers.alice.tools.alice_recall]\napproval_mode = "always"\n',
            "tools.alice_recall",
        ),
    )
    for extra, name in cases:
        home = tmp_path / name.replace(".", "-")
        original = (
            "[mcp_servers.alice]\n"
            'command = "uvx"\n'
            'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
            + extra
        )
        path = _seed(home, original)
        before = path.read_bytes()
        code, out, err = _install(home, tmp_path / f"vault-{name}", capsys)
        assert code == 1, (name, out, err)
        assert path.read_bytes() == before
        assert name in out
        assert (
            f"next: config.toml was not changed. Fix the {name} entry, then run install again."
        ) in out
        assert "action: written" not in out


def test_codex_env_shapes_use_the_move_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = (
        'env.KEY = "value"\n',
        'env = "x"\n',
        '[mcp_servers.alice.env]\nALICE.X = "y"\n',
        "[mcp_servers.alice.env.sub]\nk = 1\n",
    )
    for index, extra in enumerate(cases):
        home = tmp_path / f"home-{index}"
        original = (
            "[mcp_servers.alice]\n"
            'command = "uvx"\n'
            'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
            + extra
        )
        path = _seed(home, original)
        before = path.read_bytes()
        code, out, err = _install(home, tmp_path / f"vault-{index}", capsys)
        assert code == 1, (index, out, err)
        assert path.read_bytes() == before
        assert "remove env for good" not in out
        assert (
            'next: config.toml was not changed. Move each env key into '
            '[mcp_servers.alice.env] as KEY = "value", then run install again.'
        ) in out


def test_codex_empty_env_comment_stays(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    comment = '# ALICE_AGENT_API_KEY = "' + "paused" + '"\n'
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "\n"
        "[mcp_servers.alice.env]\n"
        + comment
    )
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _seed(home, original)
    code, out, err = _install(home, vault, capsys)
    assert code == 0, (out, err)
    written = path.read_text(encoding="utf-8")
    assert "[mcp_servers.alice.env]\n" + comment in written
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert "action: unchanged" in out
    assert path.read_text(encoding="utf-8") == written


def test_codex_number_like_keys_are_not_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = (
        "9223372036854775808 = 1\n"
        "1e400 = 1\n"
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, (out, err)
    written = path.read_text(encoding="utf-8")
    assert "9223372036854775808 = 1" in written
    assert "1e400 = 1" in written


def test_codex_number_range_names_the_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = "/real/codex-vault"
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        f'args = ["alice-memory", "mcp", "--data-dir", "{vault}"]\n'
        "startup_timeout_sec = 9223372036854775808\n"
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (out, err)
    assert path.read_bytes() == before
    assert "outside the i64 range (line 4)" in out
    assert vault in out
    assert "Add the alice entry above" not in out
    assert _HAND_NEXT in out


def test_codex_env_names_are_snippet_escaped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    forged = "A" + "\n" + "next: FORGED"
    raw_c1 = "B" + "\u0085"

    def quoted(name: str) -> str:
        return '"' + name.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'

    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx" # note\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "\n"
        "[mcp_servers.alice.env]\n"
        f"{quoted(forged)} = \"1\"\n"
        f"{quoted(raw_c1)} = \"2\"\n"
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (out, err)
    assert path.read_bytes() == before
    assert "\nnext: FORGED" not in out
    assert "\u0085" not in out
    assert "\\u000a" in out or "\\n" in out
    assert "\\u0085" in out


def test_codex_bare_tools_header_is_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tools = (
        "[mcp_servers.alice.tools] \n"
        "[mcp_servers.alice.tools.alice_recall]\n"
        'approval_mode = "approve"\n'
    )
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "\n"
        + tools
    )
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _seed(home, original)
    code, out, err = _install(home, vault, capsys)
    assert code == 0, (out, err)
    written = path.read_text(encoding="utf-8")
    assert tools in written
    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert "action: unchanged" in out
    assert path.read_text(encoding="utf-8") == written


def test_codex_guard_checks_are_separate() -> None:
    """Each guard reason can fire on its own.

    The comment-list check is not redundant. A block that ends inside a
    comment, with no newline before an outside ``# tail``, leaves the
    outside bytes unchanged and only this check refuses it.
    """

    entry = {"command": "uvx", "args": ["alice-memory", "mcp", "--data-dir", "/v"]}
    original = 'model = "x"\n'
    alice = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/v"]\n'
    )
    old = tomllib.loads(original)
    edited = original + "\n" + alice + "[zz_extra]\nkey = 1\n"
    with pytest.raises(CodexConfigRefused, match="changed a value outside alice"):
        host_install._codex_guard(
            original, edited, "\n", entry, (), (len(original), len(edited)), old
        )
    prefix = 'model = "x" \n\n'
    edited_ws = prefix + alice
    with pytest.raises(CodexConfigRefused, match="changed bytes outside alice"):
        host_install._codex_guard(
            original, edited_ws, "\n", entry, (), (len(prefix), len(edited_ws)), old
        )
    with pytest.raises(CodexConfigRefused, match="missing mcp_servers.alice"):
        host_install._codex_guard(
            original, original, "\n", entry, (), (len(original), len(original)), old
        )
    original_tail = 'model = "x"\n# tail'
    edited_tail = 'model = "x"\n' + alice + "# c# tail"
    insert_start = len('model = "x"\n')
    insert_end = len(edited_tail) - len("# tail")
    with pytest.raises(CodexConfigRefused, match="changed a comment outside alice"):
        host_install._codex_guard(
            original_tail,
            edited_tail,
            "\n",
            entry,
            (),
            (insert_start, insert_end),
            tomllib.loads(original_tail),
        )


@pytest.mark.skipif(os.environ.get(REAL_HOSTS_ENV) != "1", reason="set ALICE_TEST_REAL_HOSTS=1")
@pytest.mark.skipif(sys.platform != "linux", reason="the Codex real-host check runs on Linux")
@pytest.mark.skipif(_codex_bin() is None, reason="codex is not on PATH")
def test_real_codex_tools_key_and_table(tmp_path: Path) -> None:
    """Record what codex-cli 0.158.0 does with a tools key and a tools table.

    A table header and then a dotted tools key is a duplicate key. The same
    key in both forms is a duplicate key in either order. A dotted key and
    then a table for a different name loads.
    """

    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    path = _config(home)
    cases = {
        "table-then-dotted": (
            "[mcp_servers.alice.tools.before]\n"
            'approval_mode = "prompt"\n'
            "\n"
            "[mcp_servers.alice]\n"
            'command = "uvx"\n'
            'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
            'tools.alice_recall = { approval_mode = "approve" }\n'
        ),
        "same-key-dotted-then-table": (
            "[mcp_servers.alice]\n"
            'command = "uvx"\n'
            'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
            'tools.alice_recall = { approval_mode = "approve" }\n'
            "\n"
            "[mcp_servers.alice.tools.alice_recall]\n"
            'approval_mode = "prompt"\n'
        ),
        "dotted-then-other-table": (
            "[mcp_servers.alice]\n"
            'command = "uvx"\n'
            'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
            'tools.alice_recall = { approval_mode = "approve" }\n'
            "\n"
            "[mcp_servers.alice.tools.after]\n"
            'approval_mode = "approve"\n'
        ),
    }
    expected = {
        "table-then-dotted": 1,
        "same-key-dotted-then-table": 1,
        "dotted-then-other-table": 0,
    }
    for label, body in cases.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        got = _run_codex(home, cwd, "mcp", "list", "--json")
        assert got.returncode == expected[label], (label, got.returncode, got.stderr)
        if expected[label] == 1:
            assert "duplicate key" in got.stderr, (label, got.stderr)


def test_codex_unhashable_approval_mode_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A list or table approval mode is a refusal, not a TypeError.

    Mutation: test membership with ``value in _CODEX_APPROVAL_MODES`` on the
    raw value. The receipt says ``unexpected TypeError``. This test fails.
    """

    header = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
    )
    cases = (
        'tools.alice_recall = { approval_mode = ["approve"] }\n',
        "tools.alice_recall = { approval_mode = { x = 1 } }\n",
        'default_tools_approval_mode = ["approve"]\n',
    )
    for index, extra in enumerate(cases):
        home = tmp_path / f"home-{index}"
        path = _seed(home, header + extra)
        before = path.read_bytes()
        code, out, err = _install(home, tmp_path / f"vault-{index}", capsys)
        assert code == 1, (extra, out, err)
        assert path.read_bytes() == before
        assert "action: refused" in out
        assert "unexpected" not in out
        assert "approval" in out


def test_codex_output_token_limit_must_be_a_positive_integer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    header = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "[mcp_servers.alice.tools.alice_recall]\n"
    )
    for index, extra in enumerate(("output_token_limit = 0\n", "output_token_limit = 1.5\n")):
        home = tmp_path / f"limit-{index}"
        path = _seed(home, header + extra)
        before = path.read_bytes()
        code, out, err = _install(home, tmp_path / f"vault-limit-{index}", capsys)
        assert code == 1, (extra, out, err)
        assert path.read_bytes() == before
        assert "output_token_limit is not a positive integer" in out
        assert "action: refused" in out


def test_codex_array_numbers_are_refused_with_their_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    header = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
    )
    cases = (
        (header + "a = [1e400]\n", "float that is not finite (line 4)"),
        (
            header + "nums = [\n  9223372036854775808,\n]\n",
            "integer outside the i64 range (line 5)",
        ),
    )
    for index, (original, needle) in enumerate(cases):
        home = tmp_path / f"array-{index}"
        path = _seed(home, original)
        before = path.read_bytes()
        code, out, err = _install(home, tmp_path / f"vault-array-{index}", capsys)
        assert code == 1, (needle, out, err)
        assert path.read_bytes() == before
        assert needle in out


def test_codex_unparseable_header_uses_the_placeholder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'tools.alice_recall = { approval_mode = "approve", }\n'
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (out, err)
    assert path.read_bytes() == before
    assert "<the data dir your existing alice entry uses>" in out
    assert _HAND_NEXT in out


def test_codex_crlf_file_keeps_crlf_outside_strings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A CRLF file is still CRLF after install, outside multi-line strings.

    Mutation: write the block with LF. The written file has a bare LF, and
    the next install refuses mixed endings. This test fails.
    """

    from tests.unit.toml_judge import has_bare_lf_outside_multiline

    original = (
        "[mcp_servers.alice]\r\n"
        'command = "uvx"\r\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\r\n'
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, (out, err)
    written = path.read_bytes().decode("utf-8")
    assert "\r\n" in written
    assert not has_bare_lf_outside_multiline(written)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, err
    assert "mixed line endings" not in out


def test_codex_empty_multiline_string_allows_crlf_inside_another(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = (
        'x = """"""\n'
        'body = """\r\n'
        "kept\r\n"
        '"""\n'
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, (out, err)
    assert "mixed line endings" not in out
    assert 'x = """"""' in path.read_text(encoding="utf-8")


def test_codex_dry_run_masks_a_token_inside_tools(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token = "gh" + "p_" + "Q7xK9mN2pL4a"
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "tools.alice_recall = { approval_mode = \"approve\", note = \"" + token + "\" }\n"
    )
    home = tmp_path / "home"
    _seed(home, original)
    code, out, err = _install(home, tmp_path / "vault", capsys, "--dry-run")
    assert code == 0, (out, err)
    assert token not in out and token not in err
    assert "hidden: install printed these values from your file as <hidden>:" in out


def test_codex_dry_run_prints_dates_unquoted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        "tools.alice_recall = { approval_mode = \"approve\", when = 1979-05-27T07:32:00Z, "
        "seen_on = 1979-05-27, seen_at = 07:32:00 }\n"
    )
    home = tmp_path / "home"
    _seed(home, original)
    code, out, err = _install(home, tmp_path / "vault", capsys, "--dry-run")
    assert code == 0, (out, err)
    assert "1979-05-27T07:32:00Z" in out
    assert '"1979-05-27T07:32:00Z"' not in out
    assert "seen_on = 1979-05-27" in out or "1979-05-27" in out
    assert "07:32:00" in out
    assert '"07:32:00"' not in out


def test_codex_refusal_keeps_the_json_hook_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
                                    "command": "uvx alice-memory-session-start --format json",
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    original = (
        "[mcp_servers.alice]\n"
        'command = "uvx"\n'
        'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
        'cwd = "/tmp/work"\n'
    )
    path = _seed(home, original)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (out, err)
    assert path.read_bytes() == before
    assert "holds cwd" in out
    assert (
        "note: Codex rejects the output of alice-memory-session-start. "
        "Remove it from hooks.json"
    ) in out


def test_codex_failed_host_receipt_names_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A crash still names the Codex path, seals a newline, and prints next.

    Mutation: drop the seal, ignore CODEX_HOME, or omit the dry-run line.
    This test fails.
    """

    def boom(*_args: object, **_kwargs: object) -> object:
        raise TypeError("datetime")

    monkeypatch.setattr(host_install, "_install_codex_host", boom)
    vault = tmp_path / "vault"
    home = tmp_path / "home\nforged"
    code = onramp_main(
        ["install", "--home", str(home), "--data-dir", str(vault), "--host", "codex"]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "action: failed" in captured.out
    assert "reason: unexpected TypeError" in captured.out
    assert "next: config.toml was not changed. Run install again." in captured.out
    assert "\nforged" not in captured.out
    assert "\\u000a" in captured.out

    codex_home = tmp_path / "codex-real"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user-home")
    code = onramp_main(
        ["install", "--data-dir", str(vault), "--host", "codex", "--dry-run"]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert str(codex_home / "config.toml") in captured.out
    assert "next: config.toml was not changed. Run install again." in captured.out
    assert "dry run: install would refuse this file; nothing was attempted" in captured.out


def test_codex_locator_uses_the_parsed_entry_and_quoted_headers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bom = (
        "\ufeff[mcp_servers.alice]\n"
        'command = "alice-memory"\n'
        'args = ["mcp", "--data-dir", "/vault/from-file"]\n'
    )
    home = tmp_path / "bom"
    path = _seed(home, bom)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault-bom", capsys)
    assert code == 1, (out, err)
    assert path.read_bytes() == before
    assert 'command = "alice-memory"' in out
    assert "/vault/from-file" in out

    spaced = '[ "mcp_servers" . "alice" ]\ncommand = "uvx"\n['
    home = tmp_path / "spaced"
    path = _seed(home, spaced)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault-spaced", capsys)
    assert code == 1, (out, err)
    assert path.read_bytes() == before
    assert "<the data dir your existing alice entry uses>" in out
    assert _HAND_NEXT in out

    inside = 'note = """\n[mcp_servers.alice]\n"""\r\nmodel = "x"\n'
    home = tmp_path / "inside"
    path = _seed(home, inside)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault-inside", capsys)
    assert code == 1, (out, err)
    assert path.read_bytes() == before
    assert _ADD_NEXT in out
    assert _HAND_NEXT not in out
    assert "<the data dir your existing alice entry uses>" not in out

    home = tmp_path / "binary"
    path = _config(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"[mcp_servers.alice]\ncommand = \"uvx\"\n\xff\n")
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault-binary", capsys)
    assert code == 1, (out, err)
    assert path.read_bytes() == before
    assert "is not UTF-8" in out
    assert _HAND_NEXT in out
    assert "<the data dir your existing alice entry uses>" in out


_NESTED_DEPTH = 100_000
_ALICE_ENTRY = (
    "[mcp_servers.alice]\n"
    'command = "uvx"\n'
    'args = ["alice-memory", "mcp", "--data-dir", "/old"]\n'
)


def _nested_value(depth: int = _NESTED_DEPTH) -> str:
    return "x = " + "[" * depth + "]" * depth + "\n"


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("utf8", "reason: config.toml nests too deeply"),
        ("bom", "reason: config.toml starts with a BOM; remove the BOM"),
        ("non-utf8", "reason: config.toml is not UTF-8"),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_codex_deeply_nested_config_is_refused_not_a_crash(
    kind: str,
    reason: str,
    dry_run: bool,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A value nested 100,000 levels deep is a refusal, and the file is untouched.

    tomllib raises RecursionError for it. The planner, the guard, the
    refusal locator and the layer check each catch it. This test covers the
    planner for a UTF-8 file, and the locator for a BOM and a non-UTF-8 file.

    Mutation: catch only TOMLDecodeError in ``_plan_codex_text`` (utf8) or in
    ``_codex_locate_for_refusal`` (bom, non-utf8). The receipt says
    ``unexpected RecursionError``. This test fails.
    """

    body = _nested_value() + _ALICE_ENTRY
    home = tmp_path / "home"
    if kind == "utf8":
        path = _seed(home, body)
    elif kind == "bom":
        path = _seed(home, "﻿" + body)
    else:
        path = _seed(home, body.encode("utf-8") + b"# caf\xe9\n")
    before = path.read_bytes()
    extra = ("--dry-run",) if dry_run else ()
    code, out, err = _install(home, tmp_path / "vault", capsys, *extra)
    assert code == 1, (out, err)
    assert path.read_bytes() == before
    assert _backups(tmp_path / "vault") == []
    lines = out.splitlines()
    assert ("action: would-refuse" if dry_run else "action: refused") in lines
    assert reason in lines
    assert "<the data dir your existing alice entry uses>" in out
    assert _HAND_NEXT in lines


def test_codex_plan_refuses_a_deeply_nested_value() -> None:
    """The public planner refuses too, not only the install command.

    Mutation: drop the RecursionError catch in ``_plan_codex_text``. This
    test raises RecursionError instead of CodexConfigRefused and fails.
    """

    with pytest.raises(CodexConfigRefused) as caught:
        plan_codex_config(_nested_value() + _ALICE_ENTRY, "/v")
    assert caught.value.detail == "config.toml nests too deeply"


def test_codex_guard_refuses_a_deeply_nested_edit() -> None:
    """The guard parses the edited text itself, so it needs its own catch.

    Mutation: drop the RecursionError catch on the guard's ``tomllib.loads``.
    This test raises RecursionError and fails.
    """

    original = _nested_value(10)
    edited = original + "\n" + _ALICE_ENTRY
    deep = _nested_value() + "\n" + _ALICE_ENTRY
    with pytest.raises(CodexConfigRefused) as caught:
        host_install._codex_guard(
            original,
            deep,
            "\n",
            {"command": "uvx", "args": ["alice-memory", "mcp", "--data-dir", "/old"]},
            (),
            (len(original), len(edited)),
            {},
        )
    assert caught.value.detail == "config.toml nests too deeply"


def test_codex_guard_refuses_when_a_value_walker_overflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value can parse and still overflow the recursive comparison.

    A 492-level array in a tools table did this in a real run. The depth
    depends on the interpreter, so the walker is made to overflow instead.

    Mutation: call ``_codex_guard_values`` without its RecursionError catch.
    This test raises RecursionError and fails.
    """

    def overflow(_left: object, _right: object) -> bool:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(host_install, "_codex_equal", overflow)
    original = 'model = "x"\n'
    edited = original + "\n" + _ALICE_ENTRY
    with pytest.raises(CodexConfigRefused) as caught:
        host_install._codex_guard(
            original,
            edited,
            "\n",
            {"command": "uvx", "args": ["alice-memory", "mcp", "--data-dir", "/old"]},
            (),
            (len(original), len(edited)),
            {},
        )
    assert caught.value.detail == "config.toml nests too deeply"


def test_codex_deeply_nested_layer_file_is_an_unreadable_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A profile layer nested 100,000 levels deep gets the unreadable note.

    Mutation: catch only TOMLDecodeError in ``_codex_layer_defines_alice``.
    Install fails with ``unexpected RecursionError``. This test fails.
    """

    home = tmp_path / "home"
    layer = home / ".codex" / "work.config.toml"
    layer.parent.mkdir(parents=True)
    layer.write_text(_nested_value(), encoding="utf-8")
    layer_before = layer.read_bytes()
    note = f"note: {layer} could not be read"

    code, out, err = _install(home, tmp_path / "vault", capsys, "--dry-run")
    assert code == 0, (out, err)
    assert "action: dry-run" in out.splitlines()
    assert note in out.splitlines()
    assert not _config(home).exists()

    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, (out, err)
    assert "action: written" in out.splitlines()
    assert note in out.splitlines()
    assert "mcp_servers.alice" in _config(home).read_text(encoding="utf-8")
    assert layer.read_bytes() == layer_before


def test_codex_non_utf8_layer_file_is_an_unreadable_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A profile layer that is not UTF-8 gets the unreadable note, not a crash.

    Mutation: catch only OSError around ``read_text`` in
    ``_codex_layer_defines_alice``. Install fails with
    ``unexpected UnicodeDecodeError``. This test fails.
    """

    home = tmp_path / "home"
    layer = home / ".codex" / "work.config.toml"
    layer.parent.mkdir(parents=True)
    layer.write_bytes(b"# caf\xe9\nx = 1\n")
    layer_before = layer.read_bytes()
    note = f"note: {layer} could not be read"

    code, out, err = _install(home, tmp_path / "vault", capsys, "--dry-run")
    assert code == 0, (out, err)
    assert "action: dry-run" in out.splitlines()
    assert note in out.splitlines()

    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 0, (out, err)
    assert "action: written" in out.splitlines()
    assert note in out.splitlines()
    assert layer.read_bytes() == layer_before


@pytest.mark.parametrize(
    "original",
    [
        'model = "x"\r\n',
        'model = "x"\r\nname = "y"',
        'body = """\r\nkept\r\n"""\r\n',
        "# don't touch\r\nmodel = \"x\"\r\n",
    ],
    ids=["final-newline", "no-final-newline", "multiline-string", "quote-in-comment"],
)
def test_codex_crlf_file_without_alice_gets_a_crlf_block(
    original: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The block and the blank line before it are CRLF, and a re-run changes nothing.

    Mutation: render the appended block with LF, or write the separator in
    ``_codex_prepare_insert`` as LF. The written bytes differ from the
    expected CRLF bytes. This test fails.
    """

    from tests.unit.toml_judge import has_bare_lf_outside_multiline

    assert not has_bare_lf_outside_multiline(original)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _seed(home, original)
    code, out, err = _install(home, vault, capsys)
    assert code == 0, (out, err)
    shown = str(vault.resolve())
    block = (
        "[mcp_servers.alice]\r\n"
        'command = "uvx"\r\n'
        'args = ["alice-memory", "mcp", "--data-dir", "' + shown + '"]\r\n'
    )
    ending = "" if original.endswith("\r\n") else "\r\n"
    expected = original + ending + "\r\n" + block
    written = path.read_bytes().decode("utf-8")
    assert written == expected
    assert not has_bare_lf_outside_multiline(written)
    assert tomllib.loads(written)["mcp_servers"]["alice"]["command"] == "uvx"

    code, out, err = _install(home, vault, capsys)
    assert code == 0, (out, err)
    assert "action: unchanged" in out.splitlines()
    assert path.read_bytes().decode("utf-8") == expected


_PLACEHOLDER_ARGS = (
    'args = ["alice-memory", "mcp", "--data-dir", "<the data dir your existing alice entry uses>"]'
)


@pytest.mark.parametrize(
    ("original", "expected_args"),
    [
        ('x = 1e400\n[mcp_servers.alice]\ncommand = "uvx"\n', _PLACEHOLDER_ARGS),
        (
            'x = 1e400\n[mcp_servers.alice]\n\n[mcp_servers.alice.env]\nALICE_AGENT_API_KEY = "k"\n',
            _PLACEHOLDER_ARGS,
        ),
        (
            "[mcp_servers.alice]\ncwd = \"/x\"\n\n[mcp_servers.alice.env]\nALICE_AGENT_API_KEY = \"k\"\n",
            _PLACEHOLDER_ARGS,
        ),
        (
            '[mcp_servers.alice]\ncommand = "uvx"\nargs = ["alice-memory", "mcp", 1]\ncwd = "/x"\n',
            _PLACEHOLDER_ARGS,
        ),
        (
            '[mcp_servers.alice]\nargs = ["alice-memory", "mcp", "--data-dir", "/old"]\ncwd = "/x"\n',
            'args = ["alice-memory", "mcp", "--data-dir", "/old"]',
        ),
    ],
    ids=["command-only", "env-only", "env-only-planner", "args-not-strings", "args-only"],
)
def test_codex_refusal_snippet_is_never_a_partial_entry(
    original: str,
    expected_args: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An entry without a whole launcher prints install's launcher, not blanks.

    A snippet of ``command = ""`` or ``args = []`` is not something to paste.

    Mutation: build the refusal payload from any command, args or env, as
    before. The snippet has ``command = ""`` or ``args = []``. This test
    fails.
    """

    home = tmp_path / "home"
    path = _seed(home, original)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (out, err)
    assert path.read_bytes() == before
    lines = out.splitlines()
    assert 'command = ""' not in lines
    assert "args = []" not in lines
    assert 'command = "uvx"' in lines
    assert expected_args in lines
    assert "[mcp_servers.alice.env]" not in lines


def test_codex_refusal_snippet_keeps_a_whole_entry_and_hides_its_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A whole launcher is shown as the entry has it, and its env value is hidden."""

    secret = "sk-" + "wholeentry" + "value"
    original = (
        "[mcp_servers.alice]\n"
        'command = "alice-memory"\n'
        'args = ["mcp", "--data-dir", "/vault/from-file"]\n'
        'cwd = "/x"\n'
        "\n"
        "[mcp_servers.alice.env]\n"
        f'ALICE_AGENT_API_KEY = "{secret}"\n'
    )
    home = tmp_path / "home"
    path = _seed(home, original)
    before = path.read_bytes()
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (out, err)
    assert path.read_bytes() == before
    lines = out.splitlines()
    assert 'command = "alice-memory"' in lines
    assert 'args = ["mcp", "--data-dir", "/vault/from-file"]' in lines
    assert 'ALICE_AGENT_API_KEY = "<hidden>"' in lines
    assert secret not in out and secret not in err
