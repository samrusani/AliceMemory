"""alice_recall and alice_context_pack refuse a query the source search cannot take.

On the SQLite vault the source search ORs one LIKE pattern per distinct search
term and binds each as one LIKE operand. SQLite fails it for a query with about
991 or more distinct terms ("Expression tree is too large") and, with a captured
source in the vault, for a pattern over 50,000 bytes ("LIKE or GLOB pattern too
complex"). The MCP server used to answer both with ``tool_execution_failed`` and
no detail. They now answer ``invalid_request`` and name the limit.

The limits are the session brief's, through one shared function
(``source_search_query_breach``). Sizes below are literal, so the constants can
move without the tests moving with them. Every test names the edit that makes
it fail.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from alicebot_api import source_search_limits
from alicebot_api.mcp.registry import _TOOL_HANDLERS, call_mcp_tool
from alicebot_api.mcp.types import MCPInvalidRequestError, MCPToolError
from alicebot_api.mcp_server import MCPServer
from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.source_ranking import SourceRanking
from alicebot_api.source_search_limits import (
    SourceSearchQueryBreach,
    SourceSearchQueryTooLarge,
    source_search_query_breach,
)
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_connections import ConnectionFinderRequest, VNextConnectionService
from alicebot_api.vnext_context_tree import ContextTreeRequest, VNextContextTreeService
from alicebot_api.vnext_contradictions import ContradictionFinderRequest, VNextContradictionService
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MODEL_ENV,
)
from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService
from alicebot_api.vnext_source_fence import SourceReadFence

USER_ID = "00000000-0000-0000-0000-000000000001"
SOURCE_SENTENCE = "The indigo-lighthouse-42 canary stays in the vault."
TOOLS = ("alice_recall", "alice_context_pack")
ALL_SENSITIVITY = ["public", "internal", "private", "unknown"]


def _terms(count: int) -> str:
    """``count`` distinct short words, none of them a stopword."""

    return " ".join(f"zq{index}x" for index in range(count))


def _refusal(message: str) -> dict[str, object]:
    return {"error": {"code": "invalid_request", "message": message}}


def _too_many_terms(count: int) -> dict[str, object]:
    return _refusal(f"query has {count} distinct search terms; the limit is 499. Use a shorter query.")


def _vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MCPRuntimeContext:
    for name in (EMBEDDINGS_BASE_URL_ENV, EMBEDDINGS_MODEL_ENV, EMBEDDINGS_API_KEY_ENV, AGENT_API_KEY_ENV):
        monkeypatch.delenv(name, raising=False)
    # alice_capture and alice_context_pack are core tools outside the default three.
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)
    captured = call_mcp_tool(
        context,
        name="alice_capture",
        arguments={
            "raw_text": f"# Vault canary\n\n{SOURCE_SENTENCE}\n",
            "title": "Vault canary",
            "domain": "personal",
            "sensitivity": "private",
        },
    )
    assert captured["status"] == "imported", captured
    committed = call_mcp_tool(
        context,
        name="alice_memory_commit",
        arguments={
            "title": "Launch decision",
            "canonical_text": "We will ship the public acme launch checklist on Thursday.",
            "memory_type": "decision",
            "domain": "project",
            "sensitivity": "public",
            "confidence": 0.96,
            "project_scope": ["acme"],
            "rationale": "User said: remember this",
        },
    )
    assert committed["status"] == "committed", committed
    return context


def _wire(context: MCPRuntimeContext, tool: str, arguments: dict[str, object]) -> tuple[bool, dict[str, object]]:
    """One ``tools/call`` through the stdio server, framed as a client sends it.

    Returns ``(isError, parsed content[0].text)``. ``content`` must hold exactly
    one item, serialized once.
    """

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
    ]
    stdin = b"".join(json.dumps(request).encode("utf-8") + b"\n" for request in requests)
    stdout = io.BytesIO()
    assert MCPServer(context=context, input_stream=io.BytesIO(stdin), output_stream=stdout).run() == 0
    responses = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines() if line.strip()]
    response = next(item for item in responses if item.get("id") == 2)
    result = response["result"]
    assert len(result["content"]) == 1
    return bool(result["isError"]), json.loads(result["content"][0]["text"])


def _accepted(context: MCPRuntimeContext, tool: str, query: str, **extra: object) -> dict[str, object]:
    is_error, payload = _wire(context, tool, {"query": query, **extra})
    assert is_error is False, payload
    return payload


def _refused(context: MCPRuntimeContext, tool: str, query: str, **extra: object) -> dict[str, object]:
    is_error, payload = _wire(context, tool, {"query": query, **extra})
    assert is_error is True
    return payload


# -- the rule itself, in literal sizes -----------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("indigo lighthouse canary", None),
        (_terms(10), None),
        (_terms(499), None),
        (_terms(500), ("terms", 500, 499)),
        (_terms(501), ("terms", 501, 499)),
        (_terms(990), ("terms", 990, 499)),
        (_terms(5_000), ("terms", 5_000, 499)),
        # A repeated word is one term: 5,000 words, 40 distinct.
        (" ".join(f"word{index % 40}" for index in range(5_000)), None),
        # Short distinct words, three characters a term: the densest way in.
        (" ".join(f"a{chr(97 + index % 26)}{index // 26}" for index in range(520)), ("terms", 520, 499)),
        ("canary " * 5_714 + "ab", None),
        ("canary " * 5_714 + "abc", ("bytes", 40_001, 40_000)),
        ("\u0390" * 6_666, None),
        ("\u0390" * 6_667, ("folded_bytes", 40_002, 40_000)),
        ("\u0390" * 9_000, ("folded_bytes", 54_000, 40_000)),
        # 45,000 raw bytes that casefold to 15,000: the raw count still counts.
        ("\u212a" * 15_000, ("bytes", 45_000, 40_000)),
    ],
    ids=[
        "short",
        "10-terms",
        "499-terms",
        "500-terms",
        "501-terms",
        "990-terms",
        "5000-terms",
        "repeated-words",
        "dense-520-terms",
        "40000-bytes",
        "40001-bytes",
        "6666-u0390",
        "6667-u0390",
        "9000-u0390",
        "15000-kelvin",
    ],
)
def test_the_limit_sits_at_499_terms_and_40000_bytes(query: str, expected: tuple[str, int, int] | None) -> None:
    """Literal sizes for the shared rule: 499 distinct terms and 40,000 bytes, raw and casefolded.

    The count is the search's own pattern list, the phrase plus one per
    distinct term, so 499 terms make the 500 patterns the brief has always
    allowed and 500 terms do not.

    Mutation: set ``SOURCE_SEARCH_QUERY_MAX_PATTERNS`` to 100, 250, 499, 501
    or 900, or ``<=`` becomes ``<``, or count every token instead of the
    distinct terms (repeated-words), or drop the raw byte check (Kelvin), or
    the casefolded one (U+0390), or set the byte limit to 50,000 or 39,999.
    """

    breach = source_search_query_breach(query)

    if expected is None:
        assert breach is None
    else:
        assert breach == SourceSearchQueryBreach(*expected)


def test_a_huge_query_is_refused_before_it_is_tokenized(monkeypatch: pytest.MonkeyPatch) -> None:
    """The raw byte check runs first, so a huge query is never tokenized or counted.

    A 5 MB query is refused on its byte count alone. The pattern counter would
    cost a pass over all of it, so it must not run.

    Mutation: move the pattern count above the byte checks in
    ``source_search_query_breach``. The counter runs on the 5 MB query and this
    test fails.
    """

    def counted(_query: str) -> list[str]:
        raise AssertionError("the pattern counter ran on a query the byte check refuses")

    monkeypatch.setattr(source_search_limits, "_search_patterns", counted)

    breach = source_search_query_breach("canary " * 700_000)

    assert breach == SourceSearchQueryBreach("bytes", 4_900_000, 40_000)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("\ud800 indigo", None),
        ("\ud800" * 13_333, None),
        ("\ud800" * 13_334, ("bytes", 40_002, 40_000)),
    ],
    ids=["short", "13333-surrogates", "13334-surrogates"],
)
def test_a_lone_surrogate_is_measured_not_raised(query: str, expected: tuple[str, int, int] | None) -> None:
    """A lone surrogate counts as 3 UTF-8 bytes. The check answers; it never raises.

    A JSON ``\\ud800`` escape decodes to a lone surrogate, which strict UTF-8
    cannot encode. The check measures the bytes the way the standard library
    writes them out with ``surrogatepass`` and stays a pure function that
    returns a breach or ``None``, so a caller never sees ``UnicodeEncodeError``
    from it.

    Mutation: encode the raw query, or the casefolded one, as strict ``utf-8``
    without ``surrogatepass``, or as ``replace`` (one byte per surrogate).
    ``UnicodeEncodeError`` escapes the check, or the count is 13,334 and not
    40,002, and this test fails. Casefolding leaves a lone surrogate as it is,
    so both encodes see it.
    """

    breach = source_search_query_breach(query)

    if expected is None:
        assert breach is None
    else:
        assert breach == SourceSearchQueryBreach(*expected)


# -- over stdio, as a client calls the tools -----------------------------------


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("terms", [500, 501, 990, 991, 1_000, 5_000])
def test_a_query_with_too_many_distinct_terms_is_refused_and_names_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, terms: int
) -> None:
    """500 distinct terms and up answer ``invalid_request`` with the count and the limit.

    On v0.19.0 the same call answers ``tool_execution_failed`` from 991 terms
    up and takes 500 to 990. The message is exact, so a limit that moves or a
    count that is off by one fails.

    Mutation: set ``SOURCE_SEARCH_QUERY_MAX_PATTERNS`` to 501 or 900 (the 500
    case is taken), or drop the term check, or report the limit as 500, or map
    ``MCPInvalidRequestError`` to ``tool_request_failed`` in the server.
    """

    context = _vault(tmp_path, monkeypatch)

    assert _refused(context, tool, _terms(terms)) == _too_many_terms(terms)


@pytest.mark.parametrize("tool", TOOLS)
def test_a_query_with_499_terms_is_taken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    """499 distinct terms still run the whole tool, exactly as before.

    Mutation: set ``SOURCE_SEARCH_QUERY_MAX_PATTERNS`` to 100, 250 or 499, or
    ``<=`` becomes ``<``. The call is refused and this test fails.
    """

    context = _vault(tmp_path, monkeypatch)

    payload = _accepted(context, tool, "indigo " + _terms(498))

    assert "error" not in payload


@pytest.mark.parametrize("tool", TOOLS)
def test_a_normal_query_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    """A short query finds the captured source, as it does on v0.19.0.

    Mutation: refuse every query, or route the query through the bound and cut
    it. The canary sentence is not in the answer and this test fails.
    """

    context = _vault(tmp_path, monkeypatch)

    payload = _accepted(context, tool, "indigo lighthouse canary")

    assert "indigo-lighthouse-42" in json.dumps(payload)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("canary " * 5_714 + "abc", "query is 40001 UTF-8 bytes; the limit is 40000. Use a shorter query."),
        ("canary " * 7_200, "query is 50399 UTF-8 bytes; the limit is 40000. Use a shorter query."),
        (
            "\u0390" * 6_667,
            "query is 40002 UTF-8 bytes after case folding; the limit is 40000. Use a shorter query.",
        ),
        (
            "\u0390" * 9_000,
            "query is 54000 UTF-8 bytes after case folding; the limit is 40000. Use a shorter query.",
        ),
        ("\u212a" * 15_000, "query is 45000 UTF-8 bytes; the limit is 40000. Use a shorter query."),
    ],
    ids=["40001-bytes", "50399-bytes", "6667-u0390", "9000-u0390", "15000-kelvin"],
)
def test_a_query_over_the_byte_limit_is_refused_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, query: str, message: str
) -> None:
    """Raw and casefolded UTF-8 bytes are each held to 40,000.

    The 40,001 byte query has three distinct patterns, so only the byte rule
    can refuse it. On v0.19.0 the 50,399 byte and the 9,000 U+0390 queries
    answer ``tool_execution_failed`` with a captured source in the vault.

    Mutation: drop the raw byte check (Kelvin and the plain cases are taken),
    drop the casefolded one (the U+0390 cases are taken), or measure
    characters instead of bytes.
    """

    context = _vault(tmp_path, monkeypatch)

    assert _refused(context, tool, query) == _refusal(message)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    ("query", "raw_bytes", "folded_bytes"),
    [("canary " * 5_714 + "ab", 40_000, 40_000), ("\u0390" * 6_666, 13_332, 39_996)],
    ids=["40000-bytes", "6666-u0390"],
)
def test_a_query_at_the_byte_limit_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, query: str, raw_bytes: int, folded_bytes: int
) -> None:
    """Exactly 40,000 raw bytes, and 39,996 casefolded bytes, run the whole tool.

    Mutation: ``>`` becomes ``>=`` in the byte checks, or the byte limit drops
    to 39,999 or 30,000. The call is refused and this test fails.
    """

    context = _vault(tmp_path, monkeypatch)

    assert len(query.encode("utf-8")) == raw_bytes
    assert len(query.casefold().encode("utf-8")) == folded_bytes
    assert "error" not in _accepted(context, tool, query)


@pytest.mark.parametrize("tool", TOOLS)
def test_the_refusal_never_repeats_the_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    """The message holds counts and fixed words. The query text never comes back.

    Mutation: build the message from the query, or hand the query to the
    client in the error. The sentinel is in the wire text and this test fails.
    """

    context = _vault(tmp_path, monkeypatch)
    sentinel = "PRIVATE-QUERY-SENTINEL"

    is_error, payload = _wire(context, tool, {"query": f"{sentinel} {_terms(600)}"})

    assert is_error is True
    assert payload == _too_many_terms(601)
    text = json.dumps(payload)
    assert sentinel not in text
    assert "zq0x" not in text


# -- where the refusal applies -------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [{"include_sources": False}, {"context_depth": "minimal"}],
    ids=["include_sources-false", "minimal-depth"],
)
@pytest.mark.parametrize("query_kind", ["1000-terms", "50100-bytes"])
def test_a_pack_that_does_not_search_sources_keeps_taking_a_long_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: dict[str, object], query_kind: str
) -> None:
    """With sources off the pack never runs the failing search, so it is not refused.

    Measured on v0.19.0: both of these answer a full pack for a 1,000 term
    query and a 50,100 byte query. Refusing them would take away a call that
    works.

    Mutation: make the pack's early check unconditional (drop ``if
    sources_enabled``), or refuse in the MCP argument parser before the
    pack's own flags are known. The pack is refused and this test fails.
    """

    context = _vault(tmp_path, monkeypatch)
    query = _terms(1_000) if query_kind == "1000-terms" else ("canary " * 7_200)[:50_100]

    assert "error" not in _accepted(context, "alice_context_pack", query, **extra)


# Every option that changes which stages a call runs, or whether the source
# search runs at all. The refusal must come before the first stage under each.
STAGE_ORDER_CASES = [
    pytest.param("alice_recall", {}, id="recall-default"),
    pytest.param("alice_recall", {"include_sources": False}, id="recall-include_sources-false"),
    pytest.param("alice_recall", {"context_depth": "minimal"}, id="recall-minimal"),
    pytest.param("alice_recall", {"context_depth": "high"}, id="recall-high"),
    pytest.param("alice_recall", {"debug": True}, id="recall-debug"),
    pytest.param("alice_recall", {"domains": ["personal", "project"]}, id="recall-domains"),
    pytest.param("alice_recall", {"project_scope": ["acme"]}, id="recall-project_scope"),
    pytest.param("alice_recall", {"projects": ["acme"]}, id="recall-projects"),
    pytest.param("alice_context_pack", {}, id="pack-default"),
    pytest.param("alice_context_pack", {"include_sources": True}, id="pack-include_sources-true"),
    pytest.param(
        "alice_context_pack", {"context_depth": "minimal", "include_sources": True}, id="pack-minimal-sources-on"
    ),
    pytest.param("alice_context_pack", {"context_depth": "medium"}, id="pack-medium"),
    pytest.param("alice_context_pack", {"context_depth": "high"}, id="pack-high"),
    pytest.param("alice_context_pack", {"project_scope": ["acme"]}, id="pack-project_scope"),
    pytest.param("alice_context_pack", {"projects": ["acme"]}, id="pack-projects"),
]


@pytest.mark.parametrize(("tool", "extra"), STAGE_ORDER_CASES)
def test_the_refusal_comes_before_any_retrieval_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, extra: dict[str, object]
) -> None:
    """A refused query runs no memory, vector or graph stage, under any option.

    A spy on the three stages first shows the stages do run for a normal query
    under the same options, so the test cannot pass on a spy that never fires
    or on an option that skips the stages for another reason. Without the early
    check the same typed error still comes back from ``search_sources``, but
    only after every stage has run, including the query embedding call when
    embeddings are configured.

    Mutation: drop ``require_source_query_searchable`` from the recall handler
    or from ``compile_context_pack``, or make either one conditional on an
    option (``include_sources``, ``context_depth``, ``debug``, ``domains``,
    ``project_scope`` or ``projects``). The stages run for the refused query
    under that option and this test fails.
    """

    context = _vault(tmp_path, monkeypatch)
    ran: list[str] = []
    for stage in ("_memory_fts_rows", "_memory_vector_rows", "_memory_graph_rows"):
        original = getattr(VNextRetrievalService, stage)

        def spy(self, *args, _original=original, _stage=stage, **kwargs):
            ran.append(_stage)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(VNextRetrievalService, stage, spy)

    _accepted(context, tool, "indigo lighthouse canary", **extra)
    assert "_memory_fts_rows" in ran
    ran.clear()

    assert _refused(context, tool, _terms(500), **extra) == _too_many_terms(500)
    assert ran == []

    assert _refused(context, tool, "canary " * 5_714 + "abc", **extra) == _refusal(
        "query is 40001 UTF-8 bytes; the limit is 40000. Use a shorter query."
    )
    assert ran == []


# -- every caller of search_sources --------------------------------------------


def _connection(tmp_path: Path):
    """A connection to the vault ``_vault`` built, for a real SQLite store."""

    return sqlite_user_connection(resolve_db_path(data_dir=str(tmp_path), db=None), USER_ID)


def test_the_sqlite_store_refuses_the_query_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``search_sources`` is the narrowest layer every caller shares, and it is guarded.

    Mutation: drop ``check_source_search_query`` from ``search_sources``. SQLite
    raises its own ``OperationalError`` here and every caller test below fails.
    """

    _vault(tmp_path, monkeypatch)
    with _connection(tmp_path) as connection:
        store = SQLiteVNextStore(connection, USER_ID)

        store.search_sources(query="indigo " + _terms(498), sensitivity_allowed=ALL_SENSITIVITY)
        with pytest.raises(SourceSearchQueryTooLarge) as caught:
            store.search_sources(query=_terms(500), sensitivity_allowed=ALL_SENSITIVITY)

    assert caught.value.breach == SourceSearchQueryBreach("terms", 500, 499)
    assert caught.value.public_message == "query has 500 distinct search terms; the limit is 499. Use a shorter query."


@pytest.mark.parametrize("query", [_terms(500), "\u0390" * 9_000, "canary " * 7_200], ids=["terms", "u0390", "bytes"])
def test_the_finders_the_retrieval_service_and_the_context_tree_get_the_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """Each service that hands an agent query to ``search_sources`` gets the typed error.

    On v0.19.0 each of these raises SQLite's ``OperationalError``. The finders
    call ``search_sources`` first, so the real SQLite store reaches it. The
    context tree needs store methods the SQLite store does not have (it is a
    Postgres-only tool), so it runs against a stand-in whose
    ``search_sources`` is the real one.

    Mutation: drop ``check_source_search_query`` from ``search_sources``, or
    make a service catch and replace the error.
    """

    class TreeStore:
        def __init__(self, real: SQLiteVNextStore) -> None:
            self._real = real

        def list_projects(self, **_kwargs: object) -> list[dict[str, object]]:
            return []

        def search_memories(self, **_kwargs: object) -> list[dict[str, object]]:
            return []

        def search_sources(self, **kwargs: object) -> list[dict[str, object]]:
            return self._real.search_sources(**kwargs)  # type: ignore[arg-type]

    _vault(tmp_path, monkeypatch)
    with _connection(tmp_path) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        sensitivity = ("public", "internal", "private", "unknown")

        with pytest.raises(SourceSearchQueryTooLarge):
            VNextContradictionService(store).generate_contradiction_report(  # type: ignore[arg-type]
                ContradictionFinderRequest(query=query, sensitivity_allowed=sensitivity)
            )
        with pytest.raises(SourceSearchQueryTooLarge):
            VNextConnectionService(store).generate_connection_report(  # type: ignore[arg-type]
                ConnectionFinderRequest(query=query, sensitivity_allowed=sensitivity)
            )
        with pytest.raises(SourceSearchQueryTooLarge):
            VNextContextTreeService(TreeStore(store)).build_tree(  # type: ignore[arg-type]
                ContextTreeRequest(query=query, sensitivity_allowed=sensitivity)
            )
        with pytest.raises(SourceSearchQueryTooLarge):
            VNextRetrievalService(store).search_source_excerpts(  # type: ignore[arg-type]
                query=query,
                domains=[],
                sensitivity_allowed=list(sensitivity),
                limit=8,
                scope=None,
                ranking=SourceRanking.document(),
            )
        with pytest.raises(SourceSearchQueryTooLarge):
            VNextRetrievalService(store).compile_context_pack(  # type: ignore[arg-type]
                VNextRetrievalRequest(query=query),
                source_fence=SourceReadFence.unfenced()
            )


@pytest.mark.parametrize("tool", ["alice_generate_contradictions", "alice_generate_connections", "alice_vnext_context_pack"])
def test_the_legacy_tools_answer_invalid_request_over_stdio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    """The legacy tools that reach ``search_sources`` answer the same typed error.

    Mutation: drop the store check, or raise ``SourceSearchQueryTooLarge`` past
    the ``ValueError`` clause in ``call_mcp_tool``. The code is
    ``tool_execution_failed`` or ``tool_request_failed`` and this test fails.
    """

    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    context = _vault(tmp_path, monkeypatch)

    assert _refused(context, tool, _terms(500)) == _too_many_terms(500)


def test_a_store_without_the_check_is_not_refused() -> None:
    """The early check belongs to the store, so a store with no such limit is not refused.

    The Postgres store has no expression-depth or LIKE-length limit and does not
    define ``check_source_search_query``, so a long recall query still runs
    there. A store that does define it decides.

    Mutation: make ``require_source_query_searchable`` apply the shared rule
    itself instead of asking the store. The first call raises and this test
    fails.
    """

    class NoLimitStore:
        pass

    class CountingStore:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def check_source_search_query(self, query: str) -> None:
            self.seen.append(query)

    VNextRetrievalService(NoLimitStore()).require_source_query_searchable(_terms(5_000))  # type: ignore[arg-type]
    counting = CountingStore()
    VNextRetrievalService(counting).require_source_query_searchable("hello there")  # type: ignore[arg-type]
    assert counting.seen == ["hello there"]


# -- the wire contract ---------------------------------------------------------


def test_the_store_error_reaches_the_client_as_invalid_request_not_a_generic_failure() -> None:
    """``call_mcp_tool`` turns the store's error into ``MCPInvalidRequestError``, exactly.

    ``SourceSearchQueryTooLarge`` is a ``ValueError``, and the clause below it
    turns a ``ValueError`` into a plain ``MCPToolError`` whose message the wire
    hides.

    Mutation: move the ``SourceSearchQueryTooLarge`` clause below the
    ``(TypeError, ValueError)`` clause in ``call_mcp_tool``. The error is a plain
    ``MCPToolError`` and this test fails.
    """

    breach = SourceSearchQueryBreach("terms", 500, 499)

    def refuse(_context: object, _arguments: object) -> dict[str, object]:
        raise SourceSearchQueryTooLarge(breach)

    original = _TOOL_HANDLERS["alice_recall"]
    _TOOL_HANDLERS["alice_recall"] = refuse  # type: ignore[assignment]
    try:
        context = MCPRuntimeContext(database_url="sqlite:///unused.db", user_id=USER_ID)
        with pytest.raises(MCPToolError) as caught:
            call_mcp_tool(context, name="alice_recall", arguments={"query": "x"})
    finally:
        _TOOL_HANDLERS["alice_recall"] = original

    assert type(caught.value) is MCPInvalidRequestError
    assert caught.value.public_message == breach.message


# -- one rule for the brief, the store and the tools ---------------------------


def test_the_brief_the_store_and_the_tools_share_one_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Move the shared constant and the brief, the store and the tools all move.

    A copy of the rule in any of the three would keep the old limit.

    Mutation: give the brief, the store or the recall handler its own copy of
    the limit or its own counting. One of the three assertions fails.
    """

    from alicebot_api.session_briefing import _bounded_useful_query

    context = _vault(tmp_path, monkeypatch)
    query = _terms(100)
    assert _bounded_useful_query(query) == query
    assert _accepted(context, "alice_recall", query)
    monkeypatch.setattr(source_search_limits, "SOURCE_SEARCH_QUERY_MAX_PATTERNS", 50)

    bounded = _bounded_useful_query(query)
    assert bounded is not None and bounded != query
    assert _refused(context, "alice_recall", query) == _too_many_terms_with_limit(100, 49)
    with _connection(tmp_path) as connection, pytest.raises(SourceSearchQueryTooLarge):
        SQLiteVNextStore(connection, USER_ID).search_sources(query=query, sensitivity_allowed=ALL_SENSITIVITY)


def _too_many_terms_with_limit(count: int, limit: int) -> dict[str, object]:
    return _refusal(f"query has {count} distinct search terms; the limit is {limit}. Use a shorter query.")
