"""Passage ranking is the default of ``alice_recall`` on the SQLite store.

With ``ALICE_SEARCH_QUALITY`` unset, ``alice_recall`` on SQLite runs passage mode (``SourceRanking.passage()``,
up to ``PASSAGES_PER_SOURCE`` passages of one document). ``off`` gives document mode there, ``passage`` gives
passage mode, and ``on`` is still refused. PostgreSQL is unchanged: unset is document mode for every caller, and a
key with limits stays on document mode when the variable says ``passage``. The context pack, the brief, the command
line and the hook never read the variable and stay on document mode.

The store-dependent default is decided in one place, ``PASSAGE_BY_DEFAULT_STORES`` in ``alicebot_api.mcp.retrieval``,
at the edge that already read the variable. Library code reads no environment. The harness side is in
``tests/unit/test_alice_bench_ranking_mode.py``.

Each test names the edit that must make it fail. The cases ``sqlite-unset-default-stays-document``,
``postgres-unset-default-becomes-passage``, ``sqlite-off-no-longer-forces-document``, ``brief-ranks-by-passage`` and
``pack-ranks-by-passage`` of ``scripts/derived_label_mutations.json`` replay five of them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from alicebot_api.mcp import retrieval as recall_edge
from alicebot_api.mcp.retrieval import PASSAGE_BY_DEFAULT_STORES, recall_source_ranking
from alicebot_api.project_view import ProjectView
from alicebot_api.source_ranking import SEARCH_QUALITY_ENV, SourceRanking
from alicebot_api.sqlite_store import SQLiteVNextStore
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_retrieval import VNextRetrievalService
from alicebot_api.vnext_store import PostgresVNextStore
from tests.unit.test_passage_or_fallback import LONG_QUESTION, _seed_keys
from tests.unit.test_passage_ranking import USER_ID, _context, _door_call, _quiet, _stdio_call, _store
from tests.unit.test_source_ranking_plumbing import _spy, _vault

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = SourceRanking.document()
PASSAGE = SourceRanking.passage()
LIMITED = AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent", auth="agent_key")
ADMIN = AgentIdentity(agent_id="admin", permission_profile="admin_agent", auth="agent_key")


def _named(name: str) -> object:
    """An object whose class carries ``name``. The edge reads the class name of the store and nothing else."""

    return type(name, (), {})()


def _set(monkeypatch: pytest.MonkeyPatch, setting: str | None) -> None:
    if setting is None:
        monkeypatch.delenv(SEARCH_QUALITY_ENV, raising=False)
    else:
        monkeypatch.setenv(SEARCH_QUALITY_ENV, setting)


def _spy_recall_stage(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, dict[str, object]]]:
    """The ranking each source read was handed and the stage record it returned, then the real method runs."""

    seen: list[tuple[object, dict[str, object]]] = []
    original = VNextRetrievalService.search_source_excerpts

    def wrapped(self, **kwargs):  # type: ignore[no-untyped-def]
        entries, stage = original(self, **kwargs)
        seen.append((kwargs.get("ranking"), dict(stage)))
        return entries, stage

    monkeypatch.setattr(VNextRetrievalService, "search_source_excerpts", wrapped)
    return seen


# --------------------------------------------------------------------------
# The edge: store kind, caller and setting
# --------------------------------------------------------------------------

_SQLITE = "SQLiteVNextStore"
_POSTGRES = "PostgresVNextStore"
_OTHER = "SomeOtherStore"

_TABLE = [
    # SQLite: unset is passage mode for every caller, off is document mode, passage is passage mode.
    pytest.param(_SQLITE, None, None, PASSAGE, id="sqlite-owner-unset"),
    pytest.param(_SQLITE, ADMIN, None, PASSAGE, id="sqlite-admin-unset"),
    pytest.param(_SQLITE, LIMITED, None, PASSAGE, id="sqlite-limited-unset"),
    pytest.param(_SQLITE, None, "off", DOCUMENT, id="sqlite-owner-off"),
    pytest.param(_SQLITE, ADMIN, "off", DOCUMENT, id="sqlite-admin-off"),
    pytest.param(_SQLITE, LIMITED, "off", DOCUMENT, id="sqlite-limited-off"),
    pytest.param(_SQLITE, None, "passage", PASSAGE, id="sqlite-owner-passage"),
    pytest.param(_SQLITE, LIMITED, "passage", PASSAGE, id="sqlite-limited-passage"),
    # PostgreSQL: unset is document mode for the owner, an unbound admin key and a key with limits.
    pytest.param(_POSTGRES, None, None, DOCUMENT, id="postgres-owner-unset"),
    pytest.param(_POSTGRES, ADMIN, None, DOCUMENT, id="postgres-admin-unset"),
    pytest.param(_POSTGRES, LIMITED, None, DOCUMENT, id="postgres-limited-unset"),
    pytest.param(_POSTGRES, None, "off", DOCUMENT, id="postgres-owner-off"),
    pytest.param(_POSTGRES, LIMITED, "off", DOCUMENT, id="postgres-limited-off"),
    # PostgreSQL with passage: as before. A key with limits stays on document mode.
    pytest.param(_POSTGRES, None, "passage", PASSAGE, id="postgres-owner-passage"),
    pytest.param(_POSTGRES, ADMIN, "passage", PASSAGE, id="postgres-admin-passage"),
    pytest.param(_POSTGRES, LIMITED, "passage", DOCUMENT, id="postgres-limited-passage"),
    # A store the edge does not know is document mode unless the variable says passage.
    pytest.param(_OTHER, None, None, DOCUMENT, id="other-owner-unset"),
    pytest.param(_OTHER, None, "passage", PASSAGE, id="other-owner-passage"),
]


@pytest.mark.parametrize(("store_name", "identity", "setting", "expected"), _TABLE)
def test_the_edge_picks_the_ranking_by_store_caller_and_setting(
    store_name: str,
    identity: AgentIdentity | None,
    setting: str | None,
    expected: SourceRanking,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutations: leave ``PASSAGE_BY_DEFAULT_STORES`` empty (the SQLite unset rows become document mode); name
    ``PostgresVNextStore`` in it (the PostgreSQL unset rows for the owner and the admin become passage mode);
    let ``off`` fall through to the store default (the SQLite ``off`` rows become passage mode); or drop the
    PostgreSQL limited-key rule (the ``postgres-limited-passage`` row becomes passage mode).
    """

    _set(monkeypatch, setting)
    assert recall_source_ranking(_named(store_name), identity) == expected


def test_on_is_still_the_improved_query_on_both_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    """``on`` is not a default of any store. The stage refuses it later. Mutation: let the store default replace it."""

    monkeypatch.setenv(SEARCH_QUALITY_ENV, "on")
    for name in (_SQLITE, _POSTGRES):
        assert recall_source_ranking(_named(name), None).lexical == "improved"


def test_the_store_default_is_one_named_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing ``PASSAGE_BY_DEFAULT_STORES`` alone moves the default of a store, and the limited-key rule stays.

    Mutation: decide the SQLite default somewhere other than ``PASSAGE_BY_DEFAULT_STORES``.
    """

    monkeypatch.delenv(SEARCH_QUALITY_ENV, raising=False)
    assert PASSAGE_BY_DEFAULT_STORES == frozenset({SQLiteVNextStore.__name__})
    assert PostgresVNextStore.__name__ not in PASSAGE_BY_DEFAULT_STORES

    monkeypatch.setattr(recall_edge, "PASSAGE_BY_DEFAULT_STORES", frozenset({_POSTGRES}))
    assert recall_source_ranking(_named(_POSTGRES), None) == PASSAGE
    assert recall_source_ranking(_named(_POSTGRES), ADMIN) == PASSAGE
    assert recall_source_ranking(_named(_POSTGRES), LIMITED) == DOCUMENT
    assert recall_source_ranking(_named(_SQLITE), None) == DOCUMENT

    monkeypatch.setattr(recall_edge, "PASSAGE_BY_DEFAULT_STORES", frozenset())
    assert recall_source_ranking(_named(_SQLITE), None) == DOCUMENT


def test_from_environment_uses_unset_only_for_a_missing_variable() -> None:
    """The value handed in as ``unset`` replaces a missing variable and nothing else.

    Mutation: return ``unset`` for ``off``, or for ``passage``, or ignore ``unset`` when the variable is missing.
    """

    assert SourceRanking.from_environment({}) == DOCUMENT
    assert SourceRanking.from_environment({}, unset=PASSAGE) == PASSAGE
    assert SourceRanking.from_environment({}, unset=DOCUMENT) == DOCUMENT
    assert SourceRanking.from_environment({SEARCH_QUALITY_ENV: "off"}, unset=PASSAGE) == DOCUMENT
    assert SourceRanking.from_environment({SEARCH_QUALITY_ENV: " OFF "}, unset=PASSAGE) == DOCUMENT
    assert SourceRanking.from_environment({SEARCH_QUALITY_ENV: "passage"}, unset=DOCUMENT) == PASSAGE
    assert SourceRanking.from_environment({SEARCH_QUALITY_ENV: "on"}, unset=DOCUMENT).lexical == "improved"
    with pytest.raises(ValueError):
        SourceRanking.from_environment({SEARCH_QUALITY_ENV: "sideways"}, unset=PASSAGE)
    with pytest.raises(ValueError):
        SourceRanking.from_environment({SEARCH_QUALITY_ENV: ""}, unset=PASSAGE)


# --------------------------------------------------------------------------
# alice_recall on a SQLite vault
# --------------------------------------------------------------------------


def _recall(door: str, context, arguments: dict[str, object]) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return (_door_call if door == "call" else _stdio_call)(context, "alice_recall", arguments)


@pytest.mark.parametrize("door", ["call", "stdio"])
def test_unset_gives_passage_entries_on_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, door: str) -> None:
    """A long question over a three-chunk document returns the chunks, with the passage stage label.

    Mutation: leave ``PASSAGE_BY_DEFAULT_STORES`` empty, so the unset run is document mode (one entry, no
    ``chunk_index``).
    """

    _quiet(monkeypatch)
    context, database = _context(tmp_path)
    with _store(database) as conn:
        keys = _seed_keys(SQLiteVNextStore(conn, USER_ID))
    seen = _spy_recall_stage(monkeypatch)

    payload = _recall(door, context, {"query": LONG_QUESTION})

    sources = payload["sources"]
    assert {row["id"] for row in sources} == {keys}
    assert sorted(row["chunk_index"] for row in sources) == [0, 1, 2]
    assert payload["source_count"] == len(sources) == 3
    assert len(seen) == 1
    ranking, stage = seen[0]
    assert ranking == PASSAGE
    assert stage["source"] == "passage"
    assert stage["passages_per_source"] == 3


@pytest.mark.parametrize("door", ["call", "stdio"])
def test_off_gives_document_mode_on_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, door: str) -> None:
    """``off`` is the v0.20.2 ranking: one entry per document, no ``chunk_index``, the document stage label.

    Mutation: let ``off`` fall through to the store default, so SQLite runs passage mode with ``off`` set.
    """

    _quiet(monkeypatch)
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "off")
    context, database = _context(tmp_path)
    with _store(database) as conn:
        keys = _seed_keys(SQLiteVNextStore(conn, USER_ID))
    seen = _spy_recall_stage(monkeypatch)

    payload = _recall(door, context, {"query": LONG_QUESTION})

    sources = payload["sources"]
    assert [row["id"] for row in sources] == [keys]
    assert all("chunk_index" not in row for row in sources)
    assert payload["source_count"] == 1
    assert len(seen) == 1
    ranking, stage = seen[0]
    assert ranking == DOCUMENT
    assert str(stage["source"]).startswith("rrf(")
    assert "passages_per_source" not in stage


def test_passage_and_unset_agree_on_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting ``passage`` on SQLite is what unset already does. Mutation: any change that makes them differ."""

    _quiet(monkeypatch)
    context, database = _context(tmp_path)
    with _store(database) as conn:
        _seed_keys(SQLiteVNextStore(conn, USER_ID))
    unset = _door_call(context, "alice_recall", {"query": LONG_QUESTION})
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "passage")
    passage = _door_call(context, "alice_recall", {"query": LONG_QUESTION})
    assert unset["sources"] == passage["sources"]
    assert unset["source_count"] == passage["source_count"] == 3


def test_on_is_still_refused_with_nothing_else_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal of ``on`` does not depend on the store default. Mutation: catch the refusal at the edge."""

    from alicebot_api.mcp.types import MCPToolError

    _quiet(monkeypatch)
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "on")
    context, _database = _context(tmp_path)
    with pytest.raises(MCPToolError):
        _door_call(context, "alice_recall", {"query": "kettle"})


# --------------------------------------------------------------------------
# Every other caller stays on document mode, with the variable unset
# --------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["pack", "brief"])
def test_the_pack_and_the_brief_stay_on_document_mode_on_sqlite(
    route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the variable unset on SQLite, ``alice_recall`` runs passage mode and these two do not.

    Mutation: in ``compile_context_pack`` or ``compile_session_brief`` replace ``SourceRanking.document()`` with
    the ranking ``recall_source_ranking`` gives the SQLite store.
    """

    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.session_briefing import compile_local_session_brief

    context, database = _vault(tmp_path, monkeypatch)
    monkeypatch.delenv(SEARCH_QUALITY_ENV, raising=False)
    seen = _spy(monkeypatch)

    if route == "pack":
        payload = call_mcp_tool(context, name="alice_context_pack", arguments={"query": "kettle shelf"})
        assert payload["sources"], "the fixture source was not found, so the route proves nothing"
        assert all("chunk_index" not in entry for entry in payload["sources"])
        entry_points = ("_source_stage_lists",)
    else:
        brief = compile_local_session_brief(
            database,
            user_id=USER_ID,
            query="kettle shelf",
            project_view=ProjectView.unscoped(),
            exclude_global_domains=frozenset(),
        )
        assert "second shelf" in brief, "the fixture source was not found, so the route proves nothing"
        entry_points = ("search_source_excerpts", "_source_stage_lists")

    for name in entry_points:
        assert seen[name], f"{route} never reached {name}"
        assert all(value == DOCUMENT for value in seen[name]), (name, seen[name])

    # The same vault, asked through alice_recall, runs passage mode: the control that the variable is unset.
    recall_seen = _spy_recall_stage(monkeypatch)
    recalled = call_mcp_tool(context, name="alice_recall", arguments={"query": "kettle shelf"})
    assert recalled["sources"] and all("chunk_index" in entry for entry in recalled["sources"])
    assert [ranking for ranking, _stage in recall_seen] == [PASSAGE]


_EDGE_NAMES = frozenset({"from_environment", "SEARCH_QUALITY_ENV", "recall_source_ranking", "PASSAGE_BY_DEFAULT_STORES"})
_EDGE_FILES = {
    "apps/api/src/alicebot_api/source_ranking.py",
    "apps/api/src/alicebot_api/mcp/retrieval.py",
}


def test_only_the_recall_edge_reads_the_switch_or_the_store_default() -> None:
    """Library code reads no environment to choose a ranking, and no other caller asks for the recall default.

    Mutation: make the brief, the pack or the retrieval service call ``SourceRanking.from_environment`` or
    ``recall_source_ranking``, or import ``SEARCH_QUALITY_ENV`` to read the variable.
    """

    found: dict[str, set[str]] = {}
    for path in sorted((REPO_ROOT / "apps" / "api" / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in _EDGE_NAMES:
                names.add(node.id)
            elif isinstance(node, ast.Attribute) and node.attr in _EDGE_NAMES:
                names.add(node.attr)
            elif isinstance(node, ast.alias) and node.name in _EDGE_NAMES:
                names.add(node.name)
        if names:
            found[path.relative_to(REPO_ROOT).as_posix()] = names
    assert set(found) == _EDGE_FILES, found
    assert "recall_source_ranking" in found["apps/api/src/alicebot_api/mcp/retrieval.py"]
