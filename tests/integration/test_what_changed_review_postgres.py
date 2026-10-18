"""What-changed on PostgreSQL: the saved quote of a row, the pointers between decisions, and the profile matrix.

PostgreSQL returns a ``uuid`` column as a ``UUID`` object, so ``memories.superseded_by`` and ``memories.supersedes`` are
objects and the ends of an edge (a ``text`` column) are strings in whatever spelling they were written. The read once accepted
only a string, and a decision that a later one replaced was then the current one whenever the topic matched only the older
decision. Every test here reads through both doors, the HTTP route function and the MCP tool, and expects the same answer
from each.

The quote of a step is shown only when the saved-quote reader (the one the context pack and ``alice_explain`` use) leaves it in
the row. The differential test plants every citation shape of the saved-quote catalogs and compares each caller's reasons with
that reader and with the full context pack. The matrix at the end gives each profile a vault with confidential, private,
other-project, derived, unverified and archived decisions, adds them one at a time, and compares each profile's answer with
what ``alice_recall`` allows the same key.

Mutations (``scripts/derived_label_mutations.json`` replays them), each alone, in ``vnext_what_changed.py``: read
``superseded_by`` as a string only (the forward test and the contradiction test fail); read ``supersedes`` as a string only;
read the ``id`` of a start row as a string only; read ``to_id`` or ``from_id`` of an edge without ``identifier``; read
``memory_id`` of a belief without it; look up the beliefs of the ``to`` end of an edge only; judge each entry of the refs alone.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping
from uuid import UUID, uuid4

import pytest

from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.mcp.context import _vnext_context_pack_payload
from alicebot_api.mcp.memories import redact_memory_flow
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.recall_framing import quote_session_brief_text
from alicebot_api.routers import vnext_changes
from alicebot_api.routers.vnext_changes import WhatChangedBody, post_vnext_changes
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY, VNEXT_DOMAINS, AgentIdentity
from alicebot_api.vnext_agent_keys import create_agent_key, resolve_agent_identity
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_label_writes import without_insert_floor
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService
from alicebot_api.vnext_source_fence import SavedProvenanceReader, SourceReadFence
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.conftest import lock_label_fixture
from tests.unit.test_what_changed_saved_quote_reader import DECLARED, KEYED, READS_CONFIDENTIAL, catalog_cases
from tests.unit.what_changed_quote_support import PACK_CALLERS, UNLIMITED, run_quote_differential

ALPHA = "11111111-1111-4111-8111-111111111111"
BETA = "22222222-2222-4222-8222-222222222222"
MARK = "ZQXWCPG0001KEEL"
SECOND = "ZQXWCPG0002KEEL"
# The keys that read a decision with no project, and so are asked about the decisions below that carry none.
UNBOUND_READERS = ("read_only", "memory_proposal", "trusted", "admin")


def _memory(store, title, text, *, domain="project", sensitivity="public", scope=None, **extra):
    row = {
        "memory_key": f"decision.{uuid4()}",
        "memory_type": "decision",
        "title": title,
        "canonical_text": text,
        "status": "active",
        "domain": domain,
        "sensitivity": sensitivity,
        "confirmation_status": "confirmed",
        "value": {"text": text},
        "project_id": scope,
        "metadata_json": {"project_scope": [scope]} if scope else {},
    }
    row.update(extra)
    return store.create_memory(row)


def _source(store, title, *, domain="project", sensitivity="public", scope=None, **extra):
    row = {
        "source_type": "note",
        "title": title,
        "content_hash": f"sha256:{uuid4()}",
        "domain": domain,
        "sensitivity": sensitivity,
        "metadata_json": {"project_scope": [scope]} if scope else {},
    }
    row.update(extra)
    return store.create_source(row)


class Vault:
    """A migrated database with one user, and the doors that read it."""

    def __init__(self, urls, monkeypatch, label: str) -> None:
        self.app_url = urls["app"]
        self.user_id = uuid4()
        self.monkeypatch = monkeypatch
        with self.store() as store:
            ContinuityStore(store.conn).create_user(self.user_id, f"{label}@example.test", label)
        monkeypatch.setattr(vnext_changes, "get_settings", lambda: Settings(database_url=self.app_url))
        monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
        monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
        self.keys: dict[str, str] = {}

    def store(self):
        return _StoreScope(self.app_url, self.user_id)

    def mutate(self):
        """A transaction that changes labels or lifecycle: the graph lock first, then the label lock, as production does."""

        return _StoreScope(self.app_url, self.user_id, locked=True)

    def make_keys(self) -> dict[str, str]:
        with self.store() as store:
            self.keys = {
                name: create_agent_key(store, user_id=str(self.user_id), agent_id=name, permission_profile=profile, project_scope=scope)[1]
                for name, profile, scope in KEYED
            }
        return self.keys

    def route(self, key: str | None, topic: str) -> dict[str, object]:
        response = post_vnext_changes(
            self.user_id, WhatChangedBody(topic=topic), authorization=f"Bearer {key}" if key else None
        )
        assert response.status_code == 200, response.body
        return json.loads(response.body)

    def _context(self, key: str | None) -> MCPRuntimeContext:
        self.monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        if key:
            self.monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        return MCPRuntimeContext(database_url=self.app_url, user_id=UUID(str(self.user_id)))

    def tool(self, key: str | None, arguments: dict[str, object], *, name: str = "alice_what_changed") -> dict[str, object]:
        payload = call_mcp_tool(self._context(key), name=name, arguments=arguments)
        assert isinstance(payload, dict)
        return payload

    def explained(self, key: str | None, memory_id: str) -> str | None:
        """The text ``alice_explain`` returns for this key, or None when it refuses the memory."""

        try:
            return json.dumps(self.tool(key, {"memory_id": memory_id}, name="alice_explain"))
        except MCPToolError:
            return None

    def pack_rows(self, key: str | None, query: str, *, declared: bool = False) -> dict[str, Mapping[str, object]]:
        """The memory rows of the full context pack for this caller, keyed by id."""

        arguments: dict[str, object] = {"query": query, "max_items": 50, **(DECLARED if declared else {})}
        pack = _vnext_context_pack_payload(self._context(key), arguments)
        return {str(row["id"]): row for row in pack["relevant_memories"]}  # type: ignore[union-attr]

    def both(self, key: str | None, topic: str) -> tuple[dict[str, object], dict[str, object]]:
        return self.route(key, topic), self.tool(key, {"topic": topic})

    def one_chain(self, key: str | None, topic: str) -> dict[str, object]:
        route, tool = self.both(key, topic)
        assert route["chains"] == tool["chains"]
        assert len(route["chains"]) == 1, route["chains"]  # type: ignore[arg-type]
        return route["chains"][0]  # type: ignore[index]

    def readers_of_one_chain(self, topic: str) -> dict[str, dict[str, object]]:
        """The single chain that every caller with no project is given for ``topic``: the four unbound keys at both doors, the
        declared identity and the owner at the tool. They must all be the same chain."""

        keys = self.keys or self.make_keys()
        found = {name: self.one_chain(keys[name], topic) for name in UNBOUND_READERS}
        for name, arguments in (("declared", {"topic": topic, **DECLARED}), ("owner", {"topic": topic})):
            answer = self.tool(None, arguments)
            assert len(answer["chains"]) == 1, (name, answer["chains"])  # type: ignore[arg-type]
            found[name] = answer["chains"][0]  # type: ignore[index]
        return found


class _StoreScope:
    def __init__(self, url, user_id, *, locked: bool = False) -> None:
        self._scope = user_connection(url, user_id)
        self._locked = locked

    def __enter__(self):
        conn = self._scope.__enter__()
        self.store = PostgresVNextStore(conn)
        if self._locked:
            lock_label_fixture(self.store)
        return self.store

    def __exit__(self, *exc):
        return self._scope.__exit__(*exc)


def _reason(answer: dict[str, object], step_id: object) -> object:
    """The reason of the step ``step_id`` in whichever chain of the answer holds it."""

    return next(
        step for chain in answer["chains"] for step in chain["history"] if step["id"] == str(step_id)  # type: ignore[union-attr]
    )["reason"]


def _ids(chain: dict[str, object]) -> list[str]:
    return [step["id"] for step in chain["history"]]  # type: ignore[union-attr]


# --- the saved quote ----------------------------------------------------------------------------------------------------


def test_a_quote_in_a_sibling_entry_is_withheld_when_the_cited_memory_becomes_confidential(migrated_database_urls, monkeypatch):
    """The decision cites ``memory:<id>`` in one entry and keeps the words in the next. A key that may not read the cited memory
    gets no quote at either door once the memory is confidential; before that it does. The pack and ``alice_explain`` give each
    caller the same answer, and ``alice_recall`` carries the quote to none.

    Mutation: ``_reason`` takes the first quote of the stored refs without the saved-quote reader."""

    vault = Vault(migrated_database_urls, monkeypatch, "sibling")
    with vault.store() as store:
        cited = _memory(store, "ledger handbook", "the handbook sentence", scope=ALPHA)
        decision = _memory(store, "lantern policy", "public lantern policy", scope=ALPHA,
                           metadata_json={"project_scope": [ALPHA],
                                          "agentic_memory": {"source_refs": [f"memory:{cited['id']}", {"quote": MARK}]}})
    shown = {"text": quote_session_brief_text(MARK), "source_id": None}
    step = str(decision["id"])
    for answer in vault.both(None, "lantern policy"):
        assert _reason(answer, step) == shown
    keys = vault.make_keys()
    for name, key in keys.items():
        for answer in vault.both(key, "lantern policy"):
            assert _reason(answer, step) == shown, name
    declared_before = vault.tool(None, {"topic": "lantern policy", **DECLARED})
    assert _reason(declared_before, step) == shown
    with vault.mutate() as store:
        store.update_memory(memory_id=str(cited["id"]), patch={"sensitivity": "confidential", "domain": "legal"})
    for name, key in keys.items():
        answers = vault.both(key, "lantern policy")
        expected = shown if name in READS_CONFIDENTIAL else None
        assert [_reason(answer, step) for answer in answers] == [expected, expected], name
        if expected is None:
            assert MARK not in json.dumps(answers), name
        explained = vault.explained(key, step)
        assert (explained is not None and MARK in explained) == (expected is not None), name
        assert MARK not in json.dumps(vault.tool(key, {"query": "lantern policy"}, name="alice_recall"))
    declared = vault.tool(None, {"topic": "lantern policy", **DECLARED})
    assert MARK not in json.dumps(declared)
    assert _reason(declared, step) is None
    owner = vault.tool(None, {"topic": "lantern policy"})
    assert _reason(owner, step) == shown
    for name in PACK_CALLERS:
        rows = vault.pack_rows(keys.get(name), "lantern policy", declared=name == "declared")
        assert (MARK in json.dumps(rows[step], default=str)) == (name in UNLIMITED), name


@pytest.mark.parametrize("how", ["archived", "redacted"])
def test_a_quote_that_names_a_retired_memory_in_an_incidental_place_is_withheld(migrated_database_urls, monkeypatch, how):
    """An id under ``origin`` is judged when it names a stored memory, and an archived or redacted memory is stored.

    Mutation: ``_reason`` takes the first quote of the stored refs without the saved-quote reader."""

    vault = Vault(migrated_database_urls, monkeypatch, f"retired-{how}")
    with vault.store() as store:
        cited = _memory(store, "ledger handbook", "the handbook sentence", scope=ALPHA)
        decision = _memory(store, "lantern policy", "public lantern policy", scope=ALPHA,
                           metadata_json={"project_scope": [ALPHA],
                                          "agentic_memory": {"source_refs": [{"quote": MARK, "origin": str(cited["id"])}]}})
        control = _memory(store, "lantern policy", "dim lantern policy", scope=ALPHA,
                          metadata_json={"project_scope": [ALPHA], "agentic_memory": {"source_refs": [{"quote": SECOND}]}})
    keys = vault.make_keys()
    for name, key in keys.items():
        for answer in vault.both(key, "lantern policy"):
            assert _reason(answer, decision["id"]) is not None, name
    with vault.mutate() as store:
        if how == "archived":
            store.update_memory(memory_id=str(cited["id"]), patch={"status": "archived"})
        else:
            assert redact_memory_flow(store, memory_id=str(cited["id"]), reason="synthetic")["status"] == "redacted"
    for name, key in keys.items():
        answers = vault.both(key, "lantern policy")
        expected = name in UNLIMITED
        for answer in answers:
            assert (_reason(answer, decision["id"]) is not None) == expected, (how, name)
            assert _reason(answer, control["id"]) == {"text": quote_session_brief_text(SECOND), "source_id": None}, name
        if not expected:
            assert MARK not in json.dumps(answers), (how, name)
    for name in PACK_CALLERS:
        rows = vault.pack_rows(keys.get(name), "lantern policy", declared=name == "declared")
        assert (MARK in json.dumps(rows[str(decision["id"])], default=str)) == (name in UNLIMITED), (how, name)
    declared = vault.tool(None, {"topic": "lantern policy", **DECLARED})
    assert MARK not in json.dumps(declared)


class _PostgresBackend:
    """The differential check on PostgreSQL, read through the HTTP route function and the MCP tool."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    def targets(self) -> tuple[str, str, str, str]:
        with self.vault.store() as store:
            public = _source(store, "public price note", scope=ALPHA)
            hidden = _source(store, "confidential ledger note", domain="legal", sensitivity="confidential", scope=ALPHA)
            secret = _memory(store, "confidential handbook", "the confidential handbook sentence",
                             domain="legal", sensitivity="confidential", scope=ALPHA)
            open_memory = _memory(store, "open handbook", "the open handbook sentence", scope=ALPHA)
        return tuple(str(row["id"]) for row in (public, hidden, secret, open_memory))  # type: ignore[return-value]

    def plant_many(self, decisions: list[tuple[str, str, list[object]]]) -> list[str]:
        with self.vault.store() as store:
            return [
                str(_memory(store, title, text, scope=ALPHA,
                            metadata_json={"project_scope": [ALPHA], "agentic_memory": {"source_refs": refs}})["id"])
                for title, text, refs in decisions
            ]

    def make_keys(self) -> dict[str, str]:
        return self.vault.make_keys()

    def read(self, key: str | None, token: str, *, declared: bool) -> dict[str, dict[str, object]]:
        out = {"tool": self.vault.tool(key, {"topic": token, **(DECLARED if declared else {})})}
        if not declared:
            out["route"] = self.vault.route(key, token)
        return out

    def pack(self, key: str | None, token: str, *, declared: bool) -> dict[str, Mapping[str, object]]:
        return self.vault.pack_rows(key, token, declared=declared)

    def reader_rows(self, key: str | None, ids: list[str], *, declared: bool) -> list[Mapping[str, object]]:
        with self.vault.store() as store:
            if key is not None:
                identity = resolve_agent_identity(store, user_id=str(self.vault.user_id), raw_key=key, payload={})
            else:
                identity = AgentIdentity.from_payload(DECLARED) if declared else None
            reader = SavedProvenanceReader(store, fence=SourceReadFence.for_identity(identity))
            rows = [store.get_memory(memory_id) for memory_id in ids]
            reader.memories(rows)
            return [reader.memory(row) for row in rows]


def test_the_reason_is_what_the_saved_quote_reader_leaves_in_the_refs(migrated_database_urls, monkeypatch):
    """The differential check of the SQLite tests, on PostgreSQL: every citation shape of the saved-quote catalogs, alone and
    with an entry of plain words before it and after it, read at both doors by the owner, a declared identity and a key of
    each profile. The reason is the first quote the reader leaves in the refs of the row for that caller, and the pack shows the
    same quote of the same row to every caller it answers with the reference list.

    Mutations: ``_reason`` reads the stored row and not the reader's; ``_reason`` judges each entry alone."""

    vault = Vault(migrated_database_urls, monkeypatch, "differential")
    results = run_quote_differential(_PostgresBackend(vault), catalog_cases)
    both = [counts for who, counts in results.items() if who not in UNLIMITED]
    assert all(shown > 0 and kept > 0 for shown, kept in both), results
    assert results["owner"][1] < results["read_only"][1]


# --- pointers that PostgreSQL returns as UUID objects ----------------------------------------------------------------------


def test_a_replaced_decision_is_not_current_when_only_the_old_one_matches_the_topic(migrated_database_urls, monkeypatch):
    """``superseded_by`` is a ``UUID`` object. The read once dropped it, so the topic that matched only the old decision gave a
    one-step history with the old decision as current.

    Mutations: read ``superseded_by`` as a string only; read the ``id`` of a start row as a string only."""

    vault = Vault(migrated_database_urls, monkeypatch, "forward")
    with vault.store() as store:
        old = _memory(store, "price policy", "price policy is 600", valid_from="2026-01-01T00:00:00Z")
        new = _memory(store, "renewal tariff", "renewal tariff is 520", valid_from="2026-02-01T00:00:00Z", supersedes=str(old["id"]))
        store.update_memory(memory_id=str(old["id"]), patch={"superseded_by": str(new["id"])})
        starts = store.search_memories_fts(query="price policy", memory_types=("decision",), limit=5)
        assert type(starts[0]["superseded_by"]) is UUID and type(starts[0]["id"]) is UUID
    for name, chain in vault.readers_of_one_chain("price policy").items():
        assert _ids(chain) == [str(old["id"]), str(new["id"])], name
        assert chain["current"]["id"] == str(new["id"]), name
        assert chain["current"]["replaced"] == str(old["id"]), name
        assert chain["start"] == str(old["id"]), name


def test_a_decision_that_names_what_it_replaced_is_followed_back_and_forward(migrated_database_urls, monkeypatch):
    """``supersedes`` is a ``UUID`` object too. The older decision names no successor, so a topic that matches only the new
    decision reaches the old one through ``supersedes``, and a topic that matches only the old decision reaches the new one
    through the rows that name it.

    Mutations: read ``supersedes`` as a string only; leave the rows that name this decision in ``supersedes`` out of the walk."""

    vault = Vault(migrated_database_urls, monkeypatch, "back")
    with vault.store() as store:
        old = _memory(store, "price policy", "price policy is 600", valid_from="2026-01-01T00:00:00Z")
        new = _memory(store, "renewal tariff", "renewal tariff is 520", valid_from="2026-02-01T00:00:00Z", supersedes=str(old["id"]))
    for topic in ("renewal tariff", "price policy"):
        for name, chain in vault.readers_of_one_chain(topic).items():
            assert _ids(chain) == [str(old["id"]), str(new["id"])], (topic, name)
            assert chain["current"]["id"] == str(new["id"]), (topic, name)
            assert chain["current"]["replaced"] == str(old["id"]), (topic, name)


@pytest.mark.parametrize("first", ["old", "new"])
def test_a_chain_found_from_either_start_is_one_chain(migrated_database_urls, monkeypatch, first):
    """Both decisions match the topic, in either order of rank. One chain comes back, and its ``current`` is decided once.

    Mutation: leave the successor out of the walk, or read ``superseded_by`` as a string only: two chains come back."""

    vault = Vault(migrated_database_urls, monkeypatch, f"either-{first}")
    many = "price policy price policy price policy"
    with vault.store() as store:
        old = _memory(store, many if first == "old" else "price policy", "price is 600", valid_from="2026-01-01T00:00:00Z")
        new = _memory(store, many if first == "new" else "price policy", "price is 520", valid_from="2026-02-01T00:00:00Z",
                      supersedes=str(old["id"]))
        store.update_memory(memory_id=str(old["id"]), patch={"superseded_by": str(new["id"])})
    for name, chain in vault.readers_of_one_chain("price policy").items():
        assert sorted(_ids(chain)) == sorted([str(old["id"]), str(new["id"])]), name
        assert chain["current"]["id"] == str(new["id"]), name
        assert chain["start"] == str((old if first == "old" else new)["id"]), name


def _two_decisions_with_a_belief(store, *, belief_on: str = "new"):
    old = _memory(store, "belief policy", "old belief policy", valid_from="2026-01-01T00:00:00Z")
    new = _memory(store, "belief policy", "new belief policy", valid_from="2026-02-01T00:00:00Z", supersedes=str(old["id"]))
    store.update_memory(memory_id=str(old["id"]), patch={"superseded_by": str(new["id"])})
    belief = store.create_belief({"memory_id": str((new if belief_on == "new" else old)["id"]), "claim": "new belief policy"})
    return old, new, belief


def _edge(store, left, right, *, status="candidate"):
    return store.create_edge(
        {
            "from_type": left[0], "from_id": left[1], "to_type": right[0], "to_id": right[1],
            "edge_type": "contradicts", "metadata_json": {"status": status},
        }
    )


@pytest.mark.parametrize("topic", ["belief policy", "old belief policy"])
def test_an_unresolved_contradiction_disputes_the_one_chain(migrated_database_urls, monkeypatch, topic):
    """A contradiction between the old decision and the belief of the new one is unresolved. There is one chain for each topic,
    whether the topic matches both decisions or only the old one, and its ``current`` is null. No second chain starts at the old
    decision and calls it current.

    Mutation: read ``superseded_by`` as a string only: the old start is a chain of its own and says it is current."""

    vault = Vault(migrated_database_urls, monkeypatch, "disputed")
    with vault.store() as store:
        old, new, belief = _two_decisions_with_a_belief(store)
        _edge(store, ("memory", str(old["id"])), ("belief", str(belief["id"])))
    for name, chain in vault.readers_of_one_chain(topic).items():
        assert sorted(_ids(chain)) == sorted([str(old["id"]), str(new["id"])]), name
        assert chain["current"] is None, name
        assert set(chain["disputed"]) == {str(old["id"]), str(new["id"])}, name
        assert str(belief["id"]) not in json.dumps(chain), name


def test_forgotten_and_undone_successors_are_in_the_history_with_their_state(migrated_database_urls, monkeypatch):
    """A decision whose successor was forgotten or undone still shows that successor, with its real review state, and stays the
    current one. The successor is not a search hit, so it is reached only through the pointer.

    Mutation: read ``superseded_by`` as a string only: the successor is missing from the history."""

    vault = Vault(migrated_database_urls, monkeypatch, "lifecycle")
    with vault.store() as store:
        old_forget = _memory(store, "forget lantern", "original forget lantern", valid_from="2026-01-01T00:00:00Z")
        forgot = _memory(store, "forget lantern", "later forget lantern", valid_from="2026-02-01T00:00:00Z")
        old_undo = _memory(store, "undo lantern", "original undo lantern", valid_from="2026-01-01T00:00:00Z")
        undone = _memory(store, "undo lantern", "later undo lantern", valid_from="2026-02-01T00:00:00Z")
    with vault.mutate() as store:
        service = VNextMemoryCommitService(store)
        service.forget(identity=None, memory_id=str(forgot["id"]), reason="retire")
        store.update_memory(memory_id=str(old_forget["id"]), patch={"superseded_by": str(forgot["id"])})
    with vault.mutate() as store:
        VNextMemoryCommitService(store).undo(identity=None, memory_id=str(undone["id"]), reason="withdraw")
        store.update_memory(memory_id=str(old_undo["id"]), patch={"superseded_by": str(undone["id"])})
    for topic, old, gone, state in (("forget lantern", old_forget, forgot, "forgotten"), ("undo lantern", old_undo, undone, "undone")):
        for name, chain in vault.readers_of_one_chain(topic).items():
            assert _ids(chain) == [str(old["id"]), str(gone["id"])], (topic, name)
            assert chain["current"]["id"] == str(old["id"]), (topic, name)
            assert next(step for step in chain["history"] if step["id"] == str(gone["id"]))["review_state"] == state, (topic, name)


def test_an_archived_successor_is_not_a_step(migrated_database_urls, monkeypatch):
    """A successor that was archived is out of the history, and the decision that pointed at it stays current.

    Mutation: load a decision without the ``deleted_at`` test."""

    vault = Vault(migrated_database_urls, monkeypatch, "archived-successor")
    with vault.store() as store:
        old = _memory(store, "price policy", "price is 600", valid_from="2026-01-01T00:00:00Z")
        gone = _memory(store, "price policy", "price is 520", valid_from="2026-02-01T00:00:00Z", supersedes=str(old["id"]))
        store.update_memory(memory_id=str(old["id"]), patch={"superseded_by": str(gone["id"])})
        store.update_memory(memory_id=str(gone["id"]), patch={"status": "archived"})
    for name, chain in vault.readers_of_one_chain("price policy").items():
        assert _ids(chain) == [str(old["id"])], name
        assert chain["current"]["id"] == str(old["id"]), name


# --- ids that PostgreSQL does not type: the ends of an edge, the metadata of a source ----------------------------------------


@pytest.mark.parametrize("spelling", ["upper", "braces", "bare", "urn"])
@pytest.mark.parametrize("end", ["from_id", "to_id"])
def test_the_end_of_a_memory_contradiction_in_any_spelling_disputes_the_chain(migrated_database_urls, monkeypatch, end, spelling):
    """``graph_edges.from_id`` and ``to_id`` are ``text``. Either end in any spelling ``uuid.UUID`` takes names the same memory.

    Mutations: read ``from_id`` of an edge without ``identifier``; read ``to_id`` of an edge without it."""

    spell = {"upper": str.upper, "braces": lambda v: "{" + v + "}", "bare": lambda v: v.replace("-", ""), "urn": lambda v: "urn:uuid:" + v}[spelling]
    vault = Vault(migrated_database_urls, monkeypatch, f"edge-{end}-{spelling}")
    with vault.store() as store:
        old = _memory(store, "price policy", "price is 600", valid_from="2026-01-01T00:00:00Z")
        new = _memory(store, "price policy", "price is 520", valid_from="2026-02-01T00:00:00Z", supersedes=str(old["id"]))
        store.update_memory(memory_id=str(old["id"]), patch={"superseded_by": str(new["id"])})
        left, right = str(old["id"]), str(new["id"])
        left, right = (spell(left), right) if end == "from_id" else (left, spell(right))
        _edge(store, ("memory", left), ("memory", right))
    chain = vault.one_chain(vault.make_keys()["read_only"], "price policy")
    assert chain["current"] is None
    assert set(chain["disputed"]) == {str(old["id"]), str(new["id"])}


@pytest.mark.parametrize("side", ["to", "from"])
@pytest.mark.parametrize("spelling", ["canonical", "upper"])
def test_a_belief_at_either_end_of_a_contradiction_is_read_through_its_memory(migrated_database_urls, monkeypatch, side, spelling):
    """The belief an edge names is looked up by its id, whatever the spelling of the edge's text, and at either end of the
    edge. Its ``memory_id`` is the memory the chain is disputed on.

    Mutations: read ``memory_id`` of a belief without ``identifier``; read the ``id`` of a belief without it; look up the
    beliefs of the ``to`` end of an edge only."""

    vault = Vault(migrated_database_urls, monkeypatch, f"belief-{side}-{spelling}")
    with vault.store() as store:
        old, new, belief = _two_decisions_with_a_belief(store)
        belief_id = str(belief["id"]).upper() if spelling == "upper" else str(belief["id"])
        memory_end = ("memory", str(old["id"]))
        belief_end = ("belief", belief_id)
        _edge(store, memory_end, belief_end) if side == "to" else _edge(store, belief_end, memory_end)
    chain = vault.one_chain(vault.make_keys()["read_only"], "belief policy")
    assert chain["current"] is None
    assert set(chain["disputed"]) == {str(old["id"]), str(new["id"])}


@pytest.mark.parametrize("spelling", ["canonical", "upper", "braces", "bare", "urn"])
def test_a_source_replacement_pointer_in_any_spelling_is_a_step(migrated_database_urls, monkeypatch, spelling):
    """The importer stores the id of the replacing source in the JSON metadata of the old one, in the spelling its writer chose.
    The source id is a ``UUID`` object in the row. The old source is archived, which only a caller with no limits reads.

    Mutations: read the ``superseded_by`` of a source without ``identifier``; read the ``id`` of a source as a string only."""

    spell = {"canonical": str, "upper": str.upper, "braces": lambda v: "{" + v + "}", "bare": lambda v: v.replace("-", ""), "urn": lambda v: "urn:uuid:" + v}[spelling]
    vault = Vault(migrated_database_urls, monkeypatch, f"source-{spelling}")
    with vault.store() as store:
        old = _source(store, "June price note", source_type="document")
        new = _source(store, "November price note", source_type="document")
        store.update_source(source_id=str(old["id"]), patch={"metadata_json": {"superseded_by": spell(str(new["id"]))}})
        with store.conn.cursor() as cur:
            cur.execute("UPDATE sources SET deleted_at = clock_timestamp() WHERE id = %s::uuid", (str(old["id"]),))
        _memory(store, "price policy", "the price policy changed", metadata_json={"agentic_memory": {"source_refs": [str(old["id"])]}})
    keys = vault.make_keys()
    for key in (keys["admin"], None):
        chain = vault.tool(key, {"topic": "price policy"})["chains"][0]
        by_id = {step["id"]: step for step in chain["history"]}
        assert by_id[str(old["id"])]["kind"] == by_id[str(new["id"])]["kind"] == "source"
        assert by_id[str(new["id"])]["replaced"] == str(old["id"])
    # A caller with limits does not read an archived source, and is shown neither step.
    limited = vault.one_chain(keys["read_only"], "price policy")
    assert str(old["id"]) not in json.dumps(limited) and str(new["id"]) not in json.dumps(limited)


# --- the profile matrix -------------------------------------------------------------------------------------------------


def _same(left: object, right: object, note: object) -> None:
    """Fail with the lines that differ, so a changed answer shows what changed."""

    a, b = (json.dumps(value, sort_keys=True, indent=1).splitlines() for value in (left, right))
    changed = list(difflib.unified_diff(a, b, lineterm="", n=1))
    assert not changed, (note, "\n".join(changed[:60]))


def _step_ids(answer: dict[str, object]) -> set[str]:
    return {step["id"] for chain in answer["chains"] for step in chain["history"] if step["kind"] == "memory"}  # type: ignore[union-attr]


def _each_step_in_one_chain(answer: dict[str, object]) -> None:
    """A decision is a step of one chain only, and a chain has one ``current``: a chain reached from two starts is one chain."""

    seen: dict[str, str] = {}
    for chain in answer["chains"]:  # type: ignore[union-attr]
        for step in chain["history"]:
            assert step["id"] not in seen, (step["id"], seen[step["id"]], chain["start"])
            seen[step["id"]] = chain["start"]


def _recall_ids(vault: Vault, key: str | None, topic: str, *, declared: bool = False) -> set[str]:
    """What recall returns to this caller when it asks for every domain and every sensitivity: the caller's own limits decide."""

    arguments = {"query": topic, "limit": 50, "domains": list(VNEXT_DOMAINS), "sensitivity_allowed": list(ALL_SENSITIVITY),
                 **(DECLARED if declared else {})}
    recall = vault.tool(key, arguments, name="alice_recall")
    return {row["id"] for row in recall["results"]}  # type: ignore[union-attr]


def test_each_profile_is_shown_what_recall_allows_it_and_nothing_changes_with_what_it_may_not_read(migrated_database_urls, monkeypatch):
    """A vault of decisions on one topic. Confidential, private, other-project, derived, unverified and archived decisions are
    added one at a time. After each, every profile's answer names the decisions recall returns to the same caller, and an
    answer is byte for byte what it was before for every profile that recall does not give the new decision to. The profiles
    are the eight keys at both doors, a declared identity and the owner at the tool.

    Mutations: skip the label guard for a start (``_step_is_readable`` returns True); judge a derived row on its stored labels;
    stop the walk at a start without checking its readability."""

    vault = Vault(migrated_database_urls, monkeypatch, "matrix")
    topic = "keel price"
    with vault.store() as store:
        old = _memory(store, "keel price", "keel price is 600", scope=ALPHA, valid_from="2026-01-01T00:00:00Z")
        new = _memory(store, "keel price", "keel price is 520", scope=ALPHA, valid_from="2026-02-01T00:00:00Z", supersedes=str(old["id"]))
        store.update_memory(memory_id=str(old["id"]), patch={"superseded_by": str(new["id"])})
        _memory(store, "keel price list", "keel price list for public use", scope=ALPHA)
        hidden_source = _source(store, "confidential ledger note", domain="legal", sensitivity="confidential", scope=ALPHA)
    keys = vault.make_keys()
    profiles: dict[str, tuple[str | None, bool]] = {name: (key, False) for name, key in keys.items()}
    profiles["declared"] = (None, True)
    profiles["owner"] = (None, False)

    def read(name: str) -> list[dict[str, object]]:
        key, declared = profiles[name]
        if key is None:
            return [vault.tool(None, {"topic": topic, **(DECLARED if declared else {})})]
        return list(vault.both(key, topic))

    def confidential():
        with vault.store() as store:
            return _memory(store, "keel price", "keel price confidential ledger", domain="legal", sensitivity="confidential", scope=ALPHA)

    def private():
        with vault.store() as store:
            return _memory(store, "keel price", "keel price private note", sensitivity="private", scope=ALPHA)

    def other_project():
        with vault.store() as store:
            return _memory(store, "keel price", "keel price other project", scope=BETA)

    def derived():
        with vault.store() as store, without_insert_floor():
            return _memory(store, "keel price", "keel price derived row", scope=ALPHA,
                           metadata_json={"project_scope": [ALPHA], "source_id": str(hidden_source["id"])})

    def unverified():
        with vault.store() as store:
            original = _memory(store, "keel price original", "keel price original note", scope=ALPHA)
            copy = _memory(
                store, "keel price", "keel price unverified copy", scope=ALPHA,
                metadata_json=with_derived_from({"project_scope": [ALPHA], "discovered_by": "vnext_weekly_synthesis"}, {"memories": [original]}),
            )
        with vault.mutate() as store:
            assert redact_memory_flow(store, memory_id=str(original["id"]), reason="synthetic")["status"] == "redacted"
        return copy

    def archived():
        with vault.store() as store:
            row = _memory(store, "keel price", "keel price archived row", scope=ALPHA)
            later = _memory(store, "keel price", "keel price archived successor", scope=ALPHA, supersedes=str(new["id"]))
        with vault.mutate() as store:
            store.update_memory(memory_id=str(later["id"]), patch={"status": "archived"})
            store.update_memory(memory_id=str(row["id"]), patch={"status": "archived"})
        return row

    stages = [("confidential", confidential), ("private", private), ("other project", other_project),
              ("derived", derived), ("unverified", unverified), ("archived", archived)]

    def recall(name: str) -> set[str]:
        key, declared = profiles[name]
        return _recall_ids(vault, key, topic, declared=declared)

    previous = {name: read(name) for name in profiles}
    for name in profiles:
        answers = previous[name]
        assert all(_step_ids(answer) == recall(name) for answer in answers), name
        assert all(answer["chains"] == answers[0]["chains"] for answer in answers), name
        for answer in answers:
            _each_step_in_one_chain(answer)
    gains: dict[str, set[str]] = {}
    for label, make in stages:
        row = make()
        for name in profiles:
            answers = read(name)
            assert all(answer["chains"] == answers[0]["chains"] for answer in answers), (label, name)
            for answer in answers:
                _each_step_in_one_chain(answer)
            allowed = recall(name)
            assert _step_ids(answers[0]) == allowed, (label, name, _step_ids(answers[0]) ^ allowed)
            if str(row["id"]) in allowed:
                gains.setdefault(label, set()).add(name)
            else:
                _same(answers, previous[name], (label, name))
            previous[name] = answers
    # Each stage was given to some profiles and held back from the rest, so the byte comparison above had both sides to check.
    reads_confidential = {"admin", "admin_bound", "owner"}
    assert gains["confidential"] == reads_confidential
    assert gains["derived"] == reads_confidential
    assert gains["unverified"] <= reads_confidential and {"admin", "owner"} <= gains["unverified"]
    assert {"owner", "admin", "trusted"} <= gains["private"] and "read_only" not in gains["private"]
    assert {"owner", "admin", "read_only"} <= gains["other project"] and "scoped_bound" not in gains["other project"]
    assert "archived" not in gains
    assert all("owner" in who for label, who in gains.items())
    assert not ({"declared", "read_only"} & (gains["confidential"] | gains["derived"] | gains["unverified"] | gains["private"]))
    owner = vault.tool(None, {"topic": topic})
    declared = vault.tool(None, {"topic": topic, **DECLARED})
    assert len(_step_ids(owner)) > len(_step_ids(declared))
