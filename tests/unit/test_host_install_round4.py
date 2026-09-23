"""alice-memory install: review round 4 findings, 2026-09-23.

Why this exists. The round-4 review of the S4 install work found one P1
and seven P2s, several with one root cause in host_launcher, and asked for
fixes that remove whole classes:

- S1: install read an entry's data dir by hand (last "--data-dir" token),
  so ``--data /v``, ``--data-d=/v`` and relative paths were misread. It now
  parses the entry's args with the server's own parser and resolve_db_path.
- S2/S3: hooks were read back with "a backslash means a Windows path", which
  shlex never writes, so a hook with a backslash was duplicated or churned,
  and a hand-written ``--data-dir $HOME/.alice`` became <home>/$HOME/.alice.
- S5: the hook dropped the entry's uvx options, so it could resolve another
  version from another index.
- P6: a Hermes alias to a plain scalar that continues on the next line was
  read as its first line, which moved the server's data dir.
- P7: the dry-run would-refuse paths printed env, headers and url secrets.
- P3s: --db entries on Hermes had their env rewritten; the Hermes refusal
  for a foreign entry offered ~/.alice and other wording than JSON;
  backups sat next to host files and symlink targets; the Windows openclaw
  line was not held to the hook rules.

How they escaped: every earlier test wrote entries in install's own
spelling (``--data-dir /x``), hooks install itself had written, dirs with
no shell characters or backslashes, and dry runs of entries without
secrets.

Each test names the edit that makes it fail. No host binary is run.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import yaml

from alicebot_api import host_launcher
from alicebot_api.host_install import HERMES_BACKUP_MARKER, host_file_map, openclaw_add_line
from alicebot_api.host_launcher import UVX_LAUNCHER, script_launcher
from alicebot_api.onramp import main as onramp_main
from tests.unit.launcher_helpers import make_scripts, pin_launcher_search

pytestmark = pytest.mark.usefixtures("uvx_on_path")


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


def _claude_groups(home: Path) -> list[dict]:
    doc = json.loads(_files(home, "claude-code")["hooks"].read_text(encoding="utf-8"))
    return doc["hooks"]["SessionStart"]


def _claude_hook(home: Path) -> list[str]:
    return [h["command"] for group in _claude_groups(home) for h in group["hooks"]]


def _dir(tmp_path: Path, name: str) -> str:
    return str((tmp_path / name).resolve())


# --- S1: the store is read the way the server reads it --------------------------------


@pytest.mark.parametrize(
    "spelling",
    [["--data", "{v}"], ["--data-d={v}"], ["--data-dir={v}"], ["--data-dir", "/elsewhere", "--data", "{v}"]],
    ids=["abbreviated", "abbreviated-equals", "equals", "repeated-last-wins"],
)
def test_entry_store_is_read_by_the_servers_parser(
    tmp_path: Path, capsys, spelling: list[str]
) -> None:
    """Abbreviations, = forms and repeats resolve as argparse resolves them.

    The hook follows the dir the server opens, and the entry stays as it is.
    Mutation: read the store with last_option(args, "--data-dir"), the
    round-3 hand reader. A case fails.
    """

    home = tmp_path / "home"
    vault = _dir(tmp_path, "vault")
    args = ["alice-memory", "mcp", *[part.format(v=vault) for part in spelling]]
    text = _seed(_files(home, "claude-code")["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": args}}})
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert _files(home, "claude-code")["mcp"].read_text(encoding="utf-8") == text
    assert _claude_hook(home) == [f"uvx --from alice-memory alice-memory-session-start --data-dir {vault}"]


def test_the_flag_replaces_every_spelling_with_one_data_dir(tmp_path: Path, capsys) -> None:
    """--data-dir X rewrites ``--data A --data-d=B`` to one ``--data-dir X``.

    Mutation: replace only the exact spelling --data-dir. This test fails.
    """

    home = tmp_path / "home"
    new = _dir(tmp_path, "new")
    path = _files(home, "cursor")["mcp"]
    args = ["alice-memory", "mcp", "--data", "/a", "--user-email", "me@example.com", "--data-d=/b"]
    _seed(path, {"mcpServers": {"alice": {"command": "uvx", "args": args}}})
    code, out, err = _install(capsys, home, "--host", "cursor", "--data-dir", new)
    assert code == 0, (out, err)
    assert _alice(path)["args"] == [
        "alice-memory", "mcp", "--data-dir", new, "--user-email", "me@example.com"
    ]
    assert f"data_dir: /b -> {new}" in out


def test_a_relative_data_dir_is_refused_without_the_flag(tmp_path: Path, capsys) -> None:
    """A relative --data-dir resolves against the host's cwd, which install cannot know.

    The host is refused byte-identical, the paste marks where an absolute
    path goes, and with --data-dir the entry moves as usual. Mutation:
    resolve the relative path against home or cwd. This test fails.
    """

    home = tmp_path / "home"
    path = _files(home, "claude-desktop")["mcp"]
    text = _seed(path, {"mcpServers": {"alice": {"command": "uvx", "args": ["alice-memory", "mcp", "--data-dir", "vault"]}}})
    code, out, _ = _install(capsys, home, "--host", "claude-desktop")
    assert code == 1
    assert path.read_text(encoding="utf-8") == text
    assert "--data-dir vault is a relative path" in out
    assert '"<an absolute path for vault>"' in out
    new = _dir(tmp_path, "new")
    code, out, _ = _install(capsys, home, "--host", "claude-desktop", "--data-dir", new)
    assert code == 0, out
    assert _alice(path)["args"] == ["alice-memory", "mcp", "--data-dir", new]
    assert f"data_dir: vault -> {new}" in out


def test_args_the_server_rejects_keep_the_entry_and_its_hook(tmp_path: Path, capsys) -> None:
    """``--bogus`` makes alice-memory mcp exit, so install keeps both files as they are.

    The hook on another dir is not moved. Mutation: fall back to ~/.alice
    for args the parser rejects. This test fails.
    """

    home = tmp_path / "home"
    files = _files(home, "claude-code")
    text = _seed(files["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": ["alice-memory", "mcp", "--bogus", "x"]}}})
    old = "uvx --from alice-memory alice-memory-session-start --data-dir /hook/dir"
    _seed(files["hooks"], {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": old}]}]}})
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert files["mcp"].read_text(encoding="utf-8") == text
    assert _claude_hook(home) == [old]
    assert "alice-memory mcp would not start with the args --bogus x" in out


def test_db_equals_form_is_a_db_entry(tmp_path: Path, capsys) -> None:
    """``--db=/x.db`` is --db to the server: no hook is added. Mutation: hand-read --db."""

    home = tmp_path / "home"
    files = _files(home, "claude-code")
    _seed(files["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": ["alice-memory", "mcp", "--db=/x.db"]}}})
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert not files["hooks"].exists()
    assert "this entry opens --db /x.db" in out


# --- S2: hooks read back by the rules they were written with --------------------------


def test_a_script_dir_with_a_backslash_and_a_space_stays_one_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """shlex.join writes a backslash inside quotes; reading must not split on it.

    Round 3 read any hook with a backslash with str.split, so the quoted
    script path lost its basename and each run added another SessionStart
    group. Mutation: restore the backslash rule in split_command. This
    test fails.
    """

    scripts = make_scripts(tmp_path / "my tools\\x" / "bin")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=scripts)
    home = tmp_path / "home"
    vault = _dir(tmp_path, "va\\ult")
    assert _install(capsys, home, "--host", "claude-code", "--data-dir", vault)[0] == 0
    first = _claude_hook(home)
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert len(_claude_groups(home)) == 1
    assert _claude_hook(home) == first
    assert "session_start: already-present" in out
    assert "warning" not in out


def test_a_hook_with_backslash_escapes_keeps_its_own_command(
    tmp_path: Path, capsys
) -> None:
    """``--data-dir /my\\ vault`` depends on the shell's backslash processing.

    Round 4 replaced it with the entry's dir; round 5 (P2-2) reads it as the
    shell would: not literal, so the hook keeps its own command unless
    --data-dir is passed. Mutation: treat an unquoted backslash as literal.
    This test fails.
    """

    home = tmp_path / "home"
    files = _files(home, "claude-code")
    vault = _dir(tmp_path, "vault")
    _seed(files["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": ["alice-memory", "mcp", "--data-dir", vault]}}})
    old = "uvx --from alice-memory alice-memory-session-start --data-dir /my\\ vault"
    _seed(files["hooks"], {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": old}]}]}})
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    assert _claude_hook(home) == [old]
    assert "is not read literally by the shell, so install left the hook's command as it was" in out


# --- S3: shell expansion is never trusted ---------------------------------------------


def test_a_dollar_home_hook_does_not_move_a_new_entry_under_home(tmp_path: Path, capsys) -> None:
    """The tower's case: a hand-written ``--data-dir $HOME/.alice`` hook, no entry yet.

    Round 3 wrote the new entry on <home>/$HOME/.alice. Now the entry takes
    ~/.alice with a warning, and the hook keeps its own text. Mutation:
    trust a data dir with $ in it. This test fails.
    """

    home = tmp_path / "home"
    files = _files(home, "claude-code")
    old = "uvx --from alice-memory alice-memory-session-start --data-dir $HOME/.alice"
    _seed(files["hooks"], {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": old}]}]}})
    code, out, err = _install(capsys, home, "--host", "claude-code")
    assert code == 0, (out, err)
    default = str(home.resolve() / ".alice")
    assert _alice(files["mcp"])["args"] == ["alice-memory", "mcp", "--data-dir", default]
    assert "$HOME" not in json.dumps(_alice(files["mcp"]))
    assert _claude_hook(home) == [old]
    assert "is not read literally by the shell" in out


def test_a_shell_hook_moves_only_when_the_flag_asks(tmp_path: Path, capsys) -> None:
    """With --data-dir, install is changing the dir on purpose, so the hook follows.

    Mutation: keep a shell hook's text even when --data-dir is passed. This
    test fails.
    """

    home = tmp_path / "home"
    files = _files(home, "claude-code")
    old = "uvx --from alice-memory alice-memory-session-start --data-dir ~/.alice"
    _seed(files["hooks"], {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": old}]}]}})
    new = _dir(tmp_path, "new")
    code, out, err = _install(capsys, home, "--host", "claude-code", "--data-dir", new)
    assert code == 0, (out, err)
    assert _claude_hook(home) == [f"uvx --from alice-memory alice-memory-session-start --data-dir {new}"]


# --- S5: the hook carries the entry's uvx options ---------------------------------------


def test_the_written_hook_carries_the_entrys_uvx_options(tmp_path: Path, capsys) -> None:
    """Mutation: drop the options from the hook argv. This test fails.

    Since the round-7 ruling only allowlisted options reach a hook, so this
    uses --python and --prerelease (an index is refused; see round 7).
    """

    home = tmp_path / "home"
    vault = _dir(tmp_path, "vault")
    args = ["--python", "3.12", "--prerelease", "allow", "alice-memory==1.0", "mcp", "--data-dir", vault]
    _seed(_files(home, "cursor")["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": args}}})
    code, out, err = _install(capsys, home, "--host", "cursor")
    assert code == 0, (out, err)
    hooks = json.loads(_files(home, "cursor")["hooks"].read_text(encoding="utf-8"))
    assert hooks["hooks"]["sessionStart"] == [
        {
            "command": "uvx --python 3.12 --prerelease allow "
            f"--from alice-memory==1.0 alice-memory-session-start --data-dir {vault}"
        }
    ]
    assert (
        "session_start_launcher: uvx --python 3.12 --prerelease allow "
        "--from alice-memory==1.0 alice-memory-session-start"
    ) in out


# --- P7: nothing from the user's file is printed ----------------------------------------

SECRETS = ("sk-env-SECRET", "hdr-SECRET", "pw-SECRET", "q-SECRET", "old-dir-SECRET")


def _secret_entry(args: list[str]) -> dict:
    return {
        "command": "uvx",
        "args": args,
        "env": {"TOKEN": "sk-env-SECRET", "ALICE_MEMORY_DATA_DIR": "/old-dir-SECRET"},
        "headers": {"Authorization": "Bearer hdr-SECRET"},
        "url": "https://user:pw-SECRET@example.com/x?key=q-SECRET",
    }


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "real"])
def test_the_db_refusal_paste_hides_every_secret(tmp_path: Path, capsys, dry_run: bool) -> None:
    """The --db + --data-dir paste is built from the old entry; its values are hidden.

    Mutation: print the paste unmasked. This test fails.
    """

    home = tmp_path / "home"
    _seed(_files(home, "claude-desktop")["mcp"], {"mcpServers": {"alice": _secret_entry(["alice-memory", "mcp", "--db", "/x.db"])}})
    extra = ("--dry-run",) if dry_run else ()
    code, out, err = _install(capsys, home, "--host", "claude-desktop", "--data-dir", _dir(tmp_path, "v"), *extra)
    assert code == 1
    for secret in SECRETS:
        assert secret not in out + err, secret
    assert "<hidden>" in out
    assert (
        "keep: install printed these values from your file as <hidden>: env.TOKEN, "
        "env.ALICE_MEMORY_DATA_DIR, headers.Authorization, url; copy them from your "
        "existing alice entry"
    ) in out
    assert '"url": "<hidden>"' in out


def test_a_dry_run_of_an_entry_with_secrets_hides_them(tmp_path: Path, capsys) -> None:
    """env, headers, url user info and query, and the old env dir in the note.

    Mutation: stop masking url, or print the old env value in the mismatch
    note. This test fails.
    """

    home = tmp_path / "home"
    vault = _dir(tmp_path, "vault")
    _seed(_files(home, "cursor")["mcp"], {"mcpServers": {"alice": _secret_entry(["alice-memory", "mcp", "--data-dir", vault])}})
    code, out, err = _install(capsys, home, "--host", "cursor", "--dry-run")
    assert code == 0, (out, err)
    for secret in SECRETS:
        assert secret not in out + err, secret


def test_the_hermes_refusal_paste_hides_the_old_entrys_env(tmp_path: Path, capsys) -> None:
    """Hermes --db + --data-dir: the paste keeps the user's env names, not values.

    install's own ALICE_MEMORY_DATA_DIR (the new --data-dir) is shown, and
    the old one is replaced by it. Mutation: print the Hermes refusal
    payload unmasked. This test fails on TOKEN.
    """

    home = tmp_path / "home"
    config = _files(home, "hermes")["mcp"]
    config.parent.mkdir(parents=True)
    config.write_text(
        "mcp_servers:\n  alice:\n    command: uvx\n    args: [alice-memory, mcp, --db, /x.db]\n"
        "    env:\n      ALICE_MEMORY_DATA_DIR: /old-dir-SECRET\n      TOKEN: sk-hermes-SECRET\n",
        encoding="utf-8",
    )
    new = _dir(tmp_path, "new")
    for extra in (("--dry-run",), ()):
        code, out, err = _install(capsys, home, "--host", "hermes", "--data-dir", new, *extra)
        assert code == 1
        assert "old-dir-SECRET" not in out + err
        assert "sk-hermes-SECRET" not in out + err
        assert 'TOKEN: "<hidden>"' in out
        snippet = out.split("snippet:\n", 1)[1].split("\nkeep:", 1)[0].split("\nnext:", 1)[0]
        assert snippet.split("dry run:")[0].count(new) == 2


# --- P6: an alias to a multi-line plain scalar is not read as its first line ------------


@pytest.mark.parametrize(
    "anchor",
    [
        "vaults:\n  main: &v /a\n    /b\n",
        "vaults:\n  - &v /a\n    /b\n",
        "vaults:\n  main: &v\n    /a\n    /b\n",
    ],
    ids=["mapping", "list-item", "block"],
)
def test_an_alias_to_a_multiline_plain_scalar_is_refused(
    tmp_path: Path, capsys, anchor: str
) -> None:
    """PyYAML reads the whole scalar ("/a /b"); install must not read "/a".

    v0.16.0 refused; round 3 read the first line and moved the server's
    data dir. Mutation: record anchors whose scalar continues. A case fails.
    """

    home = tmp_path / "home"
    config = _files(home, "hermes")["mcp"]
    config.parent.mkdir(parents=True)
    text = anchor + "mcp_servers:\n  alice:\n    command: uvx\n    args: [alice-memory, mcp, --data-dir, *v]\n"
    assert yaml.safe_load(text)["mcp_servers"]["alice"]["args"][-1] == "/a /b"
    config.write_text(text, encoding="utf-8")
    code, out, _ = _install(capsys, home, "--host", "hermes")
    assert code == 1
    assert config.read_text(encoding="utf-8") == text
    assert "cannot be read" in out


@pytest.mark.parametrize(
    "anchor",
    ["vaults:\n  main: &v {vault}\n  other: 1\n", "servers:\n  - name: &v {vault}\n    port: 80\n"],
    ids=["mapping", "key-in-list-item"],
)
def test_an_alias_to_a_one_line_scalar_still_reads(tmp_path: Path, capsys, anchor: str) -> None:
    """Control: a one-line anchor next to other keys is still resolved.

    In a list item, ``port`` sits at the key's column, so it is a sibling,
    not a continuation. Mutation: measure continuation from the dash. The
    key-in-list-item case fails.
    """

    home = tmp_path / "home"
    config = _files(home, "hermes")["mcp"]
    config.parent.mkdir(parents=True)
    vault = _dir(tmp_path, "vault")
    text = anchor.format(vault=vault) + (
        "mcp_servers:\n  alice:\n    command: uvx\n    args: [alice-memory, mcp, --data-dir, *v]\n"
    )
    config.write_text(text, encoding="utf-8")
    code, out, err = _install(capsys, home, "--host", "hermes")
    assert code == 0, (out, err)
    assert yaml.safe_load(config.read_text(encoding="utf-8"))["mcp_servers"]["alice"]["args"][-1] == vault


# --- P3: Hermes --db env, Hermes foreign wording ---------------------------------------


def test_hermes_db_entry_keeps_its_env(tmp_path: Path, capsys) -> None:
    """Round 3 rewrote a --db entry's env ALICE_MEMORY_DATA_DIR to ~/.alice.

    Mutation: set env for --db entries too. This test fails.
    """

    home = tmp_path / "home"
    config = _files(home, "hermes")["mcp"]
    config.parent.mkdir(parents=True)
    config.write_text(
        "mcp_servers:\n  alice:\n    command: uvx\n    args: [alice-memory, mcp, --db, /x.db]\n"
        "    env:\n      ALICE_MEMORY_DATA_DIR: /where/the/user/said\n",
        encoding="utf-8",
    )
    code, out, err = _install(capsys, home, "--host", "hermes")
    assert code == 0, (out, err)
    alice = yaml.safe_load(config.read_text(encoding="utf-8"))["mcp_servers"]["alice"]
    assert alice["env"] == {"ALICE_MEMORY_DATA_DIR": "/where/the/user/said"}
    assert alice["args"] == ["alice-memory", "mcp", "--db", "/x.db"]


@pytest.mark.parametrize("host", ["hermes", "claude-desktop"])
def test_a_foreign_entry_refusal_offers_its_visible_dir_and_the_same_words(
    tmp_path: Path, capsys, host: str
) -> None:
    """A foreign entry whose ``mcp`` args the server's parser reads offers that dir.

    Hermes now says what JSON says. Mutation: offer ~/.alice, or keep the
    old Hermes wording. This test fails.
    """

    home = tmp_path / "home"
    seen = _dir(tmp_path, "seen")
    path = _files(home, host)["mcp"]
    path.parent.mkdir(parents=True, exist_ok=True)
    if host == "hermes":
        path.write_text(
            f"mcp_servers:\n  alice:\n    command: python\n    args: [-m, alicebot_api, mcp, --data-dir, {seen}]\n",
            encoding="utf-8",
        )
    else:
        _seed(path, {"mcpServers": {"alice": {"command": "python", "args": ["-m", "alicebot_api", "mcp", "--data-dir", seen]}}})
    code, out, _ = _install(capsys, home, "--host", host)
    assert code == 1
    assert seen in out.split("snippet:\n", 1)[1]
    assert str(home.resolve() / ".alice") not in out
    assert "Rename or remove that alice entry, or add the entry above under a different name by hand." in out


# --- P3: backups in one private directory under the data dir ----------------------------


def test_json_backups_go_to_the_data_dir_never_beside_the_file(tmp_path: Path, capsys) -> None:
    """One 0700 directory under the data dir, even when it already existed looser.

    Only host-configs, install's own directory, is tightened: a user's
    existing <data dir>/backups keeps its mode (round 5 P3). Mutation: back
    up next to the host file, skip the chmod on host-configs, or chmod
    backups too. This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    (vault / "backups" / "host-configs").mkdir(parents=True)
    for directory in (vault / "backups", vault / "backups" / "host-configs"):
        directory.chmod(0o755)
    path = _files(home, "claude-desktop")["mcp"]
    _seed(path, {"mcpServers": {"other": {"command": "x"}}})
    code, out, err = _install(capsys, home, "--host", "claude-desktop", "--data-dir", str(vault))
    assert code == 0, (out, err)
    assert [item.name for item in path.parent.iterdir()] == [path.name]
    backup_dir = vault / "backups" / "host-configs"
    backups = list(backup_dir.glob(f"claude-desktop-{path.name}{HERMES_BACKUP_MARKER}*"))
    assert len(backups) == 1 and f"backup: {backups[0]}" in out
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(backup_dir.parent.stat().st_mode) == 0o755


# --- P3: the Windows openclaw line follows the hook rules --------------------------------


def test_windows_openclaw_line_uses_forward_slashes_and_refuses_what_hooks_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 wrote it with strict=False: quoted $ and %, raw backslashes.

    Mutation: go back to strict=False for the openclaw line. This test fails.
    """

    monkeypatch.setattr(host_launcher, "WINDOWS_HOOKS", True)
    line = openclaw_add_line("C:\\Users\\Sam\\.alice", script_launcher("C:\\Py\\Scripts\\alice-memory.exe"))
    assert line == (
        "openclaw mcp add alice --command C:/Py/Scripts/alice-memory.exe --arg mcp "
        "--arg --data-dir --arg C:/Users/Sam/.alice"
    )
    refused = openclaw_add_line("C:\\Users\\S$m\\.alice", UVX_LAUNCHER)
    assert refused.startswith("note: install did not print an openclaw mcp add line")
    assert '"C:/Users/S$m/.alice"' in refused
