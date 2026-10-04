"""What the docs say about the legacy `alice_vnext_*` tools on SQLite is what a call does.

With `ALICE_MCP_LEGACY_TOOLS=1` the server lists every legacy tool, and on the SQLite backend the `alice_vnext_*` ones
split three ways: a call runs, a call fails because the SQLite store has no method the handler calls, or a call is
refused because the tool needs the Postgres backend. Five pages say which tool is which, in lists and sentences written
by hand. Nothing derived them, so a store method added or removed would leave every page agreeing with itself and wrong.

This test calls every `alice_vnext_*` legacy tool through `call_mcp_tool` on one temporary SQLite vault, with the legacy
flag on and no agent key, and sorts each call into one of the three. It writes no split down: the split is what the
calls do. It then reads the pages against that split: the lists in "Legacy tool surface" of `docs/alpha/mcp-tools.md`,
the list that runs in `docs/integrations/mcp.md`, the count and the pointer in `docs/alpha/known-limitations.md` and in
`docs/alpha/custom-agent-guide.md`, and the one pair of tools `docs/alpha/first-memory.md` names. The count words in the
pages are checked against the lists too.

The calls stay on one vault in this process: no network, no socket, no provider endpoint (the provider variables are
removed).

Mutations, each one alone: move `alice_vnext_context_tree` from the failing list to the running list in
`docs/alpha/mcp-tools.md`; change "Thirteen" to "Twelve" in `docs/integrations/mcp.md`; change "the five" to "the four"
before `alice_vnext_scheduler_*` in `docs/alpha/mcp-tools.md`; change "except the thirteen that run" to "except the
twelve that run" in `docs/alpha/known-limitations.md`; give `SQLiteVNextStore` a `get_artifact` method that answers a row,
so `alice_vnext_artifact_get` runs; take `list_provenance_links` away from `SQLiteVNextStore`, so
`alice_vnext_memory_audit` fails. Each fails a test below, and the last two are also written out as tests that show
this test can fail (the second by replacing the method with one that raises what a missing method raises).
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
from pathlib import Path
import re
import uuid
from uuid import UUID

import pytest

from alicebot_api import mcp_server
from alicebot_api.mcp import registry
from alicebot_api.mcp.types import MCPPreconditionFailedError, MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore

ROOT = Path(__file__).resolve().parents[2]
TOOLS_PAGE = "docs/alpha/mcp-tools.md"
GUIDE_PAGE = "docs/integrations/mcp.md"
LIMITS_PAGE = "docs/alpha/known-limitations.md"
CUSTOM_AGENT_PAGE = "docs/alpha/custom-agent-guide.md"
FIRST_MEMORY_PAGE = "docs/alpha/first-memory.md"

_USER_ID = "00000000-0000-0000-0000-000000000001"
_IDENTITY = {
    "agent_id": "legacy-tools-doc-check",
    "agent_type": "coding_agent",
    "permission_profile": "trusted_local_agent",
}
_PROVIDER_ENV = (
    "ALICE_AGENT_API_KEY",
    "ALICE_EMBEDDINGS_BASE_URL",
    "ALICE_EMBEDDINGS_MODEL",
    "ALICE_EMBEDDINGS_API_KEY",
    "ALICE_FACT_KEYS_BASE_URL",
    "ALICE_FACT_KEYS_MODEL",
    "ALICE_FACT_KEYS_API_KEY",
)
# A value for each required argument the `alice_vnext_*` schemas ask for. A new required name raises a KeyError that
# says so, instead of guessing a value.
_SAMPLE_VALUES: dict[str, object] = {
    "query": "billing",
    "raw_text": "The billing service deploys on Thursdays.",
    "title": "Billing deploy day",
    "content": "An agent output that says the billing service deploys on Thursdays.",
    "canonical_text": "The billing service deploys on Thursdays.",
    "task_type": "general",
    "instructions": "Check the billing deploy day.",
    "workflow_type": "daily_brief",
    "action": "approve",
}
_ID_NAMES = ("project_id", "artifact_id")
_COUNT_WORDS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen twenty"
).split()

RUNS = "runs"
FAILS = "fails on a missing store method"
NEEDS_POSTGRES = "refuses as needing Postgres"


def _word(number: int) -> str:
    return _COUNT_WORDS[number] if number < len(_COUNT_WORDS) else str(number)


@dataclass(frozen=True)
class Split:
    runs: frozenset[str]
    fails: frozenset[str]
    needs_postgres: frozenset[str]


def _legacy_vnext_definitions() -> list[dict[str, object]]:
    return [tool for tool in registry._LEGACY_TOOL_DEFINITIONS if str(tool["name"]).startswith("alice_vnext_")]


@pytest.fixture
def legacy_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MCPRuntimeContext:
    """One SQLite vault, a keyless context and the legacy flag on."""

    for name in _PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=_USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=UUID(_USER_ID))


def _commit(context: MCPRuntimeContext, text: str, confidence: float) -> dict[str, object]:
    return registry.call_mcp_tool(
        context,
        name="alice_memory_commit",
        arguments={
            "title": "Legacy tools doc check",
            "canonical_text": text,
            "domain": "project",
            "sensitivity": "internal",
            "confidence": confidence,
            **_IDENTITY,
        },
    )


def _arguments(context: MCPRuntimeContext, definition: dict[str, object], serial: int) -> dict[str, object]:
    """The smallest call the schema accepts, with a fresh memory or confirmation when the tool names one."""

    schema = definition["inputSchema"]
    assert isinstance(schema, dict)
    properties = schema["properties"]
    assert isinstance(properties, dict)
    arguments: dict[str, object] = {key: value for key, value in _IDENTITY.items() if key in properties}
    if "memory_id" in properties:
        memory = _commit(context, f"Legacy tool check memory number {serial} says deploys happen on Thursdays.", 0.95)
        arguments["memory_id"] = str(memory["memory"]["id"])  # type: ignore[index]
    if "confirmation_id" in properties:
        pending = _commit(context, f"Legacy tool check pending memory number {serial} awaits a yes.", 0.7)
        arguments["confirmation_id"] = str(pending["confirmation_id"])
    for field in schema.get("required", []):
        if field in arguments:
            continue
        arguments[field] = str(uuid.uuid4()) if field in _ID_NAMES else _SAMPLE_VALUES[field]
    return arguments


def _classify(context: MCPRuntimeContext, name: str, arguments: dict[str, object]) -> str:
    """Call the tool and say which of the three it did. Any other outcome is an error, never a guess."""

    try:
        registry.call_mcp_tool(context, name=name, arguments=arguments)
    except MCPPreconditionFailedError as exc:
        assert "Postgres backend" in str(exc), f"{name} was refused for a reason this test does not know: {exc}"
        return NEEDS_POSTGRES
    except AttributeError as exc:
        assert "'SQLiteVNextStore' object has no attribute" in str(exc), f"{name} failed another way: {exc!r}"
        return FAILS
    return RUNS


def _derive_split(context: MCPRuntimeContext) -> tuple[Split, dict[str, dict[str, object]]]:
    sorted_names: dict[str, set[str]] = {RUNS: set(), FAILS: set(), NEEDS_POSTGRES: set()}
    calls: dict[str, dict[str, object]] = {}
    for serial, definition in enumerate(_legacy_vnext_definitions()):
        name = str(definition["name"])
        calls[name] = _arguments(context, definition, serial)
        sorted_names[_classify(context, name, calls[name])].add(name)
    split = Split(
        runs=frozenset(sorted_names[RUNS]),
        fails=frozenset(sorted_names[FAILS]),
        needs_postgres=frozenset(sorted_names[NEEDS_POSTGRES]),
    )
    return split, calls


# --- Reading the pages ---------------------------------------------------------------------------------------------


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, heading: str) -> str:
    marker = f"\n## {heading}\n"
    assert text.count(marker) == 1, f"expected exactly one '## {heading}' heading"
    start = text.index(marker)
    end = text.find("\n## ", start + 1)
    return _flat(text[start : end if end != -1 else len(text)])


def _the_sentence_with(section: str, anchor: str, page: str) -> str:
    sentences = [sentence for sentence in re.split(r"(?<=\.)\s+", section) if anchor in sentence]
    assert len(sentences) == 1, f"{page}: expected exactly one sentence that says {anchor!r}, found {len(sentences)}"
    return sentences[0]


_TOOL_TOKEN = re.compile(r"`(alice_vnext_[a-z_]*\*?)`")


def _names_in(text: str, known: set[str]) -> set[str]:
    """The tools a passage names. `alice_vnext_scheduler_*` names every tool that starts that way; the bare family
    name `alice_vnext_*` names none."""

    names: set[str] = set()
    for token in _TOOL_TOKEN.findall(text):
        if token == "alice_vnext_*":
            continue
        if token.endswith("*"):
            names |= {name for name in known if name.startswith(token[:-1])}
        else:
            names.add(token)
    return names


def _same_tools(page: str, what: str, written: set[str], derived: frozenset[str], known: set[str]) -> list[str]:
    problems: list[str] = []
    if written - known:
        problems.append(f"{page}: names {sorted(written - known)} as {what}, and the registry has no such tool")
    if written != set(derived):
        problems.append(
            f"{page}: lists {sorted(written)} as {what}; the calls give {sorted(derived)} "
            f"(only in the page: {sorted(written - derived)}, only in the calls: {sorted(set(derived) - written)})"
        )
    return problems


def _problems(split: Split, pages: dict[str, str]) -> list[str]:
    """Every way the five pages disagree with the split the calls gave. An empty list means they agree."""

    known = {str(tool["name"]) for tool in _legacy_vnext_definitions()}
    problems: list[str] = []

    # docs/alpha/mcp-tools.md: the failing list, the Postgres list and the running list.
    tools = _section(pages[TOOLS_PAGE], "Legacy tool surface")
    failing = _the_sentence_with(tools, "fail with `tool_execution_failed`", TOOLS_PAGE)
    problems += _same_tools(TOOLS_PAGE, "failing", _names_in(failing, known), split.fails, known)

    postgres = _the_sentence_with(tools, "need Postgres and refuse the call", TOOLS_PAGE)
    problems += _same_tools(TOOLS_PAGE, "needing Postgres", _names_in(postgres, known), split.needs_postgres, known)
    family_count = re.search(r"\bthe (?P<word>\w+) `(?P<prefix>alice_vnext_[a-z_]+)\*`", postgres)
    if family_count is None:
        problems.append(f"{TOOLS_PAGE}: the Postgres sentence no longer says how many `alice_vnext_scheduler_*` tools")
    else:
        in_family = {name for name in split.needs_postgres if name.startswith(family_count.group("prefix"))}
        if family_count.group("word") != _word(len(in_family)):
            problems.append(
                f"{TOOLS_PAGE}: says {family_count.group('word')!r} `{family_count.group('prefix')}*` tools need "
                f"Postgres; the calls give {len(in_family)}"
            )

    running = _the_sentence_with(tools, " run on SQLite:", TOOLS_PAGE)
    problems += _same_tools(TOOLS_PAGE, "running", _names_in(running, known), split.runs, known)
    if not running.startswith(f"{_word(len(split.runs)).capitalize()} `alice_vnext_*` tools run on SQLite"):
        problems.append(f"{TOOLS_PAGE}: the running sentence does not open with the count {len(split.runs)}")
    family = re.search(r"memory-commit family \((?P<names>[^)]*)\)", running)
    reads = re.search(r"and (?P<word>\w+) reads and captures \((?P<names>[^)]*)\)", running)
    reads_count = 0
    if family is None or reads is None:
        problems.append(f"{TOOLS_PAGE}: the running sentence no longer splits into the memory-commit family and the reads")
    else:
        family_names = _names_in(family.group("names"), known)
        read_names = _names_in(reads.group("names"), known)
        reads_count = len(read_names)
        if family_names & read_names:
            problems.append(f"{TOOLS_PAGE}: names {sorted(family_names & read_names)} in both groups of the running list")
        if reads.group("word") != _word(len(read_names)):
            problems.append(
                f"{TOOLS_PAGE}: says {reads.group('word')!r} reads and captures and names {len(read_names)}"
            )

    # docs/integrations/mcp.md: the list that runs.
    guide = _section(pages[GUIDE_PAGE], "Legacy Tool Surface")
    guide_running = _the_sentence_with(guide, " tools run there:", GUIDE_PAGE)
    problems += _same_tools(GUIDE_PAGE, "running", _names_in(guide_running, known), split.runs, known)
    if not guide_running.startswith(f"{_word(len(split.runs)).capitalize()} `alice_vnext_*` tools run there"):
        problems.append(f"{GUIDE_PAGE}: the running sentence does not open with the count {len(split.runs)}")
    if "(../alpha/mcp-tools.md#legacy-tool-surface)" not in guide:
        problems.append(f"{GUIDE_PAGE}: no longer points at the Legacy tool surface list in mcp-tools.md")

    # docs/alpha/known-limitations.md: the count that runs, the reads and the pointer.
    bullets = [_flat(bullet) for bullet in re.split(r"\n(?=- )", pages[LIMITS_PAGE])]
    anchored = [bullet for bullet in bullets if "every `alice_vnext_*` tool except" in bullet]
    if len(anchored) != 1:
        problems.append(f"{LIMITS_PAGE}: expected one bullet that says 'every `alice_vnext_*` tool except'")
    else:
        limit_bullet = anchored[0]
        expected_run_count = f"except the {_word(len(split.runs))} that run"
        if expected_run_count not in limit_bullet:
            problems.append(f"{LIMITS_PAGE}: does not say {expected_run_count!r}")
        if reads_count and f"and {_word(reads_count)} reads and captures" not in limit_bullet:
            problems.append(f"{LIMITS_PAGE}: does not say 'and {_word(reads_count)} reads and captures'")
        if "(mcp-tools.md#legacy-tool-surface)" not in limit_bullet:
            problems.append(f"{LIMITS_PAGE}: no longer points at the Legacy tool surface list in mcp-tools.md")

    # docs/alpha/custom-agent-guide.md and docs/alpha/first-memory.md say the same thing about single tools.
    custom = _flat(pages[CUSTOM_AGENT_PAGE])
    failing_example = _the_sentence_with(custom, "most legacy tools fail", CUSTOM_AGENT_PAGE)
    for name in _names_in(failing_example, known):
        if name not in split.fails:
            problems.append(f"{CUSTOM_AGENT_PAGE}: gives {name} as a failing tool; the calls sort it as running or refused")
    only_run = _the_sentence_with(custom, "Of the `alice_vnext_*` tools, only", CUSTOM_AGENT_PAGE)
    if f"only {_word(len(split.runs))} run" not in only_run:
        problems.append(f"{CUSTOM_AGENT_PAGE}: does not say 'only {_word(len(split.runs))} run'")
    if reads_count and f"and {_word(reads_count)} reads and captures" not in only_run:
        problems.append(f"{CUSTOM_AGENT_PAGE}: does not say 'and {_word(reads_count)} reads and captures'")
    if "(mcp-tools.md#legacy-tool-surface)" not in only_run:
        problems.append(f"{CUSTOM_AGENT_PAGE}: no longer points at the Legacy tool surface list in mcp-tools.md")

    pair = re.search(
        r"`(?P<runs>alice_vnext_[a-z_]+)` runs on SQLite and `(?P<not>alice_vnext_[a-z_]+)` does not",
        _flat(pages[FIRST_MEMORY_PAGE]),
    )
    if pair is None:
        problems.append(f"{FIRST_MEMORY_PAGE}: no longer says which legacy commit tool runs on SQLite and which does not")
    else:
        if pair.group("runs") not in split.runs:
            problems.append(f"{FIRST_MEMORY_PAGE}: says {pair.group('runs')} runs on SQLite; the calls say it does not")
        if pair.group("not") in split.runs:
            problems.append(f"{FIRST_MEMORY_PAGE}: says {pair.group('not')} does not run on SQLite; the calls say it does")

    if "\n## Legacy tool surface\n" not in pages[TOOLS_PAGE]:
        problems.append(f"{TOOLS_PAGE}: has no 'Legacy tool surface' heading for the other pages to point at")
    return problems


def _pages() -> dict[str, str]:
    return {
        page: (ROOT / page).read_text(encoding="utf-8")
        for page in (TOOLS_PAGE, GUIDE_PAGE, LIMITS_PAGE, CUSTOM_AGENT_PAGE, FIRST_MEMORY_PAGE)
    }


# --- The tests -----------------------------------------------------------------------------------------------------


def test_every_alice_vnext_legacy_tool_is_sorted_by_calling_it(legacy_context: MCPRuntimeContext) -> None:
    split, calls = _derive_split(legacy_context)
    known = {str(tool["name"]) for tool in _legacy_vnext_definitions()}

    assert known, "no alice_vnext_* legacy tool is defined, so this test checks nothing"
    assert set(calls) == known
    assert split.runs | split.fails | split.needs_postgres == known
    assert not (split.runs & split.fails) and not (split.runs & split.needs_postgres)
    assert not (split.fails & split.needs_postgres)
    # Each kind exists, so each list in the pages is checked against something.
    assert split.runs and split.fails and split.needs_postgres


def test_the_pages_list_the_legacy_tools_that_run_fail_and_need_postgres_as_the_calls_sort_them(
    legacy_context: MCPRuntimeContext,
) -> None:
    split, _calls = _derive_split(legacy_context)

    assert _problems(split, _pages()) == []


def test_the_tools_the_page_says_fail_answer_tool_execution_failed_over_the_wire(
    legacy_context: MCPRuntimeContext,
) -> None:
    """The page names a wire code for the failing list, so the server is asked, not the registry."""

    split, calls = _derive_split(legacy_context)
    assert split.fails

    for name in sorted(split.fails):
        server = mcp_server.MCPServer(context=legacy_context, input_stream=BytesIO(), output_stream=BytesIO())
        response = server._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": calls[name]}}
        )
        assert response is not None
        result = response["result"]
        assert isinstance(result, dict)
        assert result["isError"] is True, name
        error = json.loads(result["content"][0]["text"])["error"]
        assert error["code"] == "tool_execution_failed", name


# --- The test can fail ---------------------------------------------------------------------------------------------


def _moved(page_text: str, old: str, new: str) -> str:
    """The page with one phrase replaced. The phrase is found across line breaks, and the rest of the page is kept
    as it is."""

    pattern = r"\s+".join(re.escape(word) for word in old.split())
    assert re.search(pattern, page_text), f"the page no longer holds {old!r}, so this edit would change nothing"
    return re.sub(pattern, lambda _match: new, page_text, count=1)


def test_a_tool_moved_between_the_lists_of_a_page_is_reported(legacy_context: MCPRuntimeContext) -> None:
    split, _calls = _derive_split(legacy_context)
    pages = _pages()
    assert _problems(split, pages) == []

    moved_to_running = {
        **pages,
        TOOLS_PAGE: _moved(
            _moved(
                pages[TOOLS_PAGE],
                "`alice_vnext_context_tree`, `alice_vnext_ingest_agent_output`",
                "`alice_vnext_ingest_agent_output`",
            ),
            "and four reads and captures (`alice_vnext_context_pack`,",
            "and four reads and captures (`alice_vnext_context_tree`, `alice_vnext_context_pack`,",
        ),
    }
    problems = _problems(split, moved_to_running)
    assert any("alice_vnext_context_tree" in problem and "running" in problem for problem in problems)
    assert any("alice_vnext_context_tree" in problem and "failing" in problem for problem in problems)

    edits = {
        "a count word of the guide": {**pages, GUIDE_PAGE: _moved(pages[GUIDE_PAGE], "Thirteen `alice_vnext_*`", "Twelve `alice_vnext_*`")},
        "a family count of the tools page": {
            **pages,
            TOOLS_PAGE: _moved(pages[TOOLS_PAGE], "the five `alice_vnext_scheduler_*`", "the four `alice_vnext_scheduler_*`"),
        },
        "the count of the known limits": {
            **pages,
            LIMITS_PAGE: _moved(pages[LIMITS_PAGE], "except the thirteen that run", "except the twelve that run"),
        },
        "a Postgres tool moved to the failing list": {
            **pages,
            TOOLS_PAGE: _moved(
                pages[TOOLS_PAGE], "`alice_vnext_recent_changes` and the five", "`alice_vnext_scheduler_pause` and the five"
            ),
        },
    }
    for what, edited_pages in edits.items():
        assert _problems(split, edited_pages), what


def test_a_store_method_that_appears_makes_a_page_wrong(
    legacy_context: MCPRuntimeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`alice_vnext_artifact_get` fails today because the store has no `get_artifact`. Give the store that one method,
    answering a row, and the call runs, so the pages that list it as failing are wrong."""

    before, _calls = _derive_split(legacy_context)
    assert "alice_vnext_artifact_get" in before.fails
    assert _problems(before, _pages()) == []

    def get_artifact(_self: object, artifact_id: str) -> dict[str, object]:
        return {"id": artifact_id, "domain": "project", "sensitivity": "internal"}

    monkeypatch.setattr(SQLiteVNextStore, "get_artifact", get_artifact, raising=False)
    after, _calls = _derive_split(legacy_context)

    assert "alice_vnext_artifact_get" in after.runs
    problems = _problems(after, _pages())
    assert any("alice_vnext_artifact_get" in problem and "failing" in problem for problem in problems)
    assert any("alice_vnext_artifact_get" in problem and "running" in problem for problem in problems)


def test_a_store_method_that_goes_makes_a_page_wrong(
    legacy_context: MCPRuntimeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make the method `alice_vnext_memory_audit` reads provenance with answer as a missing method does, and the call
    fails, so the pages that list it as running are wrong.

    The method is replaced, not deleted. Deleting a class attribute and putting it back moves it to the end of the
    class dictionary, and `test_store_events_revisions_split.py` pins that order.
    """

    before, _calls = _derive_split(legacy_context)
    assert "alice_vnext_memory_audit" in before.runs

    def gone(_self: object, *_args: object, **_kwargs: object) -> object:
        raise AttributeError("'SQLiteVNextStore' object has no attribute 'list_provenance_links'")

    monkeypatch.setattr(SQLiteVNextStore, "list_provenance_links", gone)
    after, _calls = _derive_split(legacy_context)

    assert "alice_vnext_memory_audit" in after.fails
    problems = _problems(after, _pages())
    assert any("alice_vnext_memory_audit" in problem for problem in problems)
