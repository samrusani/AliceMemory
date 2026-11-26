"""Standing to read lessons through the real PostgreSQL MCP and HTTP handlers."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from uuid import uuid4

import pytest

from alicebot_api.cli.runner import main as cli_main
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.routers.workspaces import _vnext_workspace_payload
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_keys import create_agent_key, resolve_protected_agent_identity
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.derived_labels_postgres_support import LabelHarness, label_harness  # noqa: F401
from tests.integration.redaction_containment_support import keyless_owner
from tests.integration.test_label_change_policy_postgres import _snapshot
from tests.support.lesson_visibility_contract import (
    APPROVAL_REASON,
    LESSON_BLOCK,
    REJECTION_REASON,
    assert_lesson_absent,
    check_hidden_lesson_verbs,
    check_keyless_legacy_lesson_correction,
    check_keyless_lesson_visibility,
    check_keyless_read_case,
    check_lesson_pack,
    check_lesson_publisher_ids,
    check_lesson_replays,
    check_lesson_review_growth,
    check_lesson_visibility,
    check_saved_lesson_references,
    make_review_lessons,
    omit_key_use,
    put_each_lesson_in_the_pack_event_prefix,
    seed_keyless_read_cases,
    seed_timing_lessons,
)
from tests.unit.test_identity_claim_codes import wire


@pytest.fixture
def lesson_review_postgres(label_harness, monkeypatch):
    h = label_harness
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    writer = h.key("memory_proposal_agent", project="alpha")
    reviewer = h.key("admin_agent", project="alpha")
    admin = h.key("admin_agent")
    peer = h.key("memory_proposal_agent", project="alpha")
    with h.store() as store:
        _, scoped = create_agent_key(
            store, user_id=h.user_id, agent_id="review-scoped", permission_profile="project_scoped_agent",
            project_scope="alpha",
        )
    reader = h.key("read_only_agent", project="alpha")
    trusted = h.key("trusted_local_agent")
    outside = h.key("memory_proposal_agent", project="beta")
    outside_admin = h.key("admin_agent", project="beta")
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)

    def call(key, tool, arguments):
        if key is None:
            monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        else:
            monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        return wire(context, tool, arguments)

    def snapshot(key):
        return omit_key_use({table: [row for (row,) in rows] for table, rows in _snapshot(h).items()}, key)

    return call, snapshot, writer, reviewer, admin, peer, scoped, reader, trusted, outside, outside_admin


def test_postgres_read_only_project_key_cannot_read_waiting_or_rejected_lessons(lesson_review_postgres):
    """Guard edit: include active lessons in the review-only states."""
    call, snapshot, writer, reviewer, admin, peer, scoped, reader, trusted, outside, outside_admin = lesson_review_postgres
    check_lesson_visibility(call, writer, reviewer, [
        (peer, True, False), (scoped, True, False), (reader, True, False), (trusted, True, False),
        (writer, True, True), (reviewer, True, True), (admin, True, True), (outside, False, False),
        (outside_admin, False, False),
    ], snapshot)


def test_postgres_lesson_idempotency_conflicts_withhold_the_row(lesson_review_postgres):
    """Guard edit: also refuse the writer and decider replay of a review-only lesson."""
    call, snapshot, writer, reviewer, admin, peer, scoped, _reader, trusted, outside, outside_admin = lesson_review_postgres
    check_lesson_replays(call, writer, reviewer, [
        (peer, True, False), (scoped, True, False), (trusted, True, False), (writer, True, True),
        (reviewer, True, True), (admin, True, True), (outside, False, False), (outside_admin, False, False),
    ], snapshot)


def test_postgres_lesson_mutations_use_the_missing_id_answer(lesson_review_postgres):
    """Guard edit: also hide a review-only lesson from its writer and decider at a write verb."""
    call, snapshot, writer, reviewer, _admin, peer, scoped, reader, trusted, outside, outside_admin = lesson_review_postgres
    check_hidden_lesson_verbs(call, writer, reviewer, (peer, scoped, reader, trusted, outside, outside_admin), snapshot)


@pytest.mark.parametrize("shape", ("typed", "incidental", "excerpt", "object"))
def test_postgres_saved_lesson_references_follow_review_standing(label_harness, lesson_review_postgres, shape):
    """Guard edit: remove references to review-only lessons even for their writer."""
    call, _snapshot, *keys = lesson_review_postgres
    check_saved_lesson_references(
        call, label_harness.store, keys, shape, http=label_harness.request,
        keyless_owner=lambda: keyless_owner(label_harness),
    )


def test_http_lesson_rows_and_events_follow_review_standing(label_harness, lesson_review_postgres):
    """Guard edit: exclude active lessons from the recent-commits feed."""
    h = label_harness
    call, snapshot, writer, reviewer, admin, _peer, _scoped, reader, trusted, outside, outside_admin = lesson_review_postgres
    rows = make_review_lessons(call, writer, reviewer, confirmation_writer=reviewer)
    missing = str(uuid4())
    audit = "/v0/vnext/memories/{}/audit"
    recent = "/v0/vnext/memories/recent-commits"
    workspace = "/v0/vnext/workspace"
    before = snapshot(trusted)
    missing_audit = h.request("GET", audit.format(missing), key=trusted)[:2]
    assert missing_audit[0] == 404
    assert snapshot(trusted) == before
    for memory_id, _ in rows.values():
        status, result, _ = h.request("GET", audit.format(memory_id), key=admin)
        assert status == 200 and result["memory"]["id"] == memory_id
    for state in ("candidate", "rejected", "needs_review"):
        memory_id = rows[state][0]
        before = snapshot(trusted)
        assert h.request("GET", audit.format(memory_id), key=trusted)[:2] == missing_audit
        assert snapshot(trusted) == before
    status, result, _ = h.request("GET", audit.format(rows["active"][0]), key=trusted)
    assert status == 200 and APPROVAL_REASON in json.dumps(result)
    status, result, _ = h.request("GET", recent, key=trusted)
    assert status == 200 and result["count"] == 1
    assert [item["id"] for item in result["recent_commits"]] == [rows["active"][0]]
    assert_lesson_absent(result, rows)
    status, result, _ = h.request("GET", workspace, key=trusted)
    assert status == 200
    with h.store() as store:
        identity = resolve_protected_agent_identity(store, user_id=h.user_id, raw_key=trusted, payload={})
        raw_workspace = _vnext_workspace_payload(store, identity=identity)
    for shown in (result, raw_workspace):
        assert shown["agent_activity"]["pending_review_items"] == []
        assert shown["review_memories"] == []
        assert [str(item["id"]) for item in shown["agent_activity"]["recent_commits"]] == [rows["active"][0]]
        assert_lesson_absent(shown, rows)
    serialized = json.dumps(result)
    assert REJECTION_REASON not in serialized
    assert APPROVAL_REASON in serialized
    status, result, _ = h.request("POST", "/v0/vnext/changes", key=trusted, payload={"topic": "rebasing"})
    assert status == 200
    assert_lesson_absent(result, rows)
    for key in (writer, reviewer, reader, outside, outside_admin):
        for path in (recent, workspace, audit.format(missing), *(audit.format(row[0]) for row in rows.values())):
            status, result, _ = h.request("GET", path, key=key)
            assert status == 403
            assert "unbound_operator_key_required" in result["policy_decision"]["reasons"] or key == reader


@pytest.mark.parametrize("surface", ("mcp", "http"))
def test_postgres_context_pack_recent_changes_follow_review_standing(label_harness, lesson_review_postgres, surface):
    """Guard edit: return no recent changes to callers with an identity."""
    h = label_harness
    call, _snapshot, writer, reviewer, admin, peer, scoped, reader, trusted, outside, outside_admin = lesson_review_postgres
    rows = make_review_lessons(call, writer, reviewer)
    request = {"query": "rebasing", "max_tokens": 8000, "debug": True}
    with h.store() as store:
        prefix = put_each_lesson_in_the_pack_event_prefix(store, rows)
    for key, same_scope, may_read_review in (
        (peer, True, False), (scoped, True, False), (reader, True, False), (trusted, True, False),
        (writer, True, True), (reviewer, True, True), (admin, True, True), (outside, False, False),
        (outside_admin, False, False),
    ):
        def read_http():
            status, pack, _ = h.request(
                "POST", "/v0/vnext/context-packs", payload={"query": "rebasing", "options": {"max_tokens": 8000}}, key=key,
            )
            return status != 201, pack

        read = (lambda: call(key, "alice_context_pack", request)) if surface == "mcp" else read_http
        check_lesson_pack(read, rows, prefix, may_read_review=may_read_review, same_scope=same_scope)


AUDIT_DOORS = ("explain", "review", "http", "cli")


@pytest.fixture
def exact_lesson_read(label_harness, lesson_review_postgres, monkeypatch, capsys):
    h = label_harness
    call = lesson_review_postgres[0]

    def read(door, memory_id, key):
        if door == "explain":
            failed, result = call(key, "alice_explain", {"memory_id": memory_id})
            return int(failed), result
        if door == "review":
            failed, result = call(key, "alice_memory_review", {"review_item_id": memory_id})
            return int(failed), result
        if door == "http":
            return h.request("GET", f"/v0/vnext/memories/{memory_id}/audit", key=key)[:2]
        args = ["--database-url", h.urls["app"], "--user-id", str(h.user_id), "vnext", "memories", "audit", memory_id]
        if key is None:
            monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        else:
            monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
            with h.store() as store:
                identity = resolve_protected_agent_identity(store, user_id=h.user_id, raw_key=key, payload={})
            args.extend(["--agent-id", identity.agent_id, "--permission-profile", identity.permission_profile])
            for project in identity.project_scope:
                args.extend(["--project-scope", project])
        code = cli_main(args)
        captured = capsys.readouterr()
        return code, json.loads(captured.err if code else captured.out)

    return read


@pytest.mark.parametrize("door", AUDIT_DOORS)
def test_postgres_exact_reads_finish_while_the_lifecycle_lock_is_held(
    label_harness, lesson_review_postgres, exact_lesson_read, door,
):
    """Guard edit: acquire the lifecycle lock while rechecking history."""
    h = label_harness
    _call, _snapshot, _writer, _reviewer, admin, _peer, scoped, _reader, trusted, *_ = lesson_review_postgres
    memory_id = str(h.memory(scope=("alpha",))["id"])
    with ThreadPoolExecutor(max_workers=1) as pool:
        for key in (scoped, trusted, admin, None):
            with keyless_owner(h) if key is None else nullcontext():
                with h.store() as holder:
                    holder.lock_graph_mutation()
                    future = pool.submit(exact_lesson_read, door, memory_id, key)
                    status, answer = future.result(timeout=5)
                    if door == "http" and key == scoped:
                        assert status == 403
                    else:
                        assert status == (200 if door == "http" else 0), answer
                        assert memory_id in json.dumps(answer)


@pytest.mark.parametrize("door", AUDIT_DOORS)
def test_postgres_exact_reads_withhold_a_decision_committed_during_the_history_read(
    label_harness, lesson_review_postgres, exact_lesson_read, monkeypatch, door,
):
    """Guard edit: also withhold unchanged published history from limited callers."""
    h = label_harness
    call, _snapshot, writer, reviewer, admin, _peer, scoped, _reader, trusted, *_ = lesson_review_postgres
    rows = make_review_lessons(call, writer, reviewer)
    memory_id = rows["active"][0]
    reader = trusted if door == "http" else scoped
    missing = str(uuid4())
    allowed = exact_lesson_read(door, memory_id, reader)
    assert allowed[0] == (200 if door == "http" else 0)
    absent = exact_lesson_read(door, missing, reader)
    reason = "Synthetic decision made while history was being read"
    list_revisions = PostgresVNextStore.list_revisions
    committed = []

    def revisions_after_decision(store, target_id, *args, **kwargs):
        if str(target_id) == memory_id and not committed:
            status, answer, _ = h.request("POST", f"/v0/vnext/memories/{memory_id}/review", key=admin, payload={
                "action": "reject", "reason": reason,
            })
            assert status == 200 and answer["memory"]["status"] == "rejected", answer
            committed.append(True)
        return list_revisions(store, target_id, *args, **kwargs)

    monkeypatch.setattr(PostgresVNextStore, "list_revisions", revisions_after_decision)
    answer = exact_lesson_read(door, memory_id, reader)
    assert committed
    serialized = json.dumps(answer)
    assert serialized.replace(memory_id, missing) == json.dumps(absent)
    assert reason not in serialized
    with h.store() as store:
        revision = next(row for row in store.list_revisions(memory_id) if row["reason"] == reason)
        event = next(row for row in store.list_events(target_type="memory", target_id=memory_id)
                     if row["event_type"] == "review.item_rejected")
        assert str(revision["id"]) not in serialized
        assert str(event["id"]) not in serialized


@pytest.fixture
def lesson_aggregate_rows(label_harness, lesson_review_postgres):
    h = label_harness
    call, _snapshot, writer, reviewer, admin, _peer, _scoped, _reader, trusted, *_ = lesson_review_postgres
    for index in range(3):
        failed, answer = call(writer, "alice_memory_commit", {
            "title": f"Ordinary proposal {index}",
            "canonical_text": f"Synthetic ordinary proposal number {index}.",
            "domain": "project", "sensitivity": "internal",
        })
        assert not failed and answer["memory"]["status"] == "candidate", answer
        writer_id = answer["memory"]["created_by_agent_id"]
        if index:
            failed, answer = call(reviewer, "alice_memory_correct", {
                "action": "approve", "review_item_id": answer["memory"]["id"],
            })
            assert not failed, answer

    def add_hidden_lessons():
        for index, state in enumerate(("candidate", "rejected", "candidate", "private_only", "rejected", "superseded")):
            failed, answer = call(writer, "alice_memory_commit", {
                "lesson": {**LESSON_BLOCK, "applies_when": f"Aggregate fixture {index}"},
            })
            assert not failed and answer["memory"]["status"] == "candidate", answer
            mid = answer["memory"]["id"]
            if state == "rejected":
                failed, answer = call(reviewer, "alice_memory_correct", {"action": "reject", "review_item_id": mid})
                assert not failed, answer
            if state == "private_only":
                status, answer, _ = h.request("POST", f"/v0/vnext/memories/{mid}/review", key=admin, payload={
                    "action": "private", "reason": "Synthetic private lesson",
                })
                assert status == 200, answer
            if state == "superseded":
                with h.store() as store:
                    store.update_memory(memory_id=mid, patch={"status": "superseded"}, actor_type="user")
            with h.store() as store:
                assert store.get_memory(mid)["status"] == state

    return admin, trusted, writer_id, add_hidden_lessons


@pytest.mark.parametrize("view", ("dogfooding", "workspace"))
def test_postgres_lesson_dashboard_counts_follow_review_standing(label_harness, lesson_aggregate_rows, view):
    """Guard edit: classify ordinary waiting memories as review-only lessons."""
    admin, trusted, _writer_id, add_hidden_lessons = lesson_aggregate_rows

    def counts(key):
        status, answer, _ = label_harness.request("GET", f"/v0/vnext/{view}", key=key)
        assert status == 200, answer
        dashboard = answer["dogfooding"] if view == "workspace" else answer
        return (
            dashboard["memory_status_counts"],
            dashboard["candidate_memories_created"],
            dashboard["candidate_memory_review_rate"],
            dashboard["sample_scope"]["memories"]["total_count"],
        )

    before = ({"candidate": 1, "active": 2}, 1, 0.667, 3)
    assert counts(trusted) == counts(admin) == before
    add_hidden_lessons()
    assert counts(trusted) == before
    assert counts(admin) == ({"candidate": 3, "active": 2, "rejected": 2, "private_only": 1, "superseded": 1}, 3, 0.444, 9)


def test_postgres_lesson_telemetry_counts_follow_review_standing(label_harness, lesson_aggregate_rows):
    """Guard edit: omit all readable memory proposal totals from telemetry."""
    admin, trusted, writer_id, add_hidden_lessons = lesson_aggregate_rows

    def count(key):
        status, answer, _ = label_harness.request("GET", "/v0/vnext/agents/policy-telemetry", key=key)
        assert status == 200, answer
        rows = answer["summary"]["memory_proposals_by_agent"]
        return {row["agent_id"]: row["count"] for row in rows}.get(writer_id, 0)

    assert count(trusted) == count(admin) == 3
    add_hidden_lessons()
    assert count(trusted) == 3
    assert count(admin) == 9


def test_private_only_lesson_keeps_its_standing_in_a_source_trace(label_harness, lesson_review_postgres):
    """Guard edit: include active lessons in the review-only states."""
    h = label_harness
    call, _snapshot, writer, _reviewer, admin, _peer, _scoped, _reader, trusted, *_ = lesson_review_postgres
    source = h.source(scope=("alpha",))
    path = f"/v0/vnext/traces/sources/{source['id']}"
    rows = {}
    for state in ("private_only", "active"):
        payload = {
            "lesson": {**LESSON_BLOCK, "applies_when": f"synthetic trace for {state}"},
            "source_refs": [f"source:{source['id']}"],
        }
        failed, answer = call(writer, "alice_memory_commit", payload)
        assert not failed and answer["status"] == "review_required", answer
        memory_id = answer["memory"]["id"]
        rows[state] = (memory_id, payload)
        with h.store() as store:
            memory = store.get_memory(memory_id)
            store.update_memory(memory_id=memory_id, patch={
                "metadata_json": {**memory["metadata_json"], "source_id": str(source["id"])},
            }, actor_type="user")
        status, result, _ = h.request("POST", f"/v0/vnext/memories/{memory_id}/review", key=admin, payload={
            "action": "private" if state == "private_only" else "accept", "reason": "Synthetic trace review",
        })
        assert status == 200 and result["memory"]["status"] == state, result
    status, answer, _ = h.request("GET", path, key=admin)
    assert status == 200 and rows["private_only"][0] in json.dumps(answer)
    status, answer, _ = h.request("GET", path, key=trusted)
    assert status == 200 and rows["active"][0] in json.dumps(answer)
    assert_lesson_absent(answer, rows, ("private_only",))


def test_postgres_keyless_lesson_review_uses_the_resolved_identity(label_harness, monkeypatch):
    """Guard edit: include active lessons in the review-only states."""
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    check_keyless_lesson_visibility(lambda claims, tool, args: wire(context, tool, {**args, **claims}))


@pytest.fixture
def postgres_keyless_read_cases(label_harness, monkeypatch):
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    return seed_keyless_read_cases(context, h.store)


@pytest.mark.parametrize("profile", ["trusted_local_agent", "project_scoped_agent"])
@pytest.mark.parametrize("state", ["candidate", "rejected", "private_only"])
@pytest.mark.parametrize("door", ["detail", "explain", "queue", "replay"])
def test_postgres_keyless_publisher_reads_other_writers_lessons(postgres_keyless_read_cases, profile, state, door):
    """Guard edit: limit publishing standing to owners and admins."""
    check_keyless_read_case(postgres_keyless_read_cases, profile, state, door)


def test_postgres_legacy_correction_gives_the_missing_answer_for_unpublished_lessons(label_harness, monkeypatch):
    """Guard edit: withhold even a decider's unpublished correction target."""
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    check_keyless_legacy_lesson_correction(lambda claims, tool, args: wire(context, tool, {**args, **claims}))


def test_postgres_lesson_review_cost_over_200_and_2000_waiting_lessons(label_harness, monkeypatch):
    """Guard edit: stop returning active lessons in the all-status review queue."""
    def setup(count):
        h = LabelHarness(label_harness.urls, uuid4())
        with h.store() as store:
            ContinuityStore(store.conn).create_user(h.user_id, f"timing-{h.user_id}@example.invalid", "Timing fixture")
        reader = h.key("read_only_agent", project="alpha")
        admin = h.key("admin_agent", project="alpha")
        context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
        monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
        failed, published = wire(context, "alice_memory_commit", {"lesson": LESSON_BLOCK})
        assert not failed and published["status"] == "committed"
        with h.store() as store:
            seed_timing_lessons(store, count)

        def read():
            monkeypatch.setenv("ALICE_AGENT_API_KEY", reader)
            failed, result = wire(context, "alice_memory_review", {"status": "all", "limit": 1})
            assert not failed and result["count"] == 1, result
            assert result["items"][0]["id"] == published["memory"]["id"]

        return read

    check_lesson_review_growth(setup(200), setup(2000))


def test_http_and_cli_lesson_verbs_use_the_missing_id_answer(
    label_harness, lesson_review_postgres, monkeypatch, capsys,
):
    """Guard edit: also hide a review-only lesson from its decider at a write verb."""
    h = label_harness
    call, snapshot, writer, reviewer, admin, _peer, scoped, _reader, trusted, *_ = lesson_review_postgres
    rows = make_review_lessons(call, writer, reviewer)
    missing = str(uuid4())
    monkeypatch.setenv("ALICE_AGENT_API_KEY", scoped)
    for verb in ("forget", "expire", "undo"):
        def request(memory_id):
            return h.request("POST", f"/v0/vnext/memories/{verb}", key=trusted, payload={
                "memory_id": memory_id, "reason": "Synthetic fixture",
            })[:2]

        def cli(memory_id):
            target = ["--memory-id", memory_id] if verb == "undo" else [memory_id]
            code = cli_main([
                "--database-url", h.urls["app"], "--user-id", str(h.user_id),
                "vnext", "memories", verb, *target, "--reason", "Synthetic fixture",
                "--agent-id", "review-scoped", "--permission-profile", "project_scoped_agent", "--project-scope", "alpha",
            ])
            output = capsys.readouterr()
            assert not output.out
            return code, json.loads(output.err)

        for key, run in ((trusted, request), (scoped, cli)):
            before = snapshot(key)
            absent = run(missing)
            assert snapshot(key) == before
            for state in ("candidate", "rejected"):
                memory_id = rows[state][0]
                hidden = run(memory_id)
                # Compare public bodies after normalizing the caller's own ID.
                assert json.dumps(hidden).replace(memory_id, missing) == json.dumps(absent)
                assert snapshot(key) == before
    status, result, _ = h.request("POST", "/v0/vnext/memories/forget", key=admin, payload={
        "memory_id": rows["candidate"][0], "reason": "Synthetic fixture",
    })
    assert status == 200 and result["status"] == "forgotten"


def test_http_and_cli_lesson_replays_keep_the_conflict_contract(
    label_harness, lesson_review_postgres, monkeypatch, capsys,
):
    """Guard edit: also refuse the writer and decider replay of a review-only lesson."""
    h = label_harness
    call, snapshot, writer, reviewer, admin, _peer, scoped, _reader, trusted, *_ = lesson_review_postgres
    rows = make_review_lessons(call, writer, reviewer)
    path = "/v0/vnext/memories/commit"
    plain = {"title": "Synthetic conflicting write", "canonical_text": "Synthetic conflict control."}
    control = h.request("POST", path, key=trusted, payload={
        **plain, "idempotency_key": rows["active"][1]["idempotency_key"],
    })[:2]
    assert control[0] == 400
    for state, (memory_id, payload) in rows.items():
        before = snapshot(trusted)
        status, result, _ = h.request("POST", path, key=trusted, payload=payload)
        if state == "active":
            assert status == 201 and result["memory"]["id"] == memory_id
        else:
            assert (status, result) == control
            assert snapshot(trusted) == before
        for key in (writer, admin):
            status, result, _ = h.request("POST", path, key=key, payload=payload)
            assert status == 201 and result["memory"]["id"] == memory_id

    # The CLI has no lesson-block flag. It can hit a held key with a plain commit.
    monkeypatch.setenv("ALICE_AGENT_API_KEY", scoped)
    errors = []
    for _memory_id, payload in rows.values():
        before = snapshot(scoped)
        code = cli_main([
            "--database-url", h.urls["app"], "--user-id", str(h.user_id),
            "vnext", "memories", "commit", "--title", plain["title"], "--text", plain["canonical_text"],
            "--idempotency-key", payload["idempotency_key"],
            "--agent-id", "review-scoped", "--permission-profile", "project_scoped_agent", "--project-scope", "alpha",
        ])
        result = capsys.readouterr()
        assert code == 1 and not result.out
        errors.append(json.loads(result.err))
        assert snapshot(scoped) == before
    assert errors[0] == errors[1] == errors[2]


def test_postgres_lesson_reader_sees_writer_and_reviewer_ids(label_harness, monkeypatch):
    """The limitations page changes if lesson publication stops returning the reviewer's id.

    Mutation: replace the publishing revision's agent actor id with ``owner``.
    """
    h = label_harness
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    with h.store() as store:
        writer = create_agent_key(store, user_id=h.user_id, agent_id="writer", permission_profile="project_scoped_agent", project_scope="alpha")[1]
        reviewer = create_agent_key(store, user_id=h.user_id, agent_id="reviewer", permission_profile="admin_agent", project_scope="alpha")[1]
        reader = create_agent_key(store, user_id=h.user_id, agent_id="reader", permission_profile="read_only_agent", project_scope="alpha")[1]
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)

    def call(key, tool, arguments):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        return wire(context, tool, arguments)

    check_lesson_publisher_ids(call, writer, reviewer, reader)
