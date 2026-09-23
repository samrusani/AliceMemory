"""alice-memory install: review round 6 findings, 2026-09-23.

Why this exists. The round-5 review found two P2s, each by two lenses:

- P2-A: when a hook had to keep its command (a pin below 0.16, index
  credentials, a spec install cannot read), an explicit --data-dir moved
  the MCP entry and left the hook on the old store, with
  ``session_start: already-present`` and exit 0. On Cursor that is a
  silent regression against v0.16.0.
- P2-B: the credential gate cut a URL at a quote or a space, and never
  looked at tokens in the URL path, so such secrets were copied into both
  hook files and printed.

And P3s in the same code: a pin carried in the uvx options (--with,
--exclude-newer) passed the version gate; the hook reader read past
comments and command separators and trusted brace expansion; a dry run
showed VAR=value secrets and other keys of a kept hook.

How they escaped: the round-5 tests kept hooks only on the entry's own
dir, wrote passwords without quotes or spaces, and put credentials only
in user info or a query.

Each test names the edit that makes it fail. No host binary is run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alicebot_api.host_install import host_file_map
from alicebot_api.onramp import _ERROR_CONTRACTS, main as onramp_main

pytestmark = pytest.mark.usefixtures("uvx_on_path")

HOOK = "uvx --from alice-memory alice-memory-session-start --data-dir {dir}"
BLOCKERS = {
    "pin": ["alice-memory==0.15.7", "mcp"],
    "credentials": ["--index-url", "https://me:tok-SECRET@pkgs.example/simple", "alice-memory", "mcp"],
    "unreadable": ["alice-memory===weird", "mcp"],
}


def _install(capsys, home: Path, *extra: str) -> tuple[int, str, str]:
    code = onramp_main(["install", "--home", str(home), *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _files(home: Path, host: str) -> dict[str, Path]:
    return host_file_map(home.resolve())[host]


def _seed(path: Path, doc: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


def _seed_entry(home: Path, host: str, args: list[str]) -> Path:
    path = _files(home, host)["mcp"]
    _seed(path, {"mcpServers": {"alice": {"command": "uvx", "args": args}}})
    return path


def _seed_hook(home: Path, host: str, command: str, extra: dict | None = None) -> str:
    path = _files(home, host)["hooks"]
    item = {"command": command, **(extra or {})}
    if host == "cursor":
        return _seed(path, {"version": 1, "hooks": {"sessionStart": [item]}})
    return _seed(path, {"hooks": {"SessionStart": [{"hooks": [{"type": "command", **item}]}]}})


def _hooks(home: Path, host: str) -> list[str]:
    doc = json.loads(_files(home, host)["hooks"].read_text(encoding="utf-8"))
    if host == "cursor":
        return [item["command"] for item in doc["hooks"]["sessionStart"]]
    return [h["command"] for group in doc["hooks"]["SessionStart"] for h in group["hooks"]]


def _dir(tmp_path: Path, name: str) -> str:
    return str((tmp_path / name).resolve())


# --- P2-A: a kept hook follows the entry's dir, or the host is refused -----------------


@pytest.mark.parametrize("host", ["cursor", "claude-code"])
@pytest.mark.parametrize("blocker", sorted(BLOCKERS))
def test_a_kept_hook_moves_only_its_data_dir_with_the_flag(
    tmp_path: Path, capsys, host: str, blocker: str
) -> None:
    """The hook keeps its launcher text; only --data-dir changes, and the receipt says so.

    Mutation: leave a kept hook's --data-dir where it was (the round-5
    behaviour). A case fails.
    """

    home = tmp_path / "home"
    old, new = _dir(tmp_path, "old"), _dir(tmp_path, "new")
    mcp = _seed_entry(home, host, [*BLOCKERS[blocker], "--data-dir", old])
    _seed_hook(home, host, HOOK.format(dir=old))
    code, out, err = _install(capsys, home, "--host", host, "--data-dir", new)
    assert code == 0, (out, err)
    assert json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]["alice"]["args"][-1] == new
    assert _hooks(home, host) == [HOOK.format(dir=new)]
    assert f"session_start_data_dir: {old} -> {new}" in out
    assert "session_start: updated" in out and "already-present" not in out
    assert "tok-SECRET" not in out + err


@pytest.mark.parametrize("host", ["cursor", "claude-code"])
def test_a_kept_hook_on_another_dir_follows_the_entry_without_the_flag(
    tmp_path: Path, capsys, host: str
) -> None:
    """Dirs that differ are never "already-present": the hook's dir moves to the entry's.

    Mutation: only move a kept hook's dir when --data-dir is passed. This
    test fails.
    """

    home = tmp_path / "home"
    entry_dir, hook_dir = _dir(tmp_path, "entry"), _dir(tmp_path, "hook")
    _seed_entry(home, host, ["alice-memory==0.15.7", "mcp", "--data-dir", entry_dir])
    _seed_hook(home, host, HOOK.format(dir=hook_dir))
    code, out, err = _install(capsys, home, "--host", host)
    assert code == 0, (out, err)
    assert _hooks(home, host) == [HOOK.format(dir=entry_dir)]
    assert "already-present" not in out


@pytest.mark.parametrize("host", ["cursor", "claude-code"])
@pytest.mark.parametrize("blocker", sorted(BLOCKERS))
def test_a_kept_hook_whose_dir_cannot_be_moved_refuses_the_host(
    tmp_path: Path, capsys, host: str, blocker: str
) -> None:
    """``--data-dir $HOME/old`` cannot be rewritten safely: refused, both dirs named, exit 1.

    Mutation: keep the hook silently when its dir is not trusted. A case
    fails.
    """

    home = tmp_path / "home"
    old, new = _dir(tmp_path, "old"), _dir(tmp_path, "new")
    _seed_entry(home, host, [*BLOCKERS[blocker], "--data-dir", old])
    hook = "uvx --from alice-memory alice-memory-session-start --data-dir $HOME/old"
    text = _seed_hook(home, host, hook)
    code, out, err = _install(capsys, home, "--host", host, "--data-dir", new)
    assert code == 1
    assert json.loads(err.strip().splitlines()[-1]) == {
        "error": {"code": "install_refused", "message": _ERROR_CONTRACTS["install_refused"]}
    }
    assert _files(home, host)["hooks"].read_text(encoding="utf-8") == text
    assert "session_start: refused" in out
    assert f"the MCP entry now opens {new}, but the SessionStart hook's --data-dir $HOME/old" in out
    assert f'session_start_argv: ["uvx", "--from", "alice-memory", "alice-memory-session-start", "--data-dir", "{new}"]' in out
    assert "tok-SECRET" not in out + err


# --- P2-B: credentials the round-5 gate missed ------------------------------------------

CREDENTIAL_URLS = {
    "single-quote": "https://me:p'w-SECRET@pkgs.example/simple",
    "double-quote": 'https://me:p"w-SECRET@pkgs.example/simple',
    "space": "https://me:p w-SECRET@pkgs.example/simple",
    "path-token": "https://pkgs.example/simple/a1b2c3d4e5f6a7b8c9d0SECRET1234/",
    "query": "https://pkgs.example/simple?token=q-SECRET",
    "fragment": "https://pkgs.example/simple#f-SECRET",
}


@pytest.mark.parametrize("host", ["cursor", "claude-code"])
@pytest.mark.parametrize("kind", sorted(CREDENTIAL_URLS))
@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
def test_credentials_the_round5_gate_missed_stay_out_of_the_hook_and_the_screen(
    tmp_path: Path, capsys, host: str, kind: str, dry_run: bool
) -> None:
    """No hook is written, and no receipt, warning, launcher or dry-run line shows the secret.

    Mutation: cut the URL at a quote or space, or drop the path, query or
    fragment rule. A case fails.
    """

    home = tmp_path / "home"
    vault = _dir(tmp_path, "vault")
    _seed_entry(home, host, ["--index-url", CREDENTIAL_URLS[kind], "alice-memory", "mcp", "--data-dir", vault])
    extra = ("--dry-run",) if dry_run else ()
    code, out, err = _install(capsys, home, "--host", host, *extra)
    assert code == 0, (out, err)
    assert not _files(home, host)["hooks"].exists()
    assert "SECRET" not in out + err
    assert "install never writes a URL into a hook file" in out


# --- P3: pins carried in the uvx options --------------------------------------------------


@pytest.mark.parametrize(
    "options",
    [["--with", "alice-memory==0.15.7"], ["--with", "alice-memory<0.16"], ["--exclude-newer", "2025-01-01"]],
    ids=["with-pin", "with-range", "exclude-newer"],
)
@pytest.mark.parametrize("host", ["cursor", "claude-code"])
def test_a_pin_in_the_uvx_options_keeps_the_hook_and_warns(
    tmp_path: Path, capsys, options: list[str], host: str
) -> None:
    """Mutation: skip the uvx options in the version gate. A case fails."""

    home = tmp_path / "home"
    vault = _dir(tmp_path, "vault")
    _seed_entry(home, host, [*options, "alice-memory", "mcp", "--data-dir", vault])
    old = HOOK.format(dir=vault)
    _seed_hook(home, host, old)
    code, out, err = _install(capsys, home, "--host", host)
    assert code == 0, (out, err)
    assert _hooks(home, host) == [old]
    assert "which install does not carry into a hook" in out


# --- P3: a dry run hides secrets inside a kept Alice hook ---------------------------------


def test_a_dry_run_hides_assignments_and_other_keys_of_a_kept_hook(tmp_path: Path, capsys) -> None:
    """``ALICE_TOKEN=... uvx ...`` and a hook item's own keys come from the user's file.

    Mutation: print the hook item's other keys, or its leading assignments.
    This test fails.
    """

    home = tmp_path / "home"
    vault = _dir(tmp_path, "vault")
    _seed_entry(home, "cursor", ["alice-memory==0.15.7", "mcp", "--data-dir", vault])
    command = f"ALICE_TOKEN=a-SECRET uvx --from alice-memory alice-memory-session-start --data-dir {vault}"
    _seed_hook(home, "cursor", command, {"env": {"K": "e-SECRET"}, "note": "n-SECRET"})
    code, out, err = _install(capsys, home, "--host", "cursor", "--dry-run")
    assert code == 0, (out, err)
    assert "SECRET" not in out + err
    # Since round 8 a word off the allowlist is hidden whole, name included.
    assert "<hidden> uvx --from alice-memory alice-memory-session-start --data-dir" in out
    assert "hook command (1 word install does not print), hook env, hook note" in out


# --- every receipt line passes through mask_text ------------------------------------------


def test_a_warning_quoting_a_hooks_url_data_dir_is_masked(tmp_path: Path, capsys) -> None:
    """A free-text line that quotes the user's hook: the receipt-wide mask catches it.

    Mutation: stop passing receipt lines through mask_text. This test fails.
    """

    home = tmp_path / "home"
    vault = _dir(tmp_path, "vault")
    _seed_entry(home, "cursor", ["alice-memory", "mcp", "--data-dir", vault])
    _seed_hook(home, "cursor", "uvx --from alice-memory alice-memory-session-start --data-dir https://me:tok-SECRET@h.example/x")
    code, out, err = _install(capsys, home, "--host", "cursor")
    assert code == 0, (out, err)
    assert "tok-SECRET" not in out + err
    assert "--data-dir https://<hidden> could not be relied on" in out


# --- P3: the foreign-entry reason line masks a URL command ----------------------------------


@pytest.mark.parametrize(
    "command",
    ["https://me:p'w-SECRET@mcp.example/alice", "https://me:p w-SECRET@mcp.example/alice"],
    ids=["quote", "space"],
)
def test_a_foreign_url_command_with_a_quoted_password_is_masked(
    tmp_path: Path, capsys, command: str
) -> None:
    """The command is one word to install, whatever it holds, so it is masked as one.

    The receipt-wide mask splits text at whitespace, so a space in the URL
    needs the source masking. Mutation: quote a foreign entry's command raw
    in the reason line. The space case fails.
    """

    home = tmp_path / "home"
    path = _files(home, "claude-desktop")["mcp"]
    _seed(path, {"mcpServers": {"alice": {"command": command, "args": []}}})
    code, out, err = _install(capsys, home, "--host", "claude-desktop")
    assert code == 1
    assert "SECRET" not in out + err
