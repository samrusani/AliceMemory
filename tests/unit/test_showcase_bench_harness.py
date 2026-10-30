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
    (about ten seconds of startup per key) fails here because the minting
    runs with ``subprocess`` patched to refuse any run."""

    import subprocess as _subprocess

    from scripts.showcase_bench import _create_keys

    def no_subprocess(*args: object, **kwargs: object) -> object:
        raise AssertionError("minting started a process")

    monkeypatch.setattr(_subprocess, "run", no_subprocess)
    monkeypatch.setattr(_subprocess, "Popen", no_subprocess)
    database = tmp_path / "mint.db"
    keys = _create_keys(database)
    assert set(keys) == {"sales", "support", "product", "leadership"}
    assert all(str(key).startswith("alice_sk_") for key in keys.values())


def test_the_in_process_mint_leaves_nothing_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: the in-process call restores what it touches: stdout/stderr, the
    environment, logging handlers and level, the working directory,
    ``sys.argv``, and leaves no open connection (the vault's row is committed
    and readable from a fresh connection)."""

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
    assert raw.startswith("alice_sk_")
    assert _sys.argv == argv_before
    assert os.getcwd() == cwd_before
    assert os.environ == env_before
    assert list(root.handlers) == handlers_before
    assert root.level == level_before
    with _sqlite3.connect(tmp_path / "left.db") as conn:
        row = conn.execute("SELECT agent_id, key_prefix FROM agent_api_keys WHERE agent_id='leftover-check'").fetchone()
    assert row and row[1] in raw
