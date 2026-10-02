"""The source stage takes its ranking as a required argument (search-quality spec TP1).

``SourceRanking`` is the value that will choose between ranking sources by document
(what v0.20.0 does) and by passage. This slice only carries it: ``ranking`` is a
required keyword-only argument of ``search_source_excerpts`` and
``_source_stage_lists``, the three production callers write
``SourceRanking.document()`` at the call, and every other value is refused until a
stage exists that honours it. Nothing about a result changes.

Why it is its own change. An optional ``ranking`` would be a silent choice for any
caller that forgets it, the same shape as the optional ``scope`` that once let recall
read sources a project-locked agent could not (see
``test_recall_sources_respect_the_scope_fence.py``). Making the argument required now,
while it can only have one value, means a later change that adds a second value cannot
add a caller that forgets to decide.

Every test below names the edit that must make it fail.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

from alicebot_api.project_view import ProjectView
from alicebot_api.source_ranking import SourceRanking
from alicebot_api.vnext_retrieval import VNextRetrievalService, VNextRetrievalValidationError

USER_ID = "00000000-0000-0000-0000-000000000001"
REPO_ROOT = Path(__file__).resolve().parents[2]

KETTLE_NOTE = """# Kitchen

The kettle is stored on the second shelf above the sink.
"""

class _EqualToAnything:
    """Not a SourceRanking, and equal to every value, so an equality check alone lets it through."""

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return 0


STAGE_ENTRY_POINTS = ("search_source_excerpts", "_source_stage_lists")

# A passage value, a document value with another lexical mode, a document value with
# another cap, and things that are not a SourceRanking at all.
NOT_THE_DOCUMENT_RANKING = [
    pytest.param(SourceRanking(mode="passage", passages_per_source=3, lexical="legacy"), id="passage"),
    pytest.param(SourceRanking(mode="passage", passages_per_source=1, lexical="improved"), id="passage-improved"),
    pytest.param(SourceRanking(mode="document", passages_per_source=1, lexical="improved"), id="document-improved"),
    pytest.param(SourceRanking(mode="document", passages_per_source=3, lexical="legacy"), id="document-cap-3"),
    pytest.param("document", id="string"),
    pytest.param(None, id="none"),
    pytest.param({"mode": "document"}, id="mapping"),
    pytest.param(_EqualToAnything(), id="equal-to-anything"),
]


class _TouchRecordingStore:
    """A store that holds nothing and records every attribute the service asks for.

    Missing attributes read as absent (``AttributeError``), so ``getattr(store, name,
    None)`` takes the "this store has no such stage" branch, exactly as a minimal
    store would. A call that is allowed to run therefore finishes with empty lists,
    and a call that is refused never gets as far as asking.
    """

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> object:
        self.touched.append(name)
        raise AttributeError(name)

    def search_sources(self, **_: object) -> list[dict[str, object]]:
        """The one store call the stage makes without asking whether it exists first."""

        self.touched.append("search_sources")
        return []


def _service(monkeypatch: pytest.MonkeyPatch) -> tuple[VNextRetrievalService, _TouchRecordingStore]:
    for name in ("ALICE_EMBEDDINGS_BASE_URL", "ALICE_EMBEDDINGS_MODEL", "ALICE_EMBEDDINGS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    store = _TouchRecordingStore()
    service = VNextRetrievalService(store, embedding_provider=None)  # type: ignore[arg-type]
    store.touched.clear()
    return service, store


def _call(service: VNextRetrievalService, entry_point: str, **overrides: object) -> object:
    """Call one of the two entry points with every argument but ``ranking``."""

    if entry_point == "search_source_excerpts":
        arguments: dict[str, object] = {
            "query": "kettle shelf",
            "domains": [],
            "sensitivity_allowed": ["public", "internal", "private", "unknown"],
            "limit": 8,
            "scope": None,
        }
    else:
        arguments = {
            "query": "kettle shelf",
            "domains": [],
            "sensitivity_allowed": ["public", "internal", "private", "unknown"],
            "limit": 8,
            "winning_memories": [],
        }
    arguments.update(overrides)
    return getattr(service, entry_point)(**arguments)


# ---------------------------------------------------------------------------------------------
# The value
# ---------------------------------------------------------------------------------------------


def test_the_document_ranking_is_what_v0200_has_and_is_a_plain_value() -> None:
    """Mutation: change a field of ``SourceRanking.document()`` (mode ``passage``, a cap other than 1,
    or lexical ``improved``), or make the dataclass mutable or unhashable."""

    ranking = SourceRanking.document()

    assert (ranking.mode, ranking.passages_per_source, ranking.lexical) == ("document", 1, "legacy")
    assert ranking == SourceRanking.document()
    assert hash(ranking) == hash(SourceRanking.document())
    with pytest.raises(dataclasses.FrozenInstanceError):
        ranking.mode = "passage"  # type: ignore[misc]


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"mode": "chunk"}, id="unknown-mode"),
        pytest.param({"lexical": "fancy"}, id="unknown-lexical"),
        pytest.param({"passages_per_source": 0}, id="cap-zero"),
        pytest.param({"passages_per_source": -1}, id="cap-negative"),
        pytest.param({"passages_per_source": 2.5}, id="cap-float"),
        pytest.param({"passages_per_source": "3"}, id="cap-string"),
        pytest.param({"passages_per_source": None}, id="cap-none"),
        pytest.param({"passages_per_source": True}, id="cap-bool"),
    ],
)
def test_a_ranking_the_stage_could_not_read_is_refused_at_construction(fields: dict[str, object]) -> None:
    """Mutation: drop the ``bool`` clause of ``SourceRanking.__post_init__`` (``True`` equals 1, so
    ``SourceRanking("document", True, "legacy") == SourceRanking.document()`` would hold and a
    flag would pass for a cap), or drop any other clause of it."""

    values: dict[str, object] = {"mode": "document", "passages_per_source": 1, "lexical": "legacy"}
    values.update(fields)

    with pytest.raises(ValueError):
        SourceRanking(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# The argument is required
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("entry_point", STAGE_ENTRY_POINTS)
def test_ranking_is_a_required_keyword_only_argument(entry_point: str) -> None:
    """Mutation: give ``ranking`` a default (for example ``ranking: SourceRanking =
    SourceRanking.document()``) on ``search_source_excerpts`` or on ``_source_stage_lists``, or
    make it positional."""

    parameter = inspect.signature(getattr(VNextRetrievalService, entry_point)).parameters["ranking"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty, (
        f"{entry_point}.ranking gained a default. A defaulted ranking is a silent choice for every "
        "caller that forgets it, the exact shape of the optional scope that once leaked sources."
    )


# The arguments each entry point takes with no default, besides ``ranking``. Every one of them is a
# control or the thing the control applies to: a call that leaves one out must fail, never run
# with a quiet "no filter". ``_source_stage_lists`` keeps its own default for ``scope`` (an unscoped
# stage is what a store-level test wants), so ``scope`` is listed for ``search_source_excerpts`` only.
_REQUIRED_WITH_NO_DEFAULT = {
    "search_source_excerpts": ("query", "domains", "sensitivity_allowed", "limit", "scope"),
    "_source_stage_lists": ("query", "domains", "sensitivity_allowed", "limit", "winning_memories"),
}


@pytest.mark.parametrize("entry_point", STAGE_ENTRY_POINTS)
def test_the_arguments_beside_ranking_stay_required_too(entry_point: str) -> None:
    """Mutation: give ``domains``, ``sensitivity_allowed``, ``limit``, ``scope`` (on
    ``search_source_excerpts``) or ``winning_memories`` (on ``_source_stage_lists``) a default.

    ``ranking`` joins a signature whose fences are already required, and the fence register
    names them together. A default on any of them reads as "no filter" for the caller that
    forgets it, so the check lives beside the one for ``ranking``.
    """

    parameters = inspect.signature(getattr(VNextRetrievalService, entry_point)).parameters

    for name in _REQUIRED_WITH_NO_DEFAULT[entry_point]:
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, (entry_point, name)
        assert parameters[name].default is inspect.Parameter.empty, (entry_point, name)


@pytest.mark.parametrize("entry_point", STAGE_ENTRY_POINTS)
def test_a_call_that_omits_ranking_does_not_run(entry_point: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: give ``ranking`` a default on the entry point (the call then runs against the
    store stub and returns instead of raising).

    This is the behaviour the signature pin above describes, run: the omitting call raises before
    the store is asked for anything.
    """

    service, store = _service(monkeypatch)

    with pytest.raises(TypeError, match="ranking"):
        _call(service, entry_point)

    assert store.touched == []


# ---------------------------------------------------------------------------------------------
# Only the document ranking has a code path
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("entry_point", STAGE_ENTRY_POINTS)
def test_the_document_ranking_runs_and_reads_the_store(entry_point: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards the guard. Mutation: refuse ``SourceRanking.document()`` as well, or stop the stage
    reading the store.

    The refusal tests below assert that a refused call leaves the store untouched. This control
    shows the store is touched when the call is allowed, so an empty list there means something.
    """

    service, store = _service(monkeypatch)

    _call(service, entry_point, ranking=SourceRanking.document())

    assert "search_source_chunks" in store.touched


@pytest.mark.parametrize("ranking", NOT_THE_DOCUMENT_RANKING)
@pytest.mark.parametrize("entry_point", STAGE_ENTRY_POINTS)
def test_every_other_ranking_is_refused_before_the_store_is_read(
    entry_point: str, ranking: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutations: remove the ``_require_document_ranking(ranking)`` call from ``_source_stage_lists``
    (every case then runs and returns); compare only ``ranking.mode`` in the helper (the two
    document cases that differ in lexical mode or cap then run); drop the ``isinstance`` clause
    (the equal-to-anything case then runs); or move the call below the first store read
    (``store.touched`` is then not empty).

    No stage honours a passage ranking yet, so accepting one and running the document stage
    would hand the caller a result that looks like the ranking it asked for.
    """

    service, store = _service(monkeypatch)

    with pytest.raises(VNextRetrievalValidationError):
        _call(service, entry_point, ranking=ranking)

    assert store.touched == []


# ---------------------------------------------------------------------------------------------
# The production callers write the document ranking
# ---------------------------------------------------------------------------------------------


def _vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext
    from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path

    for name in (
        "ALICE_EMBEDDINGS_BASE_URL",
        "ALICE_EMBEDDINGS_MODEL",
        "ALICE_EMBEDDINGS_API_KEY",
        AGENT_API_KEY_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)
    captured = call_mcp_tool(
        context,
        name="alice_capture",
        arguments={
            "raw_text": KETTLE_NOTE,
            "title": "Kitchen notes",
            "domain": "project",
            "sensitivity": "internal",
        },
    )
    assert captured["status"] == "imported", captured
    return context, database


def _spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[object]]:
    """Record the ``ranking`` each entry point receives, then run the real method."""

    seen: dict[str, list[object]] = {name: [] for name in STAGE_ENTRY_POINTS}
    for name in STAGE_ENTRY_POINTS:
        original = getattr(VNextRetrievalService, name)

        def wrapped(self, __original=original, __name=name, **kwargs):  # noqa: ANN001
            seen[__name].append(kwargs.get("ranking", "ranking was not passed"))
            return __original(self, **kwargs)

        monkeypatch.setattr(VNextRetrievalService, name, wrapped)
    return seen


@pytest.mark.parametrize(
    ("route", "entry_points"),
    [
        pytest.param("recall", ("search_source_excerpts", "_source_stage_lists"), id="alice_recall"),
        pytest.param("brief", ("search_source_excerpts", "_source_stage_lists"), id="session-brief"),
        pytest.param("pack", ("_source_stage_lists",), id="alice_context_pack"),
    ],
)
def test_the_three_production_callers_write_the_document_ranking(
    route: str, entry_points: tuple[str, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutations: in ``_handle_alice_recall``, ``compile_session_brief`` or ``compile_context_pack``
    pass a passage ``SourceRanking`` (and relax ``_require_document_ranking`` so the call still
    runs) or drop the ``ranking=`` argument; or stop ``search_source_excerpts`` passing its
    ``ranking`` on to ``_source_stage_lists``.

    Each route is run for real over a vault that holds one matching source. The control is that
    every named entry point was reached, so a route that stopped reading sources cannot pass by
    recording nothing.
    """

    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.session_briefing import compile_local_session_brief

    context, database = _vault(tmp_path, monkeypatch)
    seen = _spy(monkeypatch)

    if route == "recall":
        payload = call_mcp_tool(context, name="alice_recall", arguments={"query": "kettle shelf"})
        assert payload["sources"], "the fixture source was not found, so the route proves nothing"
    elif route == "pack":
        payload = call_mcp_tool(context, name="alice_context_pack", arguments={"query": "kettle shelf"})
        assert payload["sources"], "the fixture source was not found, so the route proves nothing"
    else:
        brief = compile_local_session_brief(
            database,
            user_id=USER_ID,
            query="kettle shelf",
            project_view=ProjectView.unscoped(),
            exclude_global_domains=frozenset(),
        )
        assert "second shelf" in brief, "the fixture source was not found, so the route proves nothing"

    for name in entry_points:
        assert seen[name], f"{route} never reached {name}"
        assert all(value == SourceRanking.document() for value in seen[name]), (name, seen[name])


# ---------------------------------------------------------------------------------------------
# No caller can forget, including one no test reaches yet
# ---------------------------------------------------------------------------------------------

_PRODUCT_ROOTS = ("apps/api/src", "scripts", "eval")

# (file, enclosing function, callee) -> the exact text of the ``ranking`` argument. A site the
# list does not name makes the inventory test fail, so adding a caller means stopping to say which
# ranking it wants. The recall handler is the one site the passage slice changes on purpose, and
# that slice edits this line with its own proof.
_EXPECTED_CALL_SITES = {
    (
        "apps/api/src/alicebot_api/mcp/retrieval.py",
        "_handle_alice_recall",
        "search_source_excerpts",
    ): "SourceRanking.document()",
    (
        "apps/api/src/alicebot_api/session_briefing.py",
        "compile_session_brief",
        "search_source_excerpts",
    ): "SourceRanking.document()",
    (
        "apps/api/src/alicebot_api/vnext_retrieval.py",
        "compile_context_pack",
        "_source_stage_lists",
    ): "SourceRanking.document()",
    (
        "apps/api/src/alicebot_api/vnext_retrieval.py",
        "search_source_excerpts",
        "_source_stage_lists",
    ): "ranking",
}


class _CallSites(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.stack: list[str] = []
        self.sites: dict[tuple[str, str, str], list[str | None]] = {}

    def _scoped(self, node: ast.AST, name: str) -> None:
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scoped(node, node.name)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        callee = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        if callee in STAGE_ENTRY_POINTS:
            argument = next((kw.value for kw in node.keywords if kw.arg == "ranking"), None)
            key = (self.path, self.stack[-1] if self.stack else "<module>", str(callee))
            self.sites.setdefault(key, []).append(None if argument is None else ast.unparse(argument))
        self.generic_visit(node)


def _call_sites() -> dict[tuple[str, str, str], list[str | None]]:
    sites: dict[tuple[str, str, str], list[str | None]] = {}
    for root in _PRODUCT_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if path.name.startswith("test_") or "tests" in path.relative_to(REPO_ROOT).parts:
                continue
            relative = path.relative_to(REPO_ROOT).as_posix()
            visitor = _CallSites(relative)
            visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=relative))
            sites.update(visitor.sites)
    return sites


def test_every_call_of_the_source_stage_in_the_product_names_its_ranking() -> None:
    """Mutations: delete the ``ranking=`` argument from any production call (a path no test reaches
    still fails here); change a production caller's value to anything but
    ``SourceRanking.document()``; make ``search_source_excerpts`` hard-code the value in its call
    to ``_source_stage_lists`` instead of passing its own ``ranking`` on; or add a caller that is
    not on the list.

    The behavioural test above runs the three routes. This one reads the source, so a call site
    that nothing exercises is still held to the rule.
    """

    sites = _call_sites()

    assert {key: values for key, values in sites.items()} == {
        key: [value] for key, value in _EXPECTED_CALL_SITES.items()
    }, (
        "a call of search_source_excerpts or _source_stage_lists changed, was added, or lost its ranking "
        "argument. Every caller writes the ranking it wants, so say which, and add it to the list."
    )
