"""The grounding of the context pack and the brief of ``alice_resume`` do not depend on rows the caller may not read.

Unreleased (on main, not in v0.20.0). Two readers answered a key with limits from a read that cut first and checked the
caller's labels afterwards, or that asked the store for stored labels only:

* The grounding block of the context pack lists the names of the query that no memory, source or entity supports. Its
  memory probe asked the full-text search for one row. A row that the key's limits hide by the labels of its inputs (its
  stored labels are within the limits, so the store's filters cannot take it out) came back as that one row, so a name
  that only such a row mentions read as supported, and the key could tell whether a hidden row mentions a word.
* ``alice_resume`` read the newest decision, and the newest next-action memory, with a limit of one and dropped the rows
  the key may not read afterwards, so a newer hidden row turned an older readable one into nothing. Its recent changes
  cut the events to the limit before the same drop.

A caller with limits now gets the first row, and the first events, that it may read. The owner and an unbound admin key
have no limits and keep the reads they had.

The vault tests run on a real SQLite vault with real minted keys through the MCP tool registry. Each records the answer
of a key, adds a hidden row that is newer than the readable ones, and asks again: the answers must be equal. A control
reads the hidden row as the owner, so a pass means the key was held back and not that the row never matched.

Each test names the mutation that must fail it, and the cases ``grounding-*`` and ``resume-*`` of
``scripts/derived_label_mutations.json`` replay them.
"""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.project_identity import detect_project
from alicebot_api.project_view import (
    project_first_fill,
    project_first_fill_readable,
    readable_prefix,
)
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_grounding import corpus_support
from alicebot_api.vnext_label_sql import READABLE_SCAN_CAP
from alicebot_api.vnext_label_writes import without_insert_floor
from alicebot_api.vnext_project_scope import GLOBAL_PROJECT_MARKER
from tests.unit.per_project_s2_support import USER_ID, add_loop, add_memory, context_for, db_path_for, repo_with_remote
from tests.unit.per_project_view_support import project_view
from tests.unit.test_recall_fallback_hidden_rows import HIDDEN_FROM, KEYS, VISIBLE_TEXT, _memory, _scope, _source, _Vault

NAMES_QUERY = "What do we know about Zorblax and Quuxington?"
NAME_TEXT = "Zorblax secret hidden record"
BOTH_UNSUPPORTED = {"checked": 2, "unsupported_entities": ["Zorblax", "Quuxington"]}
ONE_UNSUPPORTED = {"checked": 2, "unsupported_entities": ["Quuxington"]}

# A key bound to a project gets no grounding block (the pack has a project scope), so the grounding cells are the unbound keys.
GROUNDING_CELLS = [
    (shape, name) for shape, keys in HIDDEN_FROM.items() for name in keys if KEYS[name][1] is None
]


# -- grounding ------------------------------------------------------------------------------------------------------


def _grounding(vault: _Vault, key: str | None) -> tuple[object, object]:
    pack = vault.call(key, "alice_context_pack", query=NAMES_QUERY, debug=True)
    return pack.get("grounding"), pack["trace"].get("grounding")  # type: ignore[union-attr]


@pytest.mark.parametrize(("shape", "name"), GROUNDING_CELLS)
def test_a_hidden_row_that_mentions_a_name_does_not_change_the_grounding_of_a_restricted_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str, name: str
) -> None:
    """Mutation: replace ``if readable_rows is not None:`` in ``_probe_rows`` with ``if False:``, or pass
    ``readable_rows=None`` for the memory probe in ``corpus_support``.

    The names of the query are Zorblax and Quuxington and nothing in the vault mentions either. A hidden row that
    mentions Zorblax is then added, below a restricted domain, a confidential source three memories deep, or an input
    that does not exist. The one-row probe returned that row, so Zorblax read as supported and the block listed
    Quuxington alone. The control reads the row as the owner: for the owner Zorblax is supported.
    """

    vault = _Vault(tmp_path, monkeypatch)
    key = vault.key(name)
    before = _grounding(vault, key)
    assert before == (BOTH_UNSUPPORTED, BOTH_UNSUPPORTED)

    hidden = vault.hide(shape, NAME_TEXT)
    assert _grounding(vault, None) == (ONE_UNSUPPORTED, ONE_UNSUPPORTED), "the row must mention the name for the owner"
    after = _grounding(vault, key)

    assert after == before
    assert str(hidden["id"]) not in json.dumps(vault.call(key, "alice_context_pack", query=NAMES_QUERY, debug=True), default=str)


def test_a_readable_row_that_mentions_a_name_still_supports_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: make the memory probe for a caller with limits keep no row (``kept = []`` in
    ``_first_readable_memory_row``).

    A row the key may read is a mention. The name is supported for the key, as it was.
    """

    vault = _Vault(tmp_path, monkeypatch)
    key = vault.key("read_only")
    with vault.store() as store:
        _memory(store, "readable-mention", NAME_TEXT, {})
    assert _grounding(vault, key) == (ONE_UNSUPPORTED, ONE_UNSUPPORTED)


def test_a_hidden_row_ahead_of_a_readable_one_does_not_hide_the_mention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: replace ``room = READABLE_SCAN_CAP - len(first)`` in ``_first_readable_memory_row`` with ``room = 0``.

    Three hidden rows rank ahead of the one readable row that mentions the name, and the probe asks for one row first.
    The probe must go on to the rows after it.
    """

    vault = _Vault(tmp_path, monkeypatch)
    key = vault.key("read_only")
    with vault.store() as store:
        _memory(store, "readable-mention", "Zorblax readable record", {})
    for tag in ("one", "two", "three"):
        with vault.store() as store:
            parent = _source(store, "parent-" + tag, domain="health")
            _memory(store, "hidden-" + tag, "Zorblax Zorblax Zorblax hidden record " + tag, {"source_id": str(parent["id"])})
    assert _grounding(vault, key) == (ONE_UNSUPPORTED, ONE_UNSUPPORTED)


# The ceiling the grounding check judges with is the caller's own: a trusted key reads the private level and every domain,
# so a derived mention at either is a mention for it. Each test below puts the mention on one side of one limit.
CEILING_CELLS = [
    pytest.param("project", "private", id="private-level"),
    pytest.param("health", "public", id="health-domain"),
    pytest.param("health", "private", id="private-level-in-health"),
]


@pytest.mark.parametrize(("parent_domain", "parent_sensitivity"), CEILING_CELLS)
def test_a_derived_mention_the_trusted_key_may_read_supports_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent_domain: str, parent_sensitivity: str
) -> None:
    """Mutation: pass ``sensitivity_allowed=["public"]`` to ``admit_loaded`` in the grounding call of
    ``compile_context_pack`` (the private cases fail), and, in another run, ``domains=["project"]`` (the health cases fail).

    A trusted key may read the private sensitivity and every domain. The mention is a derived row whose source is at the
    level and in the domain named, so it reaches the check with stored labels that are public and in project and is judged
    on the labels of its input. For the trusted key the name is supported; the control reads the same vault as the owner.
    """

    vault = _Vault(tmp_path, monkeypatch)
    key = vault.key("trusted")
    with vault.store() as store:
        parent = _source(store, "mention-parent", domain=parent_domain, sensitivity=parent_sensitivity)
        _memory(store, "mention", NAME_TEXT, {"source_id": str(parent["id"])})
    assert _grounding(vault, None) == (ONE_UNSUPPORTED, ONE_UNSUPPORTED)
    assert _grounding(vault, key) == (ONE_UNSUPPORTED, ONE_UNSUPPORTED)


@pytest.mark.parametrize(("parent_domain", "parent_sensitivity"), [("project", "confidential"), ("project", "highly_sensitive")])
def test_a_derived_mention_above_the_trusted_keys_ceiling_does_not_support_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent_domain: str, parent_sensitivity: str
) -> None:
    """Mutation: replace ``readable_memory_rows=(`` in the grounding call of ``compile_context_pack`` with a check that keeps
    every row (``readable_memory_rows=lambda rows: rows,``).

    The same shape, one level above what a trusted key may read: the name is as unsupported as in a vault without the row.
    """

    vault = _Vault(tmp_path, monkeypatch)
    key = vault.key("trusted")
    with vault.store() as store:
        parent = _source(store, "mention-parent", domain=parent_domain, sensitivity=parent_sensitivity)
        _memory(store, "mention", NAME_TEXT, {"source_id": str(parent["id"])})
    everything = vault.call(None, "alice_context_pack", query=NAMES_QUERY, debug=True, sensitivity_allowed=list(ALL_SENSITIVITY))
    assert everything["grounding"] == ONE_UNSUPPORTED, "the row must be a mention for the owner who asks for every level"
    assert _grounding(vault, key) == (BOTH_UNSUPPORTED, BOTH_UNSUPPORTED)


@pytest.mark.parametrize("name", ["admin_alpha", "trusted_alpha", "read_only_alpha"])
def test_a_key_bound_to_a_project_gets_no_grounding_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Mutation: replace ``if depth != CONTEXT_DEPTH_MINIMAL and not scope.active:`` in ``compile_context_pack`` with
    ``if depth != CONTEXT_DEPTH_MINIMAL:``.

    A pack with a project scope has no grounding block, so for a bound admin key (or any bound key) there is no ceiling to
    judge a mention with. The test keeps that so: if a bound key gets a block, the ceiling cases above need a bound variant.
    """

    vault = _Vault(tmp_path, monkeypatch, scoped=True)
    key = vault.key(name)
    pack = vault.call(key, "alice_context_pack", query=NAMES_QUERY, debug=True)
    assert "grounding" not in pack


@pytest.mark.parametrize("name", [None, "admin"])
def test_the_owner_and_an_unbound_admin_key_keep_the_grounding_they_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str | None
) -> None:
    """Mutation: replace ``if source_fence.entity_read_fenced`` in the grounding call of ``compile_context_pack``
    with ``if True``.

    Neither has limits. A row below a confidential source (their own default filters leave such a row out of a
    list) still counts as a mention, as it did: the probe is the one-row probe.
    """

    vault = _Vault(tmp_path, monkeypatch)
    key = vault.key(name) if name else None
    assert _grounding(vault, key) == (BOTH_UNSUPPORTED, BOTH_UNSUPPORTED)
    vault.hide("chain", NAME_TEXT)
    assert _grounding(vault, key) == (ONE_UNSUPPORTED, ONE_UNSUPPORTED)


class _ProbeStore:
    """A store that answers the OR probe from a fixed list and records what it was asked."""

    memory_fts_offset_paging = True

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[tuple[int, int, bool]] = []
        self.readable_by: list[object] = []

    def search_memories_fts(self, *, query, domains=None, sensitivity_allowed=None, limit=50, offset=0, match_any=False, readable_by=None):  # type: ignore[no-untyped-def]
        self.calls.append((limit, offset, match_any))
        self.readable_by.append(readable_by)
        return self.rows[offset:offset + limit]


class _PrefixProbeStore(_ProbeStore):
    """The same for an adapter that takes no ``offset``: the second read is a longer prefix."""

    memory_fts_offset_paging = False

    def search_memories_fts(self, *, query, domains=None, sensitivity_allowed=None, limit=50, match_any=False):  # type: ignore[no-untyped-def,override]
        self.calls.append((limit, 0, match_any))
        return self.rows[:limit]


class _StrictProbeStore(_ProbeStore):
    """A store that predates ``match_any``: the probe falls back to the strict single-name call."""

    def search_memories_fts(self, *, query, domains=None, sensitivity_allowed=None, limit=50, offset=0):  # type: ignore[no-untyped-def,override]
        self.calls.append((limit, offset, False))
        return self.rows[offset:offset + limit]


def _rows(hidden: int, readable: int) -> list[dict[str, object]]:
    return [{"id": f"hidden-{i}", "hidden": True} for i in range(hidden)] + [{"id": f"readable-{i}"} for i in range(readable)]


def _readable(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    return [dict(row) for row in rows if not row.get("hidden")]


CAP = READABLE_SCAN_CAP


def test_the_probe_reads_past_the_first_row_to_the_first_readable_one() -> None:
    """Mutation: replace ``room = READABLE_SCAN_CAP - len(first)`` in ``_first_readable_memory_row`` with ``room = 0``.

    100 hidden rows come before the readable one. The probe asks for one row, as the owner's probe does, and then once for
    the rest of the rows up to the cap, from the offset after the first.
    """

    store = _ProbeStore(_rows(100, 1))
    assert corpus_support(["Zorblax"], store, readable_memory_rows=_readable) == {"Zorblax": True}
    assert store.calls == [(1, 0, True), (CAP - 1, 1, True)]


def test_the_probe_ends_when_the_matches_run_out_and_none_is_readable() -> None:
    """Mutation: replace ``return [] if len(later) < room else [_PROBE_GAVE_UP]`` in ``_first_readable_memory_row`` with
    ``return [_PROBE_GAVE_UP]``.

    Every match is hidden and the matches end before the cap: the name has no readable mention, which is the answer of a
    vault without those rows.
    """

    store = _ProbeStore(_rows(3, 0))
    assert corpus_support(["Zorblax"], store, readable_memory_rows=_readable) == {"Zorblax": False}
    assert store.calls == [(1, 0, True), (CAP - 1, 1, True)]


def test_the_probe_stops_at_the_cap_and_reads_the_name_as_supported() -> None:
    """Mutation: replace ``return [] if len(later) < room else [_PROBE_GAVE_UP]`` in ``_first_readable_memory_row`` with
    ``return []``.

    Hidden matches fill the cap and more follow, so the probe has not seen the last match. It reads two times and no more, and
    the name reads as supported, because the module claims that a name is unmentioned only when it has looked at every
    match. The readable row after the cap is not looked for.
    """

    store = _ProbeStore(_rows(CAP * 3, 1))
    assert corpus_support(["Zorblax"], store, readable_memory_rows=_readable) == {"Zorblax": True}
    assert store.calls == [(1, 0, True), (CAP - 1, 1, True)]


def test_the_owner_probe_is_still_one_row() -> None:
    """Mutation: pass ``readable_rows=readable_memory_rows`` for every caller (``memory_probe = True``).

    The owner and an unbound admin key leave ``readable_memory_rows`` unset: one call for one row, no second read.
    """

    store = _ProbeStore(_rows(5, 1))
    assert corpus_support(["Zorblax"], store) == {"Zorblax": True}
    assert store.calls == [(1, 0, True)]
    assert store.readable_by == [None]


def test_the_probe_of_a_caller_with_limits_gives_the_store_its_limits() -> None:
    """Mutation: drop ``filters["readable_by"] = readable_by`` in ``_first_readable_memory_row``.

    A store that names ``readable_by`` leaves out, before its limit, the rows a direct input hides from the caller, so the
    hidden rows do not use up the cap. A store that does not name it (the strict store above) is not given it.
    """

    store = _ProbeStore(_rows(0, 1))
    assert corpus_support(["Zorblax"], store, domains=("project",), sensitivity_allowed=("public",),
                          readable_memory_rows=_readable) == {"Zorblax": True}
    assert store.readable_by == [(("project",), ("public",))]


def test_an_adapter_without_offset_is_asked_for_a_prefix_up_to_the_cap() -> None:
    """Mutation: replace ``ask(limit=READABLE_SCAN_CAP)[len(first):]`` in ``_first_readable_memory_row`` with
    ``ask(limit=READABLE_SCAN_CAP)``.

    The second read is a prefix of the cap's length, and the first row, which was judged, is not judged again.
    """

    found = _PrefixProbeStore(_rows(70, 1))
    asked: list[list[str]] = []

    def recording(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
        asked.append([str(row["id"]) for row in rows])
        return _readable(rows)

    assert corpus_support(["Zorblax"], found, readable_memory_rows=recording) == {"Zorblax": True}
    assert [call[0] for call in found.calls] == [1, CAP]
    assert asked[0] == ["hidden-0"] and "hidden-0" not in asked[1] and len(asked[1]) == 70, "the first row was judged twice"
    ceiling = _PrefixProbeStore(_rows(CAP * 2, 1))
    assert corpus_support(["Zorblax"], ceiling, readable_memory_rows=_readable) == {"Zorblax": True}
    assert [call[0] for call in ceiling.calls] == [1, CAP]


def test_a_store_without_match_any_gets_the_strict_probe() -> None:
    """Mutation: drop the ``except TypeError`` branch of ``ask`` in ``_first_readable_memory_row``.

    The error is not a failed probe: the probe asks again without ``match_any`` and finds the readable row.
    """

    store = _StrictProbeStore(_rows(2, 1))
    assert corpus_support(["Zorblax"], store, readable_memory_rows=_readable) == {"Zorblax": True}


# -- the brief of alice_resume ---------------------------------------------------------------------------------------


def _brief(vault: _Vault, key: str | None, **arguments: object) -> dict[str, object]:
    brief = vault.call(key, "alice_resume", **arguments)["brief"]
    assert isinstance(brief, dict)
    return {name: value for name, value in brief.items() if name != "generated_at"}  # type: ignore[union-attr]


def _hidden_commitment(vault: _Vault) -> dict[str, object]:
    with vault.store() as store:
        parent = _source(store, "parent-commitment", domain="health", scope=vault.scope)
        return _memory(store, "hidden-commitment", "alpha hidden commitment", {"source_id": str(parent["id"]), **_scope(vault.scope)},
                       memory_type="commitment")


def _hidden_changes(vault: _Vault) -> list[dict[str, object]]:
    """Six memories newer than the readable ones that list an input that does not exist: no direct input proves them hidden,
    so the store returns their events and the check drops the events afterwards."""

    with vault.store() as store:
        return [_memory(store, f"hidden-change-{i}", f"alpha hidden change {i}", {"source_id": str(uuid4()), **_scope(vault.scope)})
                for i in range(6)]


# The hidden decision comes in the shapes of the recall tests. The hidden commitment is a row below a source in the health
# domain, which these keys may not read, and the hidden changes list an input that does not exist.
RESUME_CELLS = [
    ("last_decision", "chain", "read_only"),
    ("last_decision", "chain", "trusted"),
    ("last_decision", "domain", "read_only"),
    ("last_decision", "chain", "read_only_alpha"),
    ("next_action", "domain", "read_only"),
    ("next_action", "domain", "proposal"),
    ("recent_changes", "domain", "read_only"),
    ("recent_changes", "domain", "read_only_alpha"),
]


@pytest.mark.parametrize(("part", "shape", "name"), RESUME_CELLS)
def test_a_newer_hidden_row_does_not_change_the_resume_of_a_restricted_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, part: str, shape: str, name: str
) -> None:
    """Mutation: set ``limited = False`` in ``_vnext_resume`` (the memory reads go back to one row and the drop, the
    event reads to the cut and the drop).

    ``last_decision`` read the newest decision matching the query with a limit of one, and a newer hidden decision
    was dropped afterwards and left nothing. ``next_action`` did the same with the newest commitment, and
    ``recent_changes`` cut the events to the limit before the drop, so events of hidden memories took the places of
    readable ones. The control reads the hidden row as the owner with every sensitivity.
    """

    assert name in HIDDEN_FROM[shape]
    vault = _Vault(tmp_path, monkeypatch, scoped=KEYS[name][1] is not None)
    key = vault.key(name)
    arguments: dict[str, object] = {"query": "alpha"}
    if part == "next_action":
        with vault.store() as store:
            _memory(store, "visible-commitment", "alpha visible commitment", _scope(vault.scope), memory_type="commitment")
    if part == "recent_changes":
        arguments = {"max_recent_changes": 3}
    before = _brief(vault, key, **arguments)
    assert before[part], before

    if part == "last_decision":
        hidden_ids = {str(vault.hide(shape)["id"])}
    elif part == "next_action":
        hidden_ids = {str(_hidden_commitment(vault)["id"])}
    else:
        hidden_ids = {str(row["id"]) for row in _hidden_changes(vault)}
    everything = _brief(vault, None, **arguments, sensitivity_allowed=list(ALL_SENSITIVITY))
    shown = json.dumps(everything[part], default=str)
    assert any(hidden in shown for hidden in hidden_ids), "the owner must be able to read the newer hidden rows"

    after = _brief(vault, key, **arguments)
    assert after == before
    assert not any(hidden in json.dumps(after, default=str) for hidden in hidden_ids)


def test_a_newer_hidden_open_loop_does_not_change_the_open_loops_of_a_restricted_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: none for this file. The store's own filters take such a loop out of the read before the limit; this
    keeps it so, because the loops are read with a limit and dropped by the same guard."""

    vault = _Vault(tmp_path, monkeypatch)
    key = vault.key("read_only")
    with vault.store() as store:
        for index in range(3):
            store.create_open_loop({"title": f"visible loop {index}", "domain": "project", "sensitivity": "public", "status": "open",
                                    "metadata_json": {}})
    before = _brief(vault, key, max_open_loops=3)
    assert len(before["open_loops"]) == 3  # type: ignore[arg-type]
    with vault.store() as store:
        for index in range(3):
            parent = _source(store, f"loop-parent-{index}", domain="health")
            store.create_open_loop({"title": f"hidden loop {index}", "domain": "project", "sensitivity": "public", "status": "open",
                                    "source_id": str(parent["id"]),
                                    "metadata_json": {"discovered_by": "weekly", "source_id": str(parent["id"])}})
    assert _brief(vault, key, max_open_loops=3) == before


def _titles(items: object) -> set[str]:
    return {json.loads(item["title"]) for item in items}  # type: ignore[attr-defined, index, union-attr]


def _loops(vault: _Vault, titles: Sequence[str], *, parent: dict[str, object] | None = None) -> list[dict[str, object]]:
    """Open loops. With ``parent`` they are derived from that source and written without the insert floor, so their stored
    labels are public and project while their input is not: the store's filters let them through."""

    rows: list[dict[str, object]] = []
    with vault.store() as store:
        for title in titles:
            loop: dict[str, object] = {"title": title, "domain": "project", "sensitivity": "public", "status": "open",
                                       "metadata_json": _scope(vault.scope)}
            if parent is not None:
                loop["source_id"] = str(parent["id"])
                loop["metadata_json"] = {"discovered_by": "weekly", "source_id": str(parent["id"]), **_scope(vault.scope)}
                with without_insert_floor():
                    rows.append(dict(store.create_open_loop(loop)))
            else:
                rows.append(dict(store.create_open_loop(loop)))
    return rows


@pytest.mark.parametrize("name", ["read_only", "read_only_alpha"])
def test_a_newer_unfloored_open_loop_does_not_change_the_open_loops_of_a_restricted_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Mutation: replace ``elif limited:`` before ``loop_rows = readable_prefix(`` in ``_vnext_resume`` with ``elif False:``,
    or ``if source_fence.entity_read_fenced:`` before ``open_loop_rows = readable_prefix(`` in ``compile_context_pack``
    with ``if False:``.

    Six loops are newer than the five readable ones and derived from a source in the health domain, with stored labels that
    are public and project. The store filters by stored labels, cuts to the limit and leaves the six to the check, which
    drops them: five readable loops became none, and ``next_action`` with them. The resume brief and the pack are read
    for a key and must show the five. The control reads the six as the owner with every sensitivity.
    """

    assert name in HIDDEN_FROM["domain"]
    vault = _Vault(tmp_path, monkeypatch, scoped=KEYS[name][1] is not None)
    key = vault.key(name)
    _loops(vault, [f"visible loop {i}" for i in range(5)])
    before_resume = _brief(vault, key)
    before_pack = vault.call(key, "alice_context_pack", query="loop", debug=True)
    assert len(before_resume["open_loops"]) == 5 and len(before_pack["open_loops"]) == 5  # type: ignore[arg-type]

    with vault.store() as store:
        parent = _source(store, "loop-parent", domain="health", scope=vault.scope)
    hidden = _loops(vault, [f"hidden loop {i}" for i in range(6)], parent=parent)
    hidden_titles = {str(row["title"]) for row in hidden}
    everything = _brief(vault, None, sensitivity_allowed=list(ALL_SENSITIVITY))
    assert hidden_titles & _titles(everything["open_loops"]), "the owner must be able to read the six"

    after_resume = _brief(vault, key)
    after_pack = vault.call(key, "alice_context_pack", query="loop", debug=True)
    assert after_resume == before_resume
    assert _titles(after_pack["open_loops"]) == _titles(before_pack["open_loops"])
    assert after_pack["trace"]["stages"]["open_loops"] == before_pack["trace"]["stages"]["open_loops"]  # type: ignore[index]


@pytest.mark.parametrize("name", [None, "admin"])
def test_the_owner_and_an_unbound_admin_key_keep_the_read_they_had(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str | None) -> None:
    """Mutation: set ``limited = True`` in ``_vnext_resume``.

    Neither has limits. The newest decision matching the query is the hidden one, their own default filters drop it,
    and ``last_decision`` is empty, as it was. A key with limits is shown the readable older one.
    """

    vault = _Vault(tmp_path, monkeypatch)
    key = vault.key(name) if name else None
    assert _brief(vault, key, query="alpha")["last_decision"]
    vault.hide("chain")
    assert _brief(vault, key, query="alpha")["last_decision"] is None
    assert _brief(vault, vault.key("read_only"), query="alpha")["last_decision"]


# -- the brief in the project view -----------------------------------------------------------------------------------


def _project_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, project_visible: int, project_hidden: int, loops_visible: int = 0, loops_hidden: int = 0, provable: bool = True):
    """A vault with a global decision, ``project_visible`` readable decisions of the project, and then
    ``project_hidden`` newer ones that a read-only key may not read, and the same for open loops (``loops_visible`` readable ones,
    ``loops_hidden`` newer ones derived from a source the key may not read and written without the insert floor). The hidden
    decisions are below a source in the health domain, which a direct input proves hidden, or with ``provable=False`` list an
    input that does not exist, which nothing in their direct inputs proves. Returns the context, the key and the function that
    adds the hidden rows."""

    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    repo = repo_with_remote(tmp_path / "repo")
    detection = detect_project(argument=str(repo))
    assert detection.context is not None
    project = detection.context.ids[0]
    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    base_context = context_for(data_dir)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        add_memory(store, key="global.decision", text="alpha global decision", scope=None, memory_type="decision")
        for index in range(project_visible):
            add_memory(store, key=f"project.visible.{index}", text=f"alpha project decision {index}", scope=(project,), memory_type="decision")
        for index in range(loops_visible):
            add_loop(store, title=f"project loop {index}", scope=(project,))
        _record, raw = create_agent_key(store, user_id=USER_ID, agent_id="reader", permission_profile="read_only_agent", project_scope=None)
    context = MCPRuntimeContext(database_url=base_context.database_url, user_id=USER_ID, project_dir=str(repo))
    added: list[str] = []

    def add_hidden() -> list[str]:
        with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
            store = SQLiteVNextStore(connection, USER_ID)
            parent = store.create_source({"source_type": "note", "title": "parent", "content_hash": "p", "domain": "health",
                                          "sensitivity": "public", "metadata_json": {"project_scope": [project]}})
            for index in range(project_hidden):
                with without_insert_floor():
                    row = store.create_memory({
                        "memory_key": f"project.hidden.{index}", "canonical_text": f"alpha project hidden decision {index}",
                        "title": f"hidden-{index}", "memory_type": "decision", "status": "active", "domain": "project",
                        "sensitivity": "public", "project_scope": [project], "project_id": project,
                        "metadata_json": {"source_id": str(parent["id"]) if provable else str(uuid4()), "project_scope": [project]},
                    })
                added.append(str(row["id"]))
            for index in range(loops_hidden):
                with without_insert_floor():
                    store.create_open_loop({
                        "title": f"hidden project loop {index}", "domain": "project", "sensitivity": "public", "status": "open",
                        "source_id": str(parent["id"]), "project_id": project,
                        "metadata_json": {"discovered_by": "weekly", "source_id": str(parent["id"]), "project_scope": [project]},
                    })
        return added

    return context, raw, add_hidden


def _project_brief(context: MCPRuntimeContext, monkeypatch: pytest.MonkeyPatch, key: str | None, **arguments: object) -> dict[str, object]:
    if key:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    else:
        monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    brief = call_mcp_tool(context, name="alice_resume", arguments=dict(arguments))["brief"]
    return {name: value for name, value in brief.items() if name != "generated_at"}  # type: ignore[union-attr]


@pytest.mark.parametrize(("project_visible", "expected"), [(1, "alpha project decision 0"), (0, "alpha global decision")])
def test_a_hidden_newer_decision_of_the_project_does_not_change_the_last_decision_in_the_project_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_visible: int, expected: str
) -> None:
    """Mutation: replace ``project_first_fill_readable(`` in ``newest_readable`` (``_vnext_resume``) with
    ``project_first_fill(`` and drop its ``keep`` argument.

    In the project view the project's own decision comes before a global one, whatever their ages. With a readable
    decision of the project, a newer hidden one of the project must not hide it. With none, the key is shown the
    global decision, as it is in a vault without the hidden one, and not nothing. The hidden decisions list an input that does
    not exist, so nothing in the store proves them hidden and only the check after the read can drop them.
    """

    context, key, add_hidden = _project_vault(tmp_path, monkeypatch, project_visible=project_visible, project_hidden=2, provable=False)
    before = _project_brief(context, monkeypatch, key, query="alpha")
    assert json.loads(before["last_decision"]["canonical_text"]) == expected  # type: ignore[index]
    hidden_ids = add_hidden()
    owner = _project_brief(context, monkeypatch, None, query="alpha", sensitivity_allowed=list(ALL_SENSITIVITY))
    assert owner["last_decision"]["id"] in hidden_ids, owner["last_decision"]  # type: ignore[index]
    after = _project_brief(context, monkeypatch, key, query="alpha")
    assert after == before


def test_hidden_events_of_the_project_do_not_change_the_recent_changes_in_the_project_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: replace ``if limited:`` before ``project_first_fill_readable(`` in the events of ``_vnext_resume`` with
    ``if False:``.

    The events of four newer hidden decisions of the project, which list an input that does not exist and so are not proven
    hidden in the store, must not take the three places of the recent changes.
    """

    context, key, add_hidden = _project_vault(tmp_path, monkeypatch, project_visible=3, project_hidden=4, provable=False)
    before = _project_brief(context, monkeypatch, key, max_recent_changes=3)
    assert len(before["recent_changes"]) == 3  # type: ignore[arg-type]
    hidden_ids = add_hidden()
    owner = _project_brief(context, monkeypatch, None, max_recent_changes=3, sensitivity_allowed=list(ALL_SENSITIVITY))
    assert any(event["target_id"] in hidden_ids for event in owner["recent_changes"])  # type: ignore[attr-defined, union-attr]
    assert _project_brief(context, monkeypatch, key, max_recent_changes=3) == before


def test_provable_hidden_events_of_the_project_use_no_place_of_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: drop ``**({"readable_by": readable_by} if limited else {})`` after ``sensitivity_filter`` in ``read_events``
    (``_vnext_resume``), or ``held_back_domains["readable_by"] = readable_by`` in ``list_memory_rows``.

    More than twice the cap in hidden decisions of the project, each below a source in the health domain, are newer than the three
    readable ones. The store leaves their events and the decisions themselves out before its limit, so the key is shown the
    recent changes and the decision of the vault without them, however many there are. Without the proof the read stops at the
    cap among hidden events and the recent changes are empty.
    """

    context, key, add_hidden = _project_vault(tmp_path, monkeypatch, project_visible=3, project_hidden=CAP * 2 + 10)
    before = _project_brief(context, monkeypatch, key, max_recent_changes=3)
    assert len(before["recent_changes"]) == 3 and before["last_decision"]  # type: ignore[arg-type]
    add_hidden()
    assert _project_brief(context, monkeypatch, key, max_recent_changes=3) == before


def test_hidden_open_loops_of_the_project_do_not_change_the_open_loops_in_the_project_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: replace ``elif limited and project_view.mode == "project":`` before ``project_first_fill_readable(`` in the open
    loops of ``_vnext_resume`` with ``elif False and project_view.mode == "project":``.

    The project has three open loops the key may read and four newer ones derived from a source in the health domain, written
    without the insert floor, so the store lets them through. The key is shown the three, and the owner who asks for every
    sensitivity is shown the newer ones first.
    """

    context, key, add_hidden = _project_vault(tmp_path, monkeypatch, project_visible=1, project_hidden=0, loops_visible=3, loops_hidden=4)
    before = _project_brief(context, monkeypatch, key)
    assert len(before["open_loops"]) == 3  # type: ignore[arg-type]
    add_hidden()
    owner = _project_brief(context, monkeypatch, None, sensitivity_allowed=list(ALL_SENSITIVITY))
    assert any("hidden project loop" in item["title"] for item in owner["open_loops"])  # type: ignore[attr-defined, union-attr]
    assert _project_brief(context, monkeypatch, key) == before


# -- the reads that scan until they hold enough readable rows ---------------------------------------------------------


@pytest.mark.parametrize("seed", range(40))
def test_readable_prefix_gives_the_first_readable_rows_of_the_first_cap_rows(seed: int) -> None:
    """Mutation: replace ``if len(kept) >= want or len(rows) < window or window >= cap:`` in ``_scan_readable`` with
    ``if True:``, or replace ``window = cap`` with ``window = window + 1``.

    The scan reads ``want`` rows and then, when too few are kept, ``cap`` rows; no row is judged twice; the answer is the
    first rows the check keeps among the first ``cap``.
    """

    rng = random.Random(seed)
    cap = rng.choice([4, 9, 30])
    rows = list(range(rng.randrange(0, 60)))
    dropped = {row for row in rows if rng.random() < rng.choice([0.0, 0.3, 0.8, 1.0])}
    limit = rng.randrange(1, 6)
    asked: list[int] = []
    judged: list[int] = []

    def fetch(window: int) -> list[int]:
        asked.append(window)
        return rows[:window]

    def keep(part: Sequence[int]) -> list[int]:
        judged.extend(part)
        return [row for row in part if row not in dropped]

    kept = readable_prefix(limit=limit, fetch=fetch, keep=keep, cap=cap)
    assert kept == [row for row in rows[:cap] if row not in dropped][:limit]
    assert asked[0] == min(limit, cap) and len(asked) <= 2 and all(window <= cap for window in asked)
    assert judged == rows[:len(judged)] and len(set(judged)) == len(judged), "a row was judged twice"


def test_readable_prefix_reads_one_window_when_nothing_is_dropped() -> None:
    """Mutation: start the window at ``cap`` in ``_scan_readable``. A vault in which the check drops nothing is read as it
    was: one read of ``limit`` rows."""

    asked: list[int] = []
    rows = list(range(20))
    assert readable_prefix(limit=3, fetch=lambda window: asked.append(window) or rows[:window], keep=list) == [0, 1, 2]
    assert asked == [3]


@pytest.mark.parametrize("seed", range(60))
def test_the_readable_fill_equals_the_fill_over_the_readable_rows(seed: int) -> None:
    """Mutation: replace ``if limit // 4 > 0 or len(project_kept) < limit:`` in ``project_first_fill_readable`` with
    ``if False:``.

    Whatever the check drops, the fill gives what ``project_first_fill`` gives over the rows that are kept (the lists here
    are shorter than the cap).
    """

    rng = random.Random(seed)
    project = [("p", i) for i in range(rng.randrange(0, 40))]
    other = [("g", i) for i in range(rng.randrange(0, 40))]
    rate = rng.choice([0.0, 0.2, 0.7, 1.0])
    dropped = {row for row in [*project, *other] if rng.random() < rate}
    limit = rng.randrange(1, 9)
    view = project_view()

    def keep(rows: Sequence[tuple[str, int]]) -> list[tuple[str, int]]:
        return [row for row in rows if row not in dropped]

    def fetch_one(ids, excluded, count):  # type: ignore[no-untyped-def]
        return (other if ids == (GLOBAL_PROJECT_MARKER,) else project)[:count]

    expected = project_first_fill(
        limit=limit, view=view, exclude_global_domains=frozenset(),
        fetch=lambda ids, excluded, count: (keep(project)[:count], keep(other)[:count]),
    )
    assert project_first_fill_readable(
        limit=limit, view=view, exclude_global_domains=frozenset(), fetch_one=fetch_one, keep=keep, cap=100,
    ) == expected


@pytest.mark.parametrize(("limit", "partitions_read"), [(1, 1), (3, 1), (4, 2), (8, 2)])
def test_the_readable_fill_reads_one_window_and_skips_the_global_partition_it_cannot_use(limit: int, partitions_read: int) -> None:
    """Mutation: replace ``if limit // 4 > 0 or len(project_kept) < limit:`` in ``project_first_fill_readable`` with
    ``if True:``.

    Spec 6.2 holds ``limit // 4`` places for global rows, so a limit below four takes a global row only when the project's
    kept rows do not fill it. A vault in which the check drops nothing reads one window of ``limit`` rows per partition
    that the fill can use.
    """

    calls: list[tuple[tuple[str, ...], int]] = []

    def fetch_one(ids, excluded, count):  # type: ignore[no-untyped-def]
        calls.append((ids, count))
        return [("g" if ids == (GLOBAL_PROJECT_MARKER,) else "p", i) for i in range(count)]

    result = project_first_fill_readable(
        limit=limit, view=project_view(), exclude_global_domains=frozenset(), fetch_one=fetch_one, keep=list,
    )
    assert len(calls) == partitions_read and all(count == limit for _ids, count in calls)
    assert [row for row in result if row[0] == "p"] == [("p", i) for i in range(len([r for r in result if r[0] == "p"]))]
    assert len(result) == limit


def test_the_readable_fill_reads_the_global_partition_when_the_project_does_not_fill_the_limit() -> None:
    """Mutation: replace ``len(project_kept) < limit`` in ``project_first_fill_readable`` with ``False``.

    The project has nothing the caller may read; a limit of one then takes the first global row the caller may read.
    """

    def fetch_one(ids, excluded, count):  # type: ignore[no-untyped-def]
        return [("g" if ids == (GLOBAL_PROJECT_MARKER,) else "p", i) for i in range(count)]

    result = project_first_fill_readable(
        limit=1, view=project_view(), exclude_global_domains=frozenset(), fetch_one=fetch_one,
        keep=lambda rows: [row for row in rows if row[0] == "g" and row[1] >= 2],
    )
    assert result == [("g", 2)]
