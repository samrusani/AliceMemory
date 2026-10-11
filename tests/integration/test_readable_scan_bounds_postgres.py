"""A caller with limits reads past the rows it may not read with a proof in the store and a cap, on PostgreSQL.

Unreleased (on main, not in v0.20.0). The same checks as ``tests/unit/test_readable_scan_bounds.py`` and the loop and
ceiling cases of ``tests/unit/test_hidden_rows_resume_and_grounding.py`` on a real PostgreSQL store, through the MCP tool
registry and a minted key. The proof reads ``sources`` and ``memories`` with hashed sub-selects, and the event reader joins
``event_log`` to ``memories`` on the text of the id, so these are the queries whose plans differ from the SQLite ones.

Each test names the mutation that must fail it, and the cases ``resume-*``, ``grounding-*`` and ``readable-*`` of
``scripts/derived_label_mutations.json`` replay them.
"""

from __future__ import annotations

import json
import random
from uuid import uuid4

import pytest

from alicebot_api.mcp import retrieval as mcp_retrieval
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from alicebot_api.vnext_label_guard import admit_loaded
from alicebot_api.vnext_label_sql import READABLE_SCAN_CAP
from alicebot_api.vnext_label_writes import without_insert_floor
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.test_recall_fallback_hidden_rows_postgres import _Postgres
from tests.unit.test_hidden_rows_resume_and_grounding import (
    BOTH_UNSUPPORTED,
    NAME_TEXT,
    NAMES_QUERY,
    ONE_UNSUPPORTED,
)
from tests.unit.test_readable_scan_bounds import FILTERS, _populate
from tests.unit.test_recall_fallback_hidden_rows import _memory, _source

CAP = READABLE_SCAN_CAP


def _brief(vault: _Postgres, key: str | None, **arguments: object) -> dict[str, object]:
    brief = vault.call(key, "alice_resume", **arguments)["brief"]
    return {name: value for name, value in brief.items() if name != "generated_at"}  # type: ignore[union-attr]


def _decisions(harness, count: int, *, provable: bool, tag: str) -> None:  # type: ignore[no-untyped-def]
    with harness.store() as store:
        parent = _source(store, "parent-" + tag, domain="health") if provable else None
        for index in range(count):
            source = str(parent["id"]) if parent is not None else str(uuid4())
            _memory(store, f"{tag}-{index}", f"alpha {tag} decision {index}", {"source_id": source})


@pytest.mark.parametrize(("hidden", "found"), [(CAP - 10, True), (CAP + 10, False)])
def test_the_read_stops_at_the_cap_and_gives_what_it_found(label_harness, monkeypatch: pytest.MonkeyPatch, hidden: int, found: bool) -> None:  # noqa: F811
    """Mutation: replace ``window = cap`` in ``_scan_readable`` with ``window = window * 8``."""

    vault = _Postgres(label_harness, monkeypatch, "read_only")
    _decisions(label_harness, hidden, provable=False, tag="hidden")
    brief = _brief(vault, vault.key, query="alpha")
    text = json.loads(brief["last_decision"]["canonical_text"]) if brief["last_decision"] else None  # type: ignore[index]
    assert (text == "alpha only visible note") is found, brief["last_decision"]


def test_rows_a_direct_input_proves_hidden_use_no_place_of_the_cap(label_harness, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Mutation: drop ``held_back_domains["readable_by"] = readable_by`` in ``list_memory_rows`` (``_vnext_resume``), or
    drop ``**({"readable_by": readable_by} if limited else {})`` in ``read_memory_events``."""

    vault = _Postgres(label_harness, monkeypatch, "read_only")
    before = _brief(vault, vault.key, query="alpha")
    assert before["last_decision"]
    _decisions(label_harness, CAP * 3, provable=True, tag="hidden")
    after = _brief(vault, vault.key, query="alpha")
    assert after["last_decision"] == before["last_decision"]
    assert after["recent_changes"] == before["recent_changes"]


@pytest.mark.parametrize("seed", range(2))
def test_the_proof_never_leaves_out_a_row_the_check_admits(label_harness, seed: int) -> None:  # noqa: F811
    """Mutation: the same edits as the SQLite test of this name, in the PostgreSQL text of ``hidden_memory_input_sql``."""

    with label_harness.store() as store:
        memories = _populate(store, random.Random(seed))
        left_out_somewhere = False
        for domains, levels in FILTERS:
            stored_fence = store.list_memories(statuses=("active",), domains=domains, sensitivity_allowed=levels, include_expired=False)
            admitted = {str(row["id"]) for row in admit_loaded(store, kind="memory", rows=stored_fence, domains=domains or [], sensitivity_allowed=levels, projects=())}
            listed = {str(row["id"]) for row in store.list_memories(statuses=("active",), domains=domains, sensitivity_allowed=levels,
                                                                     include_expired=False, readable_by=(domains, levels))}
            assert admitted <= listed, (domains, levels, sorted(admitted - listed))
            left_out_somewhere |= listed != {str(row["id"]) for row in stored_fence}
            found = {str(row["id"]) for row in store.search_memories_fts(query="alpha", domains=domains, sensitivity_allowed=levels, limit=500, readable_by=(domains, levels))}
            by_check = {str(row["id"]) for row in admit_loaded(
                store, kind="memory", rows=store.search_memories_fts(query="alpha", domains=domains, sensitivity_allowed=levels, limit=500),
                domains=domains or [], sensitivity_allowed=levels, projects=())}
            assert by_check <= found, (domains, levels, sorted(by_check - found))
            readable_targets = {str(row["id"]) for row in admit_loaded(store, kind="memory", rows=memories, domains=domains or [], sensitivity_allowed=levels, projects=())}
            all_events = store.list_resume_memory_events(statuses=("active",), limit=5000, domains=None, sensitivity_allowed=None)
            kept = store.list_resume_memory_events(statuses=("active",), limit=5000, domains=None, sensitivity_allowed=None, readable_by=(domains, levels))
            assert {str(e["target_id"]) for e in all_events if str(e["target_id"]) in readable_targets} <= {str(e["target_id"]) for e in kept}, (domains, levels)
        assert left_out_somewhere, "the proof left nothing out for any pair of limits, so it proved nothing"


def test_limits_that_admit_no_sensitivity_read_no_events(label_harness) -> None:  # noqa: F811
    """Mutation: replace ``return "\\n                  AND FALSE", ()`` in ``postgres_readable_events_sql`` with ``return "", ()``.

    Limits that admit no level of sensitivity admit no event. The control reads the same events with no limits.
    """

    with label_harness.store() as store:
        _populate(store, random.Random(0))
        assert store.list_resume_memory_events(statuses=("active",), limit=50, domains=None, sensitivity_allowed=None)
        assert store.list_resume_memory_events(statuses=("active",), limit=50, domains=None, sensitivity_allowed=None, readable_by=(None, [])) == []


def test_the_batched_event_fence_gives_the_verdicts_of_the_fence_by_id(label_harness) -> None:  # noqa: F811
    """Mutation: replace ``honoured = target_id in admitted`` in ``_resume_events_honouring_policy_fence`` with ``honoured = True``."""

    with label_harness.store() as store:
        _populate(store, random.Random(1))
        events = list(store.list_resume_memory_events(statuses=("active", "candidate"), limit=5000, domains=None, sensitivity_allowed=None))
        events.append({"id": "x", "target_type": "memory", "target_id": str(uuid4())})
        for domains, levels in FILTERS:
            arguments = dict(effective_domains=tuple(domains or ()), effective_sensitivity_allowed=tuple(levels),
                             exclude_global_domains=frozenset(), effective_project_scope=())
            one_by_one = [e for e in events if mcp_retrieval._resume_event_honours_policy_fence(store, e, **arguments)]
            together = mcp_retrieval._resume_events_honouring_policy_fence(store, events, **arguments)
            assert [e["id"] for e in together] == [e["id"] for e in one_by_one], (domains, levels)
            assert one_by_one and len(one_by_one) < len(events)


def test_a_newer_unfloored_open_loop_does_not_change_the_open_loops_of_a_restricted_key(label_harness, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Mutation: replace ``elif limited:`` before ``loop_rows = readable_prefix(`` in ``_vnext_resume`` with ``elif False:``, or
    ``if source_fence.entity_read_fenced:`` before ``open_loop_rows = readable_prefix(`` in ``compile_context_pack`` with ``if False:``."""

    vault = _Postgres(label_harness, monkeypatch, "read_only")
    with label_harness.store() as store:
        for index in range(5):
            store.create_open_loop({"title": f"visible loop {index}", "domain": "project", "sensitivity": "public", "status": "open", "metadata_json": {}})
    before = _brief(vault, vault.key)
    before_pack = vault.call(vault.key, "alice_context_pack", query="loop", debug=True)
    assert len(before["open_loops"]) == 5 and len(before_pack["open_loops"]) == 5  # type: ignore[arg-type]
    with label_harness.store() as store:
        parent = _source(store, "loop-parent", domain="health")
        with without_insert_floor():
            for index in range(6):
                store.create_open_loop({"title": f"hidden loop {index}", "domain": "project", "sensitivity": "public", "status": "open",
                                        "source_id": str(parent["id"]),
                                        "metadata_json": {"discovered_by": "weekly", "source_id": str(parent["id"])}})
    everything = _brief(vault, None, sensitivity_allowed=list(ALL_SENSITIVITY))
    assert any("hidden loop" in item["title"] for item in everything["open_loops"])  # type: ignore[attr-defined, union-attr]
    assert _brief(vault, vault.key) == before
    after_pack = vault.call(vault.key, "alice_context_pack", query="loop", debug=True)
    assert [item["title"] for item in after_pack["open_loops"]] == [item["title"] for item in before_pack["open_loops"]]


@pytest.mark.parametrize(("parent_domain", "parent_sensitivity", "supported"), [("project", "private", True), ("health", "public", True), ("project", "confidential", False)])
def test_the_grounding_check_judges_a_derived_mention_by_the_ceiling_of_the_trusted_key(
    label_harness, monkeypatch: pytest.MonkeyPatch, parent_domain: str, parent_sensitivity: str, supported: bool  # noqa: F811
) -> None:
    """Mutation: pass ``sensitivity_allowed=["public"]`` (the private case fails) or ``domains=["project"]`` (the health case
    fails) to ``admit_loaded`` in the grounding call of ``compile_context_pack``."""

    vault = _Postgres(label_harness, monkeypatch, "trusted")
    with label_harness.store() as store:
        parent = _source(store, "mention-parent", domain=parent_domain, sensitivity=parent_sensitivity)
        _memory(store, "mention", NAME_TEXT, {"source_id": str(parent["id"])})
    pack = vault.call(vault.key, "alice_context_pack", query=NAMES_QUERY, debug=True)
    assert pack["grounding"] == (ONE_UNSUPPORTED if supported else BOTH_UNSUPPORTED)

