"""A save of the brain charter and the check that lets a key make it are one step.

``PUT /v0/vnext/settings/brain-charter`` replaces the whole charter, so it first reads the stored one and refuses a key
that may not read it. Without a lock between that read and the write, an owner who classifies the charter after the
read would find it replaced by a key that can no longer read it. The charter lock is held from the read to the end of
the transaction, and every save takes it, so a save that arrives meanwhile waits and is judged on the charter it finds.
"""
from __future__ import annotations

import json
import threading
import time

import psycopg
from psycopg.rows import dict_row

from alicebot_api.vnext_store import PostgresVNextStore, lock_brain_charter
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.operator_route_probes import Call
from tests.integration.operator_route_runner import run_call
from tests.integration.operator_route_vault import Vault

PATH = "/v0/vnext/settings/brain-charter"


def _put(vault: Vault, key, **body):
    return run_call(vault, "PUT", PATH, Call("ad hoc", body=body), key)


def _stored(vault: Vault) -> dict:
    status, text = run_call(vault, "GET", PATH, Call("ad hoc"), vault.keys["admin"])
    assert status == 200
    return json.loads(text)["brain_charter"]


def _waiters(harness) -> int:
    """Transactions waiting for the charter lock of the test user."""

    with psycopg.connect(harness.urls["app"], autocommit=True, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
            "AND classid = (hashtext('vnext_brain_charter')::bigint & 4294967295) "
            "AND objid = (hashtext(%s)::bigint & 4294967295)",
            (str(harness.user_id),),
        ).fetchone()
    return int(row["n"])


def _wait_for_a_waiter(harness, *, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _waiters(harness) >= 1:
            return True
        time.sleep(0.01)
    return False


def _after_the_first_read(monkeypatch, action):
    """Run ``action`` once, right after the first read of the stored charter returns, inside that request."""

    original = PostgresVNextStore.get_brain_charter
    fired = []

    def read(self):
        row = original(self)
        if not fired:
            fired.append(True)
            action()
        return row

    monkeypatch.setattr(PostgresVNextStore, "get_brain_charter", read)


def test_a_charter_classified_after_the_check_is_not_replaced_by_the_key_that_checked(label_harness, monkeypatch):
    vault = Vault(label_harness, "k").build()
    trusted, admin = vault.keys["trusted"], vault.keys["admin"]
    # The vault's charter is confidential. Made private, the trusted key may read it and so may replace it.
    assert _put(vault, admin, content_markdown="first", sensitivity="private")[0] == 200
    seen = {}

    def classify_meanwhile():
        def save():
            seen["answer"] = _put(vault, admin, content_markdown="classified again", sensitivity="confidential")

        thread = threading.Thread(target=save)
        thread.start()
        seen["thread"] = thread
        # The classification has to wait for the save that is checking, and must not slip in between.
        seen["waited"] = _wait_for_a_waiter(label_harness)

    _after_the_first_read(monkeypatch, classify_meanwhile)
    status, _text = _put(vault, trusted, content_markdown="trusted overwrite")
    seen["thread"].join(30)
    assert status == 200 and seen["answer"][0] == 200
    # The classification came last, so the confidential charter is the one that stands.
    stored = _stored(vault)
    assert stored["content_markdown"] == "classified again" and stored["sensitivity"] == "confidential"
    assert seen["waited"], "the classification did not wait for the save in progress"


def test_a_key_that_arrives_during_a_save_is_judged_on_the_charter_that_save_leaves(label_harness, monkeypatch):
    vault = Vault(label_harness, "k2").build()
    trusted, admin = vault.keys["trusted"], vault.keys["admin"]
    assert _put(vault, admin, content_markdown="first", sensitivity="private")[0] == 200
    seen = {}

    def arrive_meanwhile():
        def save():
            seen["answer"] = _put(vault, trusted, content_markdown="trusted overwrite")

        thread = threading.Thread(target=save)
        thread.start()
        seen["thread"] = thread
        seen["waited"] = _wait_for_a_waiter(label_harness)

    _after_the_first_read(monkeypatch, arrive_meanwhile)
    status, _text = _put(vault, admin, content_markdown="classified", sensitivity="confidential")
    seen["thread"].join(30)
    assert status == 200
    # The key found the charter the save left, which it may not read, and was refused.
    assert seen["answer"][0] == 403
    stored = _stored(vault)
    assert stored["content_markdown"] == "classified" and stored["sensitivity"] == "confidential"
    assert seen["waited"], "the key did not wait for the save in progress"


def test_every_save_of_the_charter_waits_for_the_charter_lock(label_harness):
    failures: list[BaseException] = []
    done = threading.Event()

    def save():
        try:
            with label_harness.store() as other:
                other.upsert_brain_charter({"content_markdown": "second"}, actor_type="user")
        except BaseException as exc:  # reported by the assertion below
            failures.append(exc)
        done.set()

    with label_harness.store() as holder:
        lock_brain_charter(holder)
        thread = threading.Thread(target=save)
        thread.start()
        assert _wait_for_a_waiter(label_harness), "the save did not wait for the charter lock"
        assert not done.is_set()
    thread.join(30)
    assert done.is_set() and not failures
