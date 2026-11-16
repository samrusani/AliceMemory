"""A temporal expiry does not cancel a waiting inline confirmation."""

from datetime import UTC, datetime

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401


def test_postgres_expired_waiting_write_can_be_confirmed_but_is_not_in_pack(label_harness):
    """The limitations page changes when expiry or confirmation changes.

    Mutation: replace ``valid_to_iso = _valid_to_iso(valid_to)`` with a far-future date in expire.
    """
    h = label_harness
    key = h.key("project_scoped_agent", project="alpha")
    status, pending, _ = h.request("POST", "/v0/vnext/memories/commit", key=key, payload={
        "title": "amber windmill", "canonical_text": "The amber windmill is adjacent to the orchard.",
        "domain": "project", "sensitivity": "internal", "project_scope": ["alpha"], "confidence": 0.7,
    })
    assert status == 201 and pending["status"] == "confirmation_required"
    memory_id, confirmation_id = pending["memory"]["id"], pending["confirmation_id"]
    status, expired, _ = h.request("POST", "/v0/vnext/memories/expire", key=key, payload={
        "memory_id": memory_id, "reason": "Synthetic expiry",
    })
    assert status == 200 and expired["status"] == "expired"
    status, confirmed, _ = h.request("POST", "/v0/vnext/memories/confirm", key=key, payload={
        "confirmation_id": confirmation_id, "action": "confirm",
    })
    assert status == 200 and confirmed["status"] == "committed"
    row = confirmed["memory"]
    assert row["status"] == "active"
    assert datetime.fromisoformat(row["valid_to"].replace("Z", "+00:00")) < datetime.now(UTC)
    status, pack, _ = h.request("POST", "/v0/vnext/context-packs", key=key, payload={
        "query": "amber windmill orchard",
    })
    assert status == 201 and all(item["id"] != memory_id for item in pack["relevant_memories"])
    assert any(item.get("target_id") == memory_id for item in pack["recent_changes"])
