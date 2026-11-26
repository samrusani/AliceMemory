"""Shared rule write assertions for the SQLite and PostgreSQL front doors."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
from uuid import uuid4

import pytest

from alicebot_api import vnext_memory_commit
from alicebot_api.mcp.review import _vnext_memory_correct
from alicebot_api.vnext_agent_keys import create_agent_key, hash_agent_key, resolve_agent_identity
from alicebot_api.vnext_label_writes import acquire_exclusive_label_lock
from alicebot_api.vnext_source_fence import SavedProvenanceReader, SourceReadFence
from tests.support.keyless_team_contract import LESSON

RULE = {
    "rule": "Read the check results before release.",
    "reason": "A skipped check can leave a problem unseen.",
    "applies_when": "Reviewing a release",
    "paths": ["docs/*.md"],
}
OWNER_REQUIRED = "rule_requires_owner_or_admin"
CANNOT_WAIT = "rule_cannot_wait"
MARK = "keyless_lesson_publishing_standing"
TEXT = "Rule: Read the check results before release. Reason: A skipped check can leave a problem unseen. Applies when: Reviewing a release"
REQUEST = {"sensitivity": "internal", "project_scope": ["alpha"]}
WAIT_CASES = (
    ({"sensitivity": "confidential"}, "sensitive_memory_requires_confirmation"),
    ({"sensitivity": "highly_sensitive"}, "sensitive_memory_requires_confirmation"),
    ({"domain": "health"}, "sensitive_memory_requires_confirmation"),
    ({"domain": "financial"}, "sensitive_memory_requires_confirmation"),
    ({"domain": "legal"}, "sensitive_memory_requires_confirmation"),
    ({"source_refs": ["fixture"] * 9}, "bulk_source_refs_require_review"),
    ({"intent": "suggested_write"}, "explicit_memory_intent_required"),
    ({"contradiction_refs": ["A fixture disagrees."]}, "contradiction_requires_confirmation"),
)
BAD_REQUESTS = (
    {"rule": {**RULE, "rule": "a" * 401}},
    {"rule": {**RULE, "reason": "b" * 601}},
    {"rule": {**RULE, "applies_when": "c" * 301}},
    {"rule": {**RULE, "paths": ["docs/*.md"] * 17}},
    {"rule": {**RULE, "paths": ["d" * 201]}},
    {"rule": {**RULE, "paths": ["../docs/*.md"]}},
    {"rule": {**RULE, "paths": ["/docs/*.md"]}},
    {"rule": {**RULE, "paths": ["~/docs/*.md"]}},
    {"rule": {**RULE, "paths": ["d:\\docs\\file.md"]}},
    {"rule": {**RULE, "paths": ["docs\\file.md"]}},
    {"rule": {**RULE, "rule": "   "}},
    {"rule": {"rule": RULE["rule"]}},
    {"rule": {"reason": RULE["reason"]}},
    {"rule": {**RULE, "reason": "Text \x00 with a control"}},
    {"rule": {**RULE, "replaces": "not a memory id"}},
    {"rule": {**RULE, "extra": "unsupported"}},
    {"rule": RULE, "lesson": LESSON},
    {"rule": RULE, "confidence": 0.9},
    {"rule": RULE, "source_type": "direct_user_instruction"},
    {"rule": RULE, "memory_type": "fact"},
    {"rule": RULE, "canonical_text": "Free text is invalid with a rule."},
)

KEY_USE_CASES = (
    ("new", "admin", "lesson"),
    ("new", "admin", "rule"),
    ("replay", "admin", "lesson"),
    ("replay", "admin", "rule"),
    ("missing-source", "admin", "lesson"),
    ("missing-source", "admin", "rule"),
    ("missing-source", "trusted", "lesson"),
    ("missing-source", "trusted", "rule"),
    ("invalid-block", "admin", "lesson"),
    ("invalid-block", "admin", "rule"),
    ("missing-replacement", "admin", "rule"),
    ("different-block", "admin", "lesson"),
    ("different-block", "admin", "rule"),
    ("standing", "readonly", "lesson"),
    ("standing", "trusted", "rule"),
)

def row(vault, memory_id):
    with vault.store() as store:
        return store.get_memory_for_redaction(str(memory_id))


def seed_state(vault, memory_id, **patch):
    with vault.store() as store:
        store.lock_graph_mutation()
        acquire_exclusive_label_lock(store)
        return store.update_memory(memory_id=str(memory_id), patch=patch, actor_type="user")


def stored(vault, *, caller="admin", block=RULE, **extra):
    code, receipt = vault.call("commit", {**REQUEST, "rule": block, **extra}, caller)
    assert vault.ok(code) and receipt["status"] == "committed", (code, receipt)
    return receipt


def check_derived_block(vault, caller):
    result = stored(vault, caller=caller, title="A supplied title")
    memory = result["memory"]
    assert memory["status"] == "active"
    assert memory["title"] == RULE["rule"]
    assert memory["canonical_text"] == memory["value"]["text"] == TEXT
    assert memory["value"]["rule"] == result["rule"] == RULE
    assert memory["memory_type"] == "procedure"
    assert memory["domain"] == "project"


def check_exact_bounds(vault):
    block = {
        "rule": "  " + "a" * 400 + "  ",
        "reason": "b" * 600,
        "applies_when": "c" * 300,
        "paths": ["docs/" + f"{i:02}" + "d" * 193 for i in range(16)],
    }
    result = stored(vault, block=block)
    assert result["rule"] == {**block, "rule": block["rule"].strip()}


def key_last_used(vault, caller):
    with vault.store() as store:
        record = store.get_agent_api_key_by_hash(hash_agent_key(vault.keys[caller]))
    assert record is not None
    stamp = record["last_used_at"]
    return datetime.fromisoformat(str(stamp)) if stamp is not None else None


def snapshot_without_key_use(vault, caller):
    tables = vault.snapshot()
    keys = Counter()
    key_hash = hash_agent_key(vault.keys[caller])
    for entry, count in tables["agent_api_keys"].items():
        record = json.loads(entry)
        if record["key_hash"] == key_hash:
            del record["last_used_at"]
        keys[json.dumps(record, sort_keys=True)] += count
    return {**tables, "agent_api_keys": keys}


def check_commit_key_use(vault, case, caller, kind):
    block = RULE if kind == "rule" else LESSON
    args = {**REQUEST, kind: block}
    initial = None
    if case in {"replay", "different-block"}:
        args["idempotency_key"] = f"fixture-{kind}-{case}"
        code, initial = vault.call("commit", args, caller)
        assert vault.ok(code) and initial["status"] == "committed", (code, initial)
    if case == "missing-source":
        args["source_refs"] = [f"source:{uuid4()}"]
    elif case == "invalid-block":
        # An empty required text field passes the MCP schema's string type
        # check and reaches block validation after authentication.
        field = "rule" if kind == "rule" else "what_happened"
        args[kind] = {**block, field: ""}
    elif case == "missing-replacement":
        args["rule"] = {**RULE, "replaces": str(uuid4())}
    elif case == "different-block":
        field = "rule" if kind == "rule" else "do_instead"
        args[kind] = {**block, field: "Read the fixture again."}

    before = key_last_used(vault, caller)
    tables = snapshot_without_key_use(vault, caller) if case == "replay" and kind == "rule" else None
    code, answer = vault.call("commit", args, caller)
    after = key_last_used(vault, caller)
    moves = after is not None and (before is None or after > before)
    transport = getattr(vault, "transport", "sqlite-mcp")
    print("KEY USE", transport, case, caller, kind, code, answer.get("status"), moves)

    if case in {"new", "replay"}:
        assert vault.ok(code) and answer["status"] == "committed", (code, answer)
        if case == "replay":
            assert answer.get("idempotent_replay") is True
            if tables is not None:
                assert snapshot_without_key_use(vault, caller) == tables
    elif case == "standing":
        expected = OWNER_REQUIRED if kind == "rule" else "agent_policy_blocked"
        assert vault.ok(code) and answer["reason"] == expected, (code, answer)
    else:
        expected = {
            "missing-source": (404, "not_found"),
            "invalid-block": (422, "invalid_request"),
            "missing-replacement": (404, "not_found"),
            "different-block": (400, "tool_request_failed"),
        }[case]
        assert code == expected[transport != "http"], (code, answer)
    assert moves, (transport, case, caller, kind, before, after)


def check_cannot_wait(vault, caller, fields, original_reason):
    before = vault.memories()
    code, answer = vault.call("commit", {"rule": RULE, **fields}, caller)
    assert vault.ok(code) and answer["status"] == "rejected", (code, answer)
    assert answer["reason"] == CANNOT_WAIT
    assert original_reason in answer["reasons"]
    assert vault.memories() == before


def check_replace(vault, caller):
    first = stored(vault, caller=caller)
    old = first["memory"]["id"]
    args = {
        **REQUEST, "rule": {**RULE, "rule": "Read the release checklist twice.", "replaces": old},
        "idempotency_key": "fixture-replacement",
    }
    code, second = vault.call("commit", args, caller)
    assert vault.ok(code) and second["status"] == "committed", (code, second)
    new = second["memory"]["id"]
    assert str(second["memory"]["supersedes"]) == str(old)
    assert str(row(vault, new)["supersedes"]) == str(old)
    retired = row(vault, old)
    assert retired["status"] == "superseded"
    assert str(retired["superseded_by"]) == str(new)
    assert retired["canonical_text"] == TEXT
    before = snapshot_without_key_use(vault, caller)
    key_used_at = key_last_used(vault, caller)
    code, replay = vault.call("commit", args, caller)
    assert vault.ok(code) and replay == {**second, "idempotent_replay": True}, (code, replay)
    assert snapshot_without_key_use(vault, caller) == before
    assert key_last_used(vault, caller) > key_used_at


def check_replacement_replay_fences_the_predecessor(vault):
    old = stored(vault, project_scope=["beta"])["memory"]["id"]
    args = {**REQUEST, "rule": {**RULE, "replaces": old}, "idempotency_key": "fixture-scoped-replay"}
    code, initial = vault.call("commit", args, "admin")
    assert vault.ok(code) and initial["rule"]["replaces"] == old, initial
    before = snapshot_without_key_use(vault, "bound-admin")
    exact = vault.call("commit", args, "bound-admin")
    missing = vault.call("commit", {**args, "rule": {**RULE, "replaces": str(uuid4())}}, "bound-admin")
    assert exact == missing and not vault.ok(exact[0]), (exact, missing)
    assert snapshot_without_key_use(vault, "bound-admin") == before


def check_replay_fences_the_committed_rule(vault):
    with vault.store() as store:
        user_id = vault.context.user_id
        _, vault.keys["beta-admin"] = create_agent_key(
            store, user_id=user_id, agent_id="beta-admin", permission_profile="admin_agent", project_scope="beta",
        )
    args = {"rule": RULE, "idempotency_key": "fixture-beta-rule"}
    code, initial = vault.call("commit", args, "beta-admin")
    assert vault.ok(code) and initial["status"] == "committed"
    assert initial["memory"]["project_id"] == "beta"
    code, replay = vault.call("commit", args, "beta-admin")
    assert vault.ok(code) and replay == {**initial, "idempotent_replay": True}

    before = snapshot_without_key_use(vault, "bound-admin")
    code, refusal = vault.call("commit", args, "bound-admin")
    expected = 400 if getattr(vault, "transport", None) == "http" else "tool_request_failed"
    assert code == expected and "memory" not in refusal, (code, refusal)
    assert snapshot_without_key_use(vault, "bound-admin") == before


def check_rule_reference_in_saved_provenance(vault):
    prior = stored(vault, project_scope=["beta"])["memory"]["id"]
    head = stored(vault, block={**RULE, "replaces": prior})["memory"]["id"]
    with vault.store() as store:
        identity = resolve_agent_identity(
            store, user_id=vault.context.user_id, raw_key=vault.keys["bound-admin"], payload={},
        )
        current = store.get_memory(head)
        assert current["value"]["rule"]["replaces"] == prior
        assert str(current["supersedes"]) == prior
        reader = SavedProvenanceReader(store, fence=SourceReadFence.for_identity(identity))
        shown = reader.memory(current)
        assert shown["value"]["rule"] == RULE
        assert "supersedes" not in shown


def check_rule_native_review_count(vault):
    hidden, _ = seed_lifecycle_subject(vault, "rule", "rejected")
    visible, _ = seed_lifecycle_subject(vault, "plain", "rejected")
    with vault.store() as store:
        assert hidden != visible
        assert store.count_original_label_statuses("memory") == {"rejected": 2}
        assert store.count_original_label_statuses("memory", lesson_reader="trusted") == {"rejected": 1}


def check_correction_rejects_rule_body(vault, action, field):
    args = {"title": "A fixture fact", "canonical_text": "An ordinary fixture memory.", **REQUEST}
    code, created = vault.call("commit", args, "admin")
    assert vault.ok(code) and created["status"] == "committed"
    memory_id = created["memory"]["id"]
    correction = {"action": action, "review_item_id": memory_id}
    body = {"text": "A valid revision of the fixture memory."}
    rule_correction = {**correction, field: {**body, "rule": RULE}}
    before = vault.memories()
    code, result = vault.call("review", rule_correction, "admin")
    assert code == "invalid_request", (code, result)
    # The advertised body schema rejects the public request first. The
    # handler must still refuse the block when given an already parsed body.
    with pytest.raises(vnext_memory_commit.VNextMemoryCommitValidationError, match="a rule can only be written"):
        _vnext_memory_correct(vault.context, rule_correction)
    assert vault.memories() == before
    code, result = vault.call("review", {**correction, field: body}, "admin")
    assert vault.ok(code), (code, result)


def check_rule_path_credential_floor(vault, monkeypatch):
    marker = "AK" + "IAIOSFODNN7EXAMPLE"
    args = {"rule": {**RULE, "paths": [marker]}}
    request = vnext_memory_commit.memory_commit_request_from_payload(args, user_id=vault.context.user_id)
    assert vnext_memory_commit._request_contains_secret_marker(request) is not None
    # Isolate the later floor from the earlier independent commit scan.
    monkeypatch.setattr(vnext_memory_commit, "_request_contains_secret_marker", lambda request: None)
    check_credentials(vault, "paths", marker)


def check_bad_target(vault, target_state, caller):
    if target_state in {"plain", "lesson"}:
        fields = {"lesson": LESSON} if target_state == "lesson" else {"title": "Fixture", "canonical_text": "An ordinary fact."}
        code, target = vault.call("commit", {**REQUEST, **fields}, "admin")
        assert vault.ok(code) and target["status"] == "committed", (code, target)
        target_id = target["memory"]["id"]
    else:
        # A different project is invisible to a key bound to alpha.
        scope = ["beta"] if caller in {"bound-admin", "scoped"} else ["alpha"]
        target = stored(vault, project_scope=scope)
        target_id = target["memory"]["id"]
        if target_state == "expired":
            seed_state(vault, target_id, valid_to="2020-01-01T00:00:00Z")
        else:
            seed_state(vault, target_id, status=target_state)
    before = vault.memories()
    missing = str(uuid4())
    args = REQUEST
    result = vault.call("commit", {**args, "rule": {**RULE, "replaces": target_id}}, caller)
    absent = vault.call("commit", {**args, "rule": {**RULE, "replaces": missing}}, caller)
    for _, response in (result, absent):
        if "policy_decision" in response:
            # Two separate requests carry different server-generated trace IDs.
            decision = response["policy_decision"]
            assert decision["trace_id"] == decision["policy_decision"]["trace_id"]
            del decision["trace_id"]
            del decision["policy_decision"]["trace_id"]
    assert result == absent, (target_state, caller, result, absent)
    assert vault.memories() == before


def check_refusal_footprint(vault):
    before = vault.snapshot()
    _, baseline = vault.call("commit", {"title": "Fixture", "canonical_text": "Fixture content."}, "readonly")
    after_baseline = vault.snapshot()
    _, refusal = vault.call("commit", {"rule": RULE}, "trusted")
    after_refusal = vault.snapshot()
    assert baseline["reason"] == "agent_policy_blocked"
    assert refusal["reason"] == OWNER_REQUIRED
    assert before.keys() == after_baseline.keys() == after_refusal.keys()

    def changed(initial, final):
        changes = {}
        for table in initial:
            prior = [json.loads(entry) for entry in (initial[table] - final[table]).elements()]
            next_rows = [json.loads(entry) for entry in (final[table] - initial[table]).elements()]
            if not prior and not next_rows:
                continue
            if len(prior) == len(next_rows) == 1 and prior[0].get("id") == next_rows[0].get("id"):
                changes[table] = {"updated": sorted(key for key in next_rows[0] if next_rows[0][key] != prior[0].get(key))}
            else:
                changes[table] = {
                    "removed": len(prior),
                    "inserted": len(next_rows),
                    "columns": sorted({key for entry in next_rows for key, value in entry.items() if value is not None}),
                    "events": sorted(entry["event_type"] for entry in next_rows if "event_type" in entry),
                }
        return changes

    baseline_changes = changed(before, after_baseline)
    rule_changes = changed(after_baseline, after_refusal)
    assert baseline_changes == rule_changes
    assert baseline_changes["agent_api_keys"] == {"updated": ["last_used_at"]}
    assert set(baseline_changes) == {"agent_api_keys", "agent_identities", "event_log"}
    print("REFUSAL", json.dumps(rule_changes, sort_keys=True))


def check_wrong_standing(vault, caller, *, nested, would_wait=False):
    claims = vault.claims(caller)
    identity = {"agent_identity": claims} if nested else claims
    before = vault.memories()
    wait_fields = {"source_refs": ["fixture"] * 9} if would_wait else {}
    code, answer = vault.call("commit", {**REQUEST, "rule": RULE, **identity, **wait_fields}, caller)
    assert vault.ok(code) and answer["status"] == "rejected", (code, answer)
    assert answer["reason"] == OWNER_REQUIRED
    assert answer["receipt"] == "Only the owner or an admin key can write a rule."
    assert "rule_cannot_wait" not in answer["reasons"]
    assert vault.memories() == before


def check_credentials(vault, field, marker):
    rule = {**RULE, field: [marker] if field == "paths" else marker}
    before = vault.memories()
    code, result = vault.call("commit", {"rule": rule}, "admin")
    assert vault.ok(code) and result["status"] == "rejected", (code, result)
    assert result["reason"] == "unsafe_secret_storage"
    assert marker not in json.dumps(result)
    assert vault.memories() == before


def check_lesson_surfaces(vault):
    from alicebot_api.project_view import ProjectView
    from alicebot_api.mcp.lessons import _select_published_lessons
    from alicebot_api.session_briefing import compile_session_brief
    from alicebot_api.vnext_agent_control import ALL_SENSITIVITY

    lesson = {**LESSON, "applies_when": RULE["applies_when"]}
    code, published = vault.call("commit", {**REQUEST, "lesson": lesson}, "admin")
    assert vault.ok(code) and published["status"] == "committed"
    code, waiting = vault.call("commit", {**REQUEST, "lesson": lesson}, "proposal")
    assert vault.ok(code) and waiting["status"] == "review_required"
    assert [item["id"] for item in waiting["similar"]] == [published["memory"]["id"]]

    def view():
        request = {"task": RULE["applies_when"]}
        code, lessons = vault.call("alice_lessons", request, "proposal")
        assert vault.ok(code) and lessons["count"] == 1
        assert lessons["yours"]["waiting"] == 1
        code, queue = vault.call("alice_memory_review", {"status": "pending_review"}, "admin")
        assert vault.ok(code) and queue["count"] == 1
        with vault.store() as store:
            choices, _ = _select_published_lessons(
                store, domains=[], sensitivity=list(ALL_SENSITIVITY), projects=(), limit=21, order=None,
            )
            assert len(choices) == 1
            lines = []
            if not hasattr(vault, "transport"):
                brief = compile_session_brief(
                    store, effective_domains=(), effective_sensitivity_allowed=ALL_SENSITIVITY,
                    effective_project_scope=(), project_view=ProjectView.unscoped(), exclude_global_domains=frozenset(),
                    query=None,
                )
                lines = [line for line in brief.splitlines() if "Shared lessons:" in line]
                assert any("Shared lessons: 1." in line for line in lines)
        return lessons, queue, lines

    before = view()
    made = stored(vault)
    assert view() == before
    code, recall = vault.call("alice_recall", {"query": RULE["rule"]}, "admin")
    assert vault.ok(code) and str(made["memory"]["id"]) in json.dumps(recall)

STATES = (
    "active", "rejected", "superseded", "expired", "archived", "private_only", "stale",
    "pending", "past-deadline", "missing-confirmation", "null-confirmation", "resolved-confirmation", "candidate",
)
DOORS = (
    "correct", "legacy-correct", "edit-text", "edit-title", "supersede", "confirm-text", "confirm",
    "confirm-reject", "accept", "reject", "private", "undo", "forget", "expire",
    "unexpire", "redact", "accept-consolidation",
)
_PENDING = {"pending", "past-deadline", "missing-confirmation", "null-confirmation", "resolved-confirmation"}
PAST = "2020-01-01T00:00:00Z"


def seed_lifecycle_subject(vault, kind, state, *, created_by="admin"):
    if kind == "rule":
        made = stored(vault)
    else:
        fields = {"lesson": LESSON} if kind == "lesson" else {"title": "Fixture", "canonical_text": "Fixture content."}
        code, made = vault.call("commit", {**REQUEST, **fields})
        assert vault.ok(code) and made["status"] == "committed", (code, made)
    mid = made["memory"]["id"]
    confirmation_id = f"confirm-{uuid4()}"
    meta = made["memory"]["metadata_json"]
    write = dict(meta["agentic_memory"])
    patch = {}
    if state in {"expired", "stale"}:
        patch["valid_to"] = PAST
    if state in _PENDING:
        write["status"] = "confirmation_required"
        if state != "missing-confirmation":
            write["confirmation"] = None if state == "null-confirmation" else {
                "confirmation_id": confirmation_id,
                "status": "confirmed" if state == "resolved-confirmation" else "pending",
                "agent_id": created_by,
                "expires_at": PAST if state == "past-deadline" else "2099-01-01T00:00:00Z",
                "proposed_text": made["memory"]["canonical_text"],
            }
        with vault.store() as store:
            store.lock_graph_mutation()
            acquire_exclusive_label_lock(store)
            memory = made["memory"]
            waiting = store.create_memory({
                field: memory[field]
                for field in ("title", "canonical_text", "summary", "memory_type", "domain", "sensitivity", "value")
            } | {
                "memory_key": f"fixture.pending.{uuid4()}",
                "status": "needs_review",
                "confirmation_status": "unconfirmed",
                "metadata_json": {**meta, "agentic_memory": write},
                "created_by_agent_id": created_by,
                "confirmation_id": confirmation_id,
            }, actor_type="user")
        return str(waiting["id"]), confirmation_id
    elif state not in {"active", "expired"}:
        patch["status"] = state
        if state == "private_only":
            patch["sensitivity"] = "private"
        if state == "candidate":
            patch["metadata_json"] = {**meta, "agentic_memory": {**write, "status": "review_required"}}
    if patch:
        seed_state(vault, mid, **patch)
    return mid, confirmation_id


def lifecycle_request(door, *, http, memory_id, confirmation_id):
    reason = "Synthetic lifecycle decision."
    text = "Synthetic replacement text."
    if door in {"confirm-text", "confirm", "confirm-reject"}:
        if door == "confirm-reject" and not http:
            return "commit", {"confirmation_id": confirmation_id, "confirmation_action": "reject"}
        payload = {"confirmation_id": confirmation_id, "action": "reject" if door == "confirm-reject" else "confirm"}
        if door == "confirm-text":
            payload["canonical_text"] = text
        if http:
            return "confirm", payload
        return "manage", {"memory_id": memory_id, **payload}
    if door in {"undo", "forget", "expire", "unexpire", "redact", "accept-consolidation"}:
        return "manage", {"action": door.replace("-", "_"), "memory_id": memory_id, "reason": reason}
    if door in {"correct", "legacy-correct"}:
        return "correct", {"memory_id": memory_id, "canonical_text": text, "reason": reason}
    action = {
        "edit-text": "edit" if http else "edit-and-approve",
        "edit-title": "edit" if http else "edit-and-approve",
        "supersede": "supersede-existing",
        "accept": "accept" if http else "approve",
        "reject": "reject",
        "private": "private",
    }[door]
    payload = {"action": action, "memory_id" if http else "review_item_id": memory_id, "reason": reason}
    if door == "edit-text":
        payload.update({"canonical_text": text} if http else {"body": {"text": text}})
    if door == "edit-title":
        payload["title"] = text
    if door == "supersede":
        payload["replacement_title"] = text
    return "review", payload


def lifecycle_reply(vault, door, kind, state, caller):
    mid, confirmation_id = seed_lifecycle_subject(vault, kind, state)
    before = row(vault, mid)
    with vault.store() as store:
        before_count = len(store.list_revisions(str(mid)))
    http = getattr(vault, "transport", "mcp") == "http"
    door_name, args = lifecycle_request(door, http=http, memory_id=mid, confirmation_id=confirmation_id)
    code, answer = vault.call(door_name, args, caller)
    after = row(vault, mid)
    with vault.store() as store:
        after_count = len(store.list_revisions(str(mid)))
    detail = answer.get("detail")
    error = answer.get("error") or (detail if isinstance(detail, dict) else {})
    reasons = (answer.get("policy_decision") or {}).get("reasons")
    label = answer.get("reason") or error.get("code") or (detail if isinstance(detail, str) else None)
    if isinstance(label, str):
        if "this memory carries a " in label:
            label = "protected block"
        else:
            label = label.replace(str(mid), "<memory>")
    key = kind
    changes = sorted(column for column in before if before[column] != after[column])
    text_kept = before["title"] == after["title"] and before["canonical_text"] == after["canonical_text"]
    block_kept = before["value"].get(key) == after["value"].get(key)
    return {
        "reply": code, "reason": label, "reasons": reasons, "result_status": answer.get("status"),
        "from": before["status"], "row": after["status"], "validity": after["valid_to"] != before["valid_to"],
        "text_kept": text_kept, "block_kept": block_kept, "other_columns": changes,
        "revisions": after_count - before_count,
    }


def check_lifecycle_case(vault, door, state, *, keyed=True):
    callers = ("admin", "trusted") if keyed else ("owner", "trusted", "scoped")
    for caller in callers:
        lesson = lifecycle_reply(vault, door, "lesson", state, caller)
        rule = lifecycle_reply(vault, door, "rule", state, caller)
        # Keep the backend's public reason as well as the stored result in the evidence.
        print("LIFECYCLE", json.dumps({
            "transport": getattr(vault, "transport", "sqlite-mcp"), "state": state, "door": door,
            "caller": caller, "keyed": keyed, "lesson": lesson, "rule": rule,
        }, default=str))
        compared = set(rule) - {"other_columns"}
        assert {field: lesson[field] for field in compared} == {field: rule[field] for field in compared}, (
            door, state, caller, lesson, rule,
        )
        if keyed and caller == "trusted":
            assert not lesson["other_columns"] and not rule["other_columns"], (door, state, lesson, rule)
