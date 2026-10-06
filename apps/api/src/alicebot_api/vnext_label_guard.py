"""Read-time check for a derived row.

One guard per request. An inactive guard returns its input and reads nothing.
An active guard settles the row with its inputs and answers the door with that
effective label.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace, field
from typing import Any, TypeVar
from contextvars import ContextVar
from functools import wraps
from contextlib import contextmanager

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
    dependencies_of,
    identifier,
    is_derived,
    input_admitted,
    settle_labels,
)
from alicebot_api.vnext_label_closure import collect_label_rows
from alicebot_api.vnext_project_scope import project_floor_shape, project_scope_identity, project_scopes_overlap, resolve_project_scope


_GUARD_USER = "label-guard"
_Row = TypeVar("_Row", bound=Mapping[str, object])


def _row_label_key(kind: str, row: Mapping[str, object]) -> tuple:
    return (kind, *(repr(row.get(field)) for field in (
        "id", "user_id", "domain", "sensitivity", "metadata_json", "value", "project_id", "source_id", "artifact_type", "project_scope", "project_floor",
    )))


@dataclass
class _RequestLabels:
    nodes: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)
    targets: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    source_copies: dict = field(default_factory=dict)
    row_sets: dict = field(default_factory=dict)
    dependencies: dict = field(default_factory=dict)
    source_admission: dict = field(default_factory=dict)

    def clear(self):
        self.nodes.clear()
        self.labels.clear()
        self.targets.clear()
        self.counts.clear()
        self.source_copies.clear()
        self.row_sets.clear()
        self.dependencies.clear()
        self.source_admission.clear()


_REQUEST_LABELS: ContextVar[tuple[Any, _RequestLabels] | None] = ContextVar("request_labels", default=None)


def invalidate_read_labels(store: Any) -> None:
    current = _REQUEST_LABELS.get()
    if current is not None and current[0] is store:
        current[1].clear()


def request_row_cache(store: Any, namespace: str) -> dict | None:
    """Raw rows may be reused only within the active request, before admission."""
    current = _REQUEST_LABELS.get()
    if current is None or current[0] is not store:
        return None
    return current[1].row_sets.setdefault(namespace, {})


@contextmanager
def label_read_scope(store):
    current = _REQUEST_LABELS.get()
    if current is not None and current[0] is store:
        yield
        return
    token = _REQUEST_LABELS.set((store, _RequestLabels()))
    try:
        yield
    finally:
        _REQUEST_LABELS.reset(token)


def label_read_request(fn):
    """Share ancestry only for one synchronous read, including nested services."""
    @wraps(fn)
    def wrapped(first, *args, **kwargs):
        store = getattr(first, "store", first)
        # Keep current label writers outside the request's cached snapshot.
        # These services read label tables and write only traces or telemetry.
        if getattr(store, "conn", None) is not None:
            lock = getattr(store, "lock_label_writes", None)
            if callable(lock):
                lock()
        with label_read_scope(store):
            return fn(first, *args, **kwargs)
    return wrapped


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
    _request: _RequestLabels | None = None

    def _state(self) -> _RequestLabels:
        current = _REQUEST_LABELS.get()
        state = current[1] if current is not None and current[0] is self.store else self._request
        if state is None:
            state = _RequestLabels()
        self._request = state
        self._nodes = state.nodes
        return state

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
        state = self._state()
        # Distinct projections and stored aliases are settled separately.
        # Only labels, never caller admission, are shared in this request.
        key = _row_label_key(kind, row)
        label = state.labels.get(key)
        template = (key[0], *key[2:])
        if label is None:
            label = state.source_copies.get(template)
        if label is None:
            nodes = self._collected(kind, row)
            settled = settle_labels(nodes, on_cycle="unverified", max_hops=HOP_BOUND, max_nodes=NODE_BOUND)
            label = settled.by_stored(kind, str(row.get("id") or ""), user_id=_GUARD_USER)
            # Copies of the same original sources have identical effective
            # labels. No derived parent, alias, missing parent or root cycle
            # is allowed in this shortcut; those retain an independent walk.
            refs = dependencies_of(kind, row)
            if refs and all(
                ref_kind == "source" and len(state.nodes.get((ref_kind, ref_id), [])) == 1
                and not is_derived(ref_kind, state.nodes[(ref_kind, ref_id)][0])
                for ref_kind, ref_id in refs
            ):
                state.source_copies[template] = label
        state.labels[key] = label
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
        # Store records expose scope and floor at the top level as well. The
        # resolver reads those first, so both representations must agree.
        copy["project_scope"] = list(metadata["project_scope"])
        copy["project_floor"] = list(metadata["project_floor"])
        if not copy["project_scope"]:
            copy["project_id"] = None
        return copy

    def admit_rows(self, kind: str, rows: Sequence[_Row]) -> list[_Row]:
        """Rows whose effective labels pass this guard's filters. Originals of the rows, not copies."""

        if not self.active:
            return [row for row in rows if isinstance(row, Mapping)]
        state = self._state()
        reader = getattr(self.store, "read_label_rows", None)
        wanted: dict[str, set[str]] = {}
        for row in rows:
            if isinstance(row, Mapping):
                state.targets[(kind, str(row.get("id")))] = row
                dependency_key = (kind, *(repr(row.get(field)) for field in (
                    "metadata_json", "value", "source_id", "source_artifact_id", "artifact_id", "memory_id", "artifact_type",
                )))
                refs = state.dependencies.get(dependency_key)
                if refs is None:
                    refs = dependencies_of(kind, row)
                    state.dependencies[dependency_key] = refs
                for ref_kind, ref_id in refs:
                    if (ref_kind, ref_id) not in state.nodes:
                        wanted.setdefault(ref_kind, set()).add(ref_id)
        if callable(reader):
            for ref_kind, ids in wanted.items():
                for ref_id in ids:
                    state.nodes[(ref_kind, ref_id)] = []
                for found in reader(ref_kind, sorted(ids)):
                    canonical = identifier(found.get("id"))
                    if canonical in ids:
                        state.nodes[(ref_kind, canonical)].append(dict(found))
        kept: list[_Row] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            key = _row_label_key(kind, row)
            template = (key[0], *key[2:])
            admission_key = (template, self.domains, self.sensitivity_allowed, self.projects, self.all_of)
            if template in state.source_copies and admission_key in state.source_admission:
                if state.source_admission[admission_key]:
                    kept.append(row)
                continue
            effective = self.effective_row(kind, row)
            admitted = isinstance(effective, Mapping) and self._admits_effective(effective, kind=kind)
            if template in state.source_copies:
                state.source_admission[admission_key] = admitted
            if admitted:
                kept.append(row)
        return kept

    def readable_status_counts(self, kind: str) -> dict[str, int]:
        """Count the complete population through the same effective admission.

        Display pages and stored-label SQL counts cannot establish this total.
        Stores must expose the complete narrow population rather than guessing
        a restricted total from a sample.
        """

        iterator = getattr(self.store, "iter_label_rows", None)
        if not callable(iterator):
            raise TypeError("readable counts require complete label enumeration")
        state = self._state()
        key = (kind, self.domains, self.sensitivity_allowed, self.projects, self.all_of)
        if key in state.counts:
            return dict(state.counts[key])
        counts: dict[str, int] = {}
        for batch in iterator(kind):
            for row in self.admit_rows(kind, batch):
                status = str(row.get("status", "unknown"))
                counts[status] = counts.get(status, 0) + 1
        state.counts[key] = dict(counts)
        return counts

    def admit_related_rows(self, rows: Sequence[_Row], *, kind: str, field: str) -> list[_Row]:
        """Admit an event or rating by its target's current effective label."""

        if not self.active:
            return [row for row in rows if isinstance(row, Mapping)]
        reader = getattr(self.store, "read_label_rows", None)
        if not callable(reader):
            return []
        ids = list(dict.fromkeys(str(row.get(field)) for row in rows if row.get(field)))
        state = self._state()
        missing = [row_id for row_id in ids if (kind, row_id) not in state.targets]
        for row in reader(kind, missing) if missing else []:
            state.targets[(kind, str(row.get("id")))] = row
        found = [state.targets[(kind, row_id)] for row_id in ids if (kind, row_id) in state.targets]
        admitted = {str(row.get("id")) for row in (
            self.admit_beliefs(found) if kind == "belief" else self.admit_rows(kind, found)
        )}
        return [row for row in rows if str(row.get(field) or "") in admitted]

    def admit_events(self, rows: Sequence[_Row]) -> list[_Row]:
        """Known label targets are admitted before an event exposes their IDs."""

        if not self.active:
            return [row for row in rows if isinstance(row, Mapping)]
        admitted: set[int] = set()
        kinds = {"source", "memory", "open_loop", "artifact", "project", "belief"}
        for kind in kinds:
            targets = [row for row in rows if str(row.get("target_type")) == kind]
            admitted.update(id(row) for row in self.admit_related_rows(targets, kind=kind, field="target_id"))
        return [row for row in rows if id(row) in admitted or (
            str(row.get("target_type")) not in kinds and not str(row.get("event_type", "")).endswith(".labels_raised")
        )]

    def readable_event_count(self) -> int:
        """Complete event count after current target admission."""

        iterator = getattr(self.store, "iter_label_events", None)
        if not callable(iterator):
            raise TypeError("readable counts require complete event enumeration")
        return sum(len(self.admit_events(batch)) for batch in iterator())

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

            resolution = resolve_project_scope(row)
            scope = source_project_scope(row) if canon_kind(kind) == "source" else resolution.values
            if canon_kind(kind) == "project" and not resolution.present and not is_derived(kind, row):
                # Original project rows are selected by ID, slug or name. A
                # canonical or effective derived scope remains authoritative.
                scope = project_scope_identity([*scope, str(row.get("id") or ""), row.get("slug"), row.get("name")])
            _shape, floor = project_floor_shape(row)
            if not project_scopes_overlap(scope, self.projects, floor=floor):
                return False
        return True

    def _collected(self, kind: str, row: Mapping[str, object]) -> list[dict[str, object]]:
        self._state()
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
    rows: Sequence[_Row],
    identity: AgentIdentity | None,
) -> list[_Row]:
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
