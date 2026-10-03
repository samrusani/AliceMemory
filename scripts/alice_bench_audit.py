#!/usr/bin/env python3
"""alice_bench_audit: flag every answering run that used a tool other than the wrapper.

Tier 2 answering agents get one way to search, the ``search`` command of
``alice_bench.py``. A run that read a file, ran a raw shell command, called the
tool of another server or committed anything is outside the protocol. This script
reads each run's tool-use record and decides, deterministically and without a
model:

* a flagged run is graded "not answered" for its arm. It is not re-run, because
  agents break out when a search fails, and re-running voided runs would leave
  each arm's surviving runs conditioned differently;
* the void rate of each arm is reported;
* a run lost to an infrastructure crash (a timeout, not a policy break) may be
  re-run. It is recorded separately, and the same rule applies to every arm.

A run record is a JSON object::

    {"run_id": "A-q007-1", "arm": "A", "question_id": "q007", "repeat": 1,
     "tool_calls": [{"name": "Bash", "input": {"command": "..."}}],
     "infrastructure_failure": null}

``tool_calls`` may be replaced by ``"transcript": "path/to/run.jsonl"``, a transcript
with one JSON object per line whose ``message.content`` blocks of type ``tool_use``
carry ``name`` and ``input``. The runs file holds a JSON list or one run per line.

What counts as the wrapper is given on the command line: ``--allow-tool`` names a
tool exactly, and ``--allow-bash-regex`` is a full-match pattern for the command of a
shell tool call, so the template fixes the arm, the run directory and the state
directory and an agent cannot add ``--budget`` or point at another state directory.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

DEFAULT_SHELL_TOOLS = ("Bash",)
NOT_ANSWERED = "not_answered"


def tool_calls_from_transcript(lines: Iterable[str]) -> list[dict[str, Any]]:
    """The tool calls in a transcript, in order.

    A line is read when it is a JSON object. Assistant messages carry their tool
    calls as ``tool_use`` blocks in ``message.content``. A line with its own
    ``tool_calls`` list is read too. Lines that are not JSON are skipped, because a
    transcript can hold progress text, and the audit only needs the calls.
    """

    calls: list[dict[str, Any]] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            entry = json.loads(text)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), list):
            for block in message["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    calls.append({"name": str(block.get("name", "")), "input": block.get("input") or {}})
        extra = entry.get("tool_calls")
        if isinstance(extra, list):
            for call in extra:
                if isinstance(call, dict):
                    calls.append({"name": str(call.get("name", "")), "input": call.get("input") or {}})
    return calls


def classify_call(
    call: Mapping[str, Any],
    *,
    allowed_tools: Sequence[str],
    allowed_bash: Sequence[re.Pattern[str]],
    shell_tools: Sequence[str],
) -> str | None:
    """``None`` when the call is the wrapper, else the reason it is not."""

    name = str(call.get("name", ""))
    if name in allowed_tools:
        return None
    if name in shell_tools:
        arguments = call.get("input") or {}
        command = arguments.get("command") if isinstance(arguments, dict) else None
        if not isinstance(command, str):
            return "shell_call_without_command"
        if any(pattern.fullmatch(command.strip()) for pattern in allowed_bash):
            return None
        return "shell_command_not_the_wrapper"
    return "tool_not_allowed"


def audit_run(
    run: Mapping[str, Any],
    *,
    allowed_tools: Sequence[str],
    allowed_bash: Sequence[re.Pattern[str]],
    shell_tools: Sequence[str] = DEFAULT_SHELL_TOOLS,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """The verdict for one run."""

    if "tool_calls" in run:
        calls = [dict(call) for call in run["tool_calls"]]
    elif "transcript" in run:
        path = Path(str(run["transcript"]))
        if base_dir is not None and not path.is_absolute():
            path = base_dir / path
        calls = tool_calls_from_transcript(path.read_text(encoding="utf-8").splitlines())
    else:
        raise ValueError(f"run {run.get('run_id')} has neither tool_calls nor transcript")
    violations: list[dict[str, Any]] = []
    wrapper_calls = 0
    for index, call in enumerate(calls):
        reason = classify_call(call, allowed_tools=allowed_tools, allowed_bash=allowed_bash, shell_tools=shell_tools)
        if reason is None:
            wrapper_calls += 1
        else:
            violations.append({"index": index, "tool": str(call.get("name", "")), "reason": reason})
    flagged = bool(violations)
    infrastructure = run.get("infrastructure_failure")
    return {
        "run_id": run.get("run_id"),
        "arm": run.get("arm"),
        "question_id": run.get("question_id"),
        "tool_calls": len(calls),
        "wrapper_calls": wrapper_calls,
        "violations": violations,
        "flagged": flagged,
        # A policy break stands. It is graded, never re-run.
        "graded_as": NOT_ANSWERED if flagged else None,
        "infrastructure_failure": infrastructure,
        "rerun_allowed": bool(infrastructure) and not flagged,
    }


def audit_runs(
    runs: Sequence[Mapping[str, Any]],
    *,
    allowed_tools: Sequence[str],
    allowed_bash: Sequence[re.Pattern[str]],
    shell_tools: Sequence[str] = DEFAULT_SHELL_TOOLS,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    verdicts = [
        audit_run(
            run,
            allowed_tools=allowed_tools,
            allowed_bash=allowed_bash,
            shell_tools=shell_tools,
            base_dir=base_dir,
        )
        for run in runs
    ]
    arms: dict[str, dict[str, Any]] = {}
    for verdict in verdicts:
        arm = str(verdict["arm"])
        cell = arms.setdefault(arm, {"runs": 0, "flagged": 0, "infrastructure_failures": 0})
        cell["runs"] += 1
        cell["flagged"] += 1 if verdict["flagged"] else 0
        cell["infrastructure_failures"] += 1 if verdict["infrastructure_failure"] else 0
    for cell in arms.values():
        cell["void_rate"] = cell["flagged"] / cell["runs"] if cell["runs"] else 0.0
    return {
        "runs": verdicts,
        "arms": arms,
        "flagged_runs": [v["run_id"] for v in verdicts if v["flagged"]],
        # Recorded apart from the flagged runs: only a crash that is not a policy break.
        "infrastructure_reruns": [v["run_id"] for v in verdicts if v["rerun_allowed"]],
    }


def load_runs(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("["):
        loaded = json.loads(text)
        if not isinstance(loaded, list):
            raise ValueError("the runs file must hold a list")
        return [dict(item) for item in loaded]
    return [dict(json.loads(line)) for line in text.splitlines() if line.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Flag answering runs that used a tool other than the wrapper.")
    parser.add_argument("--runs", required=True, help="JSON list, or one run record per line.")
    parser.add_argument("--allow-tool", action="append", default=[], help="Exact name of a wrapper tool. Repeatable.")
    parser.add_argument(
        "--allow-bash-regex",
        action="append",
        default=[],
        help="Full-match pattern for the command of a shell call that is the wrapper. Repeatable.",
    )
    parser.add_argument("--shell-tool", action="append", default=None, help="Name of a shell tool. Default: Bash.")
    parser.add_argument("--out", default=None, help="Write the audit as JSON here.")
    parser.add_argument("--fail-on-flagged", action="store_true", help="Exit 1 when any run is flagged.")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    runs_path = Path(args.runs)
    patterns = [re.compile(pattern) for pattern in args.allow_bash_regex]
    report = audit_runs(
        load_runs(runs_path),
        allowed_tools=list(args.allow_tool),
        allowed_bash=patterns,
        shell_tools=tuple(args.shell_tool) if args.shell_tool else DEFAULT_SHELL_TOOLS,
        base_dir=runs_path.parent,
    )
    encoded = json.dumps(report, indent=1, sort_keys=True)
    if args.out:
        Path(args.out).write_text(encoded + "\n", encoding="utf-8")
    for arm, cell in sorted(report["arms"].items()):
        print(f"arm {arm}: {cell['runs']} runs, {cell['flagged']} flagged, void rate {cell['void_rate']:.1%}, "
              f"{cell['infrastructure_failures']} infrastructure failures")
    print(f"flagged runs: {len(report['flagged_runs'])}; infrastructure re-runs allowed: {len(report['infrastructure_reruns'])}")
    return 1 if args.fail_on_flagged and report["flagged_runs"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
