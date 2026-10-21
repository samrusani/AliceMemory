"""Unit test guard: no test reaches the real home folders of agent hosts.

The guard is built when this file is imported, before any test patches
``HOME``, so it keeps watching the real home. See
``tests/support/host_home_guard.py`` for what it watches and how.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support.host_home_guard import HostHomeGuard, process_guard

PROCESS_GUARD = process_guard()


@pytest.fixture(autouse=True)
def host_home_guard(tmp_path_factory: pytest.TempPathFactory) -> Iterator[HostHomeGuard]:
    """Fail a test that opens, stats, lists or writes under a real host folder.

    The access is refused when it happens. A test that catches the error
    still fails here, from the record the guard keeps. Fix the test: set
    ``HOME`` to ``tmp_path`` or pass ``--home``. Do not exempt it.
    """

    PROCESS_GUARD.allow(tmp_path_factory.getbasetemp())
    with PROCESS_GUARD.armed():
        yield PROCESS_GUARD
    if PROCESS_GUARD.violations:
        lines = "\n".join(f"  {line}" for line in PROCESS_GUARD.violations)
        pytest.fail(
            "this unit test reached into a real agent host folder under the real home:\n"
            f"{lines}\n"
            "Set HOME to tmp_path (and CODEX_HOME style variables to a tmp_path "
            "folder) or pass --home, so the test never leaves its own folder.",
            pytrace=False,
        )


@pytest.fixture
def process_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME as an empty temp folder, for code that resolves against the process home.

    The planners and the install command without ``--home`` read
    ``Path.home()`` for ``~/.alice`` and ``~/.codex``. Without this they would
    reach the real home. Child processes inherit the new HOME.
    """

    home = tmp_path / "process-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """One line that shows the guard was armed, so a quiet run can be told from an absent guard."""

    terminalreporter.write_line(f"host home guard: {PROCESS_GUARD.checks} path checks, {PROCESS_GUARD.refused} refused")
