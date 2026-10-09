"""The connector health block, the cursors of a sync and the failure list hold what their caller may read.

A connector keeps one cursor, the position of the last item it imported, and for a file or a page that is the path or the
address of the item. The health block also names the last captured item by source id and external id. Each is shown to a
caller with limits only when the source it came from is stored, not deleted, and inside the caller's limits on its
effective labels. The owner and an unbound admin key are shown the block as it was stored. The events of a sync record the
same cursors, so a feed of events holds them to the same rule.
"""
from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_connectors import VNextConnectorService
from alicebot_api.vnext_label_guard import LabelGuard, guard_for_caller

HIDDEN_PATH = "/vault/HIDDEN-FILE.md"
SHOWN_PATH = "/vault/SHOWN-FILE.md"
TRUSTED = AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent", auth="agent_api_key")
ADMIN = AgentIdentity(agent_id="admin", permission_profile="admin_agent", auth="agent_api_key")


@pytest.fixture
def store():
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "connector-health@example.invalid")
    return SQLiteVNextStore(conn, user_id)


def _import(service: VNextConnectorService, path: str, sensitivity: str, **extra) -> object:
    return service.sync_items(
        "local_folder", [{"path": path, "title": path, "text": f"text of {path}", **extra}], default_sensitivity=sensitivity
    )


def _health(store, identity) -> dict:
    return VNextConnectorService(store).connector_health("local_folder", guard=guard_for_caller(store, identity))


def test_the_owner_and_an_unbound_admin_key_are_shown_the_last_item_and_the_cursor(store):
    service = VNextConnectorService(store)
    _import(service, HIDDEN_PATH, "confidential")
    for identity in (None, ADMIN):
        health = _health(store, identity)
        assert health["last_captured_item"]["external_id"] == HIDDEN_PATH
        assert health["cursor_state"] == HIDDEN_PATH


def test_a_caller_with_limits_is_not_shown_a_confidential_item_or_the_cursor_it_set(store):
    service = VNextConnectorService(store)
    result = _import(service, HIDDEN_PATH, "confidential")
    health = _health(store, TRUSTED)
    assert health["last_captured_item"] is None and health["cursor_state"] is None
    # What carries no row text stays: the counters and the times.
    assert health["items_captured"] == 1 and health["last_success_at"] is not None
    assert result.source_ids and HIDDEN_PATH not in str(health) and result.source_ids[0] not in str(health)


def test_a_caller_with_limits_is_shown_the_item_and_the_cursor_of_a_source_it_may_read(store):
    service = VNextConnectorService(store)
    _import(service, SHOWN_PATH, "public")
    health = _health(store, TRUSTED)
    assert health["last_captured_item"]["external_id"] == SHOWN_PATH and health["cursor_state"] == SHOWN_PATH


def test_the_item_and_the_cursor_are_judged_apart(store):
    service = VNextConnectorService(store)
    _import(service, HIDDEN_PATH, "confidential")
    # A failed item holds the cursor back, so it still names the confidential file while the last import is public.
    service.sync_items(
        "local_folder",
        [{"path": "", "external_id": "bad"}, {"path": SHOWN_PATH, "title": "t", "text": "public", "mtime_ns": 5}],
        default_sensitivity="public",
    )
    assert _health(store, None)["cursor_state"] == HIDDEN_PATH
    health = _health(store, TRUSTED)
    assert health["last_captured_item"]["external_id"] == SHOWN_PATH
    assert health["cursor_state"] is None


def test_a_cursor_set_by_a_readable_import_is_shown_though_an_older_import_is_not(store):
    service = VNextConnectorService(store)
    _import(service, HIDDEN_PATH, "confidential")
    # A later cursor (a number sorts above a path) is set by a public import, which is the only one that carries it.
    _import(service, SHOWN_PATH, "public", mtime_ns=5)
    assert _health(store, None)["cursor_state"] == f"5:{SHOWN_PATH}"
    health = _health(store, TRUSTED)
    assert health["cursor_state"] == f"5:{SHOWN_PATH}" and health["last_captured_item"]["external_id"] == SHOWN_PATH


def test_a_cursor_that_no_import_carries_is_not_shown_to_a_caller_with_limits(store):
    service = VNextConnectorService(store)
    _import(service, SHOWN_PATH, "public")
    # The local store keeps no connector state row, so the cursor is read from the newest completed sync.
    store.append_event(
        {
            "target_type": "connector", "target_id": "local_folder", "event_type": "connector.sync_completed",
            "actor_type": "system", "occurred_at": "2099-01-01T00:00:00Z",
            "payload_json": {"sync_cursor": HIDDEN_PATH, "item_count": 1, "imported_count": 0},
        }
    )
    assert _health(store, None)["cursor_state"] == HIDDEN_PATH
    assert _health(store, TRUSTED)["cursor_state"] is None


def test_a_source_row_marked_deleted_is_not_shown_even_when_the_store_returns_it(store, monkeypatch):
    service = VNextConnectorService(store)
    _import(service, SHOWN_PATH, "public")
    assert _health(store, TRUSTED)["last_captured_item"] is not None
    original = store.get_source
    monkeypatch.setattr(store, "get_source", lambda source_id: {**original(source_id), "deleted_at": "2026-10-09T00:00:00Z"})
    health = _health(store, TRUSTED)
    assert health["last_captured_item"] is None and health["cursor_state"] is None


def test_an_import_whose_source_is_deleted_or_missing_is_not_shown_to_a_caller_with_limits(store):
    service = VNextConnectorService(store)
    result = _import(service, SHOWN_PATH, "public")
    source_id = result.source_ids[0]
    store.conn.execute("UPDATE sources SET deleted_at = ? WHERE id = ?", ("2026-10-09T00:00:00Z", source_id))
    health = _health(store, TRUSTED)
    assert health["last_captured_item"] is None and health["cursor_state"] is None
    assert _health(store, None)["last_captured_item"]["external_id"] == SHOWN_PATH


def test_an_import_event_naming_no_source_is_not_shown_to_a_caller_with_limits(store):
    service = VNextConnectorService(store)
    store.append_event(
        {
            "target_type": "connector", "target_id": "local_folder", "event_type": "connector.item_imported",
            "actor_type": "system", "occurred_at": "2026-10-09T00:00:00Z",
            "payload_json": {"external_id": HIDDEN_PATH, "sync_cursor": HIDDEN_PATH, "source_id": "not-a-source"},
        }
    )
    health = _health(store, TRUSTED)
    assert health["last_captured_item"] is None
    assert service.connector_health("local_folder", guard=LabelGuard.unlimited(store))["last_captured_item"] is not None


def test_a_snapshot_of_all_connectors_is_reused_only_for_the_grant_it_was_judged_under(store):
    from alicebot_api.vnext_label_guard import label_read_scope

    service = VNextConnectorService(store)
    _import(service, HIDDEN_PATH, "confidential")

    def local_folder(snapshot):
        return next(item for item in snapshot["items"] if item["connector_name"] == "local_folder")

    with label_read_scope(store):
        owner = service.connector_health_all(guard=LabelGuard.unlimited(store))
        limited = service.connector_health_all(guard=guard_for_caller(store, TRUSTED))
        again = service.connector_health_all(guard=LabelGuard.unlimited(store))
    assert local_folder(owner)["cursor_state"] == HIDDEN_PATH
    assert local_folder(limited)["cursor_state"] is None and local_folder(limited)["last_captured_item"] is None
    assert local_folder(again)["cursor_state"] == HIDDEN_PATH
    with label_read_scope(store):
        first = service.connector_health_all(guard=guard_for_caller(store, TRUSTED))
        second = service.connector_health_all(guard=LabelGuard.unlimited(store))
    assert local_folder(first)["cursor_state"] is None and local_folder(second)["cursor_state"] == HIDDEN_PATH


def test_the_cursors_of_a_sync_record_are_the_ones_the_caller_may_read(store):
    service = VNextConnectorService(store)
    _import(service, HIDDEN_PATH, "confidential")
    result = service.sync_items(
        "local_folder", [{"path": "/vault/A.md", "title": "a", "text": "below the cursor"}], default_sensitivity="public"
    )
    assert result.skipped_count == 1 and result.previous_cursor == HIDDEN_PATH
    shown = service.shown_sync_record(result, guard=guard_for_caller(store, TRUSTED))
    assert shown["previous_cursor"] is None and shown["sync_cursor"] is None
    assert shown["skipped_count"] == 1 and HIDDEN_PATH not in str(shown)
    for identity in (None, ADMIN):
        assert service.shown_sync_record(result, guard=guard_for_caller(store, identity)) == result.to_record()
    assert result.to_record()["previous_cursor"] == HIDDEN_PATH


def test_the_cursors_a_failed_sync_recorded_are_the_ones_the_caller_may_read(store):
    service = VNextConnectorService(store)
    _import(service, HIDDEN_PATH, "confidential")
    service.sync_items("local_folder", [{"path": "", "external_id": "bad"}], default_sensitivity="public")
    events = [
        event
        for event in store.list_events(target_type="connector", target_id="local_folder")
        if event["event_type"] in {"connector.item_failed", "connector.sync_failed"}
    ]
    assert {event["event_type"] for event in events} == {"connector.item_failed", "connector.sync_failed"}
    owner = service.shown_event_cursors(events, guard=LabelGuard.unlimited(store))
    assert owner == events
    shown = service.shown_event_cursors(events, guard=guard_for_caller(store, TRUSTED))
    sync_failed = next(event for event in shown if event["event_type"] == "connector.sync_failed")
    assert sync_failed["payload_json"]["previous_cursor"] is None and sync_failed["payload_json"]["sync_cursor"] is None
    # The stored event is not edited, and the owner still reads the cursor in it.
    stored = next(event for event in events if event["event_type"] == "connector.sync_failed")
    assert stored["payload_json"]["previous_cursor"] == HIDDEN_PATH


def test_a_cursor_is_shown_only_when_every_import_that_carries_it_is_readable(store):
    service = VNextConnectorService(store)
    _import(service, HIDDEN_PATH, "confidential")
    readable = _import(service, SHOWN_PATH, "public", mtime_ns=5)
    trusted = guard_for_caller(store, TRUSTED)
    assert service.shown_cursor("local_folder", HIDDEN_PATH, guard=trusted) is None
    # A second import, of a source the caller may read, names the same cursor. The first still does not let it through.
    store.append_event(
        {
            "target_type": "connector", "target_id": "local_folder", "event_type": "connector.item_imported",
            "actor_type": "system", "occurred_at": "2026-10-09T00:00:00Z",
            "payload_json": {"external_id": SHOWN_PATH, "sync_cursor": HIDDEN_PATH, "source_id": readable.source_ids[0]},
        }
    )
    assert service.shown_cursor("local_folder", HIDDEN_PATH, guard=trusted) is None
    assert service.shown_cursor("local_folder", HIDDEN_PATH, guard=LabelGuard.unlimited(store)) == HIDDEN_PATH
    # Both imports readable: the cursor is shown.
    store.conn.execute("UPDATE sources SET sensitivity = 'public' WHERE title = ?", (HIDDEN_PATH,))
    assert service.shown_cursor("local_folder", HIDDEN_PATH, guard=trusted) == HIDDEN_PATH


def _feed(store, identity) -> list:
    return guard_for_caller(store, identity).admit_events(store.list_events())


def _cursors(events) -> set:
    found = set()
    for event in events:
        payload = event.get("payload_json") or {}
        found.update(payload.get(name) for name in ("cursor_value", "previous_cursor", "sync_cursor") if payload.get(name))
    return found


def _state_updated(store, cursor, **extra):
    return store.append_event(
        {
            "target_type": "connector", "target_id": "local_folder", "event_type": "connector.state_updated",
            "actor_type": "system", "occurred_at": "2026-10-09T00:00:01Z",
            "payload_json": {"connector_name": "local_folder", "cursor_type": "sync_cursor", "cursor_value": cursor, **extra},
        }
    )


def test_the_events_of_a_sync_hold_the_cursors_the_caller_may_read(store):
    service = VNextConnectorService(store)
    _import(service, HIDDEN_PATH, "confidential")
    # The next sync starts where the confidential one stopped and ends on a public file.
    _import(service, SHOWN_PATH, "public", mtime_ns=5)
    _state_updated(store, HIDDEN_PATH)
    _state_updated(store, f"5:{SHOWN_PATH}")
    stored = store.list_events()
    assert {"connector.sync_started", "connector.sync_completed", "connector.state_updated"} <= {
        event["event_type"] for event in stored
    }

    # The owner and an unbound admin key read every event as it was stored.
    for identity in (None, ADMIN):
        assert _feed(store, identity) == stored
    assert HIDDEN_PATH in _cursors(stored)

    limited = _feed(store, TRUSTED)
    assert HIDDEN_PATH not in str(limited)
    by_type: dict[str, list[dict]] = {}
    for event in limited:
        by_type.setdefault(event["event_type"], []).append(event["payload_json"])
    # Both syncs started with a null previous cursor in front of this key, the second one where the file of the
    # confidential import stopped. Only the second sync is shown to it whole.
    assert [p["previous_cursor"] for p in by_type["connector.sync_started"]] == [None, None]
    assert [(p["previous_cursor"], p["sync_cursor"]) for p in by_type["connector.sync_completed"]] == [
        (None, f"5:{SHOWN_PATH}")
    ]
    # The cursor of the public file is still shown, and the one of the confidential file is not.
    assert sorted(str(p["cursor_value"]) for p in by_type["connector.state_updated"]) == sorted(["None", f"5:{SHOWN_PATH}"])
    # Nothing else in an event changed, and the stored events are not edited.
    completed = by_type["connector.sync_completed"][0]
    assert completed["imported_count"] == 1 and completed["connector_name"] == "local_folder"
    assert HIDDEN_PATH in str(store.list_events())


def test_the_event_feeds_follow_the_same_rule(store):
    service = VNextConnectorService(store)
    _import(service, HIDDEN_PATH, "confidential")
    _import(service, SHOWN_PATH, "public", mtime_ns=5)
    _state_updated(store, HIDDEN_PATH)
    guard = guard_for_caller(store, TRUSTED)
    newest = guard.newest_admitted_events(lambda size: store.list_events(limit=size), want=20)
    assert newest and HIDDEN_PATH not in str(newest) and f"5:{SHOWN_PATH}" in str(newest)
    # A reader that counts the events does not need the cursors, and the count is the same with or without them.
    counted = guard.admit_events(store.list_events(), cursors=False)
    assert len(counted) == len(guard.admit_events(store.list_events())) == guard.readable_event_count()
    assert HIDDEN_PATH in str(counted)
    # A guard with no limits hands the events back as they are.
    assert LabelGuard.unlimited(store).admit_events(store.list_events()) == store.list_events()


def test_a_cursor_that_cannot_be_tied_to_a_connector_or_to_text_is_not_shown(store):
    service = VNextConnectorService(store)
    _import(service, SHOWN_PATH, "public")
    _state_updated(store, 12345)
    _state_updated(store, "")
    store.append_event(
        {
            "target_type": "other", "target_id": "x", "event_type": "connector.sync_started", "actor_type": "system",
            "occurred_at": "2026-10-09T00:00:02Z",
            "payload_json": {"connector_name": None, "previous_cursor": SHOWN_PATH},
        }
    )
    shown = [
        event["payload_json"]
        for event in _feed(store, TRUSTED)
        if event["event_type"] == "connector.state_updated" or event["payload_json"].get("connector_name") is None
    ]
    values = [p["cursor_value"] for p in shown if "cursor_value" in p]
    # The number is not shown and the empty text stays empty.
    assert sorted(str(value) for value in values) == ["", "None"]
    # The cursor of an event that names no connector cannot be tied to an import, though the file it names is readable.
    assert [p["previous_cursor"] for p in shown if "previous_cursor" in p] == [None]
