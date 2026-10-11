"""Recall's OR fallback is decided on the rows the caller may read.

Unreleased (on main, not in v0.20.0). The full-text stage of ``alice_recall`` and of the context pack runs a strict pass
(every word of the query must match) and, when a query has two or more words and the strict pass finds nothing, one OR
pass (any word). On main before this change the strict pass counted rows the caller may not read: a row hidden by its
effective label (its inputs are above the key's limits, so the SQL filter cannot reject it by its own stored label) that
matched every word stopped the OR pass, the row was dropped afterwards, and the key got no result and the stage label
``sqlite_fts`` where a vault without that row gave the visible rows that match one word and ``sqlite_fts_or_fallback``.
Nothing of the hidden row was returned, but the count and the label told the key that a row it may not read matches all
its words. v0.20.0 returned the hidden row itself, because it had no effective labels.

The tests here run on a real SQLite vault with real minted keys, through the MCP tool registry. Each puts one visible
public memory that matches the word ``alpha`` only and, once the key's answer is recorded, adds a hidden row that
matches ``alpha beta``. The answer and the whole debug trace of the key must be equal before and after. A control
reads the hidden row as the owner with every sensitivity, so a pass means the key was held back, not that the row never
matched.

Each test names the mutation that must fail it, and the cases ``recall-fallback-*`` of
``scripts/derived_label_mutations.json`` replay them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY, AgentIdentity
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_label_writes import without_insert_floor
from alicebot_api.vnext_retrieval import VNextRetrievalService
from alicebot_api.vnext_source_fence import SourceReadFence

QUERY = "alpha beta"
VISIBLE_TEXT = "alpha only visible note"
HIDDEN_TEXT = "alpha beta hidden note"
OR_PASS = "sqlite_fts_or_fallback"
STRICT_PASS = "sqlite_fts"

# name: (permission profile, project the key is bound to)
KEYS = {
    "read_only": ("read_only_agent", None),
    "read_only_alpha": ("read_only_agent", "alpha"),
    "project_alpha": ("project_scoped_agent", "alpha"),
    "proposal": ("memory_proposal_agent", None),
    "trusted": ("trusted_local_agent", None),
    "trusted_alpha": ("trusted_local_agent", "alpha"),
    "admin_alpha": ("admin_agent", "alpha"),
    "admin": ("admin_agent", None),
}


def _memory(
    store: SQLiteVNextStore, key: str, text: str, metadata: dict[str, object], *, memory_type: str = "decision"
) -> dict[str, object]:
    with without_insert_floor():
        row = store.create_memory({
            "memory_key": key, "canonical_text": text, "title": key, "memory_type": memory_type, "status": "active",
            "domain": "project", "sensitivity": "public", "metadata_json": metadata,
        })
    append_event(store, event_type="memory.labels_raised", actor_type="system",
                 target_type="memory", target_id=str(row["id"]), payload={"cause": "repair_v3"})
    return dict(row)


def _source(store: SQLiteVNextStore, key: str, *, domain: str = "project", sensitivity: str = "public",
            scope: tuple[str, ...] = ()) -> dict[str, object]:
    return dict(store.create_source({
        "source_type": "note", "title": key, "content_hash": key, "domain": domain, "sensitivity": sensitivity,
        "metadata_json": {"project_scope": list(scope)} if scope else {},
    }))


# Each shape writes one memory that matches ``alpha beta`` whose stored labels are public, project and no scope, so the
# SQL filters of the search cannot reject it, and whose inputs put it above the limits of the keys listed in HIDDEN_FROM.
def _hidden_by_domain(store: SQLiteVNextStore, scope: tuple[str, ...], text: str = HIDDEN_TEXT) -> dict[str, object]:
    parent = _source(store, "parent-health", domain="health", scope=scope)
    return _memory(store, "hidden", text, {"source_id": str(parent["id"]), **_scope(scope)})


def _hidden_three_hops_below_a_confidential_source(
    store: SQLiteVNextStore, scope: tuple[str, ...], text: str = HIDDEN_TEXT
) -> dict[str, object]:
    # The SQL filter looks one memory deep at most, so a chain of three reaches the Python guard.
    parent = _source(store, "parent-confidential", sensitivity="confidential", scope=scope)
    first = _memory(store, "first", "chain first", {"source_id": str(parent["id"]), **_scope(scope)})
    second = _memory(store, "second", "chain second", {"consolidation": {"cluster_member_ids": [str(first["id"])]}, **_scope(scope)})
    return _memory(store, "hidden", text, {"consolidation": {"cluster_member_ids": [str(second["id"])]}, **_scope(scope)})


def _hidden_in_another_project(store: SQLiteVNextStore, scope: tuple[str, ...], text: str = HIDDEN_TEXT) -> dict[str, object]:
    parent = _source(store, "parent-beta", scope=("beta",))
    return _memory(store, "hidden", text, {"source_id": str(parent["id"]), **_scope(scope)})


def _hidden_with_a_missing_input(store: SQLiteVNextStore, scope: tuple[str, ...], text: str = HIDDEN_TEXT) -> dict[str, object]:
    return _memory(store, "hidden", text, {"source_id": str(uuid4()), **_scope(scope)})


def _scope(scope: tuple[str, ...]) -> dict[str, object]:
    return {"project_scope": list(scope)} if scope else {}


SHAPES: dict[str, Callable[..., dict[str, object]]] = {
    "domain": _hidden_by_domain,
    "chain": _hidden_three_hops_below_a_confidential_source,
    "project": _hidden_in_another_project,
    "missing_input": _hidden_with_a_missing_input,
}

# Who may not read what, from the policy of each profile and from the effective labels of the row, and from nothing else
# (the first run of each pair printed the hidden text for the keys that are not listed, which is how this table was made).
HIDDEN_FROM: dict[str, tuple[str, ...]] = {
    "domain": ("read_only", "read_only_alpha", "project_alpha", "proposal"),
    "chain": ("read_only", "read_only_alpha", "project_alpha", "proposal", "trusted", "trusted_alpha", "admin_alpha"),
    "project": ("read_only_alpha", "project_alpha", "trusted_alpha", "admin_alpha"),
    "missing_input": ("read_only", "read_only_alpha", "project_alpha", "proposal", "trusted", "trusted_alpha", "admin_alpha"),
}
CELLS = [(shape, key) for shape, keys in HIDDEN_FROM.items() for key in keys]


class _Vault:
    """A throwaway SQLite vault that holds one visible memory and, once ``hide`` is called, one hidden one."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, scoped: bool = False) -> None:
        self.user_id = str(uuid4())
        self.path = tmp_path / "vault.sqlite3"
        self.monkeypatch = monkeypatch
        monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
        monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
        monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
        bootstrap_database(self.path, user_id=self.user_id, user_email="synthetic@example.invalid")
        self.scope: tuple[str, ...] = ("alpha",) if scoped else ()
        with self.store() as store:
            self.visible = _memory(store, "visible", VISIBLE_TEXT, _scope(self.scope))
        self.context = MCPRuntimeContext(database_url=sqlite_url_for_path(self.path), user_id=UUID(self.user_id))

    @contextmanager
    def store(self) -> Iterator[SQLiteVNextStore]:
        with sqlite_user_connection(self.path, self.user_id) as conn:
            yield SQLiteVNextStore(conn, self.user_id)

    def key(self, name: str) -> str:
        profile, project = KEYS[name]
        with self.store() as store:
            _record, raw = create_agent_key(store, user_id=self.user_id, agent_id=name,
                                            permission_profile=profile, project_scope=project)
        return raw

    def hide(self, shape: str, text: str | None = None) -> dict[str, object]:
        with self.store() as store:
            return SHAPES[shape](store, self.scope, *([] if text is None else [text]))

    def call(self, key: str | None, tool: str, **arguments: object) -> dict[str, object]:
        if key:
            self.monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        else:
            self.monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        return call_mcp_tool(self.context, name=tool, arguments=arguments)

    def recall(self, key: str | None, **extra: object) -> dict[str, object]:
        return self.call(key, "alice_recall", query=QUERY, debug=True, **extra)

    def owner_reads(self, row: Mapping[str, object]) -> bool:
        """The control: the owner, asking for every sensitivity, reads the hidden row through the strict pass."""

        result = self.recall(None, sensitivity_allowed=list(ALL_SENSITIVITY))
        stage = result["retrieval"]["stages"]["fts"]  # type: ignore[index]
        return str(row["id"]) in {item["id"] for item in result["results"]} and stage["source"] == STRICT_PASS  # type: ignore[index, union-attr]


def _texts(result: Mapping[str, object]) -> list[object]:
    return [json.loads(item["text"]) for item in result["results"]]  # type: ignore[index, union-attr]


@pytest.mark.parametrize(("shape", "name"), CELLS)
def test_a_hidden_strict_hit_does_not_change_the_answer_or_the_trace_of_a_restricted_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str, name: str
) -> None:
    """Mutation: replace the condition that runs the OR pass after a strict pass that left no readable row
    (``if relaxable and not selected and "match_any" not in options and read_fence.entity_read_fenced:`` in
    ``VNextRetrievalService._memory_fts_rows``) with ``if False:``.

    The pass runs the strict pass and, on main before this change, stopped there because the hidden row matched, so
    the key got no result and the label ``sqlite_fts`` where the vault without the row gave the visible memory and
    ``sqlite_fts_or_fallback``. Every hidden shape is run against every key that may not read it: a row above the
    domain limits, one below a confidential source three memories deep (the SQL filter cannot see that far), one in
    another project, and one with an input that does not exist.
    """

    vault = _Vault(tmp_path, monkeypatch, scoped=KEYS[name][1] is not None)
    key = vault.key(name)
    before = vault.recall(key)
    assert _texts(before) == [VISIBLE_TEXT], before
    assert before["retrieval"]["stages"]["fts"]["source"] == OR_PASS  # type: ignore[index]

    hidden = vault.hide(shape)
    assert vault.owner_reads(hidden), "the hidden row must match every word, so that the strict pass finds it"
    after = vault.recall(key)

    assert after == before
    assert _texts(after) == [VISIBLE_TEXT]
    assert after["retrieval"]["stages"]["fts"] == {"source": OR_PASS, "candidate_count": 1}  # type: ignore[index]
    rendered = json.dumps(after, default=str)
    assert str(hidden["id"]) not in rendered and "hidden note" not in rendered


@pytest.mark.parametrize(("shape", "name"), [("chain", "read_only"), ("domain", "project_alpha"), ("project", "trusted_alpha")])
def test_a_hidden_strict_hit_does_not_change_the_context_pack_of_a_restricted_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str, name: str
) -> None:
    """Mutation: pass ``read_fence=SourceReadFence.unfenced()`` in the ``_memory_fts_rows`` call of
    ``compile_context_pack`` (the one beside ``query=request.query``).

    The pack runs the same stage, and its trace carries the stage label and the candidate count. Everything the key
    is shown is compared except the pack and trace ids, which are minted per call, and the BM25 score of a row
    (``fts_score``) with the token figures that count its printed length. On SQLite that score is computed over every
    row of the index, a row the key may not read included, so it is not the same with and without the hidden row; this
    test is about the fallback decision, and that dependence is a separate limit that this change does not touch.
    """

    vault = _Vault(tmp_path, monkeypatch, scoped=KEYS[name][1] is not None)
    key = vault.key(name)
    before = _pack(vault.call(key, "alice_context_pack", query=QUERY, debug=True))
    assert [item["title"] for item in before["memories"]] == [json.dumps("visible")]  # type: ignore[index, union-attr]

    hidden = vault.hide(shape)
    after = _pack(vault.call(key, "alice_context_pack", query=QUERY, debug=True))

    assert after == before
    assert str(hidden["id"]) not in json.dumps(after, default=str)


def _pack(result: dict[str, object], *, or_pass: str = OR_PASS) -> dict[str, object]:
    """The pack without what differs between two calls of one vault."""

    def scrub(value: object) -> object:
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items() if k not in {"context_pack_id", "trace_id", "token_report", "budget", "fts_score"}}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    scrubbed = scrub(result)
    assert isinstance(scrubbed, dict)
    stages = scrubbed["trace"]["stages"]  # type: ignore[index]
    assert stages["fts"]["source"] == or_pass
    return scrubbed


def _source_with_chunk(store: SQLiteVNextStore, key: str, text: str, *, domain: str = "project") -> dict[str, object]:
    source = _source(store, key, domain=domain)
    store.create_source_chunk({"source_id": source["id"], "chunk_index": 0, "text": text, "metadata_json": {}})
    return source


def test_a_hidden_source_that_matches_every_word_does_not_change_the_source_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: none for this file. The chunk stage runs the same strict-then-OR rule on sources, and a source has no
    effective label of its own (its stored domain and sensitivity are the label), so the store's own filters take a
    hidden source out before the rule counts it. This test keeps it so: it was checked, and nothing needed changing.

    A source in a restricted domain that matches both words must leave the visible source that matches one word, the
    ``sources`` of the recall and the whole trace of the pack, with its ``chunk_fts_source`` label, as they were.
    """

    vault = _Vault(tmp_path, monkeypatch)
    key = vault.key("read_only")
    with vault.store() as store:
        _source_with_chunk(store, "visible-source", "alpha only visible chunk text")
    recall_before = vault.recall(key)
    pack_before = _pack(vault.call(key, "alice_context_pack", query=QUERY, debug=True))
    assert recall_before["source_count"] == 1
    assert pack_before["trace"]["stages"]["sources"]["chunk_fts_source"] == OR_PASS  # type: ignore[index]

    with vault.store() as store:
        hidden = _source_with_chunk(store, "hidden-source", "alpha beta hidden chunk text", domain="health")
    recall_after = vault.recall(key)
    pack_after = _pack(vault.call(key, "alice_context_pack", query=QUERY, debug=True))

    assert recall_after == recall_before
    assert pack_after == pack_before
    assert str(hidden["id"]) not in json.dumps([recall_after, pack_after], default=str)


def test_a_readable_strict_hit_still_stops_the_or_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: replace ``and not selected`` in the condition that runs the OR pass for a caller with limits with
    ``and True``.

    A strict hit the key may read is a result. The OR pass must not run beside it, with or without a hidden strict hit
    in the vault: the visible memory that matches one word stays out.
    """

    vault = _Vault(tmp_path, monkeypatch)
    key = vault.key("read_only")
    with vault.store() as store:
        readable = _memory(store, "readable", "alpha beta readable note", {})
    before = vault.recall(key)
    assert [item["id"] for item in before["results"]] == [str(readable["id"])]  # type: ignore[index, union-attr]
    assert before["retrieval"]["stages"]["fts"]["source"] == STRICT_PASS  # type: ignore[index]

    vault.hide("chain")
    after = vault.recall(key)
    assert after == before


@pytest.mark.parametrize("name", ["read_only", "trusted", "admin_alpha"])
def test_a_restricted_key_that_may_read_the_strict_hit_gets_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Mutation: replace ``and not selected`` in the condition that runs the OR pass for a caller with limits with
    ``and True`` (the relaxed pass then runs beside a strict hit the key may read and adds the memory that matches one word).

    The shape is one this key can read: a row above the domain limits is readable by a trusted key and by a bound
    admin key, and a row of another project by an unbound key.
    """

    shape = "project" if name == "read_only" else "domain"
    assert name not in HIDDEN_FROM[shape]
    vault = _Vault(tmp_path, monkeypatch, scoped=KEYS[name][1] is not None)
    key = vault.key(name)
    hidden = vault.hide(shape)
    result = vault.recall(key)
    assert [item["id"] for item in result["results"]] == [str(hidden["id"])]  # type: ignore[index, union-attr]
    assert result["retrieval"]["stages"]["fts"]["source"] == STRICT_PASS  # type: ignore[index]


@pytest.mark.parametrize("name", [None, "admin"])
def test_the_owner_and_an_unbound_admin_key_keep_their_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str | None) -> None:
    """Mutation: drop ``and read_fence.entity_read_fenced`` from the condition that runs the OR pass after a strict
    pass that left no readable row.

    Neither has limits, so their request filters are a selection and not a limit. A strict hit that their own default
    filters drop (here a row below a confidential source, which the default sensitivities leave out) still stops the
    OR pass, as it did before this change, and the answer is the one they had: no result and the strict label.
    """

    vault = _Vault(tmp_path, monkeypatch)
    key = vault.key(name) if name else None
    hidden = vault.hide("chain")
    result = vault.recall(key)
    assert result["results"] == [] and result["count"] == 0
    assert result["retrieval"]["stages"]["fts"] == {"source": STRICT_PASS, "candidate_count": 0}  # type: ignore[index]
    # Asking for every sensitivity reads the row, with the strict label.
    everything = vault.recall(key, sensitivity_allowed=list(ALL_SENSITIVITY))
    assert [item["id"] for item in everything["results"]] == [str(hidden["id"])]  # type: ignore[index, union-attr]


def test_every_call_of_the_stage_carries_the_fence_of_the_caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: pass ``read_fence=SourceReadFence.unfenced()`` in the ``_memory_fts_rows`` call of ``_handle_alice_recall``
    (``mcp/retrieval.py``), in the main call of ``compile_context_pack`` or in the call of its coverage clause stage
    (``_fetch_clause_rows``).

    A fence that is the owner's on one of the three calls decides the OR pass by the old rule for a key. The query
    asks "how many", which splits into two clauses and runs the stage once for the query and once for each clause.
    """

    vault = _Vault(tmp_path, monkeypatch)
    key = vault.key("read_only")
    seen: list[tuple[str, SourceReadFence]] = []
    original = VNextRetrievalService._memory_fts_rows

    def spy(self: VNextRetrievalService, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        seen.append((str(kwargs["query"]), kwargs["read_fence"]))  # type: ignore[arg-type]
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(VNextRetrievalService, "_memory_fts_rows", spy)
    vault.call(key, "alice_recall", query="how many alpha notes and beta notes did I write")
    recall_calls = list(seen)
    seen.clear()
    vault.call(key, "alice_context_pack", query="how many alpha notes and beta notes did I write")
    pack_calls = list(seen)

    assert len(recall_calls) == 1
    assert len(pack_calls) == 3, [query for query, _fence in pack_calls]
    for query, fence in [*recall_calls, *pack_calls]:
        assert fence.identity is not None and fence.identity.permission_profile == "read_only_agent", query
        assert fence.entity_read_fenced, query


# --- the stage on stores of other shapes ----------------------------------------------------------------------------


def _row(row_id: str, text: str, sensitivity: str) -> dict[str, object]:
    return {"id": row_id, "canonical_text": text, "title": text, "status": "active", "domain": "project",
            "sensitivity": sensitivity, "metadata_json": {}}


HIDDEN_ROW = _row("11111111-1111-4111-8111-111111111111", "alpha beta hidden", "confidential")
VISIBLE_ROW = _row("22222222-2222-4222-8222-222222222222", "alpha visible", "public")
READ_ONLY = SourceReadFence.for_identity(AgentIdentity(agent_id="reader", agent_type="unknown", permission_profile="read_only_agent"))
OWNER = SourceReadFence.unfenced()


class _PagedStore:
    """A native full-text store that pages by offset and takes ``match_any``, over fixed strict and OR results."""

    fts_stage_source = "fake_fts"
    memory_fts_offset_paging = True

    def __init__(self, strict: list[dict[str, object]], relaxed: list[dict[str, object]]) -> None:
        self.strict, self.relaxed = strict, relaxed
        self.calls: list[bool] = []

    def search_memories_fts(self, *, query: str, domains=None, sensitivity_allowed=None, limit: int = 50,  # type: ignore[no-untyped-def]
                            offset: int = 0, match_any: bool = False, **filters: object) -> list[dict[str, object]]:
        self.calls.append(match_any)
        return list((self.relaxed if match_any else self.strict)[offset:offset + limit])


class _LegacyPrefixStore(_PagedStore):
    """The same over the prefix API of an older adapter: no ``offset``, so the stage deepens the prefix."""

    memory_fts_offset_paging = False

    def search_memories_fts(self, *, query: str, domains=None, sensitivity_allowed=None, limit: int = 50,  # type: ignore[no-untyped-def,override]
                            match_any: bool = False, **filters: object) -> list[dict[str, object]]:
        self.calls.append(match_any)
        return list((self.relaxed if match_any else self.strict)[:limit])


class _NoRelaxedPassStore(_PagedStore):
    """A store that predates ``match_any``: asking for the relaxed pass raises ``TypeError``."""

    def search_memories_fts(self, *, query: str, domains=None, sensitivity_allowed=None, limit: int = 50,  # type: ignore[no-untyped-def,override]
                            offset: int = 0) -> list[dict[str, object]]:
        self.calls.append(False)
        return list(self.strict[offset:offset + limit])


def _run(store: object, fence: SourceReadFence) -> tuple[list[str], str]:
    rows, source = VNextRetrievalService(store)._memory_fts_rows(  # type: ignore[arg-type]
        query=QUERY, domains=[], sensitivity_allowed=["public", "internal"], limit=5, read_fence=fence,
    )
    return [str(row["id"]) for row in rows], source


@pytest.mark.parametrize("store_class", [_PagedStore, _LegacyPrefixStore])
def test_a_caller_with_limits_gets_the_or_pass_when_the_strict_rows_are_all_hidden(store_class: type[_PagedStore]) -> None:
    """Mutation: the same replacement of the condition with ``if False:`` as the first test.

    A native store and a legacy prefix adapter take the same decision. The caller sees the fallback label.
    """

    store = store_class([HIDDEN_ROW], [HIDDEN_ROW, VISIBLE_ROW])
    assert _run(store, READ_ONLY) == ([str(VISIBLE_ROW["id"])], "fake_fts_or_fallback")
    assert store.calls == [False, True]


@pytest.mark.parametrize("store_class", [_PagedStore, _LegacyPrefixStore])
def test_the_owner_does_not_get_the_or_pass_after_a_strict_hit_of_its_own_filter(store_class: type[_PagedStore]) -> None:
    """Mutation: drop ``and read_fence.entity_read_fenced`` from the condition.

    The owner's filters hid the strict row. Nothing else changes for the owner, so the OR pass is not asked for.
    """

    store = store_class([HIDDEN_ROW], [HIDDEN_ROW, VISIBLE_ROW])
    assert _run(store, OWNER) == ([], "fake_fts")
    assert store.calls == [False]


def test_a_store_without_match_any_keeps_the_strict_label_when_the_relaxed_pass_is_refused() -> None:
    """Mutation: replace ``return [], source`` in the ``TypeError`` branch of the second relaxed pass with ``pass``
    (the branch after ``relaxed = relaxed_pass()`` in the caller-with-limits condition).

    The refusal of the keyword is not a failure of the stage. The caller gets nothing and the label of the pass that ran.
    """

    store = _NoRelaxedPassStore([HIDDEN_ROW], [])
    assert _run(store, READ_ONLY) == ([], "fake_fts")

