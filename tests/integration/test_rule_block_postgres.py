"""Owner rule commits through PostgreSQL HTTP and MCP."""

from collections import Counter
import json

import psycopg
from psycopg import sql
import pytest

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, memory_commit_request_from_payload
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.support.rule_commit_contract import (
    BAD_REQUESTS, DOORS, MARK, OWNER_REQUIRED, PAST, RULE, STATES, WAIT_CASES, check_bad_target, check_cannot_wait,
    check_credentials, check_derived_block, check_exact_bounds, check_lesson_surfaces, check_lifecycle_case,
    check_refusal_footprint, check_replace, check_replacement_replay_fences_the_predecessor, check_wrong_standing,
    lifecycle_request, row, seed_lifecycle_subject, stored,
)
from tests.unit.test_identity_claim_codes import wire


class PostgresRuleVault:
    def __init__(self, harness, monkeypatch, transport, *, keyed):
        self.harness = harness
        self.monkeypatch = monkeypatch
        self.transport = transport
        self.context = MCPRuntimeContext(database_url=harness.urls["app"], user_id=harness.user_id)
        self.keys = {}
        self.profiles = {
            "admin": ("admin_agent", None), "bound-admin": ("admin_agent", "alpha"),
            "trusted": ("trusted_local_agent", None), "scoped": ("project_scoped_agent", "alpha"),
            "readonly": ("read_only_agent", None), "proposal": ("memory_proposal_agent", "alpha"),
        }
        if keyed:
            with self.store() as store:
                for name, (profile, project) in self.profiles.items():
                    _, self.keys[name] = create_agent_key(
                        store, user_id=harness.user_id, agent_id=name,
                        permission_profile=profile, project_scope=project,
                    )

    def claims(self, caller):
        return {"agent_id": caller, "permission_profile": self.profiles[caller][0]}

    def call(self, door, payload, caller="admin"):
        key = self.keys.get(caller)
        if not self.keys and caller != "owner":
            scope = {"project_scope": ["alpha"]} if caller == "scoped" else {}
            payload = {**payload, **self.claims(caller), **scope}
        if self.transport == "http":
            arguments = dict(payload)
            if door == "review":
                mid = arguments.pop("memory_id")
                path = f"/v0/vnext/memories/{mid}/review"
            elif door == "manage":
                action = arguments.pop("action")
                path = f"/v0/vnext/memories/{action.replace('_', '-')}"
            elif door == "sweep":
                path = "/v0/vnext/scheduler/workflows/staleness_sweep/run-now"
            elif door == "proposal":
                path = "/v0/vnext/memory-proposals"
            else:
                path = f"/v0/vnext/memories/{door}"
            result, body, _ = self.harness.request("POST", path, payload=arguments, key=key)
            return result, body

        if key is not None:
            self.monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        else:
            self.monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        tools = {"commit": "alice_memory_commit", "review": "alice_memory_correct",
                 "manage": "alice_memory_manage", "correct": "alice_vnext_correct_memory",
                 "sweep": "alice_vnext_scheduler_run_now"}
        if door == "sweep":
            payload = {**payload, "workflow_type": "staleness_sweep"}
        failed, response = wire(self.context, tools.get(door, door), payload)
        return (response["error"]["code"] if failed else "ok"), response

    @staticmethod
    def ok(code):
        return code in (200, 201, "ok")

    def store(self):
        return self.harness.store()

    def snapshot(self):
        with psycopg.connect(self.harness.urls["admin"]) as conn:
            names = [name for (name,) in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
            )]
            return {
                name: Counter(json.dumps(data, sort_keys=True) for (data,) in conn.execute(
                    sql.SQL("SELECT row_to_json(t) FROM {} t").format(sql.Identifier(name))
                ))
                for name in names
            }

    def memories(self):
        tables = self.snapshot()
        return {name: tables[name] for name in ("memories", "memory_revisions", "provenance_links")}


@pytest.fixture(params=["http", "mcp"])
def rule_vault(label_harness, monkeypatch, request):
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    return PostgresRuleVault(label_harness, monkeypatch, request.param, keyed=True)


@pytest.mark.parametrize("caller", ["admin", "bound-admin"])
def test_postgres_rule_is_a_derived_procedure(rule_vault, caller):
    """Guard edit: use a supplied title instead of deriving it from the rule."""
    check_derived_block(rule_vault, caller)


@pytest.mark.parametrize("caller", ["trusted", "scoped"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("would_wait", [False, True])
def test_postgres_rule_refuses_keys_without_publishing_standing(rule_vault, caller, nested, would_wait):
    """Guard edit: change the rule standing reason code."""
    check_wrong_standing(rule_vault, caller, nested=nested, would_wait=would_wait)


@pytest.mark.parametrize(("fields", "reason"), WAIT_CASES)
def test_postgres_rule_refuses_any_waiting_outcome(rule_vault, fields, reason):
    """Guard edit: change the rule waiting reason code."""
    if rule_vault.transport == "mcp" and ("intent" in fields or "contradiction_refs" in fields):
        pytest.skip("The core MCP commit does not advertise these HTTP fields.")
    check_cannot_wait(rule_vault, "admin", fields, reason)


@pytest.mark.parametrize("payload", BAD_REQUESTS)
def test_postgres_rule_shape_is_bounded(rule_vault, payload):
    """Guard edit: run the shape cases with a valid input."""
    code, result = rule_vault.call("commit", payload, "admin")
    assert code in {400, 422, "invalid_request"}, result


def test_postgres_rule_accepts_normalized_text_at_the_bounds(rule_vault):
    """Guard edit: lower the instruction limit by one character."""
    check_exact_bounds(rule_vault)


@pytest.mark.parametrize("field", ["rule", "reason", "applies_when", "paths"])
@pytest.mark.parametrize("marker", [
    "PASSWORD_DB=hunter2",
    "TOKEN_GITHUB=ghp_0123456789abcdefghijklmnopqrstuvwxyz0",
    "API_KEY_RAW=AKIAIOSFODNN7EXAMPLE",
    "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
])
def test_postgres_rule_checks_credentials_in_each_field(rule_vault, field, marker):
    """Guard edit: assert the credential reply for an ordinary synthetic marker."""
    check_credentials(rule_vault, field, marker)


@pytest.mark.parametrize("caller", ["admin", "bound-admin"])
def test_postgres_rule_replacement_is_atomic_and_replay_is_read_only(rule_vault, caller):
    """Guard edit: return an incorrect predecessor pointer in the receipt."""
    check_replace(rule_vault, caller)


def test_postgres_rule_replay_omits_an_out_of_scope_predecessor(rule_vault):
    """Guard edit: bind the replaying admin to both readable projects."""
    check_replacement_replay_fences_the_predecessor(rule_vault)


@pytest.mark.parametrize("target", ["plain", "lesson", "rejected", "superseded", "expired", "private_only", "archived", "stale"])
@pytest.mark.parametrize("caller", ["admin", "bound-admin", "trusted", "scoped"])
def test_postgres_bad_rule_replacements_use_the_missing_reply(rule_vault, target, caller):
    """Guard edit: compare a valid replacement with the missing reply."""
    check_bad_target(rule_vault, target, caller)


def test_postgres_rule_wrong_target_matches_a_missing_target_for_binding(rule_vault):
    """Guard edit: give both target fixtures the same binding."""
    check_bad_target(rule_vault, "active", "bound-admin")


def test_postgres_rule_refusal_keeps_the_read_only_write_footprint(rule_vault):
    """Guard edit: use a read-only profile for the trusted refusal."""
    check_refusal_footprint(rule_vault)


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_postgres_keyless_rule_uses_the_shared_publishing_mark(label_harness, monkeypatch, transport):
    """Guard edit: stop writing the stored publishing mark."""
    vault = PostgresRuleVault(label_harness, monkeypatch, transport, keyed=False)
    assert MARK not in stored(vault, caller="owner")["memory"]["metadata_json"]["agentic_memory"]
    for claims in ({"agent_id": "named"}, vault.claims("trusted"), vault.claims("scoped")):
        published = stored(vault, caller="owner", **claims)
        assert published["memory"]["metadata_json"]["agentic_memory"][MARK] is True
    request = memory_commit_request_from_payload({"rule": RULE}, user_id=label_harness.user_id)
    with vault.store() as store:
        result = VNextMemoryCommitService(store).commit(
            identity=AgentIdentity(agent_id="fixture-uncounted", permission_profile="trusted_local_agent"),
            request=request,
        )
    assert result["reason"] == OWNER_REQUIRED and "memory" not in result


def test_postgres_other_http_write_doors_reject_rule_blocks(label_harness, monkeypatch):
    """Guard edit: send ordinary commit and proposal bodies."""
    vault = PostgresRuleVault(label_harness, monkeypatch, "http", keyed=False)
    for door in ("commit", "proposal"):
        code, result = vault.call(door, {
            "title": "Fixture", "canonical_text": "Fixture content.", "value": {"rule": RULE},
        }, "owner")
        assert code == 422, result


def test_postgres_rule_read_surfaces_keep_lessons_and_rules_separate(label_harness, monkeypatch):
    """Guard edit: put a lesson block on the row intended to hold a rule."""
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    check_lesson_surfaces(PostgresRuleVault(label_harness, monkeypatch, "mcp", keyed=True))


@pytest.mark.parametrize("door", [door for door in DOORS if door != "legacy-correct"])
@pytest.mark.parametrize("state", STATES)
def test_postgres_rule_lifecycle_matches_lesson(rule_vault, door, state):
    """Guard edit: replace the test rule with an ordinary procedure at the lifecycle door."""
    if (door == "supersede" and rule_vault.transport == "http"
        or door in {"private", "correct"} and rule_vault.transport == "mcp"):
        pytest.skip("This door is available on the other transport.")
    check_lifecycle_case(rule_vault, door, state)


@pytest.mark.parametrize("door", [door for door in DOORS if door not in {"legacy-correct", "supersede"}])
def test_postgres_keyless_rules_use_the_lesson_lifecycle(label_harness, monkeypatch, door):
    """Guard edit: replace the test rule with an ordinary procedure at the lifecycle door."""
    vault = PostgresRuleVault(label_harness, monkeypatch, "http", keyed=False)
    for state in STATES:
        check_lifecycle_case(vault, door, state, keyed=False)


def test_postgres_trusted_author_cannot_confirm_its_own_seeded_rule(rule_vault):
    """Guard edit: give the author an admin key."""
    mid, confirmation_id = seed_lifecycle_subject(rule_vault, "rule", "pending", created_by="trusted")
    door, args = lifecycle_request(
        "confirm", http=rule_vault.transport == "http", memory_id=mid, confirmation_id=confirmation_id,
    )
    code, answer = rule_vault.call(door, args, "trusted")
    if rule_vault.transport == "http":
        assert code == 403 and "rule_requires_owner_or_admin" in answer["policy_decision"]["reasons"], answer
    else:
        assert code == "not_permitted", answer
    assert row(rule_vault, mid)["status"] == "needs_review"


@pytest.mark.parametrize("caller", ["admin", "trusted", "scoped", "readonly", "proposal"])
def test_postgres_rule_staleness_sweep_keeps_the_existing_transition(label_harness, monkeypatch, caller):
    """Guard edit: leave the expired test row's validity window open."""
    vault = PostgresRuleVault(label_harness, monkeypatch, "http", keyed=True)
    expired, _ = seed_lifecycle_subject(vault, "rule", "expired")
    active, _ = seed_lifecycle_subject(vault, "rule", "active")
    before = row(vault, expired)
    active_before = row(vault, active)
    code, result = vault.call("sweep", {"options": {"reference_time": "2025-01-01T00:00:00Z"}}, caller)
    assert code == (201 if caller in {"admin", "trusted"} else 403), result
    after = row(vault, expired)
    assert after["status"] == ("stale" if code == 201 else "active")
    assert before["valid_to"] == after["valid_to"]
    assert all(before[field] == after[field] for field in ("title", "canonical_text", "value"))
    assert row(vault, active) == active_before
    if code == 201:
        replay, result = vault.call("sweep", {"options": {"reference_time": PAST}}, caller)
        assert replay == 201, result
        assert row(vault, expired) == after
