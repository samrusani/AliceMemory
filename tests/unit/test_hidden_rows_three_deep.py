"""A row three memories below a source is judged by its effective domain and level, in the reads that look past hidden rows.

New in v0.20.2. The decision and the open loops of ``alice_resume``, the open loops of the context pack
and the grounding check of the pack read past the rows a caller's limits hide, and judge each row they keep with the
caller's own domains, levels and project scope. The store cannot take such a row out first: its stored labels are inside the
limits, and a chain of three memories is deeper than any direct input the store looks at. So the final check is the only
thing between the key and the row, and an edit to the domains, the levels or the project scope it is given shows only when
a row is hidden by exactly that one.

A hidden row sits three memories below a source (the source, a memory made from it, a memory made from that, and the row),
in a domain the key may not read (``health``) or at a level above its ceiling (``private``, where a ``read_only_agent`` key
stops at ``internal``). A control with every sensitivity reads the row, so it exists. The brief and the pack of the key must
be the same before and after the row is written. The scenario functions take the vault's calls, so the PostgreSQL twin in
``tests/integration/test_hidden_rows_three_deep_postgres.py`` runs them on a real store.

Each test names the mutation that must fail it, and the cases ``three-deep-*`` of ``scripts/derived_label_mutations.json``
replay them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest

from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from alicebot_api.vnext_label_writes import without_insert_floor
from tests.unit.test_hidden_rows_resume_and_grounding import BOTH_UNSUPPORTED, NAME_TEXT, NAMES_QUERY, ONE_UNSUPPORTED
from tests.unit.test_recall_fallback_hidden_rows import _memory, _scope, _source, _Vault

CHAINS = [("health-chain", "health", "public"), ("private-chain", "project", "private")]

Call = Callable[..., dict[str, Any]]
Stores = Callable[[], AbstractContextManager[Any]]


def _chain(store: Any, parent: dict[str, object], text: str, *, memory_type: str = "decision") -> dict[str, object]:
    """The row three memories below ``parent``; ``text`` is the text of the last one."""

    first = _memory(store, "chain-1", f"{text} first", {"source_id": str(parent["id"])}, memory_type=memory_type)
    second = _memory(store, "chain-2", f"{text} second", {"consolidation": {"cluster_member_ids": [str(first["id"])]}}, memory_type=memory_type)
    return _memory(store, "chain-3", text, {"consolidation": {"cluster_member_ids": [str(second["id"])]}}, memory_type=memory_type)


def _title(item: dict[str, Any]) -> object:
    """The title of a loop as it was written: the tools show it JSON-encoded."""

    return json.loads(item["title"])


def _resume(call: Call, key: str | None, **arguments: object) -> dict[str, Any]:
    brief = call(key, "alice_resume", **arguments)["brief"]
    last = brief.get("last_decision")
    return {"decision": json.loads(last["canonical_text"]) if last else None, "loops": [_title(item) for item in brief["open_loops"]]}


def decision_is_not_changed(call: Call, stores: Stores, key: str, domain: str, level: str) -> None:
    before = _resume(call, key, query="alpha")
    assert before["decision"], before
    with stores() as store:
        row = _chain(store, _source(store, "parent", domain=domain, sensitivity=level), "alpha hidden newest decision")
    owner = call(None, "alice_resume", query="alpha", sensitivity_allowed=list(ALL_SENSITIVITY))["brief"]["last_decision"]
    assert owner["id"] == str(row["id"]), "the row must be the newest decision for a caller who may read it"
    assert _resume(call, key, query="alpha") == before


def loops_are_not_changed(call: Call, stores: Stores, key: str, domain: str, level: str) -> None:
    with stores() as store:
        store.create_open_loop({"title": "visible loop", "domain": "project", "sensitivity": "public", "status": "open", "metadata_json": {}})
    before = _resume(call, key)
    pack_before = call(key, "alice_context_pack", query="loop", debug=True)
    assert before["loops"] == ["visible loop"] and [_title(item) for item in pack_before["open_loops"]] == ["visible loop"]
    with stores() as store:
        top = _chain(store, _source(store, "parent", domain=domain, sensitivity=level), "loop chain", memory_type="project_fact")
        with without_insert_floor():
            store.create_open_loop({
                "title": "hidden newest loop", "domain": "project", "sensitivity": "public", "status": "open",
                "metadata_json": {"derived_from": {"memories": [str(top["id"])], "counts": {"memories": 1}}},
            })
    owner = call(None, "alice_resume", sensitivity_allowed=list(ALL_SENSITIVITY))["brief"]["open_loops"]
    assert "hidden newest loop" in [_title(item) for item in owner], "the loop must be there for a caller who may read it"
    assert _resume(call, key) == before
    pack = call(key, "alice_context_pack", query="loop", debug=True)
    assert [_title(item) for item in pack["open_loops"]] == [_title(item) for item in pack_before["open_loops"]]


def mention_is_judged(call: Call, stores: Stores, key: str, domain: str, level: str, readable: bool) -> None:
    def grounding() -> object:
        return call(key, "alice_context_pack", query=NAMES_QUERY, debug=True).get("grounding")

    before = grounding()
    assert before == BOTH_UNSUPPORTED
    with stores() as store:
        _chain(store, _source(store, "parent", domain=domain, sensitivity=level), NAME_TEXT)
    assert grounding() == (ONE_UNSUPPORTED if readable else BOTH_UNSUPPORTED)


def bound_key_is_not_changed(call: Call, stores: Stores, key: str) -> None:
    with stores() as store:
        store.create_open_loop({"title": "visible loop", "domain": "project", "sensitivity": "public", "status": "open",
                                "metadata_json": {"project_scope": ["alpha"]}})
    decision, loops = _resume(call, key, query="alpha")["decision"], _resume(call, key)["loops"]
    assert decision and loops == ["visible loop"]
    with stores() as store:
        parent = _source(store, "parent-beta", scope=("beta",))
        _memory(store, "hidden", "alpha hidden newest decision", {"source_id": str(parent["id"]), **_scope(("alpha",))})
        with without_insert_floor():
            store.create_open_loop({
                "title": "hidden newest loop", "domain": "project", "sensitivity": "public", "status": "open",
                "source_id": str(parent["id"]),
                "metadata_json": {"discovered_by": "weekly", "source_id": str(parent["id"]), "project_scope": ["alpha"]},
            })
    assert _resume(call, key, query="alpha")["decision"] == decision
    assert _resume(call, key)["loops"] == loops
    pack = call(key, "alice_context_pack", query="loop", debug=True)
    assert "hidden newest loop" not in json.dumps(pack.get("open_loops", []))


@pytest.mark.parametrize(("label", "domain", "level"), CHAINS)
def test_a_hidden_decision_three_deep_does_not_change_the_last_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, domain: str, level: str
) -> None:
    """Mutation: replace ``domains=domain_filter`` with ``domains=None`` in the memory check of ``_vnext_resume`` (the health
    chain), or ``sensitivity_allowed=sensitivity_filter`` with the trusted levels there (the private chain)."""

    vault = _Vault(tmp_path, monkeypatch)
    decision_is_not_changed(vault.call, vault.store, vault.key("read_only"), domain, level)


@pytest.mark.parametrize(("label", "domain", "level"), CHAINS)
def test_a_hidden_loop_three_deep_does_not_change_the_open_loops_of_resume_or_the_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, domain: str, level: str
) -> None:
    """Mutation: replace ``sensitivity_allowed=sensitivity_filter`` with the trusted levels in the open loop check of
    ``_vnext_resume`` (the private chain), or ``sensitivity_allowed=sensitivity_allowed`` with the trusted levels in the open
    loop check of ``compile_context_pack``."""

    vault = _Vault(tmp_path, monkeypatch)
    loops_are_not_changed(vault.call, vault.store, vault.key("read_only"), domain, level)


@pytest.mark.parametrize(
    ("label", "domain", "level", "profile", "readable"),
    [
        ("health-chain", "health", "public", "read_only", False),
        ("private-chain", "project", "private", "read_only", False),
        ("private-chain-proposal", "project", "private", "proposal", True),
    ],
)
def test_a_hidden_mention_three_deep_does_not_change_the_grounding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, domain: str, level: str, profile: str, readable: bool
) -> None:
    """Mutation: replace ``sensitivity_allowed=sensitivity_allowed`` with the trusted levels (the private chain of the
    ``read_only`` key) or ``domains=domains`` with ``domains=None`` (the health chain) in the grounding check of
    ``compile_context_pack``.

    The ``proposal`` key's ceiling is ``private``, so the same row is readable to it and supports the name: the check must
    not hide more than the limits do.
    """

    vault = _Vault(tmp_path, monkeypatch)
    mention_is_judged(vault.call, vault.store, vault.key(profile), domain, level, readable)


def test_a_loop_and_a_decision_from_another_project_do_not_change_the_brief_of_a_bound_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: replace ``projects=effective_project_scope`` with ``projects=()`` in the open loop check of ``_vnext_resume``.

    A key bound to project ``alpha`` is shown a loop of its own. A newer loop and decision derived from a source of project
    ``beta`` are written with the labels of ``alpha``: only the project scope of the final check takes them out.
    """

    vault = _Vault(tmp_path, monkeypatch, scoped=True)
    bound_key_is_not_changed(vault.call, vault.store, vault.key("read_only_alpha"))
