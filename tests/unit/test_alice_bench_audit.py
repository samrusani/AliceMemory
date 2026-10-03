"""The transcript audit (scripts/alice_bench_audit.py): any call that is not the wrapper voids a run.

Tier 2 answering agents get one way to search. This script reads each run's tool-use record and
flags every call that is anything else, so the isolation of the answerers is checked by a program
and not by a person reading transcripts. A flagged run is graded "not answered" for its arm and is
never re-run. A run lost to an infrastructure crash may be re-run, and is recorded apart.

Every test names the mutation that must fail it. Test ids (TH13) follow the spec of the
search-quality release.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import scripts.alice_bench_audit as audit

WRAPPER = re.compile(
    r"python3 scripts/alice_bench\.py search --arm alice --run-dir /runs/r1 --state-dir /runs/r1/state/[a-z0-9-]+ "
    r"--query '[^']*'"
)
ALLOWED_TOOLS = ["mcp__bench__search"]


def _run(run_id: str, arm: str, calls: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"run_id": run_id, "arm": arm, "question_id": "q1", "tool_calls": calls, **extra}


def _bash(command: str) -> dict[str, Any]:
    return {"name": "Bash", "input": {"command": command}}


GOOD = _bash("python3 scripts/alice_bench.py search --arm alice --run-dir /runs/r1 --state-dir /runs/r1/state/a-q1-1 --query 'spare key'")


def _audit(runs: list[dict[str, Any]], tmp_path: Path | None = None) -> dict[str, Any]:
    return audit.audit_runs(runs, allowed_tools=ALLOWED_TOOLS, allowed_bash=[WRAPPER], base_dir=tmp_path)


def test_a_run_that_only_calls_the_wrapper_is_clean() -> None:
    """The wrapper, by tool name or by its exact command, is the only thing a run may call.

    Mutation: make ``classify_call`` flag the wrapper itself (for example compare the command with
    ``match`` against the wrong pattern).
    """

    report = _audit([_run("A-1", "A", [GOOD, {"name": "mcp__bench__search", "input": {"query": "x"}}, GOOD])])
    verdict = report["runs"][0]
    assert verdict["flagged"] is False and verdict["violations"] == []
    assert verdict["wrapper_calls"] == 3 and verdict["graded_as"] is None
    assert report["flagged_runs"] == [] and report["arms"]["A"]["void_rate"] == 0.0


def test_any_call_other_than_the_wrapper_flags_the_run_and_grades_it_not_answered() -> None:
    """TH13. A file read, a raw shell call, a second server's tool and a commit each void the run.

    Mutation: let a flagged run pass (leave ``graded_as`` empty), or re-run it silently (list it
    under the infrastructure re-runs or set ``rerun_allowed``).
    """

    breakers = {
        "file read": {"name": "Read", "input": {"file_path": "/repo/wiki/notes.md"}},
        "raw shell": _bash("grep -r secret ."),
        "second server": {"name": "mcp__alice-dev__alice_recall", "input": {"query": "x"}},
        "commit": {"name": "mcp__alice-dev__alice_memory_commit", "input": {"title": "t"}},
        "wrapper with a wider budget": _bash(
            "python3 scripts/alice_bench.py search --arm alice --run-dir /runs/r1 --state-dir /runs/r1/state/a --query 'x' --budget 99"
        ),
        "wrapper with a pipe": _bash(
            "python3 scripts/alice_bench.py search --arm alice --run-dir /runs/r1 --state-dir /runs/r1/state/a --query 'x' | head"
        ),
        "wrapper on another state dir": _bash(
            "python3 scripts/alice_bench.py search --arm alice --run-dir /runs/r1 --state-dir /tmp/fresh --query 'x'"
        ),
        "shell without a command": {"name": "Bash", "input": {}},
    }
    runs = [_run(f"G-{index}", "G", [GOOD, call], infrastructure_failure="timeout") for index, call in enumerate(breakers.values())]
    runs.append(_run("A-clean", "A", [GOOD]))
    report = _audit(runs)
    for verdict, name in zip(report["runs"][:-1], breakers):
        assert verdict["flagged"] is True, name
        assert verdict["graded_as"] == "not_answered", name
        assert verdict["rerun_allowed"] is False, name
        assert [v["index"] for v in verdict["violations"]] == [1], name
        assert verdict["wrapper_calls"] == 1
    assert report["flagged_runs"] == [f"G-{index}" for index in range(len(breakers))]
    assert report["infrastructure_reruns"] == [], "a policy break stands even when the run also crashed"
    assert report["arms"]["G"] == {
        "runs": len(breakers),
        "flagged": len(breakers),
        "infrastructure_failures": len(breakers),
        "void_rate": 1.0,
    }
    reasons = {v["reason"] for verdict in report["runs"] for v in verdict["violations"]}
    assert reasons == {"tool_not_allowed", "shell_command_not_the_wrapper", "shell_call_without_command"}


def test_the_void_rate_is_reported_per_arm_and_infrastructure_reruns_are_recorded_apart() -> None:
    """TH13. A crash that is not a policy break may be re-run, and is listed separately.

    Mutation: count infrastructure failures as flagged runs (or drop them from the re-run list), or
    compute the void rate over all arms together.
    """

    runs = [
        _run("A-1", "A", [GOOD]),
        _run("A-2", "A", [GOOD, {"name": "Read", "input": {}}]),
        _run("A-3", "A", [GOOD], infrastructure_failure="timeout"),
        _run("G-1", "G", [GOOD]),
        _run("G-2", "G", [GOOD]),
    ]
    report = _audit(runs)
    assert report["arms"]["A"] == {"runs": 3, "flagged": 1, "infrastructure_failures": 1, "void_rate": 1 / 3}
    assert report["arms"]["G"]["void_rate"] == 0.0
    assert report["flagged_runs"] == ["A-2"]
    assert report["infrastructure_reruns"] == ["A-3"]
    by_id = {verdict["run_id"]: verdict for verdict in report["runs"]}
    assert by_id["A-3"]["flagged"] is False and by_id["A-3"]["graded_as"] is None
    assert by_id["A-3"]["infrastructure_failure"] == "timeout"


def test_tool_calls_are_read_from_a_transcript_file(tmp_path: Path) -> None:
    """A run can point at a transcript: assistant ``tool_use`` blocks and ``tool_calls`` lists are read.

    Mutation: read only the first tool call of a message, or skip lines that carry ``tool_calls``.
    """

    transcript = tmp_path / "run.jsonl"
    lines = [
        "not json, a progress line",
        json.dumps({"type": "user", "message": {"role": "user", "content": "question"}}),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I will search."},
                        {"type": "tool_use", "name": "Bash", "input": {"command": GOOD["input"]["command"]}},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
                    ],
                },
            }
        ),
        json.dumps({"tool_calls": [{"name": "WebFetch", "input": {"url": "https://example.test"}}]}),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    calls = audit.tool_calls_from_transcript(transcript.read_text().splitlines())
    assert [call["name"] for call in calls] == ["Bash", "Read", "WebFetch"]
    report = _audit([{"run_id": "A-9", "arm": "A", "transcript": "run.jsonl"}], tmp_path)
    verdict = report["runs"][0]
    assert verdict["tool_calls"] == 3 and verdict["wrapper_calls"] == 1
    assert [v["tool"] for v in verdict["violations"]] == ["Read", "WebFetch"]


def test_the_command_reads_runs_and_writes_the_audit(tmp_path: Path, capsys: Any) -> None:
    """The script as a command: it prints the void rate per arm and can fail on a flagged run.

    Mutation: ignore ``--fail-on-flagged``, or drop ``--allow-bash-regex`` from the classification.
    """

    runs_path = tmp_path / "runs.json"
    runs_path.write_text(
        json.dumps([_run("A-1", "A", [GOOD]), _run("A-2", "A", [{"name": "Read", "input": {}}])]), encoding="utf-8"
    )
    out = tmp_path / "audit.json"
    args = ["--runs", str(runs_path), "--allow-bash-regex", WRAPPER.pattern, "--out", str(out)]
    assert audit.main(args) == 0
    printed = capsys.readouterr().out
    assert "arm A: 2 runs, 1 flagged, void rate 50.0%" in printed
    assert json.loads(out.read_text())["flagged_runs"] == ["A-2"]
    assert audit.main([*args, "--fail-on-flagged"]) == 1
    jsonl = tmp_path / "runs.jsonl"
    jsonl.write_text("\n".join(json.dumps(run) for run in [_run("A-1", "A", [GOOD])]) + "\n", encoding="utf-8")
    assert audit.main(["--runs", str(jsonl), "--allow-bash-regex", WRAPPER.pattern, "--fail-on-flagged"]) == 0
