"""Dispatch-only trial: do SessionStart and SessionEnd fire with no login?

``run`` installs the Claude Code hook groups and asks claude for one word.
``hook`` is the command those groups run. It uses the standard library only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_UNSET = (
    "CLAUDE_CONFIG_DIR",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
_PINNED_VERSION = "2.1.281 (Claude Code)"
_EVENTS = ("SessionStart", "SessionEnd")
_NEITHER = "Neither SessionStart nor SessionEnd fired."


def _stdin_path(artifacts: Path, event: str) -> Path:
    return artifacts / f"{event}.stdin"


def _timing_path(artifacts: Path, event: str) -> Path:
    return artifacts / f"{event}.timing.json"


def _transcript_path(artifacts: Path, event: str) -> Path:
    return artifacts / f"{event}.transcript"


def hook(event: str, artifacts: Path) -> int:
    """Save stdin exactly, copy transcript_path when it exists, and exit 0."""

    started = time.time_ns()
    raw = sys.stdin.buffer.read()
    artifacts.mkdir(parents=True, exist_ok=True)
    _stdin_path(artifacts, event).write_bytes(raw)
    payload_keys: list[str] = []
    copied = False
    line_count = 0
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        payload_keys = [str(key) for key in payload]
        transcript = payload.get("transcript_path")
        if isinstance(transcript, str) and transcript and Path(transcript).is_file():
            destination = _transcript_path(artifacts, event)
            shutil.copyfile(transcript, destination)
            copied = True
            line_count = len(Path(transcript).read_bytes().splitlines())
    elapsed_ns = time.time_ns() - started
    _timing_path(artifacts, event).write_text(
        json.dumps(
            {
                "started_ns": started,
                "in_process_ms": elapsed_ns / 1_000_000,
                "payload_keys": payload_keys,
                "copied": copied,
                "transcript_lines": line_count,
            }
        ),
        encoding="utf-8",
    )
    return 0


def _quoted_command(artifacts: Path, event: str) -> str:
    import shlex

    parts = (sys.executable, str(Path(__file__).resolve()), "hook", event, str(artifacts))
    return " ".join(shlex.quote(part) for part in parts)


def _child_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in _UNSET:
        env.pop(name, None)
    env["HOME"] = str(home)
    return env


def _event_report(artifacts: Path, event: str, launch_ns: int | None) -> dict[str, object]:
    stdin_file = _stdin_path(artifacts, event)
    timing_file = _timing_path(artifacts, event)
    if not stdin_file.is_file():
        return {
            "fired": 0,
            "payload_keys": [],
            "in_process_ms": None,
            "replay_ms": None,
            "ms_after_launch": None,
            "transcript_lines": 0,
        }
    timing = json.loads(timing_file.read_text(encoding="utf-8")) if timing_file.is_file() else {}
    started = timing.get("started_ns")
    after_launch = None
    if isinstance(started, int) and launch_ns is not None:
        after_launch = (started - launch_ns) / 1_000_000
    replay_started = time.perf_counter()
    subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "hook", event, str(artifacts)],
        input=stdin_file.read_bytes(),
        check=False,
    )
    replay_ms = (time.perf_counter() - replay_started) * 1000
    # The replay rewrites the timing file. The in-process figure above is the
    # original hook. Read the copied transcript after the replay as well.
    transcript = _transcript_path(artifacts, event)
    lines = len(transcript.read_bytes().splitlines()) if transcript.is_file() else 0
    keys = timing.get("payload_keys")
    return {
        "fired": 1,
        "payload_keys": keys if isinstance(keys, list) else [],
        "in_process_ms": timing.get("in_process_ms"),
        "replay_ms": replay_ms,
        "ms_after_launch": after_launch,
        "transcript_lines": lines if lines else timing.get("transcript_lines") or 0,
    }


def run(artifacts: Path, temp: Path) -> int:
    """Point claude at the trial hooks and write report.json."""

    from alicebot_api.host_install import claude_code_session_start_group

    artifacts.mkdir(parents=True, exist_ok=True)
    home = temp / "trial-home"
    project = temp / "trial-project"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    project.mkdir(parents=True, exist_ok=True)
    settings = {
        "hooks": {
            event: [claude_code_session_start_group(_quoted_command(artifacts, event))]
            for event in _EVENTS
        }
    }
    (home / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    env = _child_env(home)
    version = subprocess.run(
        ["claude", "--version"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    help_run = subprocess.run(
        ["claude", "--help"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    (artifacts / "claude-version.txt").write_text(version.stdout, encoding="utf-8")
    (artifacts / "claude-help.txt").write_text(help_run.stdout, encoding="utf-8")
    version_text = version.stdout.strip()
    if version_text != _PINNED_VERSION:
        return 1
    launch_ns = time.time_ns()
    started = time.perf_counter()
    completed = subprocess.run(
        ["claude", "-p", "Reply with the single word ok."],
        cwd=project,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=env,
        timeout=120,
        check=False,
    )
    wall_ms = (time.perf_counter() - started) * 1000
    stderr_lines = completed.stderr.decode("utf-8", errors="replace").splitlines()
    events = {event: _event_report(artifacts, event, launch_ns) for event in _EVENTS}
    fired = [event for event, record in events.items() if record["fired"]]
    if not fired:
        headline = _NEITHER
    else:
        headline = " ".join(f"{event} fired {events[event]['fired']} time." for event in _EVENTS)
    report = {
        "headline": headline,
        "claude_version": version_text,
        "exit_code": completed.returncode,
        "first_stderr_line": stderr_lines[0] if stderr_lines else "",
        "wall_ms": wall_ms,
        "events": events,
    }
    text = json.dumps(report, indent=2) + "\n"
    (artifacts / "report.json").write_text(text, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        Path(summary).write_text(headline + "\n\n" + text, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 3 and args[0] == "hook":
        return hook(args[1], Path(args[2]))
    if len(args) == 3 and args[0] == "run":
        return run(Path(args[1]), Path(args[2]))
    print("usage: real_host_hook_trial.py hook <event> <artifacts>", file=sys.stderr)
    print("       real_host_hook_trial.py run <artifacts> <temp>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
