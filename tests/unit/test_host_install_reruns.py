"""alice-memory install re-runs on JSON hosts: keep what the user set, stop per host.

Why this exists. Review round 2 of the S4.1 hook fix, 2026-09-22, found that
re-running install rewrote the whole ``alice`` entry in every JSON host
(Claude Desktop, Claude Code, Cursor, OpenClaw). A user's env, type,
timeout or absolute uvx path was dropped, and because --data-dir defaulted
to ~/.alice, a re-run without the flag re-pointed a custom vault at an
empty one, and pointed the newly repaired Claude Code hook there too. An
entry install never wrote (the documented Postgres entry) was overwritten
the same way. One unreadable host file aborted the run with empty stdout
after other files were already written, so receipts, including a Hermes
refusal snippet, were lost.

How it escaped. Every writer test started from an empty home or from an
entry install had just written, so no test held user keys, a custom data
dir without the flag, a foreign entry, or a broken file next to good ones.

These tests pin the decisions: an install-shaped entry keeps every key and
its data dir unless --data-dir is passed; any other alice entry refuses that
host with the file byte-identical; the hook follows the same data dir; a
JSON file is backed up before it is rewritten; a malformed file refuses its
host and a failing file fails its host, while the other hosts still run and
every receipt prints.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alicebot_api.host_install import (
    HERMES_BACKUP_MARKER,
    host_file_map,
    mcp_server_payload,
)
from alicebot_api.onramp import _ERROR_CONTRACTS, main as onramp_main

JSON_HOSTS = ("claude-desktop", "claude-code", "cursor", "openclaw")
HOOK_HOSTS = ("claude-code", "cursor")
POSTGRES_ENTRY = {
    "command": "/ABSOLUTE/PATH/TO/AliceBot/.venv/bin/python",
    "args": ["-m", "alicebot_api.mcp_server"],
    "cwd": "/ABSOLUTE/PATH/TO/AliceBot",
    "env": {
        "DATABASE_URL": "postgresql://alicebot_app:alicebot_app@localhost:5432/alicebot",
        "ALICEBOT_AUTH_USER_ID": "00000000-0000-0000-0000-000000000001",
    },
}
USER_ENTRY = {
    "command": "/opt/homebrew/bin/uvx",
    "args": ["alice-memory==0.16.0", "mcp", "--data-dir", "/custom/vault"],
    "env": {"ALICE_MCP_FULL_TOOLS": "1", "EXTRA_FLAG": "on"},
    "type": "stdio",
    "timeout": 30,
}


def _error(code: str) -> dict:
    return {"error": {"code": code, "message": _ERROR_CONTRACTS[code]}}


def _install(capsys, home: Path, *extra: str) -> tuple[int, str, list[object]]:
    code = onramp_main(["install", "--home", str(home), *extra])
    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.err.splitlines() if line.startswith("{")]
    return code, captured.out, records


def _dump(doc: object) -> str:
    return json.dumps(doc, ensure_ascii=True, indent=2) + "\n"


def _files(home: Path, host: str) -> dict[str, Path]:
    return host_file_map(home.resolve())[host]


def _with_alice(host: str, entry: object) -> dict:
    """A host file holding ``entry`` as alice next to one other server."""

    other = {"command": "node", "args": ["other.js"]}
    if host == "openclaw":
        return {"mcp": {"servers": {"other": other, "alice": entry}}}
    return {"mcpServers": {"other": other, "alice": entry}}


def _alice(host: str, doc: dict) -> object:
    if host == "openclaw":
        return doc["mcp"]["servers"]["alice"]
    return doc["mcpServers"]["alice"]


def _seed(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _block(out: str, host: str) -> str:
    """The receipt block for ``host``."""

    for block in out.split("\n\n"):
        if block.startswith(f"host: {host}\n"):
            return block
    raise AssertionError(f"no receipt for {host}:\n{out}")


def _hook_commands(home: Path, host: str) -> list[str]:
    doc = json.loads(_files(home, host)["hooks"].read_text(encoding="utf-8"))
    if host == "cursor":
        return [item["command"] for item in doc["hooks"]["sessionStart"]]
    return [
        handler["command"]
        for group in doc["hooks"]["SessionStart"]
        for handler in group["hooks"]
    ]


@pytest.mark.parametrize("host", JSON_HOSTS)
def test_rerun_without_flag_keeps_every_user_key_and_the_data_dir(
    tmp_path: Path, capsys, host: str
) -> None:
    """env, type, timeout, a pin and an absolute uvx path all survive a re-run.

    No --data-dir, so the entry's own /custom/vault stays. The file was
    already in install's format, so nothing is rewritten and no backup is
    taken. Mutation: rebuild the entry from mcp_server_payload, or default
    the data dir to ~/.alice. This test fails.
    """

    home = tmp_path / "home"
    mcp_path = _files(home, host)["mcp"]
    original = _dump(_with_alice(host, USER_ENTRY))
    _seed(mcp_path, original)

    code, out, records = _install(capsys, home, "--host", host)
    assert code == 0, (out, records)
    assert mcp_path.read_text(encoding="utf-8") == original
    block = _block(out, host)
    assert "action: unchanged" in block
    assert "kept: command, env (2 keys), type, timeout" in block
    assert HERMES_BACKUP_MARKER not in block
    if host in HOOK_HOSTS:
        assert _hook_commands(home, host) == [
            "uvx --from alice-memory alice-memory-session-start --data-dir /custom/vault"
        ]


@pytest.mark.parametrize("host", JSON_HOSTS)
def test_rerun_with_flag_rewrites_only_the_data_dir_and_backs_up_first(
    tmp_path: Path, capsys, host: str
) -> None:
    """An explicit --data-dir changes that one arg; the old file is backed up.

    Mutation: replace the whole args list, drop a user key, or skip the
    backup. This test fails.
    """

    home = tmp_path / "home"
    new_vault = (tmp_path / "new-vault").resolve()
    mcp_path = _files(home, host)["mcp"]
    original = _dump(_with_alice(host, USER_ENTRY))
    _seed(mcp_path, original)

    code, out, records = _install(capsys, home, "--host", host, "--data-dir", str(new_vault))
    assert code == 0, (out, records)
    written = json.loads(mcp_path.read_text(encoding="utf-8"))
    expected = dict(USER_ENTRY)
    expected["args"] = ["alice-memory==0.16.0", "mcp", "--data-dir", str(new_vault)]
    assert _alice(host, written) == expected
    block = _block(out, host)
    assert f"data_dir: /custom/vault -> {new_vault}" in block
    backups = sorted(mcp_path.parent.glob(f"{mcp_path.name}{HERMES_BACKUP_MARKER}*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    assert f"backup: {backups[0]}" in block


@pytest.mark.parametrize("host", HOOK_HOSTS)
def test_custom_data_dir_survives_a_rerun_without_the_flag(
    tmp_path: Path, capsys, host: str
) -> None:
    """install --data-dir X, then plain install: MCP entry and hook stay on X.

    Mutation: resolve a missing flag to ~/.alice. This test fails.
    """

    home = tmp_path / "home"
    vault = (tmp_path / "custom").resolve()
    assert _install(capsys, home, "--host", host, "--data-dir", str(vault))[0] == 0
    code, out, records = _install(capsys, home, "--host", host)
    assert code == 0, (out, records)
    mcp = json.loads(_files(home, host)["mcp"].read_text(encoding="utf-8"))
    assert _alice(host, mcp)["args"][-1] == str(vault)
    assert _hook_commands(home, host) == [
        f"uvx --from alice-memory alice-memory-session-start --data-dir {vault}"
    ]
    assert "session_start: already-present" in _block(out, host)


@pytest.mark.parametrize("host", HOOK_HOSTS)
def test_hook_data_dir_seeds_a_missing_mcp_entry(tmp_path: Path, capsys, host: str) -> None:
    """No MCP entry but an Alice hook on X: the new MCP entry uses X too.

    The order is --data-dir, then the MCP entry, then the hook, then
    ~/.alice. Mutation: skip the hook's data dir. This test fails.
    """

    home = tmp_path / "home"
    hooks_path = _files(home, host)["hooks"]
    command = "uvx --from alice-memory alice-memory-session-start --data-dir /hook/vault"
    if host == "cursor":
        seed = {"version": 1, "hooks": {"sessionStart": [{"command": command}]}}
    else:
        seed = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}
    _seed(hooks_path, _dump(seed))

    code, out, records = _install(capsys, home, "--host", host)
    assert code == 0, (out, records)
    mcp = json.loads(_files(home, host)["mcp"].read_text(encoding="utf-8"))
    assert _alice(host, mcp) == mcp_server_payload("/hook/vault", with_env=False)


@pytest.mark.parametrize("host", JSON_HOSTS)
def test_documented_postgres_entry_is_refused_byte_identical(
    tmp_path: Path, capsys, host: str
) -> None:
    """An alice entry install did not write is left alone and the host refused.

    The receipt names the reason and prints the entry install would write.
    For a hook host with no Alice hook, no hook is added. Mutation: treat
    any alice entry as install's, or add a hook anyway. This test fails.
    """

    home = tmp_path / "home"
    mcp_path = _files(home, host)["mcp"]
    original = _dump(_with_alice(host, POSTGRES_ENTRY))
    _seed(mcp_path, original)

    code, out, records = _install(capsys, home, "--host", host)
    assert code == 1
    assert records == [_error("install_refused")]
    assert mcp_path.read_bytes() == original.encode("utf-8")
    assert sorted(path.name for path in mcp_path.parent.iterdir() if path.name.startswith(mcp_path.name)) == [
        mcp_path.name
    ]
    block = _block(out, host)
    assert "action: refused" in block
    assert "exists and install did not write it" in block
    snippet = block.split("snippet:\n", 1)[1].split("\nnext:", 1)[0]
    assert _alice(host, json.loads(snippet))["command"] == "uvx"
    if host in HOOK_HOSTS:
        assert not _files(home, host)["hooks"].exists()
        assert "session_start: none" in block


def test_foreign_entry_still_repairs_an_existing_hook_with_its_own_data_dir(
    tmp_path: Path, capsys
) -> None:
    """Claude Code: Postgres entry refused, v0.16.0 flat hook repaired, dir kept.

    --data-dir is passed and still does not move the hook: the MCP entry that
    would have used it was refused. Mutation: point the repaired hook at the
    flag. This test fails.
    """

    home = tmp_path / "home"
    files = _files(home, "claude-code")
    _seed(files["mcp"], _dump(_with_alice("claude-code", POSTGRES_ENTRY)))
    command = "uvx --from alice-memory alice-memory-session-start --data-dir /hook/vault"
    _seed(files["hooks"], _dump({"hooks": {"SessionStart": [{"command": command}]}}))

    code, out, records = _install(capsys, home, "--host", "claude-code", "--data-dir", str(tmp_path / "flag"))
    assert code == 1 and records == [_error("install_refused")]
    settings = json.loads(files["hooks"].read_text(encoding="utf-8"))
    assert settings["hooks"]["SessionStart"] == [
        {"hooks": [{"type": "command", "command": command}]}
    ]
    assert "session_start: updated" in _block(out, "claude-code")


def test_broken_json_file_refuses_only_its_host(tmp_path: Path, capsys) -> None:
    """A .cursor/mcp.json that does not parse refuses Cursor; the rest are written.

    Until 2026-09-22 this aborted with empty stdout after three other hosts'
    files were already written. Mutation: let the parse error escape the
    host. This test fails.
    """

    home = tmp_path / "home"
    cursor = _files(home, "cursor")
    _seed(cursor["mcp"], "{not json")

    code, out, records = _install(capsys, home, "--data-dir", str(tmp_path / "vault"))
    assert code == 1
    assert records == [_error("install_refused")]
    assert cursor["mcp"].read_text(encoding="utf-8") == "{not json"
    assert not cursor["hooks"].exists()
    block = _block(out, "cursor")
    assert "action: refused" in block
    assert f"file: {cursor['mcp']}" in block
    assert "the file is not valid JSON" in block
    for host in ("claude-desktop", "claude-code", "openclaw"):
        assert "action: written" in _block(out, host)
        assert _files(home, host)["mcp"].is_file()


@pytest.mark.parametrize(
    ("label", "settings", "reason"),
    [
        ("hooks not an object", {"hooks": []}, "hooks is not an object"),
        ("SessionStart not a list", {"hooks": {"SessionStart": {}}}, "hooks.SessionStart is not a list"),
    ],
)
def test_malformed_hooks_file_refuses_the_whole_host(
    tmp_path: Path, capsys, label: str, settings: dict, reason: str
) -> None:
    """A malformed settings.json refuses Claude Code; neither file is written.

    Mutation: write the MCP file before checking the hooks file. This test
    fails.
    """

    home = tmp_path / "home"
    files = _files(home, "claude-code")
    text = _dump(settings)
    _seed(files["hooks"], text)

    code, out, records = _install(capsys, home, "--host", "claude-code")
    assert code == 1 and records == [_error("install_refused")], label
    assert files["hooks"].read_text(encoding="utf-8") == text
    assert not files["mcp"].exists()
    block = _block(out, "claude-code")
    assert reason in block
    assert f"file: {files['hooks']}" in block


def test_a_failing_host_keeps_every_receipt_and_reports_install_failed(
    tmp_path: Path, capsys
) -> None:
    """Hermes refused, then Cursor fails, then OpenClaw writes: all three print.

    Cursor's mcp.json is a directory, so reading it raises. The receipt
    names the file with a static reason and no exception text. The Hermes
    snippet survives, and install_failed wins over install_refused.
    Mutation: let the OSError escape the host. This test fails.
    """

    home = tmp_path / "home"
    hermes_config = home.resolve() / ".hermes" / "config.yaml"
    _seed(hermes_config, "mcp_servers:\n  other:\n    args: [\n      a.js\n    ]\n")
    _files(home, "cursor")["mcp"].mkdir(parents=True)

    code, out, records = _install(
        capsys,
        home,
        "--host",
        "hermes",
        "--host",
        "cursor",
        "--host",
        "openclaw",
        "--data-dir",
        str(tmp_path / "vault"),
    )
    assert code == 1
    assert records == [_error("install_failed")]
    hermes = _block(out, "hermes")
    assert "action: refused" in hermes and "snippet:\nmcp_servers:\n  alice:" in hermes
    cursor = _block(out, "cursor")
    assert "action: failed" in cursor
    assert f"file: {_files(home, 'cursor')['mcp']}" in cursor
    assert "reason: the file could not be read or written" in cursor
    assert "Errno" not in out and "Is a directory" not in out
    assert "action: written" in _block(out, "openclaw")


def test_dry_run_refusal_says_would_refuse_and_attempts_nothing(
    tmp_path: Path, capsys
) -> None:
    """A dry run over a foreign entry and an unsafe Hermes file: would-refuse.

    Exit 1 with install_refused, as a real run would, and nothing written.
    Mutation: word it as refused, or exit 0. This test fails.
    """

    home = tmp_path / "home"
    desktop = _files(home, "claude-desktop")["mcp"]
    original = _dump(_with_alice("claude-desktop", POSTGRES_ENTRY))
    _seed(desktop, original)
    hermes_config = home.resolve() / ".hermes" / "config.yaml"
    hermes_text = "mcp_servers:\n  foo:\tbar\n"
    _seed(hermes_config, hermes_text)

    code, out, records = _install(
        capsys, home, "--host", "claude-desktop", "--host", "hermes", "--dry-run"
    )
    assert code == 1
    assert records == [_error("install_refused")]
    for host in ("claude-desktop", "hermes"):
        block = _block(out, host)
        assert "action: would-refuse" in block
        assert block.rstrip().endswith(
            "dry run: install would refuse this file; nothing was attempted"
        ), block
    assert desktop.read_text(encoding="utf-8") == original
    assert hermes_config.read_text(encoding="utf-8") == hermes_text
    assert sorted(path.name for path in desktop.parent.iterdir()) == [desktop.name]


@pytest.mark.parametrize("with_flat_entry", [False, True], ids=["alone", "next-to-a-flat-entry"])
def test_malformed_alice_group_is_left_alone(
    tmp_path: Path, capsys, with_flat_entry: bool
) -> None:
    """A group whose ``hooks`` is an object is not read or changed.

    Decision for review finding 9: install does not try to repair it, and
    the claim is narrowed to "duplicate Alice entries in well-formed groups
    are removed". Claude Code ignores such a group. Alone, a valid Alice
    group is appended after it and the receipt says added. Next to a
    v0.16.0 flat entry, the flat entry is repaired in place, the malformed
    group stays where it was, and the receipt says updated. Mutation: treat
    the malformed group as Alice's, or drop it while rebuilding the list.
    This test fails.
    """

    home = tmp_path / "home"
    hooks_path = _files(home, "claude-code")["hooks"]
    malformed = {
        "hooks": {
            "type": "command",
            "command": "uvx --from alice-memory alice-memory-session-start --data-dir /x",
        }
    }
    flat = {"command": "uvx --from alice-memory alice-memory-session-start --data-dir /old"}
    seed = [malformed, flat] if with_flat_entry else [malformed]
    _seed(hooks_path, _dump({"hooks": {"SessionStart": seed}}))
    vault = (tmp_path / "vault").resolve()

    code, out, records = _install(capsys, home, "--host", "claude-code", "--data-dir", str(vault))
    assert code == 0, (out, records)
    settings = json.loads(hooks_path.read_text(encoding="utf-8"))
    alice_group = {
        "hooks": [
            {
                "type": "command",
                "command": f"uvx --from alice-memory alice-memory-session-start --data-dir {vault}",
            }
        ]
    }
    assert settings["hooks"]["SessionStart"] == [malformed, alice_group]
    expected = "updated" if with_flat_entry else "added"
    assert f"session_start: {expected}" in _block(out, "claude-code")


@pytest.mark.parametrize("host", JSON_HOSTS)
def test_bare_data_dir_at_the_end_of_args_gets_one_value(
    tmp_path: Path, capsys, host: str
) -> None:
    """An install-shaped entry whose args end in a bare --data-dir gets the value.

    Found on 2026-09-22 by the Hermes fuzz pass, which shares the helper:
    the args became [..., "--data-dir", "--data-dir", dir]. Mutation: append
    the pair whenever --data-dir has no value after it. This test fails.
    """

    home = tmp_path / "home"
    vault = (tmp_path / "vault").resolve()
    mcp_path = _files(home, host)["mcp"]
    entry = {"command": "uvx", "args": ["alice-memory", "mcp", "--data-dir"]}
    _seed(mcp_path, _dump(_with_alice(host, entry)))

    code, out, records = _install(capsys, home, "--host", host, "--data-dir", str(vault))
    assert code == 0, (out, records)
    written = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert _alice(host, written)["args"] == ["alice-memory", "mcp", "--data-dir", str(vault)]
