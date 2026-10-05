"""The open-loop review report is labelled over the sources it prints, as well as over its loops.

The report prints ``Source: source:<id>`` for each loop that links a source, and puts the same ids in its
``source_refs``. The ids are held back from a run whose own identity may not read the source, but a later reader of
the artifact has its own ceiling, and the stored label is the only thing that gates it. A loop that was labelled
``internal`` when it was made can link a source that is ``confidential`` now (the source was reclassified, or the
loop was made by a key that could read it), and a report labelled by the loop alone let a reader below confidential
see that id.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_scheduler import SchedulerRunRequest, VNextSchedulerService
from tests.unit.test_vnext_scheduler import InMemorySchedulerStore

EVERYTHING = ("public", "internal", "private", "confidential", "unknown")


class SourceReadingStore(InMemorySchedulerStore):
    """The scheduler fake with the by-id source read the reference fence and the report both use."""

    def __init__(self) -> None:
        super().__init__()
        self.sources = []
        self.source_reads: list[tuple[str, ...]] = []

    def get_sources_by_ids(self, ids, *, include_deleted: bool = False):
        self.source_reads.append(tuple(str(value) for value in ids))
        wanted = {str(value).lower() for value in ids}
        return [dict(row) for row in self.sources if str(row["id"]).lower() in wanted]

    def find_artifact_by_workflow_digest(self, *, artifact_type, workflow, digest, scope_projects=None):
        """The stores return the report an earlier run made for the same ``workflow_digest``."""

        for row in self.artifacts.values():
            metadata = row.get("metadata_json")
            if (
                row.get("artifact_type") == artifact_type
                and isinstance(metadata, dict)
                and metadata.get("workflow") == workflow
                and metadata.get("workflow_digest") == digest
            ):
                return row
        return None


def _store(*, source: dict, loop: dict | None = None) -> tuple[SourceReadingStore, dict, dict]:
    store = SourceReadingStore()
    source_row = {
        "id": str(uuid4()),
        "source_type": "manual_text",
        "title": "A source",
        "domain": "personal",
        "sensitivity": "internal",
        "metadata_json": {},
        **source,
    }
    store.sources.append(source_row)
    loop_row = {
        "id": str(uuid4()),
        "title": "Follow up on the filing",
        "status": "open",
        "description": "Waiting on a reply.",
        "domain": "personal",
        "sensitivity": "internal",
        "source_id": source_row["id"],
        **(loop or {}),
    }
    store.open_loops.append(loop_row)
    return store, source_row, loop_row


def _review(store, **request) -> dict:
    request.setdefault("sensitivity_allowed", EVERYTHING)
    result = VNextSchedulerService(store).run_now(SchedulerRunRequest(workflow_type="open_loop_review", **request))
    return result["artifact"]


@pytest.mark.parametrize("source_sensitivity", ("private", "confidential", "highly_sensitive", "sacred", "regulated"))
def test_a_linked_source_above_the_loop_labels_the_report(source_sensitivity: str) -> None:
    store, source, _loop = _store(source={"sensitivity": source_sensitivity})
    artifact = _review(store)

    assert f"source:{source['id']}" in artifact["content_markdown"]
    assert artifact["metadata_json"]["source_refs"] == [f"source:{source['id']}"]
    assert artifact["sensitivity"] == source_sensitivity


def test_a_linked_source_below_the_loop_does_not_lower_the_report() -> None:
    store, _source, _loop = _store(source={"sensitivity": "public"}, loop={"sensitivity": "private"})
    artifact = _review(store)

    assert artifact["sensitivity"] == "private"


def test_a_linked_source_in_a_restricted_domain_labels_the_domain_of_the_report() -> None:
    store, source, _loop = _store(source={"domain": "health"}, loop={"domain": "project"})
    artifact = _review(store)

    assert f"source:{source['id']}" in artifact["content_markdown"]
    assert artifact["domain"] == "health"


def test_a_report_with_no_linked_source_is_labelled_by_its_loops_and_reads_no_source() -> None:
    store, _source, _loop = _store(source={}, loop={"source_id": None, "sensitivity": "private"})
    artifact = _review(store)

    assert artifact["sensitivity"] == "private"
    assert store.source_reads == []


def test_a_source_the_run_may_not_read_is_neither_printed_nor_counted() -> None:
    """The run's own fence still holds the id back, so the label has nothing to cover."""

    store, source, _loop = _store(source={"sensitivity": "confidential"})
    artifact = _review(
        store,
        agent_identity=AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent"),
        sensitivity_allowed=("public", "internal", "private", "unknown"),
    )

    assert source["id"] not in artifact["content_markdown"]
    assert artifact["metadata_json"]["source_refs"] == []
    assert artifact["sensitivity"] == "internal"


def test_a_store_that_cannot_read_sources_by_id_prints_none_and_labels_by_the_loops() -> None:
    store = InMemorySchedulerStore()
    store.open_loops.append(
        {
            "id": str(uuid4()),
            "title": "Follow up",
            "status": "open",
            "domain": "personal",
            "sensitivity": "internal",
            "source_id": str(uuid4()),
        }
    )
    artifact = _review(store)

    assert artifact["metadata_json"]["source_refs"] == []
    assert artifact["sensitivity"] == "internal"


def test_the_same_inputs_return_the_report_an_earlier_run_made() -> None:
    store, _source, _loop = _store(source={"sensitivity": "confidential"})
    first = _review(store)
    second = _review(store)

    assert second["id"] == first["id"]
    assert len(store.artifacts) == 1


def test_reclassifying_a_linked_source_makes_a_new_report() -> None:
    """The digest that identifies a run covers the sources it prints, so a source that was raised since the last
    report does not hand back the earlier report with its earlier label."""

    store, source, _loop = _store(source={"sensitivity": "internal"})
    first = _review(store)
    assert first["sensitivity"] == "internal"
    source["sensitivity"] = "confidential"
    second = _review(store)

    assert second["id"] != first["id"]
    assert second["sensitivity"] == "confidential"
