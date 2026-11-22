"""Common assertions for lesson visibility at the MCP doors."""

import json
from time import process_time
from uuid import uuid4

from alicebot_api.vnext_agent_keys import hash_agent_key
from alicebot_api.vnext_event_log import append_event


def omit_key_use(rows, key):
    """Ignore only the timestamp of authentication with the supplied key."""
    if key is not None:
        key_hash = hash_agent_key(key)
        for row in rows["agent_api_keys"]:
            if row.get("key_hash") == key_hash:
                row.pop("last_used_at")
    return rows


LESSON_BLOCK = {
    "applies_when": "rebasing",
    "what_happened": "A synthetic conflict hid a change.",
    "do_instead": "Inspect both sides.",
    "paths": ["src/*.py"],
}
REJECTION_REASON = "Synthetic rejection reason"
APPROVAL_REASON = "Synthetic approval reason"
STATES = ("candidate", "rejected", "active")


def make_review_lessons(call, writer, reviewer):
    rows = {}
    for state in STATES:
        payload = {
            "lesson": {**LESSON_BLOCK, "applies_when": f"rebasing synthetic {state}"},
            "idempotency_key": f"lesson-fixture-{state}-{uuid4()}",
        }
        failed, result = call(writer, "alice_memory_commit", payload)
        assert not failed and result["status"] == "review_required", result
        rows[state] = (str(result["memory"]["id"]), payload)
    failed, result = call(reviewer, "alice_memory_correct", {
        "action": "reject", "review_item_id": rows["rejected"][0], "reason": REJECTION_REASON,
    })
    assert not failed, result
    failed, result = call(reviewer, "alice_memory_correct", {
        "action": "approve", "review_item_id": rows["active"][0], "reason": APPROVAL_REASON,
    })
    assert not failed, result
    return rows


def assert_lesson_absent(body, rows, states=("candidate", "rejected")):
    serialized = json.dumps(body, default=str).lower()
    for state in states:
        memory_id, payload = rows[state]
        assert memory_id.lower() not in serialized
        assert memory_id.replace("-", "").lower() not in serialized
        assert payload["lesson"]["applies_when"].lower() not in serialized
        if state == "rejected":
            assert REJECTION_REASON.lower() not in serialized


def check_lesson_visibility(call, writer, reviewer, readers, snapshot):
    rows = make_review_lessons(call, writer, reviewer)
    missing = str(uuid4())
    active_id = rows["active"][0]
    for reader, same_scope, may_read_review in readers:
        expected = {row[0] for row in rows.values()} if may_read_review else {active_id} if same_scope else set()
        failed, review = call(reader, "alice_memory_review", {"status": "all"})
        assert not failed and {str(item["id"]) for item in review["items"]} == expected, review
        assert review["count"] == len(expected)
        failed, review = call(reader, "alice_memory_review", {"status": "pending_review", "limit": 1})
        assert not failed and [str(item["id"]) for item in review["items"]] == (
            [rows["candidate"][0]] if may_read_review else []
        )
        assert review["count"] == int(may_read_review)
        for tool, field in (("alice_memory_review", "review_item_id"), ("alice_explain", "memory_id")):
            before = snapshot(reader)
            missing_answer = call(reader, tool, {field: missing})
            assert snapshot(reader) == before
            for state, (memory_id, _) in rows.items():
                before = snapshot(reader)
                answer = call(reader, tool, {field: memory_id})
                if memory_id in expected:
                    assert not answer[0] and memory_id in json.dumps(answer[1]), answer
                    if state in {"rejected", "active"}:
                        assert (REJECTION_REASON if state == "rejected" else APPROVAL_REASON) in json.dumps(answer[1])
                else:
                    assert answer[0] and answer[0] == missing_answer[0], answer
                    assert json.dumps(answer[1]).replace(memory_id, missing) == json.dumps(missing_answer[1])
                    # Some exact doors echo the supplied ID in the same way as the missing-ID reply.
                    assert_lesson_absent(json.dumps(answer[1]).replace(memory_id, missing), rows, (state,))
                    assert snapshot(reader) == before
        failed, lessons = call(reader, "alice_lessons", {"task": "rebasing"})
        assert not failed and [str(item["id"]) for item in lessons["lessons"]] == ([active_id] if same_scope else [])


def check_lesson_replays(call, writer, reviewer, readers, snapshot):
    rows = make_review_lessons(call, writer, reviewer)
    for reader, same_scope, may_read_review in readers:
        conflict, mismatch = call(reader, "alice_memory_commit", {
            **rows["active"][1], "lesson": {**LESSON_BLOCK, "applies_when": "rebasing another change"},
        })
        assert conflict and mismatch["error"]["code"] == "tool_request_failed", mismatch
        for state, (memory_id, payload) in rows.items():
            before = snapshot(reader)
            failed, answer = call(reader, "alice_memory_commit", payload)
            if may_read_review or same_scope and state == "active":
                assert not failed and str(answer["memory"]["id"]) == memory_id, answer
                assert answer["memory"]["status"] == state
                assert answer["status"] == ("committed" if state == "active" else "review_required")
            else:
                assert failed and answer == mismatch, answer
                if state != "active":
                    assert snapshot(reader) == before
    new_payload = {"lesson": rows["candidate"][1]["lesson"]}
    peer = next(reader for reader, same_scope, may_read_review in readers if same_scope and not may_read_review)
    failed, answer = call(peer, "alice_memory_commit", new_payload)
    assert not failed and answer["status"] == "review_required", answer
    assert answer["memory"]["id"] != rows["candidate"][0]


def check_hidden_lesson_verbs(call, writer, reviewer, peers, snapshot):
    rows = make_review_lessons(call, writer, reviewer)
    missing = str(uuid4())
    for peer in peers:
        for tool, field, action in (
            ("alice_memory_correct", "review_item_id", "reject"),
            ("alice_memory_manage", "memory_id", "forget"),
            ("alice_memory_manage", "memory_id", "expire"),
            ("alice_memory_manage", "memory_id", "undo"),
        ):
            before = snapshot(peer)
            absent = call(peer, tool, {field: missing, "action": action, "reason": "Synthetic test"})
            assert snapshot(peer) == before
            for state in ("candidate", "rejected"):
                memory_id = rows[state][0]
                hidden = call(peer, tool, {field: memory_id, "action": action, "reason": "Synthetic test"})
                assert hidden[0] == absent[0]
                assert json.dumps(hidden[1]).replace(memory_id, missing) == json.dumps(absent[1])
                assert snapshot(peer) == before
    failed, result = call(writer, "alice_memory_manage", {
        "action": "forget", "memory_id": rows["candidate"][0],
    })
    assert failed and result["error"]["code"] == "not_permitted", result
    failed, result = call(reviewer, "alice_memory_manage", {
        "action": "forget", "memory_id": rows["candidate"][0],
    })
    assert not failed and result["status"] == "forgotten", result
    for peer in peers:
        failed, result = call(peer, "alice_explain", {"memory_id": rows["candidate"][0]})
        assert failed and result["error"]["code"] == "tool_request_failed", result


def seed_timing_lessons(store, count):
    for index in range(count):
        store.create_memory({
            "memory_key": f"synthetic-review-timing-{index}",
            "memory_type": "procedure",
            "status": "candidate",
            "domain": "project",
            "sensitivity": "private",
            "title": "Synthetic waiting lesson",
            "canonical_text": "Synthetic waiting lesson.",
            "value": {"lesson": LESSON_BLOCK},
            "created_by_agent_id": "timing-writer",
            "project_scope": ["alpha"],
        }, actor_type="user")


def check_lesson_review_growth(small, large):
    from tests.unit.test_saved_quote_ref_reading import assert_cost_grows_linearly

    samples = {200: [], 2000: []}

    def run(read, size):
        start = process_time()
        read()
        samples[size].append(process_time() - start)

    assert_cost_grows_linearly(lambda: run(small, 200), lambda: run(large, 2000), size_ratio=10, what="lesson review")
    for size, durations in samples.items():
        print(f"REVIEW_TIMING {size} {min(durations):.6f}")


def check_keyless_lesson_visibility(call):
    writer = {"agent_id": "local-writer", "permission_profile": "memory_proposal_agent", "project_scope": ["alpha"]}
    reviewer = {"agent_id": "local-reviewer", "permission_profile": "admin_agent"}
    peer = {"agent_id": "local-peer", "permission_profile": "trusted_local_agent"}
    rows = make_review_lessons(call, writer, reviewer)
    for identity in ({}, writer, reviewer, peer):
        failed, result = call(identity, "alice_memory_review", {"status": "all"})
        expected = {row[0] for row in rows.values()} if identity != peer else {rows["active"][0]}
        assert not failed and {item["id"] for item in result["items"]} == expected, result
        for tool, field in (("alice_memory_review", "review_item_id"), ("alice_explain", "memory_id")):
            memory_id = rows["candidate"][0]
            failed, result = call(identity, tool, {field: memory_id})
            if identity == peer:
                missing = str(uuid4())
                assert failed
                absent = call(identity, tool, {field: missing})
                assert (failed, json.dumps(result).replace(memory_id, missing)) == (absent[0], json.dumps(absent[1]))
            else:
                assert not failed and memory_id in json.dumps(result), result


def check_lesson_pack(read, rows, *, may_read_review, same_scope=True):
    failed, pack = read()
    assert not failed, pack
    recent = pack.get("recent_changes", [])
    targets = {entry["target_id"] for entry in recent}
    trace = pack["trace"]["stages"]["recent_changes"]
    assert trace["candidate_count"] == len(recent)
    if not may_read_review:
        assert_lesson_absent(pack, rows)
    for state, (memory_id, _) in rows.items():
        assert (memory_id in targets) == (same_scope and (may_read_review or state == "active")), (state, targets)


def put_each_lesson_in_the_pack_event_prefix(store, rows):
    # The pack keeps five events. Give each saved lesson one of the newest
    # events so both review-only states reach the same admission test.
    for memory_id, _payload in rows.values():
        append_event(store, event_type="memory.updated", actor_type="user", target_type="memory", target_id=memory_id)


def check_lesson_publisher_ids(call, writer, reviewer, reader):
    block = {"applies_when": "rebasing", "what_happened": "A conflict hid a change.",
             "do_instead": "Inspect both sides.", "paths": ["src/*.py"]}
    failed, result = call(writer, "alice_memory_commit", {"lesson": block})
    assert not failed and result["status"] == "review_required", result
    failed, result = call(reviewer, "alice_memory_correct", {
        "action": "approve", "review_item_id": result["memory"]["id"],
    })
    assert not failed, result
    failed, result = call(reader, "alice_lessons", {"task": "rebasing"})
    assert not failed and result["count"] == 1, result
    assert result["lessons"][0]["writer"]["id"] == "writer"
    assert result["lessons"][0]["published"]["by"] == "reviewer"
