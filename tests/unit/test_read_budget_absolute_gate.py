"""Required read-budget runs compare with the merge base and skip fixed-second limits.

The round-two and handoff suites time the workspace and dogfooding screens, and apply their
fixed-second limits, only when ``ALICE_READ_ABSOLUTE_GATE`` is ``1``. A local run with no
merge-base checkout still holds handoff pack and recall to 0.15 seconds.
"""

from __future__ import annotations

import json

import pytest

from alicebot_api.vnext_agent_control import ALL_SENSITIVITY, DEFAULT_AGENT_SENSITIVITY
from tests.integration import test_label_read_handoff_budget_postgres as handoff_postgres
from tests.performance import test_label_read_handoff_budget as handoff_sqlite
from tests.performance import test_label_round2_read_budget as round2

ROUND2_ASSERT_BUDGETS = round2.assert_budgets


class _Probe:
    def __init__(self, checkout, timings, seen):
        self.checkout = str(checkout)
        self.timings = timings
        self.seen = seen
        self.queue = []
        self.stdin = self
        self.stdout = self
        self.stderr = self
        self.stopped = False

    def write(self, text):
        command = text.strip()
        self.seen.append((self.checkout, command))
        if command == "stop":
            self.stopped = True
            return
        self.queue.append(command)

    def flush(self):
        return None

    def readline(self):
        command = self.queue.pop(0)
        wall, cpu = self.timings(self.checkout, command)
        return json.dumps({"wall": wall, "cpu": cpu}) + "\n"

    def read(self):
        return "probe failed"

    def poll(self):
        return 0 if self.stopped else None

    def communicate(self, timeout=None):
        return "", ""


class _Quiet:
    def __enter__(self):
        self.conn = self
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        return None


def _commands(seen):
    return [command for _checkout, command in seen]


def _prepare(monkeypatch, *, gate, limit, merge_base):
    monkeypatch.delenv("ALICE_READ_MAIN_CHECKOUT", raising=False)
    monkeypatch.delenv("ALICE_READ_BUDGET_EVIDENCE", raising=False)
    monkeypatch.delenv("ALICE_ROUND2_PROFILE_DIR", raising=False)
    monkeypatch.delenv("ALICE_READ_PROFILE", raising=False)
    if gate:
        monkeypatch.setenv("ALICE_READ_ABSOLUTE_GATE", "1")
    else:
        monkeypatch.delenv("ALICE_READ_ABSOLUTE_GATE", raising=False)
    if limit is None:
        monkeypatch.delenv("ALICE_READ_ABSOLUTE_LIMIT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("ALICE_READ_ABSOLUTE_LIMIT_SECONDS", limit)
    if merge_base:
        monkeypatch.setenv("ALICE_READ_MERGE_BASE_CHECKOUT", "merge-base-checkout")
    else:
        monkeypatch.delenv("ALICE_READ_MERGE_BASE_CHECKOUT", raising=False)


def _install(monkeypatch, module, name, timings):
    seen = []

    def open_probe(*args, **kwargs):
        return _Probe(args[0], timings, seen)

    monkeypatch.setattr(module, name, open_probe)
    return seen


def _round2_timings(screens, head_wall, base_wall):
    def timings(checkout, action):
        if action in screens:
            return screens[action], 9.0
        wall = base_wall if checkout == "merge-base-checkout" else head_wall
        return wall, 0.01

    return timings


def run_round2(monkeypatch, *, gate, limit=None, screens=None, head_wall=0.05, base_wall=0.05):
    """One postgres round-two budget run against stand-in probes. Returns the commands sent."""

    _prepare(monkeypatch, gate=gate, limit=limit, merge_base=True)
    screens = screens or {}
    seen = _install(
        monkeypatch, round2, "_open_probe", _round2_timings(screens, head_wall, base_wall)
    )
    ROUND2_ASSERT_BUDGETS(
        "postgres", "postgresql://app", round2.USER, {"trusted_local_agent": "synthetic-credential"}
    )
    return _commands(seen)


def run_round2_count(monkeypatch, tmp_path, *, wall, cpu):
    """The round-two SQLite count, with the database and the paired read replaced by stand-ins."""

    monkeypatch.setattr(round2, "bootstrap_database", lambda *args, **kwargs: None)
    monkeypatch.setattr(round2, "sqlite_user_connection", lambda *args, **kwargs: _Quiet())
    monkeypatch.setattr(round2, "SQLiteVNextStore", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        round2,
        "seed_varied",
        lambda *args, **kwargs: {
            "trusted_local_agent": "synthetic-credential",
            "admin_agent": "synthetic-credential",
        },
    )

    def complete_count(store, ceiling):
        if ceiling == ALL_SENSITIVITY:
            return {"active": 5000}
        assert ceiling == DEFAULT_AGENT_SENSITIVITY
        return {"active": 2500}

    monkeypatch.setattr(round2, "complete_count", complete_count)
    called = []
    monkeypatch.setattr(round2, "assert_budgets", lambda *args, **kwargs: called.append(True))
    wall_clock = {"n": 0.0}
    cpu_clock = {"n": 0.0}

    def perf():
        wall_clock["n"] += wall
        return wall_clock["n"]

    def proc():
        cpu_clock["n"] += cpu
        return cpu_clock["n"]

    monkeypatch.setattr(round2.time, "perf_counter", perf)
    monkeypatch.setattr(round2.time, "process_time", proc)
    round2.test_sqlite_varied_read_budget(tmp_path, monkeypatch, "mixed", False)
    assert called == [True]


class _Harness:
    urls = {"app": "postgresql://app", "admin": "postgresql://admin"}
    user_id = round2.USER

    def store(self):
        return _Quiet()


def _handoff_timings(screens, head_wall, base_wall, head_walls=None):
    per_action = head_walls or {}

    def timings(checkout, action):
        if action in screens:
            return screens[action], 9.0
        if checkout == "merge-base-checkout":
            return base_wall, 0.01
        return per_action.get(action, head_wall), 0.01

    return timings


def run_handoff_postgres(
    monkeypatch, *, gate, limit=None, merge_base=True, screens=None, head_wall=0.05, base_wall=0.05, head_walls=None
):
    _prepare(monkeypatch, gate=gate, limit=limit, merge_base=merge_base)
    screens = screens or {}
    seen = _install(
        monkeypatch,
        handoff_postgres,
        "open_probe",
        _handoff_timings(screens, head_wall, base_wall, head_walls),
    )
    monkeypatch.setattr(handoff_postgres, "seed_copies", lambda store, count=5000, postgres=False: ({"id": "source"}, ["row"]))
    monkeypatch.setattr(handoff_postgres.psycopg, "connect", lambda *args, **kwargs: _Quiet())
    handoff_postgres.test_postgres_five_thousand_native_read_budgets(_Harness(), False)
    return _commands(seen)


def run_handoff_sqlite(
    monkeypatch, tmp_path, *, gate, merge_base=True, counted=0.01, elapsed=0.01, head_wall=0.05, base_wall=0.05, head_walls=None
):
    _prepare(monkeypatch, gate=gate, limit=None, merge_base=merge_base)
    monkeypatch.setattr(handoff_sqlite, "bootstrap_database", lambda *args, **kwargs: None)
    monkeypatch.setattr(handoff_sqlite, "sqlite_user_connection", lambda *args, **kwargs: _Quiet())
    monkeypatch.setattr(handoff_sqlite, "SQLiteVNextStore", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        handoff_sqlite, "seed_copies", lambda store, count=5000, postgres=False: ({"id": "source"}, ["row-not-in-pack"])
    )
    monkeypatch.setattr(handoff_sqlite, "count_twice", lambda store: None)
    monkeypatch.setattr(handoff_sqlite, "run_pack", lambda store: "pack-text")
    moments = iter((0.0, counted, counted, counted + elapsed))
    monkeypatch.setattr(handoff_sqlite.time, "perf_counter", lambda: next(moments))
    seen = _install(
        monkeypatch,
        handoff_postgres,
        "open_probe",
        _handoff_timings({}, head_wall, base_wall, head_walls),
    )
    handoff_sqlite.test_sqlite_five_thousand_derived_rows_budget(tmp_path)
    return _commands(seen)


def test_a_round_two_run_without_the_absolute_gate_times_no_screen_and_applies_no_fixed_limit(monkeypatch, tmp_path):
    """A slow screen and a slow complete count pass when the variable is unset. A slow pack still fails.

    Mutations: time the screens on every postgres run (the commands then name a screen, or the slow
    screen fails); drop the ``if`` around the complete-count assert (the slow count fails); drop the
    merge-base assert (the slow pack passes).
    """

    commands = run_round2(monkeypatch, gate=False, screens={"workspace": 5.0, "dogfooding": 5.0})
    assert "workspace" not in commands
    assert "dogfooding" not in commands
    run_round2_count(monkeypatch, tmp_path, wall=5.0, cpu=0.01)
    run_round2_count(monkeypatch, tmp_path, wall=0.01, cpu=5.0)
    with pytest.raises(AssertionError, match="pack"):
        run_round2(monkeypatch, gate=False, head_wall=1.0, base_wall=0.05)


@pytest.mark.parametrize("action", ["workspace", "dogfooding"])
def test_a_round_two_run_with_the_absolute_gate_fails_a_slow_screen(monkeypatch, action):
    """The screen limit is absolute_limit_seconds(). A screen under that limit passes and one just over it fails.

    Mutations: compare the wall clock to one second whatever the variable says (the 1.4 second screen fails);
    drop the screen assert (the slow screen passes); gate only one of the two screens.
    """

    other = "dogfooding" if action == "workspace" else "workspace"
    under = {action: 1.4, other: 0.05}
    run_round2(monkeypatch, gate=True, limit="1.5", screens=under)
    over = {action: 1.500001, other: 0.05}
    with pytest.raises(AssertionError, match=action):
        run_round2(monkeypatch, gate=True, limit="1.5", screens=over)
    just_over_default = {action: 1.000001, other: 0.05}
    with pytest.raises(AssertionError, match=action):
        run_round2(monkeypatch, gate=True, screens=just_over_default)
    at_default = {action: 1.0, other: 1.0}
    run_round2(monkeypatch, gate=True, screens=at_default)


def test_a_round_two_complete_count_still_fails_when_the_absolute_gate_is_set(monkeypatch, tmp_path):
    """With the gate on, one second of wall time and one second of CPU still fail the count. The boundary passes.

    Mutation: delete the complete-count assert.
    """

    monkeypatch.setenv("ALICE_READ_ABSOLUTE_GATE", "1")
    with pytest.raises(AssertionError):
        run_round2_count(monkeypatch, tmp_path, wall=1.000001, cpu=0.01)
    with pytest.raises(AssertionError):
        run_round2_count(monkeypatch, tmp_path, wall=0.01, cpu=1.000001)
    run_round2_count(monkeypatch, tmp_path, wall=1.0, cpu=1.0)


def test_a_handoff_run_without_the_absolute_gate_times_no_screen_and_applies_no_fixed_limit(monkeypatch, tmp_path):
    """Postgres screens and the SQLite count and pack limits stay off. A slow pack still fails the merge-base gate.

    Mutations: time the screens on every postgres run; drop the ``if`` around either SQLite fixed limit
    (a five second count, or a one second pack, then fails); drop the merge-base assert.
    """

    commands = run_handoff_postgres(monkeypatch, gate=False, screens={"workspace": 5.0, "dogfooding": 5.0})
    assert "workspace" not in commands
    assert "dogfooding" not in commands
    run_handoff_sqlite(monkeypatch, tmp_path, gate=False, counted=5.0, elapsed=0.01)
    run_handoff_sqlite(monkeypatch, tmp_path, gate=False, counted=0.01, elapsed=1.0)
    with pytest.raises(AssertionError, match="pack"):
        run_handoff_postgres(monkeypatch, gate=False, head_wall=1.0, base_wall=0.05)


@pytest.mark.parametrize("action", ["workspace", "dogfooding"])
def test_a_handoff_run_with_the_absolute_gate_fails_a_slow_screen(monkeypatch, action):
    """The handoff screen median uses absolute_limit_seconds(), on a merge-base run and on a local run.

    Mutations: compare the median to one second whatever the variable says; drop either screen assert.
    """

    other = "dogfooding" if action == "workspace" else "workspace"
    under = {action: 1.4, other: 0.05}
    for merge_base in (True, False):
        run_handoff_postgres(monkeypatch, gate=True, limit="1.5", merge_base=merge_base, screens=under)
        over = {action: 1.500001, other: 0.05}
        with pytest.raises(AssertionError, match=action):
            run_handoff_postgres(monkeypatch, gate=True, limit="1.5", merge_base=merge_base, screens=over)
    just_over_default = {action: 1.000001, other: 0.05}
    with pytest.raises(AssertionError, match=action):
        run_handoff_postgres(monkeypatch, gate=True, screens=just_over_default)


def test_a_handoff_count_and_pack_still_fail_when_the_absolute_gate_is_set(monkeypatch, tmp_path):
    """With the gate on, the SQLite count still fails past one second and the pack still fails past 0.15 seconds.

    Mutation: delete either assert.
    """

    monkeypatch.setenv("ALICE_READ_ABSOLUTE_GATE", "1")
    with pytest.raises(AssertionError):
        run_handoff_sqlite(monkeypatch, tmp_path, gate=True, counted=1.000001, elapsed=0.01)
    with pytest.raises(AssertionError):
        run_handoff_sqlite(monkeypatch, tmp_path, gate=True, counted=0.01, elapsed=0.150001)
    run_handoff_sqlite(monkeypatch, tmp_path, gate=True, counted=1.0, elapsed=0.15)


def test_a_handoff_local_run_keeps_the_pack_and_recall_limits(monkeypatch, tmp_path):
    """Without a merge-base checkout, pack and recall still fail past 0.15 seconds, and the screens are not timed.

    Mutation: delete the local 0.15 second asserts in either suite.
    """

    slow_pack = {"pack": 0.2, "recall": 0.05}
    slow_recall = {"pack": 0.05, "recall": 0.2}
    screens = {"workspace": 5.0, "dogfooding": 5.0}
    with pytest.raises(AssertionError, match="pack"):
        run_handoff_postgres(monkeypatch, gate=False, merge_base=False, screens=screens, head_walls=slow_pack)
    with pytest.raises(AssertionError, match="recall"):
        run_handoff_postgres(monkeypatch, gate=False, merge_base=False, screens=screens, head_walls=slow_recall)
    commands = run_handoff_postgres(monkeypatch, gate=False, merge_base=False, screens=screens, head_wall=0.05)
    assert "workspace" not in commands
    assert "dogfooding" not in commands
    with pytest.raises(AssertionError, match="0.2"):
        run_handoff_sqlite(monkeypatch, tmp_path, gate=False, merge_base=False, head_walls=slow_pack)
    with pytest.raises(AssertionError, match="0.2"):
        run_handoff_sqlite(monkeypatch, tmp_path, gate=False, merge_base=False, head_walls=slow_recall)
