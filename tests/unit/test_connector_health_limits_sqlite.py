"""The connector health block, the cursors of a sync and the failure list hold what their caller may read.

A connector keeps one cursor, the position of the last item it imported, and for a file or a page that is the path or the
address of the item. The health block also names the last captured item by source id and external id. Each is shown to a
caller with limits only when the source it came from is stored, not deleted, and inside the caller's limits on its
effective labels. The owner and an unbound admin key are shown the block as it was stored.
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
    owner = service.shown_failures("local_folder", events, guard=LabelGuard.unlimited(store))
    assert owner == events
    shown = service.shown_failures("local_folder", events, guard=guard_for_caller(store, TRUSTED))
    sync_failed = next(event for event in shown if event["event_type"] == "connector.sync_failed")
    assert sync_failed["payload_json"]["previous_cursor"] is None and sync_failed["payload_json"]["sync_cursor"] is None
    # The stored event is not edited, and the owner still reads the cursor in it.
    stored = next(event for event in events if event["event_type"] == "connector.sync_failed")
    assert stored["payload_json"]["previous_cursor"] == HIDDEN_PATH
