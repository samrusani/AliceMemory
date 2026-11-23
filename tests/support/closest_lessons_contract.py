"""Closest-lesson contract shared by the two real stores."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
import json

import pytest

from alicebot_api.mcp import lessons
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_agent_keys import hash_agent_key
from alicebot_api.vnext_embeddings import EMBEDDING_SIGNATURE_VERSION, memory_embedding_content_sha256
from tests.support.lesson_outcomes_contract import OutcomeVault, seed_record
from tests.unit.test_identity_claim_codes import wire
from tests.unit.test_vnext_retrieval import StubEmbeddingProvider


def make_vault(context: MCPRuntimeContext, store, monkeypatch, tmp_path, *, vector, **kwargs) -> OutcomeVault:
    from alicebot_api import vnext_retrieval

    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    for name in ("ALICE_EMBEDDINGS_BASE_URL", "ALICE_EMBEDDINGS_API_KEY", "ALICE_EMBEDDINGS_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(vnext_retrieval, "get_embedding_provider", lambda: StubEmbeddingProvider() if vector else None)
    return OutcomeVault(context, store, monkeypatch, **kwargs)


def draft(task="thermometer sensor calibration", *, advice="Keep the correction.", paths=()):
    return {"applies_when": task, "what_happened": "The measurement drifted.", "do_instead": advice, "paths": list(paths)}


def commit(vault, key, block, **kwargs):
    return vault.call(key, "alice_memory_commit", {"lesson": block, "sensitivity": "private", **kwargs})


def read(vault, key, block, **claims):
    return vault.call(key, "alice_lessons", {"task": block["applies_when"], "paths": block["paths"], "limit": 3, **claims})["lessons"]


def detail(vault, key, mid, **claims):
    return vault.call(key, "alice_memory_review", {"review_item_id": mid, **claims})["review"]


def review_list(vault, key, **options):
    return vault.call(key, "alice_memory_review", {"status": "pending_review", "limit": 40, **options})["items"]


def check_matches(similar, published):
    assert isinstance(similar, list)
    assert len(similar) <= 3
    assert [entry["id"] for entry in similar] == [entry["id"] for entry in published]
    for match, lesson in zip(similar, published, strict=True):
        assert set(match) == {"id", "applies_when", "do_instead", "writer", "published", "why"}
        assert match["writer"] == lesson["writer"]
        assert match["published"] == lesson["published"]
        for field, limit in (("applies_when", 160), ("do_instead", 300)):
            text = lesson[field]
            assert match[field] == (text if len(text) <= limit else text[:limit - 1] + "…")
        assert match["why"]
        assert match["why"] in (
            ["all_words"], ["some_words"], ["vector"], ["paths"],
            ["all_words", "paths"], ["some_words", "paths"], ["vector", "paths"],
        )


def fail_review(vault, key, mid):
    with vault.monkeypatch.context() as patch:
        patch.setenv("ALICE_AGENT_API_KEY", key)
        failed, result = wire(vault.context, "alice_memory_review", {"review_item_id": mid})
    assert failed and result["error"]["code"] == "not_found"


def add_vector(vault, mid):
    with vault.store() as store:
        row = store.get_memory(mid)
        store.update_memory_embedding(
            memory_id=mid, vector=[0.1] * 1536, provider="stub", model="stub-embedding",
            content_sha256=memory_embedding_content_sha256(row), signature_version=EMBEDDING_SIGNATURE_VERSION,
        )


def check_storage_delta(before, after, *, key, agent, memory_id=None, allow_scope_update=False):
    assert after.keys() == before.keys()
    bookkeeping = {"agent_api_keys", "agent_identities", "event_log"}
    write_tables = {"memories", "memory_revisions"} if memory_id is not None else set()
    # SQLite maintains these search tables as part of the ordinary memory insert.
    fts_tables = {
        "memories_fts", "memories_fts_data", "memories_fts_idx", "memories_fts_docsize",
    } if memory_id is not None else set()
    for table in before.keys() - bookkeeping - write_tables - fts_tables:
        assert before[table] == after[table], table

    prior_keys = {row["key_hash"]: row for row in before["agent_api_keys"]}
    keys = {row["key_hash"]: row for row in after["agent_api_keys"]}
    used = hash_agent_key(key)
    assert keys.keys() == prior_keys.keys()
    assert all(row == prior_keys[hashed] for hashed, row in keys.items() if hashed != used)
    assert keys[used] == {**prior_keys[used], "last_used_at": keys[used]["last_used_at"]}
    assert keys[used]["last_used_at"] != prior_keys[used]["last_used_at"]

    prior_agents = {row["agent_id"]: row for row in before["agent_identities"]}
    agents = {row["agent_id"]: row for row in after["agent_identities"]}
    assert agents.keys() == prior_agents.keys() | {agent}
    assert all(row == prior_agents[identity] for identity, row in agents.items() if identity != agent)
    if agent in prior_agents:
        allowed = {"updated_at", "project_scope_json"} if allow_scope_update else {"updated_at"}
        assert agents[agent] == {**prior_agents[agent], **{field: agents[agent][field] for field in allowed}}
        assert agents[agent]["updated_at"] != prior_agents[agent]["updated_at"]

    expected_events = Counter({"agent.identity_upserted": 1, "policy.decision": 1})
    for table in write_tables | {"event_log"}:
        prior = {str(row["id"]): row for row in before[table]}
        rows = {str(row["id"]): row for row in after[table]}
        assert {identifier: rows[identifier] for identifier in prior} == prior, table
        added = [row for identifier, row in rows.items() if identifier not in prior]
        assert "similar" not in json.dumps(added, default=str)
        if table == "memories":
            assert len(added) == 1 and str(added[0]["id"]) == memory_id
            assert added[0]["status"] == "candidate"
        elif table == "memory_revisions":
            assert len(added) == 1 and str(added[0]["memory_id"]) == memory_id
        else:
            if memory_id is not None:
                expected_events.update(("agent.memory_review_required", "review.item_created",
                                        "memory.created", "memory_revision.created"))
            assert Counter(row["event_type"] for row in added) == expected_events


class ClosestLessonsContract:
    def test_commit_and_review_explain_matches(self, closest_vault):
        """Guard: attach closest matches to the accepted MCP commit and both review answers."""
        v = closest_vault
        admin, author, writer = v.key("decider", None, "admin_agent"), v.key("author"), v.key("writer")
        block = draft("thermometer sensor calibration " + "measurement " * 15, advice="Keep the correction. " * 20)
        first = commit(v, author, block)
        v.decide(admin, first["memory"]["id"], "correct", "approve")
        for advice in ("Discard the correction.", block["do_instead"]):
            proposal = {**block, "do_instead": advice}
            expected = read(v, writer, proposal)
            answer = commit(v, writer, proposal)
            assert answer["status"] == "review_required"
            assert answer["receipt"] == "waiting in review."
            check_matches(answer["similar"], expected)
            assert answer["similar"][0]["writer"]["id"] == "author"
            assert answer["similar"][0]["why"] == ["all_words"]
            mid = answer["memory"]["id"]
            item = next(row for row in review_list(v, admin) if row["id"] == mid)
            assert item["lesson"] == answer["lesson"] | {"paths": []}
            check_matches(item["similar"], read(v, admin, proposal))
            shown = detail(v, admin, mid)
            assert shown["similar"] == item["similar"] == detail(v, writer, mid)["similar"] == answer["similar"]
            assert shown["memory"]["status"] == "candidate"

    def test_waiting_rows_and_nonlesson_answers(self, closest_vault):
        """Guard: compute only for waiting lessons in the visible review results."""
        v = closest_vault
        admin, author, writer = v.key("decider", None, "admin_agent"), v.key("author"), v.key("writer")
        original = commit(v, author, draft())
        other = commit(v, writer, draft())
        assert original["similar"] == other["similar"] == read(v, writer, draft()) == []
        items = review_list(v, admin)
        assert len(items) == 2 and all(item["similar"] == [] for item in items)
        own = review_list(v, writer)
        assert [item["id"] for item in own] == [other["memory"]["id"]]
        fail_review(v, writer, original["memory"]["id"])

        with v.monkeypatch.context() as patch:
            def unexpected(*args, **kwargs):
                pytest.fail("the matcher ran for an ordinary or excluded row")
            patch.setattr(lessons, "_matched_lesson_entries", unexpected)
            fail_review(v, writer, original["memory"]["id"])
            ordinary = v.call(writer, "alice_memory_commit", {"title": "Plain note", "canonical_text": "Store the reading."})
            assert "similar" not in ordinary
            plain = detail(v, writer, ordinary["memory"]["id"])
            assert set(plain) == {"memory", "provenance_links", "revisions"}

        v.decide(admin, original["memory"]["id"], "correct", "approve")
        item = next(r for r in review_list(v, admin, status="active") if r["id"] == original["memory"]["id"])
        assert set(item) == {
            "canonical_text", "confidence", "created_at", "domain", "id", "memory_type",
            "provenance_count", "status", "title", "writer",
        }
        assert set(detail(v, admin, original["memory"]["id"])) == {"memory", "provenance_links", "revisions"}

    def test_partial_words_and_self_exclusion_on_replay(self, closest_vault):
        """Guard: exclude the written lesson before the strict word pass chooses whether to retry."""
        v = closest_vault
        admin = v.key("publisher", None, "admin_agent")
        for words in ("thermometer alignment", "actuator temperature", "actuator heat"):
            commit(v, admin, draft(words))
        query = draft("thermometer actuator")
        expected = read(v, admin, query)
        assert len(expected) == 3
        args = {"idempotency_key": "instrument-alignment"}
        first = commit(v, admin, query, **args)
        replay = commit(v, admin, query, **args)
        assert replay["idempotent_replay"] is True
        for result in (first, replay):
            check_matches(result["similar"], expected)
            assert all(x["why"] == ["some_words"] for x in result["similar"])
            assert result["memory"]["id"] not in [x["id"] for x in result["similar"]]

    def test_immediate_match_precedes_deferred_embedding(self, closest_vault):
        """Guard: the new draft's embedding enters the index after matching."""
        from alicebot_api.mcp import memories

        v = closest_vault
        admin = v.key("publisher", None, "admin_agent")
        commit(v, admin, draft())
        match = memories._similar_for_lesson
        persist = memories._persist_vnext_deferred_embedding_inputs
        phases = []

        def observe_match(context, identity, lesson, mid):
            with v.store() as store:
                assert store.get_memory(mid)["status"] == "active"
            phases.append("match")
            return match(context, identity, lesson, mid)

        def observe_embedding(context, inputs, **actor):
            assert inputs
            phases.append("embed")
            return persist(context, inputs, **actor)

        with v.monkeypatch.context() as patch:
            patch.setattr(memories, "_similar_for_lesson", observe_match)
            patch.setattr(memories, "_persist_vnext_deferred_embedding_inputs", observe_embedding)
            answer = commit(v, admin, draft())
        assert len(answer["similar"]) == 1
        assert phases == ["match", "embed"]

    def test_paths_and_vector_reasons(self, closest_vault, vectors_on):
        """Guard: use the matched word pass first, and then add paths in the matcher's order."""
        v = closest_vault
        admin, writer = v.key("publisher", None, "admin_agent"), v.key("writer")
        word_path = commit(v, admin, draft("thermometer calibration", paths=["fixtures/*.py"]),
                           project_scope=["alpha"])["memory"]["id"]
        path_only = commit(v, admin, draft("optical alignment", paths=["fixtures/*.py"]),
                           project_scope=["alpha"])["memory"]["id"]
        vector_only = commit(v, admin, draft("coupler vibration"), project_scope=["alpha"])["memory"]["id"]
        if vectors_on:
            for mid in (word_path, vector_only):
                add_vector(v, mid)
        query = draft("thermometer calibration", paths=["fixtures/sensor.py"])
        expected = read(v, writer, query)
        answer = commit(v, writer, query)
        check_matches(answer["similar"], expected)
        assert [(x["id"], x["why"]) for x in answer["similar"]] == [
            (word_path, ["all_words", "paths"]),
            (path_only, ["paths"]),
            *([(vector_only, ["vector"])] if vectors_on else []),
        ]

    def test_confirmation_and_replay_after_removal(self, closest_vault):
        """Guard: confirm on the existing response path and compute replays from current publications."""
        v = closest_vault
        admin, writer = v.key("publisher", None, "admin_agent"), v.key("writer")
        query = draft()
        for _ in range(3):
            commit(v, admin, query, project_scope=["alpha"])
        before = read(v, admin, query)
        held = commit(v, admin, query, confidence=0.7, idempotency_key="held-calibration")
        assert held["status"] == "confirmation_required"
        check_matches(held["similar"], before)
        pending = detail(v, admin, held["memory"]["id"])
        assert pending["similar"] == held["similar"]
        finish = v.call(admin, "alice_memory_commit",
                        {"confirmation_id": held["confirmation_id"], "confirmation_action": "confirm"})
        assert finish["status"] == "committed"
        assert set(finish) == {"confirmation_id", "lesson", "memory", "receipt", "status", "write_mode"}
        replay = commit(v, admin, query, confidence=0.7, idempotency_key="held-calibration")
        check_matches(replay["similar"], before)

        first_expected = read(v, writer, query)
        proposal = commit(v, writer, query, idempotency_key="waiting-calibration")
        check_matches(proposal["similar"], first_expected)
        removed = proposal["similar"][0]["id"]
        v.decide(admin, removed, "manage", "redact", reason="Remove the obsolete reading.")
        after = read(v, writer, query)
        result = commit(v, writer, query, idempotency_key="waiting-calibration")
        assert result["idempotent_replay"] is True
        check_matches(result["similar"], after)
        assert removed not in [x["id"] for x in result["similar"]]

    def test_limits_and_rank_prefix(self, closest_vault):
        """Read the same admitted candidates at both doors, even past a long prefix."""
        v = closest_vault
        admin, writer = v.key("publisher", None, "admin_agent"), v.key("writer")
        query = draft()
        assert commit(v, writer, query)["similar"] == []
        other = commit(v, admin, query, project_scope=["beta"])["memory"]["id"]
        high = commit(v, admin, query, project_scope=["alpha"], sensitivity="confidential")
        v.call(admin, "alice_memory_commit",
               {"confirmation_id": high["confirmation_id"], "confirmation_action": "confirm"})
        denied = commit(v, writer, query)
        assert denied["similar"] == read(v, writer, query) == []
        visible = {item["id"] for item in detail(v, admin, denied["memory"]["id"])["similar"]}
        assert visible == {other, high["memory"]["id"]}
        shown = commit(v, admin, query, project_scope=["alpha"])["memory"]["id"]
        with v.store() as store:
            for _ in range(300):
                seed_record(v, store, writer="publisher", project="beta", status="active",
                            decision_at=datetime.now(UTC), action="agentic_memory_commit", kind="created")
        prefix_query = draft("reviewing a synthetic service note thermometer")
        published = read(v, writer, prefix_query)
        assert [x["id"] for x in published] == [shown]
        check_matches(commit(v, writer, prefix_query)["similar"], published)

    @pytest.mark.parametrize("by_id", [False, True], ids=["review-list", "review-detail"])
    @pytest.mark.parametrize(
        ("caller_profile", "visible"), [("memory_proposal_agent", 2), ("admin_agent", 3)], ids=["writer", "bound-admin"],
    )
    def test_review_matches_keep_caller_limits(self, closest_vault, vectors_on, by_id, caller_profile, visible):
        """Guard: return the caller's admitted matches for a waiting lesson on either review path."""
        v = closest_vault
        publisher = v.key("publisher", None, "admin_agent")
        writer = v.key("writer")
        caller = v.key("bound-decider", "alpha", caller_profile) if caller_profile == "admin_agent" else writer
        query = draft(paths=["fixtures/calibration.py"])
        publications = (
            ("alpha", "private", "lead procedure"),
            ("alpha", "private", "followup procedure"),
            ("alpha", "confidential", "restricted procedure"),
            ("beta", "private", "remote procedure"),
            ("beta", "confidential", "remote restricted procedure"),
        )
        published = []
        for project, sensitivity, marker in publications:
            block = {
                **query,
                "applies_when": f"{query['applies_when']} {marker}",
                "what_happened": f"Observed {marker} drift.",
                "do_instead": f"Recheck the {marker} sample.",
            }
            answer = commit(v, publisher, block, project_scope=[project], sensitivity=sensitivity)
            if sensitivity == "confidential":
                answer = v.call(publisher, "alice_memory_commit", {
                    "confirmation_id": answer["confirmation_id"], "confirmation_action": "confirm",
                })
            assert answer["status"] == "committed"
            mid = answer["memory"]["id"]
            if vectors_on:
                add_vector(v, mid)
            published.append((mid, block))

        waiting = commit(v, writer, query)
        assert waiting["status"] == "review_required"
        waiting_id = waiting["memory"]["id"]
        args = {"review_item_id": waiting_id} if by_id else {"status": "pending_review"}
        lookup = read(v, caller, query)
        assert {entry["id"] for entry in lookup} == {mid for mid, _ in published[:visible]}
        answer = v.call(caller, "alice_memory_review", args)
        receipt = json.dumps(answer, ensure_ascii=False)
        # The review response can blank an id. Check every rendered field's text too.
        for _, block in published[visible:]:
            for field in ("applies_when", "what_happened", "do_instead"):
                assert block[field] not in receipt, field
        if by_id:
            item = answer["review"]
            assert item["memory"]["id"] == waiting_id
        else:
            item = next(item for item in answer["items"] if item["id"] == waiting_id)
        check_matches(item["similar"], lookup)

    def test_review_cap(self, closest_vault):
        """Guard: compute matches for ten waiting items and use null on later items."""
        v = closest_vault
        admin, writer = v.key("publisher", None, "admin_agent"), v.key("writer")
        query = draft(paths=["fixtures/cap.py"])
        commit(v, admin, query, project_scope=["alpha"])
        for _ in range(12):
            commit(v, writer, query)
        expected = read(v, admin, query)
        items = review_list(v, admin)
        assert len(items) == 12
        for index, item in enumerate(items):
            assert item["lesson"] == query
            if index < 10:
                check_matches(item["similar"], expected)
            else:
                assert item["similar"] is None
            check_matches(detail(v, admin, item["id"])["similar"], expected)

    def test_keyless_and_special_characters(self, closest_vault):
        """Guard: keep the keyless caller's policy and pass punctuation to the same matcher."""
        v = closest_vault
        query = draft('thermometer [tag:]:* AND OR NOT "', paths=["fixtures/quoted.py"])
        commit(v, None, query)
        for claims in ({}, {"agent_id": "declared", "agent_type": "coding_agent", "permission_profile": "memory_proposal_agent"}):
            expected = read(v, None, query, **claims)
            answer = commit(v, None, query, **claims)
            assert answer["status"] in {"committed", "review_required"}
            check_matches(answer["similar"], expected)
            if answer["status"] == "review_required":
                mid = answer["memory"]["id"]
                for viewer in ({}, claims):
                    visible = read(v, None, query, **viewer)
                    item = next(item for item in review_list(v, None, **viewer) if item["id"] == mid)
                    check_matches(item["similar"], visible)
                    check_matches(detail(v, None, mid, **viewer)["similar"], visible)

    def test_failed_pass_leaves_write_and_returns_null(self, closest_vault):
        """Guard: a failing match leaves the MCP write committed and signals null."""
        v = closest_vault
        writer = v.key("writer")

        def refuse(*args, **kwargs):
            raise RuntimeError("synthetic match failure")

        with v.monkeypatch.context() as patch:
            patch.setattr(lessons, "_matched_lesson_entries", refuse)
            answer = commit(v, writer, draft())
            assert answer["status"] == "review_required" and answer["similar"] is None
            mid = answer["memory"]["id"]
            shown = detail(v, writer, mid)
            assert shown["similar"] is None and shown["memory"]["status"] == "candidate"
        with v.store() as store:
            assert store.get_memory(mid)["status"] == "candidate"
            assert len(store.list_revisions(mid)) == 1
        read_only = v.key("reader", "alpha", "read_only_agent")
        with v.monkeypatch.context() as patch:
            def unexpected(*args, **kwargs):
                pytest.fail("a rejected commit ran the matcher")
            patch.setattr(lessons, "_matched_lesson_entries", unexpected)
            for rejection in (commit(v, read_only, draft()), commit(v, writer, draft(), sensitivity="confidential")):
                assert rejection["status"] == "rejected" and "similar" not in rejection

    def test_extra_read_preserves_the_explicit_storage_contract(self, closest_vault):
        """Guard: the similarity pass adds neither identity writes nor events."""
        v = closest_vault
        admin, writer = v.key("decider", None, "admin_agent"), v.key("writer")
        query = draft()
        commit(v, admin, query, project_scope=["alpha"])
        before = v.snapshot()
        result = commit(v, writer, query)
        mid = result["memory"]["id"]
        assert len(result["similar"]) == 1
        check_storage_delta(before, v.snapshot(), key=writer, agent="writer", memory_id=mid)
        for may_update_scope, call in ((True, lambda: review_list(v, admin)[0]), (False, lambda: detail(v, admin, mid))):
            before = v.snapshot()
            assert len(call()["similar"]) == 1
            check_storage_delta(before, v.snapshot(), key=admin, agent="decider", allow_scope_update=may_update_scope)

    def test_uses_only_current_publications(self, closest_vault):
        """Ignore retired, expired, pending, rejected and private-only matches."""
        v = closest_vault
        admin, writer = v.key("decider", None, "admin_agent"), v.key("writer")
        now = datetime.now(UTC)
        with v.store() as store:
            for status in ("candidate", "needs_review", "rejected", "archived", "private_only"):
                seed_record(v, store, writer="decider", status=status, decision_at=now,
                            action="agentic_memory_commit", kind="created")
            seed_record(v, store, writer="decider", status="active", valid_to=now - timedelta(days=1),
                        decision_at=now, action="agentic_memory_commit", kind="created")
        query = draft("reviewing a synthetic service note")
        assert commit(v, writer, query)["similar"] == read(v, writer, query) == []
        active = commit(v, admin, query, project_scope=["alpha"])["memory"]["id"]
        answer = commit(v, writer, query)
        check_matches(answer["similar"], read(v, writer, query))
        assert [item["id"] for item in answer["similar"]] == [active]
