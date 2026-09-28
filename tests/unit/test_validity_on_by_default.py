"""Current committed facts outrank historical ones on default reads.

Present-tense ``alice_recall`` and the session brief used to treat two
committed addresses as equal FTS / created_at hits. They now call the
same ``_prefer_current_versions`` helper the context pack already ran.
"""

from __future__ import annotations

import inspect
import io
import json
import sys
from pathlib import Path

from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, main as onramp_main, resolve_db_path, sqlite_url_for_path
from alicebot_api.session_briefing import compile_local_session_brief, compile_session_brief
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MODEL_ENV,
)

USER_ID = "00000000-0000-0000-0000-000000000001"
PRESENT_TENSE_QUERY = "where do I live?"
OLD_ADDRESS = "I live at 11 Old Street."
CURRENT_ADDRESS = "I live at 22 Current Street."
RESTRICTED_ADDRESS = "I live at 99 Secret Street."
CANDIDATE_NOTE = "Decision: I live at 77 Candidate Street.\n"
OLD_VALID_FROM = "2025-03-01T00:00:00Z"
CURRENT_VALID_FROM = "2026-03-01T00:00:00Z"

UNSCOPED_FENCES = {
    "effective_domains": (),
    "effective_sensitivity_allowed": ("public", "internal", "private", "unknown"),
    "effective_project_scope": (),
}


def _clear_env(monkeypatch) -> None:
    for env_name in (
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
        EMBEDDINGS_API_KEY_ENV,
        AGENT_API_KEY_ENV,
    ):
        monkeypatch.delenv(env_name, raising=False)


def _context(tmp_path: Path, monkeypatch) -> MCPRuntimeContext:
    _clear_env(monkeypatch)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _create_fact(
    store: SQLiteVNextStore,
    *,
    memory_key: str,
    text: str,
    valid_from: str,
    sensitivity: str = "public",
    domain: str = "project",
    project: str = "acme",
    supersedes: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "memory_key": memory_key,
        "memory_type": "semantic",
        "title": "Home address",
        "canonical_text": text,
        "status": "active",
        "domain": domain,
        "sensitivity": sensitivity,
        "valid_from": valid_from,
        "project_scope": [project],
        "metadata_json": {"project_scope": [project]},
        "value": {"text": text},
    }
    if supersedes is not None:
        payload["supersedes"] = supersedes
    return store.create_memory(payload)


def _seed_address_pair(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    """Current row first, historical row second, ancestor updated last.

    Brief lists by created_at DESC and FTS ties break on updated_at DESC.
    Writing the ancestor last makes that row the top hit unless
    ``_prefer_current_versions`` moves the replacement above it.
    """

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        current = _create_fact(
            store,
            memory_key="fact.address.current",
            text=CURRENT_ADDRESS,
            valid_from=CURRENT_VALID_FROM,
        )
        historical = _create_fact(
            store,
            memory_key="fact.address.historical",
            text=OLD_ADDRESS,
            valid_from=OLD_VALID_FROM,
        )
        store.update_memory(
            memory_id=str(current["id"]),
            patch={"supersedes": str(historical["id"])},
            actor_type="system",
        )
        store.update_memory(
            memory_id=str(historical["id"]),
            patch={"superseded_by": str(current["id"])},
            actor_type="system",
        )
        return current, historical


def _seed_restricted_address(tmp_path: Path) -> dict[str, object]:
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        return _create_fact(
            store,
            memory_key="fact.address.secret",
            text=RESTRICTED_ADDRESS,
            valid_from=CURRENT_VALID_FROM,
            sensitivity="private",
            domain="personal",
            project="other",
        )


def _recall(context: MCPRuntimeContext, **arguments) -> dict:
    from alicebot_api.mcp.registry import call_mcp_tool

    return call_mcp_tool(
        context,
        name="alice_recall",
        arguments={"query": PRESENT_TENSE_QUERY, **arguments},
    )


def _compile(tmp_path: Path, **kwargs) -> str:
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    fences = dict(UNSCOPED_FENCES)
    fences.update(kwargs)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        return compile_session_brief(store, **fences)


def _result_texts(payload: dict) -> list[str]:
    return [str(row.get("text") or "") for row in payload.get("results") or []]


def _fact_lines(brief: str) -> list[str]:
    return [line for line in brief.splitlines() if line.startswith("**fact**:")]


def test_present_tense_recall_leads_with_the_current_address(tmp_path: Path, monkeypatch) -> None:
    """Present-tense alice_recall leads with the current street and labels the old one.

    The historical row stays after the current row. It carries
    validity.superseded true, the same flag the context pack already sets.
    Fails if recall skips _prefer_current_versions, drops the old row, or
    returns that row without the superseded label.
    """

    context = _context(tmp_path, monkeypatch)
    current, _historical = _seed_address_pair(tmp_path)

    payload = _recall(context)
    texts = _result_texts(payload)

    assert texts, "recall returned no address facts"
    assert CURRENT_ADDRESS in texts[0]
    assert OLD_ADDRESS not in texts[0]
    assert any(OLD_ADDRESS in text for text in texts), (
        "historical address was dropped; demote-not-drop no longer holds"
    )
    historical = next(row for row in payload["results"] if OLD_ADDRESS in str(row.get("text") or ""))
    assert historical["validity"]["superseded"] is True
    assert historical["validity"]["superseded_by_memory_id"] == str(current["id"])
    assert payload["results"][0].get("validity", {}).get("superseded") is not True


def test_recall_leads_with_the_old_address_when_prefer_current_versions_is_skipped(
    tmp_path: Path, monkeypatch
) -> None:
    """Mutation: skip ``_prefer_current_versions`` on the recall path.

    The edit that makes
    ``test_present_tense_recall_leads_with_the_current_address`` fail is
    deleting the ``_prefer_current_versions`` call after
    ``_order_memories_for_strategy`` in ``mcp/retrieval.py``. This test
    applies that skip with a monkeypatch so the fixture stays honest:
    without the helper, fused/updated_at order still puts 11 Old Street
    first.
    """

    import alicebot_api.mcp.retrieval as retrieval_module

    def skip_prefer_current_versions(memories):
        return list(memories), 0

    monkeypatch.setattr(
        retrieval_module,
        "_prefer_current_versions",
        skip_prefer_current_versions,
    )

    context = _context(tmp_path, monkeypatch)
    _seed_address_pair(tmp_path)

    texts = _result_texts(_recall(context))

    assert texts, "recall returned no address facts"
    assert OLD_ADDRESS in texts[0]
    assert CURRENT_ADDRESS not in texts[0]


def test_session_brief_lists_the_current_address_before_the_historical_one(
    tmp_path: Path, monkeypatch
) -> None:
    """query=None brief must not print last year's street on a fact line.

    Both rows stay active. The older one has superseded_by set. The brief
    is context an agent reads as current, so that row is omitted. Fails if
    compile_session_brief still renders a memory whose superseded_by is set.
    """

    _context(tmp_path, monkeypatch)
    _seed_address_pair(tmp_path)

    brief = _compile(tmp_path, query=None)
    facts = _fact_lines(brief)

    assert any(CURRENT_ADDRESS in line for line in facts), brief
    assert not any(OLD_ADDRESS in line for line in facts), brief


def test_validity_ranking_does_not_bypass_the_policy_fence(tmp_path: Path, monkeypatch) -> None:
    """The new ranking path still applies the three effective fences by hand.

    Fails if recall or the brief ranks first and then reads without
    ``effective_domains``, ``effective_sensitivity_allowed``, or
    ``effective_project_scope``. The unscoped compile/recall below is the
    vacuous-test guard: if it stops seeing the secret street, the scoped
    asserts prove nothing.
    """

    context = _context(tmp_path, monkeypatch)
    _seed_address_pair(tmp_path)
    _seed_restricted_address(tmp_path)

    parameters = inspect.signature(compile_session_brief).parameters
    for name in (
        "effective_domains",
        "effective_sensitivity_allowed",
        "effective_project_scope",
    ):
        assert parameters[name].default is inspect.Parameter.empty
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY

    unscoped_recall = _result_texts(_recall(context))
    assert any(RESTRICTED_ADDRESS in text for text in unscoped_recall), (
        "unscoped recall lost the secret street; scoped asserts are vacuous"
    )
    assert CURRENT_ADDRESS in unscoped_recall[0]

    unscoped_brief = _compile(tmp_path, query=None)
    assert RESTRICTED_ADDRESS in unscoped_brief, (
        "unscoped brief lost the secret street; scoped asserts are vacuous"
    )

    project_locked = _result_texts(_recall(context, projects=["acme"]))
    assert not any(RESTRICTED_ADDRESS in text for text in project_locked)
    assert project_locked and CURRENT_ADDRESS in project_locked[0]

    public_only = _result_texts(_recall(context, sensitivity_allowed=["public"]))
    assert not any(RESTRICTED_ADDRESS in text for text in public_only)
    assert public_only and CURRENT_ADDRESS in public_only[0]

    domain_locked = _result_texts(_recall(context, domains=["project"]))
    assert not any(RESTRICTED_ADDRESS in text for text in domain_locked)
    assert domain_locked and CURRENT_ADDRESS in domain_locked[0]

    project_brief = _compile(tmp_path, query=None, effective_project_scope=("acme",))
    assert RESTRICTED_ADDRESS not in project_brief
    assert CURRENT_ADDRESS in project_brief

    public_brief = _compile(tmp_path, query=None, effective_sensitivity_allowed=("public",))
    assert RESTRICTED_ADDRESS not in public_brief
    assert CURRENT_ADDRESS in public_brief

    domain_brief = _compile(tmp_path, query=None, effective_domains=("project",))
    assert RESTRICTED_ADDRESS not in domain_brief
    assert CURRENT_ADDRESS in domain_brief


def test_a_capture_candidate_stays_unsearchable_as_a_memory(tmp_path: Path, monkeypatch) -> None:
    """Import stays a source. Commit stays a fact. No auto-promote.

    Fails if ranking, recall, or the brief promotes a capture candidate
    so it appears as a memory / **fact**.
    """

    from alicebot_api.mcp.registry import call_mcp_tool

    context = _context(tmp_path, monkeypatch)
    _seed_address_pair(tmp_path)
    captured = call_mcp_tool(
        context,
        name="alice_capture",
        arguments={
            "raw_text": CANDIDATE_NOTE,
            "title": "Candidate address",
            "domain": "personal",
            "sensitivity": "private",
        },
    )
    assert captured["status"] == "imported", captured

    recall = _recall(context)
    assert not any("77 Candidate Street" in text for text in _result_texts(recall))
    assert CURRENT_ADDRESS in _result_texts(recall)[0]

    brief = _compile(tmp_path, query=PRESENT_TENSE_QUERY)
    assert not any("77 Candidate Street" in line for line in _fact_lines(brief))

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        candidates = store.list_memories(status="candidate")
        committed = store.list_memories(status=None, statuses=("active", "accepted"))
    assert any(
        "77 Candidate Street" in str(row.get("canonical_text") or "") for row in candidates
    ), "capture created no address candidate; the no-promote assert is vacuous"
    assert not any("77 Candidate Street" in str(row.get("canonical_text") or "") for row in committed)


OLD_KETTLE = "The kettle is stored on the third shelf."
NEW_KETTLE = "The kettle is stored on the first shelf."
OLD_MUG = "The mug is stored on the third shelf."
NEW_MUG = "The mug is stored on the first shelf."
FRESH_SOURCE = "The fresh notebook stays on the desk."
STALE_SOURCE_LINE = "The stale notebook was on the shelf."


def _tool(context: MCPRuntimeContext, name: str, arguments: dict) -> dict:
    from alicebot_api.mcp.registry import call_mcp_tool

    return call_mcp_tool(context, name=name, arguments=arguments)


def _unquoted(value: object) -> str:
    text = str(value or "")
    if len(text) >= 2 and text.startswith('"'):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(loaded, str):
            return loaded
    return text


def _review_id(context: MCPRuntimeContext, needle: str) -> str:
    review = _tool(context, "alice_memory_review", {"status": "all", "limit": 20})
    for item in review["items"]:
        if needle in json.dumps(item, default=str):
            return str(item["id"])
    raise AssertionError(f"review item containing {needle!r} was not found")


def _source_rows(payload: dict) -> list[dict]:
    rows = payload.get("sources") or []
    return [row for row in rows if isinstance(row, dict)]


def _labelled_source(payload: dict, needle: str) -> dict:
    matches = [row for row in _source_rows(payload) if needle in _unquoted(row.get("excerpt"))]
    assert matches, payload
    return matches[0]


def _capture_sentence(context: MCPRuntimeContext, sentence: str) -> dict:
    captured = _tool(
        context,
        "alice_capture",
        {
            "raw_text": sentence,
            "title": "Shelf note",
            "domain": "project",
            "sensitivity": "public",
        },
    )
    assert captured["status"] == "imported", captured
    assert captured["candidate_memory_count"] == 1, captured
    assert captured["source_id"], captured
    return captured


def _stored_quote_and_chunks(tmp_path: Path, *, source_id: str, memory_id: str) -> tuple[list[str], list[str]]:
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        chunks = [str(chunk.get("text") or "") for chunk in store.list_source_chunks(source_id)]
        quotes = [
            str(link.get("quote") or "")
            for link in store.list_provenance_links(target_type="memory", target_id=memory_id)
            if str(link.get("evidence_role") or "") == "quoted_from"
        ]
    return chunks, quotes


def _brief_surfaces(tmp_path: Path, monkeypatch, capsys, *, query: str | None) -> dict[str, str]:
    """CLI brief, compile_local_session_brief, and both SessionStart formats."""

    local = compile_local_session_brief(
        resolve_db_path(data_dir=str(tmp_path), db=None),
        user_id=USER_ID,
        query=query,
    )
    argv = ["brief", "--data-dir", str(tmp_path), "--user-id", USER_ID]
    if query is not None:
        argv.extend(["--query", query])
    assert onramp_main(argv) == 0
    cli = capsys.readouterr().out
    from alicebot_api.session_start_hook import main as hook_main

    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert hook_main(["--data-dir", str(tmp_path), "--user-id", USER_ID, "--format", "json"]) == 0
    hook_json = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert hook_main(["--data-dir", str(tmp_path), "--user-id", USER_ID, "--format", "markdown"]) == 0
    hook_markdown = capsys.readouterr().out
    return {
        "local": local,
        "cli": cli,
        "session_json": str(hook_json["additional_context"]),
        "session_hook": str(hook_json["hookSpecificOutput"]["additionalContext"]),
        "session_markdown": hook_markdown,
    }


def _fact_lines_of(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("**fact**:")]


def _source_lines_of(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("**source**:")]


def test_context_pack_marks_the_historical_address_superseded(tmp_path: Path, monkeypatch) -> None:
    """The pack already labels the older active row. Recall must match it.

    Fails if the context pack drops validity.superseded from a row whose
    superseded_by is set.
    """

    context = _context(tmp_path, monkeypatch)
    current, _historical = _seed_address_pair(tmp_path)
    pack = _tool(context, "alice_context_pack", {"query": PRESENT_TENSE_QUERY})
    memories = [row for row in pack["memories"] if isinstance(row, dict)]
    texts = [_unquoted(row.get("canonical_text")) for row in memories]
    assert CURRENT_ADDRESS in texts[0]
    assert any(OLD_ADDRESS in text for text in texts)
    historical = next(row for row in memories if OLD_ADDRESS in _unquoted(row.get("canonical_text")))
    assert historical["validity"]["superseded"] is True
    assert historical["validity"]["superseded_by_memory_id"] == str(current["id"])


def test_session_start_and_cli_omit_the_historical_address(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """SessionStart and alice-memory brief show the current street only.

    Fails if either wrapper prints a **fact** line for the superseded_by row.
    """

    _context(tmp_path, monkeypatch)
    _seed_address_pair(tmp_path)
    surfaces = _brief_surfaces(tmp_path, monkeypatch, capsys, query=None)
    for name, text in surfaces.items():
        facts = _fact_lines_of(text)
        assert any(CURRENT_ADDRESS in line for line in facts), (name, text)
        assert not any(OLD_ADDRESS in line for line in facts), (name, text)


def test_brief_omits_a_source_line_marked_derived_memory_corrected(tmp_path: Path, monkeypatch) -> None:
    """A flagged excerpt is not a **source** line. An unflagged one stays.

    Fails if compile_session_brief stops dropping derived_memory_corrected.
    """

    from alicebot_api.vnext_retrieval import VNextRetrievalService

    _context(tmp_path, monkeypatch)

    def fake_search(self, **_kwargs):
        return (
            [
                {
                    "id": "source-stale",
                    "excerpt": STALE_SOURCE_LINE,
                    "excerpt_kind": "imported_source_material",
                    "derived_memory_corrected": True,
                    "current_memory_id": "memory-current",
                },
                {
                    "id": "source-fresh",
                    "excerpt": FRESH_SOURCE,
                    "excerpt_kind": "imported_source_material",
                },
            ],
            {"source": "test"},
        )

    monkeypatch.setattr(VNextRetrievalService, "search_source_excerpts", fake_search)
    brief = _compile(tmp_path, query="notebook shelf")
    assert not any(STALE_SOURCE_LINE in line for line in _source_lines_of(brief)), brief
    assert any(FRESH_SOURCE in line for line in _source_lines_of(brief)), brief


def test_uncorrected_capture_excerpt_is_not_marked_corrected(tmp_path: Path, monkeypatch) -> None:
    """A quote that still matches the memory is not a corrected excerpt.

    Fails if every captured source gets derived_memory_corrected, or if the
    brief drops a source whose memory was not corrected.
    """

    context = _context(tmp_path, monkeypatch)
    captured = _capture_sentence(context, OLD_KETTLE)
    memory_id = _review_id(context, OLD_KETTLE)
    chunks, quotes = _stored_quote_and_chunks(
        tmp_path, source_id=str(captured["source_id"]), memory_id=memory_id
    )
    assert any(OLD_KETTLE in chunk for chunk in chunks)
    assert OLD_KETTLE in quotes

    recall = _tool(context, "alice_recall", {"query": OLD_KETTLE})
    source = _labelled_source(recall, OLD_KETTLE)
    assert "derived_memory_corrected" not in source
    assert "current_memory_id" not in source
    brief = _compile(tmp_path, query=OLD_KETTLE)
    assert any(OLD_KETTLE in line for line in _source_lines_of(brief)), brief


def test_corrected_capture_labels_the_old_excerpt_and_drops_it_from_the_brief(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Edit-and-approve keeps the captured sentence on source excerpts only.

    Recall and the context pack return the new sentence as the fact and the
    old sentence as an excerpt with derived_memory_corrected and the current
    memory id. The brief, SessionStart, and alice-memory brief omit that
    source line. The stored chunk and quoted_from quote stay the old sentence.
    Fails if the label is missing, the excerpt is dropped, or the brief
    still prints the old sentence.
    """

    context = _context(tmp_path, monkeypatch)
    captured = _capture_sentence(context, OLD_KETTLE)
    memory_id = _review_id(context, OLD_KETTLE)
    edited = _tool(
        context,
        "alice_memory_correct",
        {
            "action": "edit-and-approve",
            "review_item_id": memory_id,
            "body": {"text": NEW_KETTLE},
            "reason": "The shelf changed.",
        },
    )
    assert edited["memory"]["canonical_text"] == NEW_KETTLE
    assert str(edited["memory"]["id"]) == memory_id

    chunks, quotes = _stored_quote_and_chunks(
        tmp_path, source_id=str(captured["source_id"]), memory_id=memory_id
    )
    assert any(OLD_KETTLE in chunk for chunk in chunks)
    assert NEW_KETTLE not in "\n".join(chunks)
    assert quotes == [OLD_KETTLE] or OLD_KETTLE in quotes

    recall = _tool(context, "alice_recall", {"query": OLD_KETTLE})
    assert any(NEW_KETTLE in text for text in _result_texts(recall))
    assert not any(OLD_KETTLE in text for text in _result_texts(recall))
    source = _labelled_source(recall, OLD_KETTLE)
    assert source["derived_memory_corrected"] is True
    assert source["current_memory_id"] == memory_id

    pack = _tool(context, "alice_context_pack", {"query": OLD_KETTLE})
    pack_source = _labelled_source(pack, OLD_KETTLE)
    assert pack_source["derived_memory_corrected"] is True
    assert pack_source["current_memory_id"] == memory_id
    pack_texts = [_unquoted(row.get("canonical_text")) for row in pack["memories"]]
    assert any(NEW_KETTLE in text for text in pack_texts)
    assert not any(text == OLD_KETTLE for text in pack_texts)

    for text in _brief_surfaces(tmp_path, monkeypatch, capsys, query=OLD_KETTLE).values():
        assert any(NEW_KETTLE in line for line in _fact_lines_of(text)), text
        assert not any(OLD_KETTLE in line for line in _fact_lines_of(text)), text
        assert not any(OLD_KETTLE in line for line in _source_lines_of(text)), text
        assert OLD_KETTLE not in text


def test_supersede_existing_keeps_the_old_sentence_on_labelled_source_excerpts(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Supersede-existing leaves the captured sentence on recall and the pack.

    The excerpt is labelled and points at the replacement. The brief does
    not print the old sentence. Fails if source excerpts are dropped, the
    label is missing, or the brief still shows the old sentence.
    """

    context = _context(tmp_path, monkeypatch)
    captured = _capture_sentence(context, OLD_MUG)
    memory_id = _review_id(context, OLD_MUG)
    superseded = _tool(
        context,
        "alice_memory_correct",
        {
            "action": "supersede-existing",
            "review_item_id": memory_id,
            "replacement_title": "Mug shelf",
            "replacement_body": {"text": NEW_MUG},
            "reason": "The shelf changed.",
        },
    )
    replacement_id = str(superseded["replacement_object"]["id"])
    assert superseded["memory"]["status"] == "superseded"
    assert str(superseded["memory"]["superseded_by"]) == replacement_id

    chunks, quotes = _stored_quote_and_chunks(
        tmp_path, source_id=str(captured["source_id"]), memory_id=memory_id
    )
    assert any(OLD_MUG in chunk for chunk in chunks)
    assert OLD_MUG in quotes

    recall = _tool(context, "alice_recall", {"query": OLD_MUG})
    source = _labelled_source(recall, OLD_MUG)
    assert source["derived_memory_corrected"] is True
    assert source["current_memory_id"] == replacement_id
    assert not any(OLD_MUG in text for text in _result_texts(recall))

    pack = _tool(context, "alice_context_pack", {"query": OLD_MUG})
    pack_source = _labelled_source(pack, OLD_MUG)
    assert pack_source["derived_memory_corrected"] is True
    assert pack_source["current_memory_id"] == replacement_id

    for text in _brief_surfaces(tmp_path, monkeypatch, capsys, query=OLD_MUG).values():
        assert any(NEW_MUG in line for line in _fact_lines_of(text)), text
        assert OLD_MUG not in text
