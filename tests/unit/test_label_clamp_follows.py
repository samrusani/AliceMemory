"""A store method that defers its lock decision to the clamp must call the clamp next."""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from alicebot_api import vnext_label_writes as writes
from alicebot_api.vnext_store import PostgresVNextStore

SOURCE = Path(__file__).resolve().parents[2] / "apps/api/src/alicebot_api"
DEFERRING = re.compile(r"prepare_label_patch\([^\n]*clamp_follows=True\)")
PAIRED = re.compile(
    r'patch = prepare_label_patch\(self, "(?P<kind>\w+)", (?P<before>\w+), patch, clamp_follows=True\)\n'
    r'\s+patch = clamp_owner_patch\(self, kind="(?P=kind)", before=(?P=before), patch=patch\)'
)


def test_every_deferring_prepare_is_followed_by_its_clamp():
    deferring = paired = 0
    for path in SOURCE.rglob("*.py"):
        text = path.read_text()
        deferring += len(DEFERRING.findall(text))
        paired += len(PAIRED.findall(text))
    # Memory and open-loop updates on PostgreSQL, and the open-loop update on SQLite.
    assert deferring == 3
    assert paired == deferring


def test_only_memory_and_open_loop_updates_defer():
    kinds = {
        match.group("kind")
        for path in SOURCE.rglob("*.py")
        for match in PAIRED.finditer(path.read_text())
    }
    assert kinds == {"memory", "open_loop"}


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, query, params=()):
        return None

    def fetchone(self):
        return dict(self.conn.grants)


def _shared_holder():
    conn = SimpleNamespace(grants={"graph": True, "labels": True, "exclusive": False}, queries=[])
    conn.cursor = lambda: _Cursor(conn)
    return PostgresVNextStore(conn)


WEEKLY = {"id": "m1", "domain": "project", "sensitivity": "public", "metadata_json": {"discovered_by": "vnext_weekly_synthesis"}}
PLAIN = {"id": "m2", "domain": "project", "sensitivity": "public", "metadata_json": {}}
EDIT = {"sensitivity": "confidential"}


def test_prepare_leaves_a_derived_label_edit_to_the_clamp_only_when_one_follows(monkeypatch):
    monkeypatch.setattr(writes, "STRICT_LOCK_ORDER", True)
    store = _shared_holder()
    assert writes.is_derived("memory", WEEKLY) and not writes.is_derived("memory", PLAIN)
    # A derived row whose store method clamps next: the clamp decides, so no lock yet.
    assert writes.prepare_label_patch(store, "memory", WEEKLY, EDIT, clamp_follows=True) == EDIT
    # No clamp follows, or the row is not derived: the label change needs exclusive L now.
    with pytest.raises(writes.LabelLockOrderError):
        writes.prepare_label_patch(store, "memory", WEEKLY, EDIT)
    with pytest.raises(writes.LabelLockOrderError):
        writes.prepare_label_patch(store, "memory", PLAIN, EDIT, clamp_follows=True)
    # An edit that names no label never needed the lock.
    assert writes.prepare_label_patch(store, "memory", WEEKLY, {"status": "active"}) == {"status": "active"}


def test_the_clamp_asks_for_exclusive_l_only_when_the_stored_label_changes(monkeypatch):
    """A refused edit on a settled row leaves the stored label alone and so takes no lock."""
    monkeypatch.setattr(writes, "STRICT_LOCK_ORDER", True)
    store = _shared_holder()
    asked = []
    monkeypatch.setattr(writes, "require_exclusive_label_lock", lambda target: asked.append(target))
    monkeypatch.setattr(writes, "collect_label_rows", lambda *args, **kwargs: ([], False))
    def settle(settled):
        label = SimpleNamespace(domain=settled[0], sensitivity=settled[1], project_scope=settled[2], project_floor=settled[3], unverified=False)
        monkeypatch.setattr(writes, "settle_labels", lambda nodes: SimpleNamespace(by_stored=lambda kind, row_id: label))

    store.append_event = lambda event: None
    # Refused: the request asks for less than the settled label, which is the stored one.
    settle(("project", "confidential", (), ()))
    row = {**WEEKLY, "sensitivity": "confidential"}
    patch = writes.clamp_owner_patch(store, kind="memory", before=row, patch={"sensitivity": "public"})
    assert asked == [] and store._label_floor_applied is True and patch["sensitivity"] == "confidential"
    # Raised: the stored label is below the settled one, so the clamp changes it.
    patch = writes.clamp_owner_patch(store, kind="memory", before=WEEKLY, patch={"sensitivity": "public"})
    assert asked == [store] and patch["sensitivity"] == "confidential"
