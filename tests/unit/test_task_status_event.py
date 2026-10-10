"""The event that records a task's new status names the artifact the task made where the event guard reads it.

A queued task that completes writes ``task.updated`` with the artifact it made inside ``details``, and the event guard
reads the ids of labelled rows at the top of a payload and not inside ``details``. The artifact of a task takes the labels
of the task, so a caller with a ceiling must not be shown the id of the artifact of a task above it. These tests run the
real store method that writes the event and the real guard that judges it:

* the ids of the artifact are written at the top of the payload, under the keys the guard maps, and ``details`` is kept;
* a caller with limits is shown the event only when it may read every artifact it names, as for ``queue.task_completed``;
* a value that is not an id names an artifact that cannot be resolved, so no caller with limits is shown the event;
* the details the queue service hands to the store are the ones the payload reads.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from alicebot_api.vnext_label_guard import LabelGuard, event_references, label_read_scope
from alicebot_api.vnext_queue import QueueTaskRequest, VNextQueueService
from alicebot_api.vnext_store import PostgresVNextStore
from tests.unit.test_vnext_queue import InMemoryVNextQueueStore
from tests.unit.test_vnext_store import RecordingConnection, RecordingCursor, _event_row

CEILING = ("public", "internal", "private", "unknown")


def _written_event(details: dict[str, object] | None, *, status: str = "completed") -> dict[str, object]:
    """The ``task.updated`` event that ``PostgresVNextStore.update_task_status`` inserts, as the guard reads an event."""

    task_id = str(uuid4())
    cursor = RecordingCursor(fetchone_results=[{"id": task_id, "status": status}, _event_row(task_id)])
    PostgresVNextStore(RecordingConnection(cursor)).update_task_status(task_id=task_id, status=status, details=details)
    inserts = [params for query, params in cursor.statements if "INSERT INTO event_log" in query]
    assert len(inserts) == 1
    values = [getattr(value, "obj", value) for value in inserts[0] or ()]
    assert "task.updated" in values and task_id in values
    payload = next(value for value in values if isinstance(value, dict) and value.get("operation") == "update_status")
    return {"event_type": "task.updated", "target_type": "task", "target_id": task_id, "payload_json": payload}


def test_a_completed_task_names_its_artifact_at_the_top_of_the_event_payload() -> None:
    artifact = str(uuid4())
    event = _written_event({"output_artifact_id": artifact, "metadata_json": {"artifact_id": artifact}})
    payload = event["payload_json"]
    assert isinstance(payload, dict)

    assert payload["artifact_id"] == artifact and "artifact_ids" not in payload
    assert (payload["operation"], payload["status"]) == ("update_status", "completed")
    # The details are kept as they were given.
    assert payload["details"] == {"output_artifact_id": artifact, "metadata_json": {"artifact_id": artifact}}
    assert event_references(event) == [("artifact", artifact)]


def test_the_ids_of_an_artifact_are_read_from_the_output_column_and_from_the_metadata() -> None:
    first, second = str(uuid4()), str(uuid4())
    cases = (
        ({"output_artifact_id": first}, {"artifact_id": first}),
        ({"metadata_json": {"artifact_id": first}}, {"artifact_id": first}),
        ({"output_artifact_id": first, "metadata_json": {"artifact_id": first, "note": "x"}}, {"artifact_id": first}),
        ({"output_artifact_id": first, "metadata_json": {"artifact_id": second}}, {"artifact_ids": [first, second]}),
    )
    for details, expected in cases:
        payload = _written_event(details)["payload_json"]
        assert isinstance(payload, dict)
        top = {key: value for key, value in payload.items() if key in {"artifact_id", "artifact_ids"}}
        assert top == expected, details
        assert payload["details"] == details


def test_a_status_change_that_names_no_artifact_writes_none() -> None:
    for details in (None, {}, {"error_message": "Queue task processing failed"}, {"metadata_json": {"note": "x"}}):
        event = _written_event(details, status="failed")
        payload = event["payload_json"]
        assert isinstance(payload, dict)
        assert not {"artifact_id", "artifact_ids"} & payload.keys(), details
        assert payload["status"] == "failed"
        assert event_references(event) == []


class _ArtifactLabels:
    """The label reads of a store that holds artifacts: the one surface the guard needs to judge a reference."""

    def __init__(self, labels: dict[str, str]) -> None:
        self.rows = [
            {"id": artifact, "artifact_type": "summary", "domain": "project", "sensitivity": sensitivity, "metadata_json": {}}
            for artifact, sensitivity in labels.items()
        ]

    def read_label_rows(self, kind: str, ids) -> list[dict[str, object]]:
        return [dict(row) for row in self.rows if kind == "artifact" and row["id"] in ids]


def _shown(store: _ArtifactLabels, events: list[dict[str, object]], *, limited: bool = True) -> list[dict[str, object]]:
    with label_read_scope(store):
        guard = LabelGuard(store, active=limited, sensitivity_allowed=CEILING)
        return guard.admit_events(events)


def _completion(artifact: str) -> dict[str, object]:
    """The two events a completed task leaves, with the artifact it made."""

    update = _written_event({"output_artifact_id": artifact, "metadata_json": {"artifact_id": artifact}})
    completed = {
        "event_type": "queue.task_completed", "target_type": "task", "target_id": update["target_id"],
        "payload_json": {"artifact_id": artifact},
    }
    return {"update": update, "completed": completed}


def test_a_caller_with_a_ceiling_is_shown_a_task_completion_only_when_it_may_read_the_artifact() -> None:
    hidden, shown = str(uuid4()), str(uuid4())
    store = _ArtifactLabels({hidden: "confidential", shown: "public"})
    above, within = _completion(hidden), _completion(shown)
    events = [above["update"], above["completed"], within["update"], within["completed"]]

    kept = _shown(store, events)

    assert kept == [within["update"], within["completed"]]
    assert hidden not in str(kept)
    # A caller with no limits is shown every one of them.
    assert _shown(store, events, limited=False) == events


def test_an_event_that_names_two_artifacts_is_shown_only_when_both_may_be_read() -> None:
    hidden, first, second = str(uuid4()), str(uuid4()), str(uuid4())
    store = _ArtifactLabels({hidden: "confidential", first: "public", second: "internal"})
    both_readable = _written_event({"output_artifact_id": first, "metadata_json": {"artifact_id": second}})
    one_hidden = _written_event({"output_artifact_id": first, "metadata_json": {"artifact_id": hidden}})

    assert event_references(one_hidden) == [("artifact", first), ("artifact", hidden)]
    assert _shown(store, [both_readable, one_hidden]) == [both_readable]


def test_an_event_whose_artifact_is_not_stored_or_is_not_an_id_is_shown_to_no_caller_with_limits() -> None:
    store = _ArtifactLabels({})
    events = [
        _written_event({"output_artifact_id": str(uuid4())}),
        _written_event({"metadata_json": {"artifact_id": {"id": str(uuid4())}}}),
        _written_event({"metadata_json": {"artifact_id": 5}}),
    ]

    assert _shown(store, events) == []
    assert _shown(store, events, limited=False) == events


def test_the_details_the_queue_service_gives_the_store_are_the_ones_the_event_names() -> None:
    class Recording(InMemoryVNextQueueStore):
        def __init__(self) -> None:
            super().__init__()
            self.updates: list[tuple[str, dict[str, object] | None]] = []

        def update_task_status(self, *, task_id, status, details=None, **kwargs):  # type: ignore[no-untyped-def]
            self.updates.append((status, details))
            return super().update_task_status(task_id=task_id, status=status, details=details, **kwargs)

    store = Recording()
    service = VNextQueueService(store)
    service.enqueue_task(
        QueueTaskRequest(title="task", task_type="summarize", instructions="do it", domain="project", sensitivity="confidential")
    )
    result = service.process_next_task()

    assert result.status == "completed" and result.artifact_id
    (status, details), = store.updates
    assert status == "completed" and details is not None
    event = _written_event(details)
    assert event_references(event) == [("artifact", result.artifact_id)]
    payload = event["payload_json"]
    assert isinstance(payload, dict) and payload["artifact_id"] == result.artifact_id


@pytest.mark.parametrize("status", ["completed", "failed", "running"])
def test_the_status_and_operation_of_the_event_are_unchanged(status: str) -> None:
    payload = _written_event({"error_message": "x"}, status=status)["payload_json"]
    assert isinstance(payload, dict)
    assert payload == {"operation": "update_status", "status": status, "details": {"error_message": "x"}}
