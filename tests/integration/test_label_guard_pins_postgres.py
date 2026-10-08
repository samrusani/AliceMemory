"""A request cache belongs to the store whose scope opened it, across tenants on PostgreSQL."""
from __future__ import annotations

from uuid import uuid4

from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_label_guard import LabelGuard, label_read_scope, request_row_cache
from alicebot_api.vnext_retrieval import VNextRetrievalService
from tests.integration.conftest import lock_label_fixture
from tests.integration.derived_labels_postgres_support import LabelHarness, label_harness  # noqa: F401


def test_second_tenant_never_receives_the_first_tenants_cached_rows(label_harness):
    one = label_harness
    two = LabelHarness(one.urls, uuid4())
    with two.store() as store:
        ContinuityStore(store.conn).create_user(two.user_id, f"tenant-two-{two.user_id}@example.invalid", "Tenant two")
    with one.store() as store:
        lock_label_fixture(store)
        private = store.create_memory({"memory_key": str(uuid4()), "canonical_text": "tenant one observation",
                                       "status": "active", "domain": "project", "sensitivity": "public",
                                       "metadata_json": {}})
    memory_id = str(private["id"])
    with one.store() as first, two.store() as second:
        with label_read_scope(first):
            assert VNextRetrievalService(first)._memories_by_ids([memory_id], effective=False)
            assert request_row_cache(first, "retrieval_memories")
            # Same ambient request, the other tenant's store: it gets no cache and no row.
            assert request_row_cache(second, "retrieval_memories") is None
            assert VNextRetrievalService(second)._memories_by_ids([memory_id], effective=False) == {}
            assert LabelGuard(second, active=True)._state() is not LabelGuard(first, active=True)._state()
