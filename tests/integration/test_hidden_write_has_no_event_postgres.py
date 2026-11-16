"""A hidden memory and a missing one leave no event on a refused write."""

from uuid import uuid4

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401


def test_postgres_hidden_and_missing_memory_expiry_leave_no_event(label_harness):
    """The limitations page changes if a refused hidden write starts recording events.

    Mutation: skip the outside-caller-limits check in the expire path.
    """
    h = label_harness
    admin, reader = h.key("admin_agent"), h.key("project_scoped_agent", project="alpha")
    status, created, _ = h.request("POST", "/v0/vnext/memories/commit", key=admin, payload={
        "title": "Hidden", "canonical_text": "Hidden synthetic", "domain": "project",
        "sensitivity": "confidential", "project_scope": ["alpha"],
    })
    assert status == 201 and created["status"] == "confirmation_required"
    answers = []
    for memory_id in (created["memory"]["id"], str(uuid4())):
        with h.store() as store:
            before = store.conn.execute("select count(*) as n from event_log").fetchone()["n"]
        status, answer, _ = h.request("POST", "/v0/vnext/memories/expire", key=reader, payload={
            "memory_id": memory_id, "reason": "Synthetic",
        })
        answers.append((status, answer))
        with h.store() as store:
            assert store.conn.execute("select count(*) as n from event_log").fetchone()["n"] == before
    assert answers[0] == answers[1] and answers[0][0] == 400
