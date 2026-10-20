"""A killed import's working copy is removed on the next open or import, and a running import's copy is never touched.

An import holds one lock on the vault's data directory from before it makes its working copy until it has removed
it. The sweep takes the same lock without waiting and removes nothing when it cannot. The lock is on the directory,
not on a file in it, so deleting a file cannot release it.

Mutations, each in ``onramp.py`` alone. ``scripts/derived_label_mutations.json`` replays the first four and the last:

* ``_run_import_snapshot`` never takes the lock (``restore_lock = None``):
  ``test_an_open_beside_a_running_import_leaves_its_working_copy`` fails.
* ``_run_import_snapshot`` takes the lock after ``mkstemp`` made the copy:
  ``test_a_sweep_loop_beside_a_real_import_never_removes_its_working_copy`` fails.
* the sweep takes the lock without ``LOCK_NB``, so it waits for a running import:
  ``test_an_open_beside_a_running_import_leaves_its_working_copy`` fails because the open does not return.
* ``_run_import_snapshot`` does not call ``sweep_restore_working_copies`` before it starts:
  ``test_an_import_removes_a_killed_imports_copy_before_it_starts`` fails.
* the sweep goes on when it could not take the lock: the same tests and the sweep loop test fail.
* the lock is a file beside the vault instead of the directory:
  ``test_deleting_files_beside_a_running_import_does_not_expose_its_working_copy`` fails.
* ``sweep_restore_working_copies`` returns before it reads the directory:
  ``test_the_next_open_removes_a_restore_working_copy_and_its_sidecars`` fails because the sentinel is still there.

Every open that runs beside a lock runs in a thread that is given 5 seconds, so a sweep that waits fails a test
and does not stop the run.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from alicebot_api import onramp
from alicebot_api.onramp import (
    acquire_restore_lock,
    bootstrap_database,
    release_restore_lock,
    sentinel_hits,
    sweep_restore_working_copies,
)
from alicebot_api.sqlite_store import sqlite_user_connection
from tests.unit.test_sqlite_onramp import USER_ID, _export_to, _seed_full_graph

ROOT = Path(__file__).resolve().parents[2]
NEEDLE = "SENTINEL-RESTORE-COPY"

_IMPORT_CHILD = """
import os, signal, sys
from alicebot_api import onramp

target, user, dump, action = sys.argv[1:5]
real_records = onramp._import_records


def records(*args, **kwargs):
    if action == "kill":
        os.kill(os.getpid(), signal.SIGKILL)
    print("ready", flush=True)
    sys.stdin.readline()
    return real_records(*args, **kwargs)


onramp._import_records = records
sys.exit(onramp.main(["import", "--db", target, "--user-id", user, "--in", dump]))
"""


def _scenario(tmp_path: Path, *, vault_exists: bool) -> tuple[Path, Path]:
    """An export file and the vault path to import it into. The vault is created first only when asked."""

    source = tmp_path / "source" / "source.db"
    source.parent.mkdir()
    bootstrap_database(source, user_id=USER_ID, user_email="local@alice")
    _seed_full_graph(source)
    dump = source.parent / "dump.jsonl"
    _export_to(source, dump)
    target = tmp_path / "vault" / "vault.db"
    target.parent.mkdir(mode=0o700)
    if vault_exists:
        bootstrap_database(target, user_id=USER_ID, user_email="local@alice")
    return dump, target


def _import(target: Path, dump: Path) -> int:
    return onramp.main(["import", "--db", str(target), "--user-id", str(USER_ID), "--in", str(dump)])


def _copies(directory: Path) -> list[str]:
    """The restore working copies in ``directory``, by name (not their sidecars)."""

    return sorted(p.name for p in directory.iterdir() if ".restore." in p.name and p.name.endswith(".tmp"))


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if ".restore." in p.name)


def _open_beside(target: Path, *, vault_exists: bool) -> tuple[bool, BaseException | None]:
    """Open the vault, as another command would, in a thread. Return (it returned within 5 seconds, its error)."""

    errors: list[BaseException] = []

    def run() -> None:
        try:
            if vault_exists:
                with sqlite_user_connection(target, USER_ID):
                    pass
            else:
                # An open would create the vault that the import is about to publish, so only the sweep runs.
                sweep_restore_working_copies(target)
        except BaseException as exc:  # reported by the caller
            errors.append(exc)

    opener = threading.Thread(target=run, daemon=True)
    opener.start()
    opener.join(5)
    return not opener.is_alive(), (errors[0] if errors else None)


def _imported_rows(target: Path) -> int:
    import sqlite3

    with sqlite3.connect(target) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])


class _SweepLoop:
    """A thread that sweeps the vault's directory over and over, as opens of the vault do."""

    def __init__(self, database: Path) -> None:
        self.database = database
        self.passes = 0
        self.error: BaseException | None = None
        self.stop = threading.Event()
        self.decided = threading.Event()
        self.release = threading.Event()
        self.deleted = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            while not self.stop.is_set():
                sweep_restore_working_copies(self.database)
                self.passes += 1
        except BaseException as exc:  # reported by the test, never lost in the thread
            self.error = exc

    def paused_remove(self, real_remove):
        """``_remove_sqlite_files`` for the sweep thread only: wait between the decision to delete and the delete."""

        def remove(path: Path) -> None:
            if threading.current_thread() is self.thread:
                self.decided.set()
                assert self.release.wait(5), "the paused sweep was never released"
                real_remove(path)
                self.deleted.set()
                return
            real_remove(path)

        return remove

    def wait_until_it_decided_or_gave_way(self, passes: int = 3) -> None:
        """Return once the sweep chose a copy to delete, or finished ``passes`` more passes without doing so."""

        start = self.passes
        deadline = time.monotonic() + 5
        while not self.decided.is_set() and self.passes < start + passes:
            assert self.error is None, self.error
            assert time.monotonic() < deadline, "the sweep loop made no progress"
            time.sleep(0.001)


def test_the_next_open_removes_a_restore_working_copy_and_its_sidecars(tmp_path) -> None:
    database = tmp_path / "vault.db"
    user = str(uuid4())
    bootstrap_database(database, user_id=user, user_email="synthetic@example.invalid")
    copy = tmp_path / ".vault.db.restore.deadbeef.tmp"
    copy.write_bytes(NEEDLE.encode())
    wal = tmp_path / ".vault.db.restore.deadbeef.tmp-wal"
    wal.write_bytes(b"wal")
    shm = tmp_path / ".vault.db.restore.deadbeef.tmp-shm"
    shm.write_bytes(b"shm")
    assert sentinel_hits(database, NEEDLE) == [copy.name]
    with sqlite_user_connection(database, user):
        pass
    assert not copy.exists()
    assert not wal.exists()
    assert not shm.exists()
    assert sentinel_hits(database, NEEDLE) == []


def test_an_open_does_not_delete_a_restore_that_still_holds_its_lock(tmp_path) -> None:
    """A vault open interleaved with import leaves the working copy alone.

    Mutation: delete a matching name even when the lock is held.
    """

    database = tmp_path / "vault.db"
    bootstrap_database(database, user_id=USER_ID, user_email="synthetic@example.invalid")
    copy = tmp_path / ".vault.db.restore.running.tmp"
    copy.write_bytes(b"restore in progress")
    descriptor = acquire_restore_lock(database)
    try:
        returned, error = _open_beside(database, vault_exists=True)
        assert error is None, error
        assert returned, "the open waited for the restore"
        assert copy.exists()
        sweep_restore_working_copies(database)
        assert copy.exists()
    finally:
        release_restore_lock(descriptor)
    sweep_restore_working_copies(database)
    assert not copy.exists()


def test_opening_the_working_copy_does_not_delete_it(tmp_path) -> None:
    database = tmp_path / "vault.db"
    user = str(uuid4())
    bootstrap_database(database, user_id=user, user_email="synthetic@example.invalid")
    copy = tmp_path / ".vault.db.restore.live.tmp"
    copy.write_bytes(database.read_bytes())
    sweep_restore_working_copies(copy)
    assert copy.exists()
    sweep_restore_working_copies(database)
    assert not copy.exists()


@pytest.mark.parametrize("vault_exists", [False, True], ids=["new-vault", "existing-vault"])
def test_a_sweep_loop_beside_a_real_import_never_removes_its_working_copy(
    tmp_path, monkeypatch, vault_exists
) -> None:
    """The sweep decides to delete the working copy, pauses, and deletes after the import has started writing.

    A thread sweeps in a loop beside the real import. A forced pause sits between the sweep's decision to delete a
    copy and its delete. The import is held right after it makes its working copy until the sweep has either decided
    to delete it or has run several passes and left it alone. Then the import goes on, and the sweep is let go in the
    middle of the import, once its records are being written. The import must finish with exit code 0.
    """

    dump, target = _scenario(tmp_path, vault_exists=vault_exists)
    loop = _SweepLoop(target)
    real_remove = onramp._remove_sqlite_files
    real_mkstemp = tempfile.mkstemp
    real_records = onramp._import_records
    prefix = f".{target.name}.restore."

    def mkstemp(*args, **kwargs):
        made = real_mkstemp(*args, **kwargs)
        if str(kwargs.get("prefix", "")).startswith(prefix):
            loop.wait_until_it_decided_or_gave_way()
        return made

    def records(*args, **kwargs):
        assert len(_copies(target.parent)) == 1, "the import's working copy is missing before its records are written"
        if loop.decided.is_set():
            loop.release.set()
            assert loop.deleted.wait(5)
        return real_records(*args, **kwargs)

    monkeypatch.setattr(onramp, "_remove_sqlite_files", loop.paused_remove(real_remove))
    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(onramp, "_import_records", records)
    loop.thread.start()
    try:
        exit_code = _import(target, dump)
    finally:
        loop.release.set()
        loop.stop.set()
        loop.thread.join(10)
    assert loop.error is None, loop.error
    assert exit_code == 0
    assert _imported_rows(target) > 0
    assert _leftovers(target.parent) == []


@pytest.mark.parametrize("vault_exists", [False, True], ids=["new-vault", "existing-vault"])
def test_an_open_beside_a_running_import_leaves_its_working_copy(tmp_path, monkeypatch, vault_exists) -> None:
    """A real import, with an open of the vault running while its working copy is live, finishes and loses nothing."""

    dump, target = _scenario(tmp_path, vault_exists=vault_exists)
    real_records = onramp._import_records
    seen: dict[str, object] = {}

    def records(*args, **kwargs):
        seen["before"] = _copies(target.parent)
        seen["open_returned"], seen["open_error"] = _open_beside(target, vault_exists=vault_exists)
        seen["after"] = _copies(target.parent)
        return real_records(*args, **kwargs)

    monkeypatch.setattr(onramp, "_import_records", records)
    exit_code = _import(target, dump)
    assert seen["open_error"] is None, seen["open_error"]
    assert seen["open_returned"], "the open waited for the running import"
    assert len(seen["before"]) == 1  # type: ignore[arg-type]
    assert seen["after"] == seen["before"]
    assert exit_code == 0
    assert _imported_rows(target) > 0
    assert _leftovers(target.parent) == []


@pytest.fixture
def children() -> Iterator[list[subprocess.Popen[str]]]:
    started: list[subprocess.Popen[str]] = []
    yield started
    for child in started:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=30)
        for stream in (child.stdin, child.stdout):
            if stream is not None:
                stream.close()


def _spawn_import(
    target: Path, dump: Path, action: str, children: list[subprocess.Popen[str]]
) -> subprocess.Popen[str]:
    """A real ``alice-memory import`` in another process. It stops, or kills itself, once its records start."""

    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(entry for entry in sys.path if entry)
    child = subprocess.Popen(
        [sys.executable, "-c", _IMPORT_CHILD, str(target), str(USER_ID), str(dump), action],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=environment,
    )
    children.append(child)
    return child


def test_an_open_in_another_process_leaves_a_running_imports_copy(tmp_path, children) -> None:
    dump, target = _scenario(tmp_path, vault_exists=True)
    child = _spawn_import(target, dump, "wait", children)
    assert child.stdout is not None and child.stdin is not None
    assert child.stdout.readline().strip() == "ready", "the import never reached its records"
    copies = _copies(target.parent)
    assert len(copies) == 1
    returned, error = _open_beside(target, vault_exists=True)
    assert error is None, error
    assert returned, "the open waited for the running import"
    sweep_restore_working_copies(target)
    assert _copies(target.parent) == copies
    child.stdin.write("\n")
    child.stdin.flush()
    assert child.wait(timeout=60) == 0
    assert _imported_rows(target) > 0
    assert _leftovers(target.parent) == []


@pytest.mark.parametrize("vault_exists", [False, True], ids=["new-vault", "existing-vault"])
def test_a_killed_import_leaves_a_copy_that_the_next_open_or_import_removes(
    tmp_path, children, vault_exists
) -> None:
    """A SIGKILLed import loses its lock with the process, so the copy it left is stale at once."""

    dump, target = _scenario(tmp_path, vault_exists=vault_exists)
    child = _spawn_import(target, dump, "kill", children)
    assert child.wait(timeout=60) == -signal.SIGKILL
    assert len(_copies(target.parent)) == 1
    if vault_exists:
        with sqlite_user_connection(target, USER_ID):
            pass
    else:
        assert _import(target, dump) == 0
    assert _leftovers(target.parent) == []


def test_deleting_files_beside_a_running_import_does_not_expose_its_working_copy(tmp_path, monkeypatch) -> None:
    """Whatever else is in the data directory is deleted while the import runs. The next open leaves the copy alone."""

    dump, target = _scenario(tmp_path, vault_exists=True)
    real_records = onramp._import_records
    seen: dict[str, object] = {}

    def records(*args, **kwargs):
        copies = _copies(target.parent)
        seen["before"] = copies
        keep = {target.name, f"{target.name}-wal", f"{target.name}-shm", f"{target.name}-journal"}
        for name in copies:
            keep.update({name, f"{name}-wal", f"{name}-shm", f"{name}-journal"})
        deleted = []
        for entry in target.parent.iterdir():
            if entry.name not in keep:
                entry.unlink()
                deleted.append(entry.name)
        seen["deleted"] = deleted
        seen["opens"] = [_open_beside(target, vault_exists=True), _open_beside(target, vault_exists=False)]
        seen["after"] = _copies(target.parent)
        return real_records(*args, **kwargs)

    monkeypatch.setattr(onramp, "_import_records", records)
    exit_code = _import(target, dump)
    assert seen["opens"] == [(True, None), (True, None)]
    assert len(seen["before"]) == 1  # type: ignore[arg-type]
    assert seen["after"] == seen["before"], f"deleted {seen['deleted']}, then the next open removed the live copy"
    assert exit_code == 0
    assert _imported_rows(target) > 0


@pytest.mark.parametrize("vault_exists", [False, True], ids=["new-vault", "existing-vault"])
def test_an_import_removes_a_killed_imports_copy_before_it_starts(tmp_path, vault_exists) -> None:
    """The import itself removes a killed import's copy: no open of the vault happens after it."""

    dump, target = _scenario(tmp_path, vault_exists=vault_exists)
    stale = target.parent / f".{target.name}.restore.killed.tmp"
    stale.write_bytes(NEEDLE.encode())
    (target.parent / f"{stale.name}-wal").write_bytes(b"wal")
    (target.parent / f"{stale.name}-shm").write_bytes(b"shm")
    assert sentinel_hits(target, NEEDLE) == [stale.name]
    assert _import(target, dump) == 0
    assert _leftovers(target.parent) == []
    assert sentinel_hits(target, NEEDLE) == []
    assert _imported_rows(target) > 0


def test_on_a_vault_that_was_never_created_labels_check_leaves_the_copy_and_the_next_import_removes_it(
    tmp_path, capsys
) -> None:
    """``labels check`` on a vault that does not exist fails and leaves the copy. The next import removes it."""

    dump, target = _scenario(tmp_path, vault_exists=False)
    stale = target.parent / f".{target.name}.restore.killed.tmp"
    stale.write_bytes(NEEDLE.encode())
    capsys.readouterr()
    assert onramp.main(["labels", "check", "--db", str(target)]) == 1
    assert "vault_not_found" in capsys.readouterr().out
    assert not target.exists()
    assert stale.exists()
    assert _import(target, dump) == 0
    assert not stale.exists()


def test_a_command_that_creates_the_vault_removes_the_copy_of_a_killed_first_import(tmp_path) -> None:
    _, target = _scenario(tmp_path, vault_exists=False)
    stale = target.parent / f".{target.name}.restore.killed.tmp"
    stale.write_bytes(NEEDLE.encode())
    assert onramp.main(["doctor", "--db", str(target), "--user-id", str(USER_ID)]) == 0
    assert target.exists()
    assert not stale.exists()


def test_the_docs_say_a_never_created_vault_is_cleaned_at_the_next_import() -> None:
    for relative in ("docs/alpha/backup-and-restore.md", "CHANGELOG.md"):
        text = " ".join((ROOT / relative).read_text(encoding="utf-8").split())
        assert "On a vault that was never created, cleanup happens at the next import into that path" in text, relative
        assert "vault_not_found" in text, relative
