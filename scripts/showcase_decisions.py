"""Record the planted Keel price decisions through the commit door.

The search benchmark does not call this module. The demo that shows recorded
decisions does. Each write goes through ``VNextMemoryCommitService``, including
the confirmation a financial decision requires and the undo that links the
older price to the newer one.
"""

from __future__ import annotations

from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_memory_commit import MemoryCommitRequest, VNextMemoryCommitService


def record_planted_prices(store, identity: AgentIdentity) -> tuple[str, str]:
    """Store the 480 price and the 520 price, and link the first to the second.

    Returns the two memory ids, older then newer.
    """

    service = VNextMemoryCommitService(store)
    older = _commit(service, store, identity, "the list price was 480 ledger units")
    newer = _commit(service, store, identity, "the list price was cut to 520 ledger units")
    service.undo(
        identity=identity,
        memory_id=older,
        superseded_by_memory_id=newer,
        reason="the June price was too high for the renewal",
    )
    return older, newer


def _commit(service: VNextMemoryCommitService, store, identity: AgentIdentity, text: str) -> str:
    result = service.commit(
        identity=identity,
        request=MemoryCommitRequest(
            user_id=str(store.user_id),
            title="Keel sensor kit price",
            canonical_text=text,
            memory_type="decision",
            domain="financial",
            sensitivity="private",
            confidence=0.95,
            rationale="the June price was too high for the renewal",
        ),
    )
    if result["status"] == "confirmation_required":
        result = service.confirm(
            identity=identity,
            confirmation_id=str(result["confirmation_id"]),
            action="confirm",
            rationale="the June price was too high for the renewal",
        )
    memory = result["memory"]
    assert isinstance(memory, dict)
    return str(memory["id"])
