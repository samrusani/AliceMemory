"""SQLite source retirement and the derived state owned by a source."""
from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass
from pathlib import Path
from contextlib import nullcontext
from uuid import UUID

from alicebot_api.vault_file_lock import vault_file_lock
from alicebot_api.vnext_stores.memory_lifecycle_common import is_redacted_memory

from alicebot_api.source_supersede import classification_refusal, eligible_source
from alicebot_api.vnext_entities import ENTITY_MENTION_EDGE_TYPE
from alicebot_api.vnext_project_scope import source_project_scope
from alicebot_api.vnext_source_fence import cited_source_ids, memory_cited_source_ids
from alicebot_api.vnext_stores.sqlite.columns import OPEN_LOOP_COLUMNS
from alicebot_api.vnext_stores.sqlite.open_loop_source_reference import named_source_ids
from alicebot_api.vnext_stores.sqlite.primitives import _utc_now_iso
from alicebot_api.vnext_label_writes import takes_label_lock

REMOVAL_MARKER = "[removed by the owner]"


def markdown_sources_by_path(self):
    rows = self._fetch_all(
        "SELECT * FROM sources WHERE user_id = ? AND source_type = 'markdown' "
        "AND connector_name = 'markdown_folder' AND deleted_at IS NULL "
        "ORDER BY captured_at, id", (self.user_id,))
    matches = {}
    for row in rows:
        if eligible_source(row):
            matches.setdefault((row["connector_name"], row["raw_path"]), []).append(row)
    return matches


def prune_sleep_rows(self, source_ids, *, dry_run=False):
    from alicebot_api.vault_sleep import load_sleep_proposals, sleep_proposals_path, _write_jsonl
    database = next((row["file"] for row in self._fetch_all("PRAGMA database_list") if row["name"] == "main"), "")
    if not database:
        return 0
    sidecar = sleep_proposals_path(Path(database))
    # Writers take the SQLite writer lock before the sidecar lock. Sleep's
    # publisher takes the same locks and rechecks liveness after this commits.
    with nullcontext() if dry_run else vault_file_lock(sidecar):
        rows = load_sleep_proposals(sidecar)
        kept = [row for row in rows if not (
            str(row.get("user_id")) == self.user_id and str(row.get("source_id")) in source_ids)]
        removed = len(rows) - len(kept)
        if removed and not dry_run:
            _write_jsonl(sidecar, kept)
        return removed



# -- finding the memories that cite a source ---------------------------------------------------------------------------
#
# A memory cites a source when it has a provenance link to the source or to one of its chunks, when its
# ``source_event_ids`` hold the source id, or when the shared reference reader names the id in its metadata or value
# (``memory_cited_source_ids``: every spelling of the id, in refs that are text, lists, objects or JSON text). The
# reader is exact and slow, so it reads only the memories that one pass over the user's memories keeps. The pass is one
# registered SQLite function, ``alice_citation_hits``: it reads each memory once and answers, for every source asked
# about at once, which of them the memory may cite. It may keep a memory the reader rejects and never drops one the
# reader accepts. A memory is kept for a source in three ways. (1) Its text (metadata and value, lower cased) holds the
# digits of the id once what ``uuid.UUID`` ignores inside an id is taken out (``urn:``, ``uuid:``, hyphens and
# underscores), so an id written with hyphens in other places, in capitals or without hyphens is the same run of digits
# as the id the store holds. (2) Its text holds an ASCII character written as a JSON escape (``\u0061``): a ref that is
# JSON text can hide any character of an id this way and the reader decodes it. Escapes of control characters and of
# letters outside ASCII are what ordinary text has, so they do not count. (3) Its text holds the digits of the id once
# the decimal digits of other scripts are read as the digits 0 to 9 they stand for: ``int(..., 16)`` reads them so, and
# an id written with them holds no run of the ASCII digits of (1). The source event ids are searched for the id as
# written. Each query is one literal, with no text joined into it.
_CITATION_FUNCTION = "alice_citation_hits"
_SCAN_FOR_CITATIONS = """
SELECT m.id AS id, alice_citation_hits(m.metadata_json, m.value, m.source_event_ids, ?) AS hits
FROM memories m WHERE m.user_id = ?
"""
_LINKED_MEMORIES = """
SELECT p.target_id AS memory_id, p.source_id AS source_id FROM provenance_links p
WHERE p.user_id = ? AND p.target_type = 'memory' AND p.source_id IN (SELECT value FROM json_each(?))
UNION
SELECT p.target_id, c.source_id FROM provenance_links p
JOIN source_chunks c ON c.user_id = p.user_id AND c.id = p.source_chunk_id
WHERE p.user_id = ? AND p.target_type = 'memory' AND c.source_id IN (SELECT value FROM json_each(?))
"""
_MEMORIES_BY_ID = "SELECT m.* FROM memories m WHERE m.user_id = ? AND m.id IN (SELECT value FROM json_each(?)) ORDER BY m.id"

# What ``uuid.UUID`` ignores inside an id, in the order it takes them out.
_IGNORED_IN_AN_ID = ("urn:", "uuid:", "-", "_")
# An ASCII character written as a JSON escape, in the lower case text the function reads. A quote, a backslash and the
# control characters are what ordinary JSON writes escaped, so they are left out.
_ESCAPED_ASCII = re.compile(r"\\u00[2-7][0-9a-f]")
# Every character that is not 0 to 9 and that ``int(..., 16)`` reads as one of those digits: the decimal digits
# (category Nd) of the other scripts, as the Unicode tables of this interpreter name them.
_OTHER_SCRIPT_DIGIT = re.compile(r"[^\D0-9]")
# A JSON escape of one character: ``\uXXXX``, and the pair of escapes ``json`` writes for a character beyond the first
# 65,536 (the mathematical digits are such characters). The single escape is matched only where the code point starts
# with 0, 1, a or f, because every decimal digit of the Basic Multilingual Plane lies in such a block. A JSON text
# inside a JSON text doubles each backslash of the inner one, so a run of backslashes is read as one. A match starts only
# at the first backslash of a run: a start at each of them would read the rest of the run again, and a text of many
# backslashes would cost the square of its length. Only an escape that stands for such a digit is replaced
# (``_digit_escape``): any other escape stands for a character that is neither a digit nor ignored, so no run of an id
# goes through it.
_DIGIT_ESCAPE = re.compile(r"(?<!\\)\\+u(?:(d[89ab][0-9a-f]{2})\\+u(d[c-f][0-9a-f]{2})|([01af][0-9a-f]{3}))")


class _AsciiDigits(dict):
    """The digit 0 to 9 that each decimal digit of another script stands for, as the text of it, learned on first use."""

    def __missing__(self, char):
        digit = self[char] = str(int(char))
        return digit


_ASCII_DIGIT = _AsciiDigits()


class _Probes:
    """What one pass looks for. ``sources`` lists ``(source id, digits or None)`` in the order the caller numbers them:
    the digits are what a text must hold to cite the source (``_citation_probe``), and ``None`` is for a string that
    is no id, which no text can cite."""

    def __init__(self, sources):
        self.ids = tuple(source_id for source_id, _ in sources)
        self.digits = tuple((index, digits) for index, (_, digits) in enumerate(sources) if digits is not None)
        self.every = frozenset(range(len(sources)))
        self.every_text = frozenset(index for index, _ in self.digits)
        self.everything = ",".join(str(index) for index in range(len(sources)))


def _digit_escape(match):
    high, low, single = match.groups()
    code = int(single, 16) if single is not None else (
        0x10000 + ((int(high, 16) - 0xD800) << 10) + (int(low, 16) - 0xDC00))
    char = chr(code)
    return char if char.isdecimal() and not char.isascii() else match.group()


def _in_ascii_digits(text):
    """``text`` with each decimal digit of another script, written as the character or as a JSON escape of it
    (``\\u0661`` or ``\\ud835\\udfce``), written as the digit 0 to 9 it stands for, or ``None`` when it holds none. An
    escape is read wherever it stands, inside a string or not: a text that holds one it should not has only made the
    candidate list longer, and the reader decides."""

    if "\\u" in text:
        text = _DIGIT_ESCAPE.sub(_digit_escape, text)
    # No decimal digit of another script has a code point below U+0660, so a text of Latin-1 characters (French, German,
    # Spanish, Portuguese, Italian) holds none, and ``encode`` tells it at a fraction of the cost of the search.
    if text.isascii() or len(text.encode("latin-1", "ignore")) == len(text) or not _OTHER_SCRIPT_DIGIT.search(text):
        return None
    return _OTHER_SCRIPT_DIGIT.sub(lambda match: _ASCII_DIGIT[match[0]], text)


def _without_ignored(text):
    for ignored in _IGNORED_IN_AN_ID:
        text = text.replace(ignored, "")
    return text


def _add_text_hits(text, probes, hits):
    """Add to ``hits`` the numbers of the sources that this lower case text may cite."""

    escaped = "\\u" in text
    if escaped and _ESCAPED_ASCII.search(text):
        hits |= probes.every_text
        return
    stripped = _without_ignored(text)
    for index, digits in probes.digits:
        if digits in stripped:
            hits.add(index)
    if len(hits) < len(probes.every) and (escaped or not text.isascii()):
        read = _in_ascii_digits(text)
        if read is not None:
            stripped = _without_ignored(read)
            for index, digits in probes.digits:
                if digits in stripped:
                    hits.add(index)


def _text_of(column):
    return column if isinstance(column, str) else "" if column is None else str(column)


def _citation_hits(metadata_json, value, source_event_ids, probes):
    """The body of ``alice_citation_hits``: the numbers of the sources a memory may cite, separated by commas, or an
    empty string. The metadata and the value are read as one text, once for all the sources (a newline stands between
    them, so no run of digits crosses from one to the other). The source event ids are searched for each id as it is
    written (the store then reads the decoded list), and a list that holds a backslash may hide an id behind an escape,
    so it keeps every source."""

    hits = set()
    events = _text_of(source_event_ids)
    if events and events != "[]":
        if "\\" in events:
            return probes.everything
        hits.update(index for index, source_id in enumerate(probes.ids) if source_id in events)
    if len(hits) < len(probes.every):
        _add_text_hits((_text_of(metadata_json) + "\n" + _text_of(value)).lower(), probes, hits)
    return ",".join(str(index) for index in sorted(hits)) if hits else ""


def _citation_probe(source_id):
    """``(canonical id, digits the stored text must hold)`` for a source id, or ``(None, None)`` for a string that is
    no id. The digits drop the leading zeros: ``uuid.UUID`` reads a string of 32 characters where whitespace, ``0x``, a
    sign or an underscore stands in the place of those zeros."""

    try:
        parsed = UUID(str(source_id))
    except ValueError:
        return None, None
    return str(parsed), parsed.hex.lstrip('0')


# What each running pass looks for, by the number the pass hands to the function. The function is registered once per
# connection and a registered function cannot be replaced while any statement of the connection is active, so the
# sources are not part of the function: the pass states its number, which is never used twice.
_RUNNING_PASSES: dict[int, _Probes] = {}
_PASS_NUMBERS = itertools.count(1)


def _citation_function(metadata_json, value, source_event_ids, pass_number):
    return _citation_hits(metadata_json, value, source_event_ids, _RUNNING_PASSES[pass_number])


def _ensure_citation_function(conn):
    """Register ``alice_citation_hits`` once per SQLite connection. It is registered by the lookup that reads it, not
    when the store is opened, so every other use of a store connection is unchanged."""

    cursor = conn.execute("SELECT 1 FROM pragma_function_list WHERE name = ? AND narg = 4 LIMIT 1", (_CITATION_FUNCTION,))
    try:
        registered = cursor.fetchone() is not None
    finally:
        cursor.close()
    if not registered:
        conn.create_function(_CITATION_FUNCTION, 4, _citation_function, deterministic=True)


def _citation_candidates(self, probes):
    """``{memory id: [number of each source it may cite]}`` for the user's memories, from one pass."""

    _ensure_citation_function(self.conn)
    pass_number = next(_PASS_NUMBERS)
    _RUNNING_PASSES[pass_number] = probes
    try:
        cursor = self._execute(_SCAN_FOR_CITATIONS, (pass_number, self.user_id))
        try:
            # Plain tuples: the pass answers for every memory of the user, and a dict for each of them costs more than
            # the pass itself reads.
            cursor.row_factory = None
            return {str(memory_id): [int(index) for index in kept.split(',')] for memory_id, kept in cursor if kept}
        finally:
            cursor.close()
    finally:
        del _RUNNING_PASSES[pass_number]


def _memories_by_id(self, memory_ids):
    """The user's memories among ``memory_ids``, in id order, as they are stored now."""

    memory_ids = list(memory_ids)
    return self._fetch_all(_MEMORIES_BY_ID, (self.user_id, json.dumps(memory_ids))) if memory_ids else []


def citing_memories_by_source(self, source_ids):
    """``{source id: [memory rows]}``: every memory of the user that cites each source, by a provenance link to the
    source or to one of its chunks, by ``source_event_ids``, or by a reference the shared reference reader names
    (``memory_cited_source_ids``: every spelling of the id, in refs that are text, lists, objects or JSON text). Each
    list is in id order.

    One pass over the memories answers for every source asked about, so a caller that retires many sources asks once
    for all of them. The pass keeps the memories that could hold an id in some spelling, and the reader reads each of
    those once, however many sources it may cite.
    """

    # No bounded list: a scrub must cover every candidate, including old rows
    # whose only reference is in value or metadata rather than a provenance link.
    ids = list(dict.fromkeys(str(source_id) for source_id in source_ids))
    if not ids:
        return {}
    canonical = {}
    sources = []
    for source_id in ids:
        canonical[source_id], digits = _citation_probe(source_id)
        sources.append((source_id, digits))
    linked = {}
    for row in self._fetch_all(_LINKED_MEMORIES, (self.user_id, json.dumps(ids), self.user_id, json.dumps(ids))):
        linked.setdefault(str(row['memory_id']), set()).add(str(row['source_id']))
    kept = {memory_id: {ids[index] for index in indexes}
            for memory_id, indexes in _citation_candidates(self, _Probes(sources)).items()}
    found = {source_id: [] for source_id in ids}
    for row in _memories_by_id(self, sorted(linked.keys() | kept.keys())):
        memory_id = str(row['id'])
        cited = set(linked.get(memory_id, ()))
        events = row['source_event_ids'] if isinstance(row['source_event_ids'], list) else []
        named = None
        for source_id in sorted(kept.get(memory_id, set()) - cited):
            if source_id in events:
                cited.add(source_id)
            elif canonical[source_id] is not None:
                if named is None:
                    named = memory_cited_source_ids(row)
                if canonical[source_id] in named:
                    cited.add(source_id)
        for source_id in sorted(cited):
            found[source_id].append(row)
    return found


def citing_memories(self, source_id):
    """Every memory of the user that cites ``source_id``: see ``citing_memories_by_source``."""

    return citing_memories_by_source(self, [source_id])[str(source_id)]


# -- finding the graph edges that name a source -------------------------------------------------------------------------
#
# An edge names a source in two ways. An END of the edge (``from_id`` or ``to_id``, whatever ``from_type`` and
# ``to_type`` say) is the source or one of its chunks, in any spelling ``cited_source_ids`` names: capitals, no hyphens,
# braces, ``urn:uuid:``, a ``source:`` prefix. Or its ``explanation`` or ``metadata_json`` names the source, as a
# connection edge does in ``metadata_json.connection.source_item``, while neither end does. The product's own SQLite
# writers make only mention edges, but ``alice-memory import`` restores an edge of any type and shape (the edges of a
# PostgreSQL vault included), so retirement cannot look at mention edges from a ``source`` end alone.
#
# One pass over the user's edges answers for every source of the command. It reads only the five columns it needs, keeps
# the edges whose text could hold an id (the same digit rule the memory pass uses, which may keep an edge the reader
# rejects and never drops one the reader accepts), and the reader decides on those.
_SCAN_FOR_EDGES = "SELECT id, from_id, to_id, explanation, metadata_json FROM graph_edges WHERE user_id = ?"
_EDGES_BY_ID = "SELECT * FROM graph_edges WHERE user_id = ? AND id IN (SELECT value FROM json_each(?)) ORDER BY id"
_CHUNKS_OF_SOURCES = (
    "SELECT id, source_id FROM source_chunks WHERE user_id = ? AND source_id IN (SELECT value FROM json_each(?))"
)


@dataclass(frozen=True, slots=True)
class EdgeHits:
    """The ids of the edges that name one source: ``ends`` have the source (or a chunk of it) at an end and are
    closed and blanked, ``notes`` name it only in their text and keep their link, with the text blanked."""

    ends: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.ends) + len(self.notes)


def _canonical_or_none(value):
    try:
        return str(UUID(str(value)))
    except ValueError:
        return None


# An id in the one spelling the store writes. A string in that spelling that is not the id of a known chunk names none.
_STORED_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _may_name_a_chunk(end, chunk_owner):
    if not isinstance(end, str) or len(end) < 32:
        return False
    if end in chunk_owner:
        return True
    return _STORED_ID.fullmatch(end) is None and any(
        canonical in chunk_owner for canonical in cited_source_ids(end).every)


def _edge_name_sets(row, wanted, chunk_owner, *, with_notes):
    """``(ends, notes)``: the canonical ids among the keys of ``wanted`` that one decoded edge names at an end, and the ones
    it names only in its text. ``wanted`` maps a canonical id to the ids as the caller gave them; ``chunk_owner`` maps the
    canonical id of a chunk to the canonical id of its source."""

    ends = set()
    for field in ("from_id", "to_id"):
        value = row.get(field)
        if value is None:
            continue
        for canonical in cited_source_ids(str(value)).every:
            ends.add(canonical)
            owner = chunk_owner.get(canonical)
            if owner is not None:
                ends.add(owner)
    notes = set()
    if with_notes:
        notes |= cited_source_ids(row.get("metadata_json")).every
        explanation = row.get("explanation")
        if isinstance(explanation, str) and explanation:
            notes |= cited_source_ids(explanation).every
    return ends & wanted.keys(), (notes - ends) & wanted.keys()


def _edge_candidates(self, probes, chunk_owner, *, with_text):
    """The ids of the user's edges that could name one of the probed ids (see ``_Probes``) or a chunk of a source."""

    kept = []
    cursor = self._execute(_SCAN_FOR_EDGES, (self.user_id,))
    try:
        # Plain tuples: the pass reads every edge of the user.
        cursor.row_factory = None
        for edge_id, from_id, to_id, explanation, metadata_json in cursor:
            parts = (_text_of(from_id), _text_of(to_id))
            if with_text:
                parts += (_text_of(explanation), _text_of(metadata_json))
            hits = set()
            _add_text_hits("\n".join(parts).lower(), probes, hits)
            if hits:
                kept.append(str(edge_id))
                continue
            if chunk_owner and (_may_name_a_chunk(from_id, chunk_owner) or _may_name_a_chunk(to_id, chunk_owner)):
                kept.append(str(edge_id))
    finally:
        cursor.close()
    return kept


def _edges_by_id(self, edge_ids):
    """The user's edges among ``edge_ids``, in id order, as they are stored now."""

    edge_ids = list(dict.fromkeys(str(edge_id) for edge_id in edge_ids))
    return self._fetch_all(_EDGES_BY_ID, (self.user_id, json.dumps(edge_ids))) if edge_ids else []


def _edges_naming(self, ids, *, with_chunks, with_text):
    """``{id as given: EdgeHits}`` for the ids asked about, from one pass over the user's edges."""

    ids = list(dict.fromkeys(str(item) for item in ids))
    if not ids:
        return {}
    found = {item: ([], []) for item in ids}
    wanted = {}
    sources = []
    for item in ids:
        canonical, digits = _citation_probe(item)
        sources.append((item, digits))
        if canonical is not None:
            wanted.setdefault(canonical, []).append(item)
    chunk_owner = {}
    if with_chunks:
        for chunk in self._fetch_all(_CHUNKS_OF_SOURCES, (self.user_id, json.dumps(ids))):
            owner = _canonical_or_none(chunk["source_id"])
            if owner is not None:
                chunk_owner[str(chunk["id"])] = owner
    probes = _Probes(sources)
    for edge in _edges_by_id(self, _edge_candidates(self, probes, chunk_owner, with_text=with_text)):
        ends, notes = _edge_name_sets(edge, wanted, chunk_owner, with_notes=with_text)
        for canonical in ends:
            for item in wanted[canonical]:
                found[item][0].append(str(edge["id"]))
        for canonical in notes:
            for item in wanted[canonical]:
                found[item][1].append(str(edge["id"]))
    return {item: EdgeHits(tuple(ends), tuple(notes)) for item, (ends, notes) in found.items()}


def edges_naming_sources(self, source_ids):
    """``{source id: EdgeHits}``: every edge of the user that names each source, of any edge type, at either end or in its
    text, in any spelling ``cited_source_ids`` names. One pass answers for every source asked about."""

    return _edges_naming(self, source_ids, with_chunks=True, with_text=True)


def edges_at_memories(self, memory_ids):
    """``{memory id: EdgeHits}`` for the edges that have a memory at an end, of any edge type, in any spelling. A memory is
    named by its ends only: a ``memory:`` ref in text names a memory and the source reader skips it."""

    return _edges_naming(self, memory_ids, with_chunks=False, with_text=False)


def _blank_edge_text(self, edges):
    for edge in edges:
        self._execute("UPDATE graph_edges SET explanation = '', metadata_json = '{}' WHERE id = ? AND user_id = ?",
                      (edge["id"], self.user_id))


def _retire_entity_if_unused(self, entity_id, now):
    """Replace the name of a linker-made entity that no live edge reaches any more."""

    self._execute(
        """UPDATE vnext_entities SET name = ?, normalized_name = 'removed:' || id,
        aliases = '[]', metadata_json = '{}', deleted_at = ?, updated_at = ?
        WHERE id = ? AND user_id = ? AND deleted_at IS NULL
        AND json_extract(metadata_json, '$.created_by') = 'vnext_entity_linker'
        AND NOT EXISTS (SELECT 1 FROM graph_edges e WHERE e.user_id = vnext_entities.user_id
            AND e.valid_to IS NULL AND ((e.to_type = 'entity' AND e.to_id = vnext_entities.id)
            OR (e.from_type = 'entity' AND e.from_id = vnext_entities.id)))""",
        (REMOVAL_MARKER, now, now, entity_id, self.user_id))


def _close_edges(self, edges, now):
    """Close and blank each edge, count the entity mention it ends and retire an entity it was the last live edge of."""

    for edge in edges:
        # Clear both surface and normalized aliases, even on an already closed edge.
        self._execute(
            "UPDATE graph_edges SET valid_to = COALESCE(valid_to, MAX(?, COALESCE(valid_from, ?))), "
            "explanation = '', metadata_json = '{}' WHERE id = ? AND user_id = ?",
            (now, now, edge['id'], self.user_id))
        if (edge.get('valid_to') is None and edge['edge_type'] == ENTITY_MENTION_EDGE_TYPE
                and edge['to_type'] == 'entity'):
            self._execute(
                "UPDATE vnext_entities SET mention_count = MAX(0, mention_count - 1), updated_at = ? "
                "WHERE id = ? AND user_id = ?", (now, edge['to_id'], self.user_id))
        for kind, entity_id in ((edge['from_type'], edge['from_id']), (edge['to_type'], edge['to_id'])):
            if kind == 'entity':
                _retire_entity_if_unused(self, entity_id, now)
    return len(edges)


def retire_edges(self, hits, now):
    """Retire the edges of one ``EdgeHits``: close and blank the edges with the source at an end, blank the text of the
    ones that name it only in their text. Rows are read as they are stored now, so an edge that an earlier retirement of
    the same command closed is blanked again and counted once. Returns ``(graph edges, mention edges)``."""

    ended = _edges_by_id(self, hits.ends)
    _close_edges(self, ended, now)
    _blank_edge_text(self, _edges_by_id(self, hits.notes))
    mentions = sum(1 for edge in ended if edge['edge_type'] == ENTITY_MENTION_EDGE_TYPE and edge['to_type'] == 'entity')
    return hits.total, mentions


def close_mention_edges(self, from_type, from_id, now):
    edges = self._fetch_all(
        "SELECT * FROM graph_edges WHERE user_id = ? AND from_type = ? AND from_id = ? "
        "AND to_type = 'entity' AND edge_type = ?",
        (self.user_id, from_type, from_id, ENTITY_MENTION_EDGE_TYPE))
    return _close_edges(self, edges, now)


# One read of the user's loops. The column list is a module constant, and the user id is bound.
_USER_OPEN_LOOPS_SQL = (
    f"SELECT {', '.join(OPEN_LOOP_COLUMNS)} FROM open_loops WHERE user_id = ? "  # nosec B608
    "ORDER BY updated_at DESC, created_at DESC, id DESC"
)
_BLANK_OPEN_LOOPS_SQL = """UPDATE open_loops SET title = ?, description = ?, status = 'dismissed',
resolved_at = ?, closed_at = ?, resolution_note = ?, metadata_json = '{}', updated_at = ?
WHERE user_id = ? AND id IN (SELECT value FROM json_each(?))"""


def _user_open_loops(self):
    """Every open loop of the user, newest first. One call is one pass."""

    return self._fetch_all(_USER_OPEN_LOOPS_SQL, (self.user_id,))


def open_loops_naming_sources(self, source_ids):
    """``{source id: [loop rows]}`` for the loops of the user that name each source.

    One pass over the user's loops answers for every source. A loop names a source when ``named_source_ids`` says so,
    which is every spelling ``cited_source_ids`` names. The lists keep the read order.
    """

    ids = list(dict.fromkeys(str(source_id) for source_id in source_ids))
    found = {source_id: [] for source_id in ids}
    if not ids:
        return found
    wanted: dict[str, list[str]] = {}
    for source_id in ids:
        try:
            canonical = str(UUID(source_id))
        except ValueError:
            canonical = source_id
        wanted.setdefault(canonical, []).append(source_id)
    for row in _user_open_loops(self):
        for canonical in named_source_ids(row):
            for source_id in wanted.get(canonical, ()):
                found[source_id].append(row)
    return found


def source_open_loop_count(self, source_id, *, named=None):
    """How many loops of the user, in any status, name ``source_id``.

    ``named`` is the answer of ``open_loops_naming_sources`` for every source of one command, so a preview asks once.
    """

    if named is None:
        named = open_loops_naming_sources(self, [source_id])
    return len(named[str(source_id)])


def blank_open_loops(self, source_id, *, now, loop_ids=None):
    """Dismiss every loop that names ``source_id`` and blank its free text and metadata.

    ``loop_ids`` are the ids an earlier pass of this command found. Without them the loops are read here. An empty
    list is an answer and does not read again. Returns the number of loops.
    """

    if loop_ids is None:
        loop_ids = [str(row["id"]) for row in open_loops_naming_sources(self, [source_id])[str(source_id)]]
    loop_ids = [str(loop_id) for loop_id in loop_ids]
    if not loop_ids:
        return 0
    return self._execute(
        _BLANK_OPEN_LOOPS_SQL,
        (REMOVAL_MARKER, REMOVAL_MARKER, now, now, REMOVAL_MARKER, now, self.user_id, json.dumps(loop_ids)),
    ).rowcount


def retire_dependents(self, source_id, *, now, scrub_candidates=False, citing_ids=None, loop_ids=None, edge_hits=None):
    """Retire what a source owns. ``citing_ids`` are the ids of the memories an earlier lookup of this transaction found
    for the source (``citing_memories_by_source``): a caller that retires many sources looks once for all of them, and the
    memories are read again here as they are stored now, so one that an earlier retirement redacted is left alone, as it
    is when each source is looked up on its own. Without them the memories are looked up here. ``edge_hits`` is the
    ``EdgeHits`` that ``edges_naming_sources`` found for the source, used the same way."""

    memories = [row for row in (citing_memories(self, source_id) if citing_ids is None
                                else _memories_by_id(self, citing_ids)) if not is_redacted_memory(row)]
    pending = [row for row in memories if row['status'] in ({'candidate', 'needs_review', 'rejected'}
               if scrub_candidates else {'candidate', 'needs_review'})]
    pending_ids = {str(row['id']) for row in pending}
    retained = [str(row['id']) for row in memories if not scrub_candidates or str(row['id']) not in pending_ids]
    for memory in pending:
        mid = str(memory['id'])
        if scrub_candidates:
            try:
                self.redact_memory_bundle(memory_id=mid, project_update_artifacts=[], actor_type='user')
                if not self.memory_redaction_bundle_is_exact(mid, []):
                    raise ValueError('Candidate bundle is incomplete')
            except Exception as exc:
                raise CandidateScrubRefused([str(row['id']) for row in pending]) from exc
            close_mention_edges(self, 'memory', mid, now)
        else:
            self.update_memory(memory_id=mid, patch={'status': 'rejected'}, actor_type='user')
            close_mention_edges(self, 'memory', mid, now)
    if scrub_candidates and pending_ids:
        # The candidates' text is gone, so an edge of any type that has one of them at an end goes too.
        for hits in edges_at_memories(self, sorted(pending_ids)).values():
            _close_edges(self, _edges_by_id(self, hits.ends), now)
    if edge_hits is None:
        edge_hits = edges_naming_sources(self, [source_id])[str(source_id)]
    graph_edges, mention_edges = retire_edges(self, edge_hits, now)
    loops = blank_open_loops(self, source_id, now=now, loop_ids=loop_ids)
    return {'candidate_memories': len(pending), 'mention_edges': mention_edges, 'graph_edges': graph_edges,
            'open_loops': loops, 'memories_citing_replaced': retained}


@takes_label_lock
def supersede_source(self, source_id, *, superseded_by, allow_looser_classification=False, dry_run=False, loop_ids=None,
                     edge_hits=None):
    with self.savepoint():
        old = self.get_source(source_id)
        new = self.get_source(superseded_by)
        if old is None or new is None or source_id == superseded_by:
            raise ValueError('Source replacement requires two live sources')
        if not eligible_source(old) or not eligible_source(new) or (
                old['raw_path'], old['connector_name']) != (new['raw_path'], new['connector_name']):
            raise ValueError('Source replacement requires the same Markdown path')
        reason = classification_refusal([old], domain=new['domain'], sensitivity=new['sensitivity'],
                                        project_scope=source_project_scope(new),
                                        allow_looser=allow_looser_classification)
        if reason:
            raise ValueError(reason)
        now = _utc_now_iso()
        # The sidecar goes first. A later rollback may lose proposals, which
        # can be generated again, but cannot leave retired evidence in it.
        prune_sleep_rows(self, {source_id}, dry_run=dry_run)
        if not dry_run:
            from alicebot_api.vnext_label_writes import raise_source_to_replacement

            raise_source_to_replacement(self, old, new)
        counts = retire_dependents(self, source_id, now=now, loop_ids=loop_ids, edge_hits=edge_hits)
        metadata = {**old['metadata_json'], 'superseded_by': superseded_by,
                    'superseded_at': now, 'supersede_reason': 'markdown_reimport'}
        self._execute("UPDATE sources SET deleted_at = ?, metadata_json = ? WHERE id = ? AND user_id = ?",
                      (now, json.dumps(metadata), source_id, self.user_id))
        self._append_mutation_event(event_type='source.superseded', target_type='source', target_id=source_id,
                                    actor_type='user', payload={'superseded_by': superseded_by, **counts})
        return counts


class CandidateScrubRefused(ValueError):
    def __init__(self, candidate_ids):
        super().__init__("Candidate memories could not be scrubbed")
        self.candidate_ids = candidate_ids


def source_inventory(self, *, query=None, superseded=False, all_sources=False, limit=50):
    if not 1 <= limit <= 1000:
        raise ValueError('Source list limit must be between 1 and 1000')
    view = "replaced" if superseded else "all" if all_sources else "live"
    rows = self._fetch_all(
        """SELECT s.*, (SELECT count(*) FROM source_chunks c
        WHERE c.user_id = s.user_id AND c.source_id = s.id) AS chunk_count
        FROM sources s WHERE s.user_id = ?
        AND ((s.deleted_at IS NULL AND ? != 'replaced')
          OR (s.deleted_at IS NOT NULL AND ? != 'live'
              AND json_extract(s.metadata_json, '$.superseded_by') IS NOT NULL))
        AND COALESCE(json_extract(s.metadata_json, '$.scrubbed'), 0) != 1
        ORDER BY s.captured_at DESC, s.id""",
        (self.user_id, view, view))
    if query:
        query = query.casefold()
        rows = [row for row in rows if any(query in str(value or '').casefold() for value in (
            row['id'], row['title'], row['external_id'], row['metadata_json'].get('relative_path')))]
    return rows[:limit]


def prunable_sources(self, *, older_than=None):
    if older_than is not None and not 0 <= older_than <= 2**63 - 1:
        raise ValueError('Source age must be between 0 and 9223372036854775807 days')
    return self._fetch_all(
        "SELECT * FROM sources WHERE user_id = ? AND deleted_at IS NOT NULL "
        "AND json_extract(metadata_json, '$.superseded_by') IS NOT NULL "
        "AND COALESCE(json_extract(metadata_json, '$.scrubbed'), 0) != 1 "
        "AND (? IS NULL OR julianday(json_extract(metadata_json, '$.superseded_at')) <= julianday(?) - ?) "
        "ORDER BY deleted_at, id", (self.user_id, older_than, _utc_now_iso(), older_than))


def count_prunable_sources(self):
    row = self._fetch_one('count replaced sources',
        "SELECT count(*) AS count FROM sources WHERE user_id = ? AND deleted_at IS NOT NULL "
        "AND json_extract(metadata_json, '$.superseded_by') IS NOT NULL "
        "AND COALESCE(json_extract(metadata_json, '$.scrubbed'), 0) != 1", (self.user_id,))
    return int(row['count'])


def optimize_scrub_indexes(self):
    # FTS5 deletion postings keep old terms until segments merge. Ordinary
    # row updates and VACUUM do not remove those terms from live index pages.
    self._execute("INSERT INTO source_chunks_fts(source_chunks_fts) VALUES('optimize')")
    self._execute("INSERT INTO memories_fts(memories_fts) VALUES('optimize')")


@takes_label_lock
def scrub_source(self, source_id, *, optimize=True, citing_ids=None, loop_ids=None, edge_hits=None):
    with self.savepoint():
        self._execute("PRAGMA secure_delete=ON")
        rows = self.get_sources_by_ids([source_id], include_deleted=True)
        if not rows or rows[0]['metadata_json'].get('scrubbed'):
            raise ValueError('Source is unknown or already scrubbed')
        now = _utc_now_iso()
        sleep_count = prune_sleep_rows(self, {source_id})
        self._execute(
            """UPDATE sources SET deleted_at = COALESCE(deleted_at, ?), title = NULL, author = NULL,
            uri = NULL, raw_path = NULL, external_id = NULL, metadata_json = ?
            WHERE id = ? AND user_id = ?""",
            (now, json.dumps({'scrubbed': True, 'scrubbed_at': now}), source_id, self.user_id))
        chunks = self._execute(
            "UPDATE source_chunks SET text = ?, metadata_json = '{}' WHERE source_id = ? AND user_id = ?",
            (REMOVAL_MARKER, source_id, self.user_id)).rowcount
        quotes = self._execute(
            """UPDATE provenance_links SET quote = ? WHERE user_id = ? AND (source_id = ?
            OR EXISTS (SELECT 1 FROM source_chunks c WHERE c.user_id = provenance_links.user_id
                       AND c.id = provenance_links.source_chunk_id AND c.source_id = ?))""",
            (REMOVAL_MARKER, self.user_id, source_id, source_id)).rowcount
        counts = retire_dependents(self, source_id, now=now, scrub_candidates=True, citing_ids=citing_ids,
                                   loop_ids=loop_ids, edge_hits=edge_hits)
        counts.update({'chunks':chunks, 'provenance_quotes':quotes, 'sleep_proposals':sleep_count})
        self._append_mutation_event(event_type='source.deleted', target_type='source', target_id=source_id,
            actor_type='user', payload={'operation':'scrub', **counts})
        if optimize:
            optimize_scrub_indexes(self)
        return counts
