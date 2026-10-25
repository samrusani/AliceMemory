"""The publish job that reruns the unit suite has time for the whole suite.

``publish-pypi.yml`` reruns ``pytest tests/unit`` in one process, from the tag,
before it builds anything. CI runs the same suite split into shards, each under
its own ``timeout-minutes``, and the split grew from one job to three, then
four and then five as the suite grew. The publish job kept a 35 minute limit. v0.20.1 was
tagged, its publish run stopped at that limit with the suite 84% done and every
test passing so far, and nothing was built. The workflow runs from the tag, so
that commit can never publish and the number is burned.

v0.20.2 raised the limit to 120 minutes. The one-process rerun then took 3,876 s on
a runner, about 65 minutes, and the release rule is that a release step must take
less than half of its job's limit (``publish-recheck-rehearsal.yml`` fails a run past
it), so 120 minutes was not enough. v0.21.0 raised the limit to 180.

The rule here: a publish job that reruns the unit suite must have at least the
time CI allows the whole suite (shard count times the shard limit) plus a fixed
margin. The margin covers the rest of the job (install, the release checks,
LongMemEval, two builds and the artifact smoke test) and the room the half-limit
rule asks for. Adding a shard, or raising the shard limit, without raising the
publish limit fails this file on the pull request that does it, not at the next
release.

Mutations that must fail this file, each alone:

* set ``timeout-minutes`` of the ``build-and-verify`` job in
  ``publish-pypi.yml`` back to 35, or to 120;
* add a sixth entry to the shard matrix in ``tests.yml``, or raise
  the shard job's ``timeout-minutes`` to 30;
* remove the ``pytest tests/unit`` rerun from ``publish-pypi.yml``;
* rename the shard matrix key ``shard`` or drop the shard job's
  ``timeout-minutes``: the test cannot find the CI limit and fails rather
  than passing on nothing;
* point one entry of the unit shard job at a path outside ``tests/unit``:
  the job is then no longer the unit shard job, and the test fails rather
  than passing on nothing.

Mutation that must not fail this file: the integration shard job of
``tests.yml`` carries the same ``shard`` and ``selection`` keys. It runs
``tests/integration``, which the publish workflow does not rerun, so its
limit is not part of the sum. The test finds the unit shard job by its
``tests/unit`` selections, not by the key alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish-pypi.yml"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"

# Minutes above the CI total (shard count times the shard limit). Twenty are for
# everything in the publish job other than the unit rerun: at v0.20.0 the whole job
# took 11 minutes, the unit rerun included. Sixty are for the half-limit rule: the
# one-process rerun took 3,876 s (65 minutes) against 100 minutes of CI shard limits,
# so the step needs a limit of at least 130 minutes, and the rest keeps room for the
# suite to grow. Five shards of 20 minutes plus 80 is the 180 of the publish job.
MARGIN_MINUTES = 80
# The most a job on a GitHub-hosted runner may run.
HOSTED_JOB_MAX_MINUTES = 360


def _jobs(path: Path) -> dict[str, dict[str, Any]]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    jobs = loaded["jobs"]
    assert isinstance(jobs, dict) and jobs
    return jobs


def _runs(job: dict[str, Any]) -> list[str]:
    return [str(step["run"]) for step in job.get("steps", []) if isinstance(step, dict) and "run" in step]


def _reruns_whole_unit_suite(job: dict[str, Any]) -> bool:
    return any("pytest tests/unit " in run or run.rstrip().endswith("pytest tests/unit") for run in _runs(job))


def _selects_only_unit_tests(include: list[Any]) -> bool:
    """True when every matrix entry runs files of ``tests/unit`` and nothing else.

    The integration shards of ``tests.yml`` use the same ``shard`` key, so the key alone
    does not tell the unit suite from the integration suite.
    """

    for entry in include:
        selection = entry.get("selection") if isinstance(entry, dict) else None
        if not isinstance(selection, str) or not selection.split():
            return False
        if not all(token.startswith("tests/unit/") for token in selection.split()):
            return False
    return True


def _ci_unit_suite_minutes() -> tuple[int, int, int]:
    """Shard count, shard limit and their product, from the CI shard job."""

    found: list[tuple[str, int, int]] = []
    for job_id, job in _jobs(TESTS_WORKFLOW).items():
        include = (job.get("strategy") or {}).get("matrix", {}).get("include")
        if not isinstance(include, list) or not include:
            continue
        if not all(isinstance(entry, dict) and "shard" in entry for entry in include):
            continue
        if not _selects_only_unit_tests(include):
            continue
        if not any("pytest" in run for run in _runs(job)):
            continue
        limit = job.get("timeout-minutes")
        assert isinstance(limit, int) and limit > 0, f"CI shard job {job_id} must set timeout-minutes"
        found.append((job_id, len(include), limit))
    assert len(found) == 1, f"expected exactly one CI unit shard job in tests.yml, found {found}"
    _, shards, limit = found[0]
    return shards, limit, shards * limit


def test_ci_has_one_unit_shard_job_with_a_limit() -> None:
    shards, limit, total = _ci_unit_suite_minutes()
    assert shards >= 1
    assert limit >= 1
    assert total == shards * limit


def test_publish_still_reruns_the_unit_suite() -> None:
    rerunning = [job_id for job_id, job in _jobs(PUBLISH_WORKFLOW).items() if _reruns_whole_unit_suite(job)]
    assert rerunning == ["build-and-verify"], rerunning


def test_every_publish_job_that_reruns_the_suite_has_time_for_it() -> None:
    shards, limit, total = _ci_unit_suite_minutes()
    needed = total + MARGIN_MINUTES
    checked = 0
    for job_id, job in _jobs(PUBLISH_WORKFLOW).items():
        if not _reruns_whole_unit_suite(job):
            continue
        checked += 1
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int), f"publish job {job_id} must set timeout-minutes"
        assert timeout >= needed, (
            f"publish job {job_id} reruns the whole unit suite in one process with "
            f"timeout-minutes {timeout}, but CI allows the suite {shards} shards x "
            f"{limit} minutes = {total} minutes, plus {MARGIN_MINUTES} for the rest of "
            f"the job: raise it to at least {needed}"
        )
        assert timeout <= HOSTED_JOB_MAX_MINUTES, f"publish job {job_id} exceeds the hosted runner job limit"
    assert checked == 1
