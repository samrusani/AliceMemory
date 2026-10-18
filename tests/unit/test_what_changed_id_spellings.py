"""Every id the what-changed read loads from a row is read in the spelling the label classifier uses.

A pointer between two decisions, a source that replaced another, and the two ends of a contradiction edge are stored by writers
of different ages and by different backends. PostgreSQL returns a ``uuid`` column as a ``UUID`` object, and a ``text`` column
(the ends of an edge) as the text it was given. SQLite returns whatever text was stored. A read that accepts only a string in
one exact spelling drops the pointer or fails to match the row it names, and a step that a later decision replaced is then
reported as the current one. These tests store each pointer field in every spelling ``uuid.UUID`` takes and fail when the
read does not follow it.

The same fields as ``UUID`` objects, and the belief rows that SQLite does not have, are in
``tests/integration/test_what_changed_review_postgres.py``.

Mutations (``scripts/derived_label_mutations.json`` replays them), each alone, in ``vnext_what_changed.py``: read
``superseded_by`` of a decision without ``identifier``; read ``supersedes`` without it; read the ``superseded_by`` of a
source without it; read ``from_id`` of an edge without it; read ``to_id`` of an edge without it; leave the successors of a
decision out of the walk.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_what_changed import what_changed
from tests.unit.legacy_citation_support import SPELLINGS

TODAY = "2026-10-14"
READER = AgentIdentity(agent_id="reader", permission_profile="read_only_agent")


def _store() -> SQLiteVNextStore:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, f"{user_id}@example.com", "Test User")
    return SQLiteVNextStore(conn, user_id)


def _decision(store: SQLiteVNextStore, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
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
    row.update(overrides)
    return store.create_memory(row)


def _point(store: SQLiteVNextStore, row_id: object, column: str, value: str) -> None:
    """Store a pointer as a writer of any age could have: as text, in the spelling given."""

    store.conn.execute(f"UPDATE memories SET {column} = ? WHERE id = ?", (value, str(row_id)))


def _ask(store: SQLiteVNextStore, topic: str, identity: AgentIdentity | None = READER) -> dict[str, object]:
    return what_changed(store, user_id=store.user_id, identity=identity, topic=topic, today=TODAY)


def _ids(chain: dict[str, object]) -> list[str]:
    return [str(step["id"]) for step in chain["history"]]  # type: ignore[union-attr]


SPELLED = sorted(SPELLINGS)


@pytest.mark.parametrize("spelling", SPELLED)
def test_a_forward_pointer_in_any_spelling_reaches_the_replacement(spelling: str) -> None:
    """Only the old decision matches the topic. Its ``superseded_by`` is stored in every spelling of the new decision's id.

    Mutation: read ``superseded_by`` as ``_text(row.get("superseded_by"))``, or as the stored value without ``identifier``."""

    store = _store()
    old = _decision(store, title="price policy", canonical_text="the price is 600", valid_from="2026-01-01T00:00:00Z")
    new = _decision(store, title="renewal tariff", canonical_text="the tariff is 520", valid_from="2026-02-01T00:00:00Z")
    _point(store, old["id"], "superseded_by", SPELLINGS[spelling](str(new["id"])))
    answer = _ask(store, "price policy")
    assert len(answer["chains"]) == 1  # type: ignore[arg-type]
    chain = answer["chains"][0]  # type: ignore[index]
    assert _ids(chain) == [str(old["id"]), str(new["id"])]
    assert chain["current"]["id"] == str(new["id"])


@pytest.mark.parametrize("spelling", SPELLED)
def test_a_back_pointer_in_any_spelling_reaches_the_replaced_decision(spelling: str) -> None:
    """Only the new decision matches the topic. Its ``supersedes`` is stored in every spelling of the old decision's id, and
    the old decision names no successor.

    Mutation: read ``supersedes`` as ``_text(row.get("supersedes"))``, or as the stored value without ``identifier``."""

    store = _store()
    old = _decision(store, title="price policy", canonical_text="the price is 600", valid_from="2026-01-01T00:00:00Z")
    new = _decision(store, title="renewal tariff", canonical_text="the tariff is 520", valid_from="2026-02-01T00:00:00Z")
    _point(store, new["id"], "supersedes", SPELLINGS[spelling](str(old["id"])))
    answer = _ask(store, "renewal tariff")
    assert len(answer["chains"]) == 1  # type: ignore[arg-type]
    chain = answer["chains"][0]  # type: ignore[index]
    assert _ids(chain) == [str(old["id"]), str(new["id"])]
    assert chain["current"]["replaced"] == str(old["id"])


@pytest.mark.parametrize("spelling", SPELLED)
def test_two_matches_joined_by_a_pointer_in_any_spelling_are_one_chain(spelling: str) -> None:
    """Both decisions match the topic and the old one points at the new one. There is one chain, one ``current`` and one start.

    Mutations: read ``superseded_by`` without ``identifier``: two chains come back and the old one says it is current; leave the
    chain's own members out of the predecessors of a step: the new decision replaced nothing."""

    store = _store()
    old = _decision(store, title="price policy", canonical_text="the price is 600", valid_from="2026-01-01T00:00:00Z")
    new = _decision(store, title="price policy", canonical_text="the price is 520", valid_from="2026-02-01T00:00:00Z")
    _point(store, old["id"], "superseded_by", SPELLINGS[spelling](str(new["id"])))
    chains = _ask(store, "price policy")["chains"]
    assert len(chains) == 1  # type: ignore[arg-type]
    assert sorted(_ids(chains[0])) == sorted([str(old["id"]), str(new["id"])])  # type: ignore[index]
    assert chains[0]["current"]["id"] == str(new["id"])  # type: ignore[index]
    # The new decision names no ``supersedes``, so the old one is its predecessor only because the old one points at it.
    assert chains[0]["current"]["replaced"] == str(old["id"])  # type: ignore[index]


def test_a_decision_that_names_what_it_replaced_is_reached_from_the_older_one() -> None:
    """The new decision says it replaced the old one, and the old one has no ``superseded_by``. A topic that matches only the old
    decision still reaches the new one, because the walk asks for the rows that name this one in ``supersedes``.

    Mutation: drop the rows that name this decision in ``supersedes`` from the walk."""

    store = _store()
    old = _decision(store, title="price policy", canonical_text="the price is 600", valid_from="2026-01-01T00:00:00Z")
    new = _decision(store, title="renewal tariff", canonical_text="the tariff is 520", valid_from="2026-02-01T00:00:00Z",
                    supersedes=str(old["id"]))
    chains = _ask(store, "price policy")["chains"]
    assert len(chains) == 1  # type: ignore[arg-type]
    assert _ids(chains[0]) == [str(old["id"]), str(new["id"])]  # type: ignore[index]
    assert chains[0]["current"]["id"] == str(new["id"])  # type: ignore[index]


@pytest.mark.parametrize("first", ["old", "new"])
def test_a_chain_found_from_either_start_is_the_same_chain(first: str) -> None:
    """The decisions match the topic in both orders of rank. One chain comes back with the same members.

    Mutation: leave the rows that name this decision in ``supersedes`` out of the walk: the old start is a chain of its own."""

    store = _store()
    old_words = "price policy price policy price policy" if first == "old" else "price policy"
    new_words = "price policy price policy price policy" if first == "new" else "price policy"
    old = _decision(store, title=old_words, canonical_text="the price is 600", valid_from="2026-01-01T00:00:00Z")
    new = _decision(store, title=new_words, canonical_text="the price is 520", valid_from="2026-02-01T00:00:00Z",
                    supersedes=str(old["id"]))
    chains = _ask(store, "price policy")["chains"]
    assert len(chains) == 1  # type: ignore[arg-type]
    assert sorted(_ids(chains[0])) == sorted([str(old["id"]), str(new["id"])])  # type: ignore[index]
    assert chains[0]["current"]["id"] == str(new["id"])  # type: ignore[index]
    assert chains[0]["start"] == str((old if first == "old" else new)["id"])  # type: ignore[index]


@pytest.mark.parametrize("spelling", SPELLED)
def test_a_source_replacement_pointer_in_any_spelling_is_a_step(spelling: str) -> None:
    """The importer stores the id of the replacing source in the metadata of the old one, in the spelling its writer chose.

    Mutation: read the ``superseded_by`` of a source without ``identifier``."""

    store = _store()
    older = store.create_source({"source_type": "document", "title": "June price note", "content_hash": f"sha256:{uuid4()}",
                                 "domain": "project", "sensitivity": "public", "source_created_at": "2025-06-02T00:00:00Z"})
    newer = store.create_source({"source_type": "document", "title": "November price note", "content_hash": f"sha256:{uuid4()}",
                                 "domain": "project", "sensitivity": "public", "source_created_at": "2025-11-18T00:00:00Z"})
    store.update_source(source_id=str(older["id"]), patch={"metadata_json": {"superseded_by": SPELLINGS[spelling](str(newer["id"]))}})
    _decision(store, title="price policy", canonical_text="the list price was cut",
              metadata_json={"agentic_memory": {"source_refs": [str(older["id"])]}})
    history = _ask(store, "price policy")["chains"][0]["history"]  # type: ignore[index]
    by_id = {step["id"]: step for step in history}
    assert by_id[str(older["id"])]["kind"] == "source"
    assert by_id[str(newer["id"])]["kind"] == "source"
    assert by_id[str(newer["id"])]["replaced"] == str(older["id"])


@pytest.mark.parametrize("spelling", SPELLED)
def test_a_cited_source_in_any_spelling_is_a_citation(spelling: str) -> None:
    """A commit names the source it cites as ``{"source_id": ...}`` in any spelling ``uuid.UUID`` takes. The saved-quote reader
    reads the id in its canonical form, and the citation is the stored source under that id."""

    store = _store()
    cited = store.create_source({"source_type": "document", "title": "June price note", "content_hash": f"sha256:{uuid4()}",
                                 "domain": "project", "sensitivity": "public", "source_created_at": "2025-06-02T00:00:00Z"})
    _decision(store, title="price policy", canonical_text="the list price was cut",
              metadata_json={"agentic_memory": {"source_refs": [{"source_id": SPELLINGS[spelling](str(cited["id"])), "quote": "cut"}]}})
    step = _ask(store, "price policy")["chains"][0]["history"][0]  # type: ignore[index]
    assert step["sources"] == [{"source_id": str(cited["id"]), "title": "June price note", "date": "2025-06-02"}]
    assert step["reason"]["source_id"] == str(cited["id"])


def _contradiction(store: SQLiteVNextStore, left: str, right: str, *, status: str = "candidate") -> None:
    store.create_graph_edge(
        {
            "from_type": "memory",
            "from_id": left,
            "to_type": "memory",
            "to_id": right,
            "edge_type": "contradicts",
            "metadata_json": {"status": status},
        }
    )


@pytest.mark.parametrize("end", ["from_id", "to_id"])
@pytest.mark.parametrize("spelling", [name for name in SPELLED if name != "canonical"])
def test_the_end_of_a_contradiction_in_any_spelling_disputes_the_chain(end: str, spelling: str) -> None:
    """An unresolved contradiction between the two decisions of a chain clears ``current``, whichever spelling stored the id of
    either end of the edge.

    Mutations: read ``from_id`` of an edge without ``identifier``; read ``to_id`` of an edge without it."""

    store = _store()
    old = _decision(store, title="price policy", canonical_text="the price is 600", valid_from="2026-01-01T00:00:00Z")
    new = _decision(store, title="price policy", canonical_text="the price is 520", valid_from="2026-02-01T00:00:00Z",
                    supersedes=str(old["id"]))
    store.update_memory(memory_id=str(old["id"]), patch={"superseded_by": str(new["id"])})
    left, right = str(old["id"]), str(new["id"])
    if end == "from_id":
        left = SPELLINGS[spelling](left)
    else:
        right = SPELLINGS[spelling](right)
    _contradiction(store, left, right)
    chains = _ask(store, "price policy")["chains"]
    assert len(chains) == 1  # type: ignore[arg-type]
    chain = chains[0]  # type: ignore[index]
    assert chain["current"] is None
    assert set(chain["disputed"]) == {str(old["id"]), str(new["id"])}
    assert json.dumps(chain).count('"disagrees_with": null') == 0
