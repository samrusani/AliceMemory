"""mcp and session-start refuse a data dir that is not absolute after ~ expansion.

``alice-memory mcp`` exits 2 and names the value. ``alice-memory-session-start``
exits 0 and prints one line, in the chosen format and on stderr. An empty
session-start value still falls back. These tests do not start the MCP server.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alicebot_api.onramp import _ERROR_CONTRACTS, main as onramp_main
from alicebot_api.session_start_hook import _data_dir_refusal_line, main as hook_main

_REFUSED = ("", "${user_config.data_dir}", "$HOME/.alice", "alice", "%USERPROFILE%\\.alice")
_NONEMPTY_REFUSED = tuple(value for value in _REFUSED if value != "")


def _refusal_line(value: str) -> str:
    return _data_dir_refusal_line(value)


def test_mcp_refuses_empty_and_relative_data_dirs(capsys: pytest.CaptureFixture[str]) -> None:
    """Each refused value is named and the process exits 2.

    Mutation: accept a relative data dir, or accept an empty data dir.
    This test fails.
    """

    for value in _REFUSED:
        code = onramp_main(["mcp", "--data-dir", value])
        captured = capsys.readouterr()
        assert code == 2, value
        payload = json.loads(captured.err)
        assert payload["error"]["code"] == "data_dir_invalid"
        assert value in payload["error"]["message"]
        assert _ERROR_CONTRACTS["data_dir_invalid"] in payload["error"]["message"]
        assert captured.out == ""


def test_mcp_accepts_tilde_and_an_absolute_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tilde path and an absolute path pass the guard. The server is not started."""

    monkeypatch.setattr("alicebot_api.onramp._run_mcp", lambda _args: 0)
    for value in ("~/.alice", "/tmp/alice-data-dir-guard"):
        code = onramp_main(["mcp", "--data-dir", value])
        captured = capsys.readouterr()
        assert code == 0, (value, captured.err)
        assert "data_dir_invalid" not in captured.err


def test_session_start_refuses_a_relative_data_dir_in_both_formats(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The refusal line is the whole stdout in each format, and it is on stderr.

    Mutation: print the old fail-open output (``{}`` or a blank line) for a
    refused value. This test fails.
    """

    for value in _NONEMPTY_REFUSED:
        line = _refusal_line(value)
        assert not line.startswith("{")
        assert not line.startswith("[")
        for output_format, flag in (("json", []), ("markdown", ["--format", "markdown"])):
            code = hook_main(["--data-dir", value, *flag])
            captured = capsys.readouterr()
            assert code == 0, (value, output_format, captured.err)
            assert line in captured.err
            if output_format == "markdown":
                assert captured.out == line + "\n"
                assert captured.out != "\n"
            else:
                payload = json.loads(captured.out)
                assert payload["hookSpecificOutput"]["additionalContext"] == line
                assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
                assert captured.out != "{}\n"


def test_session_start_accepts_tilde_and_an_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``~/.alice`` expands under HOME. An absolute path is accepted too."""

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    absolute = tmp_path / "vault"
    for value in ("~/.alice", str(absolute)):
        code = hook_main(["--data-dir", value, "--format", "markdown"])
        captured = capsys.readouterr()
        assert code == 0, (value, captured.err)
        assert "is not an absolute path" not in captured.out
        assert "is not an absolute path" not in captured.err


def test_session_start_empty_data_dir_keeps_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty value is not a refusal. It falls back, here via the env var."""

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALICE_MEMORY_DATA_DIR", str(tmp_path / "from-env"))
    code = hook_main(["--data-dir", "", "--format", "markdown"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "is not an absolute path" not in captured.out
    assert (tmp_path / "from-env" / "memory.db").is_file()
