"""The dispatch-only hook trial saves stdin and does not import Alice in hook mode."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "real_host_hook_trial.py"
_PINNED = "2.1.281 (Claude Code)"


def _run_hook(artifacts: Path, event: str, stdin: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "hook", event, str(artifacts)],
        input=stdin,
        capture_output=True,
        check=False,
    )


def test_hook_saves_stdin_bytes_and_copies_the_transcript(tmp_path: Path) -> None:
    """Odd spacing stays on disk, and a bad payload still exits 0.

    Mutation: re-dump the JSON, skip the transcript copy, or exit 1 on
    bad JSON. This test fails.
    """

    marker = "marker-" + "k7q"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("line one\n" + marker + "\n", encoding="utf-8")
    raw = ('{ "transcript_path" : "' + str(transcript) + '" }\n').encode()
    artifacts = tmp_path / "artifacts"
    completed = _run_hook(artifacts, "SessionStart", raw)
    assert completed.returncode == 0
    assert (artifacts / "SessionStart.stdin").read_bytes() == raw
    copied = artifacts / "SessionStart.transcript"
    assert copied.is_file()
    assert marker in copied.read_text(encoding="utf-8")
    bad = _run_hook(artifacts, "SessionEnd", b"this is not json")
    assert bad.returncode == 0
    assert (artifacts / "SessionEnd.stdin").read_bytes() == b"this is not json"


def _stub(record: Path) -> str:
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import json
        import os
        import subprocess
        import sys
        from pathlib import Path

        record = Path({str(record)!r})
        if "--version" in sys.argv:
            print(os.environ.get("STUB_VERSION", "9.9.9 (stub)"))
            raise SystemExit(0)
        if "--help" in sys.argv:
            print("stub help")
            raise SystemExit(0)
        home = os.environ["HOME"]
        names = (
            "CLAUDE_CONFIG_DIR",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
        )
        info = {{"home": home, "present": {{name: name in os.environ for name in names}}, "fired": []}}
        mode = os.environ.get("STUB_MODE", "")
        if mode == "silent":
            print("silent-stdout")
            raise SystemExit(int(os.environ.get("STUB_EXIT", "3")))
        settings = json.loads(Path(home, ".claude", "settings.json").read_text())
        transcript = os.environ.get("STUB_TRANSCRIPT")
        repeats = 2 if mode == "double" else 1
        for event in ("SessionStart", "SessionEnd"):
            for group in settings.get("hooks", {{}}).get(event, []):
                handlers = group.get("hooks") if isinstance(group, dict) else None
                if not isinstance(handlers, list):
                    continue
                for handler in handlers:
                    command = handler.get("command") if isinstance(handler, dict) else None
                    if not isinstance(command, str):
                        continue
                    for _ in range(repeats):
                        if transcript:
                            with open(transcript, "a", encoding="utf-8") as handle:
                                handle.write(event + "\\n")
                            payload = json.dumps({{"transcript_path": transcript}}).encode() + b"\\n"
                        else:
                            payload = b'{{"transcript_path":""}}\\n'
                        subprocess.run(command, shell=True, input=payload, check=False)
                        info["fired"].append(event)
        record.write_text(json.dumps(info))
        print("ok")
        """
    )


def test_run_unsets_credentials_and_checks_the_pinned_version(tmp_path: Path, monkeypatch) -> None:
    """The stub sees the trial HOME and none of the four credential names.

    Mutation: write a flat command item, keep ANTHROPIC_API_KEY, keep
    CLAUDE_CONFIG_DIR, or skip the version check. This test fails.
    """

    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "stub.json"
    stub = bindir / "claude"
    stub.write_text(_stub(record), encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-" + "trial")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-" + "trial")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-" + "trial")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "other-config"))
    monkeypatch.setenv("STUB_VERSION", "9.9.9 (stub)")
    wrong = subprocess.run(
        [sys.executable, str(SCRIPT), "run", str(tmp_path / "wrong-artifacts"), str(tmp_path / "wrong-temp")],
        capture_output=True,
        check=False,
    )
    assert wrong.returncode == 1

    monkeypatch.setenv("STUB_VERSION", _PINNED)
    artifacts = tmp_path / "artifacts"
    temp = tmp_path / "temp"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "run", str(artifacts), str(temp)],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    info = json.loads(record.read_text(encoding="utf-8"))
    assert info["home"] == str(temp / "trial-home")
    assert info["present"] == {
        "CLAUDE_CONFIG_DIR": False,
        "ANTHROPIC_API_KEY": False,
        "ANTHROPIC_AUTH_TOKEN": False,
        "CLAUDE_CODE_OAUTH_TOKEN": False,
    }
    assert info["fired"].count("SessionStart") == 1
    assert info["fired"].count("SessionEnd") == 1
    report = json.loads((artifacts / "report.json").read_text(encoding="utf-8"))
    assert report["claude_version"] == _PINNED
    assert report["events"]["SessionStart"]["fired"] == 1
    assert report["events"]["SessionEnd"]["fired"] == 1


def test_hook_mode_does_not_import_alicebot_api(tmp_path: Path) -> None:
    """Hook mode stays on the standard library.

    Mutation: import host_install at the top of the trial script. This
    test fails.
    """

    artifacts = tmp_path / "artifacts"
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import importlib.util\n"
        "import sys\n"
        f"spec = importlib.util.spec_from_file_location('trial_hook', {str(SCRIPT)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        f"module.main(['hook', 'SessionEnd', {str(artifacts)!r}])\n"
        "print('LOADED' if 'alicebot_api' in sys.modules else 'ABSENT')\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(probe)],
        input=b"{}\n",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    assert completed.stdout.decode().strip() == "ABSENT"


def _load_trial():
    spec = importlib.util.spec_from_file_location("real_host_hook_trial", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install_stub(tmp_path: Path, monkeypatch, *, version: str = _PINNED) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "stub.json"
    stub = bindir / "claude"
    stub.write_text(_stub(record), encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("STUB_VERSION", version)
    return record


def test_report_keeps_hook_time_counts_when_the_transcript_grows(tmp_path: Path, monkeypatch) -> None:
    """Hook-time line counts stay put when the transcript grows before replay.

    Mutation: replay into the hook directory, or report the replay line
    count as transcript_lines. SessionStart's saved copy is no longer one
    line, or the report says 2.
    """

    _install_stub(tmp_path, monkeypatch)
    transcript = tmp_path / "session.jsonl"
    monkeypatch.setenv("STUB_TRANSCRIPT", str(transcript))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    artifacts = tmp_path / "artifacts"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "run", str(artifacts), str(tmp_path / "temp")],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    report = json.loads((artifacts / "report.json").read_text(encoding="utf-8"))
    headline = "SessionStart fired 1 time. SessionEnd fired 1 time."
    assert report["headline"] == headline
    assert report["exit_code"] == 0
    assert report["first_stdout_line"] == "ok"
    start = report["events"]["SessionStart"]
    end = report["events"]["SessionEnd"]
    assert start["payload_keys"] == ["transcript_path"]
    assert start["copied"] is True
    assert end["copied"] is True
    assert start["transcript_lines"] == 1
    assert end["transcript_lines"] == 2
    assert start["replay_transcript_lines"] == 2
    assert end["replay_transcript_lines"] == 2
    assert start["replay_copied"] is True
    saved = (artifacts / "SessionStart.stdin").read_bytes()
    assert json.loads(saved)["transcript_path"] == str(transcript)
    assert len((artifacts / "SessionStart.transcript").read_bytes().splitlines()) == 1
    assert len((artifacts / "replay" / "SessionStart.transcript").read_bytes().splitlines()) == 2
    summary_text = summary.read_text(encoding="utf-8")
    assert summary_text.splitlines()[0] == headline
    assert (artifacts / "report.json").read_text(encoding="utf-8").startswith("{\n")


def test_report_when_no_hook_fires(tmp_path: Path, monkeypatch) -> None:
    """A host that fires nothing still writes the headline and its exit code.

    Mutation: drop the headline, the step summary, or the recorded exit
    code, or return the host's status from run. This test fails.
    """

    _install_stub(tmp_path, monkeypatch)
    monkeypatch.setenv("STUB_MODE", "silent")
    monkeypatch.setenv("STUB_EXIT", "3")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    artifacts = tmp_path / "artifacts"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "run", str(artifacts), str(tmp_path / "temp")],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    report = json.loads((artifacts / "report.json").read_text(encoding="utf-8"))
    assert report["headline"] == "Neither SessionStart nor SessionEnd fired."
    assert report["exit_code"] == 3
    assert report["first_stdout_line"] == "silent-stdout"
    assert report["events"]["SessionStart"]["fired"] == 0
    assert report["events"]["SessionEnd"]["fired"] == 0
    assert summary.read_text(encoding="utf-8").splitlines()[0] == report["headline"]


def test_double_fire_counts_two(tmp_path: Path, monkeypatch) -> None:
    """Two invocations of one hook read as 2, not as one overwritten file."""

    _install_stub(tmp_path, monkeypatch)
    monkeypatch.setenv("STUB_MODE", "double")
    artifacts = tmp_path / "artifacts"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "run", str(artifacts), str(tmp_path / "temp")],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    report = json.loads((artifacts / "report.json").read_text(encoding="utf-8"))
    assert report["events"]["SessionStart"]["fired"] == 2
    assert report["events"]["SessionEnd"]["fired"] == 2
    assert (artifacts / "SessionStart.fires").read_text(encoding="utf-8").splitlines() == ["1", "1"]


def test_run_refuses_a_non_empty_artifacts_directory(tmp_path: Path, monkeypatch) -> None:
    """Stale files cannot look like a fresh firing."""

    _install_stub(tmp_path, monkeypatch)
    artifacts = tmp_path / "artifacts"
    first = subprocess.run(
        [sys.executable, str(SCRIPT), "run", str(artifacts), str(tmp_path / "temp")],
        capture_output=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr.decode()
    original = (artifacts / "report.json").read_bytes()
    second = subprocess.run(
        [sys.executable, str(SCRIPT), "run", str(artifacts), str(tmp_path / "temp-2")],
        capture_output=True,
        check=False,
    )
    assert second.returncode == 1
    assert b"non-empty" in second.stderr
    assert (artifacts / "report.json").read_bytes() == original


def test_timeout_still_writes_the_report(tmp_path: Path, monkeypatch) -> None:
    """TimeoutExpired becomes exit_code timeout and the report is still written.

    Mutation: let TimeoutExpired escape, or skip the summary. This test fails.
    """

    _install_stub(tmp_path, monkeypatch)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    module = _load_trial()
    real_run = module.subprocess.run

    def fake_run(args, **kwargs):
        if isinstance(args, list) and args and args[0] == "claude" and "-p" in args:
            raise subprocess.TimeoutExpired(args, 120, output=b"Not logged in\n", stderr=b"")
        return real_run(args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    artifacts = tmp_path / "artifacts"
    try:
        code = module.run(artifacts, tmp_path / "temp")
    except subprocess.TimeoutExpired:
        raise AssertionError("timeout was not recorded") from None
    assert code == 0
    report = json.loads((artifacts / "report.json").read_text(encoding="utf-8"))
    assert report["exit_code"] == "timeout"
    assert report["first_stdout_line"] == "Not logged in"
    assert report["headline"] == "Neither SessionStart nor SessionEnd fired."
    assert summary.read_text(encoding="utf-8").splitlines()[0] == report["headline"]
