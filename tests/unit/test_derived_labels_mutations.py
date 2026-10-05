"""Kill each pure derived-label guard in memory. A survivor fails the run."""

from __future__ import annotations

import inspect
import sys
import textwrap
from types import FunctionType

import pytest

from alicebot_api import vnext_agent_control as agent_control
from alicebot_api import vnext_brain as brain
from alicebot_api import vnext_derived_labels as labels
from tests.unit import test_derived_labels_kernel as checks


def kill(owner, name, before, after, check):
    original = getattr(owner, name)
    source = textwrap.dedent(inspect.getsource(original))
    if before not in source and before.replace("'", '"') in source:
        before, after = before.replace("'", '"'), after.replace("'", '"')
    assert before in source, f"mutation no longer matches: {name}: {before}"
    namespace = dict(original.__globals__)
    exec(compile(source.replace(before, after, 1), "<guard mutation>", "exec"), namespace)
    compiled = namespace[name]
    replacement = FunctionType(
        compiled.__code__, original.__globals__, name, compiled.__defaults__, compiled.__closure__
    )
    replacement.__kwdefaults__ = compiled.__kwdefaults__
    aliases = [
        (module, key)
        for module in list(sys.modules.values())
        if module
        and (
            getattr(module, "__name__", "").startswith("alicebot_api")
            or module in (checks,)
        )
        for key, value in list(vars(module).items())
        if value is original
    ]
    for module, key in aliases:
        setattr(module, key, replacement)
    setattr(owner, name, replacement)
    try:
        try:
            check()
        except (AssertionError, pytest.fail.Exception):
            print(f"KILLED {name}: {before}")
        else:
            raise RuntimeError(f"SURVIVED {name}: {before}")
    finally:
        setattr(owner, name, original)
        for module, key in aliases:
            setattr(module, key, original)


def test_derived_label_kernel_guard_mutations():
    kill(
        labels,
        "_raised_sensitivity",
        "if rank > chosen_rank:",
        "if rank >= chosen_rank:",
        checks.test_sensitivity_is_raise_only_and_unknown_never_swaps_with_internal,
    )
    kill(
        labels,
        "union_floor",
        "combined = [*stored, *[item for part in parts for item in part]]",
        "combined = list(stored)",
        checks.test_floor_is_the_union_and_never_shrinks,
    )
    kill(
        labels,
        "_effective_scope",
        "return _copy_scope(current.stored_scope, parents)",
        "return current.stored_scope",
        checks.test_scope_rules_per_row_class,
    )
    kill(
        labels,
        "_effective_scope",
        "return ()  # aggregate rule otherwise leaves the scope empty",
        "return stored",
        checks.test_scope_rules_per_row_class,
    )
    kill(
        labels,
        "settle_labels",
        'problems[key] = "missing_dependency"',
        "pass",
        checks.test_every_unverified_case_is_unverified,
    )
    kill(
        labels,
        "_derived_from_deps",
        'if expected != len(ids):\n            return "counts_disagree", found',
        'if expected != len(ids):\n            return "", found',
        checks.test_every_unverified_case_is_unverified,
    )
    kill(
        labels,
        "_spread_unverified",
        'problems[key] = "dependency_unverified"\n                changed = True',
        "pass",
        checks.test_unverified_is_contagious_through_every_level,
    )
    kill(
        labels,
        "dependency_record",
        "if not problem and not _record_present(meta, row) and not found:",
        "if not problem and not found:",
        checks.test_every_unverified_case_is_unverified,
    )
    kill(
        agent_control,
        "evaluate_agent_policy",
        "elif project_floor and not project_floor_within(project_floor, identity.project_scope):",
        "elif False and not project_floor_within(project_floor, identity.project_scope):",
        checks.test_project_floor_blocks_a_locked_key_and_does_not_change_an_empty_floor,
    )
    kill(
        labels,
        "_effective_scope",
        "return ()  # a global input empties the report scope",
        "return current.stored_scope",
        checks.test_scope_rules_per_row_class,
    )
    kill(
        labels,
        "carries_scope",
        'if name == "source" and _scrubbed(row):',
        "if False and _scrubbed(row):",
        checks.test_every_unverified_case_is_unverified,
    )
    kill(
        labels,
        "_legacy_count_problem",
        'return "legacy_counts"',
        'return ""',
        checks.test_every_unverified_case_is_unverified,
    )
    kill(
        labels,
        "labels_raised_payload",
        'return {"cause": cause, "previous": side(previous), "new": side(new)}',
        'return {"cause": cause, "previous": side(previous), "new": side(new), "input_id": "row-1"}',
        checks.test_labels_raised_events_carry_no_text_or_ids,
    )
    original = dict(brain.SENSITIVITY_RANK)
    brain.SENSITIVITY_RANK["sacred"] = 7
    try:
        try:
            checks.test_the_seven_sensitivity_rank_tables_equal_the_kernel_table()
        except (AssertionError, pytest.fail.Exception):
            print("KILLED SENSITIVITY_RANK sacred")
        else:
            raise RuntimeError("SURVIVED brain sensitivity rank")
    finally:
        brain.SENSITIVITY_RANK.clear()
        brain.SENSITIVITY_RANK.update(original)
