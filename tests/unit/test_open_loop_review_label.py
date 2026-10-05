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

import alicebot_api.vnext_scheduler as scheduler_module
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


class SingleSourceStore(InMemorySchedulerStore):
    """The scheduler fake with a one-source-at-a-time read and no bulk read (the fallback both the reference fence and
    the label take when a store has no ``get_sources_by_ids``)."""

    def __init__(self) -> None:
        super().__init__()
        self.sources = []
        self.single_reads: list[str] = []

    def get_source(self, source_id):
        self.single_reads.append(str(source_id))
        for row in self.sources:
            if str(row["id"]).lower() == str(source_id).lower():
                return dict(row)
        return None

    find_artifact_by_workflow_digest = SourceReadingStore.find_artifact_by_workflow_digest


def _store(
    *, source: dict, loop: dict | None = None, store: InMemorySchedulerStore | None = None
) -> tuple[SourceReadingStore | SingleSourceStore, dict, dict]:
    store = store if store is not None else SourceReadingStore()
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


def test_moving_a_linked_source_to_a_restricted_domain_makes_a_new_report() -> None:
    """Only the domain of the source changes, and its sensitivity stays where it was. A digest over the sensitivity
    alone would hand back the earlier report, and with it a domain the source no longer has."""

    store, source, _loop = _store(source={"domain": "personal", "sensitivity": "internal"})
    first = _review(store)
    assert first["domain"] == "unknown"
    source["domain"] = "health"
    second = _review(store)

    assert second["id"] != first["id"]
    assert second["metadata_json"]["workflow_digest"] != first["metadata_json"]["workflow_digest"]
    assert second["domain"] == "health"
    assert second["sensitivity"] == "internal"


def _hashed_payloads(monkeypatch) -> list[dict]:
    """The payloads the open-loop review hashed into its run digest."""

    seen: list[dict] = []
    real = scheduler_module._workflow_digest

    def spy(payload):
        if isinstance(payload, dict) and payload.get("workflow") == "open_loop_review":
            seen.append(payload)
        return real(payload)

    monkeypatch.setattr(scheduler_module, "_workflow_digest", spy)
    return seen


@pytest.mark.parametrize("how", ("no_source_id", "withheld_by_the_run_fence"))
def test_a_run_that_prints_no_source_keeps_the_digest_it_always_had(monkeypatch, how: str) -> None:
    """With no source printed the payload has no ``linked_sources`` key (not an empty list), so the digest is the one
    the review had before the key existed and the report an earlier run made is still found."""

    seen = _hashed_payloads(monkeypatch)
    if how == "no_source_id":
        store, _source, _loop = _store(source={}, loop={"source_id": None})
        request: dict = {}
    else:
        store, _source, _loop = _store(source={"sensitivity": "confidential"})
        request = {
            "agent_identity": AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent"),
            "sensitivity_allowed": ("public", "internal", "private", "unknown"),
        }
    first = _review(store, **request)
    second = _review(store, **request)

    assert len(seen) == 2
    for payload in seen:
        assert "linked_sources" not in payload
    expected = scheduler_module._workflow_digest({key: value for key, value in seen[0].items() if key != "linked_sources"})
    assert first["metadata_json"]["workflow_digest"] == expected
    assert second["id"] == first["id"]


def test_a_run_that_prints_a_source_hashes_its_id_domain_and_sensitivity(monkeypatch) -> None:
    seen = _hashed_payloads(monkeypatch)
    store, source, _loop = _store(source={"domain": "health", "sensitivity": "confidential"})
    _review(store)

    assert seen[0]["linked_sources"] == [
        {"id": str(source["id"]), "domain": "health", "sensitivity": "confidential"}
    ]


def test_a_store_that_reads_one_source_at_a_time_labels_the_report_over_it() -> None:
    single = SingleSourceStore()
    store, source, _loop = _store(source={"sensitivity": "confidential", "domain": "health"}, store=single)
    artifact = _review(store)

    assert store.single_reads, "the label has to be taken from a read of the source"
    assert f"source:{source['id']}" in artifact["content_markdown"]
    assert artifact["metadata_json"]["source_refs"] == [f"source:{source['id']}"]
    assert artifact["sensitivity"] == "confidential"
    assert artifact["domain"] == "health"


def test_a_store_that_reads_one_source_at_a_time_makes_a_new_report_for_a_reclassified_source() -> None:
    single = SingleSourceStore()
    store, source, _loop = _store(source={"sensitivity": "internal"}, store=single)
    first = _review(store)
    source["sensitivity"] = "confidential"
    second = _review(store)

    assert second["id"] != first["id"]
    assert second["sensitivity"] == "confidential"
