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
as the quote of every link it makes and as the copy. The review doors store the quote
as sent in both places; a review that sends no quote stores the memory's own text as
the quote of its link and ``null`` as the copy, so there is no second copy of any
source's text to match. Nothing cuts a quote to a length on the way into a link
or a copy: a 4,000 character excerpt arrives whole on both sides. So the rule is
equality of the words, the whitespace between them ignored (``_quote_text``). A link
whose quote is a part of a copy, or the other way round, is a different quote and is
not matched, because no writer makes that relation (a reviewer who types a part of an
excerpt as the quote has chosen to attribute that part to the source).

Which sources a memory cites is read from its copies in every shape a writer stores,
not only the shapes the link writer reads (``cited_source_ids``), and from the copies
in its revisions (``previous_value``, ``new_value`` and the revision's own
``metadata_json``, where a memory proposal keeps the refs it was given). An id is read
in one of two ways. An id at a position that says it is a source (the value of
``source_id``, ``source_ids``, ``source_ref``, ``source_refs``, ``source_references``,
``selected_source_ids`` or ``sources`` at any depth, the value of ``id`` or ``ref``
inside an entry of a ref list or a provenance object, an entry of a ref list, the whole
of a string or one word of a string that is a list of ids, a word that starts with
``source:`` or ``alice://sources/``) is *named*: it must name a stored source the
caller may read, and one that does not is refused as a missing source is. An id
anywhere else (under another key, in a sentence, in an outside URL, with punctuation
around it) is *incidental*, because it may be a chunk id or a session id: it is refused
only when it names a source the store holds a row for, an archived one included, and
the caller may not read it. Two limits follow and are stated here and in the docs. An
id that names no source row (a source removed from the database, which no door of the
product does, as sources are archived) cannot be told from a chunk id, so an incidental
id of that kind is not judged. And because an incidental id changes the answer only when
it names a stored source the caller may not read, a caller that can store a memory can
learn, from reading it back, whether an id it already holds names such a source: the
quote is withheld and ``alice_explain`` refuses, or nothing changes. Judging incidental
ids at all is what keeps a confidential source named under a free key from leaking, and
withholding for every incidental id would withhold the quote of every memory whose refs
hold a chunk id, so the cost is one bit about an id the caller already has. The named
positions answer alike for a missing, an archived and an unreadable source.

An id is read in every spelling the link writer reads, and the reader is a superset of
the writer's parse (``tests/unit/test_saved_quote_ref_reading.py`` checks it against
``source_uuids_in_ref`` over random refs and over a grid of spellings). The spellings
that are easy to miss are the ones ``uuid.UUID`` takes through ``int(..., 16)``: it
strips whitespace, so 31 digits and a space, a tab or a newline are an id that starts
with ``0``, and the writer links and fences a source named that way (``source: `` and
the other 31 digits is the form the review found). The writer strips a string and
removes a lower case ``source:`` from it, and for the value of ``id``, ``ref``,
``source_id`` and ``source_ref`` it does not strip first, so the reader tries the
string as stored and stripped, with the ``source:`` prefixes (any case, any number)
removed from each, and reads each result with ``uuid.UUID`` as it stands first and
stripped second (``_whole_id``, ``_uuid_text``).

A ref that is JSON text is read as the value it decodes to. A string in a reference position that, with its whitespace
stripped, starts with ``{`` or ``[`` and decodes to an object or a list is walked by the rules that read the same value
stored as an object or a list, and its raw text is not scanned, so an id in a ``quote`` of it names nothing, as it names
nothing in an object (``tests/unit/test_saved_quote_json_refs.py``). A key that is repeated in the text keeps every value.
A text that does not decode, or is nested past the recursion limit of the decoder, is scanned as text. The link writer
never decodes JSON and no JSON text is an id it reads, so the reader stays a superset of the writer.

A commit can also cite a memory, as ``{"memory_id": "<id>", "quote": "..."}`` or as the two entries ``"memory:<id>"`` and
``{"quote": "..."}``, and the quote then holds words of that memory. The write fence reads neither shape (they name no
source), so the entry is stored as sent, and a memory that is redacted, archived, relabelled above a caller's ceiling or
made from a redacted input afterwards would keep handing its words to every reader of the commit. The reader therefore
reads the memories a row names as well (``cited_memory_refs``), in two groups that are judged as the two groups of
``CitedSourceIds`` are. An id is *named* when it follows a ``memory:`` prefix in any case and any number or an
``alice://memories/`` URL, is a word of a list of such words, is in JSON text, or stands under ``memory_id``,
``memory_ids``, ``memory_ref``, ``memory_refs``, ``memory_references``, ``source_memory_ids``, ``selected_memory_ids`` or
``memories`` at any depth, in every spelling ``uuid.UUID`` takes; a named id must name a stored memory the caller may read now,
and one that does not is refused as a missing memory is. Every other id in the ref, outside the text of a ``quote`` or
``conversation_excerpt``, is *incidental*: ``Memory <id>``, ``mem:<id>``, ``see memory: <id>``, ``[memory](<id>)``, a URL,
``alice://memory/<id>``, an id under ``memory``, ``origin``, ``ref_id``, ``parent_memory_id`` or ``supersedes``, an id used as a
key. An agent can write a memory ref in any of these ways, and the commit route saves the excerpt it was sent whatever the ref
says, so the quote is withheld whenever any id of the ref names a memory the caller may not read. An incidental id may name a
source, a chunk or a session instead, so it is refused only when it names a memory the store holds a row for (live, archived or
redacted: the lookup asks for the soft-deleted rows, ``memory_rows_including_deleted``) that the caller may not read; an id that
names no memory row is left alone, which also leaves alone the id of a memory removed from the database, and no door of the
product removes one. All the ids of all the rows of a read are looked up in slices of 500, once.

A memory is readable when its policy allows it now (``admits_memory`` on the row's effective labels; a memory that is
archived, redacted or outside the fence is refused alike). A refused memory withholds the words the row saved: the three
copies are removed, and every string of an entry that names it is set to ``null`` except the ids (``_withhold_entry_text``: the
``quote`` and ``conversation_excerpt``, any other text such as a ``text``, ``excerpt``, ``snippet`` or ``note`` field, and a
sentence that has the id in it; an id, a string that is a reference, anything under a reference key, a number and a boolean
stay, because an id alone is covered by the hidden-ids disclosure). The quote of an entry beside it that names nothing goes too
(the shape ``"memory:<id>"`` followed by ``{"quote": ...}``), and the same applies beside a refused source, whose entry is
dropped whole. The owner and an unbound ``admin_agent`` key have no limits and keep every quote. The memories of a row are
looked up only when the row holds words to withhold (a quote, an excerpt, or a string beside an id: ``_holds_words``), so a
roll-up card that lists its members as bare ``memory:<id>`` references costs no read.

The events of a feed (the workspace's recent events, the events of a source trace) hold the changes a commit was confirmed or
edited with, and those changes hold the refs and the quote it was sent with. The feed admits an event by the row it is about, so
``SavedProvenanceReader.events`` holds the payload to the same rule as the events of one memory in its audit.

A ref string is stored as sent and the proposal door bounds only the size of the
request, so every parser here is linear in the length of what it reads: a string is
walked once, and a marker is tested at the position of an id and not by rescanning
the text before it (``tests/unit/test_saved_quote_ref_reading.py`` times each shape on
inputs of several hundred thousand characters).
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from alicebot_api.vnext_agent_control import (
    ALL_SENSITIVITY,
    VNEXT_DOMAINS,
    AgentIdentity,
    evaluate_agent_policy,
    resource_project_scope,
)
from alicebot_api.vnext_project_scope import project_floor_shape, source_project_scope

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

    @property
    def entity_read_fenced(self) -> bool:
        """Whether policy limits this identity's view of vault-wide entity metadata.

        Request filters are a selection, not an authorization boundary. An
        unbound admin can read every label and keeps the owner's stored counts.
        The existing ``fenced`` flag still controls saved-provenance checks.
        """
        decision = evaluate_agent_policy(
            identity=self.identity,
            action=EXPLAIN_DISCLOSURE_ACTION,
            domains=VNEXT_DOMAINS,
            sensitivity_allowed=ALL_SENSITIVITY,
            project_scope=(),
            require_explicit_project_scope=True,
        )
        return (
            decision.decision != "allowed"
            or bool(decision.effective_project_scope)
            or set(decision.effective_domains) != set(VNEXT_DOMAINS)
            or set(decision.effective_sensitivity_allowed) != set(ALL_SENSITIVITY)
        )

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
        if row.get("unverified") and self.identity.project_scope_locked:
            return False
        _shape, floor = project_floor_shape(row)
        decision = evaluate_agent_policy(
            identity=self.identity,
            action=EXPLAIN_DISCLOSURE_ACTION,
            domains=(str(row.get("domain") or "unknown"),),
            sensitivity_allowed=(str(row.get("sensitivity") or "unknown"),),
            project_scope=project_scope,
            project_floor=floor,
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
    if not isinstance(row, Mapping):
        raise MemoryRefNotFoundError()
    from alicebot_api.vnext_label_guard import LabelGuard

    effective = LabelGuard.for_fence(store, fence).effective_row("memory", row)
    judged = effective if isinstance(effective, Mapping) else row
    if not fence.admits_memory(judged):
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


def _takes_include_deleted(method: object) -> bool:
    try:
        return "include_deleted" in inspect.signature(method).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def source_rows_including_archived(store: object, source_ids: Sequence[str]) -> dict[str, Mapping[str, object]]:
    """The rows of ``source_ids`` as a store holds them, an archived source's row (``deleted_at`` set) included.

    ``_rows_by_id`` does not return an archived source, so an archived source cannot be told from an id that names no
    source. The saved-quote reader (and ``alice_explain``) need to tell them apart for an id found where a chunk id
    could be, so they ask ``get_sources_by_ids(..., include_deleted=True)``, which both stores take. A store whose
    ``get_sources_by_ids`` has no such keyword (a stub) is read as ``_rows_by_id`` reads it. The caller decides what an
    archived row means: ``SourceReadFence.admits`` refuses it.
    """

    bulk = getattr(store, "get_sources_by_ids", None)
    if callable(bulk) and _takes_include_deleted(bulk):
        return {str(row.get("id")): row for row in bulk(list(source_ids), include_deleted=True) if isinstance(row, Mapping)}
    return _rows_by_id(store, source_ids)


def memory_rows_including_deleted(store: object, memory_ids: Sequence[str]) -> dict[str, Mapping[str, object]]:
    """The rows of ``memory_ids`` as a store holds them, a soft-deleted memory's row (``deleted_at`` set: archived or
    redacted) included.

    The saved-quote reader needs to tell a memory that was deleted from an id that names no memory at all, for an id found
    where a chunk id or a session id could be: the first is refused, the second is left alone. Both stores take
    ``get_memories_by_ids(..., include_deleted=True)``; a store whose method has no such keyword (a stub) is read as it
    answers, a live row only. The caller decides what an archived or redacted row means: ``SourceReadFence.admits_memory``
    refuses it.
    """

    bulk = getattr(store, "get_memories_by_ids", None)
    if callable(bulk):
        rows = bulk(list(memory_ids), include_deleted=True) if _takes_include_deleted(bulk) else bulk(list(memory_ids))
        return {str(row.get("id")): row for row in rows if isinstance(row, Mapping)}
    single = getattr(store, "get_memory", None)
    found: dict[str, Mapping[str, object]] = {}
    if callable(single):
        for memory_id in memory_ids:
            row = single(memory_id)
            if isinstance(row, Mapping):
                found[memory_id] = row
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
# One ``source:`` prefix, in any case. ``_source_prefixes_end`` reads the run of them a ref may start with.
_SOURCE_PREFIX = re.compile(r"source:", re.IGNORECASE)
_ALICE_SOURCE_URL = "alice://sources/"
# A ``memory:`` ref (the rollups and the consolidation write them into ``source_refs``) names a memory, so its id is
# not read as a source id at all.
_MEMORY_PREFIX = "memory:"
# An id is at least 32 characters long in every spelling ``uuid.UUID`` accepts, so a shorter string holds none.
_MIN_ID_CHARS = 32
# A SQLite build from before 3.32 allows 999 bound variables, so sources are looked up in slices.
_LOOKUP_BATCH = 500
_TOKEN_BREAK = re.compile(r"[\s,;|]+")


@dataclass(frozen=True, slots=True)
class CitedSourceIds:
    """The ids a ref names, in two groups that are judged differently.

    ``named`` are ids the ref says are sources: the value of ``source_id``, ``source_ids``, ``source_ref``,
    ``source_refs``, ``source_references``, ``selected_source_ids`` or ``sources`` (at any depth), the value of ``id``
    or ``ref`` inside an entry of a ref list or a provenance object, an entry of a ref list, and an id
    that is the whole of a string (or one of several ids that make up the whole of a string, split on whitespace,
    commas, semicolons and bars) in any spelling the link writer reads (any case, no hyphens, braces, ``urn:uuid:``, a
    ``source:`` prefix in any case), or that follows a ``source:`` prefix or an ``alice://sources/`` URL at the start
    of a word (``source:<id>#chunk-1``). An id there that names no stored source counts as a missing source does, so
    it is refused. An id that follows ``memory:`` names a memory and is not read.

    ``incidental`` are the other ids in a ref: under a key such as ``origin`` or ``chunk_id``, a bare id inside a
    sentence (``copied from source: <id>``), an id inside an outside URL (``https://host/sources/<id>``), an id with
    punctuation around it. Such an id may be a chunk id or a session id and name no source, so it is refused only when
    it names a source that was stored, whether that source is archived or not, and the caller may not read it. An id
    that names no source row at all is left alone, so a source removed from the database (no door of the product does
    that: sources are archived) cannot be told from a chunk id.
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


def _uuid_text(text: str) -> str | None:
    """``text`` (any case) as a canonical UUID in any spelling ``uuid.UUID`` accepts, else None.

    ``text`` is read as it stands first, and then with the whitespace around it removed. The first read is the one the
    link writer makes (``source_uuids_in_ref`` hands ``uuid.UUID`` the string as stored, after removing a ``source:``
    prefix), and it is not the second: ``uuid.UUID`` passes the string to ``int(..., 16)``, which strips whitespace, so a
    string of 32 characters whose first one is a space, a tab or a newline is an id of 31 digits with a leading zero, and
    stripping it first leaves 31 characters, which are no id. A source whose id starts with ``0`` can be written that
    way, and the writer links and fences it, so the reader must name it. A string that is an id only with its whitespace
    removed (a 32 digit id with a space around it) is read by the second try.
    """

    for candidate in (text, text.strip()):
        if len(candidate) < _MIN_ID_CHARS:
            continue
        try:
            return str(UUID(candidate.lower()))
        except ValueError:
            continue
    return None


def _source_prefixes_end(text: str) -> int:
    """Where the ``source:`` prefixes at the start of ``text`` end, or 0 when it starts with none.

    The prefixes are in any case and any number, with whitespace allowed before each. The whitespace after the last
    prefix is not part of them: it can be a character of the id (see ``_uuid_text``). This is the end of a match of
    ``(?:\\s*source:)*`` (ignoring case) at the start of ``text``, read by a loop over one fixed pattern, so the time is
    linear in the length of ``text`` however much whitespace it holds. ``str.isspace`` is the test ``\\s`` makes.
    """

    end = 0
    position = 0
    length = len(text)
    while True:
        while position < length and text[position].isspace():
            position += 1
        prefix = _SOURCE_PREFIX.match(text, position)
        if prefix is None:
            return end
        position = end = prefix.end()


def _whole_id(text: str) -> str | None:
    """The id ``text`` is, in every way the link writer reads one and more.

    The writer strips a string, removes a lower case ``source:`` and hands the rest to ``uuid.UUID``; for the value of
    ``id``, ``ref``, ``source_id`` and ``source_ref`` it does not strip first. The two orders differ for an id with a
    leading zero written with a whitespace character in its place (``source: `` and the 31 other digits, with a newline
    after them: the stripped string ends at the last digit and the space after the colon is the zero). So the text is read
    as it stands and stripped, and in each of the two, any number of ``source:`` prefixes (in any case, with whitespace
    before each) are removed and what follows is read by ``_uuid_text``.
    """

    for whole in (text, text.strip()):
        found = _uuid_text(whole[_source_prefixes_end(whole) :])
        if found is not None:
            return found
    return None


def _word_id(word: str) -> tuple[str | None, bool]:
    """The id a word (a piece of a string split on whitespace and commas) is or begins with, as ``(id, explicit)``.

    A word that is an id in any spelling gives ``(id, False)``. A word that starts with a ``source:`` prefix or with
    ``alice://sources/`` and then holds an id (``source:<id>``, ``source:<id>#chunk-1``, ``alice://sources/<id>``) gives
    ``(id, True)``: the word says it names a source. A word that holds no id at its start gives ``(None, False)``.
    """

    start = _source_prefixes_end(word)
    if start == 0 and word[: len(_ALICE_SOURCE_URL)].lower() == _ALICE_SOURCE_URL:
        start = len(_ALICE_SOURCE_URL)
    if start == 0:
        return _uuid_text(word), False
    whole = _uuid_text(word[start:])
    if whole is None:
        found = _ID_IN_TEXT.match(word, start)
        whole = str(UUID(found.group(0))) if found else None
    return whole, whole is not None


def _ids_in_text(text: str, *, as_ref: bool) -> tuple[set[str], set[str]]:
    """The ids in ``text``, as ``(named, incidental)``. Outside a ref position every id is incidental.

    In a ref position a string is read as follows. When the whole string is one id (after any ``source:`` prefixes) it
    is named. Otherwise it is split into words on whitespace, commas, semicolons and bars: when every word is an id
    (a list of ids) each is named; when any word is something else (a sentence, a URL) only a word that says it names
    a source (``source:<id>``, ``alice://sources/<id>``) is named and every other id in the string is incidental. The
    string is walked once, so the cost is linear in its length.
    """

    named: set[str] = set()
    incidental: set[str] = set()
    if len(text) < _MIN_ID_CHARS:
        return named, incidental
    whole = _whole_id(text)
    if whole is not None:
        (named if as_ref else incidental).add(whole)
        return named, incidental
    if as_ref:
        words: list[tuple[str, bool]] = []
        every_word_is_an_id = True
        for word in _TOKEN_BREAK.split(text.strip()):
            if not word:
                continue
            found, explicit = _word_id(word)
            if found is None:
                every_word_is_an_id = False
            else:
                words.append((found, explicit))
        for found, explicit in words:
            (named if explicit or every_word_is_an_id else incidental).add(found)
    for match in _ID_IN_TEXT.finditer(text):
        start = match.start()
        if text[max(0, start - len(_MEMORY_PREFIX)) : start].lower() == _MEMORY_PREFIX:
            continue
        incidental.add(str(UUID(match.group(0))))
    return named, incidental - named


def _keep_every_value(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """An object of a JSON text with every value of a repeated key kept, as a list under that key.

    ``json.loads`` keeps the last value of a key that is written twice, and a source id written under the first one would
    then go unread. Under a key that names a reference the list is read entry by entry, and under ``quote`` the whole
    list is skipped, so a repeated key is read as every one of its values would be read alone.
    """

    values: dict[str, list[object]] = {}
    for key, value in pairs:
        values.setdefault(key, []).append(value)
    return {key: found[0] if len(found) == 1 else found for key, found in values.items()}


def _json_container(text: str) -> object | None:
    """The list or object a string ref holds when it is JSON text, else None.

    The text is decoded as far as ``json.loads`` goes, with raw control characters (a newline or a tab typed inside a
    string, as a multi-line excerpt has) accepted: a text nested beyond the recursion limit of the interpreter is not
    decoded (``RecursionError``), and neither is a text that is not JSON, or JSON that is not an object or a list. The
    caller scans such a text as it stands. The decode and the merge of repeated keys are linear in the length of the text.
    """

    stripped = text.strip()
    if stripped[:1] not in ("{", "["):
        return None
    try:
        parsed = json.loads(stripped, object_pairs_hook=_keep_every_value, strict=False)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def cited_source_ids(value: object) -> CitedSourceIds:
    """Every source id ``value`` (a ref, a list of refs, or a provenance object) names, as ``CitedSourceIds``.

    ``value`` is read as a reference. A string is read for ids in every spelling above, a list entry by entry, and an
    object key by key: a reference key makes everything under it a reference, any other key is read for ids that name
    stored sources only, and a key whose value is a quote is skipped. A string that is a reference and is itself JSON (an
    object or a list) is read as the value it decodes to, by these same rules, and its text is not scanned: a quote inside
    it names nothing, as it names nothing in an object. A string that does not decode is scanned as text. The walk keeps its
    own stack, so a ref nested to any depth is read to the end.
    """

    named: set[str] = set()
    incidental: set[str] = set()
    pending: list[tuple[object, str]] = [(value, _REF)]
    while pending:
        node, state = pending.pop()
        if isinstance(node, str) or (state == _REF and isinstance(node, int) and not isinstance(node, bool)):
            text = node if isinstance(node, str) else str(node)
            nested = _json_container(text) if state == _REF else None
            if nested is not None:
                # JSON text is read as the value it decodes to, as the same value stored as an object or a list is read:
                # its quote is text taken from a source and names nothing. Its raw text is not scanned as well, which would
                # read the ids of a quote. The link writer never decodes JSON and no JSON text is an id it reads, so the
                # reader stays a superset of the writer.
                pending.append((nested, _REF))
                continue
            found_named, found_incidental = _ids_in_text(text, as_ref=state == _REF)
            named |= found_named
            incidental |= found_incidental
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


# -- the memories a copy names ---------------------------------------------------------------------------------------

# Keys whose value is a memory reference wherever they appear: every id under one of them names a memory, in any spelling.
# The first five are the keys the open-loop reader treats the same way (``vnext_open_loop_references``); the rest are the
# spellings a client would reach for next to them.
MEMORY_REFERENCE_KEYS = frozenset(
    {
        "memory_id",
        "memory_ids",
        "memory_ref",
        "memory_refs",
        "memory_references",
        "source_memory_ids",
        "selected_memory_ids",
        "memories",
    }
)
_MEMORY_PREFIX_PATTERN = re.compile(r"memory:", re.IGNORECASE)
_ALICE_MEMORY_URL = "alice://memories/"


def _memory_prefixes_end(text: str) -> int:
    """Where the ``memory:`` prefixes (any case, any number, whitespace allowed before each) at the start of ``text`` end, or
    0 when it starts with none. The same loop as ``_source_prefixes_end``, over one fixed pattern, so the time is linear."""

    end = 0
    position = 0
    length = len(text)
    while True:
        while position < length and text[position].isspace():
            position += 1
        prefix = _MEMORY_PREFIX_PATTERN.match(text, position)
        if prefix is None:
            return end
        position = end = prefix.end()


def _marked_memory_start(word: str) -> int:
    """Where the id of a word that says it names a memory starts (after ``memory:`` prefixes or ``alice://memories/``), or 0."""

    start = _memory_prefixes_end(word)
    if start == 0 and word[: len(_ALICE_MEMORY_URL)].lower() == _ALICE_MEMORY_URL:
        start = len(_ALICE_MEMORY_URL)
    return start


def _memory_ids_in_text(text: str, *, at_key: bool) -> set[str]:
    """The memory ids ``text`` names. Under a memory key (``at_key``) every id in the text names a memory; anywhere else
    only an id that says so does: the whole string after ``memory:`` prefixes, a word of a list of ids, and any id that
    follows ``memory:`` or ``alice://memories/`` inside longer text. The string is walked once."""

    found: set[str] = set()
    if len(text) < _MIN_ID_CHARS:
        return found
    for whole in (text, text.strip()):
        start = _marked_memory_start(whole)
        if start or at_key:
            candidate = _uuid_text(whole[start:])
            if candidate is not None:
                found.add(candidate)
                return found
    for word in _TOKEN_BREAK.split(text.strip()):
        if not word:
            continue
        start = _marked_memory_start(word)
        if start:
            candidate = _uuid_text(word[start:])
            if candidate is None:
                match = _ID_IN_TEXT.match(word, start)
                candidate = str(UUID(match.group(0))) if match else None
            if candidate is not None:
                found.add(candidate)
        elif at_key:
            candidate = _uuid_text(word)
            if candidate is not None:
                found.add(candidate)
    for match in _ID_IN_TEXT.finditer(text):
        start = match.start()
        if at_key or text[max(0, start - len(_MEMORY_PREFIX)) : start].lower() == _MEMORY_PREFIX or (
            text[max(0, start - len(_ALICE_MEMORY_URL)) : start].lower() == _ALICE_MEMORY_URL
        ):
            found.add(str(UUID(match.group(0))))
    return found


@dataclass(frozen=True, slots=True)
class CitedMemoryIds:
    """The ids a ref names that may be memories, in two groups that are judged differently, as ``CitedSourceIds`` does for sources.

    ``named`` are ids the ref says are memories: an id after a ``memory:`` prefix or an ``alice://memories/`` URL, and an id under
    one of ``MEMORY_REFERENCE_KEYS`` (at any depth), in every spelling ``uuid.UUID`` takes. An id there that names no stored
    memory counts as a missing memory does, so it is refused.

    ``incidental`` are the other ids in the ref, outside the text of a ``quote`` or ``conversation_excerpt``: under a key such as
    ``memory``, ``origin``, ``ref_id``, ``parent_memory_id`` or ``supersedes``, in a sentence (``see memory: <id>``,
    ``Memory <id>``, ``mem:<id>``), in a URL (``https://host/memories/<id>``) or beside punctuation (``[memory](<id>)``). Such an
    id may be a memory, a source, a chunk or a session, and nothing in the ref says which, so it is refused only when it names a
    memory the store holds a row for, a redacted or archived one included, and the caller may not read it. An id that names no
    memory row at all is left alone.
    """

    named: frozenset[str] = frozenset()
    incidental: frozenset[str] = frozenset()

    @property
    def every(self) -> frozenset[str]:
        return self.named | self.incidental

    def __or__(self, other: CitedMemoryIds) -> CitedMemoryIds:
        named = self.named | other.named
        return CitedMemoryIds(named=named, incidental=(self.incidental | other.incidental) - named)


_NO_CITED_MEMORIES = CitedMemoryIds()


def _every_id_in_text(text: str) -> set[str]:
    """Every id ``text`` holds, in any wording around it: the whole string in any spelling ``uuid.UUID`` takes, and each id inside
    longer text that stands apart from hex digits. The string is walked once."""

    found: set[str] = set()
    if len(text) < _MIN_ID_CHARS:
        return found
    whole = _uuid_text(text)
    if whole is not None:
        found.add(whole)
    for match in _ID_IN_TEXT.finditer(text):
        found.add(str(UUID(match.group(0))))
    return found


def cited_memory_refs(value: object) -> CitedMemoryIds:
    """Every id ``value`` (a ref, a list of refs, or a provenance object) may name as a memory, as ``CitedMemoryIds``.

    An id is *named* when it follows a ``memory:`` prefix or an ``alice://memories/`` URL, or stands under one of
    ``MEMORY_REFERENCE_KEYS`` at any depth (and everything nested below such a key is read as a memory reference). Every other
    id in the value is *incidental*. A string that is JSON text (an object or a list) is read as the value it decodes to, and the
    text of a ``quote`` or ``conversation_excerpt`` names nothing, as it names nothing in ``cited_source_ids``: the words of a
    memory that a ref quotes are not read for ids. The walk keeps its own stack, so a ref nested to any depth is read to the end.
    """

    named: set[str] = set()
    every: set[str] = set()
    pending: list[tuple[object, bool]] = [(value, False)]
    while pending:
        node, at_key = pending.pop()
        if isinstance(node, str) or (at_key and isinstance(node, int) and not isinstance(node, bool)):
            text = node if isinstance(node, str) else str(node)
            nested = _json_container(text)
            if nested is not None:
                pending.append((nested, at_key))
                continue
            named |= _memory_ids_in_text(text, at_key=at_key)
            every |= _every_id_in_text(text)
        elif isinstance(node, Mapping):
            for key, child in node.items():
                key_text = key.lower() if isinstance(key, str) else None
                if key_text in _TEXT_KEYS:
                    continue
                if isinstance(key, str):
                    every |= _every_id_in_text(key)
                pending.append((child, at_key or key_text in MEMORY_REFERENCE_KEYS))
        elif isinstance(node, (list, tuple)):
            pending.extend((child, at_key) for child in node)
    return CitedMemoryIds(named=frozenset(named), incidental=frozenset(every - named))


def cited_memory_ids(value: object) -> frozenset[str]:
    """The memory ids ``value`` says are memories (``CitedMemoryIds.named``): the ids behind a ``memory:`` prefix, an
    ``alice://memories/`` URL or one of ``MEMORY_REFERENCE_KEYS``. ``cited_memory_refs`` reads the other ids as well."""

    return cited_memory_refs(value).named


# What a writer was given as a provenance object or a list of refs is read with ``cited_source_ids``. The write fence
# and the link writer keep ``source_uuids_in_ref``, which reads fewer shapes (see its docstring): the reader judges
# what is stored, in every shape, whatever the writer did with it.


_DROPPED = object()
# A ref is a few levels deep. A quote nested deeper than this is withheld with the subtree that holds it, so a hostile depth
# cannot run the rebuild out of stack.
_SCRUB_DEPTH = 100


def _withhold_quote_text(value: object, texts: set[str] | None, depth: int = 0) -> object:
    """``value`` with every ``quote`` and ``conversation_excerpt`` at any depth set to ``None``, the marker a link's withheld
    quote carries. The same object when it holds none. A string that is JSON text is decoded, scrubbed and encoded again.

    ``texts`` collects the words of each quote that is withheld, so a copy of the same words on a link is withheld too.
    """

    if depth > _SCRUB_DEPTH:
        return None
    if isinstance(value, Mapping):
        changed = False
        rebuilt: dict[object, object] = {}
        for key, child in value.items():
            if isinstance(key, str) and key.lower() in _TEXT_KEYS:
                if child is not None:
                    changed = True
                    if texts is not None and (text := _quote_text(child)) is not None:
                        texts.add(text)
                rebuilt[key] = None
                continue
            scrubbed = _withhold_quote_text(child, texts, depth + 1)
            changed = changed or scrubbed is not child
            rebuilt[key] = scrubbed
        return rebuilt if changed else value
    if isinstance(value, (list, tuple)):
        items = [_withhold_quote_text(child, texts, depth + 1) for child in value]
        if all(new is old for new, old in zip(items, value, strict=True)):
            return value
        return items if isinstance(value, list) else tuple(items)
    if isinstance(value, str):
        nested = _json_container(value)
        if nested is None:
            return value
        scrubbed = _withhold_quote_text(nested, texts, depth + 1)
        return value if scrubbed is nested else json.dumps(scrubbed, ensure_ascii=False)
    return value


# The keys whose strings are ids. An entry that names a refused memory keeps what is under them, because an id alone is
# covered by the hidden-ids disclosure; every other string in the entry is text somebody typed.
_ID_KEYS = MEMORY_REFERENCE_KEYS | _REFERENCE_KEYS | _OWN_ID_KEYS


def _is_reference_word(word: str) -> bool:
    """True when ``word`` is an id, or an id behind a marker that says what it names (``memory:``, ``source:``, an
    ``alice://memories/`` or ``alice://sources/`` URL), optionally with a ``#fragment`` after it."""

    start = _marked_memory_start(word) or _source_prefixes_end(word)
    if not start and word[: len(_ALICE_SOURCE_URL)].lower() == _ALICE_SOURCE_URL:
        start = len(_ALICE_SOURCE_URL)
    rest = word[start:]
    if _uuid_text(rest) is not None:
        return True
    found = _ID_IN_TEXT.match(rest)
    return found is not None and (found.end() == len(rest) or rest[found.end()] == "#")


def _is_reference_text(text: str) -> bool:
    """True when ``text`` is made of references and nothing else (``memory:<id>``, a list of ids, an id): nothing a person
    typed. Text with any other word in it (a sentence, a URL, a label) is not."""

    words = [word for word in _TOKEN_BREAK.split(text.strip()) if word]
    return all(_is_reference_word(word) for word in words)


def _withhold_entry_text(value: object, texts: set[str] | None, depth: int = 0, *, id_key: bool = False) -> object:
    """``value``, an entry of a ref list that names a refused memory, with every string in it set to ``None`` except the ids.

    A ``quote`` or ``conversation_excerpt`` at any depth goes, as ``_withhold_quote_text`` does it. So does every other string
    that is not an id: a ``text``, an ``excerpt``, a ``snippet``, a ``note``, or a sentence that has the id in it, because
    the entry says whose words they are and nothing says they are not the memory's. What stays is a string that is a
    reference (an id, ``memory:<id>``, a list of them) and anything under a reference key (``memory_id``, ``source_id``,
    ``id``, ``ref``), and every number and boolean. A string that is JSON text is decoded, held to the same rule and encoded
    again. The same object when nothing changes. ``texts`` collects the words that are withheld, so a copy of them on a link is
    withheld too.
    """

    if depth > _SCRUB_DEPTH:
        return None
    if isinstance(value, Mapping):
        changed = False
        rebuilt: dict[object, object] = {}
        for key, child in value.items():
            lowered = key.lower() if isinstance(key, str) else ""
            if lowered in _TEXT_KEYS:
                if child is not None:
                    changed = True
                    if texts is not None and (text := _quote_text(child)) is not None:
                        texts.add(text)
                rebuilt[key] = None
                continue
            scrubbed = _withhold_entry_text(child, texts, depth + 1, id_key=lowered in _ID_KEYS)
            changed = changed or scrubbed is not child
            rebuilt[key] = scrubbed
        return rebuilt if changed else value
    if isinstance(value, (list, tuple)):
        items = [_withhold_entry_text(child, texts, depth + 1, id_key=id_key) for child in value]
        if all(new is old for new, old in zip(items, value, strict=True)):
            return value
        return items if isinstance(value, list) else tuple(items)
    if isinstance(value, str):
        nested = _json_container(value)
        if nested is not None:
            scrubbed = _withhold_entry_text(nested, texts, depth + 1, id_key=id_key)
            return value if scrubbed is nested else json.dumps(scrubbed, ensure_ascii=False)
        if id_key or _is_reference_text(value):
            return value
        if texts is not None and (text := _quote_text(value)) is not None:
            texts.add(text)
        return None
    return value


def _holds_words(value: object) -> bool:
    """True when ``value`` holds words to withhold from a reader who may not read the memory it cites: a ``quote`` or
    ``conversation_excerpt`` with words in it, or any string that is not an id and not under a reference key, at any depth
    (JSON text included). A list of references alone (``memory:<id>`` and the like) holds none."""

    pending: list[tuple[object, bool]] = [(value, False)]
    while pending:
        node, id_key = pending.pop()
        if isinstance(node, Mapping):
            for key, child in node.items():
                lowered = key.lower() if isinstance(key, str) else ""
                if lowered in _TEXT_KEYS:
                    if _quote_text(child) is not None:
                        return True
                else:
                    pending.append((child, lowered in _ID_KEYS))
        elif isinstance(node, (list, tuple)):
            pending.extend((child, id_key) for child in node)
        elif isinstance(node, str):
            nested = _json_container(node)
            if nested is not None:
                pending.append((nested, id_key))
            elif not id_key and not _is_reference_text(node):
                return True
    return False


def _without_refused_refs(
    refs: object,
    refused: frozenset[str],
    refused_memories: frozenset[str] = frozenset(),
    texts: set[str] | None = None,
) -> object:
    """``refs`` without the entries that name a refused source, and with the words withheld that belong to a refused memory.

    The same list when nothing changes. An entry that names a refused source is dropped, the id with its quote. An entry
    that names a refused memory stays with its ids and loses its strings: the quote and excerpt, every other string that is not
    an id (``_withhold_entry_text``). An entry that names no source and no memory, in a list that holds an entry of either kind,
    loses its quote as well: the commit that cites ``"memory:<id>"`` and then ``{"quote": ...}`` keeps the words in the second
    entry, and nothing in it says whose they are.

    An entry names a memory by any id in it (``cited_memory_refs(...).every``), so a ref that spells the id some way the reader
    has no marker for is held to the same rule once the id names a memory the caller may not read.

    A value that is not a list and names a refused source is returned as ``_DROPPED``, and the caller removes its key.
    """

    if not refused and not refused_memories:
        return refs
    if isinstance(refs, list):
        named = [
            (
                cited_source_ids(ref).every if refused or refused_memories else frozenset(),
                cited_memory_refs(ref).every if refused_memories else frozenset(),
            )
            for ref in refs
        ]
        refused_here = any(
            not refused.isdisjoint(sources) or not refused_memories.isdisjoint(memories) for sources, memories in named
        )
        kept: list[object] = []
        for ref, (sources, memories) in zip(refs, named, strict=True):
            if not refused.isdisjoint(sources):
                continue
            if not refused_memories.isdisjoint(memories):
                kept.append(_withhold_entry_text(ref, texts))
            elif refused_here and not sources and not memories:
                kept.append(_withhold_quote_text(ref, texts))
            else:
                kept.append(ref)
        unchanged = len(kept) == len(refs) and all(new is old for new, old in zip(kept, refs, strict=True))
        return refs if unchanged else kept
    if refused and not refused.isdisjoint(cited_source_ids(refs).every):
        return _DROPPED
    if refused_memories and not refused_memories.isdisjoint(cited_memory_refs(refs).every):
        return _withhold_entry_text(refs, texts)
    return refs


def _scrub_refs_key(
    container: dict[str, object],
    refused: frozenset[str],
    refused_memories: frozenset[str] = frozenset(),
    texts: set[str] | None = None,
) -> None:
    """Drop the entries of ``container["source_refs"]`` that name a refused source and withhold the quotes of the ones that
    name a refused memory, in place, on a copy the caller made."""

    if _SOURCE_REFS_KEY not in container:
        return
    scrubbed = _without_refused_refs(container[_SOURCE_REFS_KEY], refused, refused_memories, texts)
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
    """Every source id a revision names: in its ``previous_value`` and ``new_value``, and in its own ``metadata_json``
    (a memory proposal writes the ``source_refs`` it was given there), which is read as a memory's metadata is."""

    cited = _source_ids_named_by_memory_copies({"metadata_json": row.get("metadata_json")})
    for key in _REVISION_VALUE_KEYS:
        value = row.get(key)
        if isinstance(value, Mapping):
            cited |= cited_source_ids(value.get(_SOURCE_REFS_KEY))
    return cited


def _ref_containers(row: Mapping[str, object]) -> list[Mapping[str, object]]:
    """The mappings of a memory row (or of a revision's ``metadata_json``) that hold a ``source_refs`` list."""

    containers: list[Mapping[str, object]] = []
    metadata = row.get("metadata_json")
    if isinstance(metadata, Mapping):
        containers.append(metadata)
        agentic = metadata.get(_AGENTIC_MEMORY_KEY)
        if isinstance(agentic, Mapping):
            containers.append(agentic)
    value = row.get("value")
    if isinstance(value, Mapping):
        containers.append(value)
    return containers


def _memory_ids_named_by_memory_copies(row: Mapping[str, object]) -> CitedMemoryIds:
    """Every id the memory's own copies may name as a memory, when the row holds any words to withhold, else none.

    A row with no quote, no copy of one and no other text in its refs has nothing to take from a reader, so its memories are not
    looked up: a roll-up card that lists three hundred members costs no read. The ids it names are covered by the hidden-ids
    disclosure.
    """

    metadata = row.get("metadata_json")
    holds = isinstance(metadata, Mapping) and bool(_copy_quote_texts(metadata))
    if not holds:
        holds = any(_holds_words(container.get(_SOURCE_REFS_KEY)) for container in _ref_containers(row))
    if not holds:
        holds = isinstance(metadata, Mapping) and any(_holds_words(metadata.get(key)) for key in _PROVENANCE_OBJECT_KEYS)
    if not holds:
        return _NO_CITED_MEMORIES
    cited = _NO_CITED_MEMORIES
    if isinstance(metadata, Mapping):
        for key in _PROVENANCE_OBJECT_KEYS:
            cited |= cited_memory_refs(metadata.get(key))
    for container in _ref_containers(row):
        cited |= cited_memory_refs(container.get(_SOURCE_REFS_KEY))
    return cited


def _memory_ids_named_by_revision(row: Mapping[str, object]) -> CitedMemoryIds:
    """Every id a revision may name as a memory, read as ``_source_ids_named_by_revision`` reads its sources."""

    cited = _memory_ids_named_by_memory_copies({"metadata_json": row.get("metadata_json")})
    for key in _REVISION_VALUE_KEYS:
        value = row.get(key)
        if isinstance(value, Mapping):
            cited |= _memory_ids_named_by_memory_copies({"metadata_json": value, "value": value})
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


def memory_cited_source_ids(row: Mapping[str, object]) -> frozenset[str]:
    """Every source id a memory row cites, read by the rules the saved-quote reader reads a memory with.

    The reference fields the reader judges (``_source_ids_named_by_memory_copies``) are read first. The rest of
    ``metadata_json`` and of ``value`` is read with ``cited_source_ids`` as well, because other code in the product stores
    the source of a memory under ``source_id``, ``source_ids`` or ``selected_source_ids`` at the top of its metadata, and
    a retirement that looked only at the reader's own fields would leave such a memory behind. The rules are the reader's:
    an id is read in every spelling the link writer reads and in the ones ``uuid.UUID`` accepts, a ref that is JSON text
    is read as the value it decodes to, a ``quote`` or a ``conversation_excerpt`` at any depth names nothing, and a
    ``memory:`` ref names a memory.

    The answer holds the named ids and the incidental ones. A caller asks about a source that is stored, and the reader
    treats an incidental id of a stored source as a citation of it (it withholds the quote and refuses the explanation),
    so the retirement of that source treats it the same way. Provenance links and ``source_event_ids`` are not read here;
    they are columns, and the store matches them by id.
    """

    cited = _source_ids_named_by_memory_copies(row)
    for key in ("metadata_json", "value"):
        cited |= cited_source_ids(row.get(key))
    return cited.every


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
    row: Mapping[str, object],
    *,
    refused: frozenset[str],
    withhold_quotes: bool,
    refused_memories: frozenset[str] = frozenset(),
    texts: set[str] | None = None,
) -> dict[str, object]:
    out = dict(row)
    metadata = row.get("metadata_json")
    if isinstance(metadata, Mapping):
        copy = dict(metadata)
        if withhold_quotes:
            _without_quote_copies(copy)
        _scrub_refs_key(copy, refused, refused_memories, texts)
        agentic = copy.get(_AGENTIC_MEMORY_KEY)
        if isinstance(agentic, Mapping):
            inner = dict(agentic)
            _scrub_refs_key(inner, refused, refused_memories, texts)
            copy[_AGENTIC_MEMORY_KEY] = inner
        out["metadata_json"] = copy
    value = row.get("value")
    if isinstance(value, Mapping) and _SOURCE_REFS_KEY in value:
        scrubbed_value = dict(value)
        _scrub_refs_key(scrubbed_value, refused, refused_memories, texts)
        out["value"] = scrubbed_value
    return out


def _revision_without_refused_refs(
    row: Mapping[str, object],
    *,
    refused: frozenset[str],
    withhold_quotes: bool,
    refused_memories: frozenset[str] = frozenset(),
    texts: set[str] | None = None,
) -> dict[str, object]:
    out = dict(row)
    if isinstance(row.get("metadata_json"), Mapping):
        out["metadata_json"] = _memory_without_refused_provenance(
            {"metadata_json": row["metadata_json"]},
            refused=refused,
            withhold_quotes=withhold_quotes,
            refused_memories=refused_memories,
            texts=texts,
        )["metadata_json"]
    for key in _REVISION_VALUE_KEYS:
        value = row.get(key)
        if not isinstance(value, Mapping):
            continue
        scrubbed = dict(value)
        if withhold_quotes:
            _without_quote_copies(scrubbed)
        _scrub_refs_key(scrubbed, refused, refused_memories, texts)
        agentic = scrubbed.get(_AGENTIC_MEMORY_KEY)
        if isinstance(agentic, Mapping):
            inner = dict(agentic)
            _scrub_refs_key(inner, refused, refused_memories, texts)
            scrubbed[_AGENTIC_MEMORY_KEY] = inner
        out[key] = scrubbed
    return out


def _is_memory_row(value: Mapping[str, object]) -> bool:
    return "id" in value and "memory_key" in value and isinstance(value.get("metadata_json"), Mapping)


def _is_revision_row(value: Mapping[str, object]) -> bool:
    return "memory_id" in value and "revision_number" in value and "revision_type" in value


@dataclass(frozen=True, slots=True)
class _Verdict:
    """What the reader decided about one memory: the sources it cites that the caller may not read, the memories it cites
    that the caller may not read, whether its saved quotes are withheld, and the text of every quote that is withheld (so a
    copy of it elsewhere is withheld too)."""

    refused: frozenset[str]
    withhold_quotes: bool
    texts: frozenset[str]
    refused_memories: frozenset[str] = frozenset()


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
      copies in a revision's ``previous_value``, ``new_value`` and ``metadata_json``) are removed, and the entries of
      the ref lists that name the source (``source_refs`` in the metadata, in ``agentic_memory`` and in ``value``, and
      the same list in a revision's ``previous_value``, ``new_value`` and ``metadata_json``) are dropped;
    * every text that is withheld in either way (the quote of a link that is left out, and every copy that is removed)
      is withheld from the other links of the same memory too: a link whose quote says the same text (ignoring
      whitespace) is shown without its quote. The commit route saves one ``conversation_excerpt`` as the quote of the
      link it makes and as the memory's own copy, and it links only the first id of a reference that names several
      sources, so the quote left on a link to a readable source can be the bytes of a source the caller may not read,
      and the only sign of it is the memory's own copy. The match is equality of the words, which is the relation the
      writers make (see the module docstring); a link whose quote is different text (the link from a captured
      candidate to its own source, or a review's own quote) keeps it.

    * a memory that a ref of the row may name (``cited_memory_refs``: the ids a marker or a memory key says are memories, and
      every other id of the ref outside the text of a quote) and the caller may not read now (missing where a marker says it is a
      memory, archived, redacted, outside the fence, or unverified because an input of it was redacted) withholds the quotes the
      same way, with differences: the entry that names it stays, with its ids, its numbers and its booleans, and every other
      string in it becomes ``None`` (the ``quote`` and ``conversation_excerpt`` and any other text), because an id alone is covered
      by the hidden-ids disclosure. The quote of an entry beside such a ref that names nothing goes too, in a list that holds a
      refused memory or a refused source. An id that no marker says is a memory is refused only when it names a memory the store
      holds a row for. The memories of a row are looked up only when the row holds words to withhold, and not at all for the owner
      and for an unbound admin key, who have no limits. ``events`` holds the events of a feed to the same rule.

    A memory with no link at all (a commit held for review or confirmed inline, then approved) keeps the sources it
    cited only in the ref lists of its own copies, so the reader judges a row by those copies as well. It must be asked
    about a row as the store holds it: a scrub that drops the refs of a row first (the context pack's scope pass does
    this for a pack with a project scope) leaves nothing here to judge, and the quote stays.

    The links of a memory are judged against the memory's own row, the one the reader was last asked about
    (``memory``, ``memories``, ``tree``) and, for a memory it was not asked about, the row the store holds for the id.
    Ask for the memory, then its revisions, then its links: a copy of a quote that only a revision holds is added to
    the withheld texts when the revision is read.

    The owner (``fence.fenced`` is false) is shown what was stored: every method returns its input unchanged and reads
    nothing it was not asked to. A memory whose sources and memories are all admitted is returned as the same object.
    ``audit`` takes the envelope of a memory audit (the row, its revisions, links and events) through the same methods.

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
        self._memory_admitted: dict[str, bool] = {}
        self._memory_exists: dict[str, bool] = {}
        self._cited_memories: dict[int, tuple[Mapping[str, object], CitedMemoryIds]] = {}
        self._memory_limits: bool | None = None

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

    def audit(self, payload: Mapping[str, object]) -> dict[str, object]:
        """A memory audit (``VNextMemoryCommitService.audit``) with what the caller may not read withheld.

        The envelope holds the memory row, its revisions, its provenance links and the payload of its events, and each keeps
        the refs and the quote the memory was written with. They are read in the order the review reads them: the memory,
        then its revisions, its links and its events. A link whose source the caller may not read is left out. The owner is
        given the envelope itself.
        """

        if not self.fenced:
            return payload  # type: ignore[return-value]
        out = dict(payload)
        memory = payload.get("memory")
        memory_id = str(memory.get("id") or "") if isinstance(memory, Mapping) else ""
        if isinstance(memory, Mapping):
            out["memory"] = self.memory(memory)
        revisions = payload.get("revisions")
        if isinstance(revisions, list):
            out["revisions"] = [self.revision(row) if isinstance(row, Mapping) else row for row in revisions]
        links = payload.get("provenance_links")
        if isinstance(links, list):
            shown = [self.shown_link(link) if isinstance(link, Mapping) else link for link in links]
            out["provenance_links"] = [link for link in shown if link is not None]
        events = payload.get("events")
        if isinstance(events, list):
            known = self._rows.get(memory_id)
            withholds = known is not None and self._verdict(memory_id, known, self._links.get(memory_id, [])).withhold_quotes
            out["events"] = [
                self._event(event, memory_withholds=withholds) if isinstance(event, Mapping) else event for event in events
            ]
        return out

    def events(self, rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
        """Event rows as the caller is shown them in a feed of the newest events.

        A memory that is confirmed, edited or approved appends an event whose payload holds the changes it made, and the
        changes hold the refs and the quote the commit was sent with. The feed admits an event by the row it is about, so it
        shows the payload of an event about a commit the caller may read. This holds the payload to the same rule as the
        memory audit holds the events of one memory: a ref that names a source or a memory the caller may not read loses its
        quote (a source's whole entry goes), and so do the copies of the quote the payload saved. The sources and memories of all the
        events are looked up together. The owner is given the rows themselves.
        """

        if not self.fenced:
            return list(rows)  # type: ignore[arg-type]
        sources: list[str] = []
        memories: list[str] = []
        for event in rows:
            payload = event.get("payload_json") if isinstance(event, Mapping) else None
            if not isinstance(payload, Mapping):
                continue
            changes = payload.get("changes")
            for container in (payload, changes) if isinstance(changes, Mapping) else (payload,):
                sources.extend(_source_ids_named_by_memory_copies(container).every)
                memories.extend(_memory_ids_named_by_memory_copies(container).every)
        self._judge(sources)
        self._judge_memories(memories)
        return [self._event(event, memory_withholds=False) if isinstance(event, Mapping) else event for event in rows]  # type: ignore[misc]

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

    def _event(self, event: Mapping[str, object], *, memory_withholds: bool) -> Mapping[str, object]:
        """An event of a memory with the copies in its payload (and in its ``changes``) held to the same fence."""

        payload = event.get("payload_json")
        if not isinstance(payload, Mapping):
            return event
        shown = self._container(payload, memory_withholds=memory_withholds)
        changes = payload.get("changes")
        shown_changes = self._container(changes, memory_withholds=memory_withholds) if isinstance(changes, Mapping) else changes
        if shown is payload and shown_changes is changes:
            return event
        rebuilt = dict(shown)
        if shown_changes is not changes:
            rebuilt["changes"] = shown_changes
        return {**event, "payload_json": rebuilt}

    def _container(self, container: Mapping[str, object], *, memory_withholds: bool) -> Mapping[str, object]:
        """A memory-shaped payload (``metadata_json``, ``value``) with the refs of a refused source dropped and the quotes
        of a refused memory withheld. The same object when it holds nothing to withhold."""

        cited = _source_ids_named_by_memory_copies(container)
        cited_memories = _memory_ids_named_by_memory_copies(container)
        self._judge(cited.every)
        self._judge_memories(cited_memories.every)
        refused = self._refused(cited)
        refused_memories = self._refused_memories(cited_memories)
        metadata = container.get("metadata_json")
        has_copies = isinstance(metadata, Mapping) and bool(_copy_quote_texts(metadata))
        if not refused and not refused_memories and not (memory_withholds and has_copies):
            return container
        return _memory_without_refused_provenance(
            container,
            refused=refused,
            withhold_quotes=bool(refused) or bool(refused_memories) or memory_withholds,
            refused_memories=refused_memories,
        )

    def _verdict(
        self, memory_id: str, row: Mapping[str, object] | None, links: Sequence[Mapping[str, object]]
    ) -> _Verdict:
        """Judge the memory by its links and, when ``row`` is known, by every source and memory its own copies name."""

        cited = self._cited_by(row) if row is not None else _NO_CITED_IDS
        cited_memories = self._memories_cited_by(row) if row is not None else _NO_CITED_MEMORIES
        link_ids = {
            source_id for link in links if (source_id := _canonical_source_id(link.get("source_id"))) is not None
        }
        self._judge([*link_ids, *cited.every])
        self._judge_memories(cited_memories.every)
        refused = self._refused(cited, also_named=link_ids)
        refused_memories = self._refused_memories(cited_memories)
        withhold_quotes = (
            bool(refused)
            or bool(refused_memories)
            or any(_canonical_source_id(link.get("source_id")) is None for link in links)
        )
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
                for container in _ref_containers(row):
                    if _SOURCE_REFS_KEY in container:
                        _without_refused_refs(container[_SOURCE_REFS_KEY], refused, refused_memories, texts)
        return _Verdict(
            refused=refused, withhold_quotes=withhold_quotes, texts=frozenset(texts), refused_memories=refused_memories
        )

    def _memory(self, row: Mapping[str, object]) -> dict[str, object]:
        memory_id = str(row.get("id") or "")
        verdict = self._verdict(memory_id, row, self._links.get(memory_id, []))
        if not verdict.withhold_quotes:
            return row  # type: ignore[return-value]
        return _memory_without_refused_provenance(
            row, refused=verdict.refused, withhold_quotes=True, refused_memories=verdict.refused_memories
        )

    def _revision(self, row: Mapping[str, object]) -> dict[str, object]:
        memory_id = str(row.get("memory_id") or "")
        cited = _source_ids_named_by_revision(row)
        cited_memories = _memory_ids_named_by_revision(row)
        self._judge(cited.every)
        self._judge_memories(cited_memories.every)
        refused = self._refused(cited)
        refused_memories = self._refused_memories(cited_memories)
        known = self._rows.get(memory_id)
        memory_withholds = known is not None and self._verdict(memory_id, known, self._links.get(memory_id, [])).withhold_quotes
        withhold_quotes = bool(refused) or bool(refused_memories) or memory_withholds
        texts: set[str] = set()
        if withhold_quotes:
            texts |= _memory_copy_quote_texts({"metadata_json": row.get("metadata_json")})
        for key in _REVISION_VALUE_KEYS:
            value = row.get(key)
            if withhold_quotes and isinstance(value, Mapping):
                texts |= _copy_quote_texts(value)
        if not refused and not refused_memories and not texts:
            return row  # type: ignore[return-value]
        shown = _revision_without_refused_refs(
            row, refused=refused, withhold_quotes=withhold_quotes, refused_memories=refused_memories, texts=texts
        )
        if texts:
            self._revision_texts.setdefault(memory_id, set()).update(texts)
        return shown

    def _refused(self, cited: CitedSourceIds, *, also_named: Iterable[str] = ()) -> frozenset[str]:
        """The ids of ``cited`` (and ``also_named``) the caller may not be shown. A named id must name a stored source the
        fence admits. An incidental id is refused when it names a source that was stored (an archived one included) and
        the fence does not admit it, and is left alone when it names no source row. Judged already by ``_judge``."""

        return frozenset(
            {source_id for source_id in {*cited.named, *also_named} if not self._admitted.get(source_id, False)}
            | {
                source_id
                for source_id in cited.incidental
                if self._exists.get(source_id, False) and not self._admitted.get(source_id, False)
            }
        )

    def _refused_memories(self, cited: CitedMemoryIds) -> frozenset[str]:
        """The memories of ``cited`` the caller may not be shown. A named id must name a stored memory the caller may read now:
        one that is missing, archived or redacted is refused as one outside the fence is. An incidental id is refused when it
        names a memory that was stored (an archived or redacted one included) and the caller may not read it, and is left alone
        when it names no memory row. Judged already by ``_judge_memories``."""

        return frozenset(
            {memory_id for memory_id in cited.named if not self._memory_admitted.get(memory_id, False)}
            | {
                memory_id
                for memory_id in cited.incidental
                if self._memory_exists.get(memory_id, False) and not self._memory_admitted.get(memory_id, False)
            }
        )

    def _memories_cited_by(self, row: Mapping[str, object]) -> CitedMemoryIds:
        cached = self._cited_memories.get(id(row))
        if cached is not None and cached[0] is row:
            return cached[1]
        cited = _memory_ids_named_by_memory_copies(row)
        self._cited_memories[id(row)] = (row, cited)
        return cited

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
        self._judge_memories(self._memories_cited_by(found).every)
        return found

    def _prefetch(
        self, *, memories: Sequence[Mapping[str, object]], revisions: Sequence[Mapping[str, object]]
    ) -> None:
        """Read the links of every memory row and the sources they and the rows' own copies name, once."""

        self._load_links([str(row.get("id") or "") for row in memories])
        wanted: list[str] = []
        wanted_memories: list[str] = []
        for row in memories:
            memory_id = str(row.get("id") or "")
            if memory_id:
                self._rows[memory_id] = row
            wanted.extend(self._cited_by(row).every)
            wanted_memories.extend(self._memories_cited_by(row).every)
            for link in self._links.get(memory_id, []):
                source_id = _canonical_source_id(link.get("source_id"))
                if source_id is not None:
                    wanted.append(source_id)
        for row in revisions:
            wanted.extend(_source_ids_named_by_revision(row).every)
            wanted_memories.extend(_memory_ids_named_by_revision(row).every)
        self._judge(wanted)
        self._judge_memories(wanted_memories)

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
            rows.update(source_rows_including_archived(self._store, unknown[start : start + _LOOKUP_BATCH]))
        for source_id in unknown:
            row = rows.get(source_id)
            self._exists[source_id] = row is not None
            self._admitted[source_id] = row is not None and self._fence.admits(row)

    def _judge_memories(self, memory_ids: Iterable[str]) -> None:
        """Decide, once per id, whether the caller may read each memory now, and whether the id names a stored memory at all.

        A caller with no limits (the owner never reaches here; an unbound ``admin_agent`` key does) is shown every quote,
        so its memories are not looked up. Any other caller is refused a memory that has a soft-deleted row (archived or
        redacted: the lookup asks for those rows, so the reader can tell them from an id that names no memory), one that has
        no row, where the id is one a ref says is a memory, and one whose effective labels (the stored label raised to every
        input, so a row made from a redacted memory is unverified) its policy does not allow, the test ``alice_explain``
        applies. ``admits_memory`` also refuses a row with ``deleted_at`` set.
        """

        unknown = [memory_id for memory_id in dict.fromkeys(memory_ids) if memory_id not in self._memory_admitted]
        if not unknown:
            return
        if self._memory_limits is None:
            self._memory_limits = self._fence.entity_read_fenced
        if not self._memory_limits:
            self._memory_admitted.update(dict.fromkeys(unknown, True))
            return
        from alicebot_api.vnext_label_guard import LabelGuard

        guard = LabelGuard.for_fence(self._store, self._fence)
        rows: dict[str, Mapping[str, object]] = {}
        for start in range(0, len(unknown), _LOOKUP_BATCH):
            rows.update(memory_rows_including_deleted(self._store, unknown[start : start + _LOOKUP_BATCH]))
        for memory_id in unknown:
            row = rows.get(memory_id)
            self._memory_exists[memory_id] = row is not None
            if row is None:
                self._memory_admitted[memory_id] = False
                continue
            effective = guard.effective_row("memory", row)
            self._memory_admitted[memory_id] = self._fence.admits_memory(effective if isinstance(effective, Mapping) else row)

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
    "CitedMemoryIds",
    "CitedSourceIds",
    "EXPLAIN_DISCLOSURE_ACTION",
    "MEMORY_REFERENCE_KEYS",
    "MEMORY_REF_NOT_FOUND_MESSAGE",
    "MemoryRefNotFoundError",
    "SOURCE_REFERENCE_KEYS",
    "SOURCE_REF_NOT_FOUND_MESSAGE",
    "SavedProvenanceReader",
    "SourceReadFence",
    "SourceRefNotFoundError",
    "cited_memory_ids",
    "cited_memory_refs",
    "cited_source_ids",
    "cited_source_ids_in_memory_audit",
    "memory_cited_source_ids",
    "memory_rows_including_deleted",
    "resolve_attachable_memory_id",
    "resolve_attachable_source_id",
    "resolve_attachable_sources",
    "source_rows_including_archived",
    "source_uuids_in_ref",
]
