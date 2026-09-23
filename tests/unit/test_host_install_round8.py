"""alice-memory install: review round 8, 2026-09-23.

Why this exists. The final targeted check on 90fa719 (380 cells, 91 states)
found no secret in any hook file, and every re-run idempotent, but two
printed-text P2s, both from denylist masking:

- Hook words were printed with a denylist (NAME=value, URLs, secret flag
  values), so PowerShell ``$env:X="tok"``, ``$Env:X = "tok"`` and a curl
  ``-H 'Authorization: Bearer tok'`` chained before the Alice hook reached
  the dry run and the refusal. Hook words are now printed from an
  allowlist, like the carry allowlist: anything else is <hidden>.
- ``launcher.package`` was printed raw in the "cannot tell whether" warning
  and in the "asks for" customisation, so a direct-URL spec
  (alice-memory@https://user:pass@host/x.whl) printed its password; and
  mask_text stopped a URL at ' or ), which RFC 3986 allows in user info.

How they escaped: every earlier secret was a NAME=value, a URL option or a
flag named like a secret; no test used another shell's syntax, a header,
or a URL in the package spec.

Each test greps all output for the secret. No host binary is run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alicebot_api import host_launcher
from alicebot_api.host_install import host_file_map
from alicebot_api.onramp import main as onramp_main
from tests.unit.launcher_helpers import pin_launcher_search

HOOK_TAIL = "uvx --from alice-memory alice-memory-session-start --data-dir {dir}"


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


def _dir(tmp_path: Path, name: str) -> str:
    return str((tmp_path / name).resolve())


# --- a direct-URL package spec is never printed past its scheme --------------------------

URL_SPECS = {
    "paren": "alice-memory@https://me:p)w-SECRET@h.example/alice.whl",
    "quote": "alice-memory@https://me:p'w-SECRET@h.example/alice.whl",
}


@pytest.mark.parametrize("char", sorted(URL_SPECS))
@pytest.mark.parametrize("position", ["positional", "from"])
@pytest.mark.parametrize("uvx_here", [True, False], ids=["uvx-on-path", "no-uvx"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
def test_a_direct_url_spec_is_never_printed_or_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    char: str,
    position: str,
    uvx_here: bool,
    dry_run: bool,
) -> None:
    """The spec is masked in every line ("asks for", "cannot tell"), and no hook is written.

    Mutation: print launcher.package raw, or let a URL spec reach a hook.
    A case fails.
    """

    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx" if uvx_here else None)
    spec = URL_SPECS[char]
    lead = [spec] if position == "positional" else ["--from", spec, "alice-memory"]
    home = tmp_path / "home"
    mcp = _files(home, "cursor")["mcp"]
    text = _seed(mcp, {"mcpServers": {"alice": {"command": "uvx", "args": [*lead, "mcp", "--data-dir", _dir(tmp_path, "v")]}}})
    extra = ("--dry-run",) if dry_run else ()
    code, out, err = _install(capsys, home, "--host", "cursor", *extra)
    assert code == 0, (out, err)
    assert "SECRET" not in out + err
    assert not _files(home, "cursor")["hooks"].exists()
    assert mcp.read_text(encoding="utf-8") == text
    assert "alice-memory@https://<hidden>" in out
    assert "the entry's package spec is a URL (alice-memory@https://<hidden>)" in out


# --- hook words: an allowlist -------------------------------------------------------------

KEPT_HOOKS = {
    "powershell-quoted": '$env:X="tok-SECRET"; ' + HOOK_TAIL,
    "powershell-spaced": '$Env:X = "tok-SECRET"; ' + HOOK_TAIL,
    "curl-header": "curl -s -H 'Authorization: Bearer tok-SECRET' https://h.example/ping && " + HOOK_TAIL,
}


@pytest.mark.parametrize("kind", sorted(KEPT_HOOKS))
@pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
def test_a_kept_hooks_other_words_are_never_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, kind: str, windows: bool, dry_run: bool
) -> None:
    """A pinned entry keeps the hook; --data-dir refuses it; nothing of the secret is printed.

    Mutation: print hook words with the denylist again (masked_args). A
    case fails.
    """

    monkeypatch.setattr(host_launcher, "WINDOWS_HOOKS", windows)
    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    home = tmp_path / "home"
    old = "C:/old" if windows else _dir(tmp_path, "old")
    new = "C:/new" if windows else _dir(tmp_path, "new")
    _seed(_files(home, "cursor")["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": ["alice-memory==0.15.7", "mcp", "--data-dir", old]}}})
    hook = KEPT_HOOKS[kind].format(dir=old)
    hooks = _files(home, "cursor")["hooks"]
    text = _seed(hooks, {"version": 1, "hooks": {"sessionStart": [{"command": hook}]}})
    extra = ("--dry-run",) if dry_run else ()
    code, out, err = _install(capsys, home, "--host", "cursor", "--data-dir", new, *extra)
    assert code == 1
    assert "SECRET" not in out + err
    assert "session_start: refused" in out
    assert "session_start_argv:" not in out
    assert hooks.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("kind", sorted(KEPT_HOOKS))
def test_a_dry_run_of_a_kept_hook_shows_only_allowlisted_words(tmp_path: Path, capsys, kind: str) -> None:
    """Without --data-dir the hook is kept; the dry run prints it through the allowlist.

    Mutation: print hook words with the denylist again. A case fails.
    """

    home = tmp_path / "home"
    vault = _dir(tmp_path, "vault")
    _seed(_files(home, "cursor")["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": ["alice-memory==0.15.7", "mcp", "--data-dir", vault]}}})
    _seed(_files(home, "cursor")["hooks"], {"version": 1, "hooks": {"sessionStart": [{"command": KEPT_HOOKS[kind].format(dir=vault)}]}})
    code, out, err = _install(capsys, home, "--host", "cursor", "--dry-run")
    assert code == 0, (out, err)
    assert "SECRET" not in out + err
    assert f"<hidden> uvx --from alice-memory alice-memory-session-start --data-dir {vault}" in out


def test_shown_hook_words_is_an_allowlist() -> None:
    """Install's own words show; anything else, however harmless it looks, is hidden.

    Mutation: show a word the allowlist does not name. This test fails.
    """

    words = [
        "/opt/bin/uvx", "--python", "3.12", "--prerelease=allow", "-p3.12", "--offline",
        "--from", "alice-memory==0.16.0", "alice-memory-session-start", "--data-dir", "/v",
        "--from", "alice-memory@https://h/x.whl", "--index-url", "https://h/simple", "-q",
        "FOO=bar", "--data-dir", "https://h/x", "C:/Py/Scripts/alice-memory.exe",
    ]
    shown, hidden = host_launcher.shown_hook_words(words)
    assert shown == [
        "/opt/bin/uvx", "--python", "3.12", "--prerelease=allow", "-p3.12", "--offline",
        "--from", "alice-memory==0.16.0", "alice-memory-session-start", "--data-dir", "/v",
        "--from", "<hidden>", "<hidden>", "<hidden>", "<hidden>",
        "<hidden>", "--data-dir", "<hidden>", "C:/Py/Scripts/alice-memory.exe",
    ]
    assert hidden == 6


# --- mask_text: a URL runs to the end of its word ----------------------------------------------


def test_a_warning_quoting_a_url_with_a_paren_in_its_password_is_masked(tmp_path: Path, capsys) -> None:
    """A hook's --data-dir is a URL whose password holds ); the receipt-wide mask covers it.

    Mutation: stop a text URL at ). This test fails.
    """

    home = tmp_path / "home"
    vault = _dir(tmp_path, "vault")
    _seed(_files(home, "cursor")["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": ["alice-memory", "mcp", "--data-dir", vault]}}})
    _seed(_files(home, "cursor")["hooks"], {"version": 1, "hooks": {"sessionStart": [{"command": "uvx --from alice-memory alice-memory-session-start --data-dir 'https://me:p)tok-SECRET@h.example/x'"}]}})
    code, out, err = _install(capsys, home, "--host", "cursor")
    assert code == 0, (out, err)
    assert "SECRET" not in out + err
    assert "--data-dir https://<hidden> could not be relied on" in out
