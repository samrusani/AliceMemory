"""The correction label must not name a memory the caller cannot read.

`annotate_derived_memory_correction` walks `superseded_by` and stamps
`current_memory_id` on a packed source excerpt. A pointer id is itself
sensitive metadata, so every memory the walk touches has to pass the same
fence the caller's memory reads run under.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest

from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.surface_flags import MCP_FULL_TOOLS_ENV
from alicebot_api.vnext_agent_keys import create_agent_key

USER_ID = "00000000-0000-0000-0000-000000000001"
OLD = "The kettle is stored on the third shelf."
NEW = "The kettle is stored on the first shelf."


def _context(tmp_path: Path, monkeypatch) -> MCPRuntimeContext:
    monkeypatch.delenv(AGENT_API_KEY_ENV, raising=False)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _store(context, fn):
    from alicebot_api.mcp_tools import _sqlite_path_from_url

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


def _captured(context, monkeypatch):
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
    _store(context, lambda s: s.update_memory(memory_id=memory_id, patch=patch, actor_type="user"))


def _unquoted(value: object) -> str:
    text = str(value or "")
    if text.startswith('"'):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            return text
        return loaded if isinstance(loaded, str) else text
    return text


def _source(payload: dict) -> dict:
    rows = [row for row in payload.get("sources") or [] if OLD in _unquoted(row.get("excerpt"))]
    assert rows, "the stale excerpt was not returned; the assertions below would be vacuous"
    return rows[0]


def _recall_source(context, key) -> dict:
    return _source(_call(context, "alice_recall", {"query": OLD}, key=key))


def _pack_source(context, key) -> dict:
    return _source(_call(context, "alice_context_pack", {"query": OLD}, key=key, full=True))


def test_label_names_the_successor_when_it_is_inside_the_fence(tmp_path: Path, monkeypatch) -> None:
    """Positive control. Mutation: make the visibility predicate always False.

    Every id is suppressed and the label never names the successor.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context, monkeypatch)
    replacement = _supersede(context, memory_id)
    reader = _mint(context, profile="read_only_agent", project="acme")
    for source in (_recall_source(context, reader), _pack_source(context, reader)):
        assert source["derived_memory_corrected"] is True
        assert source["current_memory_id"] == replacement


def test_recall_hides_a_successor_above_the_sensitivity_ceiling(tmp_path: Path, monkeypatch) -> None:
    """The recall label withholds the id of a memory above the caller's ceiling.

    Mutation: build the recall predicate as always True, ignore sensitivity in it,
    or drop the label instead of only the id (the flag assertion fails).
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context, monkeypatch)
    replacement = _supersede(context, memory_id)
    _set(context, replacement, sensitivity="confidential")
    reader = _mint(context, profile="read_only_agent", project="acme")
    source = _recall_source(context, reader)
    assert source["derived_memory_corrected"] is True, "the stale label must survive; only the id is withheld"
    assert "current_memory_id" not in source
    assert replacement not in json.dumps(_call(context, "alice_recall", {"query": OLD}, key=reader), default=str)


def test_pack_hides_a_successor_above_the_sensitivity_ceiling(tmp_path: Path, monkeypatch) -> None:
    """Same fence on the pack's own call sequence.

    Mutation: build the pack-site predicate as always True in `compile_context_pack`.
    Recall's predicate is a separate object, so this is killed here and only here.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context, monkeypatch)
    replacement = _supersede(context, memory_id)
    _set(context, replacement, sensitivity="confidential")
    reader = _mint(context, profile="read_only_agent", project="acme")
    source = _pack_source(context, reader)
    assert source["derived_memory_corrected"] is True
    assert "current_memory_id" not in source


def test_recall_hides_an_in_place_corrected_memory_above_the_ceiling(tmp_path: Path, monkeypatch) -> None:
    """No walk at all: the derived memory itself is hidden.

    Mutation: skip the visibility check on the first hop of `_current_memory_id`.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context, monkeypatch)
    _call(
        context,
        "alice_memory_correct",
        {"action": "edit-and-approve", "review_item_id": memory_id, "body": {"text": NEW}, "reason": "moved"},
        full=True,
    )
    _set(context, memory_id, sensitivity="confidential")
    reader = _mint(context, profile="read_only_agent", project="acme")
    source = _recall_source(context, reader)
    assert source["derived_memory_corrected"] is True
    assert "current_memory_id" not in source


def test_recall_hides_a_chain_whose_last_hop_is_hidden(tmp_path: Path, monkeypatch) -> None:
    """M -> R1 (visible) -> R2 (hidden). The id must be withheld, not downgraded to R1.

    Mutation: check the first hop only, or fall back to a visible earlier hop.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context, monkeypatch)
    first = _supersede(context, memory_id)
    second = _supersede(context, first, "The kettle is stored on the second shelf.")
    _set(context, second, sensitivity="confidential")
    reader = _mint(context, profile="read_only_agent", project="acme")
    source = _recall_source(context, reader)
    assert "current_memory_id" not in source
    assert first not in json.dumps(source) and second not in json.dumps(source)


def test_recall_hides_an_id_from_a_reader_bound_to_another_project(tmp_path: Path, monkeypatch) -> None:
    """The source now belongs to project beta; the memories stay in acme.

    Mutation: make the predicate ignore project scope, or build it with no scope at the
    recall site.
    """
    context = _context(tmp_path, monkeypatch)
    source_id, memory_id = _captured(context, monkeypatch)
    replacement = _supersede(context, memory_id)

    def move(store):
        row = store.get_source(source_id)
        metadata = dict(row.get("metadata_json") or {})
        metadata["project_id"] = "beta"
        metadata["project_scope"] = ["beta"]
        store.update_source(source_id=source_id, patch={"metadata_json": metadata}, actor_type="user")

    _store(context, move)
    beta = _mint(context, profile="read_only_agent", project="beta")
    source = _recall_source(context, beta)
    assert source["derived_memory_corrected"] is True
    assert "current_memory_id" not in source
    assert replacement not in json.dumps(source)


def test_a_dangling_successor_is_not_named() -> None:
    """A pointer that cannot be resolved cannot be proven visible.

    Mutation: return the raw successor id when `get_memory` finds nothing, or when the
    store has no `get_memory` at all.
    """
    from alicebot_api.vnext_retrieval import _current_memory_id

    class Store:
        def get_memory(self, memory_id):
            return None

    class StoreWithoutLookup:
        pass

    memory = {"id": "m1", "status": "superseded", "superseded_by": "gone"}
    assert _current_memory_id(Store(), memory, memory_visible=lambda _row: True) == ""
    assert _current_memory_id(StoreWithoutLookup(), memory, memory_visible=lambda _row: True) == ""


def test_the_visibility_control_has_no_default() -> None:
    """A defaulted control is 'no fence' for the next caller that forgets it.

    Mutation: give `memory_visible` a default on any of the three functions.
    """
    from alicebot_api import vnext_retrieval as module

    for func in (
        module.annotate_derived_memory_correction,
        module._current_memory_id,
        module.VNextRetrievalService._packable_source,
    ):
        parameter = inspect.signature(func).parameters["memory_visible"]
        assert parameter.default is inspect.Parameter.empty, func.__qualname__
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def _chain(length: int) -> dict[str, dict]:
    """n0 is superseded by n1, and so on. The last row is the current, active one."""

    rows: dict[str, dict] = {}
    for index in range(length):
        last = index == length - 1
        rows[f"n{index}"] = {
            "id": f"n{index}",
            "status": "active" if last else "superseded",
            "superseded_by": None if last else f"n{index + 1}",
        }
    return rows


class _ChainStore:
    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = rows

    def get_memory(self, memory_id):
        return self.rows.get(memory_id)


def test_the_depth_cap_does_not_name_an_unchecked_row() -> None:
    """The row the walk ends on after eight fetches is checked like every other hop.

    Nine rows: the walk takes eight steps and then holds the ninth, which is current.
    It is named when visible and withheld when hidden. Mutation: return the ninth row
    without checking it, or check it only for being superseded.
    """
    from alicebot_api.vnext_retrieval import _current_memory_id

    rows = _chain(9)
    store = _ChainStore(rows)
    assert _current_memory_id(store, rows["n0"], memory_visible=lambda _row: True) == "n8"
    assert _current_memory_id(store, rows["n0"], memory_visible=lambda row: row["id"] != "n8") == ""
    # A hidden hop inside the walk is withheld the same way.
    assert _current_memory_id(store, rows["n0"], memory_visible=lambda row: row["id"] != "n7") == ""


def test_the_depth_cap_ends_the_walk_without_naming_a_stale_row() -> None:
    """Twelve visible hops: the row the cap stops on is still superseded, so nothing is named.

    Mutation: return the id of the row the walk ends on when it is visible and
    superseded. That id is a stale fact, and naming it is the error this label exists
    to prevent.
    """
    from alicebot_api.vnext_retrieval import _current_memory_id

    rows = _chain(12)
    store = _ChainStore(rows)
    assert _current_memory_id(store, rows["n0"], memory_visible=lambda _row: True) == ""
    assert _current_memory_id(store, rows["n0"], memory_visible=lambda row: row["id"] != "n8") == ""


def test_recall_hides_a_successor_outside_the_requested_domains(tmp_path: Path, monkeypatch) -> None:
    """The domain half of the fence. Fails if the predicate checks sensitivity and not domain.

    The reader asks for the ``project`` domain. The successor is now a
    ``professional`` memory, which that request's memory stages would not return.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context, monkeypatch)
    replacement = _supersede(context, memory_id)
    _set(context, replacement, domain="professional")
    reader = _mint(context, profile="read_only_agent", project="acme")
    narrowed = _source(_call(context, "alice_recall", {"query": OLD, "domains": ["project"]}, key=reader))
    assert narrowed["derived_memory_corrected"] is True
    assert "current_memory_id" not in narrowed
    open_fence = _recall_source(context, reader)
    assert open_fence["current_memory_id"] == replacement, "the control: no domain filter names it"


def _pack(context, query: str, **request_fields) -> dict:
    from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService

    return _store(
        context,
        lambda s: VNextRetrievalService(s).compile_context_pack(
            VNextRetrievalRequest(query=query, **request_fields)
        ),
    )


def _pack_memory(pack: dict, memory_id: str) -> dict:
    rows = [row for row in pack["relevant_memories"] if row.get("id") == memory_id]
    assert rows, "the successor was not packed; the assertions below would be vacuous"
    return rows[0]


def test_pack_drops_a_supersedes_pointer_to_a_predecessor_above_the_ceiling(
    tmp_path: Path, monkeypatch
) -> None:
    """The pointer sanitizer applies the sensitivity ceiling with no scope active.

    The successor is visible and its ``supersedes`` names a predecessor the
    caller's ceiling hides. Mutation: drop the visibility check in
    ``_sanitize_memory_scope_pointers``, or return early when the scope is not
    active. The pointer and the validity label built from it come back.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context, monkeypatch)
    replacement = _supersede(context, memory_id)
    _set(context, memory_id, sensitivity="confidential")

    hidden = _pack_memory(_pack(context, NEW), replacement)
    assert "supersedes" not in hidden
    assert "supersedes_memory_id" not in (hidden.get("validity") or {})

    allowed = _pack_memory(
        _pack(context, NEW, sensitivity_allowed=("public", "internal", "private", "confidential", "unknown")),
        replacement,
    )
    assert allowed.get("supersedes") == memory_id, "the control: a caller allowed to read it keeps the pointer"
    assert (allowed.get("validity") or {}).get("supersedes_memory_id") == memory_id


def test_pack_treats_a_pointer_it_cannot_resolve_as_it_did_before(tmp_path: Path, monkeypatch) -> None:
    """A pointer to no row is not a hidden row, so the fence change leaves it alone.

    A scoped pack fails closed on it, as before. An unscoped pack keeps it, and the
    high-depth `supersession_context` shows it as an id-only reference on purpose.

    Mutation: drop an unresolvable pointer on an unscoped pack. Mutation: keep one on a
    scoped pack.
    """
    context = _context(tmp_path, monkeypatch)
    _source_id, memory_id = _captured(context, monkeypatch)
    replacement = _supersede(context, memory_id)
    dead = "00000000-0000-4000-8000-00000000dead"
    _set(context, replacement, supersedes=dead)

    assert _pack_memory(_pack(context, NEW), replacement).get("supersedes") == dead
    scoped = _pack_memory(_pack(context, NEW, projects=("acme",)), replacement)
    assert "supersedes" not in scoped


def test_the_predicate_applies_people_and_time_scope_like_the_memory_stages() -> None:
    """The fence is the stages' own: a person link counts, a time window bounds.

    Mutation: drop `person_linked_memory_ids` from the `_row_matches_scope` call in
    `_memory_visibility_predicate`, or skip the scope when a window is the only filter.
    """
    from datetime import UTC, datetime

    from alicebot_api.vnext_retrieval import _memory_visibility_predicate, _ResolvedRetrievalScope

    people = _ResolvedRetrievalScope(
        projects=frozenset(), people=frozenset({"ada"}), window_start=None, window_end=None
    )
    visible = _memory_visibility_predicate(
        domains=[],
        sensitivity_allowed=["public", "private"],
        scope=people,
        person_linked_memory_ids=frozenset({"linked"}),
    )
    assert visible({"id": "linked", "domain": "project", "sensitivity": "private"})
    assert not visible({"id": "stranger", "domain": "project", "sensitivity": "private"})
    assert not visible({"id": "linked", "domain": "project", "sensitivity": "confidential"})

    window = _ResolvedRetrievalScope(
        projects=frozenset(),
        people=frozenset(),
        window_start=datetime(2026, 1, 1, tzinfo=UTC),
        window_end=datetime(2026, 2, 1, tzinfo=UTC),
    )
    in_window = _memory_visibility_predicate(
        domains=[], sensitivity_allowed=["private"], scope=window, person_linked_memory_ids=frozenset()
    )
    assert in_window({"id": "a", "sensitivity": "private", "created_at": "2026-01-15T00:00:00Z"})
    assert not in_window({"id": "b", "sensitivity": "private", "created_at": "2026-03-15T00:00:00Z"})
    unscoped = _memory_visibility_predicate(
        domains=[], sensitivity_allowed=["private"], scope=None, person_linked_memory_ids=frozenset()
    )
    assert unscoped({"id": "c", "sensitivity": "private", "created_at": "2020-01-01T00:00:00Z"})
