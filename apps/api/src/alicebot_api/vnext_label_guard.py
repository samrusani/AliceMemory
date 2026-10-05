"""Read-time check for a derived row.

One guard per request. An inactive guard returns its input and reads nothing.
An active guard settles the row with its inputs and answers the door with that
effective label.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, TypeVar

from alicebot_api.vnext_agent_control import (
    ALL_SENSITIVITY,
    VNEXT_DOMAINS,
    AgentIdentity,
    PolicyDecision,
)
from alicebot_api.vnext_derived_labels import (
    HOP_BOUND,
    NODE_BOUND,
    canon_kind,
    is_derived,
    input_admitted,
    settle_labels,
)
from alicebot_api.vnext_label_closure import collect_label_rows
from alicebot_api.vnext_project_scope import project_floor_shape, project_scopes_overlap, resolve_project_scope


_GUARD_USER = "label-guard"
_Row = TypeVar("_Row", bound=Mapping[str, object])


def _filters_admit_every(
    domains: Sequence[str] | None,
    sensitivity_allowed: Sequence[str] | None,
    projects: Sequence[str] | None,
) -> bool:
    domain_list = tuple(domains or ())
    domains_open = not domain_list or set(domain_list) >= set(VNEXT_DOMAINS)
    sensitivity_list = tuple(sensitivity_allowed or ())
    sensitivity_open = set(sensitivity_list) >= set(ALL_SENSITIVITY)
    return domains_open and sensitivity_open and not tuple(projects or ())


@dataclass
class LabelGuard:
    """The labels a door may trust for one request."""

    store: Any
    active: bool
    domains: tuple[str, ...] = ()
    sensitivity_allowed: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    all_of: tuple[str, ...] | None = None
    _nodes: dict[tuple[str, str], list[dict[str, object]]] | None = None

    @classmethod
    def for_fence(cls, store: Any, fence: Any) -> LabelGuard:
        """Exact doors. Inactive for the owner and for an unbound admin."""

        fenced = bool(getattr(fence, "entity_read_fenced", False))
        return cls(store=store, active=fenced)

    @classmethod
    def for_filters(
        cls,
        store: Any,
        domains: Sequence[str] | None,
        sensitivity_allowed: Sequence[str] | None,
        projects: Sequence[str] | None = (),
        exclude_global_domains: Sequence[str] | None = None,
        *,
        all_of: tuple[str, ...] | None = None,
    ) -> LabelGuard:
        """List doors. Inactive when the filters admit every label."""

        del exclude_global_domains
        domain_list = tuple(domains or ())
        sensitivity_list = tuple(sensitivity_allowed or ())
        project_list = tuple(projects or ())
        return cls(
            store=store,
            active=all_of is not None or not _filters_admit_every(domain_list, sensitivity_list, project_list),
            domains=domain_list,
            sensitivity_allowed=sensitivity_list,
            projects=project_list,
            all_of=all_of,
        )

    def effective_row(self, kind: str, row: Mapping[str, object] | None) -> Mapping[str, object] | None:
        """A copy whose domain, sensitivity, scope and floor are effective."""

        if row is None or not self.active or not isinstance(row, Mapping):
            return row
        if not is_derived(kind, row):
            return row
        nodes = self._collected(kind, row)
        settled = settle_labels(nodes, on_cycle="unverified", max_hops=HOP_BOUND, max_nodes=NODE_BOUND)
        label = settled.by_stored(kind, str(row.get("id") or ""), user_id=_GUARD_USER)
        copy = dict(row)
        raw_metadata = copy.get("metadata_json")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
        if label.unverified:
            copy["domain"] = label.domain
            copy["sensitivity"] = "regulated"
            metadata["project_scope"] = []
            metadata["project_floor"] = []
            copy["unverified"] = True
        else:
            copy["domain"] = label.domain
            copy["sensitivity"] = label.sensitivity
            metadata["project_scope"] = list(label.project_scope)
            metadata["project_floor"] = list(label.project_floor)
            copy["unverified"] = False
        copy["metadata_json"] = metadata
        return copy

    def admit_rows(self, kind: str, rows: Sequence[_Row]) -> list[_Row]:
        """Rows whose effective labels pass this guard's filters. Originals of the rows, not copies."""

        if not self.active:
            return [row for row in rows if isinstance(row, Mapping)]
        # Compatibility stores without an ancestry reader may retain SQL-filtered
        # rows only when no locked all-of binding needs verification.
        if not callable(getattr(self.store, "read_label_rows", None)):
            return [] if self.all_of is not None else [row for row in rows if isinstance(row, Mapping)]
        kept: list[_Row] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            effective = self.effective_row(kind, row)
            if isinstance(effective, Mapping) and self._admits_effective(effective, kind=kind):
                kept.append(row)
        return kept

    def admit_beliefs(self, beliefs: Sequence[_Row]) -> list[_Row]:
        """Beliefs whose backing memory the filters admit. One batched read."""

        if not self.active:
            return [row for row in beliefs if isinstance(row, Mapping)]
        ids = [str(row.get("memory_id")) for row in beliefs if isinstance(row, Mapping) and row.get("memory_id")]
        reader = getattr(self.store, "read_label_rows", None)
        if not callable(reader):
            return [] if self.all_of is not None else [row for row in beliefs if isinstance(row, Mapping)]
        found: dict[str, Mapping[str, object]] = {}
        if callable(reader) and ids:
            for row in reader("memory", ids):
                if isinstance(row, Mapping) and row.get("id") is not None:
                    found[str(row.get("id"))] = row
        admitted = {str(row.get("id")) for row in self.admit_rows("memory", list(found.values()))}
        return [
            row
            for row in beliefs
            if isinstance(row, Mapping) and str(row.get("memory_id") or "") in admitted
        ]

    def _admits_effective(self, row: Mapping[str, object], *, kind: str) -> bool:
        if self.all_of is not None and (row.get("unverified") or not input_admitted(kind, row, self.all_of)):
            return False
        domain = str(row.get("domain") or "unknown")
        if self.domains and domain not in self.domains and domain != "unknown":
            return False
        sensitivity = str(row.get("sensitivity") or "unknown")
        if self.sensitivity_allowed and sensitivity not in self.sensitivity_allowed:
            return False
        if self.projects:
            from alicebot_api.vnext_project_scope import source_project_scope

            scope = source_project_scope(row) if canon_kind(kind) == "source" else resolve_project_scope(row).values
            _shape, floor = project_floor_shape(row)
            if not project_scopes_overlap(scope, self.projects, floor=floor):
                return False
        return True

    def _collected(self, kind: str, row: Mapping[str, object]) -> list[dict[str, object]]:
        if self._nodes is None:
            self._nodes = {}
        nodes, _exceeded = collect_label_rows(
            self.store, [{**dict(row), "kind": canon_kind(kind)}],
            max_nodes=NODE_BOUND, max_hops=HOP_BOUND, cache=self._nodes, user_id=_GUARD_USER,
        )
        return nodes


def admit_loaded(
    store: Any,
    *,
    kind: str,
    rows: Sequence[_Row],
    domains: Sequence[str] | None,
    sensitivity_allowed: Sequence[str] | None,
    projects: Sequence[str] | None = (),
    all_of: tuple[str, ...] | None = None,
) -> list[_Row]:
    """Drop loaded inputs whose effective labels miss the request filters."""

    guard = LabelGuard.for_filters(store, domains, sensitivity_allowed, projects, all_of=all_of)
    return guard.admit_rows(kind, rows)


def effective_row_for_fence(
    store: Any,
    identity: AgentIdentity | None,
    kind: str,
    row: Mapping[str, object],
) -> Mapping[str, object]:
    """The row an exact door should hand to the policy engine."""

    from alicebot_api.vnext_source_fence import SourceReadFence

    guard = LabelGuard.for_fence(store, SourceReadFence.for_identity(identity))
    settled = guard.effective_row(kind, row)
    return settled if isinstance(settled, Mapping) else row


def policy_labels(
    row: Mapping[str, object],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Domain, sensitivity, scope and floor to hand the policy engine."""

    domain = " ".join(str(row.get("domain") or "unknown").split()).strip() or "unknown"
    sensitivity = " ".join(str(row.get("sensitivity") or "unknown").split()).strip() or "unknown"
    scope = resolve_project_scope(row).values
    _shape, floor = project_floor_shape(row)
    return (domain,), (sensitivity,), scope, floor


def sensitivity_ceiling(identity: AgentIdentity | None) -> tuple[str, ...] | None:
    """Sensitivities this caller may see, or None when the caller has no ceiling.

    The owner (no identity) and an admin key have no ceiling. Every other
    profile uses the sensitivity list the policy already gives that profile.
    This ceiling does not add a domain or a project restriction.
    """

    if identity is None:
        return None
    from alicebot_api.vnext_agent_control import _profile_sensitivity

    ceiling = _profile_sensitivity(str(identity.permission_profile))
    if set(ceiling) >= set(ALL_SENSITIVITY):
        return None
    return ceiling


def apply_sensitivity_ceiling(
    store: Any,
    *,
    kind: str,
    rows: Sequence[Mapping[str, object]],
    identity: AgentIdentity | None,
) -> list[Mapping[str, object]]:
    """Rows whose effective sensitivity is inside the caller's ceiling.

    A missing identity and an admin key keep every row, including its title
    and id. Any other caller loses a row the ceiling does not admit, so a
    count of the returned list does not reveal it.
    """

    ceiling = sensitivity_ceiling(identity)
    if ceiling is None:
        return [row for row in rows if isinstance(row, Mapping)]
    return admit_loaded(
        store,
        kind=kind,
        rows=rows,
        domains=(),
        sensitivity_allowed=ceiling,
        projects=(),
    )


def readable_count(sql_count: int, fetched: int, admitted: int) -> int:
    """A stored count, reduced only by rows this page's guard dropped.

    When the guard drops nothing the stored count is returned unchanged.
    When the fetched page is the whole set, the count is the admitted length.
    """

    dropped = fetched - admitted
    if dropped <= 0:
        return sql_count
    if sql_count <= fetched:
        return admitted
    return max(0, sql_count - dropped)


def readable_status_counts(
    counts: Mapping[str, int],
    fetched: Sequence[Mapping[str, object]],
    admitted: Sequence[Mapping[str, object]],
    *,
    field: str = "status",
) -> dict[str, int]:
    """Status counts with one taken off for each row the guard dropped."""

    if len(fetched) == len(admitted):
        return dict(counts)
    kept = {id(row) for row in admitted}
    updated = dict(counts)
    for row in fetched:
        if id(row) in kept:
            continue
        status = str(row.get(field, "unknown"))
        current = int(updated.get(status, 0))
        if current > 0:
            updated[status] = current - 1
    return updated


def apply_unverified_rule(
    decision: PolicyDecision,
    row: Mapping[str, object] | None,
    identity: AgentIdentity | None,
) -> PolicyDecision:
    """A locked identity is refused an unverified derived row."""

    unverified = isinstance(row, Mapping) and bool(row.get("unverified"))
    locked = identity is not None and bool(identity.project_scope_locked)
    if not unverified or not locked:
        return decision
    reasons = tuple(dict.fromkeys((*decision.reasons, "derived_labels_unverified")))
    return replace(decision, decision="blocked", reasons=reasons)
