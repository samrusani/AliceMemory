"""The events of a failed import name the item only to a caller who may read it.

An item that fails to import never becomes a source, so no row carries a label for it. The event it leaves behind names
it by the id the importer gave it, which for a file is its path (``external_id`` of ``connector.item_failed``,
``failed_external_ids`` of a failed sync and of a sync that finished with some failures), and records the cursor it
would have set (``sync_cursor`` of ``connector.item_failed``). In v0.20.0 and on main until now a key with a ceiling
below ``confidential`` was shown the path of a confidential file whose import failed, by the connector status route
(``recent_failures``) and by the workspace event feed.

These tests judge the events with the rule the cursors already follow: an id or a cursor is shown to a caller with
limits only when an import carries it and every source an import of it made is readable by the caller, and is ``null``
otherwise. The same cases through the mounted application, for every permission profile, are in
``tests/integration/test_connector_failure_events_postgres.py``.
"""
from __future__ import annotations

import sqlite3
from copy import deepcopy
from uuid import uuid4

import pytest

from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_connectors import (
    CONNECTOR_EVENT_CURSOR_FIELDS,
    CONNECTOR_EVENT_ITEM_ID_FIELDS,
    VNextConnectorService,
)
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
    ensure_sqlite_user(conn, user_id, "connector-failures@example.invalid")
    return SQLiteVNextStore(conn, user_id)


def _failing(service: VNextConnectorService, *paths: str) -> None:
    """Make the capture of the given files raise, the way the store does when a write fails, through the real sync."""

    real = service.capture_service.capture_source

    def capture(request, *args, **kwargs):
        if request.external_id in paths:
            raise RuntimeError("synthetic capture failure")
        return real(request, *args, **kwargs)

    service.capture_service.capture_source = capture  # type: ignore[method-assign]


def _import(service: VNextConnectorService, path: str, sensitivity: str, **extra):
    return service.sync_items(
        "local_folder", [{"path": path, "title": path, "text": f"text of {path}", **extra}], default_sensitivity=sensitivity
    )


def _connector_events(store, *types: str) -> list[dict]:
    return [
        event
        for event in store.list_events(target_type="connector", target_id="local_folder")
        if not types or event["event_type"] in types
    ]


def _feed(store, identity) -> list:
    return guard_for_caller(store, identity).admit_events(store.list_events())


def _shown(store, identity, *types: str) -> list:
    return [event for event in _feed(store, identity) if event["event_type"] in types]


def test_the_maps_name_every_event_that_records_a_failed_item() -> None:
    """Mutation: drop an event type or a field from either map.

    ``connector.item_failed`` is written when one item fails. ``connector.sync_failed`` and ``connector.sync_completed``
    both list the ids of the items that failed, because a sync that imported some items and failed others finishes as
    completed.
    """

    assert CONNECTOR_EVENT_CURSOR_FIELDS["connector.item_failed"] == ("sync_cursor",)
    assert CONNECTOR_EVENT_ITEM_ID_FIELDS == {
        "connector.item_failed": ("external_id",),
        "connector.sync_completed": ("failed_external_ids",),
        "connector.sync_failed": ("failed_external_ids",),
    }


def test_a_failed_import_of_a_confidential_file_does_not_name_it_to_a_caller_with_limits(store, monkeypatch):
    """Mutation: remove ``connector.item_failed`` from the cursor map, from the item id map, or the id field of a failed sync."""

    service = VNextConnectorService(store)
    _failing(service, HIDDEN_PATH)
    result = _import(service, HIDDEN_PATH, "confidential")
    assert result.status == "failed" and result.failed_external_ids == (HIDDEN_PATH,)
    stored = _connector_events(store, "connector.item_failed", "connector.sync_failed")
    assert {event["event_type"] for event in stored} == {"connector.item_failed", "connector.sync_failed"}
    assert next(e for e in stored if e["event_type"] == "connector.item_failed")["payload_json"]["external_id"] == HIDDEN_PATH
    assert next(e for e in stored if e["event_type"] == "connector.item_failed")["payload_json"]["sync_cursor"] == HIDDEN_PATH

    shown = _shown(store, TRUSTED, "connector.item_failed", "connector.sync_failed")
    assert {event["event_type"] for event in shown} == {"connector.item_failed", "connector.sync_failed"}
    item = next(e for e in shown if e["event_type"] == "connector.item_failed")["payload_json"]
    sync = next(e for e in shown if e["event_type"] == "connector.sync_failed")["payload_json"]
    assert item["external_id"] is None and item["sync_cursor"] is None
    assert sync["failed_external_ids"] == [None] and sync["sync_cursor"] is None and sync["previous_cursor"] is None
    assert HIDDEN_PATH not in str(shown)
    # What carries no row text stays: the failure is still listed, with its public code, message and counters.
    assert item["error_code"] == "connector_item_import_failed" and item["error_message"] == "Connector item could not be imported"
    assert sync["failed_count"] == 1 and sync["error_code"] == "connector_item_import_failed"


def test_the_owner_and_an_unbound_admin_key_are_shown_the_failed_item_as_it_was_stored(store):
    service = VNextConnectorService(store)
    _failing(service, HIDDEN_PATH)
    _import(service, HIDDEN_PATH, "confidential")
    stored = _connector_events(store, "connector.item_failed", "connector.sync_failed")
    for identity in (None, ADMIN):
        shown = _shown(store, identity, "connector.item_failed", "connector.sync_failed")
        assert sorted(shown, key=lambda e: e["event_type"]) == sorted(stored, key=lambda e: e["event_type"])
        item = next(e for e in shown if e["event_type"] == "connector.item_failed")["payload_json"]
        assert item["external_id"] == HIDDEN_PATH and item["sync_cursor"] == HIDDEN_PATH
        sync = next(e for e in shown if e["event_type"] == "connector.sync_failed")["payload_json"]
        assert sync["failed_external_ids"] == [HIDDEN_PATH]
    service_view = service.shown_event_cursors(stored, guard=LabelGuard.unlimited(store))
    assert service_view == stored


def test_a_failure_of_an_item_that_a_readable_import_carries_is_still_shown(store):
    """Mutation: read the carriers of an id from the ``sync_cursor`` field of the imports, or require none.

    The file was imported before and the caller may read that source, so the caller already knows its path. The import
    carries a cursor of its own (a file with a modification time has one that is not its path), so the id is found by the
    ``external_id`` of the import and not by its cursor. The failed attempt records a newer cursor that no import carries,
    and that one is held back.
    """

    service = VNextConnectorService(store)
    _import(service, SHOWN_PATH, "public", mtime_ns=3)
    _failing(service, SHOWN_PATH)
    result = _import(service, SHOWN_PATH, "public", mtime_ns=5)
    assert result.status == "failed"
    shown = _shown(store, TRUSTED, "connector.item_failed", "connector.sync_failed")
    item = next(e for e in shown if e["event_type"] == "connector.item_failed")["payload_json"]
    sync = next(e for e in shown if e["event_type"] == "connector.sync_failed")["payload_json"]
    assert item["external_id"] == SHOWN_PATH and sync["failed_external_ids"] == [SHOWN_PATH]
    assert item["sync_cursor"] is None


def test_a_cursor_of_a_failed_item_that_a_readable_import_carries_is_shown(store):
    service = VNextConnectorService(store)
    _import(service, SHOWN_PATH, "public")
    store.append_event(
        {
            "target_type": "connector", "target_id": "local_folder", "event_type": "connector.item_failed",
            "actor_type": "system", "occurred_at": "2099-01-01T00:00:00Z",
            "payload_json": {"connector_name": "local_folder", "external_id": SHOWN_PATH, "sync_cursor": SHOWN_PATH, "error_code": "x"},
        }
    )
    shown = _shown(store, TRUSTED, "connector.item_failed")
    assert shown[0]["payload_json"]["external_id"] == SHOWN_PATH and shown[0]["payload_json"]["sync_cursor"] == SHOWN_PATH


def test_each_id_of_a_list_is_judged_apart_and_keeps_its_place(store):
    """Mutation: judge the list as one value, or drop the ids that are not shown instead of leaving a ``null`` in their place."""

    service = VNextConnectorService(store)
    _import(service, SHOWN_PATH, "public")
    _import(service, HIDDEN_PATH, "confidential", mtime_ns=3)
    _failing(service, SHOWN_PATH, HIDDEN_PATH)
    result = service.sync_items(
        "local_folder",
        [
            {"path": SHOWN_PATH, "title": "a", "text": "a", "mtime_ns": 5},
            {"path": HIDDEN_PATH, "title": "b", "text": "b", "mtime_ns": 6},
        ],
        default_sensitivity="public",
    )
    assert result.status == "failed" and set(result.failed_external_ids) == {SHOWN_PATH, HIDDEN_PATH}
    stored = _connector_events(store, "connector.sync_failed")[0]["payload_json"]["failed_external_ids"]
    shown = _shown(store, TRUSTED, "connector.sync_failed")[0]["payload_json"]["failed_external_ids"]
    assert shown == [item if item == SHOWN_PATH else None for item in stored]
    assert len(shown) == 2 and SHOWN_PATH in shown and None in shown


def test_a_sync_that_finished_with_some_failures_does_not_name_the_failed_file(store):
    """Mutation: remove ``connector.sync_completed`` from the item id map.

    A sync that imported one file and failed another is a completed sync with ``failed_external_ids``. It is listed in the
    workspace event feed, which is not limited to failures.
    """

    service = VNextConnectorService(store)
    _failing(service, HIDDEN_PATH)
    result = service.sync_items(
        "local_folder",
        [{"path": SHOWN_PATH, "title": "a", "text": "a"}, {"path": HIDDEN_PATH, "title": "b", "text": "b", "mtime_ns": 1}],
        default_sensitivity="public",
    )
    assert result.status == "partial" and result.failed_external_ids == (HIDDEN_PATH,)
    [completed] = _connector_events(store, "connector.sync_completed")
    assert completed["payload_json"]["failed_external_ids"] == [HIDDEN_PATH]
    [shown] = _shown(store, TRUSTED, "connector.sync_completed")
    assert shown["payload_json"]["failed_external_ids"] == [None]
    assert shown["payload_json"]["imported_count"] == 1 and shown["payload_json"]["failed_count"] == 1
    assert HIDDEN_PATH not in str(_feed(store, TRUSTED))
    [seen] = _shown(store, ADMIN, "connector.sync_completed")
    assert seen["payload_json"]["failed_external_ids"] == [HIDDEN_PATH]


def test_the_events_handed_in_are_not_edited(store):
    """Mutation: write the shown value into the payload that was handed in."""

    service = VNextConnectorService(store)
    _failing(service, HIDDEN_PATH)
    _import(service, HIDDEN_PATH, "confidential")
    events = _connector_events(store)
    before = deepcopy(events)
    shown = service.shown_event_cursors(events, guard=guard_for_caller(store, TRUSTED))
    assert events == before
    assert shown != events


def test_an_id_that_cannot_be_tied_to_a_connector_or_is_not_text_is_not_shown(store):
    """Mutation: pass an id through when its event names no connector or holds something that is not text."""

    service = VNextConnectorService(store)
    _import(service, SHOWN_PATH, "public")
    guard = guard_for_caller(store, TRUSTED)
    events = [
        {"event_type": "connector.item_failed", "target_type": "agent", "target_id": "x", "payload_json": {"external_id": SHOWN_PATH}},
        {"event_type": "connector.item_failed", "target_type": "connector", "target_id": "local_folder", "payload_json": {"external_id": 5}},
        {"event_type": "connector.item_failed", "target_type": "connector", "target_id": "local_folder", "payload_json": {"external_id": {"path": SHOWN_PATH}}},
        {"event_type": "connector.sync_failed", "target_type": "connector", "target_id": "local_folder", "payload_json": {"failed_external_ids": [SHOWN_PATH, 5, None]}},
        {"event_type": "connector.sync_failed", "target_type": "connector", "target_id": "local_folder", "payload_json": {"failed_external_ids": SHOWN_PATH}},
    ]
    shown = service.shown_event_cursors(events, guard=guard)
    assert shown[0]["payload_json"]["external_id"] is None
    assert shown[1]["payload_json"]["external_id"] is None
    assert shown[2]["payload_json"]["external_id"] is None
    assert shown[3]["payload_json"]["failed_external_ids"] == [SHOWN_PATH, None, None]
    assert shown[4]["payload_json"]["failed_external_ids"] == SHOWN_PATH
    # An empty field has nothing to show and is left as it is.
    empties = [
        {"event_type": "connector.item_failed", "target_type": "connector", "target_id": "local_folder", "payload_json": {"external_id": None, "sync_cursor": ""}},
        {"event_type": "connector.sync_failed", "target_type": "connector", "target_id": "local_folder", "payload_json": {"failed_external_ids": []}},
    ]
    assert service.shown_event_cursors(empties, guard=guard) == empties


def test_the_other_connector_events_are_listed_as_they_were_stored(store):
    """The events that name no file of the owner are not changed: what they hold is a count, a time, a setting or a name the caller sent."""

    service = VNextConnectorService(store)
    payloads = {
        "connector.item_rejected": {"connector_name": "telegram", "external_id": "update-1", "reason": "chat_not_allowlisted", "chat_id": "42"},
        "connector.local_folder_scan": {"connector_name": "local_folder", "path_count": 1, "file_count": 2, "ignored_count": 0, "refused_count": 0, "truncated": False, "recursive": True, "extensions": [".md"]},
        "connector.config_updated": {"connector_name": "local_folder", "enabled": True, "configured": True},
        "connector.secret_ref_updated": {"connector_name": "local_folder", "secret_ref": "ref", "secret_present": True},
        "connector.state_update_failed": {"connector_name": "local_folder", "status": "ok"},
    }
    for event_type, payload in payloads.items():
        store.append_event(
            {
                "target_type": "connector", "target_id": payload["connector_name"], "event_type": event_type,
                "actor_type": "system", "occurred_at": "2026-10-09T00:00:00Z", "payload_json": payload,
            }
        )
    listed = {event["event_type"]: event["payload_json"] for event in _feed(store, TRUSTED) if event["event_type"] in payloads}
    assert listed == payloads
    stored = [event for event in store.list_events() if event["event_type"] in payloads]
    assert service.shown_event_cursors(stored, guard=guard_for_caller(store, TRUSTED)) == stored


def test_the_feed_of_the_newest_events_holds_the_failed_item_back_too(store):
    """The workspace reads its events through ``newest_admitted_events``, which judges the ones it shows."""

    service = VNextConnectorService(store)
    _failing(service, HIDDEN_PATH)
    _import(service, HIDDEN_PATH, "confidential")
    guard = guard_for_caller(store, TRUSTED)
    newest = guard.newest_admitted_events(lambda size: store.list_events(limit=size), want=20)
    failed = [event for event in newest if event["event_type"] in {"connector.item_failed", "connector.sync_failed"}]
    assert len(failed) == 2 and HIDDEN_PATH not in str(newest)
    owner = LabelGuard.unlimited(store).newest_admitted_events(lambda size: store.list_events(limit=size), want=20)
    assert HIDDEN_PATH in str(owner)


def test_a_failed_normalization_names_only_the_id_it_was_sent_and_is_held_back_the_same_way(store):
    """An item that is refused before any capture records the id the call sent. The rule does not depend on how it failed."""

    service = VNextConnectorService(store)
    service.sync_items("local_folder", [{"path": "", "external_id": HIDDEN_PATH}], default_sensitivity="confidential")
    [item] = _connector_events(store, "connector.item_failed")
    assert item["payload_json"]["external_id"] == HIDDEN_PATH and item["payload_json"]["sync_cursor"] is None
    [seen] = _shown(store, TRUSTED, "connector.item_failed")
    assert seen["payload_json"]["external_id"] is None
    [owner] = _shown(store, None, "connector.item_failed")
    assert owner["payload_json"]["external_id"] == HIDDEN_PATH


def test_a_sync_answer_judges_the_ids_of_its_failed_items(store):
    """Mutation: remove the failed-id judgement from ``shown_sync_record``.

    A sync of a folder names files the caller never sent, so the answer holds paths the caller may not read. The
    ids follow the rule of a cursor, each in its place in the list. The owner and an unbound admin key are shown the
    record as it was made.
    """

    service = VNextConnectorService(store)
    _import(service, SHOWN_PATH, "public", mtime_ns=3)
    _failing(service, SHOWN_PATH, HIDDEN_PATH)
    result = service.sync_items(
        "local_folder",
        [
            {"path": SHOWN_PATH, "title": "a", "text": "a", "mtime_ns": 5},
            {"path": HIDDEN_PATH, "title": "b", "text": "b", "mtime_ns": 6},
        ],
        default_sensitivity="confidential",
    )
    assert set(result.failed_external_ids) == {SHOWN_PATH, HIDDEN_PATH}
    shown = service.shown_sync_record(result, guard=guard_for_caller(store, TRUSTED))
    assert shown["failed_external_ids"] == [item if item == SHOWN_PATH else None for item in result.failed_external_ids]
    assert HIDDEN_PATH not in str(shown) and shown["failed_count"] == 2
    # The cursor the failures held back is the one the readable import set, so it is still shown.
    assert shown["previous_cursor"] == f"3:{SHOWN_PATH}" and shown["sync_cursor"] == f"3:{SHOWN_PATH}"
    for identity in (None, ADMIN):
        assert service.shown_sync_record(result, guard=guard_for_caller(store, identity)) == result.to_record()
    assert set(result.to_record()["failed_external_ids"]) == {SHOWN_PATH, HIDDEN_PATH}


def test_a_sync_answer_with_nothing_to_judge_reads_no_events(store, monkeypatch):
    """A sync that imported or skipped everything and moved no cursor has no value to judge, so it reads no events."""

    service = VNextConnectorService(store)
    result = service.sync_items("local_folder", [], default_sensitivity="public")
    assert result.failed_external_ids == () and result.previous_cursor is None and result.sync_cursor is None

    def reads(*_args, **_kwargs):
        raise AssertionError("the events of the connector were read")

    monkeypatch.setattr(service, "_connector_events", reads)
    shown = service.shown_sync_record(result, guard=guard_for_caller(store, TRUSTED))
    assert shown["failed_external_ids"] == [] and shown["previous_cursor"] is None


class _CountingList(list):
    """A list that counts how many times it is iterated."""

    passes = 0

    def __iter__(self):
        type(self).passes += 1
        return super().__iter__()


def test_the_imports_are_indexed_once_for_a_call_that_judges_many_ids(store, monkeypatch):
    """Mutation: build the index of the imports for each id (move it inside the per-value branch).

    A failed sync can list thousands of ids and a connector can hold thousands of import events. The judgement reads the
    events once for the call and not once for each id (the figure that prompted this: 3,000 ids over about 6,000 import
    events took 0.96 s a call when each id scanned every event).
    """

    service = VNextConnectorService(store)
    imports = _CountingList(
        {
            "event_type": "connector.item_imported",
            "target_type": "connector",
            "target_id": "local_folder",
            "payload_json": {"external_id": f"/vault/ok-{index}.md", "sync_cursor": f"{index}:/vault/ok-{index}.md", "source_id": str(uuid4())},
        }
        for index in range(300)
    )
    monkeypatch.setattr(service, "_connector_events", lambda *_a, **_k: imports)
    _CountingList.passes = 0
    ids = [f"/vault/bad-{index}.md" for index in range(200)]
    events = [
        {
            "event_type": "connector.sync_failed",
            "target_type": "connector",
            "target_id": "local_folder",
            "payload_json": {"failed_external_ids": ids, "previous_cursor": None, "sync_cursor": None},
        },
        {
            "event_type": "connector.item_failed",
            "target_type": "connector",
            "target_id": "local_folder",
            "payload_json": {"external_id": ids[0], "sync_cursor": f"1:{ids[0]}"},
        },
    ]
    shown = service.shown_event_cursors(events, guard=guard_for_caller(store, TRUSTED))
    assert shown[0]["payload_json"]["failed_external_ids"] == [None] * len(ids)
    assert shown[1]["payload_json"]["external_id"] is None
    assert _CountingList.passes == 1
    # The same holds for the answer of a sync that lists many ids.
    _CountingList.passes = 0
    result = type("Result", (), {})()
    record = {
        "status": "failed", "connector_name": "local_folder", "failed_external_ids": ids, "previous_cursor": None,
        "sync_cursor": None, "source_ids": [], "failed_count": len(ids),
    }
    result.connector_name = "local_folder"
    result.to_record = lambda: dict(record)
    shown_record = service.shown_sync_record(result, guard=guard_for_caller(store, TRUSTED))  # type: ignore[arg-type]
    assert shown_record["failed_external_ids"] == [None] * len(ids)
    assert _CountingList.passes == 1
