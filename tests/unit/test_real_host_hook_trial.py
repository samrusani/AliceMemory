"""The dispatch-only hook trial saves stdin and does not import Alice in hook mode."""

from __future__ import annotations

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
        settings = json.loads(Path(home, ".claude", "settings.json").read_text())
        for event in ("SessionStart", "SessionEnd"):
            for group in settings.get("hooks", {{}}).get(event, []):
                handlers = group.get("hooks") if isinstance(group, dict) else None
                if not isinstance(handlers, list):
                    continue
                for handler in handlers:
                    command = handler.get("command") if isinstance(handler, dict) else None
                    if not isinstance(command, str):
                        continue
                    subprocess.run(command, shell=True, input=b'{{"transcript_path":""}}\\n', check=False)
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
