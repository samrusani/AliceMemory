"""Receipt values cannot forge a line, and would-not-start hides flagged words.

A `--data-dir` that holds a newline used to become a second receipt line.
Would-not-start printed any argument word `masked_args` left alone, including
a word the credential check flags. These tests cover every install host, a
dry run and a real run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alicebot_api.host_install import INSTALL_HOSTS, host_file_map
from alicebot_api.onramp import main as onramp_main
from tests.unit.launcher_helpers import pin_launcher_search

# Built at runtime so the source has no credential literal.
_PAT = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
_FLAG_VALUE = "zz-SECRET-value"
_URL = "https://user:s3cret@h.example/simple"
_FORGED = "/tmp/vault" + "\n" + "action: forged" + "\t" + "\x01" + "\u2028" + "tail"
_PLAIN_BREAK = "ok" + "\n" + "action: forged"


def _install(capsys, home: Path, *extra: str) -> tuple[int, str, str]:
    code = onramp_main(["install", "--home", str(home), *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _files(home: Path, host: str) -> dict[str, Path]:
    return host_file_map(home.resolve())[host]


def _mcp_path(home: Path, host: str) -> Path:
    return _files(home, host)["mcp"]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _json_entry(host: str, args: list[str]) -> str:
    entry = {"command": "uvx", "args": args}
    if host == "openclaw":
        doc: dict[str, object] = {"mcp": {"servers": {"alice": entry}}}
    elif host == "opencode":
        doc = {
            "mcp": {
                "alice": {
                    "type": "local",
                    "command": args,
                }
            }
        }
    else:
        doc = {"mcpServers": {"alice": entry}}
    return json.dumps(doc, indent=2) + "\n"


def _yaml_args(args: list[str]) -> str:
    rendered: list[str] = []
    for arg in args:
        if any(ord(char) < 0x20 or char in " \t:[]{}\"'\\" or ord(char) > 0x7E for char in arg):
            rendered.append(json.dumps(arg))
        else:
            rendered.append(arg)
    return "[" + ", ".join(rendered) + "]"


def _seed(home: Path, host: str, args: list[str]) -> tuple[Path, str]:
    path = _mcp_path(home, host)
    if host == "hermes":
        text = "mcp_servers:\n  alice:\n    command: uvx\n    args: " + _yaml_args(args) + "\n"
    else:
        text = _json_entry(host, args)
    _write(path, text)
    return path, text


def _server_args(token: str, flag_value: str, url: str, plain: str) -> list[str]:
    return ["alice-memory", "mcp", "--token", flag_value, token, url, plain, "--bogus"]


@pytest.mark.parametrize("host", INSTALL_HOSTS)
@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
def test_a_data_dir_newline_cannot_forge_a_receipt_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, host: str, dry_run: bool
) -> None:
    """The old data dir stays inside the data_dir line, controls escaped.

    Mutation: stop escaping receipt values. The newline becomes its own
    `action: forged` line. This test fails.
    """

    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    home = tmp_path / "home"
    path, original = _seed(home, host, ["alice-memory", "mcp", "--data-dir", _FORGED])
    new_dir = tmp_path / "moved"
    extra = ("--dry-run",) if dry_run else ()
    code, out, err = _install(capsys, home, "--host", host, "--data-dir", str(new_dir), *extra)
    assert code in (0, 1), (host, out, err)
    assert "action: forged" not in out.splitlines()
    assert "\\u000aaction: forged\\u0009\\u0001\\u2028tail" in out
    assert "\u2028" not in out
    assert "\x01" not in out
    data_lines = [line for line in out.splitlines() if line.startswith("data_dir:")]
    assert data_lines
    assert all("\t" not in line and "\n" not in line for line in data_lines)
    if dry_run:
        assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("host", INSTALL_HOSTS)
@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
def test_would_not_start_prints_a_fixed_label_for_flagged_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, host: str, dry_run: bool
) -> None:
    """Credential words and masked_args words are `<hidden>`, and a newline stays escaped.

    Mutation: print masked_args output unchanged, or skip the credential
    check. The token or the flag value is on the receipt. This test fails.
    """

    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    home = tmp_path / "home"
    path, original = _seed(
        home, host, _server_args(_PAT, _FLAG_VALUE, _URL, _PLAIN_BREAK)
    )
    extra = ("--dry-run",) if dry_run else ()
    _code, out, err = _install(capsys, home, "--host", host, *extra)
    reason = "\n".join(line for line in out.splitlines() if "would not start" in line)
    assert reason
    assert _PAT not in reason
    assert _FLAG_VALUE not in reason
    assert "s3cret" not in reason
    assert "action: forged" not in out.splitlines()
    assert (
        "alice-memory mcp would not start with the args "
        "--token <hidden> <hidden> https://<hidden> ok\\u000aaction: forged --bogus"
    ) in reason
    assert path.read_text(encoding="utf-8") == original
