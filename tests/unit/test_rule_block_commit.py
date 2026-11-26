"""Owner rule commits through SQLite MCP."""

from collections import Counter
from contextlib import closing, contextmanager
import json
import sqlite3

import pytest

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, memory_commit_request_from_payload
from tests.support.rule_commit_contract import (
    BAD_REQUESTS, DOORS, MARK, OWNER_REQUIRED, RULE, STATES, WAIT_CASES, check_bad_target, check_cannot_wait,
    check_credentials, check_derived_block, check_exact_bounds, check_lesson_surfaces, check_lifecycle_case,
    check_refusal_footprint, check_replace, check_replacement_replay_fences_the_predecessor, check_wrong_standing,
    lifecycle_request, row, seed_lifecycle_subject, stored,
)
from tests.unit.test_identity_claim_codes import wire
from tests.unit.test_lessons_read import Vault


class SQLiteRuleVault:
    def __init__(self, vault, monkeypatch, *, keyed):
        self.vault = vault
        self.monkeypatch = monkeypatch
        self.keys = {}
        if keyed:
            for who, profile, scope in (
                ("admin", "admin_agent", None), ("bound-admin", "admin_agent", "alpha"),
                ("trusted", "trusted_local_agent", None), ("scoped", "project_scoped_agent", "alpha"),
                ("readonly", "read_only_agent", None), ("proposal", "memory_proposal_agent", "alpha"),
            ):
                self.keys[who] = vault.key(who, profile, scope)
        self.context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)

    def claims(self, caller):
        profile = {"trusted": "trusted_local_agent", "scoped": "project_scoped_agent", "admin": "admin_agent"}.get(caller)
        return {"agent_id": caller, "permission_profile": profile}

    def call(self, door, payload, caller="admin"):
        key = self.keys.get(caller)
        if key is not None:
            self.monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        else:
            self.monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        if not self.keys and caller != "owner":
            scope = {"project_scope": ["alpha"]} if caller == "scoped" else {}
            payload = {**payload, **self.claims(caller), **scope}
        tools = {"commit": "alice_memory_commit", "review": "alice_memory_correct",
                 "manage": "alice_memory_manage", "correct": "alice_vnext_correct_memory"}
        bad, body = wire(self.context, tools.get(door, door), payload)
        return (body["error"]["code"] if bad else "ok"), body

    @staticmethod
    def ok(code):
        return code == "ok"

    @contextmanager
    def store(self):
        with sqlite_user_connection(self.vault.path, self.vault.user) as conn:
            yield SQLiteVNextStore(conn, self.vault.user)

    def snapshot(self):
        with closing(sqlite3.connect(self.vault.path)) as conn:
            conn.row_factory = sqlite3.Row
            tables = [name for (name,) in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")]
            return {
                name: Counter(
                    json.dumps(dict(entry), sort_keys=True, default=str)
                    for entry in conn.execute('SELECT * FROM "' + name.replace('"', '""') + '"')
                )
                for name in tables
            }

    def memories(self):
        tables = self.snapshot()
        return {name: tables[name] for name in ("memories", "memory_revisions", "provenance_links")}


@pytest.fixture
def rule_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    return SQLiteRuleVault(Vault(tmp_path / "vault.db"), monkeypatch, keyed=True)


@pytest.fixture
def keyless_rule_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    return SQLiteRuleVault(Vault(tmp_path / "keyless.db"), monkeypatch, keyed=False)


@pytest.mark.parametrize("caller", ["admin", "bound-admin"])
def test_sqlite_rule_is_a_derived_procedure(rule_vault, caller):
    """Guard edit: use a supplied title instead of deriving it from the rule."""
    check_derived_block(rule_vault, caller)


@pytest.mark.parametrize("caller", ["trusted", "scoped"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("would_wait", [False, True])
def test_sqlite_rule_refuses_keys_without_publishing_standing(rule_vault, caller, nested, would_wait):
    """Guard edit: change the rule standing reason code."""
    check_wrong_standing(rule_vault, caller, nested=nested, would_wait=would_wait)


@pytest.mark.parametrize(("fields", "reason"), WAIT_CASES[:-2])
def test_sqlite_rule_refuses_any_waiting_outcome(rule_vault, fields, reason):
    """Guard edit: change the rule waiting reason code."""
    check_cannot_wait(rule_vault, "admin", fields, reason)


@pytest.mark.parametrize("payload", BAD_REQUESTS)
def test_sqlite_rule_shape_is_bounded(rule_vault, payload):
    """Guard edit: run the shape cases with a valid input."""
    code, result = rule_vault.call("commit", payload, "admin")
    assert code == "invalid_request", result


def test_sqlite_rule_accepts_normalized_text_at_the_bounds(rule_vault):
    """Guard edit: lower the instruction limit by one character."""
    check_exact_bounds(rule_vault)


@pytest.mark.parametrize("field", ["rule", "reason", "applies_when", "paths"])
@pytest.mark.parametrize("marker", [
    "PASSWORD_DB=hunter2",
    "TOKEN_GITHUB=ghp_0123456789abcdefghijklmnopqrstuvwxyz0",
    "API_KEY_RAW=AKIAIOSFODNN7EXAMPLE",
    "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
])
def test_sqlite_rule_checks_credentials_in_each_field(rule_vault, field, marker):
    """Guard edit: assert the credential reply for an ordinary synthetic marker."""
    check_credentials(rule_vault, field, marker)


@pytest.mark.parametrize("caller", ["admin", "bound-admin"])
def test_sqlite_rule_replacement_is_atomic_and_replay_is_read_only(rule_vault, caller):
    """Guard edit: return an incorrect predecessor pointer in the receipt."""
    check_replace(rule_vault, caller)


def test_sqlite_rule_replay_omits_an_out_of_scope_predecessor(rule_vault):
    """Guard edit: bind the replaying admin to both readable projects."""
    check_replacement_replay_fences_the_predecessor(rule_vault)


@pytest.mark.parametrize("target", ["plain", "lesson", "rejected", "superseded", "expired", "private_only", "archived", "stale"])
@pytest.mark.parametrize("caller", ["admin", "bound-admin", "trusted", "scoped"])
def test_sqlite_bad_rule_replacements_use_the_missing_reply(rule_vault, target, caller):
    """Guard edit: compare a valid replacement with the missing reply."""
    check_bad_target(rule_vault, target, caller)


def test_sqlite_fresh_keyless_rules_record_the_shared_publishing_mark(keyless_rule_vault):
    """Guard edit: stop writing the stored publishing mark."""
    vault = keyless_rule_vault
    assert MARK not in stored(vault, caller="owner")["memory"]["metadata_json"]["agentic_memory"]
    for claims in ({"agent_id": "named"}, vault.claims("trusted"), vault.claims("scoped")):
        result = stored(vault, caller="owner", **claims)
        assert result["memory"]["metadata_json"]["agentic_memory"][MARK] is True
    direct = memory_commit_request_from_payload({"rule": RULE}, user_id=vault.vault.user)
    with vault.store() as store:
        result = VNextMemoryCommitService(store).commit(
            identity=AgentIdentity(agent_id="uncounted", permission_profile="trusted_local_agent"), request=direct,
        )
    assert result["status"] == "rejected" and result["reason"] == OWNER_REQUIRED
    assert "memory" not in result


def test_sqlite_rule_wrong_target_matches_a_missing_target_for_binding(rule_vault):
    """Guard edit: give both target fixtures the same binding."""
    check_bad_target(rule_vault, "active", "bound-admin")


def test_sqlite_rule_other_write_doors_reject_rule_blocks(keyless_rule_vault):
    """Guard edit: pass a plain correction body instead of a rule block."""
    v = keyless_rule_vault
    for tool in ("alice_vnext_commit_memory", "alice_vnext_propose_memory"):
        code, result = v.call(tool, {"title": "Fixture", "canonical_text": "Fixture content.", "rule": RULE}, "owner")
        assert code == "invalid_request", result
    code, plain = v.call("commit", {"title": "Fixture", "canonical_text": "Fixture content."}, "owner")
    assert v.ok(code) and plain["status"] == "committed"
    mid = plain["memory"]["id"]
    code, result = v.call("review", {"action": "edit-and-approve", "review_item_id": mid, "body": {"rule": RULE}}, "owner")
    assert code == "invalid_request", result
    code, result = v.call("review", {"action": "supersede-existing", "review_item_id": mid, "replacement_body": {"rule": RULE}}, "owner")
    assert code == "invalid_request", result


def test_sqlite_rule_read_surfaces_keep_lessons_and_rules_separate(rule_vault):
    """Guard edit: put a lesson block on the row intended to hold a rule."""
    check_lesson_surfaces(rule_vault)


def test_sqlite_rule_refusal_keeps_the_read_only_write_footprint(rule_vault):
    """Guard edit: use a read-only profile for the trusted refusal."""
    check_refusal_footprint(rule_vault)


@pytest.mark.parametrize("door", [door for door in DOORS if door not in {"private", "correct"}])
@pytest.mark.parametrize("state", STATES)
def test_sqlite_rule_lifecycle_matches_lesson(rule_vault, door, state):
    """Guard edit: replace the test rule with an ordinary procedure at the lifecycle door."""
    check_lifecycle_case(rule_vault, door, state)


@pytest.mark.parametrize("door", [door for door in DOORS if door not in {"private", "correct"}])
def test_sqlite_keyless_rules_use_the_lesson_lifecycle(keyless_rule_vault, door):
    """Guard edit: replace the test rule with an ordinary procedure at the lifecycle door."""
    for state in STATES:
        check_lifecycle_case(keyless_rule_vault, door, state, keyed=False)


def test_sqlite_trusted_author_cannot_confirm_its_own_seeded_rule(rule_vault):
    """Guard edit: give the author an admin key."""
    mid, confirmation_id = seed_lifecycle_subject(rule_vault, "rule", "pending", created_by="trusted")
    door, args = lifecycle_request("confirm", http=False, memory_id=mid, confirmation_id=confirmation_id)
    code, answer = rule_vault.call(door, args, "trusted")
    assert code == "not_permitted", answer
    assert row(rule_vault, mid)["status"] == "needs_review"


def test_sqlite_sweep_route_reports_its_existing_backend_requirement(keyless_rule_vault):
    """Guard edit: use the MCP expire action in place of the scheduler tool."""
    code, result = keyless_rule_vault.call(
        "alice_vnext_scheduler_run_now", {"workflow_type": "staleness_sweep"}, "owner",
    )
    assert code == "precondition_failed", result
