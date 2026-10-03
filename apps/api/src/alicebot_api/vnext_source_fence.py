"""Which sources and memories a write may cite by id.

A write that names a source by id stores a link that later readers follow: a
commit's ``source_refs`` and a review's ``provenance`` both end in a row of
``provenance_links``, and an open loop created over HTTP stores the ``source_id``
and ``memory_id`` it was given in columns of its own. Until now each checked at
most that the row existed for the acting user. A key bound to one project could
therefore attach a source of another project, a source above its sensitivity
ceiling, a source in a domain its profile may not read, or a global one, and an
id that does not exist answered differently from one that does, so an id was an
existence oracle.

The rule here is the read fence of the caller: a source (or a memory) may be
attached only if the same caller could be shown it. The fence is the one
``alice_explain`` applies to every source and memory it discloses (the policy
engine, asked with the row's own domain, sensitivity and project scope, and
required to answer "allowed" with no filtering), so a link that passes can never
make that caller's own explain of the memory fail closed. A row that is deleted,
missing or outside the fence is refused the same way: one exception class, one
message, raised at one site per kind, so nothing in the answer tells the three
apart.

``fence`` is a required keyword-only argument of every function here that
resolves ids, with no default. ``SourceReadFence.unfenced()`` is the owner's
fence (a call with no agent identity) and is written at the call site, where a
reviewer sees it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from alicebot_api.vnext_agent_control import AgentIdentity, evaluate_agent_policy, resource_project_scope
from alicebot_api.vnext_project_scope import source_project_scope

SOURCE_REF_NOT_FOUND_MESSAGE = "the cited source was not found in the current user scope"
MEMORY_REF_NOT_FOUND_MESSAGE = "the cited memory was not found in the current user scope"

# The action ``alice_explain`` authorizes each source and memory it discloses
# under. It is a read action, so the engine adds no write-side rule to it. The
# explain tool and this fence both read it from here, so they cannot drift apart.
EXPLAIN_DISCLOSURE_ACTION = "memory.audit"


class SourceRefNotFoundError(ValueError):
    """A cited source is missing, deleted or outside the caller's read fence.

    The three cases raise this one class with ``SOURCE_REF_NOT_FOUND_MESSAGE``.
    """

    def __init__(self) -> None:
        super().__init__(SOURCE_REF_NOT_FOUND_MESSAGE)


class MemoryRefNotFoundError(ValueError):
    """A cited memory is missing, deleted, malformed or outside the caller's read fence.

    The cases raise this one class with ``MEMORY_REF_NOT_FOUND_MESSAGE``.
    """

    def __init__(self) -> None:
        super().__init__(MEMORY_REF_NOT_FOUND_MESSAGE)


@dataclass(frozen=True, slots=True)
class SourceReadFence:
    """What one caller may read of the sources and memories, as the policy engine says it."""

    identity: AgentIdentity | None

    @classmethod
    def for_identity(cls, identity: AgentIdentity | None) -> SourceReadFence:
        """The fence of a resolved caller. ``None`` is the owner, who is not fenced."""

        if identity is None:
            return cls.unfenced()
        return cls(identity=identity)

    @classmethod
    def unfenced(cls) -> SourceReadFence:
        """The owner's fence: no agent identity, so only a deleted source is refused."""

        return cls(identity=None)

    def admits(self, source: Mapping[str, object]) -> bool:
        """True when this caller may be shown ``source``."""

        return self._admits(source, project_scope=source_project_scope(source))

    def admits_memory(self, memory: Mapping[str, object]) -> bool:
        """True when this caller may be shown ``memory``.

        The project scope is read the way ``alice_explain`` reads it for a memory
        it was given by id: ``resource_project_scope``.
        """

        return self._admits(memory, project_scope=resource_project_scope(memory))

    def _admits(self, row: Mapping[str, object], *, project_scope: tuple[str, ...]) -> bool:
        if row.get("deleted_at") is not None:
            return False
        if self.identity is None:
            return True
        decision = evaluate_agent_policy(
            identity=self.identity,
            action=EXPLAIN_DISCLOSURE_ACTION,
            domains=(str(row.get("domain") or "unknown"),),
            sensitivity_allowed=(str(row.get("sensitivity") or "unknown"),),
            project_scope=project_scope,
            require_explicit_project_scope=True,
        )
        # "allowed_with_filtering" is a refusal here, as it is for explain: the
        # row's own labels are what was asked, and a filtered answer means one
        # of them is outside what the caller may read.
        return decision.decision == "allowed"


def _source_ref_values(value: object) -> list[str]:
    refs: list[str] = []
    if isinstance(value, str):
        if value.strip():
            refs.append(value.strip())
    elif isinstance(value, Mapping):
        for key in ("source_id", "id", "ref", "source_ref"):
            candidate = value.get(key)
            if isinstance(candidate, (str, int)):
                refs.append(str(candidate))
        for nested_key in ("source_ids", "source_refs", "sources"):
            refs.extend(_source_ref_values(value.get(nested_key)))
    elif isinstance(value, (list, tuple)):
        for item in value:
            refs.extend(_source_ref_values(item))
    return refs


def source_uuids_in_ref(value: object) -> list[str]:
    """Every source id a ref names, in order: ``source:`` prefix removed, lower-cased, deduplicated.

    A ref that names no UUID (a URL, a free-form label) is not a reference to a
    stored source and yields nothing.
    """

    found: list[str] = []
    for ref in _source_ref_values(value):
        normalized = ref.removeprefix("source:")
        try:
            source_id = str(UUID(normalized))
        except ValueError:
            continue
        if source_id not in found:
            found.append(source_id)
    return found


@dataclass(frozen=True, slots=True)
class AttachableSources:
    """Source ids that passed the fence, in the order a write should link them.

    Only ``resolve_attachable_sources`` builds one. A function that creates
    provenance links takes this and not a list of ids, so a caller cannot hand
    it ids that were never checked.
    """

    ids: tuple[str, ...]


def resolve_attachable_sources(
    store: object,
    refs: Sequence[object],
    *,
    fence: SourceReadFence,
) -> AttachableSources:
    """Check every source id in ``refs`` against ``fence`` and return the ones to link.

    Every id any ref names is checked, not only the first one a link would use,
    so a second id inside a nested ref cannot ride along into the stored row.
    The link goes to the first id of each ref, as a commit always did. Raises
    ``SourceRefNotFoundError`` for the first id that is missing, deleted or
    outside the fence, before the caller has written anything.
    """

    wanted: list[str] = []
    linked: list[str] = []
    for ref in refs:
        named = source_uuids_in_ref(ref)
        for source_id in named:
            if source_id not in wanted:
                wanted.append(source_id)
        if named and named[0] not in linked:
            linked.append(named[0])
    if not wanted:
        return AttachableSources(ids=())
    rows = _rows_by_id(store, wanted)
    for source_id in wanted:
        row = rows.get(source_id)
        if row is None or not fence.admits(row):
            raise SourceRefNotFoundError()
    return AttachableSources(ids=tuple(linked))


def resolve_attachable_source_id(store: object, source_id: str, *, fence: SourceReadFence) -> str:
    """Check the one source id a write keeps in a column of its own and return it in canonical form.

    An open loop stores ``source_id`` directly, with no ``provenance_links`` row.
    The value the caller sent must name a stored source that ``fence`` admits.
    A value that names no id at all (it is not a UUID in any form the link
    writer reads) is refused as a missing source is, so a malformed id and an
    unknown one answer alike. The write must store the id returned here and not
    the raw string, so what was checked is what is stored.
    """

    attachable = resolve_attachable_sources(store, [source_id], fence=fence)
    if not attachable.ids:
        raise SourceRefNotFoundError()
    return attachable.ids[0]


def resolve_attachable_memory_id(store: object, memory_id: str, *, fence: SourceReadFence) -> str:
    """Check the one memory id a write keeps in a column of its own and return it in canonical form.

    The memory must exist and not be deleted, and ``fence`` must admit it by the
    test ``alice_explain`` applies to a memory. Missing, deleted, malformed and
    out-of-fence ids raise one ``MemoryRefNotFoundError``. The write must store
    the id returned here.
    """

    try:
        canonical = str(UUID(str(memory_id).strip()))
    except ValueError:
        raise MemoryRefNotFoundError() from None
    getter = getattr(store, "get_memory", None)
    row = getter(canonical) if callable(getter) else None
    if not isinstance(row, Mapping) or not fence.admits_memory(row):
        raise MemoryRefNotFoundError()
    return canonical


def _rows_by_id(store: object, source_ids: Sequence[str]) -> dict[str, Mapping[str, object]]:
    bulk = getattr(store, "get_sources_by_ids", None)
    if callable(bulk):
        return {str(row.get("id")): row for row in bulk(list(source_ids)) if isinstance(row, Mapping)}
    single = getattr(store, "get_source", None)
    if not callable(single):
        return {}
    found: dict[str, Mapping[str, object]] = {}
    for source_id in source_ids:
        row = single(source_id)
        if isinstance(row, Mapping):
            found[source_id] = row
    return found


__all__ = [
    "AttachableSources",
    "EXPLAIN_DISCLOSURE_ACTION",
    "MEMORY_REF_NOT_FOUND_MESSAGE",
    "MemoryRefNotFoundError",
    "SOURCE_REF_NOT_FOUND_MESSAGE",
    "SourceReadFence",
    "SourceRefNotFoundError",
    "resolve_attachable_memory_id",
    "resolve_attachable_source_id",
    "resolve_attachable_sources",
    "source_uuids_in_ref",
]
