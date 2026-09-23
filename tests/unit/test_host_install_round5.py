"""alice-memory install: review round 5 findings, 2026-09-23.

Why this exists. The round-4 review ran install on v0.16.0 and on this
branch over 16 realistic starting states. Four findings survived:

- P2-1: a uvx entry pinned below 0.16 got a hook asking for
  alice-memory-session-start, which first shipped in 0.16.0, so a working
  unpinned hook on Cursor became a failing one, silently.
- P2-2: install's own single-quoted ``$`` or ``%`` counted as shell
  expansion, and a hand-written ``--data-dir "/Users/me/My Vault"`` hook
  was overwritten onto ~/.alice because it did not round-trip through
  shlex.join.
- P2-3: credentials in the entry's uvx options (--index-url
  https://user:token@...) were copied into the hook file and printed.
- P3s: the docs placeholder data dir was kept; a backup directory that
  could not be created was reported as the host file; pastes hid only
  env, headers and url; the cache rule matched any archive-vN folder; the
  sibling rule applied to hosts with no hook; a user's backups dir was
  chmodded; three PowerShell quotes were missing; and nothing pinned the
  resolve_db_path half of S1.

How they escaped: the round-4 tests pinned no entry below 0.16, used no
index credentials, wrote hand-made hooks only with characters that do
round-trip, and built caches only under a folder named uv.

Each test names the edit that makes it fail. No host binary is run.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from alicebot_api import host_launcher
from alicebot_api.host_install import HERMES_BACKUP_MARKER, host_file_map
from alicebot_api.onramp import main as onramp_main
from tests.unit.launcher_helpers import executable, make_scripts, pin_launcher_search

pytestmark = pytest.mark.usefixtures("uvx_on_path")

OLD_HOOK = "uvx --from alice-memory alice-memory-session-start --data-dir {vault}"


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


def _alice(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["alice"]


def _hooks(home: Path, host: str) -> list[str]:
    doc = json.loads(_files(home, host)["hooks"].read_text(encoding="utf-8"))
    if host == "cursor":
        return [item["command"] for item in doc["hooks"]["sessionStart"]]
    return [h["command"] for group in doc["hooks"]["SessionStart"] for h in group["hooks"]]


def _seed_hook(home: Path, host: str, command: str, *, flat: bool = False) -> None:
    path = _files(home, host)["hooks"]
    if host == "cursor":
        _seed(path, {"version": 1, "hooks": {"sessionStart": [{"command": command}]}})
    elif flat:
        _seed(path, {"hooks": {"SessionStart": [{"command": command}]}})
    else:
        _seed(path, {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}})


def _vault(tmp_path: Path, name: str = "vault") -> str:
    return str((tmp_path / name).resolve())


# --- P2-1: a spec that cannot reach 0.16 gets no hook ----------------------------------

PINS_BELOW = ["alice-memory==0.15.7", "alice-memory@0.15.3", "alice-memory<0.16", "alice-memory~=0.15.0"]


@pytest.mark.parametrize("host", ["cursor", "claude-code"])
@pytest.mark.parametrize("spec", PINS_BELOW)
def test_a_pin_below_016_keeps_a_working_hook_as_it_was(
    tmp_path: Path, capsys, host: str, spec: str
) -> None:
    """The existing unpinned hook stays; install warns about the pin.

    Mutation: build the hook from the pinned spec anyway (drop the version
    check). A case fails.
    """

    home = tmp_path / "home"
    vault = _vault(tmp_path)
    _seed(_files(home, host)["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": [spec, "mcp", "--data-dir", vault]}}})
    old = OLD_HOOK.format(vault=vault)
    _seed_hook(home, host, old)
    code, out, err = _install(capsys, home, "--host", host)
    assert code == 0, (out, err)
    assert _hooks(home, host) == [old]
    assert f"{spec} has no alice-memory-session-start (it first shipped in alice-memory 0.16.0)" in out
    assert "pin alice-memory>=0.16 or remove the pin" in out


@pytest.mark.parametrize("host", ["cursor", "claude-code"])
@pytest.mark.parametrize("spec", PINS_BELOW)
def test_a_pin_below_016_gets_no_new_hook(tmp_path: Path, capsys, host: str, spec: str) -> None:
    """Mutation: add the hook when none exists. A case fails."""

    home = tmp_path / "home"
    _seed(_files(home, host)["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": [spec, "mcp", "--data-dir", _vault(tmp_path)]}}})
    code, out, err = _install(capsys, home, "--host", host)
    assert code == 0, (out, err)
    assert not _files(home, host)["hooks"].exists()
    assert "session_start: skipped" in out and "install added no SessionStart hook" in out


def test_a_flat_v0160_hook_beside_an_old_pin_is_repaired_but_keeps_its_text(
    tmp_path: Path, capsys
) -> None:
    """Shape repair still happens on Claude Code; the command stays the user's.

    Mutation: skip the merge when the launcher is blocked. This test fails.
    """

    home = tmp_path / "home"
    vault = _vault(tmp_path)
    _seed(_files(home, "claude-code")["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": ["alice-memory==0.15.7", "mcp", "--data-dir", vault]}}})
    old = OLD_HOOK.format(vault=vault)
    _seed_hook(home, "claude-code", old, flat=True)
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    doc = json.loads(_files(home, "claude-code")["hooks"].read_text(encoding="utf-8"))
    assert doc["hooks"]["SessionStart"] == [{"hooks": [{"type": "command", "command": old}]}]


@pytest.mark.parametrize(
    ("spec", "written"),
    [("alice-memory>=0.16", True), ("alice-memory~=0.15", True), ("alice-memory===weird", False)],
    ids=["reaches-016", "compatible-0.x", "cannot-tell"],
)
def test_specs_that_reach_016_get_the_hook_and_unreadable_ones_are_left(
    tmp_path: Path, capsys, spec: str, written: bool
) -> None:
    """Control: >=0.16 and ~=0.15 (< 1.0) can reach 0.16. ``===weird`` cannot be read.

    Mutation: answer "no" for anything with a version, or "yes" for a spec
    that cannot be read. A case fails.
    """

    home = tmp_path / "home"
    vault = _vault(tmp_path)
    _seed(_files(home, "cursor")["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": [spec, "mcp", "--data-dir", vault]}}})
    code, out, err = _install(capsys, home, "--host", "cursor")
    assert code == 0, (out, err)
    if written:
        # > and ~ are shell characters, so the spec is single-quoted in the hook.
        assert _hooks(home, "cursor") == [f"uvx --from '{spec}' alice-memory-session-start --data-dir {vault}"]
    else:
        assert not _files(home, "cursor")["hooks"].exists()
        assert f"install cannot tell whether {spec} has alice-memory-session-start" in out


# --- P2-2: the shell's reading decides, not the characters -------------------------------


def test_a_hand_written_double_quoted_dir_seeds_the_new_entry(tmp_path: Path, capsys) -> None:
    """``--data-dir "/…/My Vault"`` is literal to the shell; the new entry takes it.

    Round 4 put the entry on ~/.alice because the text did not round-trip
    through shlex.join. Mutation: treat a double-quoted word as not literal.
    This test fails.
    """

    home = tmp_path / "home"
    vault = _vault(tmp_path, "My Vault")
    _seed_hook(home, "claude-code", f'uvx --from alice-memory alice-memory-session-start --data-dir "{vault}"')
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert _alice(_files(home, "claude-code")["mcp"])["args"] == ["alice-memory", "mcp", "--data-dir", vault]
    assert host_launcher.read_hook_data_dir(_hooks(home, "claude-code")[0]).raw == vault
    assert "warning" not in out


def test_installs_own_quoted_dollar_and_percent_are_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """install writes ``'…/a$b%c'``; on a re-run that hook follows the entry's new launcher.

    Round 4 read the $ as shell expansion and kept the old command, so the
    hook stayed on a dead launcher. Mutation: judge by characters, not by
    quoting. This test fails.
    """

    scripts = make_scripts(tmp_path / "venv" / "bin")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=scripts)
    home = tmp_path / "home"
    vault = _vault(tmp_path, "a$b%c")
    assert _install(capsys, home, "--host", "claude-code", "--data-dir", vault)[0] == 0
    assert "'" in _hooks(home, "claude-code")[0]
    # The scripts move; the next run finds uvx instead.
    for name in ("alice-memory", "alice-memory-session-start"):
        (scripts / name).unlink()
    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert _hooks(home, "claude-code") == [f"uvx --from alice-memory alice-memory-session-start --data-dir '{vault}'"]
    assert "not read literally" not in out


def test_a_kept_hook_beside_a_replaced_launcher_is_named_in_the_receipt(
    tmp_path: Path, capsys
) -> None:
    """A $HOME hook keeps its text; the entry's dead launcher is replaced; the receipt says both.

    Mutation: drop the kept-hook line. This test fails.
    """

    home = tmp_path / "home"
    vault = _vault(tmp_path)
    dead = str(tmp_path / "gone" / "bin" / "alice-memory")
    _seed(_files(home, "cursor")["mcp"], {"mcpServers": {"alice": {"command": dead, "args": ["mcp", "--data-dir", vault]}}})
    old = f"{tmp_path / 'gone' / 'bin' / 'alice-memory-session-start'} --data-dir $HOME/.alice"
    _seed_hook(home, "cursor", old)
    code, out, err = _install(capsys, home, "--host", "cursor")
    assert code == 0, (out, err)
    assert _hooks(home, "cursor") == [old]
    assert (
        f"warning: the MCP entry's launcher changed from {dead} mcp to uvx alice-memory mcp, "
        "but the SessionStart hook kept its own command, which may still run the old one"
    ) in out


# --- P2-3: no credentials copied into the hook file, none printed -----------------------

CREDENTIAL_OPTIONS = [
    ["--index-url", "https://user:tok-SECRET@pkgs.example/simple"],
    ["-i", "https://user:tok-SECRET@pkgs.example/simple"],
    ["--extra-index-url=https://user:tok-SECRET@pkgs.example/simple"],
    ["--default-index", "https://pkgs.example/simple?token=tok-SECRET"],
    ["--index", "corp=https://user:tok-SECRET@pkgs.example/simple"],
    ["--find-links", "https://files.example/wheels?sig=tok-SECRET"],
]
CREDENTIAL_IDS = ["index-url", "i", "extra-index-url-equals", "default-index-query", "index-named", "find-links-query"]


@pytest.mark.parametrize("options", CREDENTIAL_OPTIONS, ids=CREDENTIAL_IDS)
def test_credentials_in_uvx_options_never_reach_the_hook_file_or_the_screen(
    tmp_path: Path, capsys, options: list[str]
) -> None:
    """No hook is added, and the secret is not printed anywhere.

    Mutation: drop the credentials check, or print launcher lines unmasked.
    A case fails.
    """

    home = tmp_path / "home"
    vault = _vault(tmp_path)
    _seed(_files(home, "cursor")["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": [*options, "alice-memory", "mcp", "--data-dir", vault]}}})
    code, out, err = _install(capsys, home, "--host", "cursor")
    assert code == 0, (out, err)
    assert not _files(home, "cursor")["hooks"].exists()
    assert "tok-SECRET" not in out + err
    assert "install never writes a URL into a hook file" in out
    assert "~/.config/uv/uv.toml as [[index]]" in out
    assert "<hidden>" in out


def test_credentials_keep_an_existing_hooks_text(tmp_path: Path, capsys) -> None:
    """Mutation: rewrite the existing hook with the credentialed launcher. This test fails."""

    home = tmp_path / "home"
    vault = _vault(tmp_path)
    args = ["--index-url", "https://user:tok-SECRET@pkgs.example/simple", "alice-memory", "mcp", "--data-dir", vault]
    _seed(_files(home, "claude-code")["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": args}}})
    old = OLD_HOOK.format(vault=vault)
    _seed_hook(home, "claude-code", old)
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert _hooks(home, "claude-code") == [old]
    assert "tok-SECRET" not in _files(home, "claude-code")["hooks"].read_text(encoding="utf-8")
    assert "tok-SECRET" not in out + err


@pytest.mark.parametrize("extra", [(), ("--dry-run",)], ids=["real", "dry-run"])
def test_credentials_in_a_dry_run_and_the_openclaw_line_are_hidden(
    tmp_path: Path, capsys, extra: tuple[str, ...]
) -> None:
    """The openclaw add line shows <hidden> and says so.

    Mutation: print the openclaw line from the raw launcher. This test fails.
    """

    home = tmp_path / "home"
    vault = _vault(tmp_path)
    args = ["--index-url", "https://user:tok-SECRET@pkgs.example/simple", "alice-memory", "mcp", "--data-dir", vault]
    _seed(_files(home, "openclaw")["mcp"], {"mcp": {"servers": {"alice": {"command": "uvx", "args": args}}}})
    code, out, err = _install(capsys, home, "--host", "openclaw", *extra)
    assert code == 0, (out, err)
    assert "tok-SECRET" not in out + err
    assert "--arg 'https://<hidden>'" in out
    assert "put them back before running it" in out


def test_every_receipt_line_hides_url_credentials(tmp_path: Path, capsys) -> None:
    """Not only launcher lines: a reason line quoting a foreign entry's command is masked too.

    Mutation: stop passing receipt lines through mask_text. This test fails.
    """

    home = tmp_path / "home"
    entry = {"command": "https://user:tok-SECRET@mcp.example/alice?key=q-SECRET", "args": []}
    _seed(_files(home, "claude-desktop")["mcp"], {"mcpServers": {"alice": entry}})
    code, out, err = _install(capsys, home, "--host", "claude-desktop")
    assert code == 1
    assert "SECRET" not in out + err
    assert "its command is https://<hidden>" in out


def test_a_dry_run_hides_credentials_in_an_existing_hook(tmp_path: Path, capsys) -> None:
    """A hook written with credentials (by round 4, or by hand) is printed masked.

    Mutation: print the hook items raw in the dry-run snippet. This test fails.
    """

    home = tmp_path / "home"
    vault = _vault(tmp_path)
    index = ["--index-url", "https://user:tok-SECRET@pkgs.example/simple"]
    _seed(_files(home, "cursor")["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": [*index, "alice-memory", "mcp", "--data-dir", vault]}}})
    # The entry's credentials keep the old hook's text, so the dry run prints it.
    _seed_hook(home, "cursor", f"uvx --index-url https://user:tok-SECRET@pkgs.example/simple --from alice-memory alice-memory-session-start --data-dir {vault}")
    code, out, err = _install(capsys, home, "--host", "cursor", "--dry-run")
    assert code == 0, (out, err)
    assert "tok-SECRET" not in out + err
    assert "https://<hidden>" in out


# --- P3: the docs placeholder is unset ------------------------------------------------


@pytest.mark.parametrize("flag", [False, True], ids=["default", "flag"])
def test_the_docs_placeholder_data_dir_is_treated_as_unset(
    tmp_path: Path, capsys, flag: bool
) -> None:
    """``/ABSOLUTE/PATH/TO/.alice`` pasted from the README is replaced, and the receipt says so.

    Mutation: keep the placeholder as a data dir. A case fails.
    """

    home = tmp_path / "home"
    path = _files(home, "claude-desktop")["mcp"]
    _seed(path, {"mcpServers": {"alice": {"command": "uvx", "args": ["alice-memory", "mcp", "--data-dir", "/ABSOLUTE/PATH/TO/.alice"]}}})
    expected = _vault(tmp_path, "flagged") if flag else str(home.resolve() / ".alice")
    extra = ("--data-dir", expected) if flag else ()
    code, out, err = _install(capsys, home, "--host", "claude-desktop", *extra)
    assert code == 0, (out, err)
    assert _alice(path)["args"] == ["alice-memory", "mcp", "--data-dir", expected]
    assert f"data_dir: /ABSOLUTE/PATH/TO/.alice (the placeholder from the docs) -> {expected}" in out


# --- P3: a backup directory that cannot be made has its own reason ---------------------


def test_a_backup_directory_that_cannot_be_created_is_named(tmp_path: Path, capsys) -> None:
    """The data dir is a file, so <data dir>/backups cannot be made; the host file is untouched.

    Mutation: report it as the host file failing. This test fails.
    """

    home = tmp_path / "home"
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    path = _files(home, "claude-desktop")["mcp"]
    text = _seed(path, {"mcpServers": {"other": {"command": "x"}}})
    code, out, _ = _install(capsys, home, "--host", "claude-desktop", "--data-dir", str(blocker))
    assert code == 1
    assert path.read_text(encoding="utf-8") == text
    backup_dir = blocker.resolve() / "backups" / "host-configs"
    assert f"reason: the backup directory {backup_dir} could not be created or written" in out
    assert "the file could not be read or written" not in out


def test_a_backup_failure_on_the_entry_leaves_the_hook_not_attempted(tmp_path: Path, capsys) -> None:
    """Per-file receipts hold for backup failures too. Mutation: drop "not attempted". This test fails."""

    home = tmp_path / "home"
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    _seed(_files(home, "claude-code")["mcp"], {"mcpServers": {"other": {"command": "x"}}})
    _seed(_files(home, "claude-code")["hooks"], {"model": "opus"})
    code, out, _ = _install(capsys, home, "--host", "claude-code", "--data-dir", str(blocker))
    assert code == 1
    assert "session_start: not attempted" in out


# --- P3: every value from the user's entry is hidden, and the keep line lists them -------


def test_every_value_but_command_type_timeout_and_cwd_is_hidden(tmp_path: Path, capsys) -> None:
    """Mutation: show a key that is not command, type, timeout or cwd, or drop an item
    from the keep line. This test fails.
    """

    home = tmp_path / "home"
    entry = {
        "command": "uvx",
        "args": ["alice-memory", "mcp", "--db", "/x.db", "--user-email", "me@example.com"],
        "type": "stdio",
        "timeout": 30,
        "cwd": "/work",
        "disabled": "flag-SECRET",
        "autoApprove": ["tool-SECRET"],
        "env": {"TOKEN": "env-SECRET"},
    }
    _seed(_files(home, "claude-desktop")["mcp"], {"mcpServers": {"alice": entry}})
    code, out, _ = _install(capsys, home, "--host", "claude-desktop", "--data-dir", _vault(tmp_path), "--dry-run")
    assert code == 1
    assert "SECRET" not in out
    snippet = json.loads(out.split("snippet:\n", 1)[1].split("\nkeep:", 1)[0])
    shown = snippet["mcpServers"]["alice"]
    assert (shown["command"], shown["type"], shown["timeout"], shown["cwd"]) == ("uvx", "stdio", 30, "/work")
    assert shown["disabled"] == "<hidden>" and shown["autoApprove"] == "<hidden>"
    assert "keep: install printed these values from your file as <hidden>: disabled, autoApprove, env.TOKEN;" in out


def test_secret_flags_and_url_credentials_in_args_are_hidden(tmp_path: Path, capsys) -> None:
    """In the paste and in the reason line, which quotes the args the server rejects.

    The reason line leaked them until this test was written, 2026-09-23.
    Mutation: stop hiding the value after a flag named like a secret, or
    quote the rejected args raw. This test fails.
    """

    home = tmp_path / "home"
    args = ["alice-memory", "mcp", "--db", "/x.db", "--api-key", "k-SECRET", "--token=t-SECRET", "--password", "p-SECRET"]
    _seed(_files(home, "claude-desktop")["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": args}}})
    code, out, _ = _install(capsys, home, "--host", "claude-desktop", "--data-dir", _vault(tmp_path), "--dry-run")
    assert code == 1
    assert "SECRET" not in out
    assert "--api-key <hidden> --token=<hidden> --password <hidden>" in out


# --- P3: S4's sibling rule only where a hook is written; a cached sibling is dead --------


def test_a_wrapper_without_session_start_is_alive_on_claude_desktop(
    tmp_path: Path, capsys
) -> None:
    """Claude Desktop runs no hook, so alice-memory alone is a working launcher there.

    Mutation: require the sibling on every host. This test fails.
    """

    lonely = make_scripts(tmp_path / "wrapper" / "bin", session_start=False)
    home = tmp_path / "home"
    entry = {"command": str(lonely / "alice-memory"), "args": ["mcp", "--data-dir", _vault(tmp_path)]}
    path = _files(home, "claude-desktop")["mcp"]
    text = _seed(path, {"mcpServers": {"alice": entry}})
    code, out, err = _install(capsys, home, "--host", "claude-desktop")
    assert code == 0, (out, err)
    assert path.read_text(encoding="utf-8") == text
    assert f"launcher: kept {lonely / 'alice-memory'} mcp" in out


def test_a_sibling_that_links_into_a_uv_cache_is_dead(tmp_path: Path, capsys) -> None:
    """alice-memory is real, its session-start is a symlink into a uv cache: the pair is dead.

    Mutation: skip the cache check on the sibling. This test fails.
    """

    cache = make_scripts(tmp_path / "cache" / "uv" / "archive-v0" / "Id" / "bin")
    bin_dir = tmp_path / "venv" / "bin"
    executable(bin_dir / "alice-memory")
    (bin_dir / "alice-memory-session-start").symlink_to(cache / "alice-memory-session-start")
    home = tmp_path / "home"
    vault = _vault(tmp_path)
    path = _files(home, "cursor")["mcp"]
    _seed(path, {"mcpServers": {"alice": {"command": str(bin_dir / "alice-memory"), "args": ["mcp", "--data-dir", vault]}}})
    code, out, err = _install(capsys, home, "--host", "cursor")
    assert code == 0, (out, err)
    assert _alice(path)["command"] == "uvx"
    assert "alice-memory-session-start is inside a uv cache" in out


# --- P3: S1's resolve_db_path half ---------------------------------------------------


@pytest.mark.parametrize("form", ["symlink", "dotdot", "trailing-slash"])
def test_the_hook_gets_the_dir_the_server_resolves(tmp_path: Path, capsys, form: str) -> None:
    """The server opens resolve_db_path(...).parent; the hook must name that dir.

    Mutation: use the --data-dir text without resolve_db_path. A case fails.
    """

    real = tmp_path / "real-vault"
    real.mkdir()
    if form == "symlink":
        link = tmp_path / "link-vault"
        link.symlink_to(real, target_is_directory=True)
        written = str(link)
    elif form == "dotdot":
        (tmp_path / "sub").mkdir()
        written = str(tmp_path / "sub" / ".." / "real-vault")
    else:
        written = str(real) + "/"
    home = tmp_path / "home"
    _seed(_files(home, "claude-code")["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": ["alice-memory", "mcp", "--data-dir", written]}}})
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert _hooks(home, "claude-code") == [
        f"uvx --from alice-memory alice-memory-session-start --data-dir {os.path.realpath(real)}"
    ]


def test_backup_names_are_unchanged_by_round_5(tmp_path: Path, capsys) -> None:
    """Control: backups still land as <host>-<file>.alice-backup-<UTC> in host-configs."""

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    path = _files(home, "cursor")["mcp"]
    _seed(path, {"mcpServers": {}})
    assert _install(capsys, home, "--host", "cursor", "--data-dir", str(vault))[0] == 0
    backups = list((vault / "backups" / "host-configs").glob(f"cursor-mcp.json{HERMES_BACKUP_MARKER}*"))
    assert len(backups) == 1
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
