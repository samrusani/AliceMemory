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
from uuid import UUID

from alicebot_api.vnext_agent_control import (
    ALL_SENSITIVITY,
    VNEXT_DOMAINS,
    AgentIdentity,
    PolicyDecision,
)
from alicebot_api.vnext_derived_labels import (
    HOP_BOUND,
    NODE_BOUND,
    SENSITIVITY_RANK,
    SettledLabel,
    canon_kind,
    dependencies_of,
    dependency_label_signature,
    dependency_syntax_key,
    label_metadata_cache,
    identifier,
    has_implicit_weekly_inputs,
    is_derived,
    input_admitted,
    settle_labels,
    settle_verified_inputs,
)
from alicebot_api.vnext_label_closure import collect_label_rows
from alicebot_api.vnext_project_scope import project_floor_shape, project_scope_identity, project_scopes_overlap, resolve_project_scope


_GUARD_USER = "label-guard"
_Row = TypeVar("_Row", bound=Mapping[str, object])


def _row_label_key(kind: str, row: Mapping[str, object]) -> tuple:
    return (kind, *((field, repr(row[field])) for field in (
        "id", "user_id", "domain", "sensitivity", "metadata_json", "value", "project_id", "project", "projects", "scope_json", "source_id", "artifact_type", "project_scope", "project_floor",
    ) if field in row))


@dataclass
class _RequestLabels:
    nodes: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)
    targets: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    dependency_labels: dict = field(default_factory=dict)
    dependency_ancestry: dict = field(default_factory=dict)
    source_copies: dict = field(default_factory=dict)
    signatures: dict = field(default_factory=dict)
    parsed_signatures: dict = field(default_factory=dict)
    row_sets: dict = field(default_factory=dict)
    dependencies: dict = field(default_factory=dict)
    source_admission: dict = field(default_factory=dict)
    row_admission: dict = field(default_factory=dict)
    normalized_metadata: dict = field(default_factory=dict)
    row_keys: dict = field(default_factory=dict)
    resolved_inputs: dict = field(default_factory=dict)
    parent_labels: dict = field(default_factory=dict)
    native_labels: dict = field(default_factory=dict)

    def clear(self):
        self.nodes.clear()
        self.labels.clear()
        self.targets.clear()
        self.counts.clear()
        self.dependency_labels.clear()
        self.dependency_ancestry.clear()
        self.source_copies.clear()
        self.signatures.clear()
        self.parsed_signatures.clear()
        self.row_sets.clear()
        self.dependencies.clear()
        self.source_admission.clear()
        self.row_admission.clear()
        self.normalized_metadata.clear()
        self.row_keys.clear()
        self.resolved_inputs.clear()
        self.parent_labels.clear()
        self.native_labels.clear()


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
    state = _RequestLabels()
    token = _REQUEST_LABELS.set((store, state))
    try:
        with label_metadata_cache(state.normalized_metadata):
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
        key = self._key(kind, row)
        label = state.labels.get(key)
        template = self._signature(kind, row, key=key)
        root = (canon_kind(kind), identifier(row.get("id")))
        if label is None:
            direct = self._settled_inputs(kind, row, frozenset())
            if direct is not None:
                label = direct[0]
        if label is None and root not in state.dependency_ancestry.get(template, ()):
            label = state.dependency_labels.get(template)
        if label is None:
            refs = template[1]
            direct_ref = next(iter(refs)) if len(refs) == 1 else None
            parents: Sequence[Mapping[str, object]] = state.nodes.get(direct_ref, ()) if direct_ref is not None else ()
            if (template[3] == "copy" and not template[2] and direct_ref is not None
                    and direct_ref[0] == "source" and len(parents) == 1
                    and identifier(parents[0].get("id")) == direct_ref[1]
                    and NODE_BOUND >= 2 and HOP_BOUND >= 1):
                # A single original source terminates this walk. Ambiguous or
                # absent parents still take the complete canonical collector.
                nodes = [{**dict(row), "kind": canon_kind(kind), "user_id": _GUARD_USER},
                         {**dict(parents[0]), "kind": "source", "user_id": _GUARD_USER}]
            else:
                nodes = self._collected(kind, row)
            copy_template = None
            # A copy with exactly one original source has no recursive input
            # graph. First collect each root to check missing/ambiguous source
            # spellings and the independent bounds, then reuse only a verified
            # kernel result for identical root and parent label semantics.
            if len(nodes) == 2 and template[3] == "copy" and not template[2] and len(template[1]) == 1:
                parent = nodes[1]
                ref = next(iter(template[1]))
                if ref[0] == "source" and parent.get("kind") == "source" and identifier(parent.get("id")) == ref[1]:
                    parent_template = self._signature("source", parent, key=self._key("source", parent))
                    copy_template = (template[:1] + template[2:], parent_template)
                    label = state.source_copies.get(copy_template)
            if label is None:
                settled = settle_labels(nodes, on_cycle="unverified", max_hops=HOP_BOUND, max_nodes=NODE_BOUND)
                label = settled.by_stored(kind, str(row.get("id") or ""), user_id=_GUARD_USER)
            if copy_template is not None and not label.unverified:
                state.source_copies[copy_template] = label
            ancestry = frozenset(
                [*(ref for node in nodes for ref in dependencies_of(str(node["kind"]), node)),
                 *((str(node["kind"]), identifier(node.get("id"))) for node in nodes[1:]),
                 *(("memory", identifier(node["memory_id"])) for node in nodes
                   if node.get("kind") == "belief" and node.get("memory_id"))]
            )
            implicit_parent = False
            for node in nodes:
                if has_implicit_weekly_inputs(str(node["kind"]), node):
                    implicit_parent = True
                    break
            # Complete verified ancestry has already passed the per-root hop,
            # node, alias and missing-parent checks. Only another root outside
            # that ancestry can reuse this same dependency signature.
            if not label.unverified and root not in ancestry and not implicit_parent:
                state.dependency_labels[template] = label
                state.dependency_ancestry[template] = ancestry
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

    def _settled_inputs(self, kind: str, row: Mapping[str, object], trail: frozenset) -> tuple[SettledLabel, frozenset, int] | None:
        """Settle verified acyclic parents once, sharing only label semantics.

        Missing/ambiguous inputs, cycles, implicit weekly parents and exceeded
        per-origin bounds fall back to the complete canonical graph walk.
        Admission remains caller-specific and is never stored in this cache.
        """

        state = self._state()
        key = self._key(kind, row)
        cached = state.parent_labels.get(key)
        if len(trail) > HOP_BOUND or kind == "belief":
            return None
        if cached is not None:
            return None if cached[1] & trail else cached
        root = (canon_kind(kind), identifier(row.get("id")))
        if root in trail:
            return None
        template = self._signature(kind, row, key=key)
        refs, problem = template[1:3]
        if problem or has_implicit_weekly_inputs(kind, row):
            return None
        reader = getattr(self.store, "read_label_rows", None)
        if refs and not callable(reader):
            return None
        parents = []
        ancestry = {root}
        depth = 0
        for parent_kind, parent_id in sorted(refs):
            if parent_kind == "belief":
                return None
            parent_key = (parent_kind, parent_id)
            if parent_key not in state.nodes and callable(reader):
                state.nodes[parent_key] = [dict(found) for found in reader(parent_kind, [parent_id])
                                           if identifier(found.get("id")) == parent_id]
            raw = state.nodes[parent_key]
            if len(raw) != 1 or identifier(raw[0].get("id")) != parent_id:
                return None
            parent = self._settled_inputs(parent_kind, raw[0], trail | {root})
            if parent is None or root in parent[1]:
                return None
            parents.append(parent[0])
            ancestry.update(parent[1])
            depth = max(depth, parent[2] + 1)
            if depth > HOP_BOUND or len(ancestry) > NODE_BOUND:
                return None
        semantic_parents = tuple((parent.kind, parent.domain, parent.sensitivity, parent.project_scope,
                                  parent.project_floor, parent.carries_scope) for parent in parents)
        # Root rule and stored labels plus resolved input labels, excluding IDs.
        semantic = (template[:1] + template[2:], semantic_parents)
        label = state.resolved_inputs.get(semantic)
        if label is None:
            label = settle_verified_inputs(kind, {**dict(row), "user_id": _GUARD_USER}, parents)
            state.resolved_inputs[semantic] = label
        else:
            label = replace(label, stored_id=str(row.get("id") or ""), normalized_id=root[1])
        result = (label, frozenset(ancestry), depth)
        state.parent_labels[key] = result
        return result

    def admit_rows(self, kind: str, rows: Sequence[_Row]) -> list[_Row]:
        """Rows whose effective labels pass this guard's filters. Originals of the rows, not copies."""

        if not self.active:
            return [row for row in rows if isinstance(row, Mapping)]
        state = self._state()
        if self.sensitivity_allowed:
            highest = max(SENSITIVITY_RANK.get(value, SENSITIVITY_RANK["unknown"]) for value in self.sensitivity_allowed)
            rows = [row for row in rows if isinstance(row, Mapping)
                    and SENSITIVITY_RANK.get(str(row.get("sensitivity") or "unknown"), SENSITIVITY_RANK["unknown"]) <= highest]
        for row in rows:
            if isinstance(row, Mapping):
                state.targets[(kind, str(row.get("id")))] = row
        self._prefetch_inputs(kind, rows)
        kept: list[_Row] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if not is_derived(kind, row):
                if self._admits_effective(row, kind=kind):
                    kept.append(row)
                continue
            key = self._key(kind, row)
            row_admission_key = (key, self.domains, self.sensitivity_allowed, self.projects, self.all_of)
            if row_admission_key in state.row_admission:
                if state.row_admission[row_admission_key]:
                    kept.append(row)
                continue
            settled = state.labels.get(key)
            if settled is not None and not settled.unverified:
                admitted = self._admits_effective({"domain": settled.domain, "sensitivity": settled.sensitivity,
                    "project_scope": settled.project_scope, "project_floor": settled.project_floor,
                    "unverified": settled.unverified}, kind=kind)
                state.row_admission[row_admission_key] = admitted
                if admitted:
                    kept.append(row)
                continue
            template = self._signature(kind, row, key=key)
            admission_key = (template, self.domains, self.sensitivity_allowed, self.projects, self.all_of)
            root = (canon_kind(kind), identifier(row.get("id")))
            reusable = template in state.dependency_labels and root not in state.dependency_ancestry.get(template, ())
            if reusable and admission_key in state.source_admission:
                admitted = state.source_admission[admission_key]
                state.row_admission[row_admission_key] = admitted
                if admitted:
                    kept.append(row)
                continue
            direct = self._settled_inputs(kind, row, frozenset()) if not self.projects and self.all_of is None else None
            if direct is not None:
                # A count or unscoped list needs only the two settled labels.
                # Scope-sensitive callers still use the complete effective
                # projection, including its floor and malformed-input checks.
                label = direct[0]
                state.labels[key] = label
                admitted = self._admits_effective({"domain": label.domain, "sensitivity": label.sensitivity}, kind=kind)
            else:
                effective = self.effective_row(kind, row)
                admitted = isinstance(effective, Mapping) and self._admits_effective(effective, kind=kind)
            # This is caller admission, separate from shared label settlement.
            # Full label projections and every filter distinguish grants; all
            # request caches are cleared together on writes and rollback.
            state.row_admission[row_admission_key] = admitted
            if reusable:
                state.source_admission[admission_key] = admitted
            if admitted:
                kept.append(row)
        return kept

    def _prefetch_inputs(self, kind: str, rows: Sequence[Mapping[str, object]]) -> None:
        """Load ancestry by frontier; each origin is still verified separately.

        This cache contains raw rows including every stored alias. A bounded
        batch stops prefetching rather than changing the kernel's per-root
        hop/node limits; unresolved inputs use the normal collector.
        """
        reader = getattr(self.store, "read_label_rows", None)
        if not callable(reader):
            return
        state = self._state()
        prefetched = state.row_sets.setdefault("prefetched-input-expansions", {})
        frontier = [(kind, row) for row in rows if isinstance(row, Mapping)]
        expanded: set[tuple[str, str]] = set()
        for _ in range(HOP_BOUND + 1):
            refs: set[tuple[str, str]] = set()
            pending: list[tuple] = []
            for row_kind, row in frontier:
                if is_derived(row_kind, row):
                    key = self._key(row_kind, row)
                    if key in prefetched or key in state.labels or key in state.parent_labels:
                        continue
                    native = self._native_label_for_projection(row_kind, row, key=key)
                    if native is not None:
                        state.labels[key] = native
                        continue
                    refs.update(dependencies_of(row_kind, row))
                    pending.append(key)
            refs.difference_update(expanded)
            if len(expanded | refs) > NODE_BOUND:
                return
            if not refs:
                prefetched.update(dict.fromkeys(pending))
                return
            expanded.update(refs)
            wanted: dict[str, set[str]] = {}
            for ref_kind, ref_id in refs:
                if (ref_kind, ref_id) not in state.nodes:
                    wanted.setdefault(ref_kind, set()).add(ref_id)
            for ref_kind, ids in wanted.items():
                for ref_id in ids:
                    state.nodes[(ref_kind, ref_id)] = []
                for found in reader(ref_kind, sorted(ids)):
                    canonical = identifier(found.get("id"))
                    if canonical in ids:
                        state.nodes[(ref_kind, canonical)].append(dict(found))
            # Mark only after these immediate inputs have actually been read.
            # A bounded/partial frontier still uses per-origin settlement;
            # this raw-row reuse never supplies a label or an admission grant.
            prefetched.update(dict.fromkeys(pending))
            frontier = [(ref_kind, row) for ref_kind, ref_id in refs
                        for row in state.nodes[(ref_kind, ref_id)]]

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
        native = getattr(self.store, "count_" + {"memory": "memories", "artifact": "artifacts", "open_loop": "open_loops"}.get(kind, kind) + "_by_status", None)
        if not self.active and callable(native):
            counts = native()
            state.counts[key] = dict(counts)
            return counts
        plain_counter = getattr(self.store, "count_original_label_statuses", None)
        if callable(getattr(type(self.store), "count_original_label_statuses", None)) and callable(plain_counter) and not self.projects and self.all_of is None:
            counts = plain_counter(kind, domains=self.domains, sensitivity_allowed=self.sensitivity_allowed)
            prefilter = {"reject_sensitivity_allowed": self.sensitivity_allowed} if getattr(type(self.store), "label_count_input_prefilter", False) else {}
            unique_ids = getattr(type(self.store), "label_count_canonical_unique_ids", False)
            batches = iterator(kind, derived_only=True, **prefilter, **({"batch_size": 5000} if unique_ids else {}))
        else:
            counts = {}
            unique_ids = False
            batches = iterator(kind)
        for batch in batches:
            if unique_ids:
                # PostgreSQL stores canonical UUID primary keys. These complete
                # native label rows already contain the parent projection, so
                # resolving a counted parent need not fetch it again. Text-ID
                # stores must still load all aliases through their reader.
                for row in batch:
                    if isinstance(row.get("id"), UUID):
                        state.nodes.setdefault((canon_kind(kind), str(row["id"])), [row])
                self._settle_native_count_batch(kind, batch)
            for row in self.admit_rows(kind, batch):
                status = str(row.get("status", "unknown"))
                counts[status] = counts.get(status, 0) + 1
        state.counts[key] = dict(counts)
        return counts

    def _settle_native_count_batch(self, kind: str, rows: Sequence[Mapping[str, object]]) -> None:
        """The complete UUID-native population uses the canonical bulk kernel.

        Raw inputs are still loaded through the same bounded frontier. Cache
        only verified results for these exact count projections: incomplete
        ancestry, implicit weekly parents and every unverified row retain the
        ordinary per-origin collector. Text-ID stores never enter this path.
        """
        if not rows or any(type(row.get("id")) is not UUID for row in rows):
            return
        self._prefetch_inputs(kind, rows)
        state = self._state()
        for row in rows:
            stored = state.nodes.get((canon_kind(kind), identifier(row["id"])), ())
            if len(stored) != 1 or (stored[0] is not row and _row_label_key(kind, stored[0]) != _row_label_key(kind, row)):
                return
        nodes: list[dict[str, object]] = []
        for (node_kind, _node_id), found in state.nodes.items():
            if len(found) != 1:
                return
            row = found[0]
            if has_implicit_weekly_inputs(node_kind, row) or node_kind == "belief":
                return
            nodes.append({**row, "kind": node_kind, "user_id": _GUARD_USER})
        result = settle_labels(nodes, on_cycle="unverified", max_hops=HOP_BOUND, max_nodes=NODE_BOUND)
        settled = {label.key: label for label in result.rows}
        for row in rows:
            label = settled.get((canon_kind(kind), _GUARD_USER, identifier(row["id"])))
            if label is not None and not label.unverified:
                state.labels[self._key(kind, row)] = label
                state.native_labels[(canon_kind(kind), identifier(row["id"]))] = (row, label)

    def _native_label_for_projection(self, kind: str, row: Mapping[str, object], *, key: tuple) -> SettledLabel | None:
        """Reuse one verified UUID origin only for equivalent label semantics."""
        current = _REQUEST_LABELS.get()
        if current is None or current[0] is not self.store or type(row.get("id")) is not UUID:
            return None
        entry = current[1].native_labels.get((canon_kind(kind), identifier(row["id"])))
        if entry is None:
            return None
        raw, label = entry
        if self._signature(kind, row, key=key) != self._signature(kind, raw, key=self._key(kind, raw)):
            return None
        # Same origin, same direct inputs and stored semantics in this locked
        # snapshot: its alias/cycle/bound proof remains the same. Caller filters
        # are checked separately; writes and rollback clear this map together.
        return label

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
        if kind == "source":
            for row_id in missing:
                cached = state.nodes.get((kind, identifier(row_id)), ())
                if len(cached) == 1 and str(cached[0].get("id")) == row_id:
                    state.targets[(kind, row_id)] = cached[0]
            missing = [row_id for row_id in missing if (kind, row_id) not in state.targets]
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
        prefilter: dict[str, Any] = {"reject_sensitivity_allowed": self.sensitivity_allowed} if self.active and getattr(type(self.store), "label_count_input_prefilter", False) else {}
        canonical = getattr(type(self.store), "label_count_canonical_unique_ids", False)
        counter = getattr(self.store, "count_source_label_events", None)
        source_count = 0
        if canonical and callable(counter) and self.active and not self.projects and self.all_of is None:
            # Sources are original inputs, so this unscoped count has no
            # ancestry to settle. The native counter preserves exact target
            # spelling, current tenant, domains and sensitivity. Scoped and
            # inactive callers keep the complete per-target path below.
            source_count = counter(domains=self.domains, sensitivity_allowed=self.sensitivity_allowed)
            prefilter["exclude_source_targets"] = True
        if canonical:
            # Native UUID stores already read up to 5,000 event targets in
            # each SQL page. Admit that complete page together instead of
            # splitting its missing source reads into five database trips.
            # Every target still goes through its current effective guard.
            prefilter["batch_size"] = 5000
        return source_count + sum(len(self.admit_events(batch)) for batch in iterator(**prefilter))

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

    def _key(self, kind: str, row: Mapping[str, object]) -> tuple:
        current = _REQUEST_LABELS.get()
        if current is None or current[0] is not self.store:
            return _row_label_key(kind, row)
        keys = current[1].row_keys
        raw_key = (kind, id(row))
        if raw_key not in keys or keys[raw_key][0] is not row:
            # This locked request already pins each raw projection and clears
            # all entries on writes/rollback. A compact object key avoids
            # repeatedly hashing full UUID/metadata representations. Strong
            # references prevent id reuse, and distinct loaded projections
            # never share an identity cache entry. Outside a snapshot the
            # content key above still detects changes between guard calls.
            keys[raw_key] = (row, raw_key)
        return keys[raw_key][1]

    def _signature(self, kind: str, row: Mapping[str, object], *, key: tuple) -> tuple:
        state = self._state()
        if key not in state.signatures:
            syntax = dependency_syntax_key(kind, row)
            if syntax not in state.parsed_signatures:
                state.parsed_signatures[syntax] = dependency_label_signature(kind, row)
            state.signatures[key] = state.parsed_signatures[syntax]
        return state.signatures[key]


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
