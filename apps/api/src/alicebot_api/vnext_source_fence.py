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

The same fence also decides what a reader is shown of a link that was stored
earlier (``SavedProvenanceReader``, at the end of this module). A link keeps a
copy of the quote it was made with, and so does the memory's own metadata, and a
source can be reclassified after the link was made: its sensitivity raised, its
domain changed, its project moved, or the source archived. The check at write
time then no longer holds, so every reader of a saved quote asks the fence again,
with the caller's current permission on the linked source, before it returns the
quote.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
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

    @property
    def fenced(self) -> bool:
        """True for a caller with an agent identity; False for the owner, who is shown what was stored."""

        return self.identity is not None

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


# -- reading what a link saved ---------------------------------------------------------------------------------------

# Where a memory keeps its own copy of what a link holds. ``provenance`` is written by an edit-and-approve review,
# ``replacement_provenance`` by a supersede review, and ``agentic_memory.conversation_excerpt`` by the commit route
# (which also stores it as the quote of each link). Each is a copy of text taken from a source, so each is withheld
# whenever the memory cites a source the caller may not read.
_PROVENANCE_OBJECT_KEYS = ("provenance", "replacement_provenance")
_AGENTIC_MEMORY_KEY = "agentic_memory"
_EXCERPT_KEY = "conversation_excerpt"
# The refs a commit (and a memory proposal) was given, kept on the row as sent. An entry that names a source the
# caller may not read is dropped from the list.
_SOURCE_REFS_KEY = "source_refs"
_REVISION_VALUE_KEYS = ("previous_value", "new_value")


def _canonical_source_id(value: object) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(str(value).strip()))
    except ValueError:
        return None


def _without_refused_refs(refs: object, refused: frozenset[str]) -> object:
    """``refs`` without the entries that name a refused source. The same list when nothing is dropped."""

    if not refused or not isinstance(refs, list):
        return refs
    kept = [ref for ref in refs if refused.isdisjoint(source_uuids_in_ref(ref))]
    return refs if len(kept) == len(refs) else kept


def _source_ids_named_by_memory_copies(row: Mapping[str, object]) -> set[str]:
    """Every source id the memory's own copies of its provenance name."""

    named: set[str] = set()
    metadata = row.get("metadata_json")
    if isinstance(metadata, Mapping):
        for key in _PROVENANCE_OBJECT_KEYS:
            named.update(source_uuids_in_ref(metadata.get(key)))
        named.update(source_uuids_in_ref(metadata.get(_SOURCE_REFS_KEY)))
        agentic = metadata.get(_AGENTIC_MEMORY_KEY)
        if isinstance(agentic, Mapping):
            named.update(source_uuids_in_ref(agentic.get(_SOURCE_REFS_KEY)))
    value = row.get("value")
    if isinstance(value, Mapping):
        named.update(source_uuids_in_ref(value.get(_SOURCE_REFS_KEY)))
    return named


def _source_ids_named_by_revision(row: Mapping[str, object]) -> set[str]:
    named: set[str] = set()
    for key in _REVISION_VALUE_KEYS:
        value = row.get(key)
        if isinstance(value, Mapping):
            named.update(source_uuids_in_ref(value.get(_SOURCE_REFS_KEY)))
    return named


def _memory_without_refused_provenance(
    row: Mapping[str, object], *, refused: frozenset[str], withhold_quotes: bool
) -> dict[str, object]:
    out = dict(row)
    metadata = row.get("metadata_json")
    if isinstance(metadata, Mapping):
        copy = dict(metadata)
        if withhold_quotes:
            for key in _PROVENANCE_OBJECT_KEYS:
                copy.pop(key, None)
        if _SOURCE_REFS_KEY in copy:
            copy[_SOURCE_REFS_KEY] = _without_refused_refs(copy[_SOURCE_REFS_KEY], refused)
        agentic = copy.get(_AGENTIC_MEMORY_KEY)
        if isinstance(agentic, Mapping):
            inner = dict(agentic)
            if withhold_quotes:
                inner.pop(_EXCERPT_KEY, None)
            if _SOURCE_REFS_KEY in inner:
                inner[_SOURCE_REFS_KEY] = _without_refused_refs(inner[_SOURCE_REFS_KEY], refused)
            copy[_AGENTIC_MEMORY_KEY] = inner
        out["metadata_json"] = copy
    value = row.get("value")
    if isinstance(value, Mapping) and _SOURCE_REFS_KEY in value:
        out["value"] = {**value, _SOURCE_REFS_KEY: _without_refused_refs(value[_SOURCE_REFS_KEY], refused)}
    return out


def _revision_without_refused_refs(row: Mapping[str, object], *, refused: frozenset[str]) -> dict[str, object]:
    out = dict(row)
    for key in _REVISION_VALUE_KEYS:
        value = row.get(key)
        if isinstance(value, Mapping) and _SOURCE_REFS_KEY in value:
            out[key] = {**value, _SOURCE_REFS_KEY: _without_refused_refs(value[_SOURCE_REFS_KEY], refused)}
    return out


def _is_memory_row(value: Mapping[str, object]) -> bool:
    return "id" in value and "memory_key" in value and isinstance(value.get("metadata_json"), Mapping)


def _is_revision_row(value: Mapping[str, object]) -> bool:
    return "memory_id" in value and "revision_number" in value and "revision_type" in value


class SavedProvenanceReader:
    """What one caller may be shown of the provenance saved on memories, asked once per read.

    A link stores the quote it was made with, and a memory keeps copies of that quote in its metadata. The source the
    link names can be reclassified (sensitivity raised, domain changed, project moved) or archived after that, so the
    fence that admitted the link when it was written says nothing about the next reader. This object asks ``fence``
    again, with the source as it is now, and withholds what the caller may not read:

    * a link whose source is missing, archived or outside the fence, and a link that names no source at all, is left
      out, as a link that was never stored would be;
    * when a memory has such a link, or its own copies name such a source, its copies of the quote
      (``metadata_json.provenance``, ``metadata_json.replacement_provenance`` and
      ``metadata_json.agentic_memory.conversation_excerpt``) are removed, and the entries of the ref lists that name
      the source (``source_refs`` in the metadata, in ``agentic_memory`` and in ``value``, and the same list in a
      revision's ``previous_value`` and ``new_value``) are dropped.

    The owner (``fence.fenced`` is false) is shown what was stored: every method returns its input unchanged and reads
    nothing it was not asked to. A memory whose sources are all admitted is returned as the same object.

    ``fence`` is a required keyword-only argument, with no default.
    """

    def __init__(self, store: object, *, fence: SourceReadFence) -> None:
        self._store = store
        self._fence = fence
        self._links: dict[str, list[dict[str, object]]] = {}
        self._admitted: dict[str, bool] = {}

    @property
    def fenced(self) -> bool:
        return self._fence.fenced

    # -- links ---------------------------------------------------------------------------------------------------

    def links(self, memory_id: object) -> list[dict[str, object]]:
        """The links of ``memory_id`` that the fence admits, in the order the store lists them."""

        key = str(memory_id)
        self._load_links([key])
        raw = self._links[key]
        if not self.fenced:
            return list(raw)
        self.judge_links(raw)
        return [link for link in raw if self.admits_link(link)]

    def judge_links(self, links: Iterable[Mapping[str, object]]) -> None:
        """Look up the source of every link in one read, so ``admits_link`` does not read one at a time."""

        if self.fenced:
            self._judge(
                source_id for link in links if (source_id := _canonical_source_id(link.get("source_id"))) is not None
            )

    def admits_link(self, link: Mapping[str, object]) -> bool:
        """True when the caller may be shown ``link``: its source exists and the fence admits it."""

        if not self.fenced:
            return True
        source_id = _canonical_source_id(link.get("source_id"))
        if source_id is None:
            return False
        self._judge([source_id])
        return self._admitted.get(source_id, False)

    # -- memories and revisions ----------------------------------------------------------------------------------

    def memory(self, row: Mapping[str, object]) -> dict[str, object]:
        """``row`` with the saved provenance the caller may not read withheld."""

        if not self.fenced:
            return row  # type: ignore[return-value]
        self._prefetch(memories=[row], revisions=[])
        return self._memory(row)

    def memories(self, rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
        """Every row of ``rows`` passed through ``memory``, with one read for all of them.

        Each row is taken to be a memory row whatever its shape, so a row that lacks a field a stored memory always
        has is still held to the fence.
        """

        if not self.fenced:
            return list(rows)  # type: ignore[arg-type]
        self._prefetch(memories=rows, revisions=[])
        return [self._memory(row) for row in rows]

    def revision(self, row: Mapping[str, object]) -> dict[str, object]:
        """``row`` (a memory revision) with the refs of a refused source dropped."""

        if not self.fenced:
            return row  # type: ignore[return-value]
        self._prefetch(memories=[], revisions=[row])
        return self._revision(row)

    def tree(self, payload: object) -> object:
        """``payload`` with every memory row and revision row inside it passed through ``memory`` and ``revision``."""

        if not self.fenced:
            return payload
        memories: list[Mapping[str, object]] = []
        revisions: list[Mapping[str, object]] = []
        _collect_rows(payload, memories, revisions)
        if not memories and not revisions:
            return payload
        self._prefetch(memories=memories, revisions=revisions)
        return self._rebuild(payload)

    # -- internals -----------------------------------------------------------------------------------------------

    def _memory(self, row: Mapping[str, object]) -> dict[str, object]:
        memory_id = str(row.get("id") or "")
        links = self._links.get(memory_id, [])
        named = {
            source_id for link in links if (source_id := _canonical_source_id(link.get("source_id"))) is not None
        } | _source_ids_named_by_memory_copies(row)
        refused = frozenset(source_id for source_id in named if not self._admitted.get(source_id, False))
        withhold_quotes = bool(refused) or any(_canonical_source_id(link.get("source_id")) is None for link in links)
        if not withhold_quotes:
            return row  # type: ignore[return-value]
        return _memory_without_refused_provenance(row, refused=refused, withhold_quotes=withhold_quotes)

    def _revision(self, row: Mapping[str, object]) -> dict[str, object]:
        named = _source_ids_named_by_revision(row)
        refused = frozenset(source_id for source_id in named if not self._admitted.get(source_id, False))
        if not refused:
            return row  # type: ignore[return-value]
        return _revision_without_refused_refs(row, refused=refused)

    def _prefetch(
        self, *, memories: Sequence[Mapping[str, object]], revisions: Sequence[Mapping[str, object]]
    ) -> None:
        """Read the links of every memory row and the sources they and the rows' own copies name, once."""

        self._load_links([str(row.get("id") or "") for row in memories])
        wanted: list[str] = []
        for row in memories:
            wanted.extend(_source_ids_named_by_memory_copies(row))
            for link in self._links.get(str(row.get("id") or ""), []):
                source_id = _canonical_source_id(link.get("source_id"))
                if source_id is not None:
                    wanted.append(source_id)
        for row in revisions:
            wanted.extend(_source_ids_named_by_revision(row))
        self._judge(wanted)

    def _load_links(self, memory_ids: Sequence[str]) -> None:
        missing = [memory_id for memory_id in dict.fromkeys(memory_ids) if memory_id and memory_id not in self._links]
        if not missing:
            return
        grouped: dict[str, list[dict[str, object]]] = {memory_id: [] for memory_id in missing}
        bulk = getattr(self._store, "list_provenance_links_for_targets", None)
        if callable(bulk):
            for link in bulk(target_type="memory", target_ids=missing):
                grouped.setdefault(str(link.get("target_id")), []).append(link)
        else:
            single = getattr(self._store, "list_provenance_links", None)
            if callable(single):
                for memory_id in missing:
                    grouped[memory_id] = list(single(target_type="memory", target_id=memory_id))
        self._links.update(grouped)

    def _judge(self, source_ids: Iterable[str]) -> None:
        unknown = [source_id for source_id in dict.fromkeys(source_ids) if source_id not in self._admitted]
        if not unknown:
            return
        rows = _rows_by_id(self._store, unknown)
        for source_id in unknown:
            row = rows.get(source_id)
            self._admitted[source_id] = row is not None and self._fence.admits(row)

    def _rebuild(self, value: object) -> object:
        if isinstance(value, Mapping):
            if _is_memory_row(value):
                return self._memory(value)
            if _is_revision_row(value):
                return self._revision(value)
            changed = False
            rebuilt: dict[object, object] = {}
            for key, child in value.items():
                new_child = self._rebuild(child)
                changed = changed or new_child is not child
                rebuilt[key] = new_child
            return rebuilt if changed else value
        if isinstance(value, (list, tuple)):
            items = [self._rebuild(child) for child in value]
            if all(new is old for new, old in zip(items, value, strict=True)):
                return value
            return items if isinstance(value, list) else tuple(items)
        return value


def _collect_rows(
    value: object, memories: list[Mapping[str, object]], revisions: list[Mapping[str, object]]
) -> None:
    if isinstance(value, Mapping):
        if _is_memory_row(value):
            memories.append(value)
            return
        if _is_revision_row(value):
            revisions.append(value)
            return
        for child in value.values():
            _collect_rows(child, memories, revisions)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _collect_rows(child, memories, revisions)


__all__ = [
    "AttachableSources",
    "EXPLAIN_DISCLOSURE_ACTION",
    "MEMORY_REF_NOT_FOUND_MESSAGE",
    "MemoryRefNotFoundError",
    "SOURCE_REF_NOT_FOUND_MESSAGE",
    "SavedProvenanceReader",
    "SourceReadFence",
    "SourceRefNotFoundError",
    "resolve_attachable_memory_id",
    "resolve_attachable_source_id",
    "resolve_attachable_sources",
    "source_uuids_in_ref",
]
