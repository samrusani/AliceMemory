"""Recall's OR fallback is decided on the rows the caller may read, on PostgreSQL.

Unreleased (on main, not in v0.20.0). The same checks as ``tests/unit/test_recall_fallback_hidden_rows.py`` on a real
PostgreSQL store, through the MCP tool registry and a minted key. The strict pass (``websearch_to_tsquery``) and the OR
pass (``to_tsquery`` over the sanitized words) are the store's own, and the decision between them is the shared stage in
``VNextRetrievalService._memory_fts_rows``. A visible public memory matches ``alpha`` only. After the answer of a
restricted key is recorded, a hidden row that matches ``alpha beta`` is added, and the answer and the whole debug trace of
the key must not change: the visible memory is returned in both and the stage label is ``postgres_fts_or_fallback`` in
both.

Each test names the mutation that must fail it, and the cases ``recall-fallback-*`` of
``scripts/derived_label_mutations.json`` replay them.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.unit.test_recall_fallback_hidden_rows import (
    HIDDEN_FROM,
    KEYS,
    QUERY,
    SHAPES,
    VISIBLE_TEXT,
    _memory,
    _pack,
    _scope,
    _source,
    _texts,
)

OR_PASS = "postgres_fts_or_fallback"
STRICT_PASS = "postgres_fts"

# One key for each shape is enough here: the decision does not depend on which label hid the row, and the unit tests
# run every pair. These cover each reason a row is hidden and the three kinds of key.
CELLS = [("domain", "read_only"), ("chain", "trusted"), ("project", "project_alpha"), ("missing_input", "read_only_alpha")]


class _Postgres:
    def __init__(self, harness, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        profile, project = KEYS[name]
        self.h = harness
        self.monkeypatch = monkeypatch
        self.scope: tuple[str, ...] = ("alpha",) if project else ()
        monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
        monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
        monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
        with harness.store() as store:
            _memory(store, "visible", VISIBLE_TEXT, _scope(self.scope))
        self.key = harness.key(profile, project=project)
        self.context = MCPRuntimeContext(database_url=harness.urls["app"], user_id=UUID(str(harness.user_id)))

    def hide(self, shape: str, text: str | None = None) -> dict[str, object]:
        with self.h.store() as store:
            return SHAPES[shape](store, self.scope, *([] if text is None else [text]))

    def call(self, key: str | None, tool: str, **arguments: object) -> dict[str, object]:
        if key:
            self.monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        else:
            self.monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        return call_mcp_tool(self.context, name=tool, arguments=arguments)

    def recall(self, key: str | None, **extra: object) -> dict[str, object]:
        return self.call(key, "alice_recall", query=QUERY, debug=True, **extra)


@pytest.mark.parametrize(("shape", "name"), CELLS)
def test_a_hidden_strict_hit_does_not_change_the_answer_or_the_trace_of_a_restricted_key(
    label_harness, monkeypatch: pytest.MonkeyPatch, shape: str, name: str  # noqa: F811
) -> None:
    """Mutation: replace the condition that runs the OR pass after a strict pass that left no readable row
    (``if relaxable and not selected and "match_any" not in options and read_fence.entity_read_fenced:`` in
    ``VNextRetrievalService._memory_fts_rows``) with ``if False:``.
    """

    assert name in HIDDEN_FROM[shape]
    vault = _Postgres(label_harness, monkeypatch, name)
    before = vault.recall(vault.key)
    assert _texts(before) == [VISIBLE_TEXT], before
    assert before["retrieval"]["stages"]["fts"]["source"] == OR_PASS  # type: ignore[index]

    hidden = vault.hide(shape)
    control = vault.recall(None, sensitivity_allowed=list(ALL_SENSITIVITY))
    assert str(hidden["id"]) in {item["id"] for item in control["results"]}  # type: ignore[index, union-attr]
    assert control["retrieval"]["stages"]["fts"]["source"] == STRICT_PASS  # type: ignore[index]

    after = vault.recall(vault.key)
    assert after == before
    assert after["retrieval"]["stages"]["fts"] == {"source": OR_PASS, "candidate_count": 1}  # type: ignore[index]
    assert str(hidden["id"]) not in json.dumps(after, default=str)


@pytest.mark.parametrize(("shape", "name"), CELLS[:2])
def test_a_hidden_strict_hit_does_not_change_the_context_pack_of_a_restricted_key(
    label_harness, monkeypatch: pytest.MonkeyPatch, shape: str, name: str  # noqa: F811
) -> None:
    """Mutation: pass ``read_fence=SourceReadFence.unfenced()`` in the ``_memory_fts_rows`` call of ``compile_context_pack``."""

    vault = _Postgres(label_harness, monkeypatch, name)
    before = _pack(vault.call(vault.key, "alice_context_pack", query=QUERY, debug=True), or_pass=OR_PASS)
    hidden = vault.hide(shape)
    after = _pack(vault.call(vault.key, "alice_context_pack", query=QUERY, debug=True), or_pass=OR_PASS)
    assert after == before
    assert str(hidden["id"]) not in json.dumps(after, default=str)


def test_the_owner_keeps_its_decision(label_harness, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Mutation: drop ``and read_fence.entity_read_fenced`` from the condition that runs the OR pass.

    The owner's default filters drop a row below a confidential source; the strict pass still stopped the OR pass
    for the owner, and does.
    """

    vault = _Postgres(label_harness, monkeypatch, "read_only")
    hidden = vault.hide("chain")
    result = vault.recall(None)
    assert result["results"] == []
    assert result["retrieval"]["stages"]["fts"] == {"source": STRICT_PASS, "candidate_count": 0}  # type: ignore[index]
    everything = vault.recall(None, sensitivity_allowed=list(ALL_SENSITIVITY))
    assert [item["id"] for item in everything["results"]] == [str(hidden["id"])]  # type: ignore[index, union-attr]


def test_a_hidden_source_that_matches_every_word_does_not_change_the_source_stage(label_harness, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Mutation: none for this file. The chunk stage's own filters take a source in a restricted domain out before its
    strict-then-OR rule counts it (a source has no effective label), and this keeps it so."""

    vault = _Postgres(label_harness, monkeypatch, "read_only")

    def add_source(key: str, text: str, **labels: str) -> dict[str, object]:
        with label_harness.store() as store:
            source = _source(store, key, **labels)
            store.create_source_chunk({"source_id": source["id"], "chunk_index": 0, "text": text, "metadata_json": {}})
            return source

    add_source("visible-source", "alpha only visible chunk text")
    recall_before = vault.recall(vault.key)
    pack_before = _pack(vault.call(vault.key, "alice_context_pack", query=QUERY, debug=True), or_pass=OR_PASS)
    assert recall_before["source_count"] == 1
    assert pack_before["trace"]["stages"]["sources"]["chunk_fts_source"] == OR_PASS  # type: ignore[index]

    hidden = add_source("hidden-source", "alpha beta hidden chunk text", domain="health")
    recall_after = vault.recall(vault.key)
    pack_after = _pack(vault.call(vault.key, "alice_context_pack", query=QUERY, debug=True), or_pass=OR_PASS)

    assert recall_after == recall_before
    assert pack_after == pack_before
    assert str(hidden["id"]) not in json.dumps([recall_after, pack_after], default=str)
