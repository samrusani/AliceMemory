"""Canonical multi-project scope helpers for vNext resources."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Mapping, Sequence

from alicebot_api.vnext_repositories import JsonObject


#: The reserved marker of per-project memory (spec 6.1). It exists only inside a
#: request tuple, where it asks for rows whose project scope holds no Alice
#: project id. It is never stored on a note, a source or an open loop, it is never
#: compared against a stored value, and it is refused as caller input. A stored
#: string that happens to read ``~global`` is an ordinary free-form name.
GLOBAL_PROJECT_MARKER = "~global"
_ALICE_PROJECT_ID_PATTERN = re.compile(r"prj_[0-9a-f]{16}")

_ASCII_PROJECT_WHITESPACE = frozenset(" \t\n\r\f\v")
_ASCII_PROJECT_CASE_TRANSLATION = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)


def _project_scalar_text(value: object) -> str | None:
    """Return the shared JSON-scalar spelling accepted for project ids.

    Boolean support is intentional and predates numeric scope support; keep
    its title-case spelling so the existing Python/SQLite/PostgreSQL identity
    remains stable. Numeric JSON values are accepted only when finite and
    mathematically integral. Their fixed decimal spelling makes lexical forms
    such as ``1``, ``1.0``, and ``1e0`` one identity across all stores.
    """

    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)

    numeric: Decimal
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        try:
            # str(float) is also the JSON encoder's stable round-trip spelling.
            numeric = Decimal(str(value))
        except InvalidOperation:  # pragma: no cover - finite float backstop
            return None
    elif isinstance(value, Decimal):
        numeric = value
    else:
        return None

    if not numeric.is_finite() or numeric != numeric.to_integral_value():
        return None
    integral = numeric.to_integral_value()
    if integral == 0:
        return "0"
    return format(integral, "f")


def normalize_project_identifier(value: object) -> str:
    """Normalize only the explicitly supported ASCII project whitespace.

    Space, tab, line feed, vertical tab, form feed, and carriage return are
    collapsed to one ASCII space and trimmed at the edges.  Every non-ASCII
    code point, including Unicode whitespace, is preserved exactly.  This is
    intentionally narrower than :meth:`str.split` so Python, SQLite, and
    PostgreSQL cannot disagree because of Unicode or locale tables.
    """

    scalar = _project_scalar_text(value)
    if scalar is None:
        return ""
    normalized: list[str] = []
    pending_space = False
    for character in scalar:
        if character in _ASCII_PROJECT_WHITESPACE:
            pending_space = bool(normalized)
            continue
        if pending_space:
            normalized.append(" ")
            pending_space = False
        normalized.append(character)
    return "".join(normalized)


def project_identifier_identity(value: object) -> str:
    """Return one conservative project-identifier comparison key.

    ASCII-only identifiers compare case-insensitively.  If any non-ASCII code
    point remains after ASCII-whitespace normalization, the identifier is kept
    exact and case-sensitive instead of relying on backend-specific Unicode
    case mappings.
    """

    normalized = normalize_project_identifier(value)
    if normalized.isascii():
        return normalized.translate(_ASCII_PROJECT_CASE_TRANSLATION)
    return normalized


def normalize_project_scope(value: object) -> tuple[str, ...]:
    if value is None or type(value) in (list, tuple) and not value:
        return ()
    values: list[str] = []

    def add(item: object) -> None:
        if _project_scalar_text(item) is not None:
            normalized = normalize_project_identifier(item)
            if normalized:
                values.append(normalized)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for nested in item:
                add(nested)

    add(value)
    return tuple(dict.fromkeys(values))


def project_scope_identity(value: object) -> tuple[str, ...]:
    """Return the deterministic comparison key for a project scope.

    Persisted values keep their display spelling and first-seen ordering via
    :func:`normalize_project_scope`.  Identity comparisons are deliberately a
    separate operation: project scope is a set, ASCII identifiers compare
    case-insensitively, and identifiers containing non-ASCII compare exactly.
    The sorted tuple uses Unicode code-point order; its UTF-8 byte ordering is
    identical to PostgreSQL's explicit ``C`` collation for valid text.
    """

    return tuple(sorted({project_identifier_identity(item) for item in normalize_project_scope(value)}))


@dataclass(frozen=True, slots=True)
class ProjectScopeResolution:
    """A resolved resource scope which retains canonical-key presence.

    ``present`` distinguishes an absent canonical representation (where
    legacy fallbacks remain valid) from an explicitly empty or malformed
    canonical value (which is authoritative and must fail closed).
    """

    present: bool
    values: tuple[str, ...]

    @property
    def identity(self) -> tuple[str, ...]:
        return project_scope_identity(self.values)


def resolve_project_scope(resource: Mapping[str, object] | None) -> ProjectScopeResolution:
    """Resolve one resource's scope without widening canonical values.

    Canonical ``project_scope`` values are checked at the resource root, then
    in ``metadata_json`` and ``scope_json``.  Presence is authoritative even
    for ``[]``, ``null``, or a malformed value.  Only rows with no canonical
    key use the historical singular/nested aliases.
    """

    if resource is None:
        return ProjectScopeResolution(present=False, values=())

    def canonical_values(value: object) -> tuple[str, ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return ()
        return normalize_project_scope(value)

    containers = tuple(
        container
        for container_key in ("metadata_json", "scope_json")
        if isinstance((container := resource.get(container_key)), Mapping)
    )
    if "project_scope" in resource:
        return ProjectScopeResolution(
            present=True,
            values=canonical_values(resource.get("project_scope")),
        )
    for container in containers:
        if "project_scope" in container:
            return ProjectScopeResolution(
                present=True,
                values=canonical_values(container.get("project_scope")),
            )

    nested_scope: list[object] = []
    nested_scope_present = False
    for container in containers:
        agentic = container.get("agentic_memory")
        if isinstance(agentic, Mapping) and "project_scope" in agentic:
            nested_scope_present = True
            nested_scope.append(agentic.get("project_scope"))
        agent_identity = container.get("agent_identity")
        if isinstance(agent_identity, Mapping) and "project_scope" in agent_identity:
            nested_scope_present = True
            nested_scope.append(agent_identity.get("project_scope"))
    if nested_scope_present:
        return ProjectScopeResolution(
            present=True,
            values=normalize_project_scope(nested_scope),
        )

    direct_scope = normalize_project_scope([resource.get(key) for key in ("project_id", "project", "projects")])
    if direct_scope:
        return ProjectScopeResolution(present=False, values=direct_scope)

    legacy_values: list[object] = []
    for container in containers:
        for key in ("project_id", "project", "projects"):
            legacy_values.append(container.get(key))
        agentic = container.get("agentic_memory")
        if isinstance(agentic, Mapping):
            for key in ("project_id", "project", "projects"):
                legacy_values.append(agentic.get(key))
    return ProjectScopeResolution(
        present=False,
        values=normalize_project_scope(legacy_values),
    )


def resolve_source_metadata_project_scope(
    metadata_json: Mapping[str, object] | None,
) -> ProjectScopeResolution:
    """Adapt persisted source metadata to the universal scope resolver.

    Historical source writers persisted either a complete resource-shaped
    scope envelope (root fields plus optional ``metadata_json``/``scope_json``
    containers) or the contents of the metadata container itself.  Direct
    ``agentic_memory`` and ``agent_identity`` objects are therefore merged into
    the nested metadata container before resolving.  An explicitly stored
    container value wins on key collisions, while every canonical presence and
    fallback tier remains governed by :func:`resolve_project_scope`.
    """

    if not isinstance(metadata_json, Mapping):
        return ProjectScopeResolution(present=False, values=())

    resource = dict(metadata_json)
    stored_container = resource.get("metadata_json")
    metadata_container = dict(stored_container) if isinstance(stored_container, Mapping) else {}
    for nested_key in ("agentic_memory", "agent_identity"):
        direct_nested = resource.get(nested_key)
        if not isinstance(direct_nested, Mapping):
            continue
        stored_nested = metadata_container.get(nested_key)
        metadata_container[nested_key] = (
            {**direct_nested, **stored_nested} if isinstance(stored_nested, Mapping) else dict(direct_nested)
        )
    resource["metadata_json"] = metadata_container
    return resolve_project_scope(resource)


def source_project_scope(source: Mapping[str, object]) -> tuple[str, ...]:
    """Return scope from a persisted source row's metadata envelope.

    Unlike memories and artifacts, a source stores the complete historical
    scope envelope inside its ``metadata_json`` column.  Passing the source row
    itself to :func:`resolve_project_scope` would therefore let a stale outer
    alias win over an authoritative canonical value inside that envelope.
    """

    metadata_json = source.get("metadata_json")
    resolution = resolve_source_metadata_project_scope(metadata_json if isinstance(metadata_json, Mapping) else None)
    if resolution.present or resolution.values:
        return resolution.values
    # Some pre-envelope adapters exposed the singular project alias directly
    # on the source row.  Preserve that compatibility only when the persisted
    # envelope contains no canonical key and resolves to no legacy scope.
    return resolve_project_scope(source).values


def source_capture_identity_matches(
    source: Mapping[str, object],
    *,
    content_hashes: Sequence[str],
    project_scope: object,
    domain: object,
    sensitivity: object,
) -> bool:
    """Validate a persisted source against one requested capture identity.

    Lookup keys are only candidate selectors: source review can reassign scope
    or classification, and historical rows may carry a stale key. Every fast,
    legacy-hash, and atomic-conflict candidate therefore revalidates the
    current persisted envelope before it may be returned as a duplicate.
    """

    expected_hashes = {str(value) for value in content_hashes if str(value)}
    if expected_hashes and str(source.get("content_hash") or "") not in expected_hashes:
        return False
    if project_scope_identity(source_project_scope(source)) != project_scope_identity(project_scope):
        return False

    def classification(value: object) -> str:
        return str(value or "unknown").strip().casefold()

    return classification(source.get("domain")) == classification(domain) and classification(
        source.get("sensitivity")
    ) == classification(sensitivity)


def memory_project_scope(memory: Mapping[str, object]) -> tuple[str, ...]:
    """Return a memory's presence-aware canonical/legacy project scope."""

    return resolve_project_scope(memory).values


def canonical_memory_metadata(memory: Mapping[str, object]) -> JsonObject:
    metadata = memory.get("metadata_json")
    result = dict(metadata) if isinstance(metadata, Mapping) else {}
    resolution = resolve_project_scope(memory)
    if resolution.present or resolution.values:
        result["project_scope"] = list(resolution.values)
    return result


def expose_memory_project_scope(row: JsonObject) -> JsonObject:
    if "memory_key" in row and "canonical_text" in row:
        row["project_scope"] = list(memory_project_scope(row))
    return row


def is_alice_project_id(value: object) -> bool:
    """True for ``prj_`` and exactly 16 lowercase hex characters, after ASCII folding.

    That is an id the resolver in ``project_identity`` derives (spec 4.1). Every
    other stored string is a free-form name.
    """

    if not isinstance(value, str):
        return False
    return _ALICE_PROJECT_ID_PATTERN.fullmatch(project_identifier_identity(value)) is not None


def is_global_scope(scope: object) -> bool:
    """True when a scope holds no Alice project id: empty, or free-form names only (spec 4.1)."""

    return not any(is_alice_project_id(item) for item in project_scope_identity(scope))


def holds_global_marker(scope: object) -> bool:
    """True when a request tuple holds the reserved marker, however it is spelled."""

    return GLOBAL_PROJECT_MARKER in project_scope_identity(scope)


def refuse_global_marker(scope: object, *, where: str) -> None:
    """Raise ``ValueError`` when ``scope`` holds the reserved marker.

    For the helpers that take explicit project names only. A helper that did not
    know the marker would read it as a name no row holds and hide every global
    note without saying so, so it refuses. The message is fixed words and the name
    of the caller, never the request.
    """

    if holds_global_marker(scope):
        raise ValueError(f"{where} takes explicit project names only and does not accept the global marker")


def project_floor(resource: Mapping[str, object] | None) -> tuple[str, ...]:
    """Return the stored ``project_floor`` list, or ``()`` when the key is absent.

    A present value that is not a list of strings is not a floor. Callers that
    must refuse that shape ask :func:`project_floor_shape` first. This helper
    returns ``()`` for it so a selection door does not treat a bad value as names.
    """

    shape, values = project_floor_shape(resource)
    if shape != "list":
        return ()
    return values


def project_floor_shape(resource: Mapping[str, object] | None) -> tuple[str, tuple[str, ...]]:
    """Classify ``project_floor`` as ``absent``, ``list`` or ``malformed``.

    ``list`` carries the stored spellings, ordered by identity, with no duplicates.
    """

    if resource is None:
        return "absent", ()
    containers: list[Mapping[str, object]] = [resource]
    metadata = resource.get("metadata_json")
    if isinstance(metadata, Mapping):
        containers.append(metadata)
    seen = False
    raw: object = None
    for container in containers:
        if "project_floor" in container:
            seen = True
            raw = container.get("project_floor")
            break
    if not seen:
        return "absent", ()
    if type(raw) in (list, tuple) and not raw:
        return "list", ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return "malformed", ()
    if any(not isinstance(item, str) for item in raw):
        return "malformed", ()
    return "list", _ordered_scope(raw)


def project_floor_within(floor: object, binding: object) -> bool:
    """True when every project in ``floor`` is inside ``binding``, by identity.

    An empty floor is inside every binding. A free-form name is inside only when
    the binding names that same identity.
    """

    return set(project_scope_identity(floor)).issubset(set(project_scope_identity(binding)))


def _ordered_scope(value: object) -> tuple[str, ...]:
    """Stored spellings, first one kept, ordered by identity."""

    chosen: dict[str, str] = {}
    for item in normalize_project_scope(value):
        identity = project_identifier_identity(item)
        if identity and identity not in chosen:
            chosen[identity] = item
    return tuple(chosen[identity] for identity in sorted(chosen))


def project_scopes_overlap(
    resource_scope: object,
    requested_scope: object,
    *,
    floor: object = (),
) -> bool:
    """Does the resource's scope meet the requested tuple?

    The tuple may hold the reserved marker. The marker asks for a resource whose
    scope holds no Alice project id, and is never compared with a stored value.
    Every other entry is an identifier compared by identity.

    ``floor`` is the row's project floor. It is consulted only on the global
    branch: a row whose scope holds no Alice project id is in a view that asks
    for global rows only when every Alice project id in the floor is in the
    view. Free-form names in the floor do not hide the row. An empty floor keeps
    the previous answer.
    """

    requested = set(project_scope_identity(requested_scope))
    if not requested:
        return False
    resource = project_scope_identity(resource_scope)
    if GLOBAL_PROJECT_MARKER in requested:
        requested.discard(GLOBAL_PROJECT_MARKER)
        if not any(is_alice_project_id(item) for item in resource):
            floor_ids = {item for item in project_scope_identity(floor) if is_alice_project_id(item)}
            if floor_ids.issubset(requested):
                return True
    return bool(requested.intersection(resource))


__all__ = [
    "GLOBAL_PROJECT_MARKER",
    "canonical_memory_metadata",
    "expose_memory_project_scope",
    "holds_global_marker",
    "is_alice_project_id",
    "is_global_scope",
    "memory_project_scope",
    "normalize_project_identifier",
    "normalize_project_scope",
    "ProjectScopeResolution",
    "project_floor",
    "project_floor_shape",
    "project_floor_within",
    "project_identifier_identity",
    "project_scope_identity",
    "project_scopes_overlap",
    "refuse_global_marker",
    "resolve_project_scope",
    "resolve_source_metadata_project_scope",
    "source_capture_identity_matches",
    "source_project_scope",
]
