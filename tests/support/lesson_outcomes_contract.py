"""Shared checks for the response and read budget of a writer's lesson lookup."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from time import process_time
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from alicebot_api.credential_floor import RATIONALE_WITHHELD_PLACEHOLDER
from alicebot_api.vnext_agent_keys import create_agent_key, hash_agent_key
from tests.unit.test_saved_quote_ref_reading import assert_cost_grows_linearly


BLOCK = {
    "applies_when": "Reviewing a synthetic service note",
    "what_happened": "The example missed a dependency.",
    "do_instead": "Check dependencies before editing.",
    "paths": ["src/example.py"],
}
QUERY = {"task": "reviewing a synthetic service note", "paths": BLOCK["paths"]}
MCP_REASONS = (
    ("correct", "reject", "memory_correct_reject", "alice_memory_correct action: reject"),
    ("manage", "forget", "agentic_memory_forget", "Agentic memory forgotten."),
    ("manage", "undo", "agentic_memory_undo", "Agentic memory commit undone."),
)
HTTP_REASONS = (
    ("review", "reject", "memory_review_reject", "vNext workspace memory review action: reject"),
    ("review", "private", "memory_review_private", "vNext workspace memory review action: private"),
    ("lifecycle", "forget", "agentic_memory_forget", "Agentic memory forgotten."),
    ("lifecycle", "undo", "agentic_memory_undo", "Agentic memory commit undone."),
)
TIMING_CASES = (
    "peer waiting", "own waiting", "own recent rejects", "own old rejects",
    "own recent publications", "same writer outside project",
)


@dataclass
class OutcomeVault:
    context: object
    store: object
    monkeypatch: object
    admin_url: str | None = None
    http: object = None
    sibling: object = None

    def key(self, agent, project="alpha", profile="memory_proposal_agent"):
        with self.store() as store:
            return create_agent_key(
                store, user_id=self.context.user_id, agent_id=agent,
                permission_profile=profile, project_scope=project,
            )[1]

    def call(self, key, name, args):
        from tests.unit.test_identity_claim_codes import wire

        if key is None:
            self.monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        else:
            self.monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        failed, result = wire(self.context, name, args)
        assert not failed, (name, result)
        return result

    def read(self, key, **claims):
        return self.call(key, "alice_lessons", {**QUERY, **claims})

    def commit(self, key, *, text=None, **args):
        lesson = BLOCK if text is None else {**BLOCK, "applies_when": text}
        return self.call(key, "alice_memory_commit", {"lesson": lesson, "sensitivity": "private", **args})

    def decide(self, key, mid, door, action, **args):
        if door == "correct":
            return self.call(key, "alice_memory_correct", {"action": action, "review_item_id": mid, **args})
        if door == "manage":
            return self.call(key, "alice_memory_manage", {"action": action, "memory_id": mid, **args})
        if door == "review":
            path, payload = f"/v0/vnext/memories/{mid}/review", {"action": action, **args}
        else:
            path, payload = f"/v0/vnext/memories/{action}", {"memory_id": mid, **args}
        status, result, _ = self.http("POST", path, key=key, payload=payload)
        assert status == 200, result
        return result

    @contextmanager
    def keyless(self):
        with self.store() as store:
            conn = store.conn
            ids = conn.execute("SELECT id FROM agent_api_keys WHERE revoked_at IS NULL").fetchall()
            conn.execute("UPDATE agent_api_keys SET revoked_at = CURRENT_TIMESTAMP WHERE revoked_at IS NULL")
        try:
            yield
        finally:
            with self.store() as store:
                for row in ids:
                    marker = "%s" if self.admin_url else "?"
                    store.conn.execute(f"UPDATE agent_api_keys SET revoked_at = NULL WHERE id = {marker}", (row["id"],))

    def snapshot(self):
        if self.admin_url:
            with psycopg.connect(self.admin_url, options="-c default_transaction_read_only=on") as conn:
                tables = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
                return {
                    table: [row for (row,) in conn.execute(
                        sql.SQL("SELECT to_jsonb(t) FROM public.{} t ORDER BY to_jsonb(t)::text").format(sql.Identifier(table))
                    )]
                    for (table,) in tables
                }
        with self.store() as store:
            tables = store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            return {
                table["name"]: sorted(
                    (dict(row) for row in store.conn.execute('SELECT * FROM "' + table["name"].replace('"', '""') + '"')),
                    key=lambda row: repr(sorted(row.items())),
                )
                for table in tables
            }


def seed_record(vault, store, *, writer, project="alpha", status="candidate", updated_at=None,
                decision_at=None, action=None, kind=None, reason=None, valid_to=None):
    """Create old/out-of-window rows without changing the vault clock or revision history."""
    block = {"lesson": BLOCK}
    fields = {
        "memory_key": str(uuid4()), "memory_type": "procedure", "status": status,
        "domain": "project", "sensitivity": "private", "canonical_text": BLOCK["applies_when"],
        "value": block, "created_by_agent_id": writer, "project_scope": [project],
        "metadata_json": {"agentic_memory": {"agent_identity": {"permission_profile": "admin_agent"}}},
    }
    if valid_to is not None:
        fields["valid_to"] = valid_to.isoformat(timespec="microseconds").replace("+00:00", "Z")
    row = store.create_memory(fields, actor_type="user")
    marker = "%s" if vault.admin_url else "?"
    if updated_at is not None:
        updated = updated_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
        store.conn.execute(f"UPDATE memories SET updated_at = {marker} WHERE id = {marker}", (updated, row["id"]))
    if action:
        insert = """
            INSERT INTO memory_revisions (
                id, user_id, memory_id, sequence_no, revision_number, action, memory_key, new_value,
                revision_type, reason, actor_type, created_at, source_event_ids, candidate, text_after
            ) VALUES ({params})
        """
        slots = (marker,) * 15
        at = decision_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
        store.conn.execute(insert.format(params=", ".join(slots)), (
            str(uuid4()), str(vault.context.user_id), str(row["id"]), 1, 1, action, row["memory_key"],
            Jsonb(block) if vault.admin_url else json.dumps(block), kind, reason, "user", at,
            Jsonb([]) if vault.admin_url else "[]", Jsonb({}) if vault.admin_url else "{}", BLOCK["applies_when"],
        ))
    return str(row["id"])


def check_writer_outcomes(vault):
    writer = vault.key("writer")
    same = vault.key("writer")
    outside = vault.key("writer", "beta")
    peer = vault.key("peer")
    reader = vault.key("reader")
    admin = vault.key("admin", None, "admin_agent")

    by_role = {}
    for name, key in (("writer", writer), ("peer", peer)):
        waiting = vault.commit(key, text="Waiting with identical words")["memory"]["id"]
        expired = vault.commit(key, text="Expiring while waiting")["memory"]["id"]
        vault.decide(admin, expired, "manage", "expire", reason="The task finished.")
        published = vault.commit(key, text="Reviewing a synthetic service note for publication")["memory"]["id"]
        vault.decide(admin, published, "correct", "approve")
        published_expired = vault.commit(key, text="Published before expiry")["memory"]["id"]
        vault.decide(admin, published_expired, "correct", "approve")
        vault.decide(admin, published_expired, "manage", "expire", reason="The task finished.")
        later_rejected = vault.commit(key, text="Published before rejection")["memory"]["id"]
        vault.decide(admin, later_rejected, "correct", "approve")
        vault.decide(admin, later_rejected, "correct", "reject", reason=f"Reason for {name} after approval.")
        forgotten = vault.commit(key, text="Forgotten with identical words")["memory"]["id"]
        vault.decide(admin, forgotten, "manage", "forget", reason=f"Reason for {name} after forgetting.")
        undone = vault.commit(key, text="Undone with identical words")["memory"]["id"]
        vault.decide(admin, undone, "correct", "approve")
        vault.decide(admin, undone, "manage", "undo", reason=f"Reason for {name} after undoing.")
        rejected = vault.commit(key, text="Rejected with identical words" + ("x" * 180))["memory"]["id"]
        vault.decide(admin, rejected, "correct", "reject", reason=f"Reason for {name}. " + ("z" * 330))
        redacted = vault.commit(key, text="Removed with identical words")["memory"]["id"]
        vault.decide(admin, redacted, "manage", "redact", reason="Remove the example.")
        by_role[name] = (waiting, published, later_rejected, forgotten, undone, rejected)
    beta = vault.commit(outside, text="Waiting for review in another project")["memory"]["id"]
    owned = vault.commit(admin, text="Reviewing a synthetic service note as admin", project_scope=["alpha"])
    assert owned["status"] == "committed"
    pending = vault.commit(admin, text="Pending admin confirmation", project_scope=["alpha"], confidence=0.7)
    assert pending["memory"]["status"] == "needs_review"
    confirmed = vault.commit(admin, text="Reviewing a synthetic service note for confirmation",
                             project_scope=["alpha"], confidence=0.7)
    vault.decide(admin, confirmed["memory"]["id"], "manage", "confirm", confirmation_id=confirmed["confirmation_id"])

    baseline = vault.read(reader)
    assert set(baseline) == {"count", "framing", "lessons"}
    assert baseline["count"] == 4
    for role, key in (("writer", writer), ("peer", peer)):
        answer = vault.read(key)
        block = answer.pop("yours")
        assert answer == baseline
        assert block["waiting"] == 2 and block["published_in_last_7_days"] == 2
        assert block["not_published_in_last_7_days"] == 4
        _, _, _, forgotten, undone, rejected = by_role[role]
        details = block["not_published"]
        assert [detail["id"] for detail in details] == [rejected, undone, forgotten]
        assert details[0]["applies_when"] == ("Rejected with identical words" + ("x" * 180))[:159] + "…"
        assert details[0]["reason"] == (f"Reason for {role}. " + ("z" * 330))[:299] + "…"
        assert all(detail["at"] for detail in details)
        other = "peer" if role == "writer" else "writer"
        encoded = json.dumps(block)
        assert all(mid not in encoded for mid in (*by_role[other], beta))
        assert f"Reason for {other}" not in encoded
        if role == "writer":
            assert vault.read(same)["yours"] == block
    assert vault.read(outside)["yours"] == {
        "waiting": 1, "published_in_last_7_days": 0, "not_published_in_last_7_days": 0,
    }
    assert vault.read(admin)["yours"] == {
        "waiting": 0, "published_in_last_7_days": 2, "not_published_in_last_7_days": 0,
    }
    with vault.keyless():
        assert vault.read(None) == baseline
        assert vault.read(None, agent_id="writer", permission_profile="memory_proposal_agent",
                          project_scope=["alpha"]) == baseline


def check_outcome_ceiling(vault):
    writer = vault.key("writer", profile="project_scoped_agent")
    reader = vault.key("writer", profile="read_only_agent")
    admin = vault.key("admin", None, "admin_agent")
    private_waiting = vault.commit(writer, text="Private waiting fixture")
    private_rejected = vault.commit(writer, text="Private rejected fixture")
    vault.commit(writer, text="Internal waiting fixture", sensitivity="internal")
    waiting_id = private_waiting["memory"]["id"]
    rejected_id = private_rejected["memory"]["id"]
    reason = "The private fixture needs more evidence."
    vault.decide(admin, rejected_id, "correct", "reject", reason=reason)

    limited = vault.read(reader)["yours"]
    assert limited == {"waiting": 1, "published_in_last_7_days": 0, "not_published_in_last_7_days": 0}
    payload = json.dumps(limited)
    for private in (waiting_id, rejected_id, "Private waiting fixture", "Private rejected fixture", reason):
        assert private not in payload
    full = vault.read(writer)["yours"]
    assert (full["waiting"], full["published_in_last_7_days"], full["not_published_in_last_7_days"]) == (2, 0, 1)
    (detail,) = full["not_published"]
    assert (detail["id"], detail["applies_when"], detail["reason"]) == (rejected_id, "Private rejected fixture", reason)


def check_ordinary_memories_are_not_outcomes(vault):
    writer = vault.key("writer", profile="memory_proposal_agent")
    admin = vault.key("admin", None, "admin_agent")

    def ordinary(name, *, memory_type):
        reply = vault.call(writer, "alice_memory_commit", {
            "title": name, "canonical_text": name, "memory_type": memory_type, "sensitivity": "private",
            "domain": "project", "confidence": 0.7,
        })
        assert reply["memory"]["status"] == "candidate"
        return reply["memory"]["id"]

    ordinary("Ordinary procedure in review", memory_type="procedure")
    rejected = ordinary("Ordinary memory for rejection", memory_type="semantic")
    approved = ordinary("Ordinary memory for approval", memory_type="semantic")
    assert "yours" not in vault.read(writer)
    assert vault.decide(admin, rejected, "correct", "reject")["memory"]["status"] == "rejected"
    assert vault.decide(admin, approved, "correct", "approve")["memory"]["status"] == "active"
    assert "yours" not in vault.read(writer)
    vault.commit(writer)
    assert vault.read(writer)["yours"] == {
        "waiting": 1, "published_in_last_7_days": 0, "not_published_in_last_7_days": 0,
    }


def check_consolidation_outcome_reason(vault):
    from alicebot_api.vnext_derived_labels import with_derived_from
    from alicebot_api.vnext_memory_version import memory_version_snapshot

    writer = vault.key("writer")
    admin = vault.key("admin", None, "admin_agent")
    mids = [vault.commit(writer, text=f"Reviewing synthetic service note {index}")["memory"]["id"]
            for index in range(2)]
    for mid in mids:
        vault.decide(admin, mid, "correct", "approve")
    with vault.store() as store:
        members = [store.get_memory(mid) for mid in mids]
        candidate = store.create_memory({
            "memory_key": "fixture.lesson-consolidation", "status": "candidate", "domain": "project",
            "memory_type": "procedure", "sensitivity": "private", "project_scope": ["alpha"],
            "canonical_text": "A consolidation proposal for two synthetic service notes.",
            "metadata_json": with_derived_from({
                "candidate_kind": "memory_consolidation",
                "project_scope": ["alpha"],
                "consolidation": {
                    "proposal_kind": "dedup", "cluster_member_ids": mids, "proposed_supersede": mids,
                    "member_snapshots": [memory_version_snapshot(member) for member in members],
                },
            }, {"memories": members}),
        })["id"]
    accepted = vault.decide(admin, str(candidate), "manage", "accept_consolidation", reason="Accept these notes.")
    assert accepted["status"] == "accepted"
    assert set(accepted["superseded_member_ids"]) == set(mids)
    with vault.store() as store:
        for mid in mids:
            assert store.get_memory(mid)["status"] == "superseded"
            decision = next(row for row in store.list_revisions(mid)
                            if row["action"] == "agentic_memory_consolidation_supersede")
            assert f"{candidate}" in decision["reason"]
    outcome = vault.read(writer)["yours"]
    assert outcome["published_in_last_7_days"] == 0
    assert outcome["not_published_in_last_7_days"] == 2
    assert {detail["id"]: detail["reason"] for detail in outcome["not_published"]} == dict.fromkeys(mids)
    assert str(candidate) not in json.dumps(outcome)


def check_default_and_withheld_reasons(vault, cases):
    writer = vault.key("writer")
    admin = vault.key("admin", None, "admin_agent")
    for door, verb, action, default in cases:
        for supplied in (None, default):
            mid = vault.commit(writer, text=f"Reviewing a {door} {verb} example")["memory"]["id"]
            args = {} if supplied is None else {"reason": supplied}
            vault.decide(admin, mid, door, verb, **args)
            with vault.store() as store:
                decision = next(row for row in store.list_revisions(mid) if row["action"] == action)
                assert decision["reason"] == default
            detail = vault.read(writer)["yours"]["not_published"][0]
            assert detail["id"] == mid and detail["reason"] is None
            assert datetime.fromisoformat(detail["at"]) == _timestamp(decision["created_at"])

    from alicebot_api import vnext_memory_commit

    inline_default = "Inline memory confirmation reject."
    expiry_default = "Inline memory confirmation expired before it was acted on."
    for mode in (("MCP", "HTTP", "expired") if vault.http else ("MCP", "expired")):
        pending = vault.commit(admin, text=f"Reviewing a pending {mode} example", confidence=0.7, project_scope=["alpha"])
        mid, ticket = pending["memory"]["id"], pending["confirmation_id"]
        if mode == "HTTP":
            status, result, _ = vault.http("POST", "/v0/vnext/memories/confirm", key=admin, payload={
                "confirmation_id": ticket, "action": "reject", "rationale": inline_default,
            })
            assert status == 200, result
        else:
            with vault.monkeypatch.context() as clock:
                if mode == "expired":
                    later = datetime.now(UTC) + timedelta(days=2)
                    clock.setattr(vnext_memory_commit, "_utc_now", lambda: later)
                vault.call(admin, "alice_memory_commit", {"confirmation_id": ticket, "confirmation_action": "reject"})
        with vault.store() as store:
            action = "agentic_memory_confirmation_expired" if mode == "expired" else "agentic_memory_confirm_reject"
            decision = next(row for row in store.list_revisions(mid) if row["action"] == action)
            assert decision["reason"] == (expiry_default if mode == "expired" else inline_default)
        detail = vault.read(admin)["yours"]["not_published"][0]
        assert (detail["id"], detail["reason"], _timestamp(detail["at"])) == (mid, None, _timestamp(decision["created_at"]))

    synthetic_token = "gh" + "p_" + "0123456789abcdefghijklmnopqrstuvwxyz"
    mid = vault.commit(writer, text="Reviewing a withheld reason")["memory"]["id"]
    vault.decide(admin, mid, "correct", "reject", reason=synthetic_token)
    with vault.store() as store:
        assert next(row for row in store.list_revisions(mid) if row["action"] == "memory_correct_reject")[
            "reason"
        ] == RATIONALE_WITHHELD_PLACEHOLDER
        now = datetime.now(UTC)
        legacy = seed_record(vault, store, writer="writer", status="rejected", updated_at=now,
                             decision_at=now, action="memory_correct_reject", kind="rejected", reason=synthetic_token)
    details = vault.read(writer)["yours"]["not_published"]
    assert [(row["id"], row["reason"]) for row in details[:2]] == [(legacy, None), (mid, None)]


def _timestamp(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def check_outcome_cutoff(vault):
    from alicebot_api.mcp import lessons

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=7)
    vault.monkeypatch.setattr(lessons, "_utc_now", lambda: now)
    writer = vault.key("writer")
    included = []
    with vault.store() as store:
        seed_record(vault, store, writer="writer", status="candidate", updated_at=cutoff - timedelta(days=20))
        for rejected_at, updated_at in (
            (cutoff + timedelta(hours=1), cutoff + timedelta(hours=1)),
            (cutoff + timedelta(milliseconds=1), cutoff - timedelta(milliseconds=1)),
            (cutoff - timedelta(hours=1), cutoff - timedelta(hours=1)),
            (cutoff - timedelta(milliseconds=1), cutoff + timedelta(milliseconds=1)),
        ):
            mid = seed_record(vault, store, writer="writer", status="rejected", updated_at=updated_at,
                              decision_at=rejected_at, action="memory_correct_reject", kind="rejected",
                              reason="The example is no longer relevant.")
            if rejected_at > cutoff:
                included.append((mid, rejected_at))
        for published_at in (cutoff + timedelta(hours=1), cutoff - timedelta(hours=1)):
            seed_record(vault, store, writer="writer", status="active", updated_at=published_at,
                        decision_at=published_at, action="memory_correct_approve", kind="promoted")
    block = vault.read(writer)["yours"]
    assert (block["waiting"], block["published_in_last_7_days"], block["not_published_in_last_7_days"]) == (1, 1, 2)
    assert [(row["id"], _timestamp(row["at"])) for row in block["not_published"]] == included


def check_read_bookkeeping(vault):
    writer = vault.key("writer")
    vault.commit(writer)
    for key, name in ((vault.key("reader"), "reader"), (writer, "writer"), (vault.key("writer", "beta"), "writer")):
        before = vault.snapshot()
        answer = vault.read(key)
        after = vault.snapshot()
        assert after.keys() == before.keys()
        for table in after.keys() - {"agent_api_keys", "agent_identities", "event_log"}:
            assert after[table] == before[table], table

        def without_stamp(row, stamp):
            return {k: v for k, v in row.items() if k != stamp}
        original_keys = {row["key_hash"]: row for row in before["agent_api_keys"]}
        updated_keys = {row["key_hash"]: row for row in after["agent_api_keys"]}
        used = hash_agent_key(key)
        assert updated_keys.keys() == original_keys.keys()
        assert updated_keys[used]["last_used_at"] != original_keys[used]["last_used_at"]
        for hashed, row in updated_keys.items():
            if hashed == used:
                assert without_stamp(row, "last_used_at") == without_stamp(original_keys[hashed], "last_used_at")
            else:
                assert row == original_keys[hashed]
        original_ids = {row["agent_id"]: row for row in before["agent_identities"]}
        updated_ids = {row["agent_id"]: row for row in after["agent_identities"]}
        assert updated_ids.keys() == original_ids.keys() | {name}
        assert {k: v for k, v in updated_ids.items() if k != name} == {k: v for k, v in original_ids.items() if k != name}
        if name in original_ids:
            prior = without_stamp(original_ids[name], "updated_at")
            current = without_stamp(updated_ids[name], "updated_at")
            project = original_keys[used]["project_scope"]
            scope = updated_ids[name]["project_scope_json"]
            assert (json.loads(scope) if isinstance(scope, str) else scope) == [project]
            prior["project_scope_json"] = scope
            assert current == prior
            assert updated_ids[name]["updated_at"] != original_ids[name]["updated_at"]

        old_events = {row["id"]: row for row in before["event_log"]}
        new_events = {row["id"]: row for row in after["event_log"]}
        assert {event: new_events[event] for event in old_events} == old_events
        added = {row["event_type"]: row for event, row in new_events.items() if event not in old_events}
        assert len(new_events) - len(old_events) == len(added) == 3
        shapes = {
            "agent.identity_upserted": {"agent_id", "operation"},
            "agent.lessons_requested": {"limit", "paths_given", "returned"},
            "policy.decision": {"agent_identity", "policy_decision"},
        }
        assert added.keys() == shapes.keys()
        for name, shape in shapes.items():
            payload = added[name]["payload_json"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            assert payload.keys() == shape
            if name == "agent.lessons_requested":
                assert payload == {"limit": 5, "paths_given": 1, "returned": answer["count"]}


def seed_timing_case(vault, store, size, case, *, prefix):
    now = datetime.now(UTC)
    name = f"{prefix}-writer"
    age = timedelta(days=8 if case == "own old rejects" else 1)
    settings = {
        "peer waiting": {"writer": f"{prefix}-peer"},
        "own waiting": {"writer": name},
        "same writer outside project": {"writer": name, "project": "beta"},
        "own recent rejects": {"writer": name, "status": "rejected", "action": "memory_correct_reject", "kind": "rejected"},
        "own old rejects": {"writer": name, "status": "rejected", "action": "memory_correct_reject", "kind": "rejected"},
        "own recent publications": {"writer": name, "status": "active", "action": "memory_correct_approve", "kind": "promoted"},
    }[case]
    for _ in range(size):
        seed_record(vault, store, updated_at=now - age, decision_at=now - age, **settings)


def check_outcome_growth(vault, case):
    from alicebot_api.mcp import lessons

    seed_time = datetime.now(UTC) - timedelta(days=1)
    setups = {}
    for size, current in ((200, vault), (2000, vault.sibling())):
        key = current.key("timing-writer")
        if case == "same writer outside project":
            current.key("timing-writer", "beta")
        if case == "peer waiting":
            current.key("timing-peer")
        with current.store() as store:
            seed_record(current, store, writer="publisher", status="active", updated_at=seed_time,
                        decision_at=seed_time, action="memory_correct_approve", kind="promoted")
            seed_timing_case(current, store, size, case, prefix="timing")
        setups[size] = current, key
    expected = {
        "own waiting": "waiting", "own recent rejects": "not_published_in_last_7_days",
        "own recent publications": "published_in_last_7_days",
    }.get(case)
    samples = {200: [], 2000: []}
    outcome_seconds = 0.0
    original = lessons._lesson_outcomes

    def measured_outcomes(*args, **kwargs):
        nonlocal outcome_seconds
        start = process_time()
        result = original(*args, **kwargs)
        outcome_seconds = process_time() - start
        return result

    vault.monkeypatch.setattr(lessons, "_lesson_outcomes", measured_outcomes)

    def read(size):
        current, key = setups[size]
        start = process_time()
        answer = current.read(key)
        samples[size].append((process_time() - start, outcome_seconds))
        if expected is not None:
            assert answer["yours"][expected] == size
        else:
            assert "yours" not in answer

    assert_cost_grows_linearly(lambda: read(200), lambda: read(2000), size_ratio=10, what=case)
    if case == "own old rejects":
        small = min(block for _total, block in samples[200])
        large = min(block for _total, block in samples[2000])
        assert large < max(small * 2.25, 0.002), (small, large)
    for size, values in samples.items():
        print(f"OUTCOME_TIMING {case} {size} matched={size if expected else 0} "
              f"cpu_min={min(value[0] for value in values):.6f} block_min={min(value[1] for value in values):.6f}")
