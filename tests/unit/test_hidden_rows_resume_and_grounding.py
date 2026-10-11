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
from alicebot_api.vnext_label_writes import without_insert_floor
from tests.unit.per_project_s2_support import USER_ID, add_memory, context_for, db_path_for, repo_with_remote
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
    """Mutation: return after the first page of the memory probe (replace ``if len(rows) < page:`` in
    ``_first_readable_memory_row`` with ``if True:``).

    Two hidden rows rank ahead of the one readable row that mentions the name, and the probe asks for one row first.
    The probe must go on to the page that holds the readable one.
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

    def search_memories_fts(self, *, query, domains=None, sensitivity_allowed=None, limit=50, offset=0, match_any=False):  # type: ignore[no-untyped-def]
        self.calls.append((limit, offset, match_any))
        return self.rows[offset:offset + limit]


class _PrefixProbeStore(_ProbeStore):
    """The same for an adapter that takes no ``offset``: the probe asks for a longer prefix each time."""

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


def test_the_probe_pages_past_hidden_rows_to_the_first_readable_one() -> None:
    """Mutation: replace ``page = _READABLE_PROBE_PAGE`` in ``_first_readable_memory_row`` with ``page = 1``.

    300 hidden rows come before the readable one. The probe asks for one row, as the owner's probe does, and then for
    pages of 128 past it.
    """

    store = _ProbeStore(_rows(300, 1))
    assert corpus_support(["Zorblax"], store, readable_memory_rows=_readable) == {"Zorblax": True}
    assert store.calls == [(1, 0, True), (128, 1, True), (128, 129, True), (128, 257, True)]


def test_the_probe_ends_when_the_matches_run_out_and_none_is_readable() -> None:
    """Mutation: replace ``return []`` after ``if len(rows) < page:`` in ``_first_readable_memory_row`` with ``return None``.

    Every match is hidden: the name has no readable mention, which is the answer of a vault without those rows.
    """

    store = _ProbeStore(_rows(3, 0))
    assert corpus_support(["Zorblax"], store, readable_memory_rows=_readable) == {"Zorblax": False}
    assert store.calls == [(1, 0, True), (128, 1, True)]


def test_the_owner_probe_is_still_one_row() -> None:
    """Mutation: pass ``readable_rows=readable_memory_rows`` for every caller (``memory_probe = True``).

    The owner and an unbound admin key leave ``readable_memory_rows`` unset: one call for one row, no paging.
    """

    store = _ProbeStore(_rows(5, 1))
    assert corpus_support(["Zorblax"], store) == {"Zorblax": True}
    assert store.calls == [(1, 0, True)]


def test_an_adapter_without_offset_is_asked_for_longer_prefixes_up_to_a_ceiling() -> None:
    """Mutation: replace ``page = min(page * 8, _READABLE_PROBE_PREFIX_CEILING)`` with ``page = page``.

    No readable row within the ceiling: the probe could not run, which grounding reads as no claim.
    """

    found = _PrefixProbeStore(_rows(70, 1))
    assert corpus_support(["Zorblax"], found, readable_memory_rows=_readable) == {"Zorblax": True}
    assert [call[0] for call in found.calls] == [1, 8, 64, 512]
    ceiling = _PrefixProbeStore(_rows(20000, 0))
    assert corpus_support(["Zorblax"], ceiling, readable_memory_rows=_readable) is None
    assert [call[0] for call in ceiling.calls][-1] == 16384


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
    with vault.store() as store:
        parent = _source(store, "parent-changes", domain="health", scope=vault.scope)
        return [_memory(store, f"hidden-change-{i}", f"alpha hidden change {i}", {"source_id": str(parent["id"]), **_scope(vault.scope)})
                for i in range(6)]


# The hidden decision comes in the shapes of the recall tests. The hidden commitment and the hidden changes are rows below a
# source in the health domain, which these keys may not read.
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


def _project_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, project_visible: int, project_hidden: int):
    """A vault with a global decision, ``project_visible`` readable decisions of the project, and then
    ``project_hidden`` newer ones that a read-only key may not read. Returns the context and the key."""

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
                        "metadata_json": {"source_id": str(parent["id"]), "project_scope": [project]},
                    })
                added.append(str(row["id"]))
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
    global decision, as it is in a vault without the hidden one, and not nothing.
    """

    context, key, add_hidden = _project_vault(tmp_path, monkeypatch, project_visible=project_visible, project_hidden=2)
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

    The events of four newer hidden decisions of the project must not take the three places of the recent changes.
    """

    context, key, add_hidden = _project_vault(tmp_path, monkeypatch, project_visible=3, project_hidden=4)
    before = _project_brief(context, monkeypatch, key, max_recent_changes=3)
    assert len(before["recent_changes"]) == 3  # type: ignore[arg-type]
    hidden_ids = add_hidden()
    owner = _project_brief(context, monkeypatch, None, max_recent_changes=3, sensitivity_allowed=list(ALL_SENSITIVITY))
    assert any(event["target_id"] in hidden_ids for event in owner["recent_changes"])  # type: ignore[attr-defined, union-attr]
    assert _project_brief(context, monkeypatch, key, max_recent_changes=3) == before


# -- the reads that grow until they hold enough readable rows ---------------------------------------------------------


@pytest.mark.parametrize("seed", range(40))
def test_readable_prefix_gives_the_first_readable_rows_of_the_whole_list(seed: int) -> None:
    """Mutation: replace ``if len(kept) >= limit or len(rows) < window:`` in ``readable_prefix`` with ``if True:``."""

    rng = random.Random(seed)
    rows = list(range(rng.randrange(0, 60)))
    dropped = {row for row in rows if rng.random() < rng.choice([0.0, 0.3, 0.8, 1.0])}
    limit = rng.randrange(1, 6)
    asked: list[int] = []

    def fetch(window: int) -> list[int]:
        asked.append(window)
        return rows[:window]

    kept = readable_prefix(limit=limit, fetch=fetch, keep=lambda part: [row for row in part if row not in dropped])
    assert kept == [row for row in rows if row not in dropped][:limit]
    assert asked[0] == limit
    assert all(later == earlier * 8 for earlier, later in zip(asked, asked[1:], strict=False))


def test_readable_prefix_reads_one_window_when_nothing_is_dropped() -> None:
    """Mutation: start the window at ``limit * 8`` in ``readable_prefix``. A vault in which the check drops nothing is
    read as it was: one read of ``limit`` rows."""

    asked: list[int] = []
    rows = list(range(20))
    assert readable_prefix(limit=3, fetch=lambda window: asked.append(window) or rows[:window], keep=list) == [0, 1, 2]
    assert asked == [3]


@pytest.mark.parametrize("seed", range(60))
def test_the_readable_fill_equals_the_fill_over_the_readable_rows(seed: int) -> None:
    """Mutation: replace ``if all(`` in ``project_first_fill_readable`` with ``if True or all(``.

    Whatever the check drops, the fill gives what ``project_first_fill`` gives over the rows that are kept.
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

    def fetch(ids, excluded, count):  # type: ignore[no-untyped-def]
        return project[:count], other[:count]

    expected = project_first_fill(
        limit=limit, view=view, exclude_global_domains=frozenset(),
        fetch=lambda ids, excluded, count: (keep(project)[:count], keep(other)[:count]),
    )
    assert project_first_fill_readable(
        limit=limit, view=view, exclude_global_domains=frozenset(), fetch=fetch, keep=keep,
    ) == expected


def test_the_readable_fill_reads_one_window_when_nothing_is_dropped() -> None:
    """Mutation: start the window at ``limit * 8`` in ``project_first_fill_readable``."""

    calls: list[int] = []

    def fetch(ids, excluded, count):  # type: ignore[no-untyped-def]
        calls.append(count)
        return [("p", i) for i in range(count)], [("g", i) for i in range(count)]

    result = project_first_fill_readable(
        limit=4, view=project_view(), exclude_global_domains=frozenset(), fetch=fetch, keep=list,
    )
    assert calls == [4]
    assert result == [("p", 0), ("p", 1), ("p", 2), ("g", 0)]
