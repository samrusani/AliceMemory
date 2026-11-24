"""Charter promotion and label standing through the mounted application."""

from __future__ import annotations

from collections import Counter
from uuid import uuid4

import pytest

from alicebot_api.vnext_agent_keys import create_agent_key
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401

PATH = "/v0/vnext/settings/brain-charter"
PERSONAL = {"persona": "personal"}
ENTERPRISE = {"persona": "enterprise"}
DECLARED_TRUSTED = {"agent_id": "charter-writer", "permission_profile": "trusted_local_agent"}
FORM_FIELDS = {
    "content_markdown": "A revised synthetic charter.",
    "sensitivity": "private",
    "life_domains_json": {"default_domain": "project", "default_sensitivity": "private"},
    "autonomous_rules_json": ["Generated artifacts require explicit review before promotion."],
    "quality_standard_json": ["Show provenance for answers and generated artifacts."],
}
REPLACED_FIELDS = {
    "owner_json": {},
    "active_projects_json": [],
    "communication_style_json": {},
    "priorities_json": {},
}
EMPTY_REQUESTS = [
    pytest.param({}, {}, id="omitted-field"),
    pytest.param({"memory_philosophy_json": {"other": "new"}}, {"other": "new"}, id="omitted-key"),
    pytest.param({"memory_philosophy_json": {"promotion": None}}, {"promotion": None}, id="null"),
]


@pytest.fixture(autouse=True)
def controlled_promotion_environment(monkeypatch):
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.setenv("ALICE_MEMORY_PERSONA", "enterprise")
    monkeypatch.delenv("ALICE_MEMORY_ESCALATION_FILTERS", raising=False)
    monkeypatch.delenv("ALICE_MEMORY_OWNER_ALIASES", raising=False)


def _caller(harness, name):
    if name == "owner":
        return None, {}, ("user", None)
    if name.startswith("declared_"):
        claims = {**DECLARED_TRUSTED, "permission_profile": name.removeprefix("declared_")}
        return None, claims, ("agent", claims["agent_id"])
    with harness.store() as store:
        _, raw_key = create_agent_key(
            store, user_id=harness.user_id, agent_id="charter-writer", permission_profile=name,
        )
    return raw_key, {}, ("agent", "charter-writer")


def _snapshot(harness):
    with harness.store() as store:
        return store.get_brain_charter(), {row["id"]: row for row in store.list_events(limit=100)}


def _save(harness, key, claims, **fields):
    status, reply, _ = harness.request("PUT", PATH, key=key, payload={**FORM_FIELDS, **claims, **fields})
    return status, reply


def _save_allowed(harness, key, claims, actor, **fields):
    _, before = _snapshot(harness)
    status, reply = _save(harness, key, claims, **fields)
    assert status == 200, reply
    stored, after = _snapshot(harness)
    assert reply["brain_charter"]["id"] == str(stored["id"])
    events = [event for event_id, event in after.items() if event_id not in before]
    assert [(event["event_type"], event["actor_type"], event["actor_id"]) for event in events] == [
        ("brain_charter.upserted", *actor),
    ]
    return stored


def _assert_refusal(harness, before, status, reply, reason, actor):
    assert status == 403, reply
    assert reply["policy_decision"]["reasons"] == [reason]
    row, events = _snapshot(harness)
    assert row == before[0]
    added = [event for event_id, event in events.items() if event_id not in before[1]]
    assert Counter(event["event_type"] for event in added) == {"policy.decision": 1, "agent.policy_blocked": 1}
    for event in added:
        assert (event["actor_type"], event["actor_id"]) == actor
        assert (event["target_type"], event["target_id"]) == ("http_route", PATH)
        assert event["payload_json"]["policy_decision"] == reply["policy_decision"]


def _commit_observer(harness, *, keyed):
    if keyed:
        return harness.key("trusted_local_agent"), {}
    return None, {**DECLARED_TRUSTED, "agent_id": "charter-observer"}


def _commit(harness, observer):
    key, claims = observer
    status, reply, _ = harness.request("POST", "/v0/vnext/memories/commit", key=key, payload={
        **claims,
        "title": "Synthetic orchard hours",
        "canonical_text": f"The test orchard opens on Saturday. Record {uuid4()}.",
        "domain": "personal",
        "sensitivity": "internal",
        "source_type": "direct_user_instruction",
        "intent": "explicit_remember",
        "confidence": 0.7,
    })
    assert status == 201, reply
    return reply["status"], reply["policy_decision"]["reason"]


@pytest.mark.parametrize("caller", ["trusted_local_agent", "declared_trusted_local_agent"])
@pytest.mark.parametrize(("initial", "fields", "reason"), [
    pytest.param(None, {"memory_philosophy_json": {"promotion": PERSONAL}},
                 "brain_charter_promotion_review_required", id="first-save"),
    pytest.param({}, {"memory_philosophy_json": {"promotion": PERSONAL}},
                 "brain_charter_promotion_review_required", id="add-block"),
    pytest.param({}, {"memory_philosophy_json": {"promotion": {}}},
                 "brain_charter_promotion_review_required", id="add-empty-object"),
    pytest.param({"promotion": PERSONAL}, {"memory_philosophy_json": {"promotion": ENTERPRISE}},
                 "brain_charter_promotion_review_required", id="change-block"),
    pytest.param({"promotion": PERSONAL}, {"sensitivity": "internal"},
                 "brain_charter_label_review_required", id="lower-to-internal"),
    pytest.param({"promotion": PERSONAL}, {"sensitivity": "public"},
                 "brain_charter_label_review_required", id="lower-to-public"),
])
def test_trusted_writes_cannot_change_promotion_or_lower_the_label(label_harness, caller, initial, fields, reason):
    h = label_harness
    if initial is not None:
        _save_allowed(h, None, {}, ("user", None), memory_philosophy_json=initial)
    key, claims, actor = _caller(h, caller)
    observer = _commit_observer(h, keyed=key is not None)
    before_commit = _commit(h, observer)
    if key is not None and initial == {"promotion": PERSONAL}:
        assert before_commit == ("committed", "promotion_auto_promoted")
    before = _snapshot(h)

    status, reply = _save(h, key, claims, **fields)

    _assert_refusal(h, before, status, reply, reason, actor)
    assert _commit(h, observer) == before_commit


@pytest.mark.parametrize("caller", ["trusted_local_agent", "declared_trusted_local_agent", "owner", "admin_agent"])
@pytest.mark.parametrize(("fields", "expected_philosophy"), EMPTY_REQUESTS)
def test_empty_promotion_requests_follow_the_callers_standing(label_harness, caller, fields, expected_philosophy):
    h = label_harness
    _save_allowed(
        h, None, {}, ("user", None), memory_philosophy_json={"promotion": PERSONAL, "old": "discard"},
        owner_json={"name": "Fixture"}, active_projects_json=[{"name": "Fixture"}],
        communication_style_json={"tone": "plain"}, priorities_json={"old": "discard"},
        life_domains_json={"project": "old"}, autonomous_rules_json=["Old rule"], quality_standard_json=["Old standard"],
    )
    key, claims, actor = _caller(h, caller)
    keep = caller in ("trusted_local_agent", "declared_trusted_local_agent")
    if keep:
        observer = _commit_observer(h, keyed=key is not None)
        before_commit = _commit(h, observer)
        if key is not None:
            assert before_commit == ("committed", "promotion_auto_promoted")

    stored = _save_allowed(h, key, claims, actor, **fields)

    assert {name: stored[name] for name in FORM_FIELDS | REPLACED_FIELDS} == FORM_FIELDS | REPLACED_FIELDS
    expected = {**expected_philosophy, "promotion": PERSONAL} if keep else expected_philosophy
    assert stored["memory_philosophy_json"] == expected
    if keep:
        assert _commit(h, observer) == before_commit


def test_omitting_legacy_invalid_promotion_keeps_the_stored_value(label_harness):
    h = label_harness
    with h.store() as store:
        store.upsert_brain_charter({"content_markdown": "Old policy", "memory_philosophy_json": {"promotion": "invalid"}})
    key, claims, actor = _caller(h, "trusted_local_agent")
    observer = _commit_observer(h, keyed=True)
    before_commit = _commit(h, observer)
    assert before_commit[0] == "confirmation_required"

    stored = _save_allowed(h, key, claims, actor)

    assert stored["memory_philosophy_json"] == {"promotion": "invalid"}
    assert _commit(h, observer) == before_commit


def test_omitting_legacy_promotion_preserves_the_database_value_without_decoding(label_harness):
    h = label_harness
    _save_allowed(h, None, {}, ("user", None))
    deep_value = "[" * 1500 + '"fixture"' + "]" * 1500
    stored_block = '{"persona": "personal", "metadata": ' + deep_value + "}"
    with h.store() as store:
        store.conn.execute(
            "UPDATE brain_charters SET memory_philosophy_json = jsonb_build_object('promotion', %s::jsonb)",
            (stored_block,),
        )
    key, claims, actor = _caller(h, "trusted_local_agent")

    stored = _save_allowed(h, key, claims, actor, memory_philosophy_json={"other": "new"})

    assert stored["memory_philosophy_json"]["other"] == "new"
    with h.store() as store:
        assert store.conn.execute(
            "SELECT (memory_philosophy_json -> 'promotion') = %s::jsonb AS preserved FROM brain_charters",
            (stored_block,),
        ).fetchone()["preserved"]


@pytest.mark.parametrize("caller", ["owner", "admin_agent", "declared_admin_agent"])
def test_review_standing_can_set_change_drop_promotion_and_lower_the_label(label_harness, caller):
    h = label_harness
    key, claims, actor = _caller(h, caller)
    for philosophy, label in [({"promotion": PERSONAL}, "private"), ({"promotion": ENTERPRISE}, "internal"), ({}, "public")]:
        stored = _save_allowed(h, key, claims, actor, sensitivity=label, memory_philosophy_json=philosophy)
        assert (stored["sensitivity"], stored["memory_philosophy_json"]) == (label, philosophy)


def test_trusted_saves_can_raise_a_label_and_leave_promotion_equal_as_json(label_harness):
    h = label_harness
    admin = h.key("admin_agent")
    key, claims, actor = _caller(h, "trusted_local_agent")
    stored = _save_allowed(h, key, claims, actor, sensitivity="internal")
    assert (stored["sensitivity"], stored["memory_philosophy_json"]) == ("internal", {})
    stored = _save_allowed(h, key, claims, actor, sensitivity="unknown")
    assert stored["sensitivity"] == "unknown"

    block = {**PERSONAL, "metadata": {"number": [1], "enabled": True}}
    status, reply = _save(h, admin, {}, sensitivity="internal", memory_philosophy_json={"promotion": block})
    assert status == 200, reply
    equal_block = {"metadata": {"enabled": True, "number": [1.0]}, **PERSONAL}
    stored = _save_allowed(h, key, claims, actor, memory_philosophy_json={"promotion": equal_block, "other": "new"})
    assert stored["sensitivity"] == "private"
    assert stored["memory_philosophy_json"] == {"promotion": block, "other": "new"}

    different_block = {**PERSONAL, "metadata": {"number": [True], "enabled": True}}
    before = _snapshot(h)
    status, reply = _save(h, key, claims, memory_philosophy_json={"promotion": different_block})
    _assert_refusal(h, before, status, reply, "brain_charter_promotion_review_required", actor)


@pytest.mark.parametrize("caller", ["trusted_local_agent", "admin_agent", "owner", "declared_trusted_local_agent"])
@pytest.mark.parametrize("promotion", ["invalid", {"persona": ["invalid"]}, {"persona": "invalid"}])
def test_a_promotion_the_reader_refuses_answers_400_without_storage(label_harness, caller, promotion):
    h = label_harness
    _save_allowed(h, None, {}, ("user", None), memory_philosophy_json={"promotion": PERSONAL})
    key, claims, _ = _caller(h, caller)
    before = _snapshot(h)

    status, reply = _save(h, key, claims, memory_philosophy_json={"promotion": promotion})

    assert status == 400, reply
    assert _snapshot(h) == before


@pytest.mark.parametrize("profile", ["trusted_local_agent", "admin_agent"])
def test_bound_keys_keep_the_existing_charter_refusal(label_harness, profile):
    h = label_harness
    _save_allowed(h, None, {}, ("user", None), memory_philosophy_json={"promotion": PERSONAL})
    with h.store() as store:
        project = store.create_project({"name": "Fixture project", "slug": "fixture-project"})
    key = h.key(profile, project=[str(project["id"])])
    before, _ = _snapshot(h)
    status, reply = _save(h, key, {})
    assert status == 403 and reply["policy_decision"]["reasons"] == ["unbound_operator_key_required"]
    assert _snapshot(h)[0] == before


def test_requested_and_stored_ceiling_refusals_still_take_precedence(label_harness):
    h = label_harness
    key, claims, actor = _caller(h, "trusted_local_agent")
    before = _snapshot(h)
    status, reply = _save(h, key, claims, sensitivity="confidential")
    assert status == 403 and "sensitivity_above_agent_ceiling" in reply["policy_decision"]["reasons"]
    assert _snapshot(h)[0] == before[0]

    admin = h.key("admin_agent")
    status, reply = _save(h, admin, {}, sensitivity="confidential")
    assert status == 200, reply
    before = _snapshot(h)
    status, reply = _save(h, key, claims)
    _assert_refusal(h, before, status, reply, "brain_charter_outside_caller_limits", actor)
