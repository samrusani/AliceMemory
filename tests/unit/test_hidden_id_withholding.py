"""A key with limits is not shown the id of a row it may not read.

Mutation: ``strip_hidden_ids`` returns ``value`` before it walks. ``test_listing_fields_lose_a_hidden_id_and_the_count_matches``
fails because the confidential memory id is still in ``candidate_memory_ids``.
"""

from __future__ import annotations

from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_hidden_ids import apply_hidden_id_withholding, strip_hidden_ids

HIDDEN = "11111111-1111-4111-8111-111111111111"
VISIBLE = "22222222-2222-4222-8222-222222222222"
SOURCE = "33333333-3333-4333-8333-333333333333"
LOOP = "44444444-4444-4444-8444-444444444444"
ARTIFACT = "55555555-5555-4555-8555-555555555555"


def _identity(profile: str) -> AgentIdentity:
    return AgentIdentity(agent_id="reader", permission_profile=profile, auth="agent_api_key")


class _Store:
    def __init__(self) -> None:
        self.rows = {
            "memory": [
                {"id": HIDDEN, "domain": "project", "sensitivity": "confidential"},
                {"id": VISIBLE, "domain": "project", "sensitivity": "public"},
            ],
            "source": [{"id": SOURCE, "domain": "project", "sensitivity": "confidential"}],
            "open_loop": [{"id": LOOP, "domain": "project", "sensitivity": "confidential"}],
            "artifact": [{"id": ARTIFACT, "domain": "project", "sensitivity": "confidential"}],
        }

    def read_label_rows(self, kind: str, ids: list[str]):
        return [row for row in self.rows.get(kind, []) if row["id"] in ids]


def _payload() -> dict:
    return {
        "items": [{"id": VISIBLE, "metadata_json": {"candidate_memory_ids": [HIDDEN, VISIBLE]}}],
        "count": 1,
        "source_refs": [f"source:{SOURCE}", f"memory:{VISIBLE}"],
        "artifact_id": ARTIFACT,
        "content_markdown": f"The note {HIDDEN} stays in words only.",
        "member_snapshots": [{"id": HIDDEN, "status": "active"}, {"id": VISIBLE, "status": "active"}],
        "open_loop_ids": [LOOP, VISIBLE],
        "detail": f"missing {HIDDEN}",
    }


def test_listing_fields_lose_a_hidden_id_and_the_count_matches() -> None:
    hidden = {HIDDEN, SOURCE, LOOP, ARTIFACT}
    payload = strip_hidden_ids(_payload(), hidden)
    assert payload["items"][0]["metadata_json"]["candidate_memory_ids"] == [VISIBLE]
    assert payload["source_refs"] == [f"memory:{VISIBLE}"]
    assert payload["artifact_id"] is None
    assert HIDDEN not in payload["content_markdown"]
    assert "stays in words only" in payload["content_markdown"]
    assert payload["member_snapshots"] == [{"id": VISIBLE, "status": "active"}]
    assert payload["open_loop_ids"] == [VISIBLE]
    assert payload["detail"] == f"missing {HIDDEN}"
    assert payload["count"] == 1


def test_an_archived_row_is_withheld_and_a_forgotten_row_stays() -> None:
    archived = "66666666-6666-4666-8666-666666666666"
    forgotten = "77777777-7777-4777-8777-777777777777"
    deleted = "88888888-8888-4888-8888-888888888888"
    store = _Store()
    store.rows["memory"].extend(
        [
            {"id": archived, "domain": "project", "sensitivity": "public", "status": "archived"},
            {"id": forgotten, "domain": "project", "sensitivity": "public", "status": "forgotten"},
        ]
    )
    store.rows["source"].append(
        {"id": deleted, "domain": "project", "sensitivity": "public", "deleted_at": "2026-01-01T00:00:00Z"}
    )
    payload = {"memory_ids": [archived, forgotten, VISIBLE], "source_id": deleted}
    limited = apply_hidden_id_withholding(store, _identity("read_only_agent"), payload)
    assert limited["memory_ids"] == [forgotten, VISIBLE]
    assert limited["source_id"] is None


def test_an_id_the_caller_named_stays_and_another_hidden_id_does_not() -> None:
    """The id in the request is not a row the caller discovered. Another unreadable id still goes."""

    payload = {
        "target_id": HIDDEN,
        "source_refs": [f"source:{SOURCE}", f"memory:{HIDDEN}"],
        "candidate_memory_ids": [HIDDEN, VISIBLE],
    }
    shown = strip_hidden_ids(payload, {HIDDEN, SOURCE}, keep={HIDDEN})
    assert shown["target_id"] == HIDDEN
    assert shown["source_refs"] == [f"memory:{HIDDEN}"]
    assert shown["candidate_memory_ids"] == [HIDDEN, VISIBLE]


def test_every_spelling_of_a_hidden_id_is_removed() -> None:
    spellings = [
        HIDDEN.upper(),
        HIDDEN.replace("-", ""),
        "{" + HIDDEN + "}",
        "urn:uuid:" + HIDDEN,
    ]
    payload = {"candidate_memory_ids": spellings, "memory_id": HIDDEN.upper()}
    shown = apply_hidden_id_withholding(_Store(), _identity("trusted_local_agent"), payload)
    assert shown["candidate_memory_ids"] == []
    assert shown["memory_id"] is None
    kept = apply_hidden_id_withholding(_Store(), _identity("admin_agent"), payload)
    assert kept["candidate_memory_ids"] == spellings


def test_a_limited_key_loses_every_id_kind_and_the_owner_keeps_them() -> None:
    store = _Store()
    limited = apply_hidden_id_withholding(store, _identity("trusted_local_agent"), _payload())
    assert HIDDEN not in limited["items"][0]["metadata_json"]["candidate_memory_ids"]
    assert SOURCE not in "".join(str(item) for item in limited["source_refs"])
    assert f"memory:{VISIBLE}" in limited["source_refs"]
    assert limited["artifact_id"] is None
    assert LOOP not in limited["open_loop_ids"]
    owner = apply_hidden_id_withholding(store, None, _payload())
    admin = apply_hidden_id_withholding(store, _identity("admin_agent"), _payload())
    assert owner["artifact_id"] == ARTIFACT
    assert admin["artifact_id"] == ARTIFACT
    assert owner is not None and admin["member_snapshots"][0]["id"] == HIDDEN
