"""The showcase bench's harness rules: the keys are minted through the
product command's own entry point, in process, and the call leaves nothing
behind."""

from __future__ import annotations

import contextlib
import io
import os
import uuid
from pathlib import Path

import pytest


def test_the_bench_mints_its_keys_without_a_process_per_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: the bench mints its team keys by calling the product command's
    own entry point in process; minting by starting an interpreter per key
    (about 0.4 s of startup per key with a warm bytecode cache, the median of
    six runs of the command) fails here because the minting runs with
    ``subprocess``, ``os.system`` and ``os.posix_spawn`` patched to refuse
    any run."""

    import subprocess as _subprocess

    from scripts.showcase_bench import _create_keys

    def no_subprocess(*args: object, **kwargs: object) -> object:
        raise AssertionError("minting started a process")

    monkeypatch.setattr(_subprocess, "run", no_subprocess)
    monkeypatch.setattr(_subprocess, "Popen", no_subprocess)
    monkeypatch.setattr(os, "system", no_subprocess)
    monkeypatch.setattr(os, "posix_spawn", no_subprocess)
    monkeypatch.setattr(os, "posix_spawnp", no_subprocess)
    database = tmp_path / "mint.db"
    keys = _create_keys(database)
    assert set(keys) == {"sales", "support", "product", "leadership"}
    assert all(str(key).startswith("alice_sk_") for key in keys.values())


def test_the_in_process_mint_leaves_nothing_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: the in-process call leaves the caller as it found it: the same
    stdout and stderr with nothing written to them (the raw key never reaches
    the caller's output), the environment, the root logger's handlers and
    level, the working directory and ``sys.argv``, and no open connection (the
    vault's row is committed and readable from a fresh connection). The
    command changes none of the last five today; these checks catch a
    change."""

    import logging as _logging
    import sqlite3 as _sqlite3
    import sys as _sys

    from scripts.showcase_bench import _mint_via_command

    root = _logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    argv_before = list(_sys.argv)
    cwd_before = os.getcwd()
    env_before = dict(os.environ)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        raw = _mint_via_command(tmp_path / "left.db", uuid.UUID("00000000-0000-0000-0000-000000000001"),
                                "leftover-check", "admin_agent", "leftover")
        assert _sys.stdout is out and _sys.stderr is err
    assert out.getvalue() == "" and err.getvalue() == ""
    assert raw.startswith("alice_sk_")
    assert _sys.argv == argv_before
    assert os.getcwd() == cwd_before
    assert os.environ == env_before
    assert list(root.handlers) == handlers_before
    assert root.level == level_before
    with _sqlite3.connect(tmp_path / "left.db") as conn:
        row = conn.execute("SELECT agent_id, key_prefix FROM agent_api_keys WHERE agent_id='leftover-check'").fetchone()
    assert row and row[1] in raw


def test_a_failed_mint_raises_the_command_error(tmp_path: Path) -> None:
    """Guard: when the command exits nonzero, the bench raises ``BenchError``
    with the exit code and the command's error output, not a parse error on
    empty output.

    Mutation: drop the exit code check (the manifest's
    ``the-bench-reports-a-failed-mint``)."""

    from scripts.alice_bench import BenchError
    from scripts.showcase_bench import _mint_via_command

    with pytest.raises(BenchError, match=r"keys create failed \(2\): .*invalid_request"):
        _mint_via_command(tmp_path / "bad.db", uuid.UUID("00000000-0000-0000-0000-000000000001"),
                          "bad-profile", "no_such_profile", "bad")
