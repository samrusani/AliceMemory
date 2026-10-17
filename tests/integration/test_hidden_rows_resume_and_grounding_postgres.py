"""The grounding of the context pack and the brief of ``alice_resume`` do not depend on rows the caller may not read, on PostgreSQL.

New in v0.20.2. The same checks as ``tests/unit/test_hidden_rows_resume_and_grounding.py`` on a real
PostgreSQL store, through the MCP tool registry and a minted key. The probe of the grounding block asks the store's
``search_memories_fts`` (with ``offset``) and the brief asks ``list_memories`` and the two event readers; the checks that
decide which rows a caller may read are the shared ones.

Each test names the mutation that must fail it, and the cases ``grounding-*`` and ``resume-*`` of
``scripts/derived_label_mutations.json`` replay them.
"""

from __future__ import annotations

import json

import pytest

from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.test_recall_fallback_hidden_rows_postgres import _Postgres
from tests.unit.test_hidden_rows_resume_and_grounding import (
    BOTH_UNSUPPORTED,
    NAME_TEXT,
    NAMES_QUERY,
    ONE_UNSUPPORTED,
)
from tests.unit.test_recall_fallback_hidden_rows import _memory, _source


def _grounding(vault: _Postgres, key: str | None) -> tuple[object, object]:
    pack = vault.call(key, "alice_context_pack", query=NAMES_QUERY, debug=True)
    return pack.get("grounding"), pack["trace"].get("grounding")  # type: ignore[union-attr]


def _brief(vault: _Postgres, key: str | None, **arguments: object) -> dict[str, object]:
    brief = vault.call(key, "alice_resume", **arguments)["brief"]
    return {name: value for name, value in brief.items() if name != "generated_at"}  # type: ignore[union-attr]


@pytest.mark.parametrize(("shape", "name"), [("domain", "read_only"), ("chain", "trusted"), ("missing_input", "proposal")])
def test_a_hidden_row_that_mentions_a_name_does_not_change_the_grounding_of_a_restricted_key(
    label_harness, monkeypatch: pytest.MonkeyPatch, shape: str, name: str  # noqa: F811
) -> None:
    """Mutation: replace ``if readable_rows is not None:`` in ``_probe_rows`` with ``if False:``."""

    vault = _Postgres(label_harness, monkeypatch, name)
    before = _grounding(vault, vault.key)
    assert before == (BOTH_UNSUPPORTED, BOTH_UNSUPPORTED)
    hidden = vault.hide(shape, NAME_TEXT)
    assert _grounding(vault, None) == (ONE_UNSUPPORTED, ONE_UNSUPPORTED), "the row must mention the name for the owner"
    assert _grounding(vault, vault.key) == before
    assert str(hidden["id"]) not in json.dumps(vault.call(vault.key, "alice_context_pack", query=NAMES_QUERY, debug=True), default=str)


def test_the_owner_keeps_the_grounding_it_had(label_harness, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Mutation: replace ``if source_fence.entity_read_fenced`` in the grounding call of ``compile_context_pack`` with ``if True``."""

    vault = _Postgres(label_harness, monkeypatch, "read_only")
    vault.hide("chain", NAME_TEXT)
    assert _grounding(vault, None) == (ONE_UNSUPPORTED, ONE_UNSUPPORTED)


@pytest.mark.parametrize("part", ["last_decision", "next_action", "recent_changes"])
def test_a_newer_hidden_row_does_not_change_the_resume_of_a_restricted_key(
    label_harness, monkeypatch: pytest.MonkeyPatch, part: str  # noqa: F811
) -> None:
    """Mutation: set ``limited = False`` in ``_vnext_resume``."""

    vault = _Postgres(label_harness, monkeypatch, "read_only")
    arguments: dict[str, object] = {"query": "alpha"}
    if part == "next_action":
        with label_harness.store() as store:
            _memory(store, "visible-commitment", "alpha visible commitment", {}, memory_type="commitment")
    if part == "recent_changes":
        arguments = {"max_recent_changes": 3}
    before = _brief(vault, vault.key, **arguments)
    assert before[part], before

    with label_harness.store() as store:
        parent = _source(store, "parent-health", domain="health")
        hidden = [
            _memory(store, f"hidden-{index}", f"alpha hidden {part} {index}", {"source_id": str(parent["id"])},
                    memory_type="commitment" if part == "next_action" else "decision")
            for index in range(1 if part != "recent_changes" else 6)
        ]
    hidden_ids = {str(row["id"]) for row in hidden}
    everything = _brief(vault, None, **arguments, sensitivity_allowed=list(ALL_SENSITIVITY))
    assert any(hidden_id in json.dumps(everything[part], default=str) for hidden_id in hidden_ids)

    after = _brief(vault, vault.key, **arguments)
    assert after == before
    assert not any(hidden_id in json.dumps(after, default=str) for hidden_id in hidden_ids)


def test_the_owner_keeps_the_resume_it_had(label_harness, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Mutation: set ``limited = True`` in ``_vnext_resume``."""

    vault = _Postgres(label_harness, monkeypatch, "read_only")
    assert _brief(vault, None, query="alpha")["last_decision"]
    vault.hide("chain")
    assert _brief(vault, None, query="alpha")["last_decision"] is None
    assert _brief(vault, vault.key, query="alpha")["last_decision"]
