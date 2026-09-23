"""alice-memory install on JSON hosts: review round 3 findings, 2026-09-23.

Why this exists. The round-3 review of the S4 install work found, and the
tower reproduced:

- 1: the data dir was read from env ALICE_MEMORY_DATA_DIR when args had
  none, but ``alice-memory mcp`` never reads that env, so the hook moved to
  a dir the server did not open. --db entries and repeated --data-dir were
  not handled.
- 8: a Claude Code run whose hook write failed reported ``action: failed``
  with ``session_start: added``.
- 9: "would not change" compared bytes, so a .claude.json that Claude Code
  reformats was rewritten and backed up on every run.
- 13: dry-run printed whole host files, other servers' tokens included.
- 14: JSON nested deeply enough raised RecursionError out of the run.
- 15: a symlinked host config was replaced by a regular file.
- 16: Cursor dropped the user's extra keys on the Alice hook item.

One more was found by running install while writing the round-3 docs: the
refusal for a --db entry plus --data-dir offered an entry on ~/.alice.

How they escaped: the round-2 tests seeded entries whose args always held
--data-dir, hooks files whose writes succeeded, files in install's own
format, small documents and regular files, never a symbolic link, and
checked dry-run output only for the lines they expected.

Each test names the edit that makes it fail.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from alicebot_api.host_install import HERMES_BACKUP_MARKER, host_file_map
from alicebot_api.onramp import _ERROR_CONTRACTS, main as onramp_main

pytestmark = pytest.mark.usefixtures("uvx_on_path")


def _install(capsys, home: Path, *extra: str) -> tuple[int, str, list[object]]:
    code = onramp_main(["install", "--home", str(home), *extra])
    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.err.splitlines() if line.startswith("{")]
    return code, captured.out, records


def _error(code: str) -> dict:
    return {"error": {"code": code, "message": _ERROR_CONTRACTS[code]}}


def _files(home: Path, host: str) -> dict[str, Path]:
    return host_file_map(home.resolve())[host]


def _seed(path: Path, doc: object, *, indent: int = 2) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, indent=indent) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


def _claude_hook(home: Path) -> list[str]:
    doc = json.loads(_files(home, "claude-code")["hooks"].read_text(encoding="utf-8"))
    return [h["command"] for group in doc["hooks"]["SessionStart"] for h in group["hooks"]]


def _block(out: str, host: str) -> str:
    for block in out.split("\n\n"):
        if block.startswith(f"host: {host}\n"):
            return block
    raise AssertionError(f"no receipt for {host}:\n{out}")


# --- finding 1: the data dir is the one the server opens ------------------------------


def test_env_only_entry_is_on_the_default_dir_and_the_hook_follows_it(
    tmp_path: Path, capsys
) -> None:
    """args without --data-dir run on ~/.alice whatever env ALICE_MEMORY_DATA_DIR says.

    The entry stays as it was; the hook goes to ~/.alice; a note names the
    mismatch. Mutation: read the env as the data dir. This test fails.
    """

    home = tmp_path / "home"
    entry = {"command": "uvx", "args": ["alice-memory", "mcp"], "env": {"ALICE_MEMORY_DATA_DIR": "/custom"}}
    mcp_text = _seed(_files(home, "claude-code")["mcp"], {"mcpServers": {"alice": entry}})

    code, out, records = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, records)
    assert _files(home, "claude-code")["mcp"].read_text(encoding="utf-8") == mcp_text
    default = home.resolve() / ".alice"
    assert _claude_hook(home) == [
        f"uvx --from alice-memory alice-memory-session-start --data-dir {default}"
    ]
    # Review round 4 P7: the old value is not printed.
    assert "env ALICE_MEMORY_DATA_DIR is set to another dir (<hidden>), but alice-memory mcp opens" in out
    assert "/custom" not in out
    assert "data_dir:" not in out


def test_db_entry_is_kept_and_its_hook_store_left_alone(tmp_path: Path, capsys) -> None:
    """--db without the flag: the entry stays; the hook keeps its own data dir.

    Mutation: point the hook at the entry's --data-dir or ~/.alice. This
    test fails.
    """

    home = tmp_path / "home"
    files = _files(home, "claude-code")
    entry = {"command": "uvx", "args": ["alice-memory", "mcp", "--db", "/x/alice.db"]}
    mcp_text = _seed(files["mcp"], {"mcpServers": {"alice": entry}})
    old_hook = "uvx --from alice-memory alice-memory-session-start --data-dir /hook/store"
    _seed(files["hooks"], {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": old_hook}]}]}})

    code, out, records = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, records)
    assert files["mcp"].read_text(encoding="utf-8") == mcp_text
    assert _claude_hook(home) == [old_hook]
    assert "this entry opens --db /x/alice.db" in out


def test_db_entry_hook_takes_the_entrys_launcher_and_keeps_its_store(
    tmp_path: Path, capsys
) -> None:
    """One launcher per host holds for --db entries too; the hook's data dir stays.

    Mutation: leave the --db entry's hook command as it was. This test fails.
    """

    home = tmp_path / "home"
    files = _files(home, "claude-code")
    entry = {"command": "uvx", "args": ["alice-memory==0.16.0", "mcp", "--db", "/x.db"]}
    _seed(files["mcp"], {"mcpServers": {"alice": entry}})
    old_hook = "uvx --from alice-memory alice-memory-session-start --data-dir /hook/store"
    _seed(files["hooks"], {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": old_hook}]}]}})
    code, out, records = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, records)
    assert _claude_hook(home) == [
        "uvx --from alice-memory==0.16.0 alice-memory-session-start --data-dir /hook/store"
    ]


def test_db_entry_without_a_hook_gets_none(tmp_path: Path, capsys) -> None:
    """Mutation: add a hook on ~/.alice for a --db entry. This test fails."""

    home = tmp_path / "home"
    files = _files(home, "claude-code")
    _seed(files["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": ["alice-memory", "mcp", "--db", "/x.db"]}}})
    code, out, records = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, records)
    assert not files["hooks"].exists()
    assert "so install added no SessionStart hook" in out


def test_db_entry_with_the_flag_is_refused_naming_the_conflict(tmp_path: Path, capsys) -> None:
    """--data-dir cannot move a --db entry: refuse, byte-identical.

    Mutation: write --data-dir next to --db. This test fails.
    """

    home = tmp_path / "home"
    path = _files(home, "claude-desktop")["mcp"]
    entry = {"command": "uvx", "args": ["alice-memory", "mcp", "--db", "/x.db"]}
    text = _seed(path, {"mcpServers": {"alice": entry}})
    code, out, records = _install(capsys, home, "--host", "claude-desktop", "--data-dir", str(tmp_path / "v"))
    assert code == 1 and records == [_error("install_refused")]
    assert path.read_text(encoding="utf-8") == text
    assert "opens the database --db /x.db, which --data-dir does not move" in out


def test_db_conflict_snippet_is_the_entry_on_the_dir_asked_for(tmp_path: Path, capsys) -> None:
    """The paste is this entry on --data-dir's dir, never install's entry on ~/.alice.

    Found by execution while writing the round-3 docs, 2026-09-23: the
    refusal printed an entry on ~/.alice (maybe an empty store), not on the
    dir the user passed, and a next line written for foreign entries.
    Mutation: offer install's entry on the plan's data dir. This test fails.
    """

    home = tmp_path / "home"
    path = _files(home, "claude-desktop")["mcp"]
    entry = {
        "command": "uvx",
        "args": ["alice-memory==0.16.0", "mcp", "--db", "/x.db"],
        "timeout": 30,
    }
    _seed(path, {"mcpServers": {"alice": entry}})
    vault = str((tmp_path / "v").resolve())
    code, out, _ = _install(capsys, home, "--host", "claude-desktop", "--data-dir", vault)
    assert code == 1
    block = _block(out, "claude-desktop")
    snippet = block.split("snippet:\n", 1)[1].split("\nnext:", 1)[0]
    assert json.loads(snippet) == {
        "mcpServers": {
            "alice": {
                "command": "uvx",
                "args": ["alice-memory==0.16.0", "mcp", "--data-dir", vault],
                "timeout": 30,
            }
        }
    }
    assert str(home.resolve() / ".alice") not in block and "~/.alice" not in block
    assert block.rstrip("\n").endswith(
        "next: claude_desktop_config.json was not changed. Run install without --data-dir "
        f"to keep --db /x.db, or replace the alice entry with the one above to use {vault}."
    )


def test_hermes_db_conflict_snippet_is_the_entry_on_the_dir_asked_for(
    tmp_path: Path, capsys
) -> None:
    """Hermes: same paste, with the env map on the new dir. Mutation: drop the paste."""

    home = tmp_path / "home"
    path = _files(home, "hermes")["mcp"]
    path.parent.mkdir(parents=True)
    text = (
        "mcp_servers:\n  alice:\n    command: uvx\n"
        "    args: [alice-memory, mcp, --db, /x.db]\n"
    )
    path.write_text(text, encoding="utf-8")
    vault = str((tmp_path / "v").resolve())
    code, out, _ = _install(capsys, home, "--host", "hermes", "--data-dir", vault)
    assert code == 1
    assert path.read_text(encoding="utf-8") == text
    block = _block(out, "hermes")
    assert f'      - "--data-dir"\n      - "{vault}"\n' in block
    assert f'      ALICE_MEMORY_DATA_DIR: "{vault}"\n' in block
    snippet = block.split("snippet:\n", 1)[1].split("\nnext:", 1)[0]
    assert "--db" not in snippet and "x.db" not in snippet
    assert str(home.resolve() / ".alice") not in block and "~/.alice" not in block
    assert "Run install without --data-dir to keep --db /x.db" in block


def test_repeated_data_dir_uses_the_last_and_the_flag_collapses_them(
    tmp_path: Path, capsys
) -> None:
    """argparse keeps the last --data-dir, so the hook does too; the flag leaves one.

    Mutation: read the first --data-dir, or replace only the first. This
    test fails.
    """

    home = tmp_path / "home"
    files = _files(home, "claude-code")
    args = ["alice-memory", "mcp", "--data-dir", "/first", "--data-dir=/second"]
    _seed(files["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": args}}})
    assert _install(capsys, home, "--host", "claude-code")[0] == 0
    assert _claude_hook(home) == [
        "uvx --from alice-memory alice-memory-session-start --data-dir /second"
    ]
    new = (tmp_path / "new").resolve()
    code, out, records = _install(capsys, home, "--host", "claude-code", "--data-dir", str(new))
    assert code == 0, (out, records)
    written = json.loads(files["mcp"].read_text(encoding="utf-8"))["mcpServers"]["alice"]
    assert written["args"] == ["alice-memory", "mcp", "--data-dir", str(new)]
    assert "the entry had 2 --data-dir options; install left one" in out


def test_a_hook_on_another_dir_is_moved_to_the_entrys_and_says_so(
    tmp_path: Path, capsys
) -> None:
    """Mutation: keep the hook's own dir, or drop the receipt line. This test fails."""

    home = tmp_path / "home"
    files = _files(home, "claude-code")
    entry_dir = str((tmp_path / "entry").resolve())
    _seed(files["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": ["alice-memory", "mcp", "--data-dir", entry_dir]}}})
    stale = "uvx --from alice-memory alice-memory-session-start --data-dir /other"
    _seed(files["hooks"], {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": stale}]}]}})
    code, out, records = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, records)
    assert _claude_hook(home) == [
        f"uvx --from alice-memory alice-memory-session-start --data-dir {entry_dir}"
    ]
    assert f"session_start_data_dir: /other -> {entry_dir}" in out


# --- finding 8: receipts per file -------------------------------------------------------


def test_hook_write_failure_after_the_entry_is_written_reports_each_file(
    tmp_path: Path, capsys
) -> None:
    """MCP written, hook failed: the receipt says exactly that, exit install_failed.

    Mutation: report the whole host as action: failed. This test fails.
    """

    home = tmp_path / "home"
    files = _files(home, "claude-code")
    _seed(files["hooks"], {"theme": "dark"})
    hooks_dir = files["hooks"].parent
    hooks_dir.chmod(0o500)
    try:
        code, out, records = _install(capsys, home, "--host", "claude-code", "--data-dir", str(tmp_path / "v"))
    finally:
        hooks_dir.chmod(0o700)
    assert code == 1 and records == [_error("install_failed")]
    block = _block(out, "claude-code")
    assert "action: written" in block
    assert "session_start: failed" in block
    assert f"session_start_file: {files['hooks']}" in block
    assert files["mcp"].is_file()


def test_entry_write_failure_says_the_hook_was_not_attempted(tmp_path: Path, capsys) -> None:
    """Mutation: say session_start: added when nothing was written. This test fails."""

    home = tmp_path / "home"
    files = _files(home, "cursor")
    files["mcp"].parent.mkdir(parents=True)
    files["mcp"].parent.chmod(0o500)
    try:
        code, out, records = _install(capsys, home, "--host", "cursor", "--data-dir", str(tmp_path / "v"))
    finally:
        files["mcp"].parent.chmod(0o700)
    assert code == 1 and records == [_error("install_failed")]
    block = _block(out, "cursor")
    assert "action: failed" in block
    assert "session_start: not attempted" in block


# --- finding 9: compare parsed JSON -----------------------------------------------------


def test_a_reformatted_file_with_nothing_to_change_is_not_rewritten(
    tmp_path: Path, capsys
) -> None:
    """Claude Code rewrites .claude.json in its own layout; no write, no backup.

    Mutation: compare bytes. This test fails.
    """

    home = tmp_path / "home"
    vault = (tmp_path / "v").resolve()
    path = _files(home, "claude-desktop")["mcp"]
    doc = {"zeta": 1, "mcpServers": {"alice": {"command": "uvx", "args": ["alice-memory", "mcp", "--data-dir", str(vault)]}}}
    text = _seed(path, doc, indent=4)
    code, out, records = _install(capsys, home, "--host", "claude-desktop", "--data-dir", str(vault))
    assert code == 0, (out, records)
    assert path.read_text(encoding="utf-8") == text
    assert "action: unchanged" in out
    assert list(path.parent.glob(f"*{HERMES_BACKUP_MARKER}*")) == []


# --- finding 13: dry-run shows only Alice, env values hidden ---------------------------


def test_dry_run_prints_only_the_alice_entry_and_hook_with_env_hidden(
    tmp_path: Path, capsys
) -> None:
    """Other servers' tokens, oauthAccount and alice's env values stay out.

    Mutation: print the whole planned file, or print env values. This test
    fails.
    """

    home = tmp_path / "home"
    secrets = ["sk-other-server-TOKEN", "oauth-SECRET", "sk-alice-env-TOKEN", "hook-SECRET"]
    for host in ("claude-desktop", "claude-code", "cursor", "openclaw"):
        files = _files(home, host)
        entry = {"command": "uvx", "args": ["alice-memory", "mcp"], "env": {"API_KEY": secrets[2]}}
        other = {"command": "node", "args": ["x"], "env": {"TOKEN": secrets[0]}}
        doc: dict = {"oauthAccount": {"token": secrets[1]}}
        if host == "openclaw":
            doc["mcp"] = {"servers": {"other": other, "alice": entry}}
        else:
            doc["mcpServers"] = {"other": other, "alice": entry}
        _seed(files["mcp"], doc)
        if "hooks" in files:
            neighbour = {"type": "command", "command": f"notify --token {secrets[3]}"}
            hooks = (
                {"hooks": {"SessionStart": [{"hooks": [neighbour]}]}}
                if host == "claude-code"
                else {"version": 1, "hooks": {"sessionStart": [{"command": neighbour["command"]}]}}
            )
            _seed(files["hooks"], hooks)

    code, out, records = _install(capsys, home, "--dry-run")
    assert code == 0, (out, records)
    for secret in secrets:
        assert secret not in out, secret
    assert out.count('"API_KEY": "<hidden>"') == 4
    assert "alice-memory-session-start" in _block(out, "cursor")


# --- finding 14: deep nesting is a malformed file --------------------------------------


def test_deeply_nested_json_refuses_only_its_host(tmp_path: Path, capsys) -> None:
    """RecursionError no longer escapes the run.

    Mutation: stop catching RecursionError. This test fails.
    """

    home = tmp_path / "home"
    deep = _files(home, "claude-desktop")["mcp"]
    deep.parent.mkdir(parents=True)
    text = '{"a": ' + "[" * 200_000 + "]" * 200_000 + "}\n"
    deep.write_text(text, encoding="utf-8")
    code, out, records = _install(capsys, home, "--data-dir", str(tmp_path / "v"))
    assert code == 1 and records == [_error("install_refused")]
    assert "nested too deeply" in _block(out, "claude-desktop")
    assert deep.read_text(encoding="utf-8") == text
    assert "action: written" in _block(out, "cursor")


# --- finding 15: symlinks are written through ------------------------------------------


def test_symlinked_host_files_are_written_through(tmp_path: Path, capsys) -> None:
    """The link stays a link; the target is edited; the backup is in the data dir.

    Since review round 4 (tower decision) no backup goes next to the target,
    which can sit in a dotfiles git repo. Mutation: write to the link path.
    This test fails.
    """

    home = tmp_path / "home"
    files = _files(home, "claude-code")
    dotfiles = tmp_path / "dotfiles"
    mcp_target = dotfiles / "claude.json"
    hooks_target = dotfiles / "settings.json"
    _seed(mcp_target, {"theme": "dark"})
    _seed(hooks_target, {"model": "opus"})
    home.mkdir()
    files["mcp"].symlink_to(mcp_target)
    files["hooks"].parent.mkdir()
    files["hooks"].symlink_to(hooks_target)

    vault = tmp_path / "v"
    code, out, records = _install(capsys, home, "--host", "claude-code", "--data-dir", str(vault))
    assert code == 0, (out, records)
    assert files["mcp"].is_symlink() and files["hooks"].is_symlink()
    assert "alice" in json.loads(mcp_target.read_text(encoding="utf-8"))["mcpServers"]
    assert "SessionStart" in json.loads(hooks_target.read_text(encoding="utf-8"))["hooks"]
    assert sorted(item.name for item in dotfiles.iterdir()) == ["claude.json", "settings.json"]
    backups = vault / "backups" / "host-configs"
    assert len(list(backups.glob(f"claude-code-claude.json{HERMES_BACKUP_MARKER}*"))) == 1
    assert len(list(backups.glob(f"claude-code-settings.json{HERMES_BACKUP_MARKER}*"))) == 1
    assert f"target: {mcp_target.resolve()}" in out
    assert f"session_start_target: {hooks_target.resolve()}" in out


@pytest.mark.parametrize("kind", ["dangling", "loop"])
def test_broken_symlinks_refuse_the_host(tmp_path: Path, capsys, kind: str) -> None:
    """Mutation: follow a broken link and create a file. This test fails."""

    home = tmp_path / "home"
    path = _files(home, "claude-desktop")["mcp"]
    path.parent.mkdir(parents=True)
    if kind == "dangling":
        path.symlink_to(tmp_path / "nowhere.json")
    else:
        other = path.parent / "other.json"
        other.symlink_to(path)
        path.symlink_to(other)
    code, out, records = _install(capsys, home, "--host", "claude-desktop", "--data-dir", str(tmp_path / "v"))
    assert code == 1 and records == [_error("install_refused")]
    assert "symbolic link whose target is missing or loops" in out
    assert path.is_symlink()
    assert sorted(os.listdir(path.parent)) == sorted(
        ["claude_desktop_config.json"] + (["other.json"] if kind == "loop" else [])
    )


# --- finding 16: Cursor keeps the user's keys on the Alice item -------------------------


def test_cursor_keeps_extra_keys_on_the_alice_item(tmp_path: Path, capsys) -> None:
    """Mutation: replace the item with a bare {"command": ...}. This test fails."""

    home = tmp_path / "home"
    hooks = _files(home, "cursor")["hooks"]
    item = {
        "command": "uvx --from alice-memory alice-memory-session-start --data-dir /old",
        "timeout": 5,
        "note": "mine",
    }
    _seed(hooks, {"version": 1, "hooks": {"sessionStart": [item]}})
    new = (tmp_path / "new").resolve()
    code, out, records = _install(capsys, home, "--host", "cursor", "--data-dir", str(new))
    assert code == 0, (out, records)
    written = json.loads(hooks.read_text(encoding="utf-8"))["hooks"]["sessionStart"]
    assert written == [
        {
            "command": f"uvx --from alice-memory alice-memory-session-start --data-dir {new}",
            "timeout": 5,
            "note": "mine",
        }
    ]
