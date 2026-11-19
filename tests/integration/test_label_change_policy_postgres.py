"""Authorization and committed audit evidence for changes to sensitivity labels."""

from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from alicebot_api.vnext_agent_keys import hash_agent_key
from alicebot_api.vnext_connectors import VNextConnectorService
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401 (fixture)


CONNECTOR = "browser_clipper"
CONNECTOR_PATH = f"/v0/vnext/connectors/{CONNECTOR}"
REVIEW_CASES = [
    ("update", "private", "confidential"),
    ("review", "private", "confidential"),
    ("update", "private", "public"),
    ("update", "unknown", "private"),
    ("assign_project", "private", "confidential"),
]


def _review(h, source_id, payload, *, key=None):
    return h.request("POST", f"/v0/vnext/sources/{source_id}/review", payload=payload, key=key)[:2]


def _config(h, payload, *, key=None, connector=CONNECTOR):
    return h.request("PATCH", f"/v0/vnext/connectors/{connector}/config", payload=payload, key=key)[:2]


def _snapshot(h):
    # Each test has a disposable database. Inspect every table through a separate,
    # read-only transaction so the assertion covers committed rows outside the route.
    with psycopg.connect(h.urls["admin"], options="-c default_transaction_read_only=on") as conn:
        tables = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        ).fetchall()
        return {
            table: conn.execute(
                sql.SQL("SELECT to_jsonb(t) FROM public.{} t ORDER BY to_jsonb(t)::text").format(sql.Identifier(table))
            ).fetchall()
            for (table,) in tables
        }


def _new_events(before, after):
    return [row for (row,) in after["event_log"] if (row,) not in before["event_log"]]


def _assert_refusal(status, body):
    assert status == 403
    assert set(body) == {"detail", "policy_decision"}
    assert body["detail"] == "agent policy blocked this action"
    decision = body["policy_decision"]
    assert decision["action"] == "sensitivity.change"
    assert decision["decision"] == "blocked"
    assert decision["reasons"] == ["human_or_admin_review_required"]


def _assert_key_use(before, after, key):
    if key is None:
        assert before == after == []
        return None
    key_hash = hash_agent_key(key)
    (previous,) = [row for (row,) in before if row["key_hash"] == key_hash]
    (current,) = [row for (row,) in after if row["key_hash"] == key_hash]
    used_at = current["last_used_at"]
    assert used_at is not None and used_at != previous["last_used_at"]
    assert current == {**previous, "last_used_at": used_at}
    assert [row for (row,) in after if row["key_hash"] != key_hash] == [
        row for (row,) in before if row["key_hash"] != key_hash
    ]
    return current["agent_id"]


def _assert_other_tables_equal(before, after, excluded):
    assert before.keys() == after.keys()
    for table in before.keys() - excluded:
        assert after[table] == before[table], table


def _assert_only_key_use_recorded(before, after, key):
    _assert_key_use(before["agent_api_keys"], after["agent_api_keys"], key)
    _assert_other_tables_equal(before, after, {"agent_api_keys"})


def _assert_only_refusal_recorded(before, after, body, target_type, target_id, *, key, declared_agent_id=None):
    key_agent_id = _assert_key_use(before["agent_api_keys"], after["agent_api_keys"], key)
    caller_id = key_agent_id if key is not None else declared_agent_id
    _assert_other_tables_equal(before, after, {"event_log", "agent_identities", "agent_api_keys"})

    old_identities = {row["agent_id"]: row for (row,) in before["agent_identities"]}
    new_identities = {row["agent_id"]: row for (row,) in after["agent_identities"]}
    previous = old_identities.pop(caller_id, None)
    caller = new_identities.pop(caller_id)
    assert new_identities == old_identities
    assert caller["permission_profile"] == body["policy_decision"]["permission_profile"]
    if previous is not None:
        assert caller["updated_at"] != previous["updated_at"]
        assert caller == {**previous, "updated_at": caller["updated_at"]}

    assert all(row in after["event_log"] for row in before["event_log"])
    added = _new_events(before, after)
    assert sorted(row["event_type"] for row in added) == [
        "agent.identity_upserted", "agent.policy_blocked", "policy.decision"
    ]
    for row in added:
        assert row["actor_id"] == caller_id
        if row["event_type"] == "agent.identity_upserted":
            assert (row["target_type"], row["target_id"]) == ("agent_identity", caller["id"])
        else:
            assert (row["target_type"], row["target_id"]) == (target_type, target_id)
            assert row["payload_json"]["policy_decision"] == body["policy_decision"]


def _review_payload(h, action, sensitivity):
    payload = {"action": action, "sensitivity": sensitivity, "title": "Reviewed synthetic note"}
    if action == "assign_project":
        with h.store() as store:
            project = store.create_project({"name": "Label review", "slug": "label-review"})
        payload["project_id"] = str(project["id"])
    return payload


@pytest.mark.parametrize(("action", "current", "requested"), REVIEW_CASES)
def test_a_trusted_key_cannot_change_a_stored_source_label(label_harness, monkeypatch, action, current, requested):
    """Mutation: answer the source sensitivity refusal with HTTP 400."""
    from alicebot_api import vnext_label_writes

    h = label_harness
    source = h.source(sensitivity=current)
    source_id = str(source["id"])
    h.memory(source=source)
    payload = _review_payload(h, action, requested)
    key = h.key("trusted_local_agent")
    h.key("admin_agent")
    before = _snapshot(h)

    def unexpected_preview(*args, **kwargs):
        pytest.fail("source preview ran before sensitivity authorization")

    monkeypatch.setattr(vnext_label_writes, "count_rows_hidden_by_scope_move", unexpected_preview)
    status, body = _review(h, source_id, payload, key=key)
    _assert_refusal(status, body)
    _assert_only_refusal_recorded(before, _snapshot(h), body, "source", source_id, key=key)


@pytest.mark.parametrize("payload", [
    {"action": "update", "sensitivity": "private"},
    {"action": "update", "title": "Revised synthetic note"},
    {"action": "update", "domain": "professional"},
    {"action": "archive", "sensitivity": "confidential"},
])
def test_a_trusted_key_can_review_without_changing_source_sensitivity(label_harness, payload):
    """Mutation: apply the authorization check even when sensitivity is unchanged."""
    h = label_harness
    source_id = str(h.source(sensitivity="private")["id"])
    key = h.key("trusted_local_agent")
    before = _snapshot(h)
    status, body = _review(h, source_id, payload, key=key)
    assert status == 200
    assert body["archived"] is (payload["action"] == "archive")
    assert body["source"]["sensitivity"] == "private"
    with h.store() as store:
        source = store.get_source(source_id)
    if body["archived"]:
        assert source is None
    else:
        assert source["sensitivity"] == "private"
        for field in ("title", "domain"):
            if field in payload:
                assert source[field] == payload[field]
    assert not any(row["event_type"].startswith(("policy.", "agent.")) for row in _new_events(before, _snapshot(h)))


def test_source_validation_and_read_fence_precede_sensitivity_authorization(label_harness):
    """Mutation: answer a missing or unreadable source with HTTP 400."""
    h = label_harness
    visible_id = str(h.source(sensitivity="private")["id"])
    hidden_id = str(h.source(sensitivity="confidential")["id"])
    missing_id = str(uuid4())
    key = h.key("trusted_local_agent")
    cases = [
        (visible_id, "invalid", 400, "vNext source review action is invalid"),
        (visible_id, "assign_project", 400, "project_id is required"),
        (hidden_id, "update", 404, f"vNext source {hidden_id} was not found"),
        (missing_id, "assign_project", 404, f"vNext source {missing_id} was not found"),
    ]
    for source_id, action, status, detail in cases:
        before = _snapshot(h)
        assert _review(h, source_id, {"action": action, "sensitivity": "public"}, key=key) == (
            status, {"detail": detail},
        )
        _assert_only_key_use_recorded(before, _snapshot(h), key)


@pytest.mark.parametrize("sensitivity", ["confidential", "internal"])
@pytest.mark.parametrize("additional_fields", [{}, {"enabled": True}, {"config_json": {"synthetic": True}}])
def test_a_trusted_key_cannot_change_the_default_for_a_connector(label_harness, sensitivity, additional_fields):
    """Mutation: answer the connector sensitivity refusal with HTTP 400."""
    h = label_harness
    with h.store() as store:
        VNextConnectorService(store).update_config(CONNECTOR, default_sensitivity="private", enabled=False)
    key = h.key("trusted_local_agent")
    h.key("admin_agent")
    before = _snapshot(h)
    status, body = _config(h, {"default_sensitivity": sensitivity, **additional_fields}, key=key)
    _assert_refusal(status, body)
    _assert_only_refusal_recorded(before, _snapshot(h), body, "connector", CONNECTOR, key=key)


@pytest.mark.parametrize("payload", [{"default_sensitivity": "private"}, {}])
@pytest.mark.parametrize("has_setting", [False, True])
def test_a_trusted_key_can_configure_a_connector_at_its_current_default(label_harness, payload, has_setting):
    """Mutation: check sensitivity authorization when the field repeats the effective default."""
    h = label_harness
    if has_setting:
        with h.store() as store:
            VNextConnectorService(store).update_config(CONNECTOR, default_sensitivity="private")
    key = h.key("trusted_local_agent")
    before = _snapshot(h)
    status, body = _config(h, {**payload, "enabled": True}, key=key)
    assert status == 200 and body["enabled"] is True and body["default_sensitivity"] == "private"
    with h.store() as store:
        assert store.get_connector_setting(CONNECTOR)["enabled"] is True
    assert {row["event_type"] for row in _new_events(before, _snapshot(h))} == {"connector.settings_updated"}


def test_connector_validation_precedes_sensitivity_authorization(label_harness):
    """Mutation: answer invalid connector configuration with HTTP 403."""
    h = label_harness
    key = h.key("trusted_local_agent")
    polling = {"default_sensitivity": "confidential", "poll_interval_seconds": 30}
    cases = [
        ("telegram", polling, "Telegram source ingestion is on-demand and does not accept polling or secret configuration"),
        ("missing", {"default_sensitivity": "confidential"}, "vNext connector config request is invalid"),
        ("TELEGRAM", polling, "vNext connector config request is invalid"),
    ]
    for connector, payload, detail in cases:
        before = _snapshot(h)
        assert _config(h, payload, key=key, connector=connector) == (400, {"detail": detail})
        _assert_only_key_use_recorded(before, _snapshot(h), key)


@pytest.mark.parametrize(("key_profile", "claims", "allowed", "has_policy"), [
    (None, {}, True, False),
    (None, {"permission_profile": "trusted_local_agent"}, True, False),
    (None, {"agent_id": "declared-trusted", "permission_profile": "trusted_local_agent"}, False, True),
    (None, {"agent_id": "declared-admin", "permission_profile": "admin_agent"}, True, True),
    ("admin_agent", {}, True, True),
    ("admin_agent", {"permission_profile": "trusted_local_agent"}, False, True),
])
def test_both_label_changes_use_the_resolved_caller(label_harness, key_profile, claims, allowed, has_policy):
    """Mutation: keep the old source sensitivity after authorization."""
    h = label_harness
    source_id = str(h.source(sensitivity="private")["id"])
    key = h.key(key_profile) if key_profile else None
    before = _snapshot(h)
    source_status, source_body = _review(h, source_id, {"action": "review", "sensitivity": "confidential", **claims}, key=key)
    after_source = _snapshot(h)
    connector_status, connector_body = _config(h, {"default_sensitivity": "confidential", **claims}, key=key)
    after = _snapshot(h)
    if allowed:
        assert source_status == connector_status == 200
        assert source_body["source"]["sensitivity"] == connector_body["default_sensitivity"] == "confidential"
    else:
        _assert_refusal(source_status, source_body)
        _assert_refusal(connector_status, connector_body)
        declared = claims.get("agent_id")
        _assert_only_refusal_recorded(before, after_source, source_body, "source", source_id, key=key, declared_agent_id=declared)
        _assert_only_refusal_recorded(
            after_source, after, connector_body, "connector", CONNECTOR, key=key, declared_agent_id=declared,
        )
    added = _new_events(before, after)
    observed = [
        (row["target_type"], row["payload_json"]["policy_decision"]["decision"])
        for row in added if row["event_type"] == "policy.decision"
    ]
    decision = "allowed" if allowed else "blocked"
    assert sorted(observed) == ([("connector", decision), ("source", decision)] if has_policy else [])


@pytest.mark.parametrize("as_admin", [False, True])
def test_owner_and_admin_source_changes_reach_derived_rows(label_harness, as_admin):
    """Mutation: keep the old source sensitivity after authorization."""
    h = label_harness
    key = h.key("admin_agent") if as_admin else None
    for action, current, requested in REVIEW_CASES:
        source = h.source(sensitivity=current, text=f"Synthetic label test {uuid4()}")
        memory = h.memory(source=source)
        payload = _review_payload(h, action, requested)
        source_id = str(source["id"])
        status, body = _review(h, source_id, payload, key=key)
        assert status == 200 and body["source"]["sensitivity"] == requested
        with h.store() as store:
            assert store.get_source(source_id)["sensitivity"] == requested
            if requested == "confidential":
                assert store.get_memory(str(memory["id"]))["sensitivity"] == requested
    for requested in ("confidential", "internal"):
        status, body = _config(h, {"default_sensitivity": requested, "enabled": True}, key=key)
        assert status == 200 and body["default_sensitivity"] == requested


@pytest.mark.parametrize(("key_profile", "claims", "status"), [
    (None, {"agent_id": "declared", "agent_type": "unknown-type"}, 400),
    (None, {"agent_id": "declared", "permission_profile": "unknown-profile"}, 400),
    (None, {"agent_id": 1}, 400),
    (None, {"agent_identity": "invalid"}, 422),
    ("trusted_local_agent", {"permission_profile": "admin_agent"}, 403),
    ("trusted_local_agent", {"agent_id": "other-agent"}, 403),
])
def test_identity_validation_runs_before_the_label_policy(label_harness, key_profile, claims, status):
    """Mutation: answer invalid identity claims with HTTP 422."""
    h = label_harness
    source_id = str(h.source(sensitivity="private")["id"])
    key = h.key(key_profile) if key_profile else None
    before = _snapshot(h)
    response, source = _review(h, source_id, {"action": "review", "sensitivity": "internal", **claims}, key=key)
    assert response == status
    response, connector = _config(h, {"default_sensitivity": "internal", **claims}, key=key)
    assert response == status
    assert "policy_decision" not in source and "policy_decision" not in connector
    after = _snapshot(h)
    assert after["sources"] == before["sources"]
    assert after["connector_settings"] == before["connector_settings"]


@pytest.mark.parametrize("profile", ["admin_agent", "trusted_local_agent"])
def test_project_bound_keys_still_receive_the_operator_refusal(label_harness, profile):
    """Mutation: an operator refusal answers HTTP 400."""
    h = label_harness
    project = str(uuid4())
    source_id = str(h.source(scope=(project,), sensitivity="private")["id"])
    key = h.key(profile, project=project)
    for status, body in (
        _review(h, source_id, {"action": "review", "sensitivity": "internal"}, key=key),
        _config(h, {"default_sensitivity": "internal"}, key=key),
    ):
        assert status == 403
        assert body["policy_decision"]["action"] == "http.operator.access"
        assert body["policy_decision"]["reasons"] == ["unbound_operator_key_required"]


def test_connector_sync_uses_the_admin_selected_default(label_harness):
    """Mutation: record an incorrect status for a connector sensitivity refusal."""
    h = label_harness
    admin = h.key("admin_agent")
    trusted = h.key("trusted_local_agent")
    assert _config(h, {"enabled": True, "default_sensitivity": "confidential"}, key=admin)[0] == 200
    _assert_refusal(*_config(h, {"default_sensitivity": "private"}, key=trusted))
    status, result, _ = h.request("POST", CONNECTOR_PATH + "/sync", key=admin, payload={
        "items": [{"url": "https://example.test/note", "title": "Synthetic note", "text": "A synthetic paragraph."}],
    })
    assert status == 201
    with h.store() as store:
        sources = [row for row in store.list_sources(limit=10) if row["connector_name"] == CONNECTOR]
        assert len(sources) == 1, result
        source_id = str(sources[0]["id"])
        assert sources[0]["sensitivity"] == "confidential"
    status, body, _ = h.request("GET", f"/v0/vnext/sources/{source_id}", key=trusted)
    assert (status, body) == (404, {"detail": f"vNext source {source_id} was not found"})
