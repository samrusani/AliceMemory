"""The connector screens do not name a file whose import failed to a key that may not read it, on PostgreSQL.

An item that fails to import never becomes a source, so no row carries a label for it, and the events it leaves
behind name it by the id the importer gave it (for a file, its path) and by the cursor it would have set. The
connector status route lists those events as ``recent_failures`` and the workspace lists them in ``recent_events``.
A failure is shown with its id and cursor only to a caller who may read a source that an import of the same id made.

Each case runs through the mounted application with real keys, for every permission profile, over a failure recorded
by the real sync (the capture of one file raises, as the store does when a write fails).
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from alicebot_api.vnext_capture import VNextCaptureService
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)

HIDDEN_PATH = "/vault/HIDDEN-FILE.md"
SHOWN_PATH = "/vault/SHOWN-FILE.md"
SYNC = "/v0/vnext/connectors/local_folder/sync"
STATUS = "/v0/vnext/connectors/local_folder/status"
SCREENS = (
    STATUS,
    "/v0/vnext/workspace",
    "/v0/vnext/connectors",
    "/v0/vnext/connectors/health",
    "/v0/vnext/dogfooding",
    "/v0/vnext/doctor",
)
FAILURES = ("connector.item_failed", "connector.sync_failed")


@pytest.fixture
def failing_capture(monkeypatch):
    """Make the capture of the files in ``failing`` raise inside the real sync."""

    failing: set[str] = set()
    real = VNextCaptureService.capture_source

    def capture(self, request, *args, **kwargs):
        if request.external_id in failing:
            raise RuntimeError("synthetic capture failure")
        return real(self, request, *args, **kwargs)

    monkeypatch.setattr(VNextCaptureService, "capture_source", capture)
    return failing


def sync(harness, key, items, *, sensitivity, expect=(200, 201, 207, 400)):
    status, body, _headers = harness.request("POST", SYNC, payload={"default_sensitivity": sensitivity, "items": items}, key=key)
    assert status in expect, (status, body)
    return status, body


def record_failures(harness, key, failing) -> None:
    """A readable import, then a failed attempt at it, then a failed attempt at a confidential file that was never imported."""

    sync(harness, key, [{"path": SHOWN_PATH, "title": "shown", "text": "a public note"}], sensitivity="public")
    failing.update({SHOWN_PATH, HIDDEN_PATH})
    status, body = sync(
        harness,
        key,
        [
            {"path": SHOWN_PATH, "title": "shown", "text": "a public note, newer", "mtime_ns": 5},
            {"path": HIDDEN_PATH, "title": "hidden", "text": "a confidential note", "mtime_ns": 6},
        ],
        sensitivity="confidential",
    )
    assert body["status"] == "failed" and set(body["failed_external_ids"]) == {SHOWN_PATH, HIDDEN_PATH}


def get(harness, path, key):
    status, body, _headers = harness.request("GET", path, key=key)
    return status, body


def events_of(body, *types) -> list[dict]:
    return [event for event in body["recent_failures"] if event["event_type"] in types]


def test_the_status_of_a_connector_lists_a_failed_file_only_to_the_owner_and_an_unbound_admin_key(label_harness, failing_capture):
    """Mutation: remove ``connector.item_failed`` or a failure field from the maps of ``vnext_connectors``."""

    admin = label_harness.key("admin_agent")
    trusted = label_harness.key("trusted_local_agent")
    record_failures(label_harness, admin, failing_capture)

    status, body = get(label_harness, STATUS, admin)
    assert status == 200
    items = events_of(body, "connector.item_failed")
    assert {item["payload_json"]["external_id"] for item in items} == {SHOWN_PATH, HIDDEN_PATH}
    assert {item["payload_json"]["sync_cursor"] for item in items} == {f"5:{SHOWN_PATH}", f"6:{HIDDEN_PATH}"}
    [sync_failed] = events_of(body, "connector.sync_failed")
    assert set(sync_failed["payload_json"]["failed_external_ids"]) == {SHOWN_PATH, HIDDEN_PATH}

    status, body = get(label_harness, STATUS, trusted)
    assert status == 200
    assert HIDDEN_PATH not in json.dumps(body)
    items = events_of(body, "connector.item_failed")
    assert len(items) == 2
    # The file a readable import made is a file the key already knows. The cursor of the failed attempt is a newer
    # one that no import carries, and the confidential file was never imported, so neither is named.
    shown = {item["payload_json"]["external_id"] for item in items}
    assert shown == {SHOWN_PATH, None}
    assert all(item["payload_json"]["sync_cursor"] is None for item in items)
    [sync_failed] = events_of(body, "connector.sync_failed")
    assert sorted(sync_failed["payload_json"]["failed_external_ids"], key=str) == sorted([SHOWN_PATH, None], key=str)
    assert sync_failed["payload_json"]["failed_count"] == 2
    assert sync_failed["payload_json"]["error_code"] == "connector_item_import_failed"
    assert all(item["payload_json"]["error_code"] == "connector_item_import_failed" for item in items)
    # The health block of the same answer reports the failures as counts and a public code.
    assert body["health"]["items_failed"] == 2 and body["health"]["last_error_code"] == "connector_item_import_failed"


def test_the_workspace_event_feed_does_not_name_a_failed_file_to_a_key_with_a_ceiling(label_harness, failing_capture):
    admin = label_harness.key("admin_agent")
    trusted = label_harness.key("trusted_local_agent")
    sync(label_harness, admin, [{"path": SHOWN_PATH, "title": "shown", "text": "a public note"}], sensitivity="public")
    failing_capture.add(HIDDEN_PATH)
    # One file imports and one fails, so the sync finishes as completed and lists the failed id.
    status, body = sync(
        label_harness,
        admin,
        [{"path": HIDDEN_PATH, "title": "hidden", "text": "a confidential note", "mtime_ns": 9}, {"path": "/vault/OTHER.md", "title": "o", "text": "o", "mtime_ns": 10}],
        sensitivity="public",
    )
    assert body["status"] == "partial" and body["failed_external_ids"] == [HIDDEN_PATH]
    status, workspace = get(label_harness, "/v0/vnext/workspace", trusted)
    assert status == 200 and HIDDEN_PATH not in json.dumps(workspace)
    connector_events = [e for e in workspace["recent_events"] if e["event_type"] in {"connector.item_failed", "connector.sync_completed"}]
    assert any(e["event_type"] == "connector.item_failed" and e["payload_json"]["external_id"] is None for e in connector_events)
    completed = [e for e in connector_events if e["payload_json"].get("failed_external_ids")]
    assert completed and all(e["payload_json"]["failed_external_ids"] == [None] for e in completed)
    status, workspace = get(label_harness, "/v0/vnext/workspace", admin)
    assert status == 200 and HIDDEN_PATH in json.dumps(workspace)


@pytest.mark.parametrize(
    ("profile", "bound", "limited"),
    [
        ("admin_agent", None, False),
        ("trusted_local_agent", None, True),
        ("admin_agent", "Orchard", None),
        ("trusted_local_agent", "Orchard", None),
        ("project_scoped_agent", None, None),
        ("project_scoped_agent", "Orchard", None),
        ("memory_proposal_agent", None, None),
        ("read_only_agent", None, None),
    ],
    ids=["admin-unbound", "trusted-unbound", "admin-bound", "trusted-bound", "scoped-unbound", "scoped-bound", "proposal", "read-only"],
)
def test_every_permission_profile_is_shown_or_refused_the_failed_file_by_its_limits(label_harness, failing_capture, profile, bound, limited):
    """The two profiles the operator gate admits are the only ones that reach the screens.

    The unbound admin key has no limits and is shown the file. The unbound trusted key has a ceiling below
    ``confidential`` and is shown none. Every other profile, and every key bound to a project, is refused at the gate.
    """

    owner_key = label_harness.key("admin_agent")
    record_failures(label_harness, owner_key, failing_capture)
    key = owner_key if (profile, bound) == ("admin_agent", None) else label_harness.key(profile, project=bound)
    for path in SCREENS:
        status, body = get(label_harness, path, key)
        text = json.dumps(body)
        if limited is None:
            assert status == 403, (path, status, text[:200])
            assert HIDDEN_PATH not in text and SHOWN_PATH not in text, path
        elif limited:
            assert status == 200, (path, status, text[:200])
            assert HIDDEN_PATH not in text, path
        else:
            assert status == 200, (path, status, text[:200])
    status, body = get(label_harness, STATUS, key)
    if limited is False:
        assert HIDDEN_PATH in json.dumps(body)


def test_the_owner_is_shown_the_failed_file_as_it_was_stored(label_harness, failing_capture):
    record_failures(label_harness, None, failing_capture)
    for path in (STATUS, "/v0/vnext/workspace"):
        status, body = get(label_harness, path, None)
        assert status == 200 and HIDDEN_PATH in json.dumps(body), path
    status, body = get(label_harness, STATUS, None)
    items = events_of(body, "connector.item_failed")
    assert {item["payload_json"]["sync_cursor"] for item in items} == {f"5:{SHOWN_PATH}", f"6:{HIDDEN_PATH}"}
    [sync_failed] = events_of(body, "connector.sync_failed")
    assert set(sync_failed["payload_json"]["failed_external_ids"]) == {SHOWN_PATH, HIDDEN_PATH}
