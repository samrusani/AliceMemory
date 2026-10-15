"""Recorded decision chains, the caller's limits, and the 128 cap."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from alicebot_api.recall_framing import quote_session_brief_text
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_what_changed import NOTICE, NOT_FOUND, SEARCH, what_changed

TODAY = "2026-10-14"


def _store() -> SQLiteVNextStore:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, f"{user_id}@example.com", "Test User")
    return SQLiteVNextStore(conn, user_id)


def _memory(store: SQLiteVNextStore, **overrides: object) -> dict[str, object]:
    created_at = overrides.pop("created_at", None)
    memory: dict[str, object] = {
        "memory_key": f"decision.{uuid4()}",
        "value": {"text": "a decision"},
        "status": "active",
        "memory_type": "decision",
        "title": "decision",
        "canonical_text": "a recorded decision",
        "domain": "project",
        "sensitivity": "public",
        "confirmation_status": "confirmed",
    }
    memory.update(overrides)
    row = store.create_memory(memory)
    if isinstance(created_at, str):
        store.conn.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            (created_at, row["id"]),
        )
        row = store.get_memory(str(row["id"]))
    return row


def _ask(store: SQLiteVNextStore, topic: str, identity: AgentIdentity | None = None) -> dict[str, object]:
    return what_changed(store, user_id=store.user_id, identity=identity, topic=topic, today=TODAY)


def _ids(answer: dict[str, object]) -> list[str]:
    chains = answer["chains"]
    assert isinstance(chains, list)
    found: list[str] = []
    for chain in chains:
        assert isinstance(chain, dict)
        history = chain["history"]
        assert isinstance(history, list)
        found.extend(str(step["id"]) for step in history if isinstance(step, dict))
    return found


def test_a_chain_comes_back_oldest_first_with_its_source_and_a_stored_quote() -> None:
    store = _store()
    source = store.create_source(
        {
            "source_type": "document",
            "title": "Keel sensor kit list price cut",
            "content_hash": f"sha256:{uuid4()}",
            "domain": "financial",
            "sensitivity": "private",
            "source_created_at": "2025-11-18T00:00:00Z",
        }
    )
    older = _memory(
        store,
        title="Keel sensor kit price",
        canonical_text="the list price was 640 ledger units",
        domain="financial",
        sensitivity="private",
        valid_from="2025-06-02T00:00:00Z",
        created_at="2025-06-02T00:00:00Z",
        created_by_agent_id="showcase-sales",
    )
    newer = _memory(
        store,
        title="Keel sensor kit price",
        canonical_text="the list price was cut to 520 ledger units",
        domain="financial",
        sensitivity="private",
        valid_from="2025-11-18T00:00:00Z",
        created_at="2025-11-18T00:00:00Z",
        created_by_agent_id="showcase-sales",
        supersedes=str(older["id"]),
        metadata_json={
            "agentic_memory": {
                "source_refs": [
                    {
                        "source_id": str(source["id"]),
                        "quote": "the June price was too high for the renewal",
                    }
                ]
            }
        },
    )
    store.update_memory(memory_id=str(older["id"]), patch={"superseded_by": str(newer["id"])})

    answer = _ask(store, "Keel sensor kit price", AgentIdentity(agent_id="showcase-sales", permission_profile="trusted_local_agent"))
    assert answer["notice"] == NOTICE
    chains = answer["chains"]
    assert isinstance(chains, list) and len(chains) == 1
    chain = chains[0]
    assert isinstance(chain, dict)
    history = chain["history"]
    assert isinstance(history, list)
    assert [step["id"] for step in history] == [str(older["id"]), str(newer["id"])]
    assert chain["current"]["id"] == str(newer["id"])
    assert chain["current"]["replaced"] == str(older["id"])
    assert chain["current"]["reason"] == {
        "text": quote_session_brief_text("the June price was too high for the renewal"),
        "source_id": str(source["id"]),
    }
    assert chain["current"]["sources"] == [
        {"source_id": str(source["id"]), "title": "Keel sensor kit list price cut", "date": "2025-11-18"}
    ]
    assert "the June price" not in chain["current"]["change"]
    dumped = json.dumps(answer)
    assert "count" not in dumped
    assert "gap" not in dumped


def test_the_read_takes_the_label_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store()
    calls: list[bool] = []

    def lock(*, exclusive: bool = False) -> None:
        del exclusive
        calls.append(True)

    monkeypatch.setattr(store, "lock_label_writes", lock)
    _memory(store, title="lantern policy", canonical_text="the lantern stays lit")
    calls.clear()
    _ask(store, "lantern")
    assert calls == [True]


def test_a_quote_that_names_a_hidden_input_is_not_a_reason() -> None:
    store = _store()
    public = store.create_source(
        {
            "source_type": "note",
            "title": "public price note",
            "content_hash": f"sha256:{uuid4()}",
            "domain": "project",
            "sensitivity": "public",
        }
    )
    hidden = store.create_source(
        {
            "source_type": "note",
            "title": "confidential price note",
            "content_hash": f"sha256:{uuid4()}",
            "domain": "legal",
            "sensitivity": "confidential",
        }
    )
    secret = "the hidden ledger quote"
    _memory(
        store,
        title="keel price",
        canonical_text="the list price changed",
        metadata_json={
            "agentic_memory": {
                "source_refs": [
                    {
                        "source_id": str(public["id"]),
                        "source_ids": [str(public["id"]), str(hidden["id"])],
                        "quote": secret,
                    }
                ]
            }
        },
    )
    only_ids = "a quote stored with source ids"
    _memory(
        store,
        title="keel stock",
        canonical_text="the stock count changed",
        metadata_json={
            "agentic_memory": {
                "source_refs": [{"source_ids": [str(public["id"])], "quote": only_ids}]
            }
        },
    )
    sales = AgentIdentity(agent_id="showcase-sales", permission_profile="trusted_local_agent")
    answer = _ask(store, "keel", sales)
    dumped = json.dumps(answer)
    assert secret not in dumped
    assert "confidential price note" not in dumped
    reasons = [
        step["reason"]["text"]
        for chain in answer["chains"]
        for step in chain["history"]
        if isinstance(step.get("reason"), dict)
    ]
    assert quote_session_brief_text(only_ids) in reasons


def test_a_reason_is_not_taken_from_the_note_text() -> None:
    store = _store()
    memory = _memory(
        store,
        title="Keel sensor kit price",
        canonical_text="because the June price was too high for the renewal",
        domain="financial",
        sensitivity="private",
    )
    step = _ask(store, "Keel sensor kit price")["chains"][0]["history"][0]
    assert step["id"] == str(memory["id"])
    assert step["reason"] is None
    assert step["sources"] == []


def test_uuid_links_on_a_search_row_stay_one_chain() -> None:
    """PostgreSQL full-text rows carry UUID objects. The walk still follows them.

    The oldest row is the first search hit. Dropping ``_plain_row`` leaves
    ``superseded_by`` unread, so the later steps become extra chains and the
    December step is not upcoming on the first chain.
    """

    store = _store()
    older = _memory(
        store,
        title="quillon keel price",
        canonical_text="the quillon keel price was 640 ledger units",
        valid_from="2026-01-15T00:00:00Z",
        created_at="2026-01-15T00:00:00Z",
    )
    current = _memory(
        store,
        title="quillon keel price",
        canonical_text="the quillon keel price was cut to 520 ledger units",
        valid_from="2026-06-02T00:00:00Z",
        created_at="2026-06-02T00:00:00Z",
        supersedes=str(older["id"]),
    )
    later = _memory(
        store,
        title="quillon keel price",
        canonical_text="the quillon keel price becomes 480 ledger units in December",
        valid_from="2026-12-01T00:00:00Z",
        created_at="2026-07-01T00:00:00Z",
        supersedes=str(current["id"]),
    )
    store.update_memory(memory_id=str(older["id"]), patch={"superseded_by": str(current["id"])})
    store.update_memory(memory_id=str(current["id"]), patch={"superseded_by": str(later["id"])})

    def search(**_kwargs: object) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for memory_id in (str(older["id"]), str(current["id"]), str(later["id"])):
            row = dict(store.get_memory(memory_id))
            for key in ("id", "superseded_by", "supersedes"):
                if row.get(key):
                    row[key] = UUID(str(row[key]))
            rows.append(row)
        return rows

    store.search_memories_fts = search  # type: ignore[method-assign]
    chains = _ask(store, "quillon keel")["chains"]
    assert isinstance(chains, list)
    assert len(chains) == 1
    chain = chains[0]
    assert isinstance(chain, dict)
    assert chain["current"]["id"] == str(current["id"])
    assert chain["upcoming"] == [str(later["id"])]
    assert [step["id"] for step in chain["history"]] == [
        str(older["id"]),
        str(current["id"]),
        str(later["id"]),
    ]


def test_two_unrelated_decisions_stay_two_chains() -> None:
    store = _store()
    first = _memory(store, title="Keel sensor kit price", canonical_text="keep the 2024 price", created_at="2025-01-01T00:00:00Z")
    second = _memory(store, title="Keel sensor kit warranty", canonical_text="warranty stays two years", created_at="2025-02-01T00:00:00Z")
    chains = _ask(store, "Keel sensor kit")["chains"]
    assert [chain["start"] for chain in chains] == [str(second["id"]), str(first["id"])]
    assert all(len(chain["history"]) == 1 for chain in chains)


def test_needs_review_is_history_and_a_later_date_is_upcoming() -> None:
    store = _store()
    current = _memory(
        store,
        title="lantern policy",
        canonical_text="the lantern stays lit",
        valid_from="2026-01-01T00:00:00Z",
        created_at="2026-01-01T00:00:00Z",
    )
    review = _memory(
        store,
        title="lantern policy",
        canonical_text="the lantern may be dimmed",
        status="needs_review",
        confirmation_status="confirmed",
        valid_from="2026-02-01T00:00:00Z",
        created_at="2026-02-01T00:00:00Z",
        supersedes=str(current["id"]),
    )
    later = _memory(
        store,
        title="lantern policy",
        canonical_text="the lantern switches off in March",
        valid_from="2026-11-01T00:00:00Z",
        created_at="2026-03-01T00:00:00Z",
    )
    store.update_memory(memory_id=str(current["id"]), patch={"superseded_by": str(review["id"])})
    chains = _ask(store, "lantern")["chains"]
    joined = next(chain for chain in chains if any(step["id"] == str(current["id"]) for step in chain["history"]))
    upcoming = next(chain for chain in chains if any(step["id"] == str(later["id"]) for step in chain["history"]))
    assert joined["current"]["id"] == str(current["id"])
    reviewed = next(step for step in joined["history"] if step["id"] == str(review["id"]))
    assert reviewed["review_state"] == "needs_review"
    assert upcoming["current"] is None
    assert upcoming["upcoming"] == [str(later["id"])]


def test_an_older_disagreement_does_not_clear_a_newer_settled_step() -> None:
    store = _store()
    first = _memory(store, title="lantern policy", canonical_text="lit", created_at="2026-01-01T00:00:00Z", valid_from="2026-01-01T00:00:00Z")
    second = _memory(
        store,
        title="lantern policy",
        canonical_text="dim",
        created_at="2026-02-01T00:00:00Z",
        valid_from="2026-02-01T00:00:00Z",
        supersedes=str(first["id"]),
    )
    third = _memory(
        store,
        title="lantern policy",
        canonical_text="lit again",
        created_at="2026-03-01T00:00:00Z",
        valid_from="2026-03-01T00:00:00Z",
        supersedes=str(second["id"]),
    )
    store.update_memory(memory_id=str(first["id"]), patch={"superseded_by": str(second["id"])})
    store.update_memory(memory_id=str(second["id"]), patch={"superseded_by": str(third["id"])})
    store.create_graph_edge(
        {
            "from_type": "memory",
            "from_id": str(first["id"]),
            "to_type": "memory",
            "to_id": str(second["id"]),
            "edge_type": "contradicts",
            "metadata_json": {"status": "candidate"},
        }
    )
    chain = _ask(store, "lantern")["chains"][0]
    assert chain["current"]["id"] == str(third["id"])
    assert chain["disputed"] == []
    older = next(step for step in chain["history"] if step["id"] == str(first["id"]))
    assert older["disagrees_with"] == str(second["id"])


def test_the_newest_unresolved_disagreement_clears_current() -> None:
    store = _store()
    first = _memory(store, title="lantern policy", canonical_text="lit", created_at="2026-01-01T00:00:00Z", valid_from="2026-01-01T00:00:00Z")
    second = _memory(
        store,
        title="lantern policy",
        canonical_text="dim",
        created_at="2026-02-01T00:00:00Z",
        valid_from="2026-02-01T00:00:00Z",
        supersedes=str(first["id"]),
    )
    store.update_memory(memory_id=str(first["id"]), patch={"superseded_by": str(second["id"])})
    store.create_graph_edge(
        {
            "from_type": "memory",
            "from_id": str(first["id"]),
            "to_type": "memory",
            "to_id": str(second["id"]),
            "edge_type": "contradicts",
            "metadata_json": {"status": "needs_review"},
        }
    )
    chain = _ask(store, "lantern")["chains"][0]
    assert chain["current"] is None
    assert chain["disputed"] == sorted([str(first["id"]), str(second["id"])])


def test_a_resolved_disagreement_keeps_current() -> None:
    store = _store()
    first = _memory(store, title="lantern policy", canonical_text="lit", valid_from="2026-01-01T00:00:00Z", created_at="2026-01-01T00:00:00Z")
    second = _memory(
        store,
        title="lantern policy",
        canonical_text="dim",
        valid_from="2026-02-01T00:00:00Z",
        created_at="2026-02-01T00:00:00Z",
        supersedes=str(first["id"]),
    )
    store.update_memory(memory_id=str(first["id"]), patch={"superseded_by": str(second["id"])})
    store.create_graph_edge(
        {
            "from_type": "memory",
            "from_id": str(first["id"]),
            "to_type": "memory",
            "to_id": str(second["id"]),
            "edge_type": "contradicts",
            "metadata_json": {"status": "retire"},
        }
    )
    chain = _ask(store, "lantern")["chains"][0]
    assert chain["current"]["id"] == str(second["id"])
    assert chain["disputed"] == []
    assert chain["current"]["disagrees_with"] == str(first["id"])


def test_a_cycle_and_a_chain_longer_than_64_stop() -> None:
    store = _store()
    left = _memory(store, title="cycle policy", canonical_text="left")
    right = _memory(store, title="cycle policy", canonical_text="right", supersedes=str(left["id"]))
    store.update_memory(memory_id=str(left["id"]), patch={"superseded_by": str(right["id"]), "supersedes": str(right["id"])})
    store.update_memory(memory_id=str(right["id"]), patch={"superseded_by": str(left["id"])})
    cycle = _ask(store, "cycle")["chains"][0]["history"]
    assert sorted(step["id"] for step in cycle) == sorted([str(left["id"]), str(right["id"])])
    assert len(cycle) == len({step["id"] for step in cycle})

    long_store = _store()
    rows = [
        _memory(
            long_store,
            title="lantern" if index == 0 else f"step {index}",
            canonical_text="lantern policy" if index == 0 else f"step {index}",
            created_at=f"2020-01-{(index % 28) + 1:02d}T00:00:{index:02d}Z",
            valid_from=f"2020-{(index // 28) + 1:02d}-01T00:00:00Z",
        )
        for index in range(70)
    ]
    for index, row in enumerate(rows[:-1]):
        long_store.update_memory(
            memory_id=str(row["id"]),
            patch={"superseded_by": str(rows[index + 1]["id"])},
        )
        long_store.update_memory(memory_id=str(rows[index + 1]["id"]), patch={"supersedes": str(row["id"])})
    history = _ask(long_store, "lantern")["chains"][0]["history"]
    assert len(history) == 65
    assert str(rows[-1]["id"]) not in [step["id"] for step in history]
    assert len(history) == len({step["id"] for step in history})


def test_hidden_steps_do_not_change_the_answer_below_the_cap() -> None:
    store = _store()
    visible = _memory(
        store,
        title="lantern policy",
        canonical_text="the lantern stays lit",
        domain="project",
        sensitivity="public",
        created_at="2026-01-01T00:00:00Z",
    )
    support = AgentIdentity(agent_id="showcase-support", permission_profile="memory_proposal_agent")
    before = json.dumps(_ask(store, "lantern", support), sort_keys=True)
    hidden = _memory(
        store,
        title="lantern policy",
        canonical_text="a confidential handbook change",
        domain="legal",
        sensitivity="confidential",
        created_at="2026-04-01T00:00:00Z",
        supersedes=str(visible["id"]),
    )
    store.update_memory(memory_id=str(visible["id"]), patch={"superseded_by": str(hidden["id"])})
    store.create_graph_edge(
        {
            "from_type": "memory",
            "from_id": str(visible["id"]),
            "to_type": "memory",
            "to_id": str(hidden["id"]),
            "edge_type": "contradicts",
            "metadata_json": {"status": "candidate"},
        }
    )
    after = _ask(store, "lantern", support)
    assert json.dumps(after, sort_keys=True) == before
    assert str(hidden["id"]) not in json.dumps(after)
    assert after["chains"][0]["current"]["id"] == str(visible["id"])
    assert after["chains"][0]["current"]["replaced"] is None
    assert after["chains"][0]["current"]["disagrees_with"] is None


def test_a_support_key_does_not_see_an_hr_decision() -> None:
    store = _store()
    hr = _memory(
        store,
        title="handbook leave",
        canonical_text="the handbook leave rule changed",
        domain="legal",
        sensitivity="confidential",
    )
    price = _memory(
        store,
        title="keel price draft",
        canonical_text="the keel price draft is 640",
        domain="financial",
        sensitivity="private",
    )
    support = AgentIdentity(agent_id="showcase-support", permission_profile="memory_proposal_agent")
    sales = AgentIdentity(agent_id="showcase-sales", permission_profile="trusted_local_agent")
    leadership = AgentIdentity(agent_id="showcase-leadership", permission_profile="admin_agent")
    support_ids = set(_ids(_ask(store, "handbook keel", support)))
    sales_ids = set(_ids(_ask(store, "handbook keel", sales)))
    leadership_ids = set(_ids(_ask(store, "handbook keel", leadership)))
    assert str(hr["id"]) not in support_ids
    assert str(price["id"]) not in support_ids
    assert str(price["id"]) in sales_ids
    assert str(hr["id"]) not in sales_ids
    assert leadership_ids == {str(hr["id"]), str(price["id"])}


def test_a_bound_product_key_sees_only_its_project() -> None:
    store = _store()
    project_id = str(uuid4())
    own = _memory(
        store,
        title="lantern feature",
        canonical_text="ship the lantern feature",
        domain="project",
        sensitivity="public",
        project_id=project_id,
    )
    other = _memory(store, title="lantern feature", canonical_text="a different lantern feature", project_id=str(uuid4()))
    product = AgentIdentity(
        agent_id="showcase-product",
        permission_profile="project_scoped_agent",
        project_scope=(project_id,),
        project_scope_locked=True,
    )
    found = set(_ids(_ask(store, "lantern feature", product)))
    assert found == {str(own["id"])}
    assert str(other["id"]) not in found


def test_plain_notes_and_a_hidden_topic_say_the_same_thing() -> None:
    store = _store()
    _memory(store, memory_type="semantic", title="a note", canonical_text="plain notes have no decision")
    empty = _ask(store, "missing topic")
    hidden_store = _store()
    _memory(
        hidden_store,
        title="handbook leave",
        canonical_text="the handbook leave rule changed",
        domain="legal",
        sensitivity="confidential",
    )
    support = AgentIdentity(agent_id="showcase-support", permission_profile="memory_proposal_agent")
    hidden = _ask(hidden_store, "handbook", support)
    assert empty["chains"] == []
    assert hidden["chains"] == []
    assert empty["message"] == hidden["message"] == NOT_FOUND
    assert empty["search"] == hidden["search"] == SEARCH
    assert "count" not in json.dumps(empty)
    assert "gap" not in json.dumps(hidden)


def test_an_answer_at_the_128_cap_gives_no_count_and_no_gap_marker() -> None:
    store = _store()
    support = AgentIdentity(agent_id="showcase-support", permission_profile="memory_proposal_agent")
    visible = _memory(
        store,
        title="lantern policy",
        canonical_text="the readable lantern policy",
        created_at="2020-01-01T00:00:00Z",
    )
    for index in range(128):
        _memory(
            store,
            title="lantern policy",
            canonical_text=f"hidden lantern {index}",
            domain="legal",
            sensitivity="confidential",
            created_at=f"2024-01-01T00:00:{index:02d}Z" if index < 60 else f"2024-06-01T00:{index:02d}:00Z",
        )
    answer = _ask(store, "lantern", support)
    dumped = json.dumps(answer)
    assert answer["chains"] == []
    assert answer["message"] == NOT_FOUND
    assert "count" not in dumped
    assert "gap" not in dumped
    assert str(visible["id"]) not in dumped


def test_the_budget_of_10000_hidden_candidates_stops_at_128(capsys: pytest.CaptureFixture[str]) -> None:
    import time

    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, f"{user_id}@example.com", "Test User")
    store = SQLiteVNextStore(conn, user_id)
    visible = str(uuid4())
    conn.execute(
        """
        INSERT INTO memories (
          id, user_id, memory_key, value, status, source_event_ids, memory_type,
          title, canonical_text, domain, sensitivity, confirmation_status, created_at
        ) VALUES (?, ?, ?, '{}', 'active', '[]', 'decision', ?, ?, 'project', 'public', 'confirmed', ?)
        """,
        (visible, user_id, f"decision.{visible}", "lantern policy", "the readable lantern policy", "2020-01-01T00:00:00Z"),
    )
    rows = [
        (
            str(uuid4()),
            user_id,
            f"decision.hidden.{index}",
            "lantern policy",
            f"hidden lantern {index}",
            f"2024-01-01T00:00:{index % 60:02d}Z",
        )
        for index in range(10_000)
    ]
    conn.executemany(
        """
        INSERT INTO memories (
          id, user_id, memory_key, value, status, source_event_ids, memory_type,
          title, canonical_text, domain, sensitivity, confirmation_status, created_at
        ) VALUES (?, ?, ?, '{}', 'active', '[]', 'decision', ?, ?, 'legal', 'confidential', 'confirmed', ?)
        """,
        rows,
    )
    conn.commit()
    support = AgentIdentity(agent_id="showcase-support", permission_profile="memory_proposal_agent")
    started = time.perf_counter()
    answer = what_changed(store, user_id=user_id, identity=support, topic="lantern", today=TODAY)
    elapsed = time.perf_counter() - started
    dumped = json.dumps(answer)
    assert answer["chains"] == []
    assert "count" not in dumped
    assert "gap" not in dumped
    assert visible not in dumped
    assert elapsed < 1.5
    recall_started = time.perf_counter()
    store.search_memories_fts(query="lantern", memory_types=("decision",), limit=20)
    recall_elapsed = time.perf_counter() - recall_started
    print(f"what_changed_10000_seconds={elapsed:.4f}")
    print(f"recall_10000_seconds={recall_elapsed:.4f}")
    captured = capsys.readouterr()
    assert "what_changed_10000_seconds=" in captured.out
    assert recall_elapsed < 1.5


def test_a_handwritten_decision_comes_back() -> None:
    store = _store()
    memory = _memory(
        store,
        title="Sunday kiln",
        canonical_text="the kiln door stays shut on Sundays",
        domain="project",
        sensitivity="public",
    )
    found = _ids(_ask(store, "kiln door"))
    assert found == [str(memory["id"])]


def test_a_source_replacement_the_importer_recorded_is_a_step() -> None:
    store = _store()
    older = store.create_source(
        {
            "source_type": "document",
            "title": "June price note",
            "content_hash": f"sha256:{uuid4()}",
            "domain": "financial",
            "sensitivity": "private",
            "source_created_at": "2025-06-02T00:00:00Z",
        }
    )
    newer = store.create_source(
        {
            "source_type": "document",
            "title": "November price note",
            "content_hash": f"sha256:{uuid4()}",
            "domain": "financial",
            "sensitivity": "private",
            "source_created_at": "2025-11-18T00:00:00Z",
        }
    )
    store.update_source(source_id=str(older["id"]), patch={"metadata_json": {"superseded_by": str(newer["id"])}})
    _memory(
        store,
        title="Keel sensor kit price",
        canonical_text="the list price was cut",
        domain="financial",
        sensitivity="private",
        metadata_json={"agentic_memory": {"source_refs": [str(older["id"])]}},
    )
    history = _ask(store, "Keel sensor", AgentIdentity(agent_id="showcase-sales", permission_profile="trusted_local_agent"))["chains"][0]["history"]
    kinds = {step["id"]: step["kind"] for step in history}
    assert kinds[str(older["id"])] == "source"
    assert kinds[str(newer["id"])] == "source"
    replacement = next(step for step in history if step["id"] == str(newer["id"]))
    assert replacement["replaced"] == str(older["id"])


def test_the_setup_script_records_the_planted_prices_through_commit(tmp_path: Path) -> None:
    from scripts.showcase_decisions import record_planted_prices
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    import scripts.alice_bench as bench
    from uuid import UUID

    db_path = tmp_path / "decisions.sqlite"
    older, newer = record_planted_prices(db_path)
    sales = AgentIdentity(agent_id="showcase-sales", permission_profile="trusted_local_agent")
    with sqlite_user_connection(db_path, UUID(bench.DEFAULT_USER_ID)) as conn:
        store = SQLiteVNextStore(conn, bench.DEFAULT_USER_ID)
        chain = _ask(store, "Keel sensor kit price", sales)["chains"][0]
    assert [step["id"] for step in chain["history"]] == [older, newer]
    assert chain["current"]["id"] == newer
    assert chain["current"]["replaced"] == older
    assert chain["current"]["reason"]["text"] == quote_session_brief_text(
        "the June price was too high for the renewal"
    )
    assert chain["current"]["change"] == quote_session_brief_text(
        "the list price was cut to 520 ledger units"
    )
    assert "decision_text" not in str(chain["current"]["change"])


def test_the_demo_binds_the_product_key_to_the_product_project(tmp_path: Path) -> None:
    from scripts.showcase_demo import prepare_demo_product

    db_path = tmp_path / "demo.sqlite"
    project_id = prepare_demo_product(db_path)
    again = prepare_demo_product(db_path)
    assert again == project_id
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT project_scope FROM agent_api_keys WHERE agent_id = ?",
        ("showcase-product",),
    ).fetchone()
    assert row["project_scope"] == project_id
    assert "prepare_demo_product" not in Path("scripts/alice_bench.py").read_text(encoding="utf-8")
