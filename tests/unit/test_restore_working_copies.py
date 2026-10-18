"""A killed import's working copy is removed on the next open, and the sentinel search sees the data directory.

Mutation: ``sweep_restore_working_copies`` returns before it reads the directory.
``test_the_next_open_removes_a_restore_working_copy_and_its_sidecars`` fails because the sentinel is still in the
directory.
"""

from __future__ import annotations

from uuid import uuid4

from alicebot_api.onramp import (
    acquire_restore_lock,
    bootstrap_database,
    release_restore_lock,
    sentinel_hits,
    sweep_restore_working_copies,
)
from alicebot_api.sqlite_store import sqlite_user_connection

NEEDLE = "SENTINEL-RESTORE-COPY"


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

    Mutation: delete a matching name even when its lock is held.
    """

    database = tmp_path / "vault.db"
    user = str(uuid4())
    bootstrap_database(database, user_id=user, user_email="synthetic@example.invalid")
    copy = tmp_path / ".vault.db.restore.running.tmp"
    copy.write_bytes(b"restore in progress")
    descriptor = acquire_restore_lock(copy)
    try:
        with sqlite_user_connection(database, user):
            pass
        assert copy.exists()
        sweep_restore_working_copies(database)
        assert copy.exists()
    finally:
        release_restore_lock(descriptor, copy)
    sweep_restore_working_copies(database)
    assert not copy.exists()
    assert not (tmp_path / ".vault.db.restore.running.tmp.lock").exists()


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
