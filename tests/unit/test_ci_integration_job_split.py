"""The PostgreSQL integration job runs as shards behind a summary job that keeps the required name.

The job named "Integration tests (Postgres + pgvector, role separation)" ran every file of
``tests/integration`` in one process, then four SQLite read-budget files of ``tests/performance``,
and a pull request waited about an hour for it. It is now parallel shard jobs over
``tests/integration``, the SQLite budget files in jobs of their own, and a final job that has the
old name, runs whatever the others did and fails unless each of them succeeded. The main ruleset
requires that name, and a skipped required check counts as passing, so the summary has to run on
failure and has to fail on a skip. These tests read the workflow file and run the summary's
script. They do not call GitHub. The proof in real CI is in the pull request.

The two smoke checks ("Default surface integration smoke (Postgres)" and "Read-budget maximum
workload smoke (Postgres)") stay in the ``python-integration`` job, which has its own required
names and is not part of the summary's verdict.

Mutation notes live on each test. A miss raises AssertionError.
"""

from __future__ import annotations

import collections
import fnmatch
import glob
from pathlib import Path
import re
import shlex
import subprocess

import pytest
import yaml

import scripts.check_github_release_checks as release_checks

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "tests.yml"
REQUIRED_NAME = "Integration tests (Postgres + pgvector, role separation)"
DEFAULT_SURFACE_NAME = "Default surface integration smoke (Postgres)"
MAXIMUM_SMOKE_NAME = "Read-budget maximum workload smoke (Postgres)"
SUMMARY_JOB = "python-integration-summary"
SHARDS_JOB = "python-integration-shards"
BUDGETS_JOB = "python-sqlite-read-budgets"
SMOKE_JOB = "python-integration"
VERDICT_JOBS = (SHARDS_JOB, BUDGETS_JOB)
INTEGRATION_DIR = REPO_ROOT / "tests" / "integration"
PERFORMANCE_DIR = REPO_ROOT / "tests" / "performance"
MERGE_BASE_STEP = "Merge base with main for the read-budget gate"
UPLOAD_STEP = "Retain paired read-budget samples, including failed runs"
SHARD_STEP = "Integration tests of this shard"
MAXIMUM_SMOKE_STEP = "Maximum-workload read-budget controls"
# The weekly parent budget test sets its own per-test limit with a marker; the smoke step uses the same number.
WEEKLY_PARENT_TEST = REPO_ROOT / "tests" / "integration" / "test_weekly_parent_read_budget_postgres.py"
BUDGET_STEP = "SQLite random mixed-parent and keyless read budgets against the merge base"
# The source text of a test file that writes samples to ALICE_READ_BUDGET_EVIDENCE, directly or
# through a helper of tests/performance that does.
EVIDENCE_WRITERS = re.compile(r"\b(append_evidence|paired_budgets|assert_budgets|paired_resume_budgets)\(")
NAME_TEMPLATE_KEY = re.compile(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}")


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


def _parts() -> list[dict]:
    include = _job(BUDGETS_JOB)["strategy"]["matrix"]["include"]
    assert isinstance(include, list)
    return sorted(include, key=lambda entry: entry["part"])


def _needs(job_id: str) -> list[str]:
    needs = _job(job_id).get("needs")
    return [needs] if isinstance(needs, str) else list(needs or [])


def _keys(node: object) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {key for child in node.values() for key in _keys(child)}
    if isinstance(node, list):
        return {key for child in node for key in _keys(child)}
    return set()


def _job_names(job_id: str) -> list[str]:
    """Every check run name a job produces, the matrix keys filled in."""

    job = _job(job_id)
    template = job["name"]
    include = (job.get("strategy") or {}).get("matrix", {}).get("include")
    matrix_lists = {key: value for key, value in (job.get("strategy") or {}).get("matrix", {}).items() if key != "include"}
    if include:
        names = []
        for entry in include:
            name = template
            for key in NAME_TEMPLATE_KEY.findall(template):
                name = name.replace("${{ matrix." + key + " }}", str(entry[key]))
            names.append(name)
        return names
    keys = NAME_TEMPLATE_KEY.findall(template)
    if not keys:
        return [template]
    names = [template]
    for key in keys:
        names = [name.replace("${{ matrix." + key + " }}", str(value)) for name in names for value in matrix_lists[key]]
    return names


# --- the required name -----------------------------------------------------------------------


def test_one_job_has_the_required_name_and_it_is_the_summary() -> None:
    """The ruleset context is produced by exactly one job, the summary, and by no other.

    The ruleset and the exact-SHA release check both match a check run by name. Two jobs with
    the name would let the later conclusion decide, and a renamed summary would leave the
    context waiting forever. The two smoke checks are still produced by the matrix of the
    ``python-integration`` job, each once, under the names the ruleset and the release check hold.

    Mutation: rename the summary, give a shard or the budget job the same name, put the name back
    into the ``python-integration`` matrix, drop the summary job, or rename a smoke check.
    """

    jobs = _workflow()["jobs"]
    produced = collections.Counter(name for job_id in jobs for name in _job_names(job_id))
    assert produced[REQUIRED_NAME] == 1
    named = [job_id for job_id, job in jobs.items() if job.get("name") == REQUIRED_NAME]
    assert named == [SUMMARY_JOB]
    assert REQUIRED_NAME in release_checks.REQUIRED_CHECKS
    assert REQUIRED_NAME in release_checks.BRANCH_PROTECTION_REQUIRED_CHECKS
    assert sorted(_job_names(SMOKE_JOB)) == sorted([DEFAULT_SURFACE_NAME, MAXIMUM_SMOKE_NAME])
    assert produced[DEFAULT_SURFACE_NAME] == produced[MAXIMUM_SMOKE_NAME] == 1
    assert DEFAULT_SURFACE_NAME in release_checks.REQUIRED_CHECKS
    assert DEFAULT_SURFACE_NAME in release_checks.BRANCH_PROTECTION_REQUIRED_CHECKS
    assert MAXIMUM_SMOKE_NAME in release_checks.REQUIRED_CHECKS
    assert MAXIMUM_SMOKE_NAME not in release_checks.BRANCH_PROTECTION_REQUIRED_CHECKS


def test_the_shard_and_budget_names_are_distinct_and_in_no_required_list() -> None:
    """Each shard and budget check run has its own name, and none of them is required.

    The ruleset and the exact-SHA release check name the summary only. A shard or budget name in
    either list would have to be required in the ruleset too, and one more check would then block
    every pull request.

    Mutation: write a name template without its matrix key, add a shard or budget name to
    ``REQUIRED_CHECKS`` or to ``BRANCH_PROTECTION_REQUIRED_CHECKS``, or give a budget part the
    name of a shard.
    """

    shard_template = _job(SHARDS_JOB)["name"]
    assert "${{ matrix.shard }}" in shard_template
    assert f"of {len(_shards())} " in shard_template
    part_template = _job(BUDGETS_JOB)["name"]
    assert "${{ matrix.part }}" in part_template
    assert f"of {len(_parts())}" in part_template
    new_names = [*_job_names(SHARDS_JOB), *_job_names(BUDGETS_JOB)]
    assert len(set(new_names)) == len(new_names) == len(_shards()) + len(_parts())
    other_names = [name for job_id in _workflow()["jobs"] if job_id not in (SHARDS_JOB, BUDGETS_JOB) for name in _job_names(job_id)]
    assert not set(new_names) & set(other_names)
    assert not set(new_names) & set(release_checks.REQUIRED_CHECKS)
    assert not set(new_names) & set(release_checks.BRANCH_PROTECTION_REQUIRED_CHECKS)


def test_the_summary_runs_on_every_outcome_and_needs_every_job_that_carries_a_test() -> None:
    """The summary cannot be skipped and waits for the shards and the budget job.

    A job that is skipped (its needs failed) never reports, and a required context that is
    skipped counts as passing, so ``always()`` is what keeps a failed shard from turning the
    required check green by silence.

    Mutation: drop ``if: always()``, use ``success()`` or ``!cancelled()`` instead, or drop one of
    the two jobs from ``needs``.
    """

    summary = _job(SUMMARY_JOB)
    assert summary["if"] == "always()"
    assert sorted(_needs(SUMMARY_JOB)) == sorted(VERDICT_JOBS)
    assert "strategy" not in summary and "continue-on-error" not in summary
    for job_id in VERDICT_JOBS:
        assert job_id in _workflow()["jobs"]
        assert _needs(job_id) == []


def test_no_integration_job_can_turn_its_own_failure_into_a_success() -> None:
    """A failure inside a shard or a budget part reaches the summary as ``failure``.

    ``continue-on-error`` turns a failed job or step into a green one, and a job or step ``if``
    can skip the work that proves something. The one step that has an ``if`` is the upload of the
    samples, which runs on every outcome so a failed run keeps them.

    Mutation: add ``continue-on-error: true`` to a job or to a step, or an ``if`` to a job or to
    the pytest step, the merge base step or the install step of a shard or of the budget job.
    """

    for job_id in VERDICT_JOBS:
        job = _job(job_id)
        assert "continue-on-error" not in _keys(job), job_id
        assert "if" not in job, job_id
        for step in _steps(job_id):
            if step.get("name") == UPLOAD_STEP:
                assert str(step["if"]).startswith("always()"), job_id
                assert "actions/upload-artifact@" in step["uses"]
            else:
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
    assert mapped == set(VERDICT_JOBS) == set(_needs(SUMMARY_JOB))
    assert len(env) == len(VERDICT_JOBS)
    script = _summary_script()
    for name, expression in env.items():
        job_id = re.fullmatch(r"\$\{\{ needs\.([\w-]+)\.result \}\}", expression).group(1)  # type: ignore[union-attr]
        assert f'"{job_id}=${name}"' in script


def test_the_summary_passes_only_when_every_job_succeeded() -> None:
    """Both ``success`` passes, and nothing else does, a skip included.

    Mutation: test only for ``failure`` instead of requiring ``success``, tolerate ``skipped``, or
    end the script with ``exit 0``.
    """

    everything = {job_id: "success" for job_id in VERDICT_JOBS}
    passed = _run_summary(everything)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    for job_id in VERDICT_JOBS:
        for result in ("failure", "cancelled", "skipped", ""):
            completed = _run_summary({**everything, job_id: result})
            assert completed.returncode == 1, (job_id, result, completed.stdout)
            assert f"{job_id} finished as" in completed.stdout


def test_the_summary_names_every_job_that_did_not_succeed() -> None:
    """Two bad jobs are both named in the log, so the first fix does not hide the second.

    Mutation: leave the loop at the first bad job.
    """

    completed = _run_summary({SHARDS_JOB: "failure", BUDGETS_JOB: "skipped"})
    assert completed.returncode == 1
    assert f"{SHARDS_JOB} finished as 'failure'" in completed.stdout
    assert f"{BUDGETS_JOB} finished as 'skipped'" in completed.stdout
    only_one = _run_summary({SHARDS_JOB: "success", BUDGETS_JOB: "failure"})
    assert only_one.returncode == 1
    assert SHARDS_JOB + " finished" not in only_one.stdout


# --- the shards ------------------------------------------------------------------------------


def _pytest_collects(path: Path) -> bool:
    """Pytest's default ``python_files`` patterns, which this repository does not override."""

    return fnmatch.fnmatch(path.name, "test_*.py") or fnmatch.fnmatch(path.name, "*_test.py")


def _test_files(directory: Path) -> list[str]:
    """Every file ``pytest <directory>`` would collect tests from, relative to the repository."""

    return sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in directory.rglob("*.py")
        if "__pycache__" not in path.parts and _pytest_collects(path)
    )


def _parse(selection: str, directory: str) -> tuple[list[str], str | None]:
    """The file patterns of a selection and its one keyword expression, if it has one.

    A selection is file patterns that bash expands, and at most ``-k`` with one expression. A
    keyword applies to every test of its command, so a selection that has one holds one file.
    """

    tokens = shlex.split(selection)
    patterns: list[str] = []
    keyword: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "-k":
            assert keyword is None and index + 1 < len(tokens), selection
            keyword = tokens[index + 1]
            index += 2
            continue
        assert token.startswith(directory) and token.endswith(".py"), token
        assert not token.startswith("-"), token
        patterns.append(token)
        index += 1
    return patterns, keyword


def _expand(patterns: list[str]) -> dict[str, list[str]]:
    """What bash does to the patterns: every pattern becomes the files it matches, sorted."""

    return {
        token: sorted(Path(match).relative_to(REPO_ROOT).as_posix() for match in glob.glob(str(REPO_ROOT / token)))
        for token in patterns
    }


def _held(entry: dict, directory: str) -> set[str]:
    patterns, _keyword = _parse(entry["selection"], directory)
    return {match for matches in _expand(patterns).values() for match in matches}


def _is_complement(first: str, second: str) -> bool:
    """True when one keyword is ``not`` the other, bare or in parentheses, so together they select every test once."""

    def negates(positive: str, negative: str) -> bool:
        return negative in (f"not {positive}", f"not ({positive})")

    return negates(first, second) or negates(second, first)


def _check_selections(entries: list[dict], key: str, directory: str, everything: list[str]) -> None:
    """Every file of ``everything`` is held by exactly one entry, or by the two entries of a keyword pair.

    ``key`` is the matrix key that numbers the entries (``shard`` or ``part``).
    """

    owners: dict[str, list[tuple[int, str | None]]] = collections.defaultdict(list)
    for entry in entries:
        patterns, keyword = _parse(entry["selection"], directory)
        assert patterns, entry[key]
        for token, matches in _expand(patterns).items():
            assert matches, f"pattern {token} of {key} {entry[key]} matches no file, bash would pass it literally"
            for match in matches:
                assert _pytest_collects(Path(match)), f"{match} is not a test file pytest collects by default"
                owners[match].append((entry[key], keyword))
    # A file matched twice inside one entry is still one file; the same file in two entries is the defect.
    claims_by_file = {name: sorted(set(claims), key=lambda claim: (claim[0], claim[1] or "")) for name, claims in owners.items()}

    outside = [name for name in everything if name not in claims_by_file]
    twice: dict[str, list[tuple[int, str | None]]] = {}
    for name, claims in claims_by_file.items():
        if len(claims) == 1 and claims[0][1] is None:
            continue
        if len(claims) == 2 and claims[0][1] is not None and claims[1][1] is not None and _is_complement(claims[0][1], claims[1][1]):
            continue
        twice[name] = claims
    extra = sorted(set(claims_by_file) - set(everything))
    assert outside == [], f"files in no {key}: {outside}"
    assert twice == {}, f"files in more than one {key}, or split by keywords that are not each other's complement: {twice}"
    assert extra == []


def test_the_selections_are_plain_file_patterns_and_a_keyword_entry_holds_one_file() -> None:
    """The model of a shard (bash expands file patterns, pytest collects those files) holds.

    Nothing may narrow a shard that the model cannot see: no ``python_files``, ``testpaths`` or
    ``addopts`` override that changes what a file argument collects (the unit split test pins
    ``pyproject.toml``), no option but ``-k``, no directory and no node id in a selection. ``-k``
    applies to every test of its command, so an entry that has one holds exactly one file, and
    the two entries that hold that file carry ``K`` and ``not K``, which together select every
    test of it once.

    Mutation: add a directory, a node id or an option such as ``-x`` to a selection, give a
    ``-k`` entry a second file, or write the second keyword as something other than the
    complement of the first.
    """

    for entries, key, directory in ((_shards(), "shard", "tests/integration/"), (_parts(), "part", "tests/performance/")):
        keyword_files: dict[str, int] = collections.Counter()
        for entry in entries:
            patterns, keyword = _parse(entry["selection"], directory)
            assert patterns, (key, entry[key])
            for token in patterns:
                assert re.fullmatch(directory + r"[\w\[\]\-!*?]+\.py", token), token
            if keyword is not None:
                held = _held(entry, directory)
                assert len(held) == 1, f"{key} {entry[key]} has -k and more than one file"
                keyword_files[next(iter(held))] += 1
        assert keyword_files, f"no {key} splits a file by keyword; delete this rule or restore the split"
        assert set(keyword_files.values()) == {2}, keyword_files


def test_the_shards_are_disjoint_and_together_cover_every_integration_test_file_once() -> None:
    """Every file of tests/integration that pytest collects is in exactly one shard.

    This is what keeps a new test file from falling outside every shard: the file is matched
    here the day it is added, and a file that no pattern matches, or two shards match, fails
    this test in the pull request that adds it. A file that is split by keyword is the one
    exception, and it must be held by exactly the two shards that carry ``K`` and ``not K``. Whole
    files are the unit of selection, so equal file sets give equal collected node ids.

    Mutation: drop a pattern from one shard (its files are then in none), widen a pattern so it
    also matches a file of another shard, add a test file that no pattern matches, or let the
    pattern of a shard match the file that two other shards split by keyword.
    """

    everything = _test_files(INTEGRATION_DIR)
    assert everything, "no integration test files found"
    _check_selections(_shards(), "shard", "tests/integration/", everything)


def test_no_shard_is_empty_and_the_shards_are_numbered_in_order() -> None:
    """The shards are numbered 1 to N and each runs at least one file.

    Mutation: leave a shard with a selection that matches nothing, skip a number, or repeat one.
    """

    shards = _shards()
    assert [entry["shard"] for entry in shards] == list(range(1, len(shards) + 1))
    assert len(shards) >= 2
    for entry in shards:
        patterns, _keyword = _parse(entry["selection"], "tests/integration/")
        assert any(_expand(patterns).values()), entry["shard"]


def test_every_shard_runs_its_own_selection_with_the_flags_of_the_old_step() -> None:
    """The pytest step of a shard runs the matrix selection with the legacy flag and the old bounds.

    The step is the old "Integration tests" step with its file argument replaced by the selection
    and its session bound set per shard: it has to stay under the job limit by the time the upload
    needs.

    Mutation: run ``tests/integration`` whole in a shard, drop ``ALICE_LEGACY_SURFACES=1``,
    ``--timeout=180`` or ``--session-timeout``, or raise the session bound past the job limit.
    """

    step = _step(SHARDS_JOB, SHARD_STEP)
    run = " ".join(step["run"].split())
    assert run.startswith(
        "ALICE_LEGACY_SURFACES=1 ./.venv/bin/python -m pytest ${{ matrix.selection }} -q -p no:cacheprovider "
        "--durations=20 --timeout=180 --session-timeout="
    )
    assert "tests/integration " not in run + " "
    session = int(re.search(r"--session-timeout=(\d+)$", run).group(1))  # type: ignore[union-attr]
    limit = _job(SHARDS_JOB)["timeout-minutes"]
    assert 600 <= session and session + 5 * 60 <= limit * 60, (session, limit)
    assert step["env"] == {
        "DATABASE_URL": "postgresql://alicebot_app:ci@localhost:5432/alicebot",
        "DATABASE_ADMIN_URL": "postgresql://alicebot_admin:ci@localhost:5432/alicebot",
        "ALICE_READ_MERGE_BASE_CHECKOUT": "${{ github.workspace }}/.read-budget-merge-base",
        "ALICE_READ_BUDGET_EVIDENCE": "${{ runner.temp }}/round3-postgres-read-budgets.jsonl",
    }


def test_the_maximum_workload_smoke_gives_each_test_the_limit_of_the_weekly_parent_budget() -> None:
    """The four tests of the maximum-workload step get the per-test limit that the weekly parent budget test sets for
    itself, and the job leaves room for all of them.

    The step reads the largest fixtures on three revisions and writes read profiles, and
    ``test_postgres_round3_random_read_budgets[True-3000-extra-rows]`` ran past the 180 seconds of the shards twice, once
    in the test and once in the teardown that drops its database. The weekly parent test sets
    ``pytest.mark.timeout(600)`` for the same reason, and the step uses the number that marker holds: the two are
    compared, so one cannot move without the other. The tests run one after another, so the worst case is the number of
    node ids times the limit, and it has to leave the setup and the upload room inside ``timeout-minutes`` of the job.
    A session bound, if the step ever gets one, has to hold at least one test and end before the job does.

    Mutation: set the step's ``--timeout`` back to 180, to 601 or drop it; change the number of the marker in the weekly
    test; set ``timeout-minutes`` of the job to 40 or less; add a ``--session-timeout`` below the per-test limit.
    """

    run = " ".join(_step(SMOKE_JOB, MAXIMUM_SMOKE_STEP)["run"].split())
    limit = re.search(r"--timeout=(\d+)(?!\d)", run)
    assert limit is not None, run
    weekly = re.search(r"@pytest\.mark\.timeout\((\d+)\)", WEEKLY_PARENT_TEST.read_text(encoding="utf-8"))
    assert weekly is not None
    assert int(limit.group(1)) == int(weekly.group(1)) == 600
    nodes = re.findall(r"'tests/integration/[^']+'", run)
    assert len(nodes) == 4, nodes
    job_seconds = _job(SMOKE_JOB)["timeout-minutes"] * 60
    assert len(nodes) * 600 + 15 * 60 <= job_seconds, job_seconds
    session = re.search(r"--session-timeout=(\d+)", run)
    if session is not None:
        assert 600 <= int(session.group(1)) and int(session.group(1)) + 5 * 60 <= job_seconds


def _same_steps_as_the_smoke_job(job_id: str, names: tuple[str, ...]) -> None:
    for name in names:
        assert _step(job_id, name)["run"] == _step(SMOKE_JOB, name)["run"], (job_id, name)


def test_each_shard_keeps_the_job_environment_of_the_old_job() -> None:
    """Same runner, Postgres service, Python, install, role bootstrap and merge base as the single job.

    The service, the bootstrap and the merge base step are compared with the ones the
    ``python-integration`` smoke job still has, so the two cannot drift apart.

    Mutation: change the Python version, the image digest or the app role, drop ``fetch-depth: 0``
    (the merge base needs the history), drop the merge base step or the dev install.
    """

    job = _job(SHARDS_JOB)
    smoke = _job(SMOKE_JOB)
    assert job["runs-on"] == smoke["runs-on"] == "ubuntu-latest"
    assert job["strategy"]["fail-fast"] is False
    assert job["services"] == smoke["services"]
    steps = _steps(SHARDS_JOB)
    assert steps[0]["uses"].startswith("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1")
    assert steps[0]["with"] == {"fetch-depth": 0}
    assert any(step.get("uses", "").startswith("actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0") and step["with"] == {"python-version": "3.12", "cache": "pip"} for step in steps)
    _same_steps_as_the_smoke_job(
        SHARDS_JOB,
        (MERGE_BASE_STEP, "Install (repo-local venv, integration tests spawn scripts that require .venv)", "Bootstrap extension and app role"),
    )
    names = [step.get("name") for step in steps]
    assert names.index(MERGE_BASE_STEP) < names.index(SHARD_STEP)
    assert names.index("Bootstrap extension and app role") < names.index(SHARD_STEP)
    assert "CREATE ROLE alicebot_app LOGIN PASSWORD 'ci'" in _step(SHARDS_JOB, "Bootstrap extension and app role")["run"]


def test_no_shard_measures_the_prelabels_checkout_or_applies_the_absolute_gate() -> None:
    """The shards compare the head with the merge base only, as the old required step did.

    The pre-labels checkout and the fixed-second limits belong to the maximum-workload smoke job.

    Mutation: set ``ALICE_READ_MAIN_CHECKOUT`` or ``ALICE_READ_ABSOLUTE_GATE`` on a shard or on a
    budget part, in the environment or in the command.
    """

    for job_id, name in ((SHARDS_JOB, SHARD_STEP), (BUDGETS_JOB, BUDGET_STEP)):
        step = _step(job_id, name)
        assert "ALICE_READ_MAIN_CHECKOUT" not in step["env"], job_id
        assert "ALICE_READ_MAIN_CHECKOUT" not in _job(job_id).get("env", {}), job_id
        for variable in ("ALICE_READ_ABSOLUTE_GATE", "ALICE_READ_ABSOLUTE_LIMIT_SECONDS"):
            assert variable not in step["env"] and variable not in step["run"], (job_id, variable)
            assert variable not in _job(job_id).get("env", {}), (job_id, variable)
        assert step["env"]["ALICE_READ_MERGE_BASE_CHECKOUT"].endswith("/.read-budget-merge-base")


# --- the SQLite read budgets -----------------------------------------------------------------


def test_every_sqlite_budget_file_runs_in_exactly_one_budget_part() -> None:
    """The four SQLite read-budget files of tests/performance each run, once, in some part.

    These files compare the head with the merge base on SQLite and had no job of their own: they
    were the second step of the old required job, so a split that forgot them would pass. The
    round-three file is split by keyword between two parts, as in the shards.

    Mutation: drop a file from the selection of a part, list a file in two parts, or add a test
    file to tests/performance that no part selects.
    """

    everything = _test_files(PERFORMANCE_DIR)
    assert everything == sorted(
        f"tests/performance/{name}"
        for name in (
            "test_label_read_handoff_budget.py",
            "test_label_round2_read_budget.py",
            "test_label_round3_read_budget.py",
            "test_resume_hidden_rows_budget.py",
        )
    )
    _check_selections(_parts(), "part", "tests/performance/", everything)
    assert [entry["part"] for entry in _parts()] == list(range(1, len(_parts()) + 1))


def test_a_budget_part_needs_no_database_and_measures_the_merge_base() -> None:
    """The SQLite budget job has no Postgres service and no database URL, and compares with the merge base.

    The files read ``ALICE_READ_MERGE_BASE_CHECKOUT`` and write ``ALICE_READ_BUDGET_EVIDENCE``;
    their Postgres twins live in tests/integration and run in the shards.

    Mutation: add a ``services`` block or a ``DATABASE_URL`` to the budget job, drop the merge
    base step, or point the evidence variable at the Postgres file name.
    """

    job = _job(BUDGETS_JOB)
    assert job["runs-on"] == "ubuntu-latest" and job["strategy"]["fail-fast"] is False
    assert "services" not in job
    step = _step(BUDGETS_JOB, BUDGET_STEP)
    assert not [key for key in step["env"] if key.startswith("DATABASE")]
    assert step["env"]["ALICE_READ_BUDGET_EVIDENCE"] == "${{ runner.temp }}/round3-sqlite-read-budgets.jsonl"
    assert step["env"]["ALICE_READ_MERGE_BASE_CHECKOUT"] == "${{ github.workspace }}/.read-budget-merge-base"
    run = " ".join(step["run"].split())
    assert run.startswith("./.venv/bin/python -m pytest ${{ matrix.selection }} -q --timeout=180 --session-timeout=")
    session = int(re.search(r"--session-timeout=(\d+)$", run).group(1))  # type: ignore[union-attr]
    assert 600 <= session and session + 5 * 60 <= _job(BUDGETS_JOB)["timeout-minutes"] * 60
    steps = _steps(BUDGETS_JOB)
    assert steps[0]["with"] == {"fetch-depth": 0}
    _same_steps_as_the_smoke_job(
        BUDGETS_JOB,
        (MERGE_BASE_STEP, "Install (repo-local venv, integration tests spawn scripts that require .venv)"),
    )
    names = [item.get("name") for item in steps]
    assert names.index(MERGE_BASE_STEP) < names.index(BUDGET_STEP)


# --- the evidence ----------------------------------------------------------------------------


def _upload_names() -> list[tuple[str, str]]:
    """Every artifact name the workflow can upload in one run, matrix keys filled in, as (job id, name)."""

    found: list[tuple[str, str]] = []
    for job_id, job in _workflow()["jobs"].items():
        include = (job.get("strategy") or {}).get("matrix", {}).get("include")
        for step in _steps(job_id):
            if "upload-artifact" not in str(step.get("uses")):
                continue
            template = step["with"]["name"]
            if "${{ matrix." not in template:
                found.append((job_id, template))
                continue
            keys = NAME_TEMPLATE_KEY.findall(template)
            assert include and all(all(key in entry for key in keys) for entry in include), (job_id, template)
            for entry in include:
                name = template
                for key in keys:
                    name = name.replace("${{ matrix." + key + " }}", str(entry[key]))
                found.append((job_id, name))
    return found


def test_no_two_uploads_of_one_run_can_have_the_same_artifact_name() -> None:
    """The artifact names of a run are all different, and a rerun of a job gets a new one.

    ``strategy.job-index`` counts the entries of one matrix, so two jobs both gave a name ending
    in ``-0-``; GitHub refuses a second upload of a name in one run. Each upload below has the
    job's own words in its name and ``github.run_attempt``, which a rerun raises.

    Mutation: go back to ``strategy.job-index`` in any name, drop the shard number from the shard
    name or the part number from the part name, drop ``github.run_attempt`` from one, or give two
    jobs the same prefix.
    """

    names = _upload_names()
    only_budget_uploads = [(job_id, name) for job_id, name in names if "read-budgets" in name]
    assert {job_id for job_id, _ in only_budget_uploads} == {SMOKE_JOB, SHARDS_JOB, BUDGETS_JOB}
    rendered = [name for _, name in only_budget_uploads]
    assert len(set(rendered)) == len(rendered), rendered
    assert "job-index" not in WORKFLOW_PATH.read_text(encoding="utf-8")
    for job_id, step in ((job_id, step) for job_id in (SMOKE_JOB, SHARDS_JOB, BUDGETS_JOB) for step in _steps(job_id) if step.get("name") == UPLOAD_STEP):
        assert "${{ github.run_attempt }}" in step["with"]["name"], job_id
    # Within a job whose matrix has several entries, the key that tells the entries apart is in the name.
    assert "${{ matrix.shard }}" in _step(SHARDS_JOB, UPLOAD_STEP)["with"]["name"]
    assert "${{ matrix.part }}" in _step(BUDGETS_JOB, UPLOAD_STEP)["with"]["name"]


def test_every_job_that_writes_samples_uploads_them_on_every_outcome() -> None:
    """The upload runs after a failure, fails if the file a job should have written is missing, and does not outlive a week.

    Mutation: drop ``always()`` from an upload, switch ``if-no-files-found`` to ``warn`` or
    ``ignore`` in a job that always writes samples, or upload a file the job does not write.
    """

    budget_upload = _step(BUDGETS_JOB, UPLOAD_STEP)
    shard_upload = _step(SHARDS_JOB, UPLOAD_STEP)
    smoke_upload = _step(SMOKE_JOB, UPLOAD_STEP)
    for upload in (budget_upload, shard_upload, smoke_upload):
        assert upload["if"].startswith("always()")
        assert upload["uses"].startswith("actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9")
        assert upload["with"]["if-no-files-found"] == "error"
        assert upload["with"]["retention-days"] == 7
    assert budget_upload["if"] == "always()"
    assert str(budget_upload["with"]["path"]).strip() == "${{ runner.temp }}/round3-sqlite-read-budgets.jsonl"
    assert shard_upload["if"] == "always() && matrix.read_budget_evidence"
    assert str(shard_upload["with"]["path"]).strip() == "${{ runner.temp }}/round3-postgres-read-budgets.jsonl"
    # The smoke job writes the Postgres samples and the profiles, and never the SQLite samples.
    smoke_paths = [line.strip() for line in str(smoke_upload["with"]["path"]).splitlines() if line.strip()]
    assert "${{ runner.temp }}/round3-postgres-read-budgets.jsonl" in smoke_paths
    assert "${{ runner.temp }}/round3-read-profiles/" in smoke_paths
    assert not [path for path in smoke_paths if "sqlite" in path]


def test_the_shards_that_hold_a_test_that_writes_samples_upload_them_and_no_other_does() -> None:
    """``read_budget_evidence`` is true exactly for the shards that run a file that writes samples.

    A shard with no such file has no samples file, and ``if-no-files-found: error`` would fail it,
    so it must not upload. A shard with one that skipped the upload would lose the samples that
    the paired comparison is read from. A file writes samples when it calls the helper that does,
    directly or through the budget helpers of tests/performance.

    Mutation: move a file that calls ``paired_budgets``, ``assert_budgets``,
    ``paired_resume_budgets`` or ``append_evidence`` into a shard whose flag is false, or set the
    flag of the shard that has none to true.
    """

    writers = {
        path
        for path in _test_files(INTEGRATION_DIR)
        if EVIDENCE_WRITERS.search((REPO_ROOT / path).read_text(encoding="utf-8"))
    }
    assert writers >= {
        "tests/integration/test_label_read_handoff_budget_postgres.py",
        "tests/integration/test_label_round2_read_budget_postgres.py",
        "tests/integration/test_label_round3_read_budget_postgres.py",
        "tests/integration/test_resume_hidden_rows_budget_postgres.py",
    }, writers
    for entry in _shards():
        patterns, _keyword = _parse(entry["selection"], "tests/integration/")
        held = {match for matches in _expand(patterns).values() for match in matches}
        assert entry["read_budget_evidence"] is bool(held & writers), (entry["shard"], sorted(held & writers))


# --- the limits ------------------------------------------------------------------------------


@pytest.mark.parametrize("job_id", (SHARDS_JOB, BUDGETS_JOB, SUMMARY_JOB))
def test_every_new_job_has_a_timeout_and_the_shards_and_parts_leave_room_for_a_slow_runner(job_id: str) -> None:
    """A hung job stops in minutes, and a slow runner is not cut off.

    The old job had 120 minutes. The summary only reads results, so it gets 5. A shard or part gets
    more than twice the minutes the measurement in ``tests.yml`` gives it and less than an hour.

    Mutation: remove ``timeout-minutes`` from a job, set the summary above 10, or a shard or part
    below 25 or above 60.
    """

    timeout = _job(job_id)["timeout-minutes"]
    assert isinstance(timeout, int)
    if job_id == SUMMARY_JOB:
        assert 0 < timeout <= 10
    else:
        assert 25 <= timeout <= 60


def test_the_release_guide_names_the_summary_job_and_the_rule_for_the_shards() -> None:
    """RELEASING.md tells the next maintainer which job carries the required name and what not to do.

    Mutation: delete the paragraph, rename the job in it, or drop the sentence that says to keep
    ``if: always()`` or the one that says not to require the shard jobs one by one.
    """

    text = " ".join((REPO_ROOT / "RELEASING.md").read_text(encoding="utf-8").split())
    marker = f"The required check named `{REQUIRED_NAME}` is the summary job of the PostgreSQL integration tests in `tests.yml`."
    assert text.count(marker) == 1
    # The paragraph of the integration split, not the one of the unit split before it.
    paragraph = text.split(marker, 1)[1].split("If that readback reports drift", 1)[0]
    assert "Keep its name and its `if: always()`" in paragraph
    assert "do not require the shard or budget jobs one by one" in paragraph
    assert "a skipped required check counts as passing" in paragraph
    assert "tests/unit/test_ci_integration_job_split.py" in paragraph
    assert "`Default surface integration smoke (Postgres)`" in paragraph and "`Read-budget maximum workload smoke (Postgres)`" in paragraph


def test_the_exact_sha_check_reads_only_the_summary_and_the_extra_check_runs_do_not_matter() -> None:
    """The release check passes with the shard check runs present and follows the summary name.

    With every job green the check passes. With the summary red and every shard green it fails
    on the summary, and a missing summary fails as a missing check, so the extra check runs
    neither help nor hurt it.

    Mutation: add a shard or budget name to ``REQUIRED_CHECKS`` (the names would then have to be
    required in the ruleset too).
    """

    extra = [*_job_names(SHARDS_JOB), *_job_names(BUDGETS_JOB)]
    green = [
        {"name": name, "id": index, "conclusion": "success"}
        for index, name in enumerate([*release_checks.REQUIRED_CHECKS, *extra])
    ]
    assert release_checks.validate_check_runs(green) == []

    red_summary = [dict(run, conclusion="failure") if run["name"] == REQUIRED_NAME else run for run in green]
    assert release_checks.validate_check_runs(red_summary) == [
        f"latest required exact-SHA check did not succeed: {REQUIRED_NAME} (failure)"
    ]

    no_summary = [run for run in green if run["name"] != REQUIRED_NAME]
    assert release_checks.validate_check_runs(no_summary) == [f"missing required exact-SHA check: {REQUIRED_NAME}"]

    only_shards_red = [dict(run, conclusion="failure") if run["name"] in extra else run for run in green]
    assert release_checks.validate_check_runs(only_shards_red) == []
    assert not set(extra) & set(release_checks.REQUIRED_CHECKS)
    assert not set(extra) & set(release_checks.BRANCH_PROTECTION_REQUIRED_CHECKS)
