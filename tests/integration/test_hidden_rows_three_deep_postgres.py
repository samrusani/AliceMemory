"""A row three memories below a source is judged by its effective domain and level, on PostgreSQL.

Unreleased (on main, not in v0.20.0). The scenarios of ``tests/unit/test_hidden_rows_three_deep.py`` on a real PostgreSQL
store, through the MCP tool registry and a minted key: a hidden decision, a hidden open loop (in the brief and in the pack)
and a hidden mention (in the grounding block) three memories below a source the key may not read, and, for a key bound to a
project, a decision and a loop derived from a source of another project.

Each test names the mutation that must fail it, and the cases ``three-deep-*`` of ``scripts/derived_label_mutations.json``
replay them.
"""

from __future__ import annotations

import pytest

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.test_recall_fallback_hidden_rows_postgres import _Postgres
from tests.unit.test_hidden_rows_three_deep import (
    CHAINS,
    bound_key_is_not_changed,
    decision_is_not_changed,
    loops_are_not_changed,
    mention_is_judged,
)


@pytest.mark.parametrize(("label", "domain", "level"), CHAINS)
def test_a_hidden_decision_three_deep_does_not_change_the_last_decision(
    label_harness, monkeypatch: pytest.MonkeyPatch, label: str, domain: str, level: str  # noqa: F811
) -> None:
    """Mutation: the edits of the SQLite test of this name."""

    vault = _Postgres(label_harness, monkeypatch, "read_only")
    decision_is_not_changed(vault.call, label_harness.store, vault.key, domain, level)


@pytest.mark.parametrize(("label", "domain", "level"), CHAINS)
def test_a_hidden_loop_three_deep_does_not_change_the_open_loops_of_resume_or_the_pack(
    label_harness, monkeypatch: pytest.MonkeyPatch, label: str, domain: str, level: str  # noqa: F811
) -> None:
    """Mutation: the edits of the SQLite test of this name."""

    vault = _Postgres(label_harness, monkeypatch, "read_only")
    loops_are_not_changed(vault.call, label_harness.store, vault.key, domain, level)


@pytest.mark.parametrize(
    ("label", "domain", "level", "profile", "readable"),
    [
        ("health-chain", "health", "public", "read_only", False),
        ("private-chain", "project", "private", "read_only", False),
        ("private-chain-proposal", "project", "private", "proposal", True),
    ],
)
def test_a_hidden_mention_three_deep_does_not_change_the_grounding(
    label_harness, monkeypatch: pytest.MonkeyPatch, label: str, domain: str, level: str, profile: str, readable: bool  # noqa: F811
) -> None:
    """Mutation: the edits of the SQLite test of this name."""

    vault = _Postgres(label_harness, monkeypatch, profile)
    mention_is_judged(vault.call, label_harness.store, vault.key, domain, level, readable)


def test_a_loop_and_a_decision_from_another_project_do_not_change_the_brief_of_a_bound_key(
    label_harness, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Mutation: the edit of the SQLite test of this name."""

    vault = _Postgres(label_harness, monkeypatch, "read_only_alpha")
    bound_key_is_not_changed(vault.call, label_harness.store, vault.key)
