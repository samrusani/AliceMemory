"""One memory.propose write, shared by the HTTP, MCP and CLI doors.

Why it exists (S4.4 round 2, owner ruling C2(ii), 2026-09-23). The three
propose handlers (POST /v0/vnext/memory-proposals, MCP
alice_vnext_propose_memory, CLI ``vnext agents propose-memory``) each wrote
the row themselves. None of them consulted the credential floor, and they
disagreed on what they kept: the HTTP door stored the rationale and
source_refs, the CLI stored the rationale, MCP stored neither. Under an
auto-promote persona a proposal lands active, so the promotion floor was the
only credential control on these doors, and addendum F1 makes it a
precondition that it never is.

Every door now builds a MemoryProposal and calls propose_memory. It refuses
credential material over every field the write persists before anything is
written (the policy audit rows included), then asks the door to authorize,
then writes one row shape: the memory, its creation revision, the proposal
event, the review item when review is required, and the promotion event.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from alicebot_api.credential_floor import refuse_credential_material, stored_text_fields
from alicebot_api.vnext_agent_control import (
    AgentIdentity,
    PolicyDecision,
    agent_metadata,
    append_promotion_event,
)
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_promotion_policy import PromotionCandidate, promotion_candidate_for_proposal
from alicebot_api.vnext_repositories import JsonObject


class ProposalStore(Protocol):
    """What a proposal write needs: both vNext stores provide it."""

    def create_memory(self, memory: JsonObject, *, actor_type: str = ...) -> JsonObject: ...

    def append_revision(self, revision: JsonObject, *, actor_type: str = ...) -> JsonObject: ...

    def append_event(self, event: JsonObject) -> JsonObject: ...


class MemoryProposalRefused(ValueError):
    """The proposal carries credential material; nothing was written."""


@dataclass(frozen=True, slots=True)
class MemoryProposal:
    """What a door read from its caller, before policy decides the status."""

    proposal_type: str
    title: str
    canonical_text: str
    memory_type: str
    domain: str = "unknown"
    sensitivity: str = "unknown"
    confidence: float | None = 0.5
    rationale: str | None = None
    source_refs: tuple[object, ...] = ()
    project_scope: tuple[str, ...] = ()
    source_type: str = "trusted_agent"
    contradiction_refs: tuple[str, ...] = ()
    conversation_excerpt: str | None = None
    proposal_id: str | None = None
    trace_id: str | None = None

    @property
    def summary(self) -> str:
        return self.canonical_text[:280]

    def promotion_candidate(self) -> PromotionCandidate:
        return promotion_candidate_for_proposal(
            canonical_text=self.canonical_text,
            title=self.title,
            memory_type=self.memory_type,
            domain=self.domain,
            sensitivity=self.sensitivity,
            source_type=self.source_type,
            source_refs=self.source_refs,
            contradiction_refs=self.contradiction_refs,
            conversation_excerpt=self.conversation_excerpt,
            rationale=self.rationale,
            project_scope=self.project_scope,
        )


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    decision: PolicyDecision
    identity: AgentIdentity | None
    # None when policy blocked the proposal: nothing but the audit was written.
    memory: JsonObject | None


# The door's authorization: given the promotion candidate, resolve the agent
# identity and evaluate memory.propose, writing the policy audit as it does.
Authorize = Callable[[PromotionCandidate], tuple[AgentIdentity | None, PolicyDecision]]


def refuse_proposal_credentials(proposal: MemoryProposal) -> None:
    """The credential floor over every field a proposal persists, as stored.

    Reading order: the title (left out when it is a prefix of the text), the
    text, then the rationale, the source refs, the project scope and the
    identifiers the row and its events keep. The summary is a prefix of the
    text and is left out as a derived copy.
    """

    refuse_credential_material(
        *stored_text_fields(proposal.title, proposal.canonical_text, proposal.summary),
        proposal.rationale,
        list(proposal.source_refs),
        list(proposal.project_scope),
        proposal.proposal_type,
        proposal.proposal_id,
        proposal.trace_id,
        error=MemoryProposalRefused,
    )


def propose_memory(store: ProposalStore, *, proposal: MemoryProposal, authorize: Authorize) -> ProposalOutcome:
    """Gate, authorize and write one memory proposal."""

    refuse_proposal_credentials(proposal)
    identity, decision = authorize(proposal.promotion_candidate())
    if decision.decision == "blocked":
        return ProposalOutcome(decision=decision, identity=identity, memory=None)
    if identity is None:
        raise MemoryProposalRefused("agent identity is required for memory proposals")

    # A promoted proposal is a live memory, not a review item. With no
    # persona configured review_required stays True and this is the
    # candidate row it always was.
    review_required = decision.review_required
    proposal_id = proposal.proposal_id or str(uuid4())
    source_refs = list(proposal.source_refs)
    effective_scope = list(decision.effective_project_scope)
    metadata: JsonObject = {
        "proposal_id": proposal_id,
        "proposal_type": proposal.proposal_type,
        "source_refs": source_refs,
        "project_scope": effective_scope,
        "rationale": proposal.rationale,
        "review_required": review_required,
        **agent_metadata(identity, decision),
    }
    trace_id = proposal.trace_id or decision.trace_id
    memory: JsonObject = store.create_memory(
        {
            "memory_type": proposal.memory_type,
            "memory_key": f"agent_proposal.{proposal.proposal_type}.{proposal_id}",
            "value": {
                "proposal_type": proposal.proposal_type,
                "text": proposal.canonical_text,
                "source_refs": source_refs,
                "rationale": proposal.rationale,
            },
            "status": "candidate" if review_required else "active",
            "project_id": effective_scope[0] if len(effective_scope) == 1 else None,
            "confidence": proposal.confidence,
            "title": proposal.title,
            "canonical_text": proposal.canonical_text,
            "summary": proposal.summary,
            "domain": proposal.domain,
            "sensitivity": proposal.sensitivity,
            "metadata_json": metadata,
        },
        actor_type="agent",
    )
    store.append_revision(
        {
            "memory_id": str(memory["id"]),
            "memory_key": str(memory["memory_key"]),
            "new_value": memory.get("value"),
            "revision_type": "created",
            "action": "agent_memory_proposal",
            "text_after": proposal.canonical_text,
            "reason": proposal.rationale or "Agent proposed memory for human review.",
            "actor_type": "agent",
            "actor_id": identity.agent_id,
            "metadata_json": metadata,
        },
        actor_type="agent",
    )
    append_event(
        store,
        event_type="agent.memory_proposed",
        actor_type="agent",
        actor_id=identity.agent_id,
        target_type="memory",
        target_id=str(memory["id"]),
        trace_id=trace_id,
        run_id=identity.agent_run_id,
        payload={
            "proposal_type": proposal.proposal_type,
            "agent_identity": identity.to_record(),
            "policy_decision": decision.to_record(),
        },
    )
    if review_required:
        append_event(
            store,
            event_type="review.item_created",
            actor_type="agent",
            actor_id=identity.agent_id,
            target_type="memory",
            target_id=str(memory["id"]),
            trace_id=trace_id,
            run_id=identity.agent_run_id,
            payload={"review_required": True, "proposal_type": proposal.proposal_type},
        )
    append_promotion_event(
        store,
        identity=identity,
        decision=decision,
        target_type="memory",
        target_id=str(memory["id"]),
        trace_id=trace_id,
    )
    return ProposalOutcome(decision=decision, identity=identity, memory=memory)


__all__ = [
    "Authorize",
    "MemoryProposal",
    "MemoryProposalRefused",
    "ProposalOutcome",
    "ProposalStore",
    "propose_memory",
    "refuse_proposal_credentials",
]
