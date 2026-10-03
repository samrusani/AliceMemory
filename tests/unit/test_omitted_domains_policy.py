"""A request that names no domain is held to the profile's domains, in the policy engine.

Unreleased (on main, not in v0.20.0). In v0.20.0 ``_filtered_domains`` removed the held-back domains (family, health,
spiritual, legal, financial) from the domains a caller listed, and left a request that listed none as an empty
``effective_domains``. Every reader reads an empty tuple as "no domain filter", so a key that was refused when it asked for
``health`` read health notes when it asked for nothing. The engine now writes the profile's permitted domains out when
the request names none, and never hands an empty tuple to a restricted profile.

These tests hold the engine. The readers are held in ``test_omitted_domains_every_reader.py``.

Each test names the mutation that must fail it. The mutations were made by hand in a scratch edit of
``vnext_agent_control.py`` and the file was restored by copying the saved copy back.
"""

from __future__ import annotations

import pytest

import alicebot_api.vnext_agent_control as agent_control
import alicebot_api.vnext_memory_commit as memory_commit
from alicebot_api.vnext_agent_control import (
    PERMISSION_PROFILES,
    READ_ACTIONS,
    RESTRICTED_DOMAINS,
    UNRESTRICTED_DOMAIN_PROFILES,
    VNEXT_DOMAINS,
    WRITE_ACTIONS,
    AgentIdentity,
    evaluate_agent_policy,
    permitted_domains,
)

HELD_BACK = ("family", "health", "spiritual", "legal", "financial")
PERMITTED = ("professional", "personal", "learning", "relationship", "project", "agent_run", "system", "unknown")
RESTRICTED_PROFILES = tuple(p for p in PERMISSION_PROFILES if p not in {"trusted_local_agent", "admin_agent"})
UNRESTRICTED_PROFILES = ("trusted_local_agent", "admin_agent")

#: The read actions the tools and routes ask the engine about with a domain list.
READ_ACTIONS_WITH_DOMAINS = (
    "memory.recall",
    "context_pack.request",
    "open_loop.lookup",
    "recent_decisions.lookup",
    "recent_changes.lookup",
    "review_items.lookup",
    "connections.find",
    "contradictions.find",
)

#: Every shape of a domain request, including the ones that are not labels.
REQUESTS: dict[str, tuple[str, ...]] = {
    "omitted": (),
    "one_permitted": ("project",),
    "one_held_back": ("health",),
    "mixed": ("project", "health"),
    "unknown_label": ("unknown",),
    "not_a_label": ("banana",),
    "wrong_case": ("HEALTH",),
    "all_thirteen": tuple(VNEXT_DOMAINS),
    "only_held_back": HELD_BACK,
}


def _identity(profile: str) -> AgentIdentity:
    return AgentIdentity(agent_id=f"agent-{profile}", agent_type="coding_agent", permission_profile=profile)


def _decide(profile: str, action: str, domains: tuple[str, ...]) -> agent_control.PolicyDecision:
    return evaluate_agent_policy(
        identity=_identity(profile),
        action=action,
        domains=domains,
        # Under every profile's ceiling, so a sensitivity filter never makes the decision differ.
        sensitivity_allowed=("public",),
        project_scope=("alpha",),
    )


# ---------------------------------------------------------------------------------------------------------------------
# 1. What a profile may read


def test_the_permitted_set_is_the_thirteen_labels_minus_the_five_and_includes_unknown() -> None:
    """Mutation: drop ``"unknown"`` from the set (filter it out in ``permitted_domains``), or hold back ``personal``.

    The rule: a restricted profile reads every domain except family, health, spiritual, legal and financial,
    including ``unknown``. ``unknown`` stays in because the stores return ``unknown`` rows under every domain filter
    and the source fence tests the row's own label the same way, so leaving it out here would only disagree with them.
    """

    for profile in RESTRICTED_PROFILES:
        permitted = permitted_domains(profile)
        assert permitted == PERMITTED
        assert "unknown" in permitted
        assert set(permitted).isdisjoint(RESTRICTED_DOMAINS)
        assert set(permitted) | set(RESTRICTED_DOMAINS) == set(VNEXT_DOMAINS)


def test_unrestricted_profiles_have_no_permitted_set_and_that_is_not_an_empty_one() -> None:
    """Mutation: return ``()`` for the trusted profile (``permitted_domains`` answers an empty tuple, not ``None``).

    ``None`` means every domain; an empty tuple would mean none. The engine tells them apart, and the first one is the
    only answer that lets a request with no domains read everything.
    """

    assert permitted_domains("trusted_local_agent") is None
    assert permitted_domains("admin_agent") is None
    assert UNRESTRICTED_DOMAIN_PROFILES == frozenset(UNRESTRICTED_PROFILES)


def test_the_vocabulary_has_one_definition() -> None:
    """Mutation: write the thirteen labels out a second time in ``vnext_memory_commit.py`` instead of importing them.

    The commit service, the tool schemas and the policy engine read one tuple. Two copies could drift, and the engine
    derives what a profile may read from it.
    """

    assert memory_commit.VNEXT_DOMAINS is agent_control.VNEXT_DOMAINS
    assert len(VNEXT_DOMAINS) == 13
    assert len(set(VNEXT_DOMAINS)) == 13


# ---------------------------------------------------------------------------------------------------------------------
# 2. A request that names no domain


@pytest.mark.parametrize("profile", RESTRICTED_PROFILES)
@pytest.mark.parametrize("action", READ_ACTIONS_WITH_DOMAINS)
def test_a_restricted_profile_that_names_no_domain_gets_its_permitted_domains(profile: str, action: str) -> None:
    """Mutation: remove the ``if not domains: return permitted, ()`` branch of ``_filtered_domains``.

    ``effective_domains`` is the eight permitted labels, in the order of the vocabulary. The decision stays
    ``allowed`` with no reason added (``allowed_with_filtering`` would make every caller that refuses a filtered
    target, the artifact doors, refuse a project key), and ``requested_domains`` still says nothing was asked.
    """

    decision = _decide(profile, action, ())
    assert decision.decision == "allowed"
    assert decision.reasons == ()
    assert decision.requested_domains == ()
    assert decision.effective_domains == PERMITTED
    assert set(decision.effective_domains).isdisjoint(HELD_BACK)


def test_the_policy_record_of_an_omitted_request_says_what_was_asked_and_what_was_allowed() -> None:
    """Mutation: record the permitted domains as the requested ones (``requested_domains=effective_domains``).

    The record is what the policy event and the context pack carry. The request is empty, the effective list is the
    eight labels, so an auditor can tell a request for nothing from a request for those eight.
    """

    record = _decide("project_scoped_agent", "memory.recall", ()).to_record()
    assert record["requested_domains"] == []
    assert record["effective_domains"] == list(PERMITTED)
    assert record["decision"] == "allowed"


@pytest.mark.parametrize("profile", UNRESTRICTED_PROFILES)
@pytest.mark.parametrize("action", READ_ACTIONS_WITH_DOMAINS)
def test_an_unrestricted_profile_that_names_no_domain_still_reads_every_domain(profile: str, action: str) -> None:
    """Mutation: empty ``UNRESTRICTED_DOMAIN_PROFILES`` so the trusted and admin profiles take the permitted set.

    Their ``effective_domains`` stays empty, which every reader reads as "no domain filter", as before. A request
    that does name domains is passed through whole, held-back ones included.
    """

    decision = _decide(profile, action, ())
    assert decision.decision == "allowed"
    assert decision.effective_domains == ()
    assert decision.reasons == ()
    named = _decide(profile, action, ("health", "project"))
    assert named.effective_domains == ("health", "project")
    assert named.decision == "allowed"


@pytest.mark.parametrize("domains", [(), ("health",), ("health", "project"), ("banana",)])
def test_the_owner_is_unchanged(domains: tuple[str, ...]) -> None:
    """Mutation: apply the permitted set to a call with no identity (derive it before the ``identity is None`` return).

    A call with no agent identity is the owner. Whatever it asks for is what the readers get, an empty request included.
    """

    decision = evaluate_agent_policy(identity=None, action="memory.recall", domains=domains)
    assert decision.decision == "allowed"
    assert decision.effective_domains == domains
    assert decision.permission_profile == "user_or_system"


# ---------------------------------------------------------------------------------------------------------------------
# 3. A request that names domains is a further restriction


@pytest.mark.parametrize("profile", RESTRICTED_PROFILES)
def test_a_named_domain_narrows_the_permitted_set_and_a_held_back_one_is_still_refused(profile: str) -> None:
    """Mutation: let a named domain widen the request (skip the held-back test in the explicit branch).

    ``project`` alone is ``project`` alone, not the eight. ``health`` alone is refused as it was in v0.20.0. A mix
    keeps the permitted label and is marked as filtered, as in v0.20.0.
    """

    one = _decide(profile, "memory.recall", ("project",))
    assert (one.decision, one.effective_domains, one.reasons) == ("allowed", ("project",), ())

    refused = _decide(profile, "memory.recall", ("health",))
    assert refused.decision == "blocked"
    assert refused.effective_domains == ()
    assert "all_requested_domains_restricted" in refused.reasons

    mixed = _decide(profile, "memory.recall", ("project", "health"))
    assert mixed.decision == "allowed_with_filtering"
    assert mixed.effective_domains == ("project",)
    assert "restricted_domain_filtered" in mixed.reasons

    unknown = _decide(profile, "memory.recall", ("unknown",))
    assert unknown.decision == "allowed"
    assert unknown.effective_domains == ("unknown",)

    everything = _decide(profile, "memory.recall", tuple(VNEXT_DOMAINS))
    assert everything.decision == "allowed_with_filtering"
    assert everything.effective_domains == PERMITTED


@pytest.mark.parametrize("profile", RESTRICTED_PROFILES)
@pytest.mark.parametrize("label", ["banana", "HEALTH", "Health"])
def test_a_request_that_is_not_a_label_never_becomes_no_filter(profile: str, label: str) -> None:
    """Mutation: intersect the request with the permitted set and pass the empty result on (return ``()`` for
    ``("banana",)``), which a reader would read as "everything".

    A word that is not a label matches no row under a domain filter (labels are lowercase by the database check) and
    the stores add the ``unknown`` rows, so the request returns what a named domain returns and no more. It must not
    turn into an empty tuple. ``HEALTH`` is not ``health`` to the engine, and the readers compare labels exactly, so
    it matches no health row either; the end-to-end test proves that on a vault.
    """

    decision = _decide(profile, "memory.recall", (label,))
    assert decision.decision == "allowed"
    assert decision.effective_domains == (label,)
    assert decision.effective_domains != ()
    assert set(decision.effective_domains).isdisjoint(HELD_BACK)


# ---------------------------------------------------------------------------------------------------------------------
# 4. The invariant, over every profile, action and request


@pytest.mark.parametrize("profile", PERMISSION_PROFILES)
def test_no_restricted_profile_leaves_the_engine_with_an_empty_filter_or_a_held_back_domain(profile: str) -> None:
    """Mutation: remove the omitted-request branch of ``_filtered_domains`` (the same edit as the first test above).

    For every profile, every action the engine knows and every shape of request: if the profile is restricted and the
    decision is not a refusal, ``effective_domains`` is not empty and holds none of the five. Readers treat an empty
    tuple as "no filter", so an empty one for a restricted profile is a leak, and this is the check for it.
    """

    restricted = profile not in UNRESTRICTED_PROFILES
    seen = 0
    for action in sorted(READ_ACTIONS | WRITE_ACTIONS):
        for name, domains in REQUESTS.items():
            decision = _decide(profile, action, domains)
            if restricted and decision.decision != "blocked":
                seen += 1
                assert decision.effective_domains, (profile, action, name)
                assert set(decision.effective_domains).isdisjoint(HELD_BACK), (profile, action, name)
            if not restricted and not domains and decision.decision != "blocked":
                assert decision.effective_domains == (), (profile, action, name)
    if restricted:
        assert seen > 50  # the loop judged many decisions, so a refusal of everything cannot pass it


def test_a_profile_with_no_permitted_domains_is_refused_not_unrestricted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: remove the ``elif not effective_domains and permitted_domains(profile) is not None`` branch.

    No profile has an empty permitted set today. If every domain were held back, a request that names none would
    have an empty effective list, which a reader reads as "everything". The engine must refuse it instead.
    """

    monkeypatch.setattr(agent_control, "RESTRICTED_DOMAINS", frozenset(VNEXT_DOMAINS))
    assert permitted_domains("project_scoped_agent") == ()
    decision = _decide("project_scoped_agent", "memory.recall", ())
    assert decision.decision == "blocked"
    assert decision.reasons == ("no_permitted_domains",)
    assert decision.effective_domains == ()
    # A named domain gets the refusal it always got, with its own reason.
    named = _decide("project_scoped_agent", "memory.recall", ("project",))
    assert named.decision == "blocked"
    assert "all_requested_domains_restricted" in named.reasons
    # An unrestricted profile is not affected by the held-back set at all.
    assert _decide("trusted_local_agent", "memory.recall", ()).decision == "allowed"
