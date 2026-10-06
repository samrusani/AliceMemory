"""A plain source review cannot write back an earlier project assignment."""
from threading import Event
from uuid import uuid4

import pytest

from alicebot_api.routers import vnext_memories as router
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.derived_labels_postgres_support import label_harness, join_thread
from tests.integration.test_derived_labels_concurrency_postgres import _thread


@pytest.mark.parametrize("strict", [False, True])
def test_plain_source_review_serializes_before_project_move(label_harness, monkeypatch, strict):
    from alicebot_api import vnext_label_writes
    monkeypatch.setattr(vnext_label_writes, "STRICT_LOCK_ORDER", strict)
    h = label_harness
    alpha, beta = str(uuid4()), str(uuid4())
    with h.store() as store:
        for project, name in ((alpha, "Alpha"), (beta, "Beta")):
            store.create_project({"id": project, "name": name, "slug": name.lower()})
    source = h.source(scope=(alpha,))
    read, release = Event(), Event()
    original = PostgresVNextStore.get_source
    def pause_first_read(self, source_id):
        row = original(self, source_id)
        if not read.is_set():
            read.set()
            assert release.wait(5)
        return row
    monkeypatch.setattr(PostgresVNextStore, "get_source", pause_first_read)
    responses = []
    review, review_failures = _thread(lambda: responses.append(router.review_vnext_source(
        source["id"], router.VNextSourceReviewRequest(user_id=h.user_id, action="review"))))
    assert read.wait(2)
    move, move_failures = _thread(lambda: responses.append(router.review_vnext_source(
        source["id"], router.VNextSourceReviewRequest(user_id=h.user_id, action="assign_project", project_id=beta))))
    try:
        h.wait_relabel()
        assert review.is_alive() and move.is_alive()
    finally:
        release.set()
        join_thread(review, review_failures)
        join_thread(move, move_failures)
    assert all(response.status_code == 200 for response in responses)
    with h.store() as store:
        assert store.get_source(str(source["id"]))["metadata_json"]["project_scope"] == [beta]


def test_event_prefilter_preserves_visible_and_noncontiguous_ceiling_controls(label_harness):
    h = label_harness
    public = h.memory()
    private_source = h.source(sensitivity="confidential")
    hidden = h.memory(source=private_source)
    with h.store() as store:
        public_events = store.list_memory_events(sensitivity_allowed=["public"], limit=100)
        assert str(public["id"]) in {str(event["target_id"]) for event in public_events}
        assert str(hidden["id"]) not in {str(event["target_id"]) for event in public_events}
        # This is a conservative SQL filter. A gap in the allowed set is left
        # to effective admission because a derived label can increase into it.
        all_events = store.list_memory_events(sensitivity_allowed=["public", "regulated"], limit=100)
        assert str(hidden["id"]) in {str(event["target_id"]) for event in all_events}
