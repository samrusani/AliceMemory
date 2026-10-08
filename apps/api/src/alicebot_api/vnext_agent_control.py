from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from typing import Iterable, Mapping
from uuid import uuid4

from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_promotion_policy import (
    PromotionCandidate,
    PromotionDecision,
    PromotionSettings,
    evaluate_promotion,
    writer_trust_for,
)
from alicebot_api.vnext_project_scope import (
    normalize_project_identifier,
    normalize_project_scope,
    project_floor_within,
    project_scope_identity,
    resolve_project_scope,
)
from alicebot_api.vnext_repositories import JsonObject


AGENT_TYPES = (
    "personal_assistant",
    "coding_agent",
    "research_agent",
    "workflow_agent",
    "unknown",
)

PERMISSION_PROFILES = (
    "read_only_agent",
    "project_scoped_agent",
    "trusted_local_agent",
    "memory_proposal_agent",
    "admin_agent",
)

POLICY_DECISIONS = ("allowed", "allowed_with_filtering", "requires_review", "blocked")

AGENT_AUTH_METHODS = ("agent_api_key", "unauthenticated_local")

RESTRICTED_DOMAINS = frozenset({"family", "health", "spiritual", "legal", "financial"})
# The thirteen domain labels. One definition: the commit service, the tool schemas and the schema vocabulary check
# all read this tuple (``vnext_memory_commit`` still exports the name), and the policy engine derives what a
# profile may read from it. A test keeps both database schemas equal to it.
VNEXT_DOMAINS = (
    "professional",
    "personal",
    "family",
    "health",
    "spiritual",
    "financial",
    "legal",
    "learning",
    "relationship",
    "project",
    "agent_run",
    "system",
    "unknown",
)
# Profiles that read every domain. Any other profile is held back from RESTRICTED_DOMAINS.
UNRESTRICTED_DOMAIN_PROFILES = frozenset({"trusted_local_agent", "admin_agent"})
RESTRICTED_SENSITIVITY = frozenset({"confidential", "highly_sensitive", "sacred", "regulated"})
ALL_SENSITIVITY = ("public", "internal", "private", "confidential", "highly_sensitive", "sacred", "regulated", "unknown")
DEFAULT_AGENT_SENSITIVITY = ("public", "internal", "private", "unknown")

READ_ACTIONS = {
    "memory.recall",
    "context_pack.request",
    "project.dashboard",
    "open_loop.lookup",
    "recent_decisions.lookup",
    "recent_changes.lookup",
    "connections.find",
    "contradictions.find",
    "review_items.lookup",
    "artifact.lookup",
    "memory.audit",
    "memory.recent_commits",
    "scheduler.status",
    "http.operator.access",
}

WRITE_ACTIONS = {
    "source.capture",
    "queue_task.create",
    "artifact.generate",
    "memory.commit",
    "memory.confirm",
    "memory.correct",
    "memory.forget",
    "memory.undo",
    "memory.expire",
    "memory.unexpire",
    "memory.redact",
    "memory.accept_consolidation",
    "memory.propose",
    "memory.quarantine",
    "http.route.mutate",
    "open_loop.create",
    "open_loop.update",
    "artifact.review",
    "artifact.feedback",
    "artifact.export",
    "scheduler.run_now",
    "scheduler.run_due",
    "scheduler.pause",
    "scheduler.resume",
    "scheduler.configure",
}

# ``memory_proposal_agent`` may submit a review-only memory proposal (the
# ``memory.commit`` entry point is deliberately retained because the commit
# service downgrades this profile to a candidate).  It may not apply review
# decisions or mutate any already-persisted object.
PROPOSAL_ONLY_WRITE_ACTIONS = frozenset({"memory.propose", "memory.commit"})

# Actions only a human (identity=None) or an admin agent may perform:
# review decisions (artifact/memory review, consolidation acceptance) and
# destructive operations (content redaction). Enforced in
# evaluate_agent_policy with the same profile-restriction mechanism as the
# scheduler controls.
HUMAN_OR_ADMIN_ACTIONS = frozenset(
    {
        "artifact.review",
        "memory.review",
        "memory.redact",
        "memory.accept_consolidation",
        # Quarantining everything one agent key auto-promoted is an operator
        # action. An agent that could run it against another key could bury
        # that key's writes; an agent that could run it against its own could
        # not erase anything (the sweep only expires, and is itself audited),
        # but it still has no business holding the control.
        "memory.quarantine",
    }
)


class AgentIdentityValidationError(ValueError):
    """Raised when an agent identity payload is malformed."""


class AgentPolicyBlockedError(PermissionError):
    """Raised when a vNext agent policy blocks an operation."""

    def __init__(self, decision: "PolicyDecision") -> None:
        super().__init__("agent policy blocked this action")
        self.decision = decision


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    agent_id: str
    agent_type: str = "unknown"
    agent_run_id: str | None = None
    task_id: str | None = None
    project_scope: tuple[str, ...] = ()
    permission_profile: str = "read_only_agent"
    auth: str = "unauthenticated_local"
    # True only when the authenticated key record carries a project_scope
    # binding. Never payload-settable: resolve_agent_identity sets it, and
    # from_payload always leaves it False. When locked, policy evaluation
    # inherits the binding for omitted scopes and blocks every read or write
    # whose explicit project scope leaves the binding.
    project_scope_locked: bool = False

    @property
    def actor_type(self) -> str:
        return "agent"

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "AgentIdentity | None":
        nested = payload.get("agent_identity")
        source: Mapping[str, object]
        if isinstance(nested, Mapping):
            source = nested
        else:
            source = payload

        agent_id = _optional_text(source.get("agent_id"))
        if agent_id is None:
            return None

        agent_type = _optional_text(source.get("agent_type")) or _default_agent_type(agent_id)
        if agent_type not in AGENT_TYPES:
            raise AgentIdentityValidationError(f"agent_type must be one of {', '.join(AGENT_TYPES)}")

        permission_profile = _optional_text(source.get("permission_profile")) or _default_permission_profile(agent_id)
        if permission_profile not in PERMISSION_PROFILES:
            raise AgentIdentityValidationError(
                f"permission_profile must be one of {', '.join(PERMISSION_PROFILES)}"
            )

        return cls(
            agent_id=agent_id,
            agent_type=agent_type,
            agent_run_id=_optional_text(source.get("agent_run_id")),
            task_id=_optional_text(source.get("task_id")),
            project_scope=tuple(_string_list(source.get("project_scope"))),
            permission_profile=permission_profile,
        )

    def to_record(self) -> JsonObject:
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "agent_run_id": self.agent_run_id,
            "task_id": self.task_id,
            "project_scope": list(self.project_scope),
            "permission_profile": self.permission_profile,
            "auth": self.auth,
            "project_scope_locked": self.project_scope_locked,
        }


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: str
    action: str
    permission_profile: str
    reasons: tuple[str, ...] = ()
    requested_domains: tuple[str, ...] = ()
    effective_domains: tuple[str, ...] = ()
    requested_sensitivity_allowed: tuple[str, ...] = DEFAULT_AGENT_SENSITIVITY
    effective_sensitivity_allowed: tuple[str, ...] = DEFAULT_AGENT_SENSITIVITY
    requested_project_scope: tuple[str, ...] = ()
    effective_project_scope: tuple[str, ...] = ()
    review_required: bool = False
    trace_id: str = ""
    workflow_type: str | None = None
    # Populated only when a deployment has configured a promotion persona.
    # Left None otherwise so an unconfigured deployment records byte-identical
    # policy payloads to the pre-promotion engine.
    promotion: PromotionDecision | None = None

    def to_record(self) -> JsonObject:
        record: JsonObject = {
            "decision": self.decision,
            "action": self.action,
            "permission_profile": self.permission_profile,
            "reasons": list(self.reasons),
            "requested_domains": list(self.requested_domains),
            "effective_domains": list(self.effective_domains),
            "requested_sensitivity_allowed": list(self.requested_sensitivity_allowed),
            "effective_sensitivity_allowed": list(self.effective_sensitivity_allowed),
            "requested_project_scope": list(self.requested_project_scope),
            "effective_project_scope": list(self.effective_project_scope),
            "review_required": self.review_required,
            "trace_id": self.trace_id,
            "workflow_type": self.workflow_type,
        }
        if self.promotion is not None:
            record["promotion"] = self.promotion.to_record()
        return record


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentIdentityValidationError("agent identity fields must be strings")
    normalized = " ".join(value.split()).strip()
    return normalized or None


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AgentIdentityValidationError("project_scope must be a list of strings")
    for item in value:
        if not isinstance(item, str):
            raise AgentIdentityValidationError("project_scope must be a list of strings")
        if normalize_project_identifier(item) == "":
            raise AgentIdentityValidationError("project_scope identifiers must not be blank")
    return list(normalize_project_scope(value))


def _default_agent_type(agent_id: str) -> str:
    if agent_id.casefold() == "hermes":
        return "personal_assistant"
    if agent_id.casefold() == "openclaw":
        return "coding_agent"
    return "unknown"


def _default_permission_profile(agent_id: str) -> str:
    if agent_id.casefold() == "hermes":
        return "trusted_local_agent"
    if agent_id.casefold() == "openclaw":
        return "project_scoped_agent"
    return "read_only_agent"


def _profile_sensitivity(profile: str) -> tuple[str, ...]:
    if profile == "read_only_agent":
        return ("public", "internal", "unknown")
    if profile == "admin_agent":
        return ALL_SENSITIVITY
    return DEFAULT_AGENT_SENSITIVITY


def permitted_domains(profile: str) -> tuple[str, ...] | None:
    """The domains a profile may read, or ``None`` when it may read every domain.

    ``None`` and an empty tuple are different answers and must stay different. ``None`` is an unrestricted
    profile: a request that names no domain reads all of them. A restricted profile gets the labels it may
    read, which is every label except ``RESTRICTED_DOMAINS`` and includes ``unknown`` (the stores return
    ``unknown`` rows under every domain filter, and the source fence tests the row's own domain the same way).
    An empty tuple would mean a profile that may read nothing, which the policy turns into a refusal, never
    into a search with no domain filter.
    """

    if profile in UNRESTRICTED_DOMAIN_PROFILES:
        return None
    return tuple(domain for domain in VNEXT_DOMAINS if domain not in RESTRICTED_DOMAINS)


def _filtered_domains(profile: str, domains: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    permitted = permitted_domains(profile)
    if permitted is None:
        return domains, ()
    if not domains:
        # A request that names no domain is not a request for every domain. Every reader treats an empty
        # ``effective_domains`` as "no domain filter", so the profile's own domains are written out here, and
        # the readers never see an empty tuple for a profile that has a held-back set.
        return permitted, ()
    blocked = tuple(domain for domain in domains if domain in RESTRICTED_DOMAINS)
    allowed = tuple(domain for domain in domains if domain not in RESTRICTED_DOMAINS)
    return allowed, blocked


def _filtered_sensitivity(profile: str, sensitivity_allowed: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    profile_allowed = set(_profile_sensitivity(profile))
    requested = sensitivity_allowed or _profile_sensitivity(profile)
    allowed = tuple(value for value in requested if value in profile_allowed)
    filtered = tuple(value for value in requested if value not in profile_allowed)
    return allowed or tuple(_profile_sensitivity(profile)), filtered


def resource_project_scope(resource: Mapping[str, object] | None) -> tuple[str, ...]:
    """Return the persisted project identifiers carried by one resource.

    Current rows use ``project_scope`` as the canonical, overlap-aware scope.
    Older rows may retain singular project fields or nested agent scope.  Once
    a canonical scope is present it is authoritative: stale compatibility
    metadata must never widen the resource into another project.
    """

    return resolve_project_scope(resource).values


def evaluate_agent_policy(
    *,
    identity: AgentIdentity | None,
    action: str,
    domains: tuple[str, ...] = (),
    sensitivity_allowed: tuple[str, ...] = DEFAULT_AGENT_SENSITIVITY,
    project_scope: tuple[str, ...] = (),
    project_floor: tuple[str, ...] = (),
    workflow_type: str | None = None,
    write_policy: str | None = None,
    require_explicit_project_scope: bool = False,
    promotion_settings: PromotionSettings | None = None,
    promotion_candidate: PromotionCandidate | None = None,
    owner_verified: bool = False,
) -> PolicyDecision:
    trace_id = f"policy-{uuid4()}"
    if identity is None:
        return PolicyDecision(
            decision="allowed",
            action=action,
            permission_profile="user_or_system",
            requested_domains=domains,
            effective_domains=domains,
            requested_sensitivity_allowed=sensitivity_allowed,
            effective_sensitivity_allowed=sensitivity_allowed,
            requested_project_scope=project_scope,
            effective_project_scope=project_scope,
            trace_id=trace_id,
            workflow_type=workflow_type,
        )

    profile = identity.permission_profile
    # Promotion is computed here, from the candidate, rather than accepted
    # ready-made: the hard floor must be recomputed by the engine on every
    # evaluation so no caller can hand in a decision that skips it.
    promotion: PromotionDecision | None = None
    if promotion_settings is not None:
        candidate = promotion_candidate or PromotionCandidate(
            domain=domains[0] if domains else "unknown",
            sensitivity=sensitivity_allowed[0] if sensitivity_allowed else "unknown",
        )
        # An agent is evaluating (the identity-None case returned above), so
        # the candidate is agent-written whatever the caller supplied, and how
        # far that agent is trusted comes from how its identity was
        # established rather than from anything in the payload.
        promotion = evaluate_promotion(
            settings=promotion_settings,
            candidate=replace(candidate, written_by_agent=True),
            permission_profile=profile,
            writer_trust=writer_trust_for(identity_auth=identity.auth, owner_verified=owner_verified),
        )
    promotion_permits_write = promotion is not None and promotion.auto_promote
    reasons: list[str] = []
    effective_domains, filtered_domains = _filtered_domains(profile, domains)
    effective_sensitivity, filtered_sensitivity = _filtered_sensitivity(profile, sensitivity_allowed)
    effective_project_scope = project_scope or identity.project_scope
    decision = "allowed"
    review_required = False

    if filtered_domains:
        reasons.append("restricted_domain_filtered")
        decision = "allowed_with_filtering"
    if filtered_sensitivity:
        reasons.append("restricted_sensitivity_filtered")
        decision = "allowed_with_filtering"
    if domains and not effective_domains and filtered_domains:
        reasons.append("all_requested_domains_restricted")
        decision = "blocked"
    elif not effective_domains and permitted_domains(profile) is not None:
        # Fail closed. Every reader reads an empty ``effective_domains`` as "no domain filter", so a restricted
        # profile must never leave here with one. No profile reaches this today (each has permitted domains);
        # a profile whose permitted set is empty would.
        reasons.append("no_permitted_domains")
        decision = "blocked"

    if profile == "read_only_agent" and action in WRITE_ACTIONS:
        reasons.append("read_only_agent_cannot_write")
        decision = "blocked"

    if (
        profile == "memory_proposal_agent"
        and action in WRITE_ACTIONS
        and action not in PROPOSAL_ONLY_WRITE_ACTIONS
    ):
        reasons.append("memory_proposal_agent_cannot_mutate")
        decision = "blocked"

    # Key-bound project scope is an authorization boundary, not merely a write
    # hint.  An omitted scope inherits the key binding; an explicit scope must
    # stay wholly inside it.  On a violation the effective scope is empty so a
    # caller cannot accidentally continue with the unauthorized request.
    if identity.project_scope_locked:
        bound_scope = set(project_scope_identity(identity.project_scope))
        out_of_scope = tuple(
            value
            for value in project_scope
            if not set(project_scope_identity((value,))).issubset(bound_scope)
        )
        if out_of_scope or (require_explicit_project_scope and not project_scope):
            reasons.append("project_scope_binding_violation")
            decision = "blocked"
            effective_project_scope = ()
        elif project_floor and not project_floor_within(project_floor, identity.project_scope):
            # The floor is part of the same binding test as the scope. A locked
            # key reads a derived row only when every project in the floor is
            # inside the binding. An empty floor does not add a refusal.
            reasons.append("project_floor_binding_violation")
            decision = "blocked"
            effective_project_scope = ()
        else:
            effective_project_scope = project_scope or identity.project_scope

    if action == "memory.propose":
        if profile not in {"project_scoped_agent", "trusted_local_agent", "memory_proposal_agent", "admin_agent"}:
            reasons.append("memory_proposal_permission_required")
            decision = "blocked"
        else:
            review_required = True
            if decision == "allowed":
                decision = "requires_review"

    if action in HUMAN_OR_ADMIN_ACTIONS and profile != "admin_agent":
        reasons.append("human_or_admin_review_required")
        decision = "blocked"

    if action == "http.operator.access":
        if profile not in {"trusted_local_agent", "admin_agent"}:
            reasons.append("trusted_or_admin_agent_required_for_operator_route")
            decision = "blocked"
        if identity.project_scope_locked or identity.project_scope:
            reasons.append("unbound_operator_key_required")
            decision = "blocked"

    if action == "artifact.export" and profile not in {"trusted_local_agent", "admin_agent"}:
        reasons.append("trusted_or_admin_agent_required_for_artifact_export")
        decision = "blocked"

    if (
        write_policy
        and write_policy != "proposal_only"
        and profile != "admin_agent"
        and not promotion_permits_write
    ):
        reasons.append("no_auto_promotion")
        decision = "blocked"

    if profile == "project_scoped_agent":
        requested_scope = project_scope or identity.project_scope
        if not requested_scope and action in {
            "context_pack.request",
            "artifact.generate",
            "memory.commit",
            "memory.propose",
            "scheduler.run_now",
        }:
            reasons.append("project_scope_required")
            decision = "blocked"
        if action == "scheduler.run_now" and workflow_type not in {
            "connection_report",
            "contradiction_report",
            "project_update_scan",
            "memory_consolidation",
        }:
            reasons.append("project_scoped_agent_cannot_run_global_workflow")
            decision = "blocked"
        if action == "scheduler.run_due":
            reasons.append("project_scoped_agent_cannot_trigger_global_scheduler")
            decision = "blocked"
        if action in {"scheduler.pause", "scheduler.resume", "scheduler.configure"}:
            reasons.append("project_scoped_agent_cannot_control_global_scheduler")
            decision = "blocked"

    if action == "scheduler.configure" and profile != "admin_agent":
        reasons.append("admin_agent_required_for_scheduler_configuration")
        decision = "blocked"
    if action in {"scheduler.pause", "scheduler.resume"} and profile not in {"trusted_local_agent", "admin_agent"}:
        reasons.append("trusted_or_admin_agent_required_for_scheduler_control")
        decision = "blocked"
    if action == "scheduler.run_now" and profile not in {"project_scoped_agent", "trusted_local_agent", "admin_agent"}:
        reasons.append("trusted_agent_required_for_scheduler_run")
        decision = "blocked"
    if action == "scheduler.run_due" and profile not in {"trusted_local_agent", "admin_agent"}:
        reasons.append("trusted_or_admin_agent_required_for_scheduler_due_run")
        decision = "blocked"

    # Promotion may only upgrade an outcome the engine already permitted and
    # merely gated on review. "blocked" and "allowed_with_filtering" are
    # untouchable here: a persona setting must never widen an authorization
    # boundary, only remove the human gate in front of one.
    if promotion_permits_write and decision == "requires_review":
        reasons.append("promotion_auto_promoted")
        decision = "allowed"
        review_required = False

    return PolicyDecision(
        decision=decision,
        action=action,
        permission_profile=profile,
        reasons=tuple(dict.fromkeys(reasons)),
        requested_domains=domains,
        effective_domains=effective_domains,
        requested_sensitivity_allowed=sensitivity_allowed,
        effective_sensitivity_allowed=effective_sensitivity,
        requested_project_scope=project_scope,
        effective_project_scope=effective_project_scope,
        review_required=review_required or decision == "requires_review",
        trace_id=trace_id,
        workflow_type=workflow_type,
        promotion=promotion,
    )


def _event_payload(event: Mapping[str, object]) -> Mapping[str, object]:
    payload = event.get("payload_json")
    if isinstance(payload, Mapping):
        return payload
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        return payload
    return {}


def _policy_decision_from_event(event: Mapping[str, object]) -> Mapping[str, object] | None:
    payload = _event_payload(event)
    decision = payload.get("policy_decision")
    return decision if isinstance(decision, Mapping) else None


def _agent_id_for_event(event: Mapping[str, object]) -> str:
    actor_id = event.get("actor_id")
    if isinstance(actor_id, str) and actor_id:
        return actor_id
    payload = _event_payload(event)
    identity = payload.get("agent_identity")
    if isinstance(identity, Mapping) and isinstance(identity.get("agent_id"), str):
        return str(identity["agent_id"])
    return "unknown"


def _counter_rows(counter: Counter[str], *, key_name: str) -> list[JsonObject]:
    return [
        {key_name: key, "count": count}
        for key, count in counter.most_common()
    ]


def _nested_counter_rows(counter: Mapping[str, Counter[str]], *, key_name: str, nested_key: str) -> list[JsonObject]:
    rows: list[JsonObject] = []
    for key in sorted(counter):
        nested = counter[key]
        rows.append(
            {
                key_name: key,
                "count": sum(nested.values()),
                nested_key: dict(nested.most_common()),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            -(count if isinstance((count := row.get("count")), int) else 0),
            str(row[key_name]),
        ),
    )


def summarize_agent_policy_telemetry(
    *,
    agent_events: Iterable[Mapping[str, object]],
    artifacts: Iterable[Mapping[str, object]] = (),
    memories: Iterable[Mapping[str, object]] = (),
) -> JsonObject:
    """Build a compact operator summary from append-only agent events."""

    blocks_by_agent: defaultdict[str, Counter[str]] = defaultdict(Counter)
    filters_by_agent: defaultdict[str, Counter[str]] = defaultdict(Counter)
    review_by_agent: defaultdict[str, Counter[str]] = defaultdict(Counter)
    restricted_domains: Counter[str] = Counter()
    workflows: Counter[str] = Counter()
    workflow_agents: defaultdict[str, Counter[str]] = defaultdict(Counter)
    memory_proposals: Counter[str] = Counter()
    artifact_generation: Counter[str] = Counter()
    total_policy_decisions = 0
    total_agent_events = 0
    seen_policy_traces: set[str] = set()
    memory_proposal_ids: set[str] = set()
    artifact_generation_ids: set[str] = set()

    for event in agent_events:
        total_agent_events += 1
        agent_id = _agent_id_for_event(event)
        event_type = str(event.get("event_type") or "")
        payload = _event_payload(event)
        decision = _policy_decision_from_event(event)
        if decision is not None:
            trace_id = str(decision.get("trace_id") or event.get("trace_id") or "")
            if trace_id and trace_id in seen_policy_traces:
                decision = None
            elif trace_id:
                seen_policy_traces.add(trace_id)
        if decision is not None:
            total_policy_decisions += 1
            action = str(decision.get("action") or "unknown")
            decision_value = str(decision.get("decision") or "")
            if decision_value == "blocked":
                blocks_by_agent[agent_id][action] += 1
            if decision_value == "allowed_with_filtering":
                filters_by_agent[agent_id][action] += 1
            if decision.get("review_required") is True or decision_value == "requires_review":
                review_by_agent[agent_id][action] += 1
            requested_domains = decision.get("requested_domains")
            effective_domains_value = decision.get("effective_domains")
            effective_domains = (
                {domain for domain in effective_domains_value if isinstance(domain, str)}
                if isinstance(effective_domains_value, list)
                else set()
            )
            if isinstance(requested_domains, list):
                for domain in requested_domains:
                    if isinstance(domain, str) and domain not in effective_domains:
                        restricted_domains[domain] += 1
            else:
                reasons = decision.get("reasons")
                restricted_domain_filtered = (
                    any(reason == "restricted_domain_filtered" for reason in reasons)
                    if isinstance(reasons, list)
                    else False
                )
                if restricted_domain_filtered:
                    restricted_domains["restricted_domain_filtered"] += 1
            workflow_type = decision.get("workflow_type")
            if action == "scheduler.run_now" and isinstance(workflow_type, str) and workflow_type:
                workflows[workflow_type] += 1
                workflow_agents[workflow_type][agent_id] += 1

        if event_type == "agent.memory_proposed":
            if isinstance(event.get("target_id"), str):
                memory_proposal_ids.add(str(event["target_id"]))
            memory_proposals[agent_id] += 1
        if event_type in {"agent.artifact_generated", "artifact.generated"}:
            if isinstance(event.get("target_id"), str):
                artifact_generation_ids.add(str(event["target_id"]))
            artifact_generation[agent_id] += 1
        if event_type.startswith("scheduler.") and agent_id != "unknown":
            workflow_type = payload.get("workflow_type")
            if isinstance(workflow_type, str) and workflow_type:
                workflows[workflow_type] += 1
                workflow_agents[workflow_type][agent_id] += 1

    for memory in memories:
        metadata = memory.get("metadata_json")
        memory_id = str(memory.get("id")) if memory.get("id") is not None else ""
        if (
            isinstance(metadata, Mapping)
            and isinstance(metadata.get("agent_id"), str)
            and memory_id not in memory_proposal_ids
        ):
            memory_proposals[str(metadata["agent_id"])] += 1

    for artifact in artifacts:
        metadata = artifact.get("metadata_json")
        artifact_id = str(artifact.get("id")) if artifact.get("id") is not None else ""
        if isinstance(metadata, Mapping) and metadata.get("generated_by") == "agent" and artifact_id not in artifact_generation_ids:
            metadata_agent_id = metadata.get("agent_id")
            artifact_generation[
                str(metadata_agent_id) if isinstance(metadata_agent_id, str) and metadata_agent_id else "unknown"
            ] += 1

    return {
        "total_agent_events": total_agent_events,
        "total_policy_decisions": total_policy_decisions,
        "policy_blocks_by_agent": _nested_counter_rows(blocks_by_agent, key_name="agent_id", nested_key="actions"),
        "policy_filters_by_agent": _nested_counter_rows(filters_by_agent, key_name="agent_id", nested_key="actions"),
        "requires_review_by_agent": _nested_counter_rows(review_by_agent, key_name="agent_id", nested_key="actions"),
        "restricted_domains_requested": _counter_rows(restricted_domains, key_name="domain"),
        "workflows_triggered_by_agents": [
            {
                "workflow_type": workflow_type,
                "count": count,
                "agents": dict(workflow_agents[workflow_type].most_common()),
            }
            for workflow_type, count in workflows.most_common()
        ],
        "memory_proposals_by_agent": _counter_rows(memory_proposals, key_name="agent_id"),
        "artifact_generation_by_agent": _counter_rows(artifact_generation, key_name="agent_id"),
    }


def ensure_policy_allowed(decision: PolicyDecision) -> None:
    if decision.decision == "blocked":
        raise AgentPolicyBlockedError(decision)


def append_policy_events(
    store,
    *,
    identity: AgentIdentity | None,
    decision: PolicyDecision,
    target_type: str | None = None,
    target_id: str | None = None,
) -> None:
    if identity is None:
        return
    payload: JsonObject = {
        "agent_identity": identity.to_record(),
        "policy_decision": decision.to_record(),
    }
    append_event(
        store,
        event_type="policy.decision",
        actor_type=identity.actor_type,
        actor_id=identity.agent_id,
        target_type=target_type,
        target_id=target_id,
        trace_id=decision.trace_id,
        run_id=identity.agent_run_id,
        payload=payload,
    )
    if decision.decision == "blocked":
        append_event(
            store,
            event_type="agent.policy_blocked",
            actor_type=identity.actor_type,
            actor_id=identity.agent_id,
            target_type=target_type,
            target_id=target_id,
            trace_id=decision.trace_id,
            run_id=identity.agent_run_id,
            payload=payload,
        )
    elif decision.decision == "allowed_with_filtering":
        append_event(
            store,
            event_type="agent.policy_filtered",
            actor_type=identity.actor_type,
            actor_id=identity.agent_id,
            target_type=target_type,
            target_id=target_id,
            trace_id=decision.trace_id,
            run_id=identity.agent_run_id,
            payload=payload,
        )


def append_promotion_event(
    store,
    *,
    identity: AgentIdentity | None,
    decision: PolicyDecision,
    target_type: str,
    target_id: str,
    trace_id: str | None = None,
) -> bool:
    """Record a proposal that promotion wrote directly instead of gating.

    Returns whether an event was appended, which is True only when a
    configured persona actually changed the outcome. An unconfigured
    deployment carries no promotion record, so nothing is written and the
    event stream is unchanged.
    """

    promotion = decision.promotion
    if promotion is None or not promotion.auto_promote or decision.review_required:
        return False
    append_event(
        store,
        event_type="memory.auto_promoted",
        actor_type=identity.actor_type if identity is not None else "user",
        actor_id=identity.agent_id if identity is not None else None,
        target_type=target_type,
        target_id=target_id,
        trace_id=trace_id or decision.trace_id,
        run_id=identity.agent_run_id if identity is not None else None,
        payload={
            "promotion": promotion.to_record(),
            "promoted_from": "requires_review",
            "gated_status_without_promotion": "review_required",
            "action": decision.action,
            "reversible_via": "memory.undo",
        },
    )
    if promotion.review_is_advisory:
        append_event(
            store,
            event_type="review.item_created",
            actor_type=identity.actor_type if identity is not None else "user",
            actor_id=identity.agent_id if identity is not None else None,
            target_type=target_type,
            target_id=target_id,
            trace_id=trace_id or decision.trace_id,
            run_id=identity.agent_run_id if identity is not None else None,
            payload={
                "review_required": False,
                "review_mode": "digest",
                "proposal_type": "auto_promoted_memory",
                "persona": promotion.persona,
            },
        )
    return True


def agent_metadata(identity: AgentIdentity | None, decision: PolicyDecision | None = None) -> JsonObject:
    metadata: JsonObject = {}
    if identity is not None:
        metadata["generated_by"] = "agent"
        metadata["agent_id"] = identity.agent_id
        metadata["agent_run_id"] = identity.agent_run_id
        metadata["agent_identity"] = identity.to_record()
    if decision is not None:
        metadata["policy_decision"] = decision.to_record()
        metadata["review_required"] = decision.review_required
    return metadata


__all__ = [
    "AGENT_AUTH_METHODS",
    "AGENT_TYPES",
    "HUMAN_OR_ADMIN_ACTIONS",
    "PERMISSION_PROFILES",
    "POLICY_DECISIONS",
    "READ_ACTIONS",
    "WRITE_ACTIONS",
    "AgentIdentity",
    "AgentIdentityValidationError",
    "AgentPolicyBlockedError",
    "PolicyDecision",
    "PromotionCandidate",
    "PromotionDecision",
    "PromotionSettings",
    "agent_metadata",
    "append_policy_events",
    "append_promotion_event",
    "ensure_policy_allowed",
    "evaluate_agent_policy",
    "resource_project_scope",
    "summarize_agent_policy_telemetry",
]
