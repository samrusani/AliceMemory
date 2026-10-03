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

One quote has several copies, and they are all withheld together. The commit route
saves one ``conversation_excerpt`` as the quote of the link it makes and as
``agentic_memory.conversation_excerpt``; an edit-and-approve review saves its
``provenance.quote`` as the quote of its link and as ``metadata_json.provenance.quote``;
a supersede review does the same with ``replacement_provenance``. The match between a
link's quote and a copy was derived by running each of those writers on SQLite
(``tests/unit/test_saved_quote_every_copy.py`` keeps the run as a test): the two strings
are the same string. The commit door collapses the whitespace of the excerpt once
(``_optional_text``, which every door into it shares) and then stores that one string
as the quote of every link it makes and as the copy, and the review doors store the
quote as sent in both places. Nothing cuts a quote to a length on the way into a link
or a copy: a 4,000 character excerpt arrives whole on both sides. So the rule is
equality of the words, the whitespace between them ignored (``_quote_text``). A link
whose quote is a part of a copy, or the other way round, is a different quote and is
not matched, because no writer makes that relation (a reviewer who types a part of an
excerpt as the quote has chosen to attribute that part to the source).

Which sources a memory cites is read from its copies in every shape a writer stores,
not only the shapes the link writer reads (``cited_source_ids``). A key that names a
source (``source_id``, ``source_ids``, ``source_refs``, ``source_references``,
``selected_source_ids`` and the like, at any depth) holds ids that must name a stored
source the caller may read; an id anywhere else in a ref is judged only when it names a
stored source, because it may be a chunk id or a session id, which no source has.
"""

from __future__ import annotations

import json
import re
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

# The keys that name a source in a ref or in the metadata of an open loop. The open-loop reverse lookup of a source
# (``list_open_loops_referencing_source``) reads these, the open-loop reader withholds an id under one of them, and the
# saved-quote reader (``cited_source_ids``) reads every id under one of them as a cited source, so a loop or a memory
# that names a source under one of them is found by the id and withheld by the same words.
SOURCE_REFERENCE_KEYS = frozenset(
    {"source_id", "source_ids", "source_ref", "source_refs", "source_references", "selected_source_ids"}
)


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
    stored source and yields nothing. This is what the link writer and the write
    fence read, and it reads fewer shapes than are ever stored (an upper case
    ``SOURCE:``, ``selected_source_ids``, an id under another key). The readers of a
    saved quote judge what is stored with ``cited_source_ids``, in every shape.
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
# The column of a link that holds the quote it was made with.
_QUOTE_KEY = "quote"


def _quote_text(value: object) -> str | None:
    """A quote as text to compare: its words with the whitespace between them collapsed, or None when it has none."""

    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _canonical_source_id(value: object) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(str(value).strip()))
    except ValueError:
        return None


# -- the ids a copy names --------------------------------------------------------------------------------------------

# The two things a position in a ref can be. ``_REF`` holds a reference to a source, so every id under it names one.
# ``_OTHER`` is anything else in the ref (a chunk id, a page, a free-form note), where an id may name no source at all.
_REF = "ref"
_OTHER = "other"
# The keys that hold a source reference wherever they appear. ``sources`` is the key the link writer also reads.
_REFERENCE_KEYS = SOURCE_REFERENCE_KEYS | {"sources"}
# Inside a ref object (an entry of a ref list, or a provenance object) the two keys the link writer reads for the
# object's own id are references as well.
_OWN_ID_KEYS = frozenset({"id", "ref"})
# Keys whose value is text taken from a source, never a reference, so an id inside it is not read.
_TEXT_KEYS = frozenset({"quote", _EXCERPT_KEY})
_ID_IN_TEXT = re.compile(
    r"(?<![0-9a-fA-F])"
    r"(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})"
    r"(?![0-9a-fA-F])"
)
_SOURCE_MARKER = re.compile(r"(?:source:|sources/)\s*$", re.IGNORECASE)
# A ``memory:`` ref (the rollups and the consolidation write them into ``source_refs``) names a memory, so its id is
# not read as a source id at all.
_MEMORY_MARKER = re.compile(r"memory:$", re.IGNORECASE)
# A SQLite build from before 3.32 allows 999 bound variables, so sources are looked up in slices.
_LOOKUP_BATCH = 500
_TOKEN_BREAK = re.compile(r"[\s,;|]+")


@dataclass(frozen=True, slots=True)
class CitedSourceIds:
    """The ids a ref names, in two groups that are judged differently.

    ``named`` are ids at a position that holds a source reference: the value of ``source_id``, ``source_ids``,
    ``source_refs``, ``source_references``, ``selected_source_ids`` or ``sources`` (at any depth), an entry of a ref
    list, an id spelled the way the link writer reads one (any case, no hyphens, braces, ``urn:uuid:``, a ``source:``
    prefix in any case), and an id that follows ``source:`` or ``sources/`` inside longer text. An id there that names
    no stored source counts as a missing source does, so it is refused. An id that follows ``memory:`` names a memory
    and is not read.

    ``incidental`` are the other ids in a ref (under a key such as ``origin`` or ``chunk_id``, or inside a note). Such
    an id may be a chunk id or a session id and name no source, so it is refused only when it names a stored source
    the caller may not read. A source that was archived or deleted cannot be told from an id that names none, so an
    id of this kind that names one is not judged.
    """

    named: frozenset[str] = frozenset()
    incidental: frozenset[str] = frozenset()

    @property
    def every(self) -> frozenset[str]:
        return self.named | self.incidental

    def __or__(self, other: CitedSourceIds) -> CitedSourceIds:
        named = self.named | other.named
        return CitedSourceIds(named=named, incidental=(self.incidental | other.incidental) - named)


_NO_CITED_IDS = CitedSourceIds()


def _whole_id(text: str) -> str | None:
    """The id ``text`` is, as the link writer reads one and more: whitespace and any number of ``source:`` prefixes
    (in any case) removed, then a UUID in any spelling ``uuid.UUID`` accepts, in canonical lower case form."""

    candidate = text.strip()
    while candidate[:7].lower() == "source:":
        candidate = candidate[7:].strip()
    try:
        return str(UUID(candidate.lower()))
    except ValueError:
        return None


def _ids_in_text(text: str, *, as_ref: bool) -> tuple[set[str], set[str]]:
    """The ids in ``text``, as ``(named, incidental)``. Outside a ref position every id is incidental."""

    named: set[str] = set()
    incidental: set[str] = set()
    if as_ref:
        for token in _TOKEN_BREAK.split(text.strip()):
            whole = _whole_id(token)
            if whole is not None:
                named.add(whole)
    else:
        whole = _whole_id(text)
        if whole is not None:
            incidental.add(whole)
    for match in _ID_IN_TEXT.finditer(text):
        if _MEMORY_MARKER.search(text, 0, match.start()):
            continue
        found = str(UUID(match.group(0)))
        if as_ref and _SOURCE_MARKER.search(text, 0, match.start()):
            named.add(found)
        else:
            incidental.add(found)
    return named, incidental - named


def _json_container(text: str) -> object | None:
    """The list or object a string ref holds when it is JSON text, else None."""

    stripped = text.strip()
    if stripped[:1] not in ("{", "["):
        return None
    try:
        parsed = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def cited_source_ids(value: object) -> CitedSourceIds:
    """Every source id ``value`` (a ref, a list of refs, or a provenance object) names, as ``CitedSourceIds``.

    ``value`` is read as a reference. A string is read for ids in every spelling above, a list entry by entry, and an
    object key by key: a reference key makes everything under it a reference, any other key is read for ids that name
    stored sources only, and a key whose value is a quote is skipped. A string that is itself JSON is read as the JSON.
    The walk keeps its own stack, so a ref nested to any depth is read to the end.
    """

    named: set[str] = set()
    incidental: set[str] = set()
    pending: list[tuple[object, str]] = [(value, _REF)]
    while pending:
        node, state = pending.pop()
        if isinstance(node, str) or (state == _REF and isinstance(node, int) and not isinstance(node, bool)):
            text = node if isinstance(node, str) else str(node)
            found_named, found_incidental = _ids_in_text(text, as_ref=state == _REF)
            named |= found_named
            incidental |= found_incidental
            if state == _REF:
                nested = _json_container(text)
                if nested is not None:
                    pending.append((nested, _REF))
        elif isinstance(node, Mapping):
            for key, child in node.items():
                key_text = key.lower() if isinstance(key, str) else None
                if key_text in _TEXT_KEYS:
                    continue
                if isinstance(key, str):
                    key_named, key_incidental = _ids_in_text(key, as_ref=state == _REF)
                    named |= key_named
                    incidental |= key_incidental
                is_reference = key_text in _REFERENCE_KEYS or (state == _REF and key_text in _OWN_ID_KEYS)
                pending.append((child, _REF if is_reference else _OTHER))
        elif isinstance(node, (list, tuple)):
            pending.extend((child, state) for child in node)
    return CitedSourceIds(named=frozenset(named), incidental=frozenset(incidental - named))


# What a writer was given as a provenance object or a list of refs is read with ``cited_source_ids``. The write fence
# and the link writer keep ``source_uuids_in_ref``, which reads fewer shapes (see its docstring): the reader judges
# what is stored, in every shape, whatever the writer did with it.


_DROPPED = object()


def _without_refused_refs(refs: object, refused: frozenset[str]) -> object:
    """``refs`` without the entries that name a refused source. The same list when nothing is dropped.

    A value that is not a list and names a refused source is returned as ``_DROPPED``, and the caller removes its key.
    """

    if not refused:
        return refs
    if isinstance(refs, list):
        kept = [ref for ref in refs if refused.isdisjoint(cited_source_ids(ref).every)]
        return refs if len(kept) == len(refs) else kept
    return refs if refused.isdisjoint(cited_source_ids(refs).every) else _DROPPED


def _scrub_refs_key(container: dict[str, object], refused: frozenset[str]) -> None:
    """Drop the entries of ``container["source_refs"]`` that name a refused source, in place, on a copy the caller made."""

    if _SOURCE_REFS_KEY not in container:
        return
    scrubbed = _without_refused_refs(container[_SOURCE_REFS_KEY], refused)
    if scrubbed is _DROPPED:
        del container[_SOURCE_REFS_KEY]
    else:
        container[_SOURCE_REFS_KEY] = scrubbed


def _source_ids_named_by_memory_copies(row: Mapping[str, object]) -> CitedSourceIds:
    """Every source id the memory's own copies of its provenance name."""

    cited = _NO_CITED_IDS
    metadata = row.get("metadata_json")
    if isinstance(metadata, Mapping):
        for key in _PROVENANCE_OBJECT_KEYS:
            cited |= cited_source_ids(metadata.get(key))
        cited |= cited_source_ids(metadata.get(_SOURCE_REFS_KEY))
        agentic = metadata.get(_AGENTIC_MEMORY_KEY)
        if isinstance(agentic, Mapping):
            cited |= cited_source_ids(agentic.get(_SOURCE_REFS_KEY))
    value = row.get("value")
    if isinstance(value, Mapping):
        cited |= cited_source_ids(value.get(_SOURCE_REFS_KEY))
    return cited


def _source_ids_named_by_revision(row: Mapping[str, object]) -> CitedSourceIds:
    cited = _NO_CITED_IDS
    for key in _REVISION_VALUE_KEYS:
        value = row.get(key)
        if isinstance(value, Mapping):
            cited |= cited_source_ids(value.get(_SOURCE_REFS_KEY))
    return cited


def cited_source_ids_in_memory_audit(audit: Mapping[str, object]) -> CitedSourceIds:
    """Every source id the copies in a memory audit name, other than through a provenance link.

    The audit (``VNextMemoryCommitService.audit``) holds the memory row, its revisions and the payload of its events,
    and each keeps the refs and the quote the memory was written with. A memory with no link (a commit held for
    review or confirmed inline, then approved) names its sources only there. The audit of a key is allowed only when
    every source named anywhere in it is readable, so the caller asks this for what a link does not say.
    """

    cited = _NO_CITED_IDS
    memory = audit.get("memory")
    if isinstance(memory, Mapping):
        cited |= _source_ids_named_by_memory_copies(memory)
    revisions = audit.get("revisions")
    for revision in revisions if isinstance(revisions, list) else []:
        if isinstance(revision, Mapping):
            cited |= _source_ids_named_by_revision(revision)
    events = audit.get("events")
    for event in events if isinstance(events, list) else []:
        payload = event.get("payload_json") if isinstance(event, Mapping) else None
        if not isinstance(payload, Mapping):
            continue
        cited |= _source_ids_named_by_memory_copies(payload)
        changes = payload.get("changes")
        if isinstance(changes, Mapping):
            cited |= _source_ids_named_by_memory_copies(changes)
    return cited


def source_ids_named_by_memory_audit(audit: Mapping[str, object]) -> set[str]:
    """The ids ``cited_source_ids_in_memory_audit`` finds at a position that holds a source reference."""

    return set(cited_source_ids_in_memory_audit(audit).named)


# -- the quote copies ------------------------------------------------------------------------------------------------


def _copy_quote_texts(container: Mapping[str, object]) -> set[str]:
    """The text of every saved copy of a quote in ``container`` (a memory's metadata, or a revision's value), as
    ``_quote_text`` reads it: the quote of ``provenance`` and ``replacement_provenance``, and the excerpt of
    ``agentic_memory``. These are the copies ``_without_quote_copies`` removes."""

    texts: set[str] = set()
    for key in _PROVENANCE_OBJECT_KEYS:
        copy = container.get(key)
        text = _quote_text(copy.get(_QUOTE_KEY) if isinstance(copy, Mapping) else copy)
        if text is not None:
            texts.add(text)
    agentic = container.get(_AGENTIC_MEMORY_KEY)
    if isinstance(agentic, Mapping):
        excerpt = _quote_text(agentic.get(_EXCERPT_KEY))
        if excerpt is not None:
            texts.add(excerpt)
    excerpt = _quote_text(container.get(_EXCERPT_KEY))
    if excerpt is not None:
        texts.add(excerpt)
    return texts


def _without_quote_copies(container: dict[str, object]) -> None:
    """Remove the saved copies of a quote from ``container``, in place, on a copy the caller made."""

    for key in _PROVENANCE_OBJECT_KEYS:
        container.pop(key, None)
    container.pop(_EXCERPT_KEY, None)
    agentic = container.get(_AGENTIC_MEMORY_KEY)
    if isinstance(agentic, Mapping) and _EXCERPT_KEY in agentic:
        container[_AGENTIC_MEMORY_KEY] = {key: item for key, item in agentic.items() if key != _EXCERPT_KEY}


def _memory_copy_quote_texts(row: Mapping[str, object]) -> set[str]:
    metadata = row.get("metadata_json")
    return _copy_quote_texts(metadata) if isinstance(metadata, Mapping) else set()


def _memory_without_refused_provenance(
    row: Mapping[str, object], *, refused: frozenset[str], withhold_quotes: bool
) -> dict[str, object]:
    out = dict(row)
    metadata = row.get("metadata_json")
    if isinstance(metadata, Mapping):
        copy = dict(metadata)
        if withhold_quotes:
            _without_quote_copies(copy)
        _scrub_refs_key(copy, refused)
        agentic = copy.get(_AGENTIC_MEMORY_KEY)
        if isinstance(agentic, Mapping):
            inner = dict(agentic)
            _scrub_refs_key(inner, refused)
            copy[_AGENTIC_MEMORY_KEY] = inner
        out["metadata_json"] = copy
    value = row.get("value")
    if isinstance(value, Mapping) and _SOURCE_REFS_KEY in value:
        scrubbed_value = dict(value)
        _scrub_refs_key(scrubbed_value, refused)
        out["value"] = scrubbed_value
    return out


def _revision_without_refused_refs(
    row: Mapping[str, object], *, refused: frozenset[str], withhold_quotes: bool
) -> dict[str, object]:
    out = dict(row)
    for key in _REVISION_VALUE_KEYS:
        value = row.get(key)
        if not isinstance(value, Mapping):
            continue
        scrubbed = dict(value)
        if withhold_quotes:
            _without_quote_copies(scrubbed)
        _scrub_refs_key(scrubbed, refused)
        agentic = scrubbed.get(_AGENTIC_MEMORY_KEY)
        if isinstance(agentic, Mapping):
            inner = dict(agentic)
            _scrub_refs_key(inner, refused)
            scrubbed[_AGENTIC_MEMORY_KEY] = inner
        out[key] = scrubbed
    return out


def _is_memory_row(value: Mapping[str, object]) -> bool:
    return "id" in value and "memory_key" in value and isinstance(value.get("metadata_json"), Mapping)


def _is_revision_row(value: Mapping[str, object]) -> bool:
    return "memory_id" in value and "revision_number" in value and "revision_type" in value


@dataclass(frozen=True, slots=True)
class _Verdict:
    """What the reader decided about one memory: the sources it cites that the caller may not read, whether its saved
    quotes are withheld, and the text of every quote that is withheld (so a copy of it elsewhere is withheld too)."""

    refused: frozenset[str]
    withhold_quotes: bool
    texts: frozenset[str]


class SavedProvenanceReader:
    """What one caller may be shown of the provenance saved on memories, asked once per read.

    A link stores the quote it was made with, and a memory keeps copies of that quote in its metadata. The source the
    link names can be reclassified (sensitivity raised, domain changed, project moved) or archived after that, so the
    fence that admitted the link when it was written says nothing about the next reader. This object asks ``fence``
    again, with the source as it is now, and withholds what the caller may not read:

    * a link whose source is missing, archived or outside the fence, and a link that names no source at all, is left
      out, as a link that was never stored would be;
    * when a memory has such a link, or its own copies name such a source (``cited_source_ids``: every id any ref of
      the memory names, in every shape), its copies of the quote (``metadata_json.provenance``,
      ``metadata_json.replacement_provenance`` and ``metadata_json.agentic_memory.conversation_excerpt``, and the same
      copies in a revision's ``previous_value`` and ``new_value``) are removed, and the entries of the ref lists that
      name the source (``source_refs`` in the metadata, in ``agentic_memory`` and in ``value``, and the same list in a
      revision's ``previous_value`` and ``new_value``) are dropped;
    * every text that is withheld in either way (the quote of a link that is left out, and every copy that is removed)
      is withheld from the other links of the same memory too: a link whose quote says the same text (ignoring
      whitespace) is shown without its quote. The commit route saves one ``conversation_excerpt`` as the quote of the
      link it makes and as the memory's own copy, and it links only the first id of a reference that names several
      sources, so the quote left on a link to a readable source can be the bytes of a source the caller may not read,
      and the only sign of it is the memory's own copy. The match is equality of the words, which is the relation the
      writers make (see the module docstring); a link whose quote is different text (the link from a captured
      candidate to its own source, or a review's own quote) keeps it.

    A memory with no link at all (a commit held for review or confirmed inline, then approved) keeps the sources it
    cited only in the ref lists of its own copies, so the reader judges a row by those copies as well. It must be asked
    about a row as the store holds it: a scrub that drops the refs of a row first (the context pack's scope pass does
    this for a pack with a project scope) leaves nothing here to judge, and the quote stays.

    The links of a memory are judged against the memory's own row, the one the reader was last asked about
    (``memory``, ``memories``, ``tree``) and, for a memory it was not asked about, the row the store holds for the id.
    Ask for the memory, then its revisions, then its links: a copy of a quote that only a revision holds is added to
    the withheld texts when the revision is read.

    The owner (``fence.fenced`` is false) is shown what was stored: every method returns its input unchanged and reads
    nothing it was not asked to. A memory whose sources are all admitted is returned as the same object.

    ``fence`` is a required keyword-only argument, with no default.
    """

    def __init__(self, store: object, *, fence: SourceReadFence) -> None:
        self._store = store
        self._fence = fence
        self._links: dict[str, list[dict[str, object]]] = {}
        self._admitted: dict[str, bool] = {}
        self._exists: dict[str, bool] = {}
        self._rows: dict[str, Mapping[str, object]] = {}
        self._looked_up: set[str] = set()
        self._cited: dict[int, tuple[Mapping[str, object], CitedSourceIds]] = {}
        self._revision_texts: dict[str, set[str]] = {}

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
        shown = (self._shown(link, siblings=raw) for link in raw)
        return [link for link in shown if link is not None]

    def judge_links(self, links: Iterable[Mapping[str, object]]) -> None:
        """Look up the sources of ``links`` and of the other links of the same memories in one read, so
        ``admits_link`` and ``shown_link`` do not read one at a time."""

        if not self.fenced:
            return
        given = list(links)
        self._load_links([str(link.get("target_id") or "") for link in given])
        every = [*given]
        for target_id in dict.fromkeys(str(link.get("target_id") or "") for link in given):
            every.extend(self._links.get(target_id, []))
        self._judge(
            source_id for link in every if (source_id := _canonical_source_id(link.get("source_id"))) is not None
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

    def shown_link(self, link: Mapping[str, object]) -> dict[str, object] | None:
        """``link`` as the caller is shown it: ``None`` when it is left out (``admits_link`` is false), the link with
        its ``quote`` withheld when the memory withholds that text (a link that is left out says it, or a copy on the
        memory that names a source the caller may not read does), the link itself otherwise."""

        if not self.fenced:
            return link  # type: ignore[return-value]
        target_id = str(link.get("target_id") or "")
        self._load_links([target_id])
        return self._shown(link, siblings=self._links.get(target_id, []))

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
        """``row`` (a memory revision) with the refs of a refused source dropped, and its copies of a quote removed
        when the revision or its memory cites such a source."""

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

    def _shown(self, link: Mapping[str, object], *, siblings: Sequence[Mapping[str, object]]) -> dict[str, object] | None:
        self._judge(
            source_id for other in (link, *siblings) if (source_id := _canonical_source_id(other.get("source_id"))) is not None
        )
        if not self.admits_link(link):
            return None
        quote = _quote_text(link.get(_QUOTE_KEY))
        if quote is None:
            return link  # type: ignore[return-value]
        memory_id = str(link.get("target_id") or "")
        if quote in self._verdict(memory_id, self._row_for(memory_id), siblings).texts:
            return {**link, _QUOTE_KEY: None}
        return link  # type: ignore[return-value]

    def _verdict(
        self, memory_id: str, row: Mapping[str, object] | None, links: Sequence[Mapping[str, object]]
    ) -> _Verdict:
        """Judge the memory by its links and, when ``row`` is known, by every source its own copies name."""

        cited = self._cited_by(row) if row is not None else _NO_CITED_IDS
        link_ids = {
            source_id for link in links if (source_id := _canonical_source_id(link.get("source_id"))) is not None
        }
        self._judge([*link_ids, *cited.every])
        refused = frozenset(
            {source_id for source_id in link_ids | cited.named if not self._admitted.get(source_id, False)}
            | {
                source_id
                for source_id in cited.incidental
                if self._exists.get(source_id, False) and not self._admitted.get(source_id, False)
            }
        )
        withhold_quotes = bool(refused) or any(_canonical_source_id(link.get("source_id")) is None for link in links)
        # A copy a revision lost because the revision cites a refused source is withheld whatever the memory cites.
        texts = set(self._revision_texts.get(memory_id, ()))
        if withhold_quotes:
            texts |= {
                text
                for link in links
                if not self.admits_link(link) and (text := _quote_text(link.get(_QUOTE_KEY))) is not None
            }
            if row is not None:
                texts |= _memory_copy_quote_texts(row)
        return _Verdict(refused=refused, withhold_quotes=withhold_quotes, texts=frozenset(texts))

    def _memory(self, row: Mapping[str, object]) -> dict[str, object]:
        memory_id = str(row.get("id") or "")
        verdict = self._verdict(memory_id, row, self._links.get(memory_id, []))
        if not verdict.withhold_quotes:
            return row  # type: ignore[return-value]
        return _memory_without_refused_provenance(row, refused=verdict.refused, withhold_quotes=True)

    def _revision(self, row: Mapping[str, object]) -> dict[str, object]:
        memory_id = str(row.get("memory_id") or "")
        cited = _source_ids_named_by_revision(row)
        self._judge(cited.every)
        refused = frozenset(
            {source_id for source_id in cited.named if not self._admitted.get(source_id, False)}
            | {
                source_id
                for source_id in cited.incidental
                if self._exists.get(source_id, False) and not self._admitted.get(source_id, False)
            }
        )
        known = self._rows.get(memory_id)
        memory_withholds = known is not None and self._verdict(memory_id, known, self._links.get(memory_id, [])).withhold_quotes
        withhold_quotes = bool(refused) or memory_withholds
        texts: set[str] = set()
        for key in _REVISION_VALUE_KEYS:
            value = row.get(key)
            if withhold_quotes and isinstance(value, Mapping):
                texts |= _copy_quote_texts(value)
        if not refused and not texts:
            return row  # type: ignore[return-value]
        if texts:
            self._revision_texts.setdefault(memory_id, set()).update(texts)
        return _revision_without_refused_refs(row, refused=refused, withhold_quotes=withhold_quotes)

    def _cited_by(self, row: Mapping[str, object]) -> CitedSourceIds:
        cached = self._cited.get(id(row))
        if cached is not None and cached[0] is row:
            return cached[1]
        cited = _source_ids_named_by_memory_copies(row)
        self._cited[id(row)] = (row, cited)
        return cited

    def _row_for(self, memory_id: str) -> Mapping[str, object] | None:
        """The row of ``memory_id`` the reader was asked about, else the one the store holds, else None."""

        known = self._rows.get(memory_id)
        if known is not None or not memory_id or memory_id in self._looked_up:
            return known
        self._looked_up.add(memory_id)
        if _canonical_source_id(memory_id) is None:
            return None
        getter = getattr(self._store, "get_memory", None)
        found = getter(memory_id) if callable(getter) else None
        if not isinstance(found, Mapping):
            return None
        self._rows[memory_id] = found
        self._judge(self._cited_by(found).every)
        return found

    def _prefetch(
        self, *, memories: Sequence[Mapping[str, object]], revisions: Sequence[Mapping[str, object]]
    ) -> None:
        """Read the links of every memory row and the sources they and the rows' own copies name, once."""

        self._load_links([str(row.get("id") or "") for row in memories])
        wanted: list[str] = []
        for row in memories:
            memory_id = str(row.get("id") or "")
            if memory_id:
                self._rows[memory_id] = row
            wanted.extend(self._cited_by(row).every)
            for link in self._links.get(memory_id, []):
                source_id = _canonical_source_id(link.get("source_id"))
                if source_id is not None:
                    wanted.append(source_id)
        for row in revisions:
            wanted.extend(_source_ids_named_by_revision(row).every)
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
        rows: dict[str, Mapping[str, object]] = {}
        for start in range(0, len(unknown), _LOOKUP_BATCH):
            rows.update(_rows_by_id(self._store, unknown[start : start + _LOOKUP_BATCH]))
        for source_id in unknown:
            row = rows.get(source_id)
            self._exists[source_id] = row is not None
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
    "CitedSourceIds",
    "EXPLAIN_DISCLOSURE_ACTION",
    "MEMORY_REF_NOT_FOUND_MESSAGE",
    "MemoryRefNotFoundError",
    "SOURCE_REFERENCE_KEYS",
    "SOURCE_REF_NOT_FOUND_MESSAGE",
    "SavedProvenanceReader",
    "SourceReadFence",
    "SourceRefNotFoundError",
    "cited_source_ids",
    "cited_source_ids_in_memory_audit",
    "resolve_attachable_memory_id",
    "resolve_attachable_source_id",
    "resolve_attachable_sources",
    "source_ids_named_by_memory_audit",
    "source_uuids_in_ref",
]
