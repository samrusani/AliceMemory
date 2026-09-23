"""alice-memory install: review round 7 part 1, 2026-09-23.

Why this exists. The targeted check on 255e765 ran 216 comparisons against
v0.16.0 (54 states, with and without --data-dir, on both trees). Two P2s
were plain bugs:

- P2-1: the round-6 kept-hook move applied to a --db entry, whose hook
  store install promises to leave alone: a pinned or credentialed --db
  entry had its hook's --data-dir moved to ~/.alice, while the receipt
  said the store was left as it was.
- P2-2: the round-6 kept-hook refusal printed a hook's leading
  VAR=secret in session_start_argv, in real and dry runs.

And P3s in the same code: the version-option gate missed attached short
forms (-walice-memory==0.15.7, -c/path), uv's --constraint and --override
spellings, --exclude-newer-package with a separate value (read as the
package spec, so the entry was refused as foreign), and --with-editable
pointing at a local path; Windows mode split --data-dir="C:/x" into an
empty value and a stray word.

How they escaped: the round-6 tests used entries without --db and hooks
without assignments, spelled every uvx option long, and wrote Windows
hooks only with bare or fully quoted words.

Each test names the edit that makes it fail. No host binary is run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alicebot_api import host_launcher
from alicebot_api.host_install import host_file_map
from alicebot_api.onramp import main as onramp_main

pytestmark = pytest.mark.usefixtures("uvx_on_path")

HOOK = "uvx --from alice-memory alice-memory-session-start --data-dir {dir}"
BLOCKERS = {
    "pin": ["alice-memory==0.15.7", "mcp"],
    "credentials": ["--index-url", "https://me:tok-SECRET@pkgs.example/simple", "alice-memory", "mcp"],
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


def _seed_hook(home: Path, host: str, command: str) -> str:
    path = _files(home, host)["hooks"]
    if host == "cursor":
        return _seed(path, {"version": 1, "hooks": {"sessionStart": [{"command": command}]}})
    return _seed(path, {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}})


def _hooks(home: Path, host: str) -> list[str]:
    doc = json.loads(_files(home, host)["hooks"].read_text(encoding="utf-8"))
    if host == "cursor":
        return [item["command"] for item in doc["hooks"]["sessionStart"]]
    return [h["command"] for group in doc["hooks"]["SessionStart"] for h in group["hooks"]]


def _dir(tmp_path: Path, name: str) -> str:
    return str((tmp_path / name).resolve())


# --- P2-1: a --db entry's hook store is never moved ----------------------------------------


@pytest.mark.parametrize("host", ["cursor", "claude-code"])
@pytest.mark.parametrize("blocker", sorted(BLOCKERS))
def test_a_db_entrys_kept_hook_keeps_its_store(
    tmp_path: Path, capsys, host: str, blocker: str
) -> None:
    """The hook's --data-dir stays; the "left alone" line is true.

    Mutation: move a kept hook's dir for --db entries too (the round-6
    behaviour). A case fails.
    """

    home = tmp_path / "home"
    store = _dir(tmp_path, "hook-store")
    _seed_entry(home, host, [*BLOCKERS[blocker], "--db", "/x.db"])
    hook = HOOK.format(dir=store)
    _seed_hook(home, host, hook)
    code, out, err = _install(capsys, home, "--host", host)
    assert code == 0, (out, err)
    assert _hooks(home, host) == [hook]
    assert "install left it, and the SessionStart hook's store, as they were" in out
    assert "session_start_data_dir" not in out
    assert "tok-SECRET" not in out + err


# --- P2-2: NAME=value words in a hook are never printed ------------------------------------

ASSIGNMENT_FORMS = {
    "leading": "ALICE_TOKEN=tok-SECRET uvx --from alice-memory alice-memory-session-start --data-dir $HOME/old",
    "env": "env ALICE_TOKEN=tok-SECRET uvx --from alice-memory alice-memory-session-start --data-dir $HOME/old",
    "env-path-and-option": "/usr/bin/env -u X ALICE_TOKEN=tok-SECRET uvx --from alice-memory alice-memory-session-start --data-dir $HOME/old",
    "two": "A=tok-SECRET B=tok-SECRET uvx --from alice-memory alice-memory-session-start --data-dir $HOME/old",
}


@pytest.mark.parametrize("form", sorted(ASSIGNMENT_FORMS))
@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
def test_the_kept_hook_refusal_never_prints_an_assignment(
    tmp_path: Path, capsys, form: str, dry_run: bool
) -> None:
    """No output shows a NAME=value value, and no masked argv is offered by hand.

    Part 1 masked the argv; the round-7 ruling says never print a masked
    hand-add argv, since it could only be used by pasting the hidden value
    back. So the refusal says to change the --data-dir by hand instead.
    Mutation: print the refusal argv with masked_args only (round 6), or
    print it masked. A case fails.
    """

    home = tmp_path / "home"
    _seed_entry(home, "cursor", ["alice-memory==0.15.7", "mcp", "--data-dir", _dir(tmp_path, "old")])
    _seed_hook(home, "cursor", ASSIGNMENT_FORMS[form])
    extra = ("--dry-run",) if dry_run else ()
    code, out, err = _install(capsys, home, "--host", "cursor", "--data-dir", _dir(tmp_path, "new"), *extra)
    assert code == 1
    assert "session_start_argv:" not in out
    assert "SECRET" not in out + err
    assert f"change its --data-dir to {_dir(tmp_path, 'new')} by hand" in out
    if dry_run:
        assert "words install does not print)" in out or "word install does not print)" in out


# --- P3: uvx options that can change the release, spelled any way ---------------------------


@pytest.mark.parametrize(
    "options",
    [
        ["-walice-memory==0.15.7"],
        ["-c/etc/pins.txt"],
        ["--constraint", "/etc/pins.txt"],
        ["--override", "/etc/pins.txt"],
        ["--exclude-newer-package", "alice-memory=2025-01-01"],
        ["--with-editable", "/src/alice"],
    ],
    ids=["w-attached", "c-attached", "constraint", "override", "exclude-newer-package", "with-editable"],
)
@pytest.mark.parametrize("host", ["cursor", "claude-code"])
def test_version_options_keep_the_hook_and_are_not_foreign(
    tmp_path: Path, capsys, options: list[str], host: str
) -> None:
    """Each keeps the hook and warns; none is refused as an entry install did not write.

    Mutation: drop the attached short form, the singular aliases, the
    separate --exclude-newer-package value, or --with-editable. A case fails.
    """

    home = tmp_path / "home"
    vault = _dir(tmp_path, "vault")
    _seed_entry(home, host, [*options, "alice-memory", "mcp", "--data-dir", vault])
    hook = HOOK.format(dir=vault)
    _seed_hook(home, host, hook)
    code, out, err = _install(capsys, home, "--host", host)
    assert code == 0, (out, err)
    assert "install did not write it" not in out
    assert _hooks(home, host) == [hook]
    assert "which install does not carry into a hook" in out


# --- P3: Windows reads --data-dir="C:/x" as one word -----------------------------------------


def test_windows_equals_form_with_a_quoted_value_reads_as_one_word() -> None:
    """Mutation: split quoted and bare pieces into separate words (round 6). This test fails."""

    command = 'C:/Tools/alice-memory-session-start.exe --data-dir="C:/My Vault"'
    read = host_launcher.read_hook_data_dir(command, windows=True)
    assert (read.raw, read.trusted) == ("C:/My Vault", True)
    assert host_launcher.split_command(command, windows=True) == [
        "C:/Tools/alice-memory-session-start.exe",
        "--data-dir=C:/My Vault",
    ]
    moved, problem = host_launcher.replace_hook_data_dir(command, "C:\\New Dir", windows=True)
    assert problem is None
    assert moved == 'C:/Tools/alice-memory-session-start.exe "--data-dir=C:/New Dir"'


def test_windows_refusal_argv_keeps_the_equals_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The argv offered for a Windows hook replaces the = value, with no stray word.

    Mutation: split quoted and bare pieces into separate words. This test
    fails.
    """

    monkeypatch.setattr(host_launcher, "WINDOWS_HOOKS", True)
    home = tmp_path / "home"
    new = "C:/new"
    _seed_entry(home, "cursor", ["alice-memory==0.15.7", "mcp", "--data-dir", "C:/old"])
    _seed_hook(home, "cursor", 'C:/Tools/alice-memory-session-start.exe --data-dir="%USERPROFILE%/old"')
    code, out, _ = _install(capsys, home, "--host", "cursor", "--data-dir", new)
    assert code == 1
    line = next(line for line in out.splitlines() if line.startswith("session_start_argv: "))
    argv = json.loads(line.split(": ", 1)[1])
    assert argv[0] == "C:/Tools/alice-memory-session-start.exe"
    assert argv[1].startswith("--data-dir=") and argv[1] != "--data-dir="
    assert len(argv) == 2
