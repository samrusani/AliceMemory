"""Relative data directories are refused before a vault is created.

Mutation: drop ``_refuse_mcp_data_dir`` or the session-start absolute check.
These tests fail.
"""

from __future__ import annotations

import json
from pathlib import Path

from alicebot_api.onramp import main as onramp_main
from alicebot_api.session_start_hook import main as hook_main


def test_mcp_refuses_a_relative_data_dir(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    code = onramp_main(["mcp", "--data-dir", "alice"])
    captured = capsys.readouterr()
    assert code == 2
    payload = json.loads(captured.err)
    assert payload["error"]["code"] == "data_dir_invalid"
    assert "alice" in payload["error"]["message"]
    assert not (tmp_path / "alice").exists()
    assert captured.out == ""


def test_mcp_refuses_an_empty_data_dir(capsys) -> None:
    code = onramp_main(["mcp", "--data-dir", ""])
    captured = capsys.readouterr()
    assert code == 2
    payload = json.loads(captured.err)
    assert payload["error"]["code"] == "data_dir_invalid"
    assert payload["error"]["message"].endswith(': ""')


def test_mcp_refuses_unexpanded_home_forms(capsys) -> None:
    for value in ("$HOME/.alice", "%USERPROFILE%\\.alice", "${user_config.data_dir}"):
        code = onramp_main(["mcp", "--data-dir", value])
        captured = capsys.readouterr()
        assert code == 2, value
        payload = json.loads(captured.err)
        assert value in payload["error"]["message"]


def test_mcp_accepts_tilde_without_starting(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("alicebot_api.onramp._run_mcp", lambda _args: 0)
    assert onramp_main(["mcp", "--data-dir", "~/.alice"]) == 0
    assert not (tmp_path / ".alice").exists()


def test_session_start_names_a_relative_data_dir(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(""))
    code = hook_main(["--data-dir", "alice", "--format", "json"])
    captured = capsys.readouterr()
    assert code == 0
    line = 'Alice: the data directory "alice" is not an absolute path; set an absolute path.'
    assert line in captured.err
    payload = json.loads(captured.out)
    assert payload["hookSpecificOutput"]["additionalContext"] == line
    assert not line.startswith("{") and not line.startswith("[")
    assert not (tmp_path / "alice").exists()


def test_session_start_markdown_names_a_relative_data_dir(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(""))
    code = hook_main(["--data-dir", "$HOME/.alice", "--format", "markdown"])
    captured = capsys.readouterr()
    assert code == 0
    line = 'Alice: the data directory "$HOME/.alice" is not an absolute path; set an absolute path.'
    assert captured.out == line + "\n"
    assert line in captured.err
    assert not (tmp_path / ".alice").exists()


def test_session_start_empty_data_dir_uses_the_env_fallback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("ALICE_MEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(""))
    assert hook_main(["--data-dir", ""]) == 0
    captured = capsys.readouterr()
    assert "not an absolute path" not in captured.err
    assert (tmp_path / "memory.db").exists()
