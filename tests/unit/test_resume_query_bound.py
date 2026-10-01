"""alice_resume and alice_recent_decisions refuse a query the SQLite literal match cannot take.

Both tools hand the caller's query to four SQLite reads (``list_memories``,
``list_open_loops``, ``list_open_loop_events`` and ``list_resume_memory_events``)
that match it as one literal substring. Each binds ``'%' || lower(?) || '%'``
with the query backslash-escaped, and SQLite refuses an operand over 50,000
bytes ("LIKE or GLOB pattern too complex"). It does so only when a read reaches a
row, so v0.19.2 answered ``tool_execution_failed`` with no detail once the vault
held an active memory (``alice_resume``) or a stored decision
(``alice_recent_decisions``), and took the same query on an empty vault. They now
refuse it before any read, with ``invalid_request`` and a message that names the
limit.

The limit is the one the source search already uses, 40,000 bytes, counted raw
and again after the LIKE escape, from ``source_search_limits``. There is no
distinct-term count and no casefolded count, because the match is one operand
and SQLite's ``lower()`` folds only ASCII. Sizes below are literal, so the
constant can move without the tests moving with it. Every test names the edit
that makes it fail.
"""

from __future__ import annotations

import ast
import io
import json
from pathlib import Path

import pytest

from alicebot_api import source_search_limits
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.retrieval import (
    _require_literal_match_query,
    _vnext_recent_decisions,
    _vnext_resume,
)
from alicebot_api.mcp_server import MCPServer
from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.source_search_limits import (
    SourceSearchQueryBreach,
    SourceSearchQueryTooLarge,
    literal_match_operand,
    literal_match_query_breach,
    source_search_query_breach,
)
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MODEL_ENV,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "apps/api/src/alicebot_api"
USER_ID = "00000000-0000-0000-0000-000000000001"
TOOLS = ("alice_resume", "alice_recent_decisions")
ALL_SENSITIVITY = ("public", "internal", "private", "unknown")


def _refusal(message: str) -> dict[str, object]:
    return {"error": {"code": "invalid_request", "message": message}}


def _too_many_bytes(count: int) -> dict[str, object]:
    return _refusal(f"query is {count} UTF-8 bytes; the limit is 40000. Use a shorter query.")


def _too_many_escaped_bytes(count: int) -> dict[str, object]:
    return _refusal(
        f"query is {count} UTF-8 bytes after escaping %, _ and backslash; the limit is 40000. Use a shorter query."
    )


def _terms(count: int) -> str:
    """``count`` distinct short words, none of them a stopword."""

    return " ".join(f"zq{index}x" for index in range(count))


def _bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MCPRuntimeContext:
    for name in (EMBEDDINGS_BASE_URL_ENV, EMBEDDINGS_MODEL_ENV, EMBEDDINGS_API_KEY_ENV, AGENT_API_KEY_ENV):
        monkeypatch.delenv(name, raising=False)
    # alice_recent_decisions is a core tool outside the default three.
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _decide(context: MCPRuntimeContext, title: str, text: str) -> None:
    committed = call_mcp_tool(
        context,
        name="alice_memory_commit",
        arguments={
            "title": title,
            "canonical_text": text,
            "memory_type": "decision",
            "domain": "project",
            "sensitivity": "public",
            "confidence": 0.96,
            "rationale": "User said: remember this",
        },
    )
    assert committed["status"] == "committed", committed


def _vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MCPRuntimeContext:
    """A vault where every one of the four reads reaches a row.

    An active decision (its commit writes an event), and an open loop (its
    creation writes an event). On v0.19.2 each of the four reads then fails on a
    50,000 byte query.
    """

    context = _bootstrap(tmp_path, monkeypatch)
    _decide(context, "Launch decision", "We will ship the public acme launch checklist on Thursday.")
    with _connection(tmp_path) as connection:
        SQLiteVNextStore(connection, USER_ID).create_open_loop(
            {"title": "Ship the checklist", "domain": "project", "sensitivity": "public"}
        )
    return context


def _connection(tmp_path: Path):
    return sqlite_user_connection(resolve_db_path(data_dir=str(tmp_path), db=None), USER_ID)


def _wire(context: MCPRuntimeContext, tool: str, arguments: dict[str, object]) -> tuple[bool, dict[str, object]]:
    """One ``tools/call`` through the stdio server, framed as a client sends it."""

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
    ]
    stdin = b"".join(json.dumps(request).encode("utf-8") + b"\n" for request in requests)
    stdout = io.BytesIO()
    assert MCPServer(context=context, input_stream=io.BytesIO(stdin), output_stream=stdout).run() == 0
    responses = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines() if line.strip()]
    result = next(item for item in responses if item.get("id") == 2)["result"]
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
        ("launch checklist", None),
        ("a" * 40_000, None),
        ("a" * 40_001, ("bytes", 40_001, 40_000)),
        ("canary " * 7_199, ("bytes", 50_393, 40_000)),
        # Each of \, % and _ is one byte and escapes to two.
        ("_" * 20_000, None),
        ("_" * 20_001, ("escaped_bytes", 40_002, 40_000)),
        ("%" * 20_000, None),
        ("%" * 20_001, ("escaped_bytes", 40_002, 40_000)),
        ("\\" * 20_000, None),
        ("\\" * 20_001, ("escaped_bytes", 40_002, 40_000)),
        # SQLite fails 25,000 underscores: 50,000 escaped bytes and two %.
        ("_" * 25_000, ("escaped_bytes", 50_000, 40_000)),
        # 40,000 raw bytes that escape to 50,001: the escaped count still counts.
        ("a" * 29_999 + "_" * 10_001, ("escaped_bytes", 50_001, 40_000)),
        # Raw bytes come first, so this is a raw breach and not an escaped one.
        ("a" * 30_000 + "_" * 10_001, ("bytes", 40_001, 40_000)),
        # Not escaped, and SQLite's lower() does not grow them: raw bytes only.
        ("ΐ" * 20_000, None),
        ("ΐ" * 20_001, ("bytes", 40_002, 40_000)),
    ],
    ids=[
        "short",
        "40000-bytes",
        "40001-bytes",
        "50393-bytes",
        "20000-underscores",
        "20001-underscores",
        "20000-percents",
        "20001-percents",
        "20000-backslashes",
        "20001-backslashes",
        "25000-underscores",
        "raw-40000-escapes-to-50001",
        "raw-40001-wins",
        "20000-u0390",
        "20001-u0390",
    ],
)
def test_the_literal_match_limit_is_40000_raw_bytes_and_40000_escaped_bytes(
    query: str, expected: tuple[str, int, int] | None
) -> None:
    """Literal sizes for the rule: 40,000 UTF-8 bytes as sent, and again once ``\\``, ``%`` and ``_`` are escaped.

    The escaped count is the operand SQLite checks, less its two ``%``. SQLite
    fails 49,999 plain bytes and 25,000 underscores on the build measured.

    Mutation: drop the escaped byte check (the underscore, percent, backslash
    and mixed cases are taken), drop the raw one (a 40,001 byte query reports
    ``escaped_bytes``), swap the two so the escaped count goes first, set the
    limit to 50,000 or 39,999, or count casefolded bytes instead (U+0390 grows
    to 120,000 and is refused).
    """

    breach = literal_match_query_breach(query)

    if expected is None:
        assert breach is None
    else:
        assert breach == SourceSearchQueryBreach(*expected)


def test_a_huge_literal_query_is_refused_before_it_is_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The raw byte check runs first, so a huge query is never escaped.

    Mutation: move the escaped count above the raw byte check in
    ``literal_match_query_breach``. The escape runs on the 5 MB query and this
    test fails.
    """

    def escaped(_value: str) -> str:
        raise AssertionError("the escape ran on a query the raw byte check refuses")

    monkeypatch.setattr(source_search_limits, "_escape_like_literal", escaped)

    breach = literal_match_query_breach("canary " * 700_000)

    assert breach == SourceSearchQueryBreach("bytes", 4_900_000, 40_000)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("\ud800 launch", None),
        ("\ud800" * 13_333, None),
        ("\ud800" * 13_334, ("bytes", 40_002, 40_000)),
        ("\ud800" * 100 + "_" * 100, None),
    ],
    ids=["short", "13333-surrogates", "13334-surrogates", "surrogates-and-escapes"],
)
def test_a_lone_surrogate_is_measured_by_the_literal_match_not_raised(
    query: str, expected: tuple[str, int, int] | None
) -> None:
    """A lone surrogate counts as 3 UTF-8 bytes, in the raw count and in the escaped one.

    A JSON ``\\ud800`` escape decodes to a lone surrogate, which strict UTF-8
    cannot encode. The check returns a breach or ``None`` and never raises
    ``UnicodeEncodeError``.

    Mutation: encode the raw query, or the escaped one, as strict ``utf-8``
    without ``surrogatepass``, or as ``replace``. ``UnicodeEncodeError`` escapes
    the check, or the count is 13,334 and not 40,002, and this test fails. The
    last case reaches the escaped encode with a surrogate in it.
    """

    breach = literal_match_query_breach(query)

    if expected is None:
        assert breach is None
    else:
        assert breach == SourceSearchQueryBreach(*expected)


def test_literal_match_operand_is_the_escaped_query_or_the_typed_error() -> None:
    """``literal_match_operand`` returns the operand the reads used to bind, and refuses the rest.

    The escape is the one the reads always applied: a backslash before each
    ``\\``, ``%`` and ``_``. The refusal carries the breach, so the MCP server
    can name the limit.

    Mutation: return the query without escaping it, escape it twice, or return
    the operand without checking it.
    """

    assert literal_match_operand("100%_done\\") == "100\\%\\_done\\\\"
    assert literal_match_operand("launch") == "launch"
    with pytest.raises(SourceSearchQueryTooLarge) as caught:
        literal_match_operand("_" * 20_001)
    assert caught.value.breach == SourceSearchQueryBreach("escaped_bytes", 40_002, 40_000)
    assert caught.value.public_message == (
        "query is 40002 UTF-8 bytes after escaping %, _ and backslash; the limit is 40000. Use a shorter query."
    )


# -- over stdio, as a client calls the tools -----------------------------------


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("canary " * 5_714 + "abc", _too_many_bytes(40_001)),
        ("canary " * 7_200, _too_many_bytes(50_399)),
        ("a" * 49_999, _too_many_bytes(49_999)),
        ("a" * 60_000, _too_many_bytes(60_000)),
        ("ΐ" * 20_001, _too_many_bytes(40_002)),
        ("_" * 20_001, _too_many_escaped_bytes(40_002)),
        ("_" * 25_000, _too_many_escaped_bytes(50_000)),
        ("%" * 25_000, _too_many_escaped_bytes(50_000)),
        ("a" * 29_999 + "_" * 10_001, _too_many_escaped_bytes(50_001)),
    ],
    ids=[
        "40001-bytes",
        "50399-bytes",
        "49999-bytes",
        "60000-bytes",
        "20001-u0390",
        "20001-underscores",
        "25000-underscores",
        "25000-percents",
        "raw-40000-escapes-to-50001",
    ],
)
def test_a_query_over_the_limit_is_refused_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, query: str, expected: dict[str, object]
) -> None:
    """Both tools answer ``invalid_request`` with the measured size and the limit.

    On v0.19.2 the 49,999, 50,399 and 60,000 byte queries, the 25,000
    underscores and the 25,000 percents answer ``tool_execution_failed`` with no
    detail from a vault that holds an active memory and a stored decision, and
    the other cases are taken. The message is exact, so a limit that moves or a
    count that is off by one fails.

    Mutation: drop the escaped byte check (the underscore and percent cases are
    taken or fail inside SQLite), drop the raw one, map
    ``SourceSearchQueryTooLarge`` to ``tool_execution_failed`` in the server, or
    remove the store check and the handler check together.
    """

    context = _vault(tmp_path, monkeypatch)

    assert _refused(context, tool, query) == expected


@pytest.mark.parametrize("tool", TOOLS)
def test_the_refusal_does_not_depend_on_what_the_vault_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    """An empty vault refuses the query too. v0.19.2 took 60,000 bytes there.

    SQLite fails only a read that reaches a row, so a refusal that waits for
    SQLite answers differently for an empty vault and a full one. This one looks
    at the query alone.

    Mutation: refuse by catching SQLite's ``OperationalError`` instead of
    measuring the query. The empty vault runs no read that fails and the call
    is taken, so this test fails.
    """

    context = _bootstrap(tmp_path, monkeypatch)

    assert _refused(context, tool, "canary " * 7_200) == _too_many_bytes(50_399)
    assert _refused(context, tool, "_" * 25_000) == _too_many_escaped_bytes(50_000)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    ("query", "label"),
    [
        ("canary " * 5_714 + "ab", "40000 bytes"),
        ("_" * 20_000, "20000 underscores, 40000 bytes escaped"),
        ("ΐ" * 20_000, "20000 U+0390, 120000 bytes casefolded"),
        (_terms(4_000), "4000 distinct terms"),
        ("a" * 29_999 + "_" * 5_000, "raw 34999, escaped 39999"),
    ],
    ids=["40000-bytes", "20000-underscores", "20000-u0390", "4000-terms", "escaped-39999"],
)
def test_a_query_inside_the_limit_runs_the_whole_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, query: str, label: str
) -> None:
    """Queries at and under the limit are taken whole, with every read reaching a row.

    All five are taken by v0.19.2 too. The 4,000 distinct terms are the case the
    source search would refuse (it allows 499): this match is one operand, so
    the term count does not apply to it. The U+0390 query casefolds to 120,000
    bytes, which the source search would refuse: SQLite's ``lower()`` leaves it
    alone here.

    Mutation: lower the byte limit to 39,999 or 30,000, change ``>`` to ``>=``,
    hold this match to ``source_search_query_breach`` (the term count and the
    casefolded count refuse the last three), or count escaped bytes against 30,000.
    """

    context = _vault(tmp_path, monkeypatch)

    assert "error" not in _accepted(context, tool, query), label


@pytest.mark.parametrize("tool", TOOLS)
def test_a_normal_query_finds_the_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    """A short query matches as before, case-insensitively.

    Mutation: refuse every query, or route the query through a bound that cuts
    it. The decision is not in the answer and this test fails.
    """

    context = _vault(tmp_path, monkeypatch)

    payload = _accepted(context, tool, "LAUNCH checklist")

    assert "Launch decision" in json.dumps(payload)


def test_the_operand_is_escaped_as_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_`` and ``%`` in a query match themselves, not any character.

    The decision text ``alpha_beta`` is found by ``alpha_beta`` and not by
    ``alphaxbeta`` text, and ``100%`` does not match ``1000``.

    Mutation: ``literal_match_operand`` returns the query unescaped. The
    underscore matches ``x`` and the percent matches ``0`` and this test fails.
    """

    context = _bootstrap(tmp_path, monkeypatch)
    _decide(context, "Rollout literal", "The alpha_beta rollout reached 100% of users.")
    _decide(context, "Rollout wildcard", "The alphaxbeta rollout reached 1000 of users.")

    def titles(tool: str, query: str) -> list[str]:
        found = json.dumps(_accepted(context, tool, query))
        return [title for title in ("Rollout literal", "Rollout wildcard") if title in found]

    for tool in TOOLS:
        assert titles(tool, "alpha_beta") == ["Rollout literal"]
        assert titles(tool, "100%") == ["Rollout literal"]


@pytest.mark.parametrize("tool", TOOLS)
def test_the_refusal_never_repeats_the_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    """The message holds counts and fixed words. The query text never comes back.

    Mutation: build the message from the query, or hand the query to the client
    in the error. The sentinel is in the wire text and this test fails.
    """

    context = _vault(tmp_path, monkeypatch)
    sentinel = "PRIVATE-QUERY-SENTINEL"

    is_error, payload = _wire(context, tool, {"query": f"{sentinel} " + "canary " * 7_200})

    assert is_error is True
    assert payload == _too_many_bytes(len(sentinel) + 1 + 50_399)
    assert sentinel not in json.dumps(payload)


OPTION_CASES = [
    pytest.param(tool, extra, id=f"{tool}-{label}")
    for tool in TOOLS
    for label, extra in (
        ("none", {}),
        ("project", {"project": "acme"}),
        ("project_scope", {"project_scope": ["acme"]}),
        ("domains", {"domains": ["project"]}),
        ("sensitivity", {"sensitivity_allowed": ["public"]}),
        ("window", {"since": "2020-01-01T00:00:00Z", "until": "2099-01-01T00:00:00Z"}),
        ("thread", {"thread_id": "00000000-0000-0000-0000-0000000000aa"}),
    )
] + [
    pytest.param("alice_recent_decisions", {"limit": 1}, id="alice_recent_decisions-limit"),
    pytest.param("alice_resume", {"max_open_loops": 0, "max_recent_changes": 0}, id="alice_resume-no-loops-no-changes"),
]


@pytest.mark.parametrize(("tool", "extra"), OPTION_CASES)
def test_the_refusal_holds_under_every_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, extra: dict[str, object]
) -> None:
    """The fence, the window and the size arguments do not change the answer to a long query.

    ``alice_resume`` with no open loops and no recent changes runs only the two
    ``list_memories`` reads, and still refuses.

    Mutation: in ``list_memories``, call ``literal_match_operand`` only when no
    ``domains``, ``projects`` or time filter is given (escape by hand otherwise),
    and drop the handlers' checks. The project, project_scope, domains and window
    cases of ``alice_resume`` fail inside SQLite and this test fails.
    """

    context = _vault(tmp_path, monkeypatch)

    assert _refused(context, tool, "canary " * 7_200, **extra) == _too_many_bytes(50_399)


EMPTY_FENCE = {
    "effective_project_scope": (),
    "effective_domains": (),
    "effective_sensitivity_allowed": (),
}

# Called straight at the handler, because the fence a caller's policy resolves
# to is not an argument. ``alice_resume`` runs without open loops or recent
# changes, which bind the query without looking at the fence.
HANDLER_CALLS = {
    "resume": lambda context, query: _vnext_resume(
        context, {"query": query, "max_open_loops": 0, "max_recent_changes": 0}, **EMPTY_FENCE
    ),
    "recent_decisions": lambda context, query: _vnext_recent_decisions(
        context, arguments={"query": query}, limit=5, **EMPTY_FENCE
    ),
}


@pytest.mark.parametrize("handler", sorted(HANDLER_CALLS))
@pytest.mark.parametrize("populated", [True, False], ids=["populated", "empty"])
def test_a_caller_whose_fence_admits_nothing_is_refused_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler: str, populated: bool
) -> None:
    """An empty sensitivity fence reads nothing, and a long query is still refused.

    ``list_memories`` returns before it builds the query clause when the fence
    allows no sensitivity, so no read binds the operand and SQLite never sees
    it. The handler's own check is what keeps the answer the same as for any
    other caller. A short query under the same fence is taken, and finds nothing
    although the vault holds the decision.

    Mutation: drop ``_require_literal_match_query`` from either handler. No read
    binds the query, the call is taken, and this test fails.
    """

    context = _vault(tmp_path, monkeypatch) if populated else _bootstrap(tmp_path, monkeypatch)
    call = HANDLER_CALLS[handler]

    with pytest.raises(SourceSearchQueryTooLarge) as caught:
        call(context, "canary " * 7_200)
    assert caught.value.breach == SourceSearchQueryBreach("bytes", 50_399, 40_000)

    assert "Launch decision" not in json.dumps(call(context, "launch"))


def test_a_store_without_the_check_is_not_refused() -> None:
    """The early check belongs to the store, so a store with no such limit is not refused.

    The Postgres store binds the query as an ``ILIKE`` operand with no length
    limit and does not define ``check_literal_match_query``, so a long query
    still runs there. A store that does define it decides, and no query means
    nothing to check.

    Mutation: make ``_require_literal_match_query`` apply the shared rule
    itself instead of asking the store. The first call raises and this test
    fails.
    """

    class NoLimitStore:
        pass

    class CountingStore:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def check_literal_match_query(self, query: str) -> None:
            self.seen.append(query)

    _require_literal_match_query(NoLimitStore(), "canary " * 7_200)
    counting = CountingStore()
    _require_literal_match_query(counting, "hello there")
    _require_literal_match_query(counting, None)
    assert counting.seen == ["hello there"]


# -- the four reads, at the narrowest layer every caller shares ----------------

READS = {
    "list_memories": (
        lambda store, query: store.list_memories(
            status=None, statuses=("active",), memory_types=("decision",), query=query
        ),
        "launch",
    ),
    "list_open_loops": (
        lambda store, query: store.list_open_loops(status=None, statuses=("open",), query=query),
        "checklist",
    ),
    "list_open_loop_events": (
        lambda store, query: store.list_open_loop_events(statuses=("open",), query=query),
        "checklist",
    ),
    "list_resume_memory_events": (
        lambda store, query: store.list_resume_memory_events(statuses=("active",), query=query),
        "launch",
    ),
}


@pytest.mark.parametrize("read", sorted(READS))
def test_each_literal_read_refuses_the_query_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read: str
) -> None:
    """Each of the four reads refuses an over-limit query on its own, with the typed error.

    A short query finds a row in each read, so the vault does reach the LIKE.
    Then the same read, called directly and not through a tool, raises
    ``SourceSearchQueryTooLarge`` for 50,399 bytes and for 25,000 underscores,
    and takes 40,000 bytes and 20,000 underscores. On v0.19.2 the two refused
    queries raise SQLite's ``OperationalError`` from each of the four.

    Mutation: in the one read under test, bind ``_escape_like_literal(query)``
    in place of ``literal_match_operand(query)`` (the four sites are in
    ``sqlite_store.py``, ``vnext_stores/sqlite/memory_access.py`` and
    ``vnext_stores/sqlite/graph_open_loops.py``, two there). SQLite raises its own
    error from that read and its case fails.
    """

    call, hit = READS[read]
    _vault(tmp_path, monkeypatch)
    with _connection(tmp_path) as connection:
        store = SQLiteVNextStore(connection, USER_ID)

        assert call(store, hit), "the vault must reach the LIKE in this read"
        with pytest.raises(SourceSearchQueryTooLarge) as caught:
            call(store, ("canary " * 7_200).strip())
        assert caught.value.breach == SourceSearchQueryBreach("bytes", 50_399, 40_000)
        with pytest.raises(SourceSearchQueryTooLarge) as caught:
            call(store, "_" * 25_000)
        assert caught.value.breach == SourceSearchQueryBreach("escaped_bytes", 50_000, 40_000)
        assert call(store, "a" * 40_000) == []
        assert call(store, "_" * 20_000) == []


def test_the_store_check_refuses_a_literal_query_and_takes_one_inside_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``check_literal_match_query`` is the store's early check, with the same rule.

    Mutation: make the method do nothing, or call ``require_source_search_query``
    in its place (the 4,000 term query is then refused).
    """

    _vault(tmp_path, monkeypatch)
    with _connection(tmp_path) as connection:
        store = SQLiteVNextStore(connection, USER_ID)

        store.check_literal_match_query(_terms(4_000))
        store.check_literal_match_query("_" * 20_000)
        with pytest.raises(SourceSearchQueryTooLarge) as caught:
            store.check_literal_match_query("_" * 20_001)

    assert caught.value.breach == SourceSearchQueryBreach("escaped_bytes", 40_002, 40_000)


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            function = child.func
            if isinstance(function, ast.Name):
                names.add(function.id)
            elif isinstance(function, ast.Attribute):
                names.add(function.attr)
    return names


def test_no_sqlite_read_builds_the_literal_predicate_without_the_check() -> None:
    """A SQLite function that builds the literal LIKE predicate binds its operand through the check.

    ``_sqlite_ascii_literal_contains_sql`` is the predicate every one of the four
    reads uses. A fifth read that uses it with ``_escape_like_literal`` would
    carry no limit, and v0.19.2 failed the same way for the four it had. So in
    the SQLite store modules every function that calls the predicate builder
    must call ``literal_match_operand`` and must not call ``_escape_like_literal``,
    and the four reads are the ones that do.

    Mutation: bind ``_escape_like_literal(...)`` in place of
    ``literal_match_operand(...)`` in any of the four reads, or add a read that
    calls ``_sqlite_ascii_literal_contains_sql`` and escapes by itself.
    """

    paths = [SOURCE_ROOT / "sqlite_store.py", *sorted((SOURCE_ROOT / "vnext_stores/sqlite").glob("*.py"))]
    checked: set[str] = set()
    for path in paths:
        if path.name == "query_predicates.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.FunctionDef):
                continue
            called = _called_names(node)
            if "_sqlite_ascii_literal_contains_sql" in called:
                assert "literal_match_operand" in called, f"{path.name}:{node.name} binds a query without the check"
                assert "_escape_like_literal" not in called, f"{path.name}:{node.name} escapes by itself"
                checked.add(node.name)
            else:
                assert "_escape_like_literal" not in called, f"{path.name}:{node.name} escapes a query by itself"

    assert checked == {
        "list_memories",
        "list_open_loops",
        "list_open_loop_events",
        "list_resume_memory_events",
    }


def test_the_literal_match_shares_the_source_search_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Move the shared byte constant and both the source search and the literal match move.

    It is the same 50,000 byte SQLite operand with the same margin, so there is
    one number to learn and one place to change it.

    Mutation: give the literal match its own byte constant. Its limit stays at
    40,000 and the 1,500 byte query is taken.
    """

    context = _vault(tmp_path, monkeypatch)
    monkeypatch.setattr(source_search_limits, "SOURCE_SEARCH_QUERY_MAX_BYTES", 1_000)

    assert source_search_query_breach("a" * 1_500) == SourceSearchQueryBreach("bytes", 1_500, 1_000)
    for tool in TOOLS:
        assert _refused(context, tool, "a" * 1_500) == _refusal(
            "query is 1500 UTF-8 bytes; the limit is 1000. Use a shorter query."
        )
        assert "error" not in _accepted(context, tool, "a" * 900)


# -- the HTTP routes -----------------------------------------------------------


def test_no_http_route_builds_the_sqlite_store() -> None:
    """The HTTP API reads the Postgres store, which has no LIKE operand limit, so it is unchanged.

    The four reads are SQLite store methods. A route module that imported the
    SQLite store, or the helper that opens it, could hand a caller's query to
    them. None does, so no HTTP route reaches the failure this closes.

    Mutation: add ``from alicebot_api.sqlite_store import SQLiteVNextStore`` to
    ``main.py`` or to any module under ``routers/``.
    """

    forbidden_modules = {"alicebot_api.sqlite_store"}
    forbidden_names = {"SQLiteVNextStore", "sqlite_user_connection"}
    paths = [SOURCE_ROOT / "main.py", *sorted((SOURCE_ROOT / "routers").glob("*.py"))]
    assert len(paths) > 5
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in forbidden_modules, path.name
                assert not {alias.name for alias in node.names} & forbidden_names, path.name
            elif isinstance(node, ast.Import):
                assert not {alias.name for alias in node.names} & forbidden_modules, path.name
