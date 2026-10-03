"""The SQLite unit job runs as shards behind a summary job that keeps the required check name.

The job named "Unit tests + live eval battery (SQLite)" ran every unit test, the eval battery
and the coverage gate in one place and reached 17 minutes 50 seconds against a 20 minute limit.
It is now parallel shard jobs, one eval job and one coverage job, and a final job that has the
old name, runs whatever the others did and fails unless each of them succeeded. The main
ruleset requires that name, and a skipped required check counts as passing, so the summary has
to run on failure and has to fail on a skip. These tests read the workflow file and run the
summary's script. They do not call GitHub. The proof in real CI is in the pull request.

Mutation notes live on each test. A miss raises AssertionError.
"""

from __future__ import annotations

import collections
import fnmatch
import glob
from pathlib import Path
import re
import subprocess
import tomllib

import pytest
import yaml

import scripts.check_github_release_checks as release_checks
import scripts.combine_python_coverage as combine

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "tests.yml"
PUBLISH_PATH = REPO_ROOT / ".github" / "workflows" / "publish-pypi.yml"
REQUIRED_NAME = "Unit tests + live eval battery (SQLite)"
SUMMARY_JOB = "python-unit"
SHARDS_JOB = "python-unit-shards"
EVAL_JOB = "python-eval-battery"
COVERAGE_JOB = "python-unit-coverage"
UNIT_JOBS = (SHARDS_JOB, EVAL_JOB, COVERAGE_JOB)
UNIT_DIR = REPO_ROOT / "tests" / "unit"


def _workflow() -> dict:
    loaded = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _job(job_id: str) -> dict:
    job = _workflow()["jobs"].get(job_id)
    assert isinstance(job, dict), job_id
    return job


def _steps(job_id: str) -> list[dict]:
    steps = _job(job_id).get("steps")
    assert isinstance(steps, list)
    return [step for step in steps if isinstance(step, dict)]


def _step(job_id: str, name: str) -> dict:
    found = [step for step in _steps(job_id) if step.get("name") == name]
    assert len(found) == 1, (job_id, name)
    return found[0]


def _shards() -> list[dict]:
    include = _job(SHARDS_JOB)["strategy"]["matrix"]["include"]
    assert isinstance(include, list)
    return sorted(include, key=lambda entry: entry["shard"])


def _needs(job_id: str) -> list[str]:
    needs = _job(job_id).get("needs")
    return [needs] if isinstance(needs, str) else list(needs or [])


def _keys(node: object) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {key for child in node.values() for key in _keys(child)}
    if isinstance(node, list):
        return {key for child in node for key in _keys(child)}
    return set()


# --- the required name -----------------------------------------------------------------------


def test_one_job_has_the_required_name_and_it_is_the_summary() -> None:
    """The ruleset context is produced by exactly one job, the summary, and by no other.

    The ruleset and the exact-SHA release check both match a check run by name. Two jobs with
    the name would let the later conclusion decide, and a renamed summary would leave the
    context waiting forever.

    Mutation: rename the summary, give a shard or the eval job the same name, or drop the
    summary job.
    """

    jobs = _workflow()["jobs"]
    named = [job_id for job_id, job in jobs.items() if job.get("name") == REQUIRED_NAME]
    assert named == [SUMMARY_JOB]
    assert REQUIRED_NAME in release_checks.REQUIRED_CHECKS
    assert REQUIRED_NAME in release_checks.BRANCH_PROTECTION_REQUIRED_CHECKS


def test_the_shard_names_are_distinct_and_none_is_the_required_name() -> None:
    """Each shard check run has its own name, so a shard can be told from the summary.

    Mutation: write the name template without ``matrix.shard``.
    """

    template = _job(SHARDS_JOB)["name"]
    assert "${{ matrix.shard }}" in template
    shard_names = [template.replace("${{ matrix.shard }}", str(entry["shard"])) for entry in _shards()]
    other_names = [job["name"] for job_id, job in _workflow()["jobs"].items() if job_id != SHARDS_JOB]
    assert len(set(shard_names)) == len(shard_names) == len(_shards())
    assert not set(shard_names) & set(other_names)
    assert REQUIRED_NAME not in shard_names
    assert f"of {len(_shards())}" in template


def test_the_summary_runs_on_every_outcome_and_needs_every_unit_job() -> None:
    """The summary cannot be skipped and waits for every job that carries a test.

    A job that is skipped (its needs failed) never reports, and a required context that is
    skipped counts as passing, so ``always()`` is what keeps a failed shard from turning the
    required check green by silence.

    Mutation: drop ``if: always()``, use ``success()`` or ``!cancelled()`` instead, or drop one of
    the three jobs from ``needs``.
    """

    summary = _job(SUMMARY_JOB)
    assert summary["if"] == "always()"
    assert sorted(_needs(SUMMARY_JOB)) == sorted(UNIT_JOBS)
    assert "strategy" not in summary and "continue-on-error" not in summary
    # Every job that carries a unit test or an eval is in the verdict.
    for job_id in UNIT_JOBS:
        assert job_id in _workflow()["jobs"]
    assert _needs(COVERAGE_JOB) == [SHARDS_JOB]


def test_no_unit_job_can_turn_its_own_failure_into_a_success() -> None:
    """A failure inside a unit job reaches the summary as ``failure``.

    ``continue-on-error`` turns a failed job or step into a green one, and a job or step ``if``
    can skip the work that proves something.

    Mutation: add ``continue-on-error: true`` to a job or to a step, or an ``if`` to a job or to
    any step of the shard, eval or coverage job.
    """

    for job_id in UNIT_JOBS:
        job = _job(job_id)
        assert "continue-on-error" not in _keys(job), job_id
        assert "if" not in job, job_id
        for step in _steps(job_id):
            assert "if" not in step, (job_id, step.get("name"))


def _summary_script() -> str:
    steps = _steps(SUMMARY_JOB)
    assert len(steps) == 1
    script = steps[0]["run"]
    assert isinstance(script, str)
    return script


def _run_summary(results: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the summary's own script with the results GitHub would hand it."""

    env_template = _job(SUMMARY_JOB)["env"]
    env = {"PATH": "/usr/bin:/bin"}
    for name, expression in env_template.items():
        job_id = re.fullmatch(r"\$\{\{ needs\.([\w-]+)\.result \}\}", expression).group(1)  # type: ignore[union-attr]
        env[name] = results[job_id]
    return subprocess.run(["bash", "-c", _summary_script()], env=env, capture_output=True, text=True, check=False)


def test_the_summary_script_reads_the_result_of_every_needed_job() -> None:
    """Each needed job's result reaches the script, and the script names the same jobs.

    Mutation: map a result variable to the wrong job, leave a needed job out of the ``env`` or
    out of the script's loop.
    """

    env = _job(SUMMARY_JOB)["env"]
    mapped = {re.fullmatch(r"\$\{\{ needs\.([\w-]+)\.result \}\}", value).group(1) for value in env.values()}  # type: ignore[union-attr]
    assert mapped == set(UNIT_JOBS) == set(_needs(SUMMARY_JOB))
    script = _summary_script()
    for name, expression in env.items():
        job_id = re.fullmatch(r"\$\{\{ needs\.([\w-]+)\.result \}\}", expression).group(1)  # type: ignore[union-attr]
        assert f'"{job_id}=${name}"' in script


def test_the_summary_passes_only_when_every_job_succeeded() -> None:
    """All three ``success`` passes, and nothing else does.

    Mutation: test only for ``failure`` instead of requiring ``success``, tolerate ``skipped``, or
    end the script with ``exit 0``.
    """

    everything = {job_id: "success" for job_id in UNIT_JOBS}
    passed = _run_summary(everything)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    for job_id in UNIT_JOBS:
        for result in ("failure", "cancelled", "skipped", ""):
            completed = _run_summary({**everything, job_id: result})
            assert completed.returncode == 1, (job_id, result, completed.stdout)
            assert f"{job_id} finished as" in completed.stdout


def test_the_summary_names_every_job_that_did_not_succeed() -> None:
    """Two bad jobs are both named in the log, so the first fix does not hide the second.

    Mutation: leave the loop at the first bad job.
    """

    completed = _run_summary({SHARDS_JOB: "failure", EVAL_JOB: "success", COVERAGE_JOB: "skipped"})
    assert completed.returncode == 1
    assert f"{SHARDS_JOB} finished as 'failure'" in completed.stdout
    assert f"{COVERAGE_JOB} finished as 'skipped'" in completed.stdout
    assert EVAL_JOB + " finished" not in completed.stdout


# --- the shards ------------------------------------------------------------------------------


def _pytest_collects(path: Path) -> bool:
    """Pytest's default ``python_files`` patterns, which this repository does not override."""

    return fnmatch.fnmatch(path.name, "test_*.py") or fnmatch.fnmatch(path.name, "*_test.py")


def _unit_test_files() -> list[str]:
    """Every file ``pytest tests/unit`` would collect tests from, relative to the repository."""

    return sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in UNIT_DIR.rglob("*.py")
        if "__pycache__" not in path.parts and _pytest_collects(path)
    )


def _expand(selection: str) -> dict[str, list[str]]:
    """What bash does to a selection: every pattern becomes the files it matches, sorted."""

    expanded: dict[str, list[str]] = {}
    for token in selection.split():
        assert token.startswith("tests/unit/") and token.endswith(".py"), token
        assert not token.startswith("-"), token
        expanded[token] = sorted(
            Path(match).relative_to(REPO_ROOT).as_posix() for match in glob.glob(str(REPO_ROOT / token))
        )
    return expanded


def test_pytest_collects_by_its_defaults_and_the_selections_are_plain_file_patterns() -> None:
    """The model of a shard (bash expands file patterns, pytest collects those files) holds.

    Nothing may narrow a shard that the model cannot see: no ``python_files``, ``testpaths`` or
    ``addopts`` override that changes what a file argument collects, and no selection token that
    is an option, a directory or a node id.

    Mutation: add ``python_files`` or ``addopts`` to ``[tool.pytest.ini_options]``, or add
    ``-k`` or a directory to a selection.
    """

    options = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
    assert set(options) == {"pythonpath", "testpaths"}
    for entry in _shards():
        for token in entry["selection"].split():
            assert re.fullmatch(r"tests/unit/[\w\[\]\-!*?]+\.py", token), token


def test_the_shards_are_disjoint_and_together_cover_every_unit_test_file_once() -> None:
    """Every file of tests/unit that pytest collects is in exactly one shard.

    This is what keeps a new test file from falling outside every shard: the file is matched
    here the day it is added, and a file that no pattern matches, or two shards match, fails
    this test in the pull request that adds it. Whole files are the unit of selection, so equal
    file sets give equal collected node ids (``pytest --collect-only`` per shard, unioned, was
    compared with the full collection for the pull request that split the job).

    Mutation: drop a pattern from one shard (its files are then in none), widen a pattern so it
    also matches a file of another shard, or add a test file that no pattern matches.
    """

    owners: dict[str, list[int]] = collections.defaultdict(list)
    for entry in _shards():
        for token, matches in _expand(entry["selection"]).items():
            assert matches, f"pattern {token} of shard {entry['shard']} matches no file, bash would pass it literally"
            for match in matches:
                assert _pytest_collects(Path(match)), f"{match} is not a test file pytest collects by default"
                owners[match].append(entry["shard"])
    # A file matched twice inside one shard is still one file; the same file in two shards is the defect.
    owners = {name: sorted(set(shards)) for name, shards in owners.items()}

    everything = _unit_test_files()
    assert everything, "no unit test files found"
    outside = [name for name in everything if name not in owners]
    twice = {name: shards for name, shards in owners.items() if len(shards) != 1}
    extra = sorted(set(owners) - set(everything))
    assert outside == [], f"files in no shard: {outside}"
    assert twice == {}, f"files in more than one shard: {twice}"
    assert extra == []


def test_no_shard_is_empty_and_the_shards_are_numbered_in_order() -> None:
    """Three shards numbered 1 to 3 and each runs at least one file.

    Mutation: leave a shard with a selection that matches nothing, skip a number, or repeat one.
    """

    shards = _shards()
    assert [entry["shard"] for entry in shards] == list(range(1, len(shards) + 1))
    assert len(shards) == 3
    for entry in shards:
        assert any(_expand(entry["selection"]).values()), entry["shard"]


def test_every_shard_runs_its_own_selection_with_coverage_and_no_threshold() -> None:
    """The pytest step of the shard runs the matrix selection, measures coverage and sets no floor.

    A floor per shard would fail on the part of the code that the other shards cover. The floor
    is the coverage job's.

    Mutation: run ``tests/unit`` whole in the shard, drop ``--cov=alicebot_api``, or add
    ``--cov-fail-under`` here.
    """

    run = _step(SHARDS_JOB, "Unit tests of this shard with coverage data")["run"]
    normalised = " ".join(run.split())
    assert normalised == "python -m pytest ${{ matrix.selection }} -q --cov=alicebot_api --cov-report="
    assert "tests/unit " not in normalised + " "


def test_each_shard_keeps_the_job_environment_of_the_old_job() -> None:
    """Same runner, Python and install as the single job, and the full history for the tag test.

    Mutation: change the Python version, drop ``fetch-depth: 0`` (a unit test reads release notes
    at their tags), or drop the dev install.
    """

    job = _job(SHARDS_JOB)
    assert job["runs-on"] == "ubuntu-latest"
    assert job["strategy"]["fail-fast"] is False
    steps = _steps(SHARDS_JOB)
    checkout = steps[0]
    assert checkout["uses"].startswith("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1")
    assert checkout["with"] == {"fetch-depth": 0}
    assert steps[1]["with"] == {"python-version": "3.12", "cache": "pip"}
    assert _step(SHARDS_JOB, "Install")["run"] == "python -m pip install -e '.[dev]'"


# --- the eval battery and the other steps ----------------------------------------------------

OLD_STEP_NAMES = (
    "All model-free LongMemEval tests and dataset-manifest consistency",
    "Replay committed LongMemEval evidence",
    "Configured vector-stage contract",
    "Live eval battery on sqlite in-memory (model-free, fts_only)",
    "Canonical release gate fails closed without a vector provider",
)

OLD_COMMANDS = (
    "python -m pytest eval/longmemeval -q",
    "python scripts/check_longmemeval_evidence.py",
    "tests/unit/test_vnext_retrieval.py::test_context_pack_fuses_vector_results_with_rrf_when_provider_is_configured",
    "tests/unit/test_vnext_evals.py::test_retrieval_quality_metrics_against_known_rankings",
    "python -m alicebot_api eval run --suite all > /tmp/eval.json",
    "python -m alicebot_api eval run --suite all --release-gate > /tmp/eval_gate.json",
)


def test_every_other_step_of_the_old_job_runs_in_exactly_one_place() -> None:
    """The LongMemEval tests and evidence replay, the vector-stage contract and both eval runs.

    Each runs once, in the eval job, and nowhere else in the workflow, so a shard neither
    repeats them nor loses them.

    Mutation: delete one of the steps, move it to a shard, or copy it to a second job.
    """

    text = " ".join(WORKFLOW_PATH.read_text(encoding="utf-8").split())
    for command in OLD_COMMANDS:
        assert text.count(command) == 1, command
    eval_names = [step.get("name") for step in _steps(EVAL_JOB)]
    for name in OLD_STEP_NAMES:
        assert eval_names.count(name) == 1, name
        for job_id in (SHARDS_JOB, COVERAGE_JOB, SUMMARY_JOB):
            assert name not in [step.get("name") for step in _steps(job_id)], (job_id, name)
    eval_text = " ".join(str(step.get("run", "")) for step in _steps(EVAL_JOB))
    eval_text = " ".join(eval_text.split())
    for command in OLD_COMMANDS:
        assert command in eval_text, command


def test_the_eval_battery_keeps_its_environment_and_both_assertions() -> None:
    """The fts_only labelling and the fail-closed release gate are still asserted.

    Mutation: drop the in-memory database variable from either eval step, or drop the
    ``fts_only`` assertion or the release-gate failure assertion.
    """

    live = _step(EVAL_JOB, "Live eval battery on sqlite in-memory (model-free, fts_only)")
    gate = _step(EVAL_JOB, "Canonical release gate fails closed without a vector provider")
    assert live["env"] == gate["env"] == {"ALICEBOT_EVAL_DATABASE_URL": "sqlite:///:memory:"}
    assert 'assert retrieval["metrics"]["retrieval_mode"] == "fts_only"' in live["run"]
    assert 'assert report["status"] == "pass"' in live["run"]
    assert 'test "$code" -ne 0' in gate["run"]
    assert 'assert report["status"] == "fail"' in gate["run"]
    assert _steps(EVAL_JOB)[0]["with"] == {"fetch-depth": 0}


# --- the combined coverage -------------------------------------------------------------------


def test_each_shard_writes_its_own_data_file_and_uploads_exactly_it() -> None:
    """The file a shard writes, uploads and the combine step reads have one name.

    Mutation: point ``COVERAGE_FILE`` at a shared name (the shards then overwrite each other's
    data on a shared path or upload the wrong file), change the name in the script or the
    workflow alone, or let the upload tolerate a missing file.
    """

    name = combine.DATA_FILE_TEMPLATE.format(number="${{ matrix.shard }}")
    assert _job(SHARDS_JOB)["env"] == {"COVERAGE_FILE": f"${{{{ github.workspace }}}}/coverage-data/{name}"}
    upload = _steps(SHARDS_JOB)[-1]
    assert upload["uses"].startswith("actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a")
    assert upload["with"]["path"] == f"coverage-data/{name}"
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["name"] == "python-unit-coverage-shard-${{ matrix.shard }}"
    assert upload["with"]["retention-days"] == 1


def test_the_combine_job_downloads_every_shard_and_demands_one_file_per_shard() -> None:
    """The combine job gets all shard artifacts into one folder and is told how many to expect.

    Mutation: change the download pattern so it misses a shard, change ``--shards`` away from
    the number of shards, or point ``--data-dir`` somewhere the download does not write.
    """

    download = next(step for step in _steps(COVERAGE_JOB) if "download-artifact" in str(step.get("uses")))
    assert download["uses"].startswith("actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c")
    assert download["with"]["merge-multiple"] is True
    for entry in _shards():
        artifact = f"python-unit-coverage-shard-{entry['shard']}"
        assert fnmatch.fnmatch(artifact, download["with"]["pattern"]), artifact
    combine_step = _step(COVERAGE_JOB, "Combine the shard coverage data (every shard must have written some)")
    assert combine_step["run"] == (
        f"python scripts/combine_python_coverage.py --data-dir {download['with']['path']} --shards {len(_shards())}"
    )


def test_the_combined_gate_keeps_the_threshold_and_the_per_file_floor_of_the_single_job() -> None:
    """The combined data is held to 50 percent overall and 45 percent on the governed router files.

    The 50 is the ``--cov-fail-under`` the single job passed and the publish workflow still
    passes. The per-file check is the same script, paths and floor, reading the same JSON path.

    Mutation: lower either number, drop a ``--path``, write the JSON somewhere the check does not
    read, or drop the report or xml step.
    """

    gate = _step(COVERAGE_JOB, "Combined coverage baseline")["run"]
    lines = [line.strip() for line in gate.strip().splitlines()]
    assert lines[:3] == [
        "python -m coverage report --fail-under=50",
        "python -m coverage xml",
        "python -m coverage json -o /tmp/alicebot-python-coverage.json",
    ]
    assert len(lines) == 4
    publish = PUBLISH_PATH.read_text(encoding="utf-8")
    assert "--cov-fail-under=50" in publish
    check = lines[3]
    assert check.startswith("python scripts/check_python_coverage.py --coverage-json /tmp/alicebot-python-coverage.json ")
    assert check.endswith(" --min-percent 45")
    publish_check = next(line.strip() for line in publish.splitlines() if "scripts/check_python_coverage.py" in line)
    assert check == publish_check


def test_the_combine_job_installs_what_the_data_was_measured_against() -> None:
    """The combine job needs the package installed (coverage resolves ``source`` by import).

    Mutation: drop the install step, or the setup-python step.
    """

    names = [step.get("name") for step in _steps(COVERAGE_JOB)]
    assert "Install" in names
    assert _step(COVERAGE_JOB, "Install")["run"] == "python -m pip install -e '.[dev]'"
    assert any("setup-python" in str(step.get("uses")) for step in _steps(COVERAGE_JOB))


# --- what the release check sees -------------------------------------------------------------


def test_the_exact_sha_check_reads_only_the_summary_and_the_extra_check_runs_do_not_matter() -> None:
    """The release check passes with the shard check runs present and follows the summary name.

    With every job green the check passes. With the summary red and every shard green it fails
    on the summary, and a missing summary fails as a missing check, so the extra check runs
    neither help nor hurt it.

    Mutation: add a shard name to ``REQUIRED_CHECKS`` (the shard names then would have to be
    required in the ruleset too).
    """

    shard_names = [_job(SHARDS_JOB)["name"].replace("${{ matrix.shard }}", str(e["shard"])) for e in _shards()]
    extra = [*shard_names, _job(EVAL_JOB)["name"], _job(COVERAGE_JOB)["name"]]
    green = [{"name": name, "id": index, "conclusion": "success"} for index, name in enumerate(
        [*release_checks.REQUIRED_CHECKS, *extra]
    )]
    assert release_checks.validate_check_runs(green) == []

    red_summary = [dict(run, conclusion="failure") if run["name"] == REQUIRED_NAME else run for run in green]
    assert release_checks.validate_check_runs(red_summary) == [
        f"latest required exact-SHA check did not succeed: {REQUIRED_NAME} (failure)"
    ]

    no_summary = [run for run in green if run["name"] != REQUIRED_NAME]
    assert release_checks.validate_check_runs(no_summary) == [f"missing required exact-SHA check: {REQUIRED_NAME}"]

    only_shards_red = [dict(run, conclusion="failure") if run["name"] in shard_names else run for run in green]
    assert release_checks.validate_check_runs(only_shards_red) == []
    assert not set(extra) & set(release_checks.REQUIRED_CHECKS)
    assert not set(extra) & set(release_checks.BRANCH_PROTECTION_REQUIRED_CHECKS)


@pytest.mark.parametrize("job_id", UNIT_JOBS)
def test_every_unit_job_has_a_timeout_under_the_limit_of_the_old_job(job_id: str) -> None:
    """A hung shard stops at the limit the old job had, never later.

    Mutation: remove ``timeout-minutes`` from a job or raise it above 20.
    """

    timeout = _job(job_id)["timeout-minutes"]
    assert isinstance(timeout, int) and 0 < timeout <= 20
