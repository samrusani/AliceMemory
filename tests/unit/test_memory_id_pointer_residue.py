"""Three memory-id pointer defects that were left after the v0.19.1 and v0.19.2 fences.

1. ``current_memory_id`` on a corrected passage named a successor that was
   forgotten, undone or rejected afterwards. The walk now ends on a row that is
   still live, or names no id.
2. The context pack derived ``validity`` after it dropped a ``superseded_by``
   pointer to a row the caller cannot read, so a memory that was still active
   lost ``validity.superseded`` in the pack while recall kept it.
3. A memory id copied into a stored memory's ``metadata_json`` came back whole
   in the typed sections of ``alice_context_pack`` with ``debug: true``,
   whatever the caller may read.

Every test names the mutation that must fail it. The mutation runs are made by
hand and restored from a saved copy of the file.
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
from pathlib import Path

import pytest

from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext, _sqlite_path_from_url
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.surface_flags import MCP_FULL_TOOLS_ENV
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_source_fence import SourceReadFence

USER_ID = "00000000-0000-0000-0000-000000000001"
OLD = "The kettle is stored on the third shelf."
NEW = "The kettle is stored on the first shelf."
MIDDLE = "The kettle is stored on the second shelf."
ALL_LEVELS = ("public", "internal", "private", "confidential", "unknown")


def _context(tmp_path: Path, monkeypatch) -> MCPRuntimeContext:
    monkeypatch.delenv(AGENT_API_KEY_ENV, raising=False)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _store(context, fn):
    with sqlite_user_connection(_sqlite_path_from_url(context.database_url), USER_ID) as conn:
        return fn(SQLiteVNextStore(conn, USER_ID))


def _call(context, name, arguments, *, key=None, full=False):
    from alicebot_api.mcp.registry import call_mcp_tool

    if key is None:
        os.environ.pop(AGENT_API_KEY_ENV, None)
    else:
        os.environ[AGENT_API_KEY_ENV] = key
    if full:
        os.environ[MCP_FULL_TOOLS_ENV] = "1"
    else:
        os.environ.pop(MCP_FULL_TOOLS_ENV, None)
    try:
        return call_mcp_tool(context, name=name, arguments=arguments)
    finally:
        os.environ.pop(AGENT_API_KEY_ENV, None)
        os.environ.pop(MCP_FULL_TOOLS_ENV, None)


def _mint(context, *, profile: str, project: str) -> str:
    _record, raw = _store(
        context,
        lambda s: create_agent_key(
            s, user_id=USER_ID, agent_id=f"{profile}-{project}", permission_profile=profile, project_scope=project
        ),
    )
    return raw


def _captured(context) -> tuple[str, str]:
    writer = _mint(context, profile="project_scoped_agent", project="acme")
    captured = _call(
        context,
        "alice_capture",
        {"raw_text": OLD, "title": "Shelf note", "domain": "project", "sensitivity": "public"},
        key=writer,
        full=True,
    )
    review = _call(context, "alice_memory_review", {"status": "all", "limit": 20}, full=True)
    memory_id = next(str(i["id"]) for i in review["items"] if OLD in json.dumps(i, default=str))
    return str(captured["source_id"]), memory_id


def _supersede(context, memory_id: str, text: str = NEW) -> str:
    done = _call(
        context,
        "alice_memory_correct",
        {
            "action": "supersede-existing",
            "review_item_id": memory_id,
            "replacement_title": "Kettle",
            "replacement_body": {"text": text},
            "reason": "moved",
        },
        full=True,
    )
    return str(done["replacement_object"]["id"])


def _set(context, memory_id: str, **patch) -> None:
    _store(context, lambda s: s.update_memory(memory_id=memory_id, patch=patch, actor_type="user", label_write=True))


def _add_metadata(context, memory_id: str, **keys) -> None:
    def merge(store):
        row = store.get_memory(memory_id)
        metadata = dict(row["metadata_json"])
        metadata.update(keys)
        store.update_memory(memory_id=memory_id, patch={"metadata_json": metadata}, actor_type="user", label_write=True)

    _store(context, merge)


def _soft_delete(context, memory_id: str) -> None:
    with sqlite3.connect(_sqlite_path_from_url(context.database_url)) as conn:
        conn.execute("UPDATE memories SET deleted_at = '2026-01-01T00:00:00Z' WHERE id = ?", (memory_id,))


def _unquoted(value: object) -> str:
    text = str(value or "")
    if text.startswith('"'):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            return text
        return loaded if isinstance(loaded, str) else text
    return text


def _stale_source(payload: dict) -> dict:
    rows = [row for row in payload.get("sources") or [] if OLD in _unquoted(row.get("excerpt"))]
    assert rows, "the stale excerpt was not returned; the assertions below would be vacuous"
    return rows[0]


def _recall_source(context, key=None) -> dict:
    return _stale_source(_call(context, "alice_recall", {"query": OLD}, key=key))


def _pack_source(context, key=None) -> dict:
    return _stale_source(_call(context, "alice_context_pack", {"query": OLD}, key=key, full=True))


def _retire(context, memory_id: str, how: str) -> None:
    if how == "forget":
        _call(context, "alice_memory_manage", {"action": "forget", "memory_id": memory_id}, full=True)
    elif how == "undo":
        _call(context, "alice_memory_manage", {"action": "undo", "memory_id": memory_id}, full=True)
    elif how == "reject":
        _call(
            context,
            "alice_memory_correct",
            {"action": "reject", "review_item_id": memory_id, "reason": "wrong"},
            full=True,
        )
    else:  # pragma: no cover - guards the parametrisation
        raise AssertionError(how)


# -- 1. the label names a successor that was retired ------------------------------


@pytest.mark.parametrize("how", ["forget", "undo", "reject"])
def test_label_names_no_id_after_the_successor_is_retired(tmp_path: Path, monkeypatch, how: str) -> None:
    """Recall and the pack stop naming a successor once it is forgotten, undone or rejected.

    The first loop proves the label names the successor while it is live, so the
    second loop is not vacuous. The keyless owner reads both. Mutation: return the
    id of the row the walk ends on without checking that it is live, that is
    `return memory_id` in the two branches of `_current_memory_id` that
    `_memory_was_retired` guards. Forget and undo fail through the no-pointer
    branch, reject through the not-superseded branch. An archived row is soft-deleted
    by the store, so it ends the walk unresolved and is covered by the deleted test.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context)
    replacement = _supersede(context, memory_id)
    for source in (_recall_source(context), _pack_source(context)):
        assert source["current_memory_id"] == replacement

    _retire(context, replacement, how)

    for source in (_recall_source(context), _pack_source(context)):
        assert source["derived_memory_corrected"] is True, "the passage is still stale; only the id goes"
        assert "current_memory_id" not in source
        assert replacement not in json.dumps(source)


def test_label_names_no_id_when_the_last_of_two_successors_was_forgotten(tmp_path: Path, monkeypatch) -> None:
    """M -> R1 -> R2 with R2 forgotten names neither R2 nor R1.

    R1 is itself superseded, so it is not the fact to read either. Mutation: when the
    last row is retired, fall back to the newest row before it that is visible.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context)
    first = _supersede(context, memory_id, MIDDLE)
    second = _supersede(context, first)
    assert _recall_source(context)["current_memory_id"] == second
    _retire(context, second, "forget")
    source = _recall_source(context)
    assert source["derived_memory_corrected"] is True
    assert "current_memory_id" not in source
    assert first not in json.dumps(source) and second not in json.dumps(source)


def test_label_names_no_id_when_the_derived_memory_itself_was_forgotten(tmp_path: Path, monkeypatch) -> None:
    """No chain at all: the memory the passage was quoted for is the one that was forgotten.

    Mutation: return the id of the starting row when it has no `superseded_by`
    pointer, without asking whether it was retired.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context)
    _retire(context, memory_id, "forget")
    for source in (_recall_source(context), _pack_source(context)):
        assert source["derived_memory_corrected"] is True
        assert "current_memory_id" not in source
        assert memory_id not in json.dumps(source)


def test_label_names_no_id_when_the_successor_was_deleted(tmp_path: Path, monkeypatch) -> None:
    """A soft-deleted successor is not returned by `get_memory`, so the walk ends unresolved.

    This held before the change and is pinned here with the other ways a successor
    leaves. Mutation: return the raw pointer id when `get_memory` finds nothing.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context)
    replacement = _supersede(context, memory_id)
    _soft_delete(context, replacement)
    for source in (_recall_source(context), _pack_source(context)):
        assert source["derived_memory_corrected"] is True
        assert "current_memory_id" not in source


def test_label_still_names_a_live_successor_in_the_fence(tmp_path: Path, monkeypatch) -> None:
    """Positive control for the tests above, with a successor that is live and readable.

    Mutation: make `_memory_was_retired` return True for every row. The id is never named.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context)
    replacement = _supersede(context, memory_id)
    reader = _mint(context, profile="read_only_agent", project="acme")
    for source in (_recall_source(context, reader), _pack_source(context, reader)):
        assert source["current_memory_id"] == replacement


def _row(row_id: str, status: str, superseded_by: str | None = None) -> dict:
    return {"id": row_id, "status": status, "superseded_by": superseded_by}


class _Rows:
    def __init__(self, *rows: dict) -> None:
        self.rows = {row["id"]: row for row in rows}

    def get_memory(self, memory_id):
        return self.rows.get(memory_id)


def test_walk_ends_on_a_live_row_only() -> None:
    """The walk by statuses, with no store behind it.

    A forgotten or undone row has status superseded and no pointer. A rejected or
    archived row has no pointer either. `stale` and `needs_review` are live statuses
    and are named as before. Mutation: delete the `_memory_was_retired` checks, or
    narrow `RETIRED_STATUSES` to the superseded status alone.
    """
    from alicebot_api.vnext_retrieval import _current_memory_id

    def visible(_row) -> bool:
        return True

    for status in ("rejected", "archived"):
        store = _Rows(_row("a", "superseded", "b"), _row("b", status))
        assert _current_memory_id(store, store.rows["a"], memory_visible=visible) == "", status
        assert _current_memory_id(store, store.rows["b"], memory_visible=visible) == "", status
    forgotten = _Rows(_row("a", "superseded", "b"), _row("b", "superseded"))
    assert _current_memory_id(forgotten, forgotten.rows["a"], memory_visible=visible) == ""
    assert _current_memory_id(forgotten, forgotten.rows["b"], memory_visible=visible) == ""
    for status in ("active", "accepted", "stale", "needs_review"):
        live = _Rows(_row("a", "superseded", "b"), _row("b", status))
        assert _current_memory_id(live, live.rows["a"], memory_visible=visible) == "b", status


def test_walk_does_not_name_a_retired_row_after_the_depth_cap() -> None:
    """Nine rows, eight hops, and the ninth row is rejected: it is not named.

    The row the walk holds when the cap ends it is checked like every other hop.
    Mutation: drop `_memory_was_retired` from the check after the loop.
    """
    from alicebot_api.vnext_retrieval import _current_memory_id

    rows = [_row(f"n{i}", "superseded", f"n{i + 1}") for i in range(8)] + [_row("n8", "rejected")]
    store = _Rows(*rows)
    assert _current_memory_id(store, store.rows["n0"], memory_visible=lambda _row: True) == ""
    rows[-1] = _row("n8", "active")
    live = _Rows(*rows)
    assert _current_memory_id(live, live.rows["n0"], memory_visible=lambda _row: True) == "n8"


# -- 2. the pack keeps validity.superseded for a pointer it drops ------------------


def _pack(context, query: str, **request_fields) -> dict:
    from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService

    return _store(
        context,
        lambda s: VNextRetrievalService(s).compile_context_pack(
            VNextRetrievalRequest(query=query, **request_fields),
            source_fence=SourceReadFence.unfenced()
        ),
    )


def _pack_memory(pack: dict, memory_id: str) -> dict:
    rows = [row for row in pack["relevant_memories"] if row.get("id") == memory_id]
    assert rows, "the memory was not packed; the assertions below would be vacuous"
    return rows[0]


def _active_row_pointing_at_a_hidden_successor(context) -> tuple[str, str]:
    """The old row is still active and its pointer names a successor above the ceiling."""
    _source_id, memory_id = _captured(context)
    replacement = _supersede(context, memory_id)
    _set(context, replacement, sensitivity="confidential")
    _set(context, memory_id, status="active")
    return memory_id, replacement


def test_pack_keeps_superseded_true_without_the_id_like_recall(tmp_path: Path, monkeypatch) -> None:
    """A still-active row with a pointer to a hidden row is superseded in the pack and in recall.

    Both name no id. The two `validity` objects are the same object. Mutation: do not
    pass `superseded_pointer_withheld` at the `_validity_annotation` call in
    `compile_context_pack`, or stop `_sanitize_memory_scope_pointers` returning the
    ids whose `superseded_by` it dropped. The pack then has no `validity`.
    """
    context = _context(tmp_path, monkeypatch)
    memory_id, replacement = _active_row_pointing_at_a_hidden_successor(context)
    reader = _mint(context, profile="read_only_agent", project="acme")
    for key in (None, reader):
        recall = _call(context, "alice_recall", {"query": OLD}, key=key)
        recalled = next(row for row in recall["results"] if row["id"] == memory_id)
        pack = _call(context, "alice_context_pack", {"query": OLD}, key=key, full=True)
        packed = next(row for row in pack["memories"] if row["id"] == memory_id)
        assert recalled["validity"]["superseded"] is True
        assert packed["validity"]["superseded"] is True
        assert packed["validity"] == recalled["validity"]
        assert "superseded_by_memory_id" not in packed["validity"]
        assert "superseded_by" not in packed
        assert replacement not in json.dumps(pack) and replacement not in json.dumps(recall)


def test_pack_names_the_id_for_a_caller_allowed_to_read_it(tmp_path: Path, monkeypatch) -> None:
    """The control: with the ceiling raised the pointer and its id are both kept.

    Mutation: build the predicate in `_sanitize_memory_scope_pointers` as always
    False, so every pointer is dropped whatever the caller may read. The flag stays,
    so this fails on the pointer and the id.
    """
    context = _context(tmp_path, monkeypatch)
    memory_id, replacement = _active_row_pointing_at_a_hidden_successor(context)
    packed = _pack_memory(_pack(context, OLD, sensitivity_allowed=ALL_LEVELS), memory_id)
    assert packed["superseded_by"] == replacement
    assert packed["validity"]["superseded"] is True
    assert packed["validity"]["superseded_by_memory_id"] == replacement


def test_pack_keeps_superseded_when_the_domain_filter_hides_the_successor(tmp_path: Path, monkeypatch) -> None:
    """The domain half of the fence. The successor is in a domain the request leaves out.

    Mutation: build the predicate in `_sanitize_memory_scope_pointers` with
    `domains=[]`. The pointer then stays and the id comes back with it.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context)
    replacement = _supersede(context, memory_id)
    _set(context, replacement, domain="professional")
    _set(context, memory_id, status="active")
    packed = _pack_memory(_pack(context, OLD, domains=("project",)), memory_id)
    assert "superseded_by" not in packed
    assert packed["validity"]["superseded"] is True
    assert "superseded_by_memory_id" not in packed["validity"]


def test_a_pointer_that_names_no_row_still_gives_the_id_on_an_unscoped_pack(tmp_path: Path, monkeypatch) -> None:
    """Unchanged: a pointer to no row is not a hidden row, so an unscoped pack keeps it.

    Mutation: return a withheld id for every dropped or kept pointer, or drop an
    unresolvable pointer on an unscoped pack.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context)
    dead = "00000000-0000-4000-8000-00000000dead"
    _set(context, memory_id, status="active", superseded_by=dead)
    packed = _pack_memory(_pack(context, OLD), memory_id)
    assert packed["superseded_by"] == dead
    assert packed["validity"]["superseded_by_memory_id"] == dead


def test_validity_marks_a_withheld_pointer_superseded_and_names_no_id() -> None:
    """The helper on its own. Withheld is not the same as no pointer.

    Mutation: ignore `superseded_pointer_withheld`, or let it write the id.
    """
    from alicebot_api.vnext_retrieval import _validity_annotation

    row = {"id": "m1", "status": "active", "superseded_by": None}
    assert _validity_annotation(dict(row)) is None
    assert _validity_annotation(dict(row), superseded_pointer_withheld=True) == {"superseded": True}
    kept = _validity_annotation({**row, "superseded_by": "m2"}, superseded_pointer_withheld=True)
    assert kept == {"superseded": True, "superseded_by_memory_id": "m2"}


def test_the_pointer_helper_reports_what_it_dropped() -> None:
    """`_drop_pointers_outside_fence` returns each (holder, key) it removed, and only those.

    Mutation: return the keys it examined, or return nothing.
    """
    from alicebot_api.vnext_retrieval import _drop_pointers_outside_fence

    rows = {"hidden": {"id": "hidden"}, "shown": {"id": "shown"}}
    holder: dict = {"a": "hidden", "b": "shown", "c": "missing"}
    dropped = _drop_pointers_outside_fence(
        [holder],
        ("a", "b", "c"),
        targets=rows,
        memory_visible=lambda row: row["id"] != "hidden",
        fail_closed_when_unresolved=False,
    )
    assert [key for _holder, key in dropped] == ["a"]
    assert holder == {"b": "shown", "c": "missing"}


# -- 3. ids copied into metadata_json in the debug sections -------------------------

HIDDEN_TEXT = "The deploy budget is private to the board."
DECISION_TEXT = "We decided the deploy window is Tuesday morning."
OTHER_ID = "11111111-2222-4333-8444-555555555555"
TYPED_SECTION = {"decision": "decisions", "procedure": "procedures", "belief": "relevant_beliefs"}


def _typed_memory_and_hidden_memory(context, memory_type: str) -> tuple[str, str]:
    """A readable memory of the given type, and a memory above the ceiling."""
    writer = _mint(context, profile="project_scoped_agent", project="acme")

    def commit(title: str, text: str, kind: str) -> str:
        done = _call(
            context,
            "alice_memory_commit",
            {
                "title": title,
                "canonical_text": text,
                "memory_type": kind,
                "domain": "project",
                "sensitivity": "public",
            },
            key=writer,
            full=True,
        )
        return str(done["memory"]["id"])

    typed = commit("Deploy note", DECISION_TEXT, memory_type)
    hidden = commit("Board note", HIDDEN_TEXT, "semantic")
    _set(context, hidden, sensitivity="confidential")
    return typed, hidden


def _debug_pack(context, *, key=None, **arguments) -> dict:
    return _call(
        context,
        "alice_context_pack",
        {"query": "deploy window Tuesday", "debug": True, **arguments},
        key=key,
        full=True,
    )


@pytest.mark.parametrize("memory_type", ["decision", "procedure", "belief"])
def test_debug_sections_carry_no_hidden_id_from_metadata(tmp_path: Path, monkeypatch, memory_type: str) -> None:
    """The keyless owner and a read-only key get no id of a memory above their ceiling.

    The id is planted six ways: as a value, in a list, behind a `memory:` prefix, in
    uppercase, inside a longer string and as a key. Mutation: make the predicate
    always True, or skip `_drop_hidden_memory_ids_from_metadata` at its call site in
    `compile_context_pack`. Dropping any one shape fails its assertion below: the
    whole-string case, the `memory:` prefix, the case fold, the replacement inside a
    longer string, or the key case.
    """
    context = _context(tmp_path, monkeypatch)
    typed, hidden = _typed_memory_and_hidden_memory(context, memory_type)
    _add_metadata(
        context,
        typed,
        related_memory=hidden,
        merged_from=[hidden, OTHER_ID],
        reference=f"memory:{hidden}",
        shouting=hidden.upper(),
        note=f"see {hidden} for the budget",
        nested={"deeper": [{"memory_id": hidden, "kept": "yes"}], hidden: "keyed by the id"},
    )
    reader = _mint(context, profile="read_only_agent", project="acme")
    for key in (None, reader):
        pack = _debug_pack(context, key=key)
        assert hidden not in json.dumps(pack) and hidden.upper() not in json.dumps(pack)
        section = pack[TYPED_SECTION[memory_type]]
        row = next(item for item in section if item["id"] == typed)
        metadata = row["metadata_json"]
        assert "related_memory" not in metadata
        assert metadata["merged_from"] == [OTHER_ID]
        assert "reference" not in metadata and "shouting" not in metadata
        assert metadata["note"] == "see (id withheld) for the budget"
        assert metadata["nested"] == {"deeper": [{"kept": "yes"}]}
        assert metadata["agent_id"] == "project_scoped_agent-acme", "the rest of the row is left alone"


def test_debug_sections_keep_the_id_for_a_caller_allowed_to_read_it(tmp_path: Path, monkeypatch) -> None:
    """The control: the same ids stay when the request's ceiling covers the memory.

    Mutation: strip every id whether or not it is visible. The fence is the only
    thing that decides.
    """
    context = _context(tmp_path, monkeypatch)
    typed, hidden = _typed_memory_and_hidden_memory(context, "decision")
    _add_metadata(context, typed, related_memory=hidden, note=f"see {hidden} for the budget")
    pack = _debug_pack(context, sensitivity_allowed=list(ALL_LEVELS))
    row = next(item for item in pack["decisions"] if item["id"] == typed)
    assert row["metadata_json"]["related_memory"] == hidden
    assert row["metadata_json"]["note"] == f"see {hidden} for the budget"


def test_debug_sections_keep_ids_that_name_no_memory_and_the_visible_ones(tmp_path: Path, monkeypatch) -> None:
    """Only a hidden memory's id goes. A source id, an unknown UUID and a visible memory's id stay.

    A captured memory carries the id of its source and of its chunk in its metadata, so
    this is the check that the scan does not take them out. Mutation: strip every
    UUID-shaped string, or treat an id that resolves to nothing as hidden.
    """
    context = _context(tmp_path, monkeypatch)
    source_id, memory_id = _captured(context)
    _set(context, memory_id, memory_type="decision", status="active")
    _typed, hidden = _typed_memory_and_hidden_memory(context, "decision")
    _add_metadata(context, memory_id, related_memory=hidden, unknown=OTHER_ID, sibling=_typed)
    before = _store(context, lambda s: s.get_memory(memory_id))["metadata_json"]
    assert before["source_id"] == source_id and before["source_chunk_id"]
    pack = _call(context, "alice_context_pack", {"query": OLD, "debug": True}, full=True)
    metadata = next(item for item in pack["decisions"] if item["id"] == memory_id)["metadata_json"]
    assert "related_memory" not in metadata
    assert metadata["source_id"] == source_id
    assert metadata["source_chunk_id"] == before["source_chunk_id"]
    assert metadata["sibling"] == _typed
    assert metadata["unknown"] == OTHER_ID


def test_metadata_ids_follow_the_requested_domains(tmp_path: Path, monkeypatch) -> None:
    """The pack's own predicate carries the requested domains.

    The target is readable under the ceiling but is in a domain the request leaves out.
    Mutation: build `pack_memory_visible` in `compile_context_pack` with `domains=[]`.
    """
    context = _context(tmp_path, monkeypatch)
    typed, hidden = _typed_memory_and_hidden_memory(context, "decision")
    _set(context, hidden, sensitivity="public", domain="professional")
    _add_metadata(context, typed, related_memory=hidden)
    narrowed = _debug_pack(context, domains=["project"])
    assert "related_memory" not in next(i for i in narrowed["decisions"] if i["id"] == typed)["metadata_json"]
    open_fence = _debug_pack(context)
    assert next(i for i in open_fence["decisions"] if i["id"] == typed)["metadata_json"]["related_memory"] == hidden


def test_metadata_ids_follow_the_project_scope(tmp_path: Path, monkeypatch) -> None:
    """A reader bound to project acme does not get the id of a memory that belongs to beta.

    The keyless owner has no project scope, so the id of the beta memory stays for the
    owner. Mutation: build the pack predicate with `scope=None`, or ignore the project
    in `_row_matches_scope`.
    """
    context = _context(tmp_path, monkeypatch)
    typed, other = _typed_memory_and_hidden_memory(context, "decision")
    _set(context, other, sensitivity="public")
    _add_metadata(context, other, project_scope=["beta"], project_id="beta")
    _add_metadata(context, typed, related_memory=other)
    reader = _mint(context, profile="read_only_agent", project="acme")
    scoped = _debug_pack(context, key=reader)
    assert "related_memory" not in next(i for i in scoped["decisions"] if i["id"] == typed)["metadata_json"]
    owner = _debug_pack(context)
    assert next(i for i in owner["decisions"] if i["id"] == typed)["metadata_json"]["related_memory"] == other


def test_a_realistic_supersession_copy_does_not_leak_into_the_debug_section(tmp_path: Path, monkeypatch) -> None:
    """The copy the supersession flow writes itself: `metadata_json.supersedes` names the old row.

    The replacement becomes a decision memory and the old row is above the ceiling.
    The `supersedes` column was already fenced, and its copy in the metadata was not.
    Mutation: drop the call to `_drop_hidden_memory_ids_from_metadata`.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context)
    replacement = _supersede(context, memory_id)
    _set(context, replacement, memory_type="decision")
    _set(context, memory_id, sensitivity="confidential")
    row = _store(context, lambda s: s.get_memory(replacement))
    assert row["metadata_json"].get("supersedes") == memory_id, "the planted copy is there to begin with"
    pack = _call(context, "alice_context_pack", {"query": NEW, "debug": True}, full=True)
    assert memory_id not in json.dumps(pack)
    decision = next(item for item in pack["decisions"] if item["id"] == replacement)
    assert "supersedes" not in decision["metadata_json"]


def test_the_compiled_pack_is_fenced_not_only_the_debug_view(tmp_path: Path, monkeypatch) -> None:
    """The service returns every row whole, and the CLI, the HTTP route and the legacy
    `alice_vnext_context_pack` tool return that pack, so the fence sits where it is built.

    The memory is a decision, so it is in `relevant_memories` and in `decisions` as
    the same object. Mutation: take the call to `_drop_hidden_memory_ids_from_metadata`
    out of `compile_context_pack`, which is what moving the fence into the debug
    presentation in `mcp/context.py` amounts to. The compiled pack then names the id.
    """
    context = _context(tmp_path, monkeypatch)
    typed, hidden = _typed_memory_and_hidden_memory(context, "decision")
    _add_metadata(context, typed, related_memory=hidden, merged_from=[hidden])
    for depth in ("minimal", "low", "medium", "high"):
        pack = _pack(context, "deploy window Tuesday", context_depth=depth)
        assert hidden not in json.dumps(pack, default=str), depth
        assert _pack_memory(pack, typed)["metadata_json"].get("merged_from") == [], depth
    allowed = _pack(context, "deploy window Tuesday", sensitivity_allowed=ALL_LEVELS)
    assert _pack_memory(allowed, typed)["metadata_json"]["related_memory"] == hidden


def test_the_metadata_fence_is_required_and_keyword_only() -> None:
    """A defaulted fence is 'no fence' for the next caller that forgets it.

    Mutation: give `memory_visible` a default on `_drop_hidden_memory_ids_from_metadata`.
    """
    from alicebot_api.vnext_retrieval import VNextRetrievalService

    parameter = inspect.signature(VNextRetrievalService._drop_hidden_memory_ids_from_metadata).parameters[
        "memory_visible"
    ]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


class _MemoryStore:
    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = rows
        self.asked: list[list[str]] = []

    def get_memories_by_ids(self, memory_ids):
        self.asked.append(list(memory_ids))
        return [self.rows[memory_id] for memory_id in memory_ids if memory_id in self.rows]


def test_metadata_scan_looks_ids_up_once_and_leaves_a_clean_row_alone() -> None:
    """One lookup for the whole pack, and a row with nothing hidden keeps its own object.

    Mutation: look each id up on its own, or rebuild `metadata_json` when nothing was
    removed. The rebuild would copy every packed row for nothing.
    """
    from alicebot_api.vnext_retrieval import VNextRetrievalService

    shown, hidden = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    store = _MemoryStore({shown: {"id": shown, "sensitivity": "public"}, hidden: {"id": hidden, "sensitivity": "secret"}})
    clean_metadata = {"link": shown}
    memories = [
        {"id": "m1", "metadata_json": {"link": hidden, "also": shown}},
        {"id": "m2", "metadata_json": clean_metadata},
        {"id": "m3", "metadata_json": {"list": [hidden, shown]}},
    ]
    service = VNextRetrievalService(store)  # type: ignore[arg-type]
    service._drop_hidden_memory_ids_from_metadata(
        memories, memory_visible=lambda row: row["sensitivity"] == "public"
    )
    assert store.asked == [[shown, hidden]]
    assert memories[0]["metadata_json"] == {"also": shown}
    assert memories[2]["metadata_json"] == {"list": [shown]}

    store.asked.clear()
    untouched = [{"id": "m4", "metadata_json": clean_metadata}]
    service._drop_hidden_memory_ids_from_metadata(untouched, memory_visible=lambda row: True)
    assert untouched[0]["metadata_json"] is clean_metadata


def test_metadata_scan_skips_text_that_is_not_an_id_and_stops_at_the_depth_cap() -> None:
    """Postgres raises on a non-uuid lookup, so only UUID-shaped text is looked up. Deep JSON is cut.

    Mutation: look every string up, or remove the depth cap. A 5,000 level nest would
    then raise `RecursionError` instead of returning.
    """
    from alicebot_api.vnext_retrieval import VNextRetrievalService

    hidden = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    store = _MemoryStore({hidden: {"id": hidden, "sensitivity": "secret"}})
    deep: object = {"leaf": hidden}
    for _ in range(5000):
        deep = {"down": deep}
    memories = [{"id": "m1", "metadata_json": {"text": "not an id", "number": 7, "flag": True, "deep": deep}}]
    VNextRetrievalService(store)._drop_hidden_memory_ids_from_metadata(  # type: ignore[arg-type]
        memories, memory_visible=lambda row: False
    )
    assert store.asked == []
    metadata = memories[0]["metadata_json"]
    assert metadata["text"] == "not an id" and metadata["number"] == 7 and metadata["flag"] is True
    assert hidden not in json.dumps(metadata), "text below the cap cannot be scanned, so it is dropped"
    levels = 0
    node = metadata["deep"]
    while node:
        node = node["down"]
        levels += 1
    assert 60 <= levels <= 70
