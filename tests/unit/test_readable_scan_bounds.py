"""A caller with limits reads past the rows it may not read with a proof in the store and a cap on rows judged.

Unreleased (on main, not in v0.20.0). ``alice_resume`` and the grounding probe now read again, past the rows a caller's limits
hide by the labels of their inputs, until they hold enough rows the caller may read. Without a bound that costs time in
proportion to the hidden rows newer than the first readable one, and a caller who chooses the query could make it so on
purpose. Two things bound it, and the tests here pin both:

* The store leaves out, before its limit, the rows that a direct input proves hidden (``readable_by``, built on
  ``hidden_memory_input_sql``): a source or a memory input in a restricted domain, when the caller's domains hold none, and a
  source above the caller's sensitivity ceiling. Such rows use no place and are not judged. The proof is only ever a
  proof of what is hidden, so it must never leave out a row the caller may read; a randomized test over many shapes and
  limits holds the stores to that against the real check.
* The read stops at ``READABLE_SCAN_CAP`` rows. Past it the caller is given the rows it may read among them, which can be
  none, and nothing else: ``alice_resume`` shows no decision and no next action and the events it found, the pack the open
  loops it found, the grounding probe reads the name as supported.

Each test names the mutation that must fail it, and the cases ``resume-*``, ``grounding-*`` and ``readable-*`` of
``scripts/derived_label_mutations.json`` replay them.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from uuid import uuid4

import pytest

from alicebot_api import vnext_label_guard
from alicebot_api.mcp import retrieval as mcp_retrieval
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from alicebot_api.vnext_label_guard import admit_loaded
from alicebot_api.vnext_label_sql import READABLE_SCAN_CAP, hidden_memory_input_sql
from tests.unit.test_recall_fallback_hidden_rows import KEYS, _memory, _scope, _source, _Vault

CAP = READABLE_SCAN_CAP


def _brief(vault: _Vault, key: str | None, **arguments: object) -> dict[str, object]:
    brief = vault.call(key, "alice_resume", **arguments)["brief"]
    assert isinstance(brief, dict)
    return {name: value for name, value in brief.items() if name != "generated_at"}  # type: ignore[union-attr]


def _text(item: object) -> object:
    return json.loads(item["canonical_text"]) if item else None  # type: ignore[index]


def _add_decisions(vault: _Vault, count: int, *, provable: bool, tag: str) -> None:
    """``count`` decisions newer than anything in the vault. Provable ones have a direct source in the health domain; the
    others list an input that does not exist, which no direct input proves hidden and the check reads as unverified."""

    with vault.store() as store:
        parent = _source(store, "parent-" + tag, domain="health", scope=vault.scope) if provable else None
        for index in range(count):
            source = str(parent["id"]) if parent is not None else str(uuid4())
            _memory(store, f"{tag}-{index}", f"alpha {tag} decision {index}", {"source_id": source, **_scope(vault.scope)})


# -- the cap ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("hidden", "found"), [(CAP - 10, True), (CAP + 10, False)])
def test_the_read_stops_at_the_cap_and_gives_what_it_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hidden: int, found: bool
) -> None:
    """Mutation: replace ``window = cap`` in ``_scan_readable`` with ``window = window * 8``, or replace
    ``READABLE_SCAN_CAP = 128`` in ``vnext_label_sql.py`` with a number above the largest count here.

    Hidden decisions that nothing proves hidden (they list an input that does not exist) come before the one readable
    decision. Fewer than the cap: the key is shown the readable decision, as in a vault without them. More than the cap: the
    read stopped at the cap, and the answer is the rows it found among them, which is none: ``last_decision`` is empty.
    The answer is the same as the one a vault without the readable decision gives, and it says only that at least the cap's
    worth of newer rows are hidden from the key.
    """

    vault = _Vault(tmp_path, monkeypatch)
    key = vault.key("read_only")
    _add_decisions(vault, hidden, provable=False, tag="hidden")
    brief = _brief(vault, key, query="alpha")
    assert (_text(brief["last_decision"]) == "alpha only visible note") is found, brief["last_decision"]


def test_no_read_judges_more_than_the_cap_or_reads_a_list_more_than_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: replace ``window = cap`` in ``_scan_readable`` with ``window = window * 2``.

    With three times the cap in hidden rows that nothing proves hidden, no call of the check gets more than the cap's worth of
    rows, and each list is read at most twice, so the cost does not grow with the vault. The calls are counted at the check and
    at the stores.
    """

    vault = _Vault(tmp_path, monkeypatch)
    key = vault.key("read_only")
    _add_decisions(vault, CAP * 3, provable=False, tag="hidden")
    judged: list[int] = []
    original = vnext_label_guard.admit_loaded

    def counting(store, *, kind, rows, **kwargs):  # type: ignore[no-untyped-def]
        judged.append(len(rows))
        return original(store, kind=kind, rows=rows, **kwargs)

    monkeypatch.setattr(vnext_label_guard, "admit_loaded", counting)
    reads: list[int] = []
    from alicebot_api.sqlite_store import SQLiteVNextStore

    original_list = SQLiteVNextStore.list_memories

    def counting_list(self, **kwargs):  # type: ignore[no-untyped-def]
        reads.append(int(kwargs.get("limit") or 0))
        return original_list(self, **kwargs)

    monkeypatch.setattr(SQLiteVNextStore, "list_memories", counting_list)
    _brief(vault, key, query="alpha")
    assert judged and max(judged) <= CAP, judged
    assert sorted(reads)[-4:] == [1, 1, CAP, CAP] or max(reads) == CAP, reads
    assert len(reads) <= 4, reads


# -- the proof in the store --------------------------------------------------------------------------------------------


def test_rows_a_direct_input_proves_hidden_use_no_place_of_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: drop ``held_back_domains["readable_by"] = readable_by`` in ``list_memory_rows`` (``_vnext_resume``), or
    drop ``**({"readable_by": readable_by} if limited else {})`` in ``read_memory_events``.

    Three times the cap in hidden decisions whose source is in the health domain come before the readable one. The store
    leaves them out before its limit, so the key is shown the readable decision, the next action and the events of a vault
    without them, however many there are. Without the proof the read stops at the cap and the decision is empty.
    """

    vault = _Vault(tmp_path, monkeypatch)
    key = vault.key("read_only")
    before = _brief(vault, key, query="alpha")
    assert _text(before["last_decision"]) == "alpha only visible note"
    _add_decisions(vault, CAP * 3, provable=True, tag="hidden")
    after = _brief(vault, key, query="alpha")
    assert _text(after["last_decision"]) == "alpha only visible note"
    assert after["recent_changes"] == before["recent_changes"]


def test_the_proof_by_domain_is_made_only_for_a_filter_without_a_restricted_domain() -> None:
    """Mutation: drop ``and not (set(domains) & RESTRICTED_DOMAINS)`` from ``hidden_memory_input_sql``.

    A derived row takes the most frequent restricted domain among its inputs. A filter that holds a restricted domain, so a
    source in the health domain proves nothing about a key that may read financial notes, gets no proof by domain.
    """

    without = hidden_memory_input_sql(["public"], sqlite=True)
    assert "'health'" not in without
    assert "'health'" in hidden_memory_input_sql(["public"], sqlite=True, domains=["project"])
    assert "'health'" in hidden_memory_input_sql(None, sqlite=True, domains=["project"])
    assert "'health'" not in hidden_memory_input_sql(["public"], sqlite=True, domains=["project", "financial"])
    assert hidden_memory_input_sql(None, sqlite=True, domains=["project", "health"]) == "TRUE"
    assert hidden_memory_input_sql(None, sqlite=True, domains=None) == "TRUE"
    for sqlite in (True, False):
        assert "label_input" not in hidden_memory_input_sql(["public"], sqlite=sqlite, domains=["project"], memory_inputs=False)
        assert "label_input" in hidden_memory_input_sql(["public"], sqlite=sqlite, domains=["project"])


# One input of each kind, in each domain and at each level, and rows that name them.
INPUT_DOMAINS = ("project", "health", "financial", "family", "unknown")
INPUT_LEVELS = ("public", "private", "confidential")
FILTERS = [
    (None, ["public", "internal", "unknown"]),
    (["project"], ["public", "internal", "unknown"]),
    (["project", "unknown"], ["public", "internal", "private", "unknown"]),
    (["project", "health"], ["public", "internal", "private", "unknown"]),
    (["project", "financial", "health", "family"], ["public", "internal", "private", "unknown"]),
    (["project"], list(ALL_SENSITIVITY)),
]


def _populate(store, rng: random.Random):  # type: ignore[no-untyped-def]
    sources = {
        (domain, level): _source(store, f"src-{domain}-{level}", domain=domain, sensitivity=level)
        for domain in INPUT_DOMAINS
        for level in INPUT_LEVELS
    }
    memories: list[dict[str, object]] = []
    for (domain, level), source in sources.items():
        memories.append(_memory(store, f"d-{domain}-{level}", f"alpha derived {domain} {level}", {"source_id": str(source["id"])}))
    # Rows that name a memory input: the input is derived itself, or stored in a domain, or redacted.
    inputs = list(memories)
    for index, parent in enumerate(inputs):
        memories.append(_memory(store, f"c-{index}", f"alpha cluster {index}", {"consolidation": {"cluster_member_ids": [str(parent["id"])]}}))
    for domain in INPUT_DOMAINS:
        stored = store.create_memory({"memory_key": f"stored-{domain}", "canonical_text": f"alpha stored {domain}", "title": f"stored-{domain}",
                                      "memory_type": "decision", "status": "active", "domain": domain, "sensitivity": "public", "metadata_json": {}})
        memories.append(dict(stored))
        memories.append(_memory(store, f"on-{domain}", f"alpha over stored {domain}", {"consolidation": {"cluster_member_ids": [str(stored["id"])]}}))
    redacted = _memory(store, "redacted", "alpha redacted", {"redacted": True})
    memories.append(_memory(store, "over-redacted", "alpha over redacted", {"consolidation": {"cluster_member_ids": [str(redacted["id"])]}}))
    memories.append(_memory(store, "missing", "alpha missing input", {"source_id": str(uuid4())}))
    memories.append(_memory(store, "original", "alpha original", {}))
    rng.shuffle(memories)
    return memories


@pytest.mark.parametrize("seed", range(4))
def test_the_proof_never_leaves_out_a_row_the_check_admits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int) -> None:
    """Mutation: replace ``input_in_restricted_domain = f"label_input.domain IN ({domain_names}) OR " if domain_blocked else ""``
    in ``hidden_memory_input_sql`` with a version that also lists ``'unknown'``, or drop the ``domains`` argument ``None``
    guard so that a filter holding a restricted domain gets the proof.

    Every row the real check (``admit_loaded`` with the same limits) admits is among the rows each reader returns when it is
    given ``readable_by``: the memory list, the full-text search and the resume event reader, over memories derived from
    sources and memory inputs in five domains and three levels, from redacted inputs and from inputs that do not exist, for
    six pairs of limits. The proof also leaves rows out, or it proves nothing: some limits drop rows.
    """

    vault = _Vault(tmp_path, monkeypatch)
    with vault.store() as store:
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
            via_fts_check = {str(row["id"]) for row in admit_loaded(
                store, kind="memory", rows=store.search_memories_fts(query="alpha", domains=domains, sensitivity_allowed=levels, limit=500),
                domains=domains or [], sensitivity_allowed=levels, projects=())}
            assert via_fts_check <= found, (domains, levels, sorted(via_fts_check - found))
            event_rows = store.list_resume_memory_events(statuses=("active",), limit=5000, domains=None, sensitivity_allowed=None)
            kept_events = store.list_resume_memory_events(statuses=("active",), limit=5000, domains=None, sensitivity_allowed=None, readable_by=(domains, levels))
            readable_targets = {str(row["id"]) for row in admitted_rows(store, domains, levels, memories)}
            assert {str(e["target_id"]) for e in event_rows if str(e["target_id"]) in readable_targets} <= {str(e["target_id"]) for e in kept_events}, (domains, levels)
        assert left_out_somewhere, "the proof left nothing out for any pair of limits, so it proved nothing"


def test_limits_that_admit_no_sensitivity_read_no_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: replace ``if readable_by is not None and readable_by[1] is not None and not readable_by[1]:`` in
    ``list_resume_memory_events`` of ``sqlite_store.py`` with ``if False:``.

    Limits that admit no level of sensitivity admit no event, and the store says so before it reads. The control reads the
    same events with no limits.
    """

    vault = _Vault(tmp_path, monkeypatch)
    with vault.store() as store:
        _populate(store, random.Random(0))
        assert store.list_resume_memory_events(statuses=("active",), limit=50, domains=None, sensitivity_allowed=None)
        assert store.list_resume_memory_events(statuses=("active",), limit=50, domains=None, sensitivity_allowed=None, readable_by=(None, [])) == []


def admitted_rows(store, domains, levels, memories):  # type: ignore[no-untyped-def]
    return admit_loaded(store, kind="memory", rows=memories, domains=domains or [], sensitivity_allowed=levels, projects=())


# -- the batched fence of the events ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(3))
def test_the_batched_event_fence_gives_the_verdicts_of_the_fence_by_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int) -> None:
    """Mutation: replace ``honoured = target_id in admitted`` in ``_resume_events_honouring_policy_fence`` with
    ``honoured = True``, or drop ``memory_window_is_open(row) and`` from its candidates.

    A caller with limits judges up to the cap's worth of events in one read. The events of every shape above and of open
    loops, of an expired memory and of a memory that is gone, get the same verdict from the batch as from the fence by id,
    event by event.
    """

    vault = _Vault(tmp_path, monkeypatch)
    with vault.store() as store:
        memories = _populate(store, random.Random(seed))
        _memory(store, "expired", "alpha expired", {})
        store.conn.execute("UPDATE memories SET valid_to = '2000-01-01T00:00:00Z' WHERE memory_key = 'expired'")
        loop = store.create_open_loop({"title": "alpha loop", "domain": "project", "sensitivity": "public", "status": "open", "metadata_json": {}})
        assert loop
        events = list(store.list_resume_memory_events(statuses=("active", "candidate"), limit=5000, domains=None, sensitivity_allowed=None))
        events += list(store.list_open_loop_events(statuses=("open",), limit=5000, domains=None, sensitivity_allowed=None))
        events.append({"id": "x", "target_type": "memory", "target_id": str(uuid4())})
        assert len(events) > len(memories)
        for domains, levels in FILTERS:
            held_back = frozenset()
            arguments = dict(effective_domains=tuple(domains or ()), effective_sensitivity_allowed=tuple(levels),
                             exclude_global_domains=held_back, effective_project_scope=())
            one_by_one = [e for e in events if mcp_retrieval._resume_event_honours_policy_fence(store, e, **arguments)]
            together = mcp_retrieval._resume_events_honouring_policy_fence(store, events, **arguments)
            assert [e["id"] for e in together] == [e["id"] for e in one_by_one], (domains, levels)
            assert one_by_one and len(one_by_one) < len(events)


def test_a_key_bound_to_a_project_reads_the_same_with_and_without_hidden_rows_the_proof_takes_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: replace ``limited = SourceReadFence...entity_read_fenced`` in ``_vnext_resume`` with ``limited = False``.

    The bound key's resume with 3 times the cap in provable hidden decisions equals its resume without them.
    """

    vault = _Vault(tmp_path, monkeypatch, scoped=True)
    key = vault.key("read_only_alpha")
    before = _brief(vault, key, query="alpha")
    _add_decisions(vault, CAP * 3, provable=True, tag="hidden")
    assert _brief(vault, key, query="alpha") == before
    assert KEYS["read_only_alpha"][1] == "alpha"


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("page", ["CHANGELOG.md", "docs/release/derived-labels-security-note-draft.md"])
def test_the_pages_state_the_cap_and_the_answer_at_it(page: str) -> None:
    """Mutation: change ``READABLE_SCAN_CAP`` in ``vnext_label_sql.py`` without the pages, or delete the sentence that gives the
    answer at the cap from either page.

    The changelog and the security note say how many rows a read judges, what a key is given when it reaches that many, and
    what that answer tells it. The number is the constant's.
    """

    text = " ".join((ROOT / page).read_text(encoding="utf-8").split())
    assert f"up to {CAP} rows" in text
    assert f"at least {CAP} newer rows are hidden from the key" in text
    assert "which can be none" in text
    assert "`alice_resume` then shows no `last_decision` and no `next_action`" in text
