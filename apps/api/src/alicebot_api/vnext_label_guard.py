"""Read-time check for a derived row.

One guard per request. An inactive guard returns its input and reads nothing.
An active guard settles the row with its inputs and answers the door with that
effective label.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace, field
from typing import Any, TypeVar, cast
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
    dependency_record,
    dependency_syntax_key,
    label_metadata_cache,
    identifier,
    has_implicit_weekly_inputs,
    is_derived,
    input_admitted,
    settle_labels,
    settle_verified_inputs,
    _mark_dependency_bounds,
)
from alicebot_api.vnext_label_closure import collect_label_rows
from alicebot_api.vnext_label_sql import (
    EVENT_CHILD_TARGETS,
    EVENT_EDGE_TARGET,
    EVENT_PAYLOAD_REFERENCES,
    EVENT_TARGET_KINDS,
    EVENT_TYPE_REFERENCES,
)
from alicebot_api.vnext_project_scope import project_floor_shape, project_scope_identity, project_scopes_overlap, resolve_project_scope


_GUARD_USER = "label-guard"
# The reach of an event feed for a caller with limits: this many events, newest first, and no more.
EVENT_FEED_SCAN_LIMIT = 2_000
_Row = TypeVar("_Row", bound=Mapping[str, object])


def _row_label_key(kind: str, row: Mapping[str, object]) -> tuple:
    return (kind, *((field, repr(row[field])) for field in (
        "id", "user_id", "domain", "sensitivity", "metadata_json", "value", "project_id", "project", "projects", "scope_json", "source_id", "artifact_type", "project_scope", "project_floor",
    ) if field in row))


def _rank_projection_supported(row: Mapping[str, object]) -> bool:
    """Only canonical scope/floor shapes use a reduced admission proof.

    A reduced proof answers a sensitivity-only question, and a project scope or floor
    never changes a sensitivity: a list of any length is a canonical shape. A scope or
    floor of another type, and every legacy project alias, still take the full kernel.
    """
    metadata = row.get("metadata_json")
    if type(metadata) is not dict:
        return False
    for container in (row, metadata):
        for name in ("project_scope", "project_floor"):
            if name in container and type(container[name]) not in (list, tuple):
                return False
        for name in ("project_id", "project", "projects", "scope_json", "agent_identity", "agentic_memory"):
            if name in container and container[name] is not None:
                return False
    return True


# An edge end of one of these kinds is judged the way the door for that kind judges it. An entity end has no label. An end of
# any other kind, or an end that no longer exists, cannot be shown to be readable, so the edge is not (the edge review door
# answers it as a missing edge).
_EDGE_END_KINDS = frozenset({"source", "memory", "belief"})


def _spellings(row_id: str) -> tuple[str, str]:
    """The id an event holds with the spaces taken off, and the canonical text of a UUID (lower case, hyphenated).

    Text that is not a UUID has the one spelling, so it is found only when a row is stored under exactly that text.
    """
    text = row_id.strip()
    return text, identifier(text)


def _payload_ids(value: object) -> list[str] | None:
    """The ids that an event payload field holds, or None when the field is not an id or a list of ids."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        ids: list[str] = []
        for item in value:
            if item is None:
                continue
            if not isinstance(item, str):
                return None
            if item.strip():
                ids.append(item)
        return ids
    return None


_TYPED_REFERENCE_KEYS = frozenset(key for fields in EVENT_TYPE_REFERENCES.values() for key in fields)


def _typed_reference_kind(event_type: str, key: str) -> str | None:
    for prefix, fields in EVENT_TYPE_REFERENCES.items():
        if event_type.startswith(prefix) and key in fields:
            return fields[key]
    return None


def event_target_references(row: Mapping[str, object]) -> list[tuple[str, str]]:
    """The labelled row, or the edge, that an event is about, as (kind, id). The id is kept exactly as the event holds it."""
    target_type = str(row.get("target_type"))
    if target_type in EVENT_TARGET_KINDS:
        return [(target_type, str(row.get("target_id") or ""))]
    if target_type == EVENT_EDGE_TARGET:
        return [("edge", str(row.get("target_id") or ""))]
    return []


def event_payload_references(row: Mapping[str, object]) -> list[tuple[str, str]]:
    """The rows the payload of an event names, as (kind, id), in the spelling the payload holds.

    A reference that cannot be resolved carries the empty id, which no row has, so a caller with limits is never admitted
    for it: a payload field that is not an id, and a chunk event that names no source.
    """
    payload = row.get("payload_json")
    fields: Mapping[str, object] = payload if isinstance(payload, Mapping) else {}
    references: list[tuple[str, str]] = []
    event_type = ""
    for key, value in fields.items():
        kind = EVENT_PAYLOAD_REFERENCES.get(key)
        if kind is None and key in _TYPED_REFERENCE_KEYS:
            event_type = event_type or str(row.get("event_type"))
            kind = _typed_reference_kind(event_type, key)
        if kind is None:
            continue
        ids = _payload_ids(value)
        if ids is None:
            references.append((kind, ""))
        else:
            references.extend((kind, row_id) for row_id in ids)
    child = EVENT_CHILD_TARGETS.get(str(row.get("target_type")))
    if child is not None and not _payload_ids(fields.get(child[1])):
        references.append((child[0], ""))
    return references


def event_references(row: Mapping[str, object]) -> list[tuple[str, str]]:
    """Every row an event names, as (kind, id): the row it is about and the rows its payload holds the ids of."""
    return [*event_target_references(row), *event_payload_references(row)]


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
    rank_rows: dict = field(default_factory=dict)
    rank_origins: dict = field(default_factory=dict)
    rank_admission: dict = field(default_factory=dict)

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
        self.rank_rows.clear()
        self.rank_origins.clear()
        self.rank_admission.clear()


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
    def unlimited(cls, store: Any) -> LabelGuard:
        """The guard of the owner and of an unbound admin key: inactive, so it returns what it is given."""

        return cls(store=store, active=False)

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
            # Redundant: a cached row is acyclic (the root-in-trail and root-in-ancestry refusals below ran when it was settled), so its ancestry cannot meet the trail.
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
        rank_ceiling = self._rank_ceiling()
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
            rank = self._native_rank_for_projection(kind, row) if rank_ceiling is not None else None
            if rank_ceiling is not None and rank is not None:
                grant_key = (self._key(kind, row), self.domains, self.sensitivity_allowed, self.projects, self.all_of)
                if grant_key not in state.rank_admission:
                    state.rank_admission[grant_key] = rank <= rank_ceiling
                if state.rank_admission[grant_key]:
                    kept.append(row)
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
            # The ancestry term is redundant: a root among the shared inputs' ancestors is on their cycle and settles to the same label as the root that stored it.
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
        if self._rank_ceiling() is not None and state.rank_origins:
            frontier = [(row_kind, row) for row_kind, row in frontier
                        if self._native_rank_for_projection(row_kind, row) is None]
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
        if self._settle_native_rank_batch(kind, rows):
            return
        for row in rows:
            stored = state.nodes.get((canon_kind(kind), identifier(row["id"])), ())
            # The length and identity terms are redundant (the caller seeds each row, the node loop below refuses duplicates, identity implies equal keys); the key comparison stays.
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

    def _rank_ceiling(self) -> int | None:
        """A complete rank prefix may use a verified sensitivity-only proof."""
        current = _REQUEST_LABELS.get()
        if (not self.active or self.domains or self.projects or self.all_of is not None
                or current is None or current[0] is not self.store
                or not getattr(type(self.store), "label_count_canonical_unique_ids", False)):
            return None
        allowed = frozenset(self.sensitivity_allowed)
        if not allowed or not allowed.issubset(SENSITIVITY_RANK):
            return None
        ceiling = max(SENSITIVITY_RANK[value] for value in allowed)
        if allowed != frozenset(value for value, rank in SENSITIVITY_RANK.items() if rank <= ceiling):
            return None
        return ceiling

    def _settle_native_rank_batch(self, kind: str, rows: Sequence[Mapping[str, object]]) -> bool:
        """Prove a complete native DAG before publishing any reduced result.

        Canonical syntax, source-kind rules and independent origin bounds stay
        authoritative. Any unsupported graph uses the full label kernel.
        These ranks never enter a full-label or effective-row cache.
        """
        if self._rank_ceiling() is None:
            return False
        state = self._state()
        for row in rows:
            found = state.nodes.get((canon_kind(kind), identifier(row["id"])), ())
            # The length and identity terms are redundant (the caller seeds each row, the node loop below refuses duplicates, identity implies equal keys); the key comparison stays.
            if len(found) != 1 or (found[0] is not row and _row_label_key(kind, found[0]) != _row_label_key(kind, row)):
                return False
        graph: dict[tuple[str, str, str], frozenset[tuple[str, str, str]]] = {}
        ranks: dict[tuple[str, str, str], int] = {}
        origins: list[tuple[str, str, str]] = []
        for (node_kind, node_id), found in state.nodes.items():
            node_kind = canon_kind(node_kind)
            if len(found) != 1:
                return False
            row = found[0]
            if type(row.get("id")) is not UUID or identifier(row["id"]) != node_id:
                return False
            if (node_kind == "belief" or has_implicit_weekly_inputs(node_kind, row)
                    or not _rank_projection_supported(row)):
                return False
            sensitivity = str(row.get("sensitivity") or "unknown")
            if sensitivity not in SENSITIVITY_RANK:
                return False
            key = (node_kind, _GUARD_USER, node_id)
            if key in graph:
                return False
            derived = is_derived(node_kind, row)
            refs, problem = dependency_record(node_kind, row) if derived else (frozenset(), "")
            if problem:
                return False
            graph[key] = frozenset((ref_kind, _GUARD_USER, ref_id) for ref_kind, ref_id in refs)
            ranks[key] = SENSITIVITY_RANK[sensitivity]
            if derived:
                origins.append(key)
        # Redundant: a missing input never completes, so the visited count below refuses the graph as well.
        if any(ref not in graph for refs in graph.values() for ref in refs):
            return False
        pending_count = {key: len(refs) for key, refs in graph.items()}
        dependants: dict[tuple[str, str, str], list[tuple[str, str, str]]] = {}
        for node_key, input_keys in graph.items():
            for input_key in input_keys:
                dependants.setdefault(input_key, []).append(node_key)
        pending = deque(key for key, count in pending_count.items() if not count)
        visited = 0
        while pending:
            key = pending.popleft()
            visited += 1
            for child in dependants.get(key, ()):
                ranks[child] = max(ranks[child], ranks[key])
                pending_count[child] -= 1
                if not pending_count[child]:
                    pending.append(child)
        if visited != len(graph):
            return False
        problems: dict[tuple[str, str, str], str] = {}
        _mark_dependency_bounds(origins, graph, problems, max_hops=HOP_BOUND, max_nodes=NODE_BOUND)
        if problems:
            return False
        for row in rows:
            canonical = (canon_kind(kind), identifier(row["id"]))
            rank = ranks[(canonical[0], _GUARD_USER, canonical[1])]
            state.rank_rows[self._key(kind, row)] = rank
            state.rank_origins[canonical] = (row, rank)
        return True

    def _native_rank_for_projection(self, kind: str, row: Mapping[str, object]) -> int | None:
        """A reduced proof belongs to one supported origin and its semantics."""
        current = _REQUEST_LABELS.get()
        if current is None or current[0] is not self.store or type(row.get("id")) is not UUID:
            return None
        state = current[1]
        entry = state.rank_origins.get((canon_kind(kind), identifier(row["id"])))
        if entry is None or not _rank_projection_supported(row):
            return None
        key = self._key(kind, row)
        if key in state.rank_rows:
            return state.rank_rows[key]
        raw, rank = entry
        projection_signature = self._signature(kind, row, key=key)
        if projection_signature != self._signature(kind, raw, key=self._key(kind, raw)):
            return None
        state.rank_rows[key] = rank
        return rank

    def _native_label_for_projection(self, kind: str, row: Mapping[str, object], *, key: tuple) -> SettledLabel | None:
        """Reuse one verified UUID origin only for equivalent label semantics."""
        current = _REQUEST_LABELS.get()
        # The UUID term is redundant: a native store keeps one canonical UUID per row (label_count_canonical_unique_ids), so a string spelling names the same row.
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

    def _admitted_target_ids(self, kind: str, ids: Sequence[str], *, echoed: bool = False) -> set[str]:
        """The ids, as given, of the rows of this kind that the guard admits now. One batched read for the ids not yet read.

        An id is looked up exactly as given. An ``echoed`` id is one a client wrote and an event repeats (the id of a project
        sent in capitals, or with spaces round it), so it is looked up as given with the spaces taken off and by its canonical
        spelling too, because the stores answer in lower case. The row stored under the spelling given is the one judged,
        and the canonical spelling stands in only when there is none.
        """

        if kind == "edge":
            return self._admitted_edge_ids(ids)
        reader = getattr(self.store, "read_label_rows", None)
        if not callable(reader):
            return set()
        ids = list(dict.fromkeys(str(row_id) for row_id in ids if row_id))
        spelled = {row_id: _spellings(row_id) if echoed else (row_id, row_id) for row_id in ids}
        wanted = list(dict.fromkeys(text for pair in spelled.values() for text in pair if text))
        state = self._state()
        missing = [row_id for row_id in wanted if (kind, row_id) not in state.targets]
        if kind == "source":
            for row_id in missing:
                cached = state.nodes.get((kind, identifier(row_id)), ())
                if len(cached) == 1 and str(cached[0].get("id")) == row_id:
                    state.targets[(kind, row_id)] = cached[0]
            missing = [row_id for row_id in missing if (kind, row_id) not in state.targets]
        for row in reader(kind, missing) if missing else []:
            state.targets[(kind, str(row.get("id")))] = row
        found = [state.targets[(kind, row_id)] for row_id in wanted if (kind, row_id) in state.targets]
        admitted = {str(row.get("id")) for row in (
            self.admit_beliefs(found) if kind == "belief" else self.admit_rows(kind, found)
        )}
        return {
            row_id for row_id, (text, canonical) in spelled.items()
            if (text in admitted if (kind, text) in state.targets else canonical in admitted)
        }

    def _admitted_edge_ids(self, ids: Sequence[str]) -> set[str]:
        """Edges whose every labelled end the guard admits. An edge has no label of its own."""

        reader = getattr(self.store, "read_label_rows", None)
        if not callable(reader):
            return set()
        ids = list(dict.fromkeys(str(row_id) for row_id in ids if row_id))
        state = self._state()
        missing = [row_id for row_id in ids if ("edge", row_id) not in state.targets]
        for row in reader("edge", missing) if missing else []:
            state.targets[("edge", str(row.get("id")))] = row
        edges = {row_id: state.targets[("edge", row_id)] for row_id in ids if ("edge", row_id) in state.targets}
        ends: dict[str, list[str]] = {}
        for edge in edges.values():
            for side in ("from", "to"):
                end_kind = str(edge.get(f"{side}_type") or "")
                if end_kind in _EDGE_END_KINDS:
                    ends.setdefault(end_kind, []).append(str(edge.get(f"{side}_id") or ""))
        readable = {end_kind: self._admitted_target_ids(end_kind, values) for end_kind, values in ends.items()}
        admitted: set[str] = set()
        for edge_id, edge in edges.items():
            for side in ("from", "to"):
                end_kind = str(edge.get(f"{side}_type") or "")
                if end_kind == "entity":
                    continue
                if end_kind not in _EDGE_END_KINDS or str(edge.get(f"{side}_id") or "") not in readable[end_kind]:
                    break
            else:
                admitted.add(edge_id)
        return admitted

    def admit_related_rows(self, rows: Sequence[_Row], *, kind: str, field: str) -> list[_Row]:
        """Admit an event or rating by its target's current effective label."""

        if not self.active:
            return [row for row in rows if isinstance(row, Mapping)]
        if not callable(getattr(self.store, "read_label_rows", None)):
            return []
        admitted = self._admitted_target_ids(kind, [str(row.get(field)) for row in rows if row.get(field)])
        return [row for row in rows if str(row.get(field) or "") in admitted]

    def admit_events(self, rows: Sequence[_Row], *, cursors: bool = True) -> list[_Row]:
        """An event is admitted when the guard admits every row it names, before it exposes their IDs.

        It names its target, when the target is a labelled row or an edge, and every id in the payload fields that hold
        the id of a labelled row (see ``event_references``). A chunk of a source has no label, so its event takes the
        label of the source it names. An event whose target has no label and whose payload names no row is admitted,
        except a ``labels_raised`` event, which says what a label was.

        A connector event also records a cursor (see ``CONNECTOR_EVENT_CURSOR_FIELDS``), and for a file or a page that is
        its path or its address. An admitted event shows each cursor only when the caller may read the source it came
        from and holds ``null`` in its place otherwise, the rule of the connector screens; the event is copied and the row
        it came from is not edited. ``cursors=False`` is for a caller that reads only how many events are admitted.
        """

        kept = self._admitted_events(rows)
        if not cursors or not self.active or not any(str(row.get("event_type", "")).startswith("connector.") for row in kept):
            return kept
        from alicebot_api.vnext_connectors import VNextConnectorService

        return cast("list[_Row]", VNextConnectorService(self.store).shown_event_cursors(kept, guard=self))

    def _admitted_events(self, rows: Sequence[_Row]) -> list[_Row]:
        if not self.active:
            return [row for row in rows if isinstance(row, Mapping)]
        named = []
        wanted: dict[tuple[str, bool], list[str]] = {}
        for row in rows:
            references = [(kind, row_id, False) for kind, row_id in event_target_references(row)]
            references.extend((kind, row_id, True) for kind, row_id in event_payload_references(row))
            named.append((row, references))
            for kind, row_id, echoed in references:
                wanted.setdefault((kind, echoed), []).append(row_id)
        admitted = {key: self._admitted_target_ids(key[0], ids, echoed=key[1]) for key, ids in wanted.items()}
        kept = []
        for row, references in named:
            for kind, row_id, echoed in references:
                if row_id not in admitted[(kind, echoed)]:
                    break
            else:
                if str(row.get("target_type")) in EVENT_TARGET_KINDS or not str(row.get("event_type", "")).endswith(".labels_raised"):
                    kept.append(row)
        return kept

    def newest_admitted_events(self, fetch: Callable[[int], Sequence[_Row]], *, want: int) -> list[_Row]:
        """The ``want`` newest events this guard admits. ``fetch(n)`` returns the newest ``n`` events, newest first.

        When the newest events are hidden from a caller with limits (a confidential source captured a moment ago leaves a
        dozen), a feed that read only its own limit would show nothing. The read widens, five times at a time, up to
        ``EVENT_FEED_SCAN_LIMIT`` events, so the feed is shorter than ``want`` only when fewer readable events lie within
        that reach.
        """

        size = want
        while True:
            fetched = fetch(size)
            admitted = self.admit_events(fetched)
            if len(admitted) >= want or len(fetched) < size or size >= EVENT_FEED_SCAN_LIMIT:
                return admitted[:want]
            size = min(size * 5, EVENT_FEED_SCAN_LIMIT)

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
        return source_count + sum(len(self._admitted_events(batch)) for batch in iterator(**prefilter))

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
        # The identity test is redundant: the strong reference kept in keys pins this id, so a stored key always names this same row.
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


def outside_caller_limits(
    store: Any,
    identity: AgentIdentity | None,
    kind: str,
    row: Mapping[str, object],
) -> bool:
    """True when the caller has limits and may not read this stored row now.

    An exact door that names a row by id asks this before it builds a policy decision from the row's labels. A row the
    caller may not read is answered exactly as a row that does not exist: the policy decision repeats the labels of the
    row it judged, which is more than a missing id tells. The owner and an unbound admin key have no limits, so for them
    this is False and their doors keep the answers they had.
    """

    from alicebot_api.vnext_source_fence import SourceReadFence

    fence = SourceReadFence.for_identity(identity)
    if not fence.entity_read_fenced:
        return False
    effective = effective_row_for_fence(store, identity, kind, row)
    if canon_kind(kind) == "source":
        return not fence.admits(effective)
    return not fence.admits_memory(effective)


def readable_rows(store: Any, identity: AgentIdentity | None, rows: Sequence[_Row]) -> list[_Row]:
    """The rows this caller may read now, judged on their effective labels. Each row carries its own ``kind``.

    A count, a preview or any other answer built from rows must be built from these, so that the caller cannot learn
    how many rows exist above their limits. The owner and an unbound admin key are not limited and get every row.
    """

    from alicebot_api.vnext_source_fence import SourceReadFence

    fence = SourceReadFence.for_identity(identity)
    guard = LabelGuard.for_fence(store, fence)
    if not guard.active:
        return [row for row in rows if isinstance(row, Mapping)]
    kept: list[_Row] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        effective = guard.effective_row(str(row.get("kind") or ""), row)
        if isinstance(effective, Mapping) and fence.admits_memory(effective):
            kept.append(row)
    return kept


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


# The reach of a row feed for a caller with limits, the same bound as an event feed.
ROW_FEED_SCAN_LIMIT = EVENT_FEED_SCAN_LIMIT


def guard_for_caller(store: Any, identity: AgentIdentity | None, *, action: str = "http.operator.access") -> LabelGuard:
    """The list guard of one caller: the filters its policy allows and the project binding it is locked to.

    The owner and an unbound admin key have no limits, so their guard is inactive and returns its input. Every other
    caller gets the domains, the sensitivities and the projects the policy engine grants it for ``action``, the same
    ones a list door reads from a decision, and a key locked to a project must have every input of a row inside it.
    A decision that blocks the caller raises ``AgentPolicyBlockedError`` and no guard is made.
    """

    from alicebot_api.vnext_agent_control import AgentPolicyBlockedError, evaluate_agent_policy
    from alicebot_api.vnext_source_fence import SourceReadFence

    if not SourceReadFence.for_identity(identity).entity_read_fenced:
        return LabelGuard(store=store, active=False)
    decision = evaluate_agent_policy(identity=identity, action=action)
    if decision.decision == "blocked":
        raise AgentPolicyBlockedError(decision)
    projects = decision.effective_project_scope
    locked = identity is not None and identity.project_scope_locked
    return LabelGuard.for_filters(
        store,
        decision.effective_domains,
        decision.effective_sensitivity_allowed,
        projects,
        all_of=projects if locked else None,
    )


def newest_admitted_rows(
    guard: LabelGuard,
    kind: str,
    fetch: Callable[[int], Sequence[_Row]],
    *,
    want: int,
) -> list[_Row]:
    """The ``want`` newest rows ``guard`` admits. ``fetch(n)`` returns the newest ``n`` rows, newest first.

    A feed that read only its own limit would show a caller with limits fewer rows than it may read whenever the newest
    rows are hidden from it. The read widens, five times at a time, up to ``ROW_FEED_SCAN_LIMIT`` rows, so the feed is
    shorter than ``want`` only when fewer readable rows lie within that reach.
    """

    size = want
    while True:
        fetched = fetch(size)
        admitted = guard.admit_rows(kind, fetched)
        if len(admitted) >= want or len(fetched) < size or size >= ROW_FEED_SCAN_LIMIT:
            return admitted[:want]
        size = min(size * 5, ROW_FEED_SCAN_LIMIT)


def clamp_request_filters(
    identity: AgentIdentity | None,
    *,
    domains: Sequence[str],
    sensitivity_allowed: Sequence[str],
    action: str = "http.operator.access",
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The domains and sensitivities a request may use: what it asked for, cut down to what its caller may read.

    A request filter is a selection, never a grant. The owner and an unbound admin key get what they asked for. A caller
    with limits asking for a level above its ceiling is read at its own levels, as the policy engine answers it, and a
    request the policy blocks outright raises ``AgentPolicyBlockedError``.
    """

    from alicebot_api.vnext_agent_control import AgentPolicyBlockedError, evaluate_agent_policy

    asked_domains, asked_sensitivity = tuple(domains), tuple(sensitivity_allowed)
    if identity is None:
        return asked_domains, asked_sensitivity
    decision = evaluate_agent_policy(
        identity=identity, action=action, domains=asked_domains, sensitivity_allowed=asked_sensitivity
    )
    if decision.decision == "blocked":
        raise AgentPolicyBlockedError(decision)
    return decision.effective_domains, decision.effective_sensitivity_allowed


def readable_own_label_rows(identity: AgentIdentity | None, rows: Sequence[_Row]) -> list[_Row]:
    """The rows this caller may read, for rows that carry labels of their own and are made from no input.

    A queued task and the brain charter are not derived, so their stored labels are the labels they have. The owner and
    an unbound admin key get every row. A caller with limits gets the rows its fence admits: a row above its ceiling is
    left out, and so is a row with no project to a key locked to a project.
    """

    from alicebot_api.vnext_source_fence import SourceReadFence

    items = [row for row in rows if isinstance(row, Mapping)]
    fence = SourceReadFence.for_identity(identity)
    if not fence.entity_read_fenced:
        return items
    return [row for row in items if fence.admits_memory(row)]
