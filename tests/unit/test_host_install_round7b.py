"""alice-memory install: review round 7 part 2, the panel's ruling on index URLs, 2026-09-23.

Why this exists. Rounds 5 and 6 tried to tell credentials from harmless
URLs by content: user info, a query, a fragment, a token-shaped path
segment. The targeted check showed no content test can tell a token from
a repo name (Cloudsmith and Gemfury put 16 and 20 character tokens in the
path; a token can sit in a host label), so the hook and the screen could
still carry a secret. The ruling:

- install never writes a URL into a hook file, and never prints one
  unmasked;
- no new hook is added when a uvx option carried from the entry holds
  ``scheme://`` in any form, names an index (--index, --index-url, -i,
  --extra-index-url, --default-index, --find-links, -f, even with a local
  path), or is not on the carry allowlist (--prerelease, --python/-p,
  --python-preference, and the flags --native-tls, --offline, --no-cache,
  --refresh);
- such an entry keeps the existing path: an existing hook keeps its
  launcher text, its --data-dir moves with the entry when literal, and the
  MCP entry keeps its options;
- every URL prints as scheme://<hidden>, host included;
- the warning names the user-level uv config, and install prints the plain
  hook argv it would add after that change (allowlisted options, no URL),
  never a masked argv to add by hand.

How it escaped: the round-5 and round-6 matrices built every credential
around user info or a query, which a content test can see.

Each test names the edit that makes it fail. No host binary is run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alicebot_api.host_install import host_file_map
from alicebot_api.onramp import main as onramp_main

pytestmark = pytest.mark.usefixtures("uvx_on_path")

HOOK = "uvx --from alice-memory alice-memory-session-start --data-dir {dir}"

# (options, the parts that must never appear anywhere). A local path and an
# off-list option carry no URL; for them the list holds the value itself.
SHAPES = {
    "cloudsmith-16": (["--index-url", "https://dl.cloudsmith.io/AbCdEf0123456789/acme/repo/python/simple/"], ["AbCdEf0123456789", "dl.cloudsmith.io"]),
    "gemfury-20": (["--index-url", "https://pypi.fury.io/A1b2C3d4E5f6G7h8I9j0/acme/"], ["A1b2C3d4E5f6G7h8I9j0", "pypi.fury.io"]),
    "letters-only-path": (["--extra-index-url", "https://pkgs.example/abcdefghijklmnopqrstuvwxyz/simple/"], ["abcdefghijklmnopqrstuvwxyz"]),
    "host-label": (["--index-url", "https://tok9secret.pkgs.example/simple/"], ["tok9secret"]),
    "with-url": (["--with", "https://files.example/tok5/alice_extra-1.0-py3-none-any.whl"], ["files.example", "tok5"]),
    "f-short": (["-f", "https://files.example/tok6/wheels/"], ["files.example", "tok6"]),
    "equals": (["--index-url=https://pkgs.example/tok7/simple"], ["pkgs.example", "tok7"]),
    "f-attached": (["-fhttps://files.example/tok8/"], ["files.example", "tok8"]),
    "i-attached": (["-ihttps://pkgs.example/tok9/simple"], ["pkgs.example", "tok9"]),
    "find-links-local": (["--find-links", "./wheels"], []),
    "off-list": (["--exclude-newer", "2025-01-01"], []),
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


def _dir(tmp_path: Path, name: str) -> str:
    return str((tmp_path / name).resolve())


@pytest.mark.parametrize("host", ["cursor", "claude-code"])
@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
def test_an_uncarried_option_gets_no_hook_and_no_url_is_written_or_printed(
    tmp_path: Path, capsys, host: str, shape: str, dry_run: bool
) -> None:
    """No hook is added, the URL is in no hook file and on no output line, the entry is untouched.

    Mutation: carry an index option or a URL value into the hook, or keep
    the host when masking. A case fails.
    """

    options, secrets = SHAPES[shape]
    home = tmp_path / "home"
    vault = _dir(tmp_path, "vault")
    mcp = _files(home, host)["mcp"]
    text = _seed(mcp, {"mcpServers": {"alice": {"command": "uvx", "args": [*options, "alice-memory", "mcp", "--data-dir", vault]}}})
    extra = ("--dry-run",) if dry_run else ()
    code, out, err = _install(capsys, home, "--host", host, *extra)
    assert code == 0, (out, err)
    assert not _files(home, host)["hooks"].exists()
    assert mcp.read_text(encoding="utf-8") == text
    for secret in secrets:
        assert secret not in out + err, secret
    assert "session_start: skipped" in out


@pytest.mark.parametrize("host", ["cursor", "claude-code"])
def test_the_index_warning_names_the_user_level_config_and_the_plain_argv(
    tmp_path: Path, capsys, host: str
) -> None:
    """The warning does not loop, and the argv printed is the plain one install would add.

    Mutation: put an uncarried option or a URL into the after-change argv,
    or name a project uv.toml. This test fails.
    """

    home = tmp_path / "home"
    vault = _dir(tmp_path, "vault")
    args = ["--prerelease", "allow", "--index-url", "https://tok9secret.pkgs.example/simple/", "alice-memory", "mcp", "--data-dir", vault]
    _seed(_files(home, host)["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": args}}})
    code, out, err = _install(capsys, home, "--host", host)
    assert code == 0, (out, err)
    assert "install never writes a URL into a hook file" in out
    assert "~/.config/uv/uv.toml as [[index]]" in out
    assert "%APPDATA%\\uv\\uv.toml on Windows" in out
    assert "not a project uv.toml" in out
    assert "UV_INDEX_<NAME>_USERNAME and UV_INDEX_<NAME>_PASSWORD" in out
    assert "remove the option from the entry's args and run install again" in out
    line = next(line for line in out.splitlines() if line.startswith("session_start_argv_after_change: "))
    assert json.loads(line.split(": ", 1)[1]) == [
        "uvx", "--prerelease", "allow", "--from", "alice-memory", "alice-memory-session-start", "--data-dir", vault,
    ]
    assert "tok9secret" not in out + err
    assert "UV_INDEX_* environment" not in out


@pytest.mark.parametrize("host", ["cursor", "claude-code"])
def test_an_existing_hook_keeps_its_text_and_follows_the_entrys_dir(
    tmp_path: Path, capsys, host: str
) -> None:
    """The refused entry's existing path: launcher text kept, --data-dir moved when literal.

    Mutation: rewrite the existing hook with the entry's launcher. This test fails.
    """

    home = tmp_path / "home"
    old, new = _dir(tmp_path, "old"), _dir(tmp_path, "new")
    mcp = _files(home, host)["mcp"]
    _seed(mcp, {"mcpServers": {"alice": {"command": "uvx", "args": ["--index-url", "https://tok9secret.pkgs.example/simple/", "alice-memory", "mcp", "--data-dir", old]}}})
    hooks = _files(home, host)["hooks"]
    hook = HOOK.format(dir=old)
    if host == "cursor":
        _seed(hooks, {"version": 1, "hooks": {"sessionStart": [{"command": hook}]}})
    else:
        _seed(hooks, {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": hook}]}]}})
    code, out, err = _install(capsys, home, "--host", host, "--data-dir", new)
    assert code == 0, (out, err)
    assert HOOK.format(dir=new) in hooks.read_text(encoding="utf-8")
    assert "tok9secret" not in hooks.read_text(encoding="utf-8") + out + err
    args = json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]["alice"]["args"]
    assert args[:2] == ["--index-url", "https://tok9secret.pkgs.example/simple/"]


@pytest.mark.parametrize("host", ["cursor", "claude-code"])
def test_allowlisted_options_are_carried(tmp_path: Path, capsys, host: str) -> None:
    """Control: --prerelease, --python and the four flags reach the hook unchanged.

    Mutation: refuse an allowlisted option. This test fails.
    """

    home = tmp_path / "home"
    vault = _dir(tmp_path, "vault")
    options = ["--prerelease", "allow", "--python", "3.12", "--python-preference", "managed", "--native-tls", "--offline", "--no-cache", "--refresh"]
    _seed(_files(home, host)["mcp"], {"mcpServers": {"alice": {"command": "uvx", "args": [*options, "alice-memory", "mcp", "--data-dir", vault]}}})
    code, out, err = _install(capsys, home, "--host", host)
    assert code == 0, (out, err)
    hooks = _files(home, host)["hooks"].read_text(encoding="utf-8")
    assert f"uvx {' '.join(options)} --from alice-memory alice-memory-session-start --data-dir {vault}" in hooks
