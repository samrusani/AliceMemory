"""The single source of truth for legal memory lifecycle transitions.

Every lifecycle mutation -- inline confirmation, dashboard review,
correction, undo, forget, expiration, unexpiration, and consolidation
supersession -- routes through :func:`resolve_transition` here, on every
backend and interface (HTTP, MCP, SQLite review path). The table rejects
transitions that were previously reachable and corrupt the store:

* confirming or rejecting a row that a review already retired
  (``rejected`` / ``superseded``), which either resurrected an impossible
  state or produced two active contradictory memories;
* correcting / undoing / forgetting an already-retired row (retirement is
  terminal and must not be reversible);
* unexpiring a ``stale`` row without restoring it to a retrievable state.

Supersession cycles (A -> B -> A) cannot be expressed as a status rule --
they are a property of the pointer graph -- so :func:`supersession_would_cycle`
guards those separately.

This module deliberately imports nothing from ``vnext_memory_commit`` so it
can be shared by the commit service, the MCP review path, and the HTTP
surface without an import cycle.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass


class LifecycleTransitionError(ValueError):
    """Raised when a lifecycle mutation is not legal from the current status."""


def default_memory_decision_reason(revision_action: str) -> str:
    """The stored reason a decision uses when the caller supplies none."""
    for prefix, description in (
        ("memory_correct_", "alice_memory_correct action: "),
        ("memory_review_", "vNext workspace memory review action: "),
        ("agentic_memory_confirm_", "Inline memory confirmation "),
    ):
        if revision_action.startswith(prefix):
            suffix = revision_action.removeprefix(prefix)
            return description + suffix + ("." if prefix == "agentic_memory_confirm_" else "")
    return {
        "agentic_memory_forget": "Agentic memory forgotten.",
        "agentic_memory_undo": "Agentic memory commit undone.",
        "agentic_memory_confirmation_expired": "Inline memory confirmation expired before it was acted on.",
    }.get(revision_action, "")


# The full memory row-status vocabulary, partitioned into the terminal
# (retired) states and the live states a memory can still be mutated from.
# Kept in lockstep with ``vnext_memory_commit.MEMORY_STATUSES`` (asserted at
# import time there).
RETIRED_STATUSES: frozenset[str] = frozenset({"superseded", "rejected", "archived"})
LIVE_STATUSES: frozenset[str] = frozenset(
    {"candidate", "active", "accepted", "needs_review", "private_only", "stale"}
)
ALL_STATUSES: frozenset[str] = RETIRED_STATUSES | LIVE_STATUSES

# A supersession successor is the new head of a memory chain. It must already
# be an accepted/live value, not a pending review candidate or a stale row that
# default retrieval excludes. ``private_only`` remains a valid head: its
# exclusion is an intentional visibility policy, not a pending/retired state.
SUPERSESSION_SUCCESSOR_STATUSES: frozenset[str] = frozenset(
    {"active", "accepted", "private_only"}
)


# -- lifecycle operations ------------------------------------------------------
#
# One constant per lifecycle verb. Callers pass these to resolve_transition;
# the string values also appear in audit metadata.
CONFIRM_ACCEPT = "confirm.accept"
CONFIRM_REJECT = "confirm.reject"
CONFIRM_EXPIRE = "confirm.expire"
REVIEW_APPROVE = "review.approve"
REVIEW_REJECT = "review.reject"
REVIEW_SUPERSEDE = "review.supersede"
CORRECT = "correct"
UNDO = "undo"
FORGET = "forget"
SUPERSEDE_MEMBER = "supersede_member"
EXPIRE = "expire"
UNEXPIRE = "unexpire"


@dataclass(frozen=True)
class _LifecycleOperation:
    """One row of the transition table: legal ``from -> to`` status moves."""

    name: str
    transitions: Mapping[str, str]
    reject_message: str  # formatted with {status}


def _to(target: str, sources: frozenset[str]) -> dict[str, str]:
    """Every ``source`` status transitions to the same ``target``."""
    return {source: target for source in sources}


def _identity(sources: frozenset[str]) -> dict[str, str]:
    """Every ``source`` status is preserved (validity-only operations)."""
    return {source: source for source in sources}


# The transition table. For each operation, the mapping's KEYS are exactly the
# statuses the operation is legal from; a status absent from the mapping is
# rejected. The VALUE is the resulting row status (equal to the key for
# validity-only operations that leave status untouched).
_OPERATIONS: dict[str, _LifecycleOperation] = {
    # Inline confirmation only acts on a row still awaiting confirmation.
    CONFIRM_ACCEPT: _LifecycleOperation(
        CONFIRM_ACCEPT,
        {"needs_review": "active"},
        "cannot confirm a memory in status '{status}'; its pending confirmation is no longer valid",
    ),
    CONFIRM_REJECT: _LifecycleOperation(
        CONFIRM_REJECT,
        {"needs_review": "rejected"},
        "cannot reject a confirmation for a memory in status '{status}'; it is no longer pending",
    ),
    CONFIRM_EXPIRE: _LifecycleOperation(
        CONFIRM_EXPIRE,
        {"needs_review": "rejected"},
        "cannot expire a confirmation for a memory in status '{status}'; it is no longer pending",
    ),
    # Dashboard review can approve/reject/supersede any live row, never a
    # retired one.
    REVIEW_APPROVE: _LifecycleOperation(
        REVIEW_APPROVE,
        _to("active", LIVE_STATUSES),
        "memory cannot be reviewed from status '{status}'",
    ),
    REVIEW_REJECT: _LifecycleOperation(
        REVIEW_REJECT,
        _to("rejected", LIVE_STATUSES),
        "memory cannot be reviewed from status '{status}'",
    ),
    REVIEW_SUPERSEDE: _LifecycleOperation(
        REVIEW_SUPERSEDE,
        _to("superseded", LIVE_STATUSES),
        "memory cannot be reviewed from status '{status}'",
    ),
    # A correction promotes to active (and the caller must also mark it
    # confirmed / clear review_required); retirement is terminal.
    CORRECT: _LifecycleOperation(
        CORRECT,
        _to("active", LIVE_STATUSES),
        "cannot correct a retired {status} memory; create a replacement instead",
    ),
    UNDO: _LifecycleOperation(
        UNDO,
        _to("superseded", LIVE_STATUSES),
        "cannot undo a {status} memory; it is already retired",
    ),
    FORGET: _LifecycleOperation(
        FORGET,
        _to("superseded", LIVE_STATUSES),
        "cannot forget a {status} memory; it is already retired",
    ),
    SUPERSEDE_MEMBER: _LifecycleOperation(
        SUPERSEDE_MEMBER,
        _to("superseded", LIVE_STATUSES),
        "cannot supersede a {status} memory; it is already retired",
    ),
    # Expiration is a validity-window operation: the status is unchanged, but
    # a retired row has no window to close.
    EXPIRE: _LifecycleOperation(
        EXPIRE,
        _identity(LIVE_STATUSES),
        "cannot expire a {status} memory",
    ),
    # Unexpiration reopens the validity window. A row swept ``stale`` by an
    # expired window becomes retrievable ``active`` again so the reported
    # status matches reality; every other live status is preserved.
    UNEXPIRE: _LifecycleOperation(
        UNEXPIRE,
        {**_identity(LIVE_STATUSES), "stale": "active"},
        "cannot unexpire a {status} memory",
    ),
}


def resolve_transition(operation: str, current_status: str) -> str:
    """Return the row status ``operation`` yields from ``current_status``.

    Raises :class:`LifecycleTransitionError` when the operation is not legal
    from ``current_status``. The returned status equals ``current_status`` for
    validity-only operations (expire, and unexpire of a non-stale row).
    """
    try:
        op = _OPERATIONS[operation]
    except KeyError as exc:  # pragma: no cover - guards programmer error
        raise LifecycleTransitionError(f"unknown lifecycle operation '{operation}'") from exc
    normalized = current_status or ""
    if normalized not in op.transitions:
        raise LifecycleTransitionError(op.reject_message.format(status=normalized or "unknown"))
    return op.transitions[normalized]


def supersession_would_cycle(
    *,
    memory_id: str,
    successor: Mapping[str, object],
    load_memory: Callable[[str], Mapping[str, object] | None],
    read_pointer: Callable[[Mapping[str, object] | None, str], str | None],
    max_depth: int = 64,
) -> bool:
    """Would recording ``memory_id`` superseded-by ``successor`` form a cycle?

    Recording the edge ``memory_id -> successor`` (``memory_id.superseded_by =
    successor``) closes a cycle iff ``memory_id`` is already reachable from
    ``successor`` by following ``superseded_by`` pointers -- i.e. the proposed
    successor is itself a descendant whose supersession chain leads back to the
    row being retired. Walking that chain (bounded, cycle-safe) detects the
    ``A -> B -> A`` case the flat status table cannot.

    Raises :class:`LifecycleTransitionError` when the chain does not terminate
    within ``max_depth`` hops: the walk cannot confirm acyclicity, so the
    supersession must be rejected (fail CLOSED) rather than allowed.
    """
    target = str(memory_id)
    current: Mapping[str, object] | None = successor
    seen: set[str] = set()
    depth = 0
    while current is not None:
        if depth >= max_depth:
            # Depth exhausted before the chain terminated: acyclicity is
            # unverifiable, so fail closed instead of allowing a possible cycle.
            raise LifecycleTransitionError(
                f"supersession chain exceeds {max_depth} hops; cannot verify "
                "acyclicity, rejecting the supersession to fail closed"
            )
        current_id_raw = current.get("id")
        if current_id_raw is None:
            raise LifecycleTransitionError(
                "supersession chain contains a row without an id; cannot verify "
                "acyclicity, rejecting the supersession to fail closed"
            )
        current_id = str(current_id_raw)
        if current_id == target:
            return True
        if current_id in seen:
            raise LifecycleTransitionError(
                f"supersession chain already contains a cycle at memory {current_id}; "
                "rejecting the supersession to fail closed"
            )
        seen.add(current_id)
        next_id = read_pointer(current, "superseded_by")
        if not next_id:
            break
        current = load_memory(next_id)
        if current is None:
            raise LifecycleTransitionError(
                f"supersession chain points to missing memory {next_id}; cannot verify "
                "acyclicity, rejecting the supersession to fail closed"
            )
        depth += 1
    return False


__all__ = [
    "CONFIRM_ACCEPT",
    "CONFIRM_EXPIRE",
    "CONFIRM_REJECT",
    "CORRECT",
    "EXPIRE",
    "FORGET",
    "LIVE_STATUSES",
    "LifecycleTransitionError",
    "REVIEW_APPROVE",
    "REVIEW_REJECT",
    "REVIEW_SUPERSEDE",
    "RETIRED_STATUSES",
    "SUPERSESSION_SUCCESSOR_STATUSES",
    "SUPERSEDE_MEMBER",
    "UNDO",
    "UNEXPIRE",
    "resolve_transition",
    "supersession_would_cycle",
]
