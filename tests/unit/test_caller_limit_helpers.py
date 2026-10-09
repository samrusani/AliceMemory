"""The three helpers that put a caller's limits on an operator read, with no store and no database.

``clamp_request_filters`` cuts the domains and sensitivities a request names down to what its caller may read,
``readable_own_label_rows`` keeps the rows with labels of their own (a queued task, the brain charter) that the
caller's fence admits, and ``guard_for_caller`` builds the list guard of a caller from its policy.

Mutations, each one alone: return the requested filters from ``clamp_request_filters`` for every identity; return
every row from ``readable_own_label_rows``; return an inactive guard from ``guard_for_caller`` for a caller with
limits; drop ``all_of`` from the guard of a locked key; do not raise for a blocked decision.
"""
from __future__ import annotations

import pytest

from alicebot_api.vnext_agent_control import AgentIdentity, AgentPolicyBlockedError
from alicebot_api.vnext_label_guard import clamp_request_filters, guard_for_caller, readable_own_label_rows

ALPHA = "prj_" + "a" * 16
LEVELS = ("public", "internal", "private", "confidential", "highly_sensitive", "sacred", "regulated", "unknown")


def _who(profile: str, *, project: str | None = None) -> AgentIdentity:
    return AgentIdentity(
        agent_id="reader", permission_profile=profile, auth="agent_api_key",
        project_scope=(project,) if project else (), project_scope_locked=project is not None,
    )


def test_the_owner_and_an_unbound_admin_key_get_the_filters_they_asked_for() -> None:
    assert clamp_request_filters(None, domains=("health",), sensitivity_allowed=LEVELS) == (("health",), LEVELS)
    assert clamp_request_filters(_who("admin_agent"), domains=("health",), sensitivity_allowed=LEVELS) == (("health",), LEVELS)


def test_a_trusted_key_asking_for_a_level_above_its_ceiling_is_read_at_its_own_levels() -> None:
    domains, sensitivity = clamp_request_filters(_who("trusted_local_agent"), domains=(), sensitivity_allowed=LEVELS)
    assert domains == ()
    assert sensitivity == ("public", "internal", "private", "unknown")
    _domains, only_confidential = clamp_request_filters(
        _who("trusted_local_agent"), domains=(), sensitivity_allowed=("confidential",)
    )
    assert only_confidential == ("public", "internal", "private", "unknown")


def test_a_read_only_key_loses_the_restricted_domains_and_the_private_level() -> None:
    domains, sensitivity = clamp_request_filters(
        _who("read_only_agent"), domains=("health", "project"), sensitivity_allowed=LEVELS, action="memory.recent_commits"
    )
    assert domains == ("project",)
    assert sensitivity == ("public", "internal", "unknown")


def test_a_caller_the_policy_blocks_raises() -> None:
    with pytest.raises(AgentPolicyBlockedError):
        clamp_request_filters(_who("read_only_agent"), domains=(), sensitivity_allowed=LEVELS)
    with pytest.raises(AgentPolicyBlockedError):
        clamp_request_filters(_who("trusted_local_agent", project=ALPHA), domains=(), sensitivity_allowed=LEVELS)


ROWS = [
    {"id": "public", "domain": "unknown", "sensitivity": "public"},
    {"id": "private", "domain": "unknown", "sensitivity": "private"},
    {"id": "confidential", "domain": "unknown", "sensitivity": "confidential"},
    {"id": "health", "domain": "health", "sensitivity": "public"},
    {"id": "scoped", "domain": "unknown", "sensitivity": "public", "metadata_json": {"project_scope": [ALPHA]}},
]


def _ids(identity: AgentIdentity | None) -> list[str]:
    return [row["id"] for row in readable_own_label_rows(identity, ROWS)]


def test_the_owner_and_an_unbound_admin_key_are_shown_every_row_of_their_own() -> None:
    everything = ["public", "private", "confidential", "health", "scoped"]
    assert _ids(None) == everything
    assert _ids(_who("admin_agent")) == everything


def test_a_trusted_key_is_shown_the_rows_inside_its_ceiling() -> None:
    assert _ids(_who("trusted_local_agent")) == ["public", "private", "health", "scoped"]


def test_a_read_only_key_is_also_held_back_from_a_restricted_domain() -> None:
    assert _ids(_who("read_only_agent")) == ["public", "scoped"]


def test_a_key_locked_to_a_project_is_shown_only_the_rows_inside_it() -> None:
    assert _ids(_who("trusted_local_agent", project=ALPHA)) == ["scoped"]


def test_rows_that_are_not_mappings_are_dropped() -> None:
    assert readable_own_label_rows(None, [None, "text", {"id": "row"}]) == [{"id": "row"}]  # type: ignore[list-item]


def test_the_guard_of_the_owner_and_of_an_unbound_admin_key_returns_its_input() -> None:
    rows = [{"id": "x", "sensitivity": "confidential", "domain": "health"}]
    for identity in (None, _who("admin_agent")):
        guard = guard_for_caller(object(), identity)
        assert guard.active is False
        assert guard.admit_rows("project", rows) == rows


def test_the_guard_of_a_caller_with_limits_carries_the_filters_its_policy_grants() -> None:
    guard = guard_for_caller(object(), _who("trusted_local_agent"), action="memory.recent_commits")
    assert guard.active is True
    assert guard.sensitivity_allowed == ("public", "internal", "private", "unknown")
    assert guard.all_of is None and guard.projects == ()
    bound = guard_for_caller(object(), _who("trusted_local_agent", project=ALPHA), action="memory.recent_commits")
    assert bound.projects == bound.all_of and len(bound.projects) == 1


def test_the_guard_of_a_blocked_caller_is_not_built() -> None:
    with pytest.raises(AgentPolicyBlockedError):
        guard_for_caller(object(), _who("read_only_agent"))
