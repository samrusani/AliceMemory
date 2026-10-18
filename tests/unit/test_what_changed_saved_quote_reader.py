"""The reason of a what-changed step is a saved quote, and the saved-quote reader decides who is shown it.

A decision keeps the quote it was recorded with in its references, and a commit can cite a memory in one entry
(``"memory:<id>"``) and keep the words in the entry beside it (``{"quote": "..."}``). The reader that the context pack and
``alice_explain`` use (``SavedProvenanceReader``) judges a row by every reference it holds. The what-changed read used to
judge each entry alone, so the entry that holds only words looked clean and its quote came back as the reason after the
memory it quoted was made confidential. A key with limits must get nothing through this read that those other doors withhold.

These tests plant the shapes of the saved-quote catalogs on SQLite, read them through the HTTP route and the MCP tool as the
owner, as a declared identity and as a key of each profile, and compare the answer with what the other readers show the same
caller of the same row:

* ``SavedProvenanceReader.memory`` is the code both the pack and ``alice_explain`` call, so it is the oracle for every shape.
* The context pack (the full compile, not the compact tool) is the second oracle for the callers it can answer. It drops the
  reference list of a pack that has a project scope, so it is asked only for a caller with no project.
* ``alice_explain`` is the third, for a key. It applies the reader to a key and not to a payload identity on a keyless
  install, so a declared identity is compared with the pack and the reader only.
* ``alice_recall`` is not an oracle for a saved quote. Its result carries no reference of a memory at all, so it holds back
  every quote and a comparison with it can never fail.

Mutations (``scripts/derived_label_mutations.json`` replays them): in ``_reason`` return the first quote of the stored row
without the reader (the sibling-entry test fails); judge each entry alone, as before (the differential test fails); make the
reader an optional argument of ``_reason`` (the signature test fails).
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from alicebot_api import vnext_what_changed
from alicebot_api.mcp.context import _vnext_context_pack_payload
from alicebot_api.mcp.memories import redact_memory_flow
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.recall_framing import quote_session_brief_text
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_agent_keys import create_agent_key, resolve_agent_identity
from alicebot_api.vnext_source_fence import SavedProvenanceReader, SourceReadFence
from tests.unit.test_saved_quote_memory_refs import _spellings
from tests.unit.test_what_changed_review import (
    ALPHA,
    _catalog,
    _install_route,
    _memory,
    _path_store,
    _route,
    _source,
)
from tests.unit.what_changed_quote_support import (
    PACK_CALLERS,
    UNLIMITED,
    reasons_of,
    run_quote_differential,
)

MARK = "ZQXWC0001KEEL"
SECOND = "ZQXWC0002KEEL"
# The keys of the profile matrix. The owner and the declared identity have no key. ``admin`` and ``admin_bound`` are the only
# keys that read a confidential memory; ``admin`` has no limits at all.
KEYED = (
    ("read_only", "read_only_agent", None),
    ("memory_proposal", "memory_proposal_agent", None),
    ("scoped_unbound", "project_scoped_agent", None),
    ("scoped_bound", "project_scoped_agent", ALPHA),
    ("trusted", "trusted_local_agent", None),
    ("trusted_bound", "trusted_local_agent", ALPHA),
    ("admin", "admin_agent", None),
    ("admin_bound", "admin_agent", ALPHA),
)
READS_CONFIDENTIAL = {"owner", "admin", "admin_bound"}
DECLARED = {"agent_identity": {"agent_id": "declared-reader", "permission_profile": "read_only_agent"}}


def _tool(
    path: Path, user_id: str, key: str | None, arguments: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    if key:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=UUID(user_id))
    payload = call_mcp_tool(context, name="alice_what_changed", arguments=arguments)
    assert isinstance(payload, dict)
    return payload


_reasons = reasons_of


def _vault(tmp_path: Path, name: str) -> tuple[Path, str]:
    path = tmp_path / f"{name}.sqlite"
    user_id = str(uuid4())
    _path_store(path, user_id)
    return path, user_id


def _keys(path: Path, user_id: str) -> dict[str, str]:
    with sqlite_user_connection(path, user_id) as conn:
        store = SQLiteVNextStore(conn, user_id)
        return {
            name: create_agent_key(store, user_id=user_id, agent_id=name, permission_profile=profile, project_scope=project)[1]
            for name, profile, project in KEYED
        }


def _both_doors(
    path: Path, user_id: str, key: str | None, topic: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, object], dict[str, object]]:
    return _route(user_id, key, topic), _tool(path, user_id, key, {"topic": topic}, monkeypatch)


def _explained(
    path: Path, user_id: str, key: str | None, memory_id: str, monkeypatch: pytest.MonkeyPatch
) -> str | None:
    """The text ``alice_explain`` returns for this key, or None when it refuses the memory."""

    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    if key:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=UUID(user_id))
    try:
        return json.dumps(call_mcp_tool(context, name="alice_explain", arguments={"memory_id": memory_id}))
    except MCPToolError:
        return None


def _pack_rows(
    path: Path, user_id: str, key: str | None, query: str, monkeypatch: pytest.MonkeyPatch, *, declared: bool = False
) -> dict[str, Mapping[str, object]]:
    """The memory rows of the full context pack for this caller, keyed by id, as the pack hands them to the caller."""

    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    if key:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=UUID(user_id))
    arguments: dict[str, object] = {"query": query, "max_items": 50, **(DECLARED if declared else {})}
    pack = _vnext_context_pack_payload(context, arguments)
    return {str(row["id"]): row for row in pack["relevant_memories"]}  # type: ignore[union-attr]


# --- the sibling entry --------------------------------------------------------------------------------------------------


def test_a_quote_in_a_sibling_entry_of_a_memory_that_became_confidential_is_withheld(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit cites ``memory:<id>`` in one entry and keeps the words in the next. After the memory is made confidential,
    every key that may not read it is shown no quote at either door, and a key that may read it still is. The context pack
    and ``alice_explain`` give each caller the same answer about the same quote.

    Mutation: ``_reason`` takes the first quote of the stored refs without asking the saved-quote reader."""

    path, user_id = _vault(tmp_path, "sibling")
    with sqlite_user_connection(path, user_id) as conn:
        store = SQLiteVNextStore(conn, user_id)
        cited = _memory(store, title="ledger handbook", canonical_text="the handbook sentence", project_id=ALPHA,
                        metadata_json={"project_scope": [ALPHA]})
        decision = _memory(
            store, title="lantern policy", canonical_text="the lantern stays lit", project_id=ALPHA,
            metadata_json={"project_scope": [ALPHA],
                           "agentic_memory": {"source_refs": [f"memory:{cited['id']}", {"quote": MARK}]}},
        )
    _install_route(monkeypatch, path, user_id)
    shown = {"text": quote_session_brief_text(MARK), "source_id": None}
    step = str(decision["id"])

    def owner(*, route: bool) -> list[object]:
        """The owner has no key. The route refuses a call with no key once a key exists, so only the tool answers then."""

        answers = [_tool(path, user_id, None, {"topic": "lantern policy"}, monkeypatch)]
        if route:
            answers.insert(0, _route(user_id, None, "lantern policy"))
        return [_reasons(answer)[step] for answer in answers]

    def declared() -> object:
        return _reasons(_tool(path, user_id, None, {"topic": "lantern policy", **DECLARED}, monkeypatch))[step]

    assert owner(route=True) == [shown] * 2
    assert declared() == shown
    keys = _keys(path, user_id)

    def with_key(name: str) -> list[object]:
        return [_reasons(answer)[step] for answer in _both_doors(path, user_id, keys[name], "lantern policy", monkeypatch)]

    for name in keys:
        assert with_key(name) == [shown] * 2, name
    with sqlite_user_connection(path, user_id) as conn:
        SQLiteVNextStore(conn, user_id).update_memory(
            memory_id=str(cited["id"]), patch={"sensitivity": "confidential", "domain": "legal"}, actor_type="system"
        )
    assert owner(route=False) == [shown]
    for name in keys:
        expected = shown if name in READS_CONFIDENTIAL else None
        answers = with_key(name)
        assert answers == [expected, expected], name
        if expected is None:
            assert MARK not in json.dumps(_both_doors(path, user_id, keys[name], "lantern policy", monkeypatch)), name
        # The same caller, asked about the same memory by ``alice_explain``, is shown the quote exactly when this read shows it.
        explained = _explained(path, user_id, keys[name], step, monkeypatch)
        assert (explained is not None and MARK in explained) == (expected is not None), name
    after = _tool(path, user_id, None, {"topic": "lantern policy", **DECLARED}, monkeypatch)
    assert _reasons(after)[step] is None
    assert MARK not in json.dumps(after)
    # The pack is asked of every caller it answers with the reference list. It shows the quote to the owner and to the
    # unbound admin key, and to no other caller, which is the answer this read gives.
    for name in PACK_CALLERS:
        key = keys.get(name)
        rows = _pack_rows(path, user_id, key, "lantern policy", monkeypatch, declared=name == "declared")
        assert (MARK in json.dumps(rows[step], default=str)) == (name in UNLIMITED), name


@pytest.mark.parametrize("how", ["archived", "redacted"])
def test_a_quote_that_names_a_retired_memory_in_an_incidental_place_is_withheld(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """An id under ``origin`` may be a chunk id, so the reader refuses it only when it names a stored memory the caller may not
    read, and an archived or redacted memory is stored. The read used to look the id up among live memories only.

    Mutation: ``_reason`` takes the first quote of the stored refs without asking the saved-quote reader."""

    path, user_id = _vault(tmp_path, f"retired-{how}")
    with sqlite_user_connection(path, user_id) as conn:
        store = SQLiteVNextStore(conn, user_id)
        cited = _memory(store, title="ledger handbook", canonical_text="the handbook sentence", project_id=ALPHA,
                        metadata_json={"project_scope": [ALPHA]})
        decision = _memory(
            store, title="lantern policy", canonical_text="the lantern stays lit", project_id=ALPHA,
            metadata_json={"project_scope": [ALPHA],
                           "agentic_memory": {"source_refs": [{"quote": MARK, "origin": str(cited["id"])}]}},
        )
        control = _memory(
            store, title="lantern policy", canonical_text="the lantern stays dim", project_id=ALPHA,
            metadata_json={"project_scope": [ALPHA], "agentic_memory": {"source_refs": [{"quote": SECOND}]}},
        )
    _install_route(monkeypatch, path, user_id)
    keys = _keys(path, user_id)
    for name, key in keys.items():
        for answer in _both_doors(path, user_id, key, "lantern policy", monkeypatch):
            assert _reasons(answer)[str(decision["id"])] is not None, name
    with sqlite_user_connection(path, user_id) as conn:
        store = SQLiteVNextStore(conn, user_id)
        if how == "archived":
            store.update_memory(memory_id=str(cited["id"]), patch={"status": "archived"}, actor_type="system")
        else:
            assert redact_memory_flow(store, memory_id=str(cited["id"]), reason="synthetic")["status"] == "redacted"
    for name, key in keys.items():
        for answer in _both_doors(path, user_id, key, "lantern policy", monkeypatch):
            reasons = _reasons(answer)
            assert (reasons[str(decision["id"])] is not None) == (name in UNLIMITED), (how, name)
            assert reasons[str(control["id"])] == {"text": quote_session_brief_text(SECOND), "source_id": None}, name
            if name not in UNLIMITED:
                assert MARK not in json.dumps(answer), (how, name)
    for name in PACK_CALLERS:
        rows = _pack_rows(path, user_id, keys.get(name), "lantern policy", monkeypatch, declared=name == "declared")
        assert (MARK in json.dumps(rows[str(decision["id"])], default=str)) == (name in UNLIMITED), (how, name)
    keyless = _tool(path, user_id, None, {"topic": "lantern policy"}, monkeypatch)
    assert _reasons(keyless)[str(decision["id"])] is not None
    declared = _tool(path, user_id, None, {"topic": "lantern policy", **DECLARED}, monkeypatch)
    assert _reasons(declared)[str(decision["id"])] is None
    assert MARK not in json.dumps(declared)


def test_the_reason_builder_takes_the_reader_as_a_required_argument() -> None:
    """The reason builder has no default for the reader that carries the caller's fence, so a call that forgets it fails at
    the call and does not fall back to the stored row.

    Mutation: give ``saved`` a default in ``_reason``."""

    parameter = inspect.signature(vnext_what_changed._reason).parameters["saved"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


# --- the shared reader is the oracle ------------------------------------------------------------------------------------


def catalog_cases(public_id: str, hidden_id: str, secret_id: str, open_id: str) -> list[tuple[str, object]]:
    """Every citation shape of the legacy catalog and of the saved-quote catalogs, as (name, entry)."""

    cases = [(name, entry) for name, entry, _quote in _catalog(secret_id, hidden_id, public_id)]
    cases.extend((f"spelling-{label}", entry) for label, (entry, _names) in _spellings(secret_id, open_id, public_id).items())
    return cases


class _SqliteBackend:
    """The differential check on a SQLite file, read through the HTTP route function and the MCP tool."""

    def __init__(self, path: Path, user_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self.path = path
        self.user_id = user_id
        self.monkeypatch = monkeypatch

    def targets(self) -> tuple[str, str, str, str]:
        with sqlite_user_connection(self.path, self.user_id) as conn:
            store = SQLiteVNextStore(conn, self.user_id)
            public = _source(store, title="public price note", metadata_json={"project_scope": [ALPHA]})
            hidden = _source(store, title="confidential ledger note", domain="legal", sensitivity="confidential",
                             metadata_json={"project_scope": [ALPHA]})
            secret = _memory(store, title="confidential handbook", canonical_text="the confidential handbook sentence",
                             domain="legal", sensitivity="confidential", project_id=ALPHA,
                             metadata_json={"project_scope": [ALPHA]})
            open_memory = _memory(store, title="open handbook", canonical_text="the open handbook sentence", project_id=ALPHA,
                                  metadata_json={"project_scope": [ALPHA]})
        _install_route(self.monkeypatch, self.path, self.user_id)
        return tuple(str(row["id"]) for row in (public, hidden, secret, open_memory))  # type: ignore[return-value]

    def plant_many(self, decisions: list[tuple[str, str, list[object]]]) -> list[str]:
        with sqlite_user_connection(self.path, self.user_id) as conn:
            store = SQLiteVNextStore(conn, self.user_id)
            return [
                str(_memory(store, title=title, canonical_text=text, project_id=ALPHA,
                            metadata_json={"project_scope": [ALPHA], "agentic_memory": {"source_refs": refs}})["id"])
                for title, text, refs in decisions
            ]

    def make_keys(self) -> dict[str, str]:
        return _keys(self.path, self.user_id)

    def read(self, key: str | None, token: str, *, declared: bool) -> dict[str, dict[str, object]]:
        arguments = {"topic": token, **(DECLARED if declared else {})}
        out = {"tool": _tool(self.path, self.user_id, key, arguments, self.monkeypatch)}
        if not declared:
            out["route"] = _route(self.user_id, key, token)
        return out

    def pack(self, key: str | None, token: str, *, declared: bool) -> dict[str, Mapping[str, object]]:
        return _pack_rows(self.path, self.user_id, key, token, self.monkeypatch, declared=declared)

    def reader_rows(self, key: str | None, ids: list[str], *, declared: bool) -> list[Mapping[str, object]]:
        with sqlite_user_connection(self.path, self.user_id) as conn:
            store = SQLiteVNextStore(conn, self.user_id)
            if key is not None:
                identity = resolve_agent_identity(store, user_id=self.user_id, raw_key=key, payload={})
            else:
                identity = AgentIdentity.from_payload(DECLARED) if declared else None
            reader = SavedProvenanceReader(store, fence=SourceReadFence.for_identity(identity))
            rows = [store.get_memory(memory_id) for memory_id in ids]
            reader.memories(rows)
            return [reader.memory(row) for row in rows]


def test_the_reason_is_what_the_saved_quote_reader_leaves_in_the_refs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """For every citation shape of the legacy catalog and of the saved-quote catalogs, alone and with an entry of plain words
    before it and after it, the reason that a step shows is the first quote the reader leaves in the refs of the same row for
    the same caller, or null. It is read at both doors, as the owner, as a declared identity and as a key of each profile.
    For each caller the full context pack answers, the pack shows the same quote of the same row. No planted quote is ever
    shown that the reader removed. The catalog holds shapes that the reader keeps for some callers and removes for others, so
    the test fails if either side of that is empty.

    Mutations: ``_reason`` reads the stored row and not the reader's (the sibling layouts fail for a caller that may not read
    the memory); ``_reason`` judges each entry alone (the same layouts fail).
    """

    path, user_id = _vault(tmp_path, "differential")
    results = run_quote_differential(_SqliteBackend(path, user_id, monkeypatch), catalog_cases)
    both = [counts for who, counts in results.items() if who not in UNLIMITED]
    assert all(shown > 0 and kept > 0 for shown, kept in both), results
    assert results["owner"][1] < results["read_only"][1]


def test_a_quote_shown_by_the_read_is_shown_by_explain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``alice_explain`` shows the saved quotes of a memory to a key that may read what it cites and refuses the rest. For a
    memory with one saved quote, the what-changed read shows that quote to a key exactly when explain does. A key bound to a
    project is shown the explain answer through the reader, so the check holds for those too.

    Mutation: ``_reason`` takes the first quote of the stored refs without asking the saved-quote reader."""

    path, user_id = _vault(tmp_path, "explain")
    with sqlite_user_connection(path, user_id) as conn:
        store = SQLiteVNextStore(conn, user_id)
        public = _source(store, title="public price note", metadata_json={"project_scope": [ALPHA]})
        hidden = _source(store, title="confidential ledger note", domain="legal", sensitivity="confidential",
                         metadata_json={"project_scope": [ALPHA]})
        secret = _memory(store, title="confidential handbook", canonical_text="the confidential handbook sentence",
                         domain="legal", sensitivity="confidential", project_id=ALPHA, metadata_json={"project_scope": [ALPHA]})
        shapes = {
            "own words": [{"source_id": str(public["id"]), "quote": "OWNWORDSKEEL"}],
            "hidden source": [{"source_id": str(hidden["id"]), "quote": "HIDDENSRCKEEL"}],
            "hidden memory": [{"memory_id": str(secret["id"]), "quote": "HIDDENMEMKEEL"}],
            "hidden memory, words beside": [f"memory:{secret['id']}", {"quote": "BESIDEKEEL"}],
            "hidden source, words beside": [{"source_id": str(hidden["id"])}, {"quote": "BESIDESRCKEEL"}],
            "words only": [{"quote": "WORDSONLYKEEL"}],
        }
        decisions = {}
        for name, refs in shapes.items():
            decisions[name] = _memory(
                store, title=f"explain keel {name}", canonical_text=f"explain keel decision {name}", project_id=ALPHA,
                metadata_json={"project_scope": [ALPHA], "agentic_memory": {"source_refs": refs}},
            )
    _install_route(monkeypatch, path, user_id)
    keys = _keys(path, user_id)
    quotes = {"own words": "OWNWORDSKEEL", "hidden source": "HIDDENSRCKEEL", "hidden memory": "HIDDENMEMKEEL",
              "hidden memory, words beside": "BESIDEKEEL", "hidden source, words beside": "BESIDESRCKEEL",
              "words only": "WORDSONLYKEEL"}
    shown_to: dict[str, set[str]] = {}
    for who, key in keys.items():
        reasons = _reasons(_route(user_id, key, "explain keel"))
        for name, row in decisions.items():
            reason = reasons[str(row["id"])]
            explained = _explained(path, user_id, key, str(row["id"]), monkeypatch)
            explain_shows = explained is not None and quotes[name] in explained
            assert (reason is not None) == explain_shows, (who, name, reason, explained is not None)
            if reason is not None:
                shown_to.setdefault(name, set()).add(who)
    # The shapes that carry something to withhold are shown to some keys and held back from the rest.
    for name in ("hidden source", "hidden memory", "hidden memory, words beside", "hidden source, words beside"):
        assert 0 < len(shown_to.get(name, set())) < len(keys), name
    assert shown_to["words only"] == set(keys) and shown_to["own words"] == set(keys)
