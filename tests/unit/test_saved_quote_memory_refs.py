"""The reader of saved quotes withholds the quote of a memory the caller may not read, with a stub store.

Unreleased (on main, not in v0.20.0). A commit can cite a memory, as ``{"memory_id": "<id>", "quote": "..."}`` or as the two
entries ``"memory:<id>"`` and ``{"quote": "..."}``, and the quote holds words of that memory. The reader
(``SavedProvenanceReader``) already withheld the quote of a source the caller may not read. These tests hold the same
reader to the same rule for a memory, with a stub store whose reads are counted:

* ``cited_memory_ids`` reads a memory ref in every spelling the reader accepts, and reads nothing it should not;
* a memory the caller may not read (missing, archived, redacted, over the ceiling, in a domain or project the caller may not
  read) takes the quote of every ref that names it, and the saved copies of the quote, and leaves the id;
* a quote that stands beside such a ref and names nothing goes with it, beside a refused source as well;
* an id in a ref that no marker says is a memory (``Memory <id>``, ``mem:<id>``, a URL, ``alice://memory/<id>``, an id under
  ``memory``, ``origin``, ``ref_id``, ``supersedes``) is looked up as well, and refused the caller when it names a memory the
  store holds a row for, a redacted or archived one included, that the caller may not read; an id that names no memory row
  is left alone;
* an entry that names a refused memory loses every string that is not an id, not the quote alone;
* the owner and an unbound admin key are shown what was stored and cost no read;
* a row with no quote to withhold costs no read, and the memories of one that has are looked up in slices;
* the events of a feed are held to the same rule as the events of one memory's audit.

``tests/unit/test_saved_quote_memory_refs_vault.py`` runs the same rules on a real vault, with real keys, at every door.
Each test names the mutation that must fail it, and ``scripts/derived_label_mutations.json`` replays it.
"""

from __future__ import annotations

import copy
import json
import random
from uuid import UUID, uuid4

import pytest

from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_source_fence import (
    MEMORY_REFERENCE_KEYS,
    CitedMemoryIds,
    SavedProvenanceReader,
    SourceReadFence,
    cited_memory_ids,
    cited_memory_refs,
)
from tests.unit.test_saved_provenance_reader import _identity, _revision
from tests.unit.test_saved_quote_copies_reader import _MemoryStore, _row

_WORDS = "Atlas played ZQXSENTINEL for 115 hours"


class _Store(_MemoryStore):
    """The stub store with the bulk memory read both real stores have, and the size of each lookup recorded."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, object]] = []

    def add_memory(
        self, *, sensitivity: str = "internal", domain: str = "project", project: str | None = "alpha", deleted: bool = False
    ) -> str:
        memory_id = str(uuid4())
        self.memories[memory_id] = {
            "id": memory_id,
            "memory_key": f"key.{memory_id}",
            "domain": domain,
            "sensitivity": sensitivity,
            "metadata_json": {"project_scope": [project]} if project else {},
            "deleted_at": "2026-10-01T00:00:00Z" if deleted else None,
        }
        return memory_id


def _unbound(profile: str) -> AgentIdentity:
    return _identity(profile, None)


def _reader(store: _Store, identity: AgentIdentity | None = None) -> SavedProvenanceReader:
    return SavedProvenanceReader(store, fence=SourceReadFence.for_identity(identity or _identity()))


def _commit(memory_id: str, refs: list[object], **kwargs) -> dict[str, object]:
    return _row(memory_id, refs=refs, **kwargs)


# -- 1. the spellings of a memory ref ----------------------------------------------------------------------------------


def _spellings(m: str, other: str, source: str) -> dict[str, tuple[object, set[str]]]:
    """Every shape that cites the memory ``m``, with the memory ids the reader must find. ``other`` is a second memory and
    ``source`` an id that is no memory."""

    return {
        "memory: prefix": (f"memory:{m}", {m}),
        "MEMORY: prefix": (f"MEMORY:{m}", {m}),
        "Memory: prefix": (f"Memory:{m}", {m}),
        "memory:memory:": (f"memory:memory:{m}", {m}),
        "padded": (f"  memory:{m}  ", {m}),
        "space before the prefix": (f" memory: {m}", {m}),
        "braces": ("memory:{" + m + "}", {m}),
        "urn": (f"memory:urn:uuid:{m}", {m}),
        "no hyphens": (f"memory:{m.replace('-', '')}", {m}),
        "upper case id": (f"memory:{m.upper()}", {m}),
        "alice url": (f"alice://memories/{m}", {m}),
        "ALICE URL": (f"ALICE://MEMORIES/{m}", {m}),
        "chunk suffix": (f"memory:{m}#chunk-1", {m}),
        "in a sentence": (f"as noted in memory:{m} earlier", {m}),
        "in parentheses": (f"(memory:{m})", {m}),
        "a list of words": (f"memory:{m}, memory:{other}", {m, other}),
        "a list with a bare id": (f"memory:{m} {source}", {m}),
        "JSON text": (json.dumps({"memory_id": m}), {m}),
        "JSON list": (json.dumps([f"memory:{m}"]), {m}),
        "memory_id key": ({"memory_id": m}, {m}),
        "memory_id upper case": ({"memory_id": m.upper()}, {m}),
        "memory_id no hyphens": ({"memory_id": m.replace("-", "")}, {m}),
        "memory_id braces": ({"memory_id": "{" + m + "}"}, {m}),
        "memory_id urn": ({"memory_id": "urn:uuid:" + m}, {m}),
        "memory_id with a prefix": ({"memory_id": f"memory:{m}"}, {m}),
        "memory_id padded": ({"memory_id": f"  {m}  "}, {m}),
        "memory_id integer": ({"memory_id": int("1" * 32)}, {str(UUID("1" * 32))}),
        "memory_ids": ({"memory_ids": [m, other]}, {m, other}),
        "memory_refs": ({"memory_refs": [f"memory:{m}"]}, {m}),
        "memory_references": ({"memory_references": [m]}, {m}),
        "source_memory_ids": ({"source_memory_ids": [m]}, {m}),
        "selected_memory_ids": ({"selected_memory_ids": [m]}, {m}),
        "memories": ({"memories": [m]}, {m}),
        "dict values under a memory key": ({"memory_ids": {"first": m, "second": other}}, {m, other}),
        "a sentence under a memory key": ({"memory_id": f"see {m} and {other}"}, {m, other}),
        "ref key": ({"ref": f"memory:{m}"}, {m}),
        "id key": ({"id": f"memory:{m}"}, {m}),
        "source_id key": ({"source_id": f"memory:{m}"}, {m}),
        "any other key": ({"origin": f"memory:{m}"}, {m}),
        "list in list": ([[f"memory:{m}"]], {m}),
        "five deep": ({"memory_ids": [[[[{"memory_id": m}]]]]}, {m}),
        "nested under an unlisted key": ({"evidence": {"memory_id": m}}, {m}),
        "an entry beside a quote": ([f"memory:{m}", {"quote": f"memory:{other}"}], {m}),
        # What names no memory.
        "a bare id": (m, set()),
        "a bare id under another key": ({"chunk_id": m}, set()),
        "a source ref": (f"source:{source}", set()),
        "a url": ("https://example.test/doc", set()),
        "a label": ("meeting notes", set()),
        "an id inside a quote": ({"memory_id": m, "quote": f"seen in memory:{other}"}, {m}),
        "an id inside an excerpt key": ({"memory_id": m, "conversation_excerpt": f"seen in memory:{other}"}, {m}),
        "a 40 digit sha is no id": (f"memory:{'a' * 40}", set()),
        "a short string": ("memory:", set()),
        "nothing": (None, set()),
    }


def test_cited_memory_ids_reads_every_spelling_a_memory_ref_is_stored_in() -> None:
    """The reader reads the ``memory:`` prefix in any case and any number, an ``alice://memories/`` URL, a word of a list, an
    id in a sentence, JSON text, and the id under a memory key in every spelling ``uuid.UUID`` takes (upper case, braces,
    ``urn:uuid:``, no hyphens, an integer of 32 digits, padding). It reads nothing from a bare id outside a memory key, from
    a ``source:`` ref, or from the text of a ``quote`` or ``conversation_excerpt``.

    Mutations, each alone, in ``vnext_source_fence.py``: drop ``memory_ids`` from ``MEMORY_REFERENCE_KEYS`` (the list rows
    fail); make ``_MEMORY_PREFIX_PATTERN`` case sensitive (the capital rows fail); return from ``cited_memory_ids`` at the
    ``_TEXT_KEYS`` test without skipping (the quote rows name ``other``); drop the ``alice://memories/`` test in
    ``_marked_memory_start`` (the URL rows fail); make ``_json_container`` return None (the JSON rows fail); read a bare id
    outside a memory key (``at_key = True`` at the root: the bare rows fail).
    """

    m, other, source = (str(uuid4()) for _ in range(3))
    for label, (ref, expected) in _spellings(m, other, source).items():
        assert set(cited_memory_ids(ref)) == expected, label
    assert cited_memory_ids([{"memory_ids": [m]}, "meeting notes", f"memory:{other}"]) == frozenset({m, other})


def test_cited_memory_ids_agrees_with_the_decoded_value_of_a_ref_that_is_json_text() -> None:
    """A ref that is JSON text is read as the value it decodes to, so the memory ids of a random ref and of its JSON text are
    the same (the independent judge here is the decoder of the standard library, and a ref is written by a seeded random
    generator over the spellings above).

    Mutation: scan the raw text of a JSON string before decoding it (drop the ``_json_container`` branch of
    ``cited_memory_ids``): the ids inside a quote of the text are read, and the sets differ.
    """

    rng = random.Random(7)
    pool = [str(uuid4()) for _ in range(6)]

    def build(depth: int) -> object:
        kind = rng.choice(("string", "memory", "object", "list", "quote"))
        if depth > 3 or kind == "string":
            return rng.choice(("notes", "https://example.test", f"source:{rng.choice(pool)}", rng.choice(pool)))
        if kind == "memory":
            return rng.choice((f"memory:{rng.choice(pool)}", f"MEMORY:{rng.choice(pool).upper()}", f"alice://memories/{rng.choice(pool)}"))
        if kind == "quote":
            return {"quote": f"memory:{rng.choice(pool)}", "note": build(depth + 1)}
        if kind == "list":
            return [build(depth + 1) for _ in range(rng.randint(0, 3))]
        keys = ("memory_id", "memory_ids", "origin", "memory_refs", "ref", "chunk_id")
        return {rng.choice(keys): build(depth + 1) for _ in range(rng.randint(1, 3))}

    nonempty = 0
    for _ in range(400):
        ref = build(0)
        assert cited_memory_ids(ref) == cited_memory_ids(json.dumps(ref)), ref
        nonempty += bool(cited_memory_ids(ref))
    assert nonempty > 100, "the generator reaches memory refs"


def test_the_reference_keys_are_the_ones_the_open_loop_reader_uses_and_more() -> None:
    """The open-loop reader withholds an id under ``memory_id``, ``memory_ids``, ``memory_ref``, ``memory_refs`` and
    ``source_memory_ids``; the saved-quote reader reads those and the spellings a client would reach for next.

    Mutation: delete one of the five keys of the open-loop reader from ``MEMORY_REFERENCE_KEYS`` in ``vnext_source_fence.py``.
    """

    from alicebot_api.vnext_open_loop_references import MEMORY_REFERENCE_KEYS as LOOP_KEYS

    assert LOOP_KEYS <= MEMORY_REFERENCE_KEYS


def test_a_ref_nested_deeper_than_the_recursion_limit_is_read_to_the_end() -> None:
    """The walk keeps its own stack, so a memory id at the bottom of a ref nested thousands of levels deep is found.

    Mutation: make ``cited_memory_ids`` recursive: the deep ref raises ``RecursionError``.
    """

    m = str(uuid4())
    deep: object = {"memory_id": m}
    for _ in range(5000):
        deep = [deep]
    assert cited_memory_ids(deep) == frozenset({m})


# -- 2. a memory the caller may not read -------------------------------------------------------------------------------

_REFUSED = {
    "above the ceiling": {"sensitivity": "confidential"},
    "restricted domain": {"domain": "health"},
    "other project": {"project": "beta"},
    "global memory": {"project": None},
    "archived": {"deleted": True},
}


@pytest.mark.parametrize("kind", [*_REFUSED, "missing"])
def test_a_ref_that_names_a_refused_memory_keeps_its_id_and_loses_its_quote(kind: str) -> None:
    """For each way a memory stops being readable (a raised ceiling, a restricted domain, another project, a global memory
    under a key bound to a project, archived, deleted from the table), the quote of the ref that names it and the three
    saved copies of a quote are withheld, the ref keeps its id and every field but the quote, and the quote beside it that
    names nothing goes too. A ref to a memory the caller may read keeps its quote. Nothing else on the row changes.

    Mutations, each alone, in ``vnext_source_fence.py``: ``return True`` from ``SourceReadFence._admits`` (every kind but
    ``archived`` and ``missing``, which the lookup decides); admit a memory that has no row (``missing``); drop the
    ``withhold_quotes`` copies (``_without_quote_copies``) from ``_memory_without_refused_provenance``; drop the sibling
    clause of ``_without_refused_refs`` (the quote entry beside ``memory:<id>`` stays).
    """

    store = _Store()
    readable = store.add_memory()
    if kind in _REFUSED:
        refused = store.add_memory(**_REFUSED[kind])  # type: ignore[arg-type]
    else:
        refused = str(uuid4())
    refs: list[object] = [
        {"memory_id": refused, "quote": _WORDS, "page": 3},
        f"memory:{refused}",
        {"quote": f"{_WORDS} again"},
        {"memory_id": readable, "quote": "readable words"},
        "https://example.test/doc",
    ]
    row = _commit(str(uuid4()), refs, copy_kind="provenance", text=_WORDS, provenance_source=None)
    before = copy.deepcopy(row)
    shown = _reader(store, _identity("project_scoped_agent") if kind == "restricted domain" else None).memory(row)
    assert row == before, "the stored row is not changed by the read"
    expected = [
        {"memory_id": refused, "quote": None, "page": 3},
        f"memory:{refused}",
        {"quote": None},
        {"memory_id": readable, "quote": "readable words"},
        "https://example.test/doc",
    ]
    assert shown["metadata_json"]["agentic_memory"]["source_refs"] == expected  # type: ignore[index]
    assert shown["value"]["source_refs"] == expected  # type: ignore[index]
    assert "provenance" not in shown["metadata_json"] and "conversation_excerpt" not in shown["metadata_json"]["agentic_memory"]  # type: ignore[operator,index]
    assert "ZQXSENTINEL" not in json.dumps(shown)
    assert shown["canonical_text"] == row["canonical_text"] and shown["id"] == row["id"]


def test_a_ref_that_names_only_readable_memories_is_returned_as_the_same_object() -> None:
    """Nothing is copied when nothing is withheld, so an authorized caller is shown the stored row, byte for byte, in every
    spelling.

    Mutation: return the rebuilt copy even when ``withhold_quotes`` is false (``_memory`` in the reader).
    """

    store = _Store()
    m = store.add_memory()
    other = store.add_memory()
    for label, (ref, expected) in _spellings(m, other, str(uuid4())).items():
        if not expected or not expected <= {m, other}:
            continue  # a ref that names no memory is the source reader's business (a bare id is a named source)
        row = _commit(str(uuid4()), [ref], text=_WORDS)
        before = copy.deepcopy(row)
        reader = _reader(store)
        assert reader.memory(row) is row, label
        assert row == before, label
        revision = _revision(str(row["id"]), [ref])
        assert reader.revision(revision) is revision, label


def test_every_spelling_of_a_ref_to_a_refused_memory_loses_the_words() -> None:
    """The commit's quote is withheld whichever way the ref names the refused memory: the quote beside the ref goes in each
    spelling of the table above, and so does the quote a ref holds in its own fields.

    Mutation: delete a spelling from the reader (``alice://memories/`` from ``_marked_memory_start``, or a key from
    ``MEMORY_REFERENCE_KEYS``): its row names no memory the reader finds, and the words stay.
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    other = store.add_memory()
    checked = 0
    for label, (ref, expected) in _spellings(refused, other, str(uuid4())).items():
        if refused not in expected or isinstance(ref, int):
            continue
        row = _commit(str(uuid4()), [ref, {"quote": _WORDS}], text=_WORDS)
        shown = _reader(store).memory(row)
        assert "ZQXSENTINEL" not in json.dumps(shown), label
        checked += 1
    assert checked > 30, "the table is walked"


# -- 3. the owner, an unbound admin key, and the cost -------------------------------------------------------------------


def test_the_owner_and_an_unbound_admin_key_are_shown_every_quote_and_cost_no_read() -> None:
    """The owner is returned the stored row and reads nothing. An unbound ``admin_agent`` key has no limits either: it keeps
    the quote of a memory that is redacted or archived (the reader asks the fence whether the key has limits and reads no
    memory when it has none). A key bound to a project is judged.

    Mutations: make ``SourceReadFence.fenced`` return ``True`` (the owner reads); drop the ``entity_read_fenced`` test of
    ``_judge_memories`` (the admin key loses the quote of an archived memory and the lookups are counted).
    """

    store = _Store()
    archived = store.add_memory(deleted=True)
    missing = str(uuid4())
    row = _commit(str(uuid4()), [{"memory_id": archived, "quote": _WORDS}, f"memory:{missing}", {"quote": _WORDS}])
    owner = SavedProvenanceReader(store, fence=SourceReadFence.unfenced())
    assert owner.memory(row) is row
    admin = _reader(store, _unbound("admin_agent"))
    assert admin.memory(row) is row
    assert store.memory_batches == [] and store.memory_reads == 0
    bound = _reader(store, _identity("admin_agent", "alpha"))
    assert bound.memory(row) is not row and "ZQXSENTINEL" not in json.dumps(bound.memory(row))


def test_a_row_with_no_quote_to_withhold_reads_no_memory_and_a_row_with_one_reads_them_in_slices() -> None:
    """A roll-up card lists three hundred ``memory:`` members and holds no quote, so its memories are not looked up. A row
    with a quote in a ref reads the memories it names in slices of 500 (a SQLite build from before 3.32 allows 999 bound
    variables), once for the row and not once per spelling.

    Mutations: drop the test for a quote at the top of ``_memory_ids_named_by_memory_copies`` (the roll-up reads 300 memories);
    look the ids up in one call (``_judge_memories`` without the slices: the largest batch is 1,200).
    """

    store = _Store()
    rollup = _row(str(uuid4()), refs=[f"memory:{uuid4()}" for _ in range(300)], copy_kind="none")
    assert _reader(store).memory(rollup) is rollup
    assert store.memory_batches == [] and store.memory_reads == 0
    ids = [store.add_memory() for _ in range(1200)]
    row = _commit(str(uuid4()), [{"memory_id": i, "quote": "words"} for i in ids])
    reader = _reader(store)
    assert reader.memory(row) is row
    assert store.memory_batches == [500, 500, 200], store.memory_batches
    reader.memory(row)
    assert store.memory_batches == [500, 500, 200], "each memory is judged once per read"


def test_the_memories_of_many_rows_are_looked_up_together() -> None:
    """A pack reads many rows at once. Their memories are judged in one lookup, and a memory that two rows cite once.

    Mutation: drop ``wanted_memories`` from ``_prefetch`` (each row looks its memories up on its own: the batches are 1 each).
    """

    store = _Store()
    shared = store.add_memory()
    own = [store.add_memory() for _ in range(5)]
    rows = [_commit(str(uuid4()), [{"memory_id": shared, "quote": "a"}, {"memory_id": i, "quote": "b"}]) for i in own]
    reader = _reader(store)
    assert all(shown is row for shown, row in zip(reader.memories(rows), rows, strict=True))
    assert store.memory_batches == [6], store.memory_batches


# -- 4. the quotes beside a refused source -----------------------------------------------------------------------------


def test_a_quote_that_names_nothing_beside_a_refused_source_is_withheld_and_a_readable_ref_keeps_its_own() -> None:
    """The commit that cites ``source:<id>`` and then ``{"quote": ...}`` keeps the words in the second entry, and nothing in
    it says whose they are. When the source is refused the entry loses its quote, as it does beside a refused memory. A ref
    that names a readable source keeps its quote, and so does a quote in a list that holds nothing refused.

    Mutations: apply the sibling clause only to memories (``refused_here`` from ``refused_memories`` alone: the source row
    keeps the quote); apply it to every entry of the list (the readable source loses its quote).
    """

    store = _Store()
    readable, refused = store.add_source(), store.add_source(sensitivity="confidential")
    row = _commit(
        str(uuid4()),
        [f"source:{refused}", {"quote": _WORDS}, {"source_id": readable, "quote": "readable words"}],
    )
    shown = _reader(store).memory(row)
    assert shown["metadata_json"]["agentic_memory"]["source_refs"] == [  # type: ignore[index]
        {"quote": None},
        {"source_id": readable, "quote": "readable words"},
    ]
    clean = _commit(str(uuid4()), [f"source:{readable}", {"quote": "readable words"}], copy_kind="none")
    assert _reader(store).memory(clean) is clean


def test_the_words_of_a_withheld_ref_are_withheld_from_a_link_that_says_the_same() -> None:
    """The quote of a ref that is withheld is withheld from the memory's links as well (ignoring the whitespace between
    words), and a link that says something else keeps its quote.

    Mutation: drop ``_without_refused_refs(..., texts)`` from the ``withhold_quotes`` branch of ``_verdict``.
    """

    store = _Store()
    source = store.add_source()
    refused = store.add_memory(sensitivity="confidential")
    memory_id = str(uuid4())
    row = _commit(memory_id, [{"memory_id": refused, "quote": _WORDS}], copy_kind="none")
    same = store.add_link(memory_id, source, quote=_WORDS.replace(" ", "  "))
    other = store.add_link(memory_id, source, quote="a different quote")
    reader = _reader(store)
    reader.memory(row)
    shown = reader.links(memory_id)
    assert [link["quote"] for link in shown] == [None, "a different quote"]
    assert shown[1] is other and same["quote"] == _WORDS.replace(" ", "  "), "the stored links are not changed"


# -- 5. revisions, the audit and JSON text -----------------------------------------------------------------------------


def test_a_revision_that_cites_a_refused_memory_loses_the_quote_and_keeps_the_id() -> None:
    """A revision keeps the refs the memory was written with in ``new_value`` and ``previous_value``, and a memory proposal
    keeps them in the revision's ``metadata_json``. Each is held to the same rule.

    Mutations: drop the memory ids of ``_revision`` (the quote stays in the revision); read only ``new_value`` (the
    ``previous_value`` row fails).
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    ref = {"memory_id": refused, "quote": _WORDS}
    revision = {
        **_revision("m1", [ref]),
        "previous_value": {"text": "x", "source_refs": [ref]},
        "metadata_json": {"source_refs": [f"memory:{refused}", {"quote": _WORDS}]},
    }
    shown = _reader(store).revision(revision)
    assert shown["new_value"]["source_refs"] == [{"memory_id": refused, "quote": None}]  # type: ignore[index]
    assert shown["previous_value"]["source_refs"] == [{"memory_id": refused, "quote": None}]  # type: ignore[index]
    assert shown["metadata_json"]["source_refs"] == [f"memory:{refused}", {"quote": None}]  # type: ignore[index]
    assert "ZQXSENTINEL" not in json.dumps(shown)
    readable = store.add_memory()
    fine = _revision("m1", [{"memory_id": readable, "quote": _WORDS}])
    assert _reader(store).revision(fine) is fine


def test_a_ref_that_is_json_text_keeps_its_id_and_loses_its_quote() -> None:
    """A JSON string in a ref position is decoded, scrubbed and encoded again: the quote becomes ``null`` and every other
    field stays.

    Mutation: leave a string alone in ``_withhold_entry_text`` (``nested = None`` in its ``str`` branch): the JSON row keeps it.
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    text = json.dumps({"memory_id": refused, "quote": _WORDS, "page": 2})
    row = _commit(str(uuid4()), [text], copy_kind="none")
    shown = _reader(store).memory(row)
    assert json.loads(shown["metadata_json"]["agentic_memory"]["source_refs"][0]) == {  # type: ignore[index]
        "memory_id": refused,
        "quote": None,
        "page": 2,
    }


def test_a_json_text_beside_a_refused_ref_that_names_nothing_loses_its_quote() -> None:
    """The entry beside a refused ref that names no id loses its quote, and when that entry is JSON text it is decoded, scrubbed
    and encoded again. A JSON text that names a readable memory beside it is not touched.

    Mutation: leave a string alone in ``_withhold_quote_text`` (``nested = None`` in its ``str`` branch): the sibling keeps it.
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    row = _commit(str(uuid4()), [f"memory:{refused}", json.dumps({"quote": _WORDS, "page": 2})], copy_kind="none")
    shown = _reader(store).memory(row)
    refs = shown["metadata_json"]["agentic_memory"]["source_refs"]  # type: ignore[index]
    assert refs[0] == f"memory:{refused}" and json.loads(refs[1]) == {"quote": None, "page": 2}
    assert "ZQXSENTINEL" not in json.dumps(shown)


def test_a_quote_nested_past_the_bound_is_withheld_with_its_subtree() -> None:
    """The rebuild is recursive, so a ref nested past ``_SCRUB_DEPTH`` is withheld with the subtree that holds the quote and
    does not raise.

    Mutation: raise the bound to 100,000 (``_SCRUB_DEPTH``): the nested row raises ``RecursionError``.
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    deep: object = {"quote": _WORDS}
    for _ in range(3000):
        deep = {"nested": deep}
    row = _commit(str(uuid4()), [{"memory_id": refused, "evidence": deep}], copy_kind="none")
    shown = _reader(store).memory(row)
    assert "ZQXSENTINEL" not in json.dumps(shown, default=str)[:200_000]


def test_the_audit_envelope_is_held_in_every_part() -> None:
    """The audit of a memory holds the row, its revisions, its links and the payload of its events. A caller with limits is
    shown none of the words in any of them; the owner is given the envelope itself. A link whose source is refused is left
    out, and an event whose payload copies the refs loses the quote the same way.

    Mutations: skip the events in ``audit`` (the event payload keeps the words); skip the revisions; return the envelope
    unchanged for a fenced caller.
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    hidden_source = store.add_source(sensitivity="confidential")
    memory_id = str(uuid4())
    ref = {"memory_id": refused, "quote": _WORDS}
    row = _commit(memory_id, [ref], text=_WORDS)
    store.memories[memory_id] = row
    link = store.add_link(memory_id, hidden_source, quote="other words")
    revision = _revision(memory_id, [ref])
    event = {
        "id": str(uuid4()),
        "event_type": "memory.updated",
        "payload_json": {
            "changes": {"metadata_json": {"agentic_memory": {"source_refs": [ref], "conversation_excerpt": _WORDS}}},
            "metadata_json": {"source_refs": [f"memory:{refused}", {"quote": _WORDS}]},
        },
    }
    envelope = {"memory": row, "revisions": [revision], "events": [event], "provenance_links": [link], "supersession_chain": []}
    before = copy.deepcopy(envelope)
    shown = _reader(store).audit(envelope)
    assert envelope == before, "the stored envelope is not changed by the read"
    assert "ZQXSENTINEL" not in json.dumps(shown, default=str)
    assert shown["provenance_links"] == [] and shown["supersession_chain"] == []
    assert refused in json.dumps(shown), "the ids stay"
    owner = SavedProvenanceReader(store, fence=SourceReadFence.unfenced()).audit(envelope)
    assert owner is envelope
    admin = _reader(store, _unbound("admin_agent")).audit(envelope)
    assert "ZQXSENTINEL" in json.dumps(admin, default=str)


# -- 6. an id that no marker says is a memory --------------------------------------------------------------------------


def _unmarked(m: str) -> dict[str, tuple[object, bool]]:
    """Every wording of a ref to the memory ``m`` that the reader has no marker for, and whether the ref holds a quote of its own
    (the others rely on the quote the commit route saves as ``conversation_excerpt``)."""

    return {
        "Memory <id>": (f"Memory {m}", False),
        "memory id <id>": (f"memory id {m}", False),
        "mem:<id>": (f"mem:{m}", False),
        "see memory: <id>": (f"see memory: {m}", False),
        "markdown link": (f"[memory]({m})", False),
        "https url": (f"https://host.example.test/memories/{m}", False),
        "alice://memory/<id> (singular)": (f"alice://memory/{m}", False),
        "id and a page": (f"{m}#page=3", False),
        "key memory": ({"memory": m, "quote": _WORDS}, True),
        "key memoryId": ({"memoryId": m, "quote": _WORDS}, True),
        "key origin": ({"origin": m, "quote": _WORDS}, True),
        "key ref_id": ({"ref_id": m, "quote": _WORDS}, True),
        "key parent_memory_id": ({"parent_memory_id": m, "quote": _WORDS}, True),
        "key supersedes": ({"supersedes": m, "quote": _WORDS}, True),
        "key type and id": ({"type": "memory", "id": m, "quote": _WORDS}, True),
        "nested": ({"evidence": [{"origin": m, "quote": _WORDS}]}, True),
        "json text": (json.dumps({"origin": m, "quote": _WORDS}), True),
        "id as a key": ({m: "see this", "quote": _WORDS}, True),
        "upper case": ({"origin": m.upper(), "quote": _WORDS}, True),
    }


def test_cited_memory_refs_reads_an_id_in_any_wording_and_none_from_the_text_of_a_quote() -> None:
    """``cited_memory_refs`` returns the ids a ref says are memories as ``named`` and every other id outside the text of a
    ``quote`` or ``conversation_excerpt`` as ``incidental``: in a sentence, in a URL, beside punctuation, under any key, as a
    key, in JSON text and in any spelling ``uuid.UUID`` takes. An id inside a quote names nothing, and ``cited_memory_ids``
    still returns the named ones alone.

    Mutations, each alone: skip the key ids in ``cited_memory_refs`` (the ``id as a key`` row); drop the ``_every_id_in_text``
    read of a string (every incidental row); read the text of a ``quote`` (the last assertion).
    """

    m, other = str(uuid4()), str(uuid4())
    for label, (ref, _quote) in _unmarked(m).items():
        cited = cited_memory_refs(ref)
        assert cited.incidental == {m}, label
        assert cited.named == frozenset(), label
        assert cited_memory_ids(ref) == frozenset(), label
    marked = cited_memory_refs([f"memory:{m}", {"origin": other, "quote": f"seen in {m} and memory:{other}"}])
    assert marked == CitedMemoryIds(named=frozenset({m}), incidental=frozenset({other}))
    assert marked.every == {m, other}
    assert cited_memory_refs({"memory_id": m, "origin": m}).incidental == frozenset(), "an id named once is not incidental too"
    assert cited_memory_refs({"origin": "meeting notes", "quote": f"memory:{m}", "conversation_excerpt": m}) == CitedMemoryIds()
    assert cited_memory_refs(None) == CitedMemoryIds()


@pytest.mark.parametrize("kind", [*_REFUSED, "redacted"])
def test_an_id_with_no_marker_that_names_a_refused_memory_takes_the_quote(kind: str) -> None:
    """A commit that names the memory some way no marker covers keeps its quote only when the caller may read the memory. For
    each way a memory stops being readable (a raised ceiling, a restricted domain, another project, a global memory under a key
    bound to a project, archived, redacted) and for each wording in the table above, the ref that holds the id and the copy the
    commit route saved of its excerpt lose the words, and the quote the ref holds loses them too. The id stays.

    Mutations, each alone, in ``vnext_source_fence.py``: read only the named ids of a row in ``_refused_memories`` (every row
    fails); keep the ids behind a ``memory:`` prefix out of ``cited_memory_refs`` (nothing changes: they are named); look the
    ids up without ``include_deleted`` (the ``archived`` and ``redacted`` rows fail, since a deleted row is then taken for an
    id that names no memory).
    """

    store = _Store()
    refused = store.add_memory(**(_REFUSED[kind] if kind in _REFUSED else {"deleted": True}))  # type: ignore[arg-type]
    identity = _identity("project_scoped_agent") if kind == "restricted domain" else None
    for label, (ref, has_quote) in _unmarked(refused).items():
        row = _commit(str(uuid4()), [ref], text=_WORDS)
        before = copy.deepcopy(row)
        shown = _reader(store, identity).memory(row)
        assert row == before, label
        assert "ZQXSENTINEL" not in json.dumps(shown), (kind, label)
        assert "conversation_excerpt" not in shown["metadata_json"]["agentic_memory"], (kind, label)  # type: ignore[index,operator]
        if has_quote and label not in ("id as a key", "key type and id"):
            # The ref keeps its id. (A key that is the id carries it in the key and is nulled with its text; an ``id`` key names a
            # source, and an id that is no stored source is refused as a missing source is, so that entry goes whole.)
            assert refused in json.dumps(shown).lower(), (kind, label)


def test_an_id_that_names_no_memory_row_leaves_the_quote_and_a_marked_one_is_refused() -> None:
    """An id that no marker says is a memory may be a source, a chunk or a session, and nothing refuses a caller the quote beside
    it: the id names no memory row, so the quote stays. The same id behind ``memory:`` or under ``memory_id`` names a memory
    that is missing, and is refused. An unmarked id of a memory the caller may read leaves the quote as well.

    Mutations: treat an incidental id that names no row as refused (``cited.incidental`` without the ``_memory_exists`` test:
    the first two rows lose the quote); read a marked id of a missing memory as readable (the last row keeps it).
    """

    store = _Store()
    source = store.add_source()
    chunk, session = str(uuid4()), str(uuid4())
    readable = store.add_memory()
    missing = str(uuid4())
    for label, refs in {
        "a source id": [{"source_id": source, "origin": source, "quote": _WORDS}],
        "a chunk id and a session id": [{"chunk_id": chunk, "session": session, "quote": _WORDS}],
        "an unmarked id of an unknown memory": [f"Memory {missing}"],
        "a memory the caller may read": [{"origin": readable, "quote": _WORDS}],
    }.items():
        row = _commit(str(uuid4()), refs, text=_WORDS)
        assert _reader(store).memory(row) is row, label
    row = _commit(str(uuid4()), [f"memory:{missing}"], text=_WORDS)
    assert "ZQXSENTINEL" not in json.dumps(_reader(store).memory(row))


def test_the_unmarked_ids_of_many_rows_are_looked_up_together_with_the_deleted_rows_included() -> None:
    """The ids a row holds are all looked up as possible memories (the source ids of its refs too), once for all the rows of a
    read and in one call that asks for the soft-deleted rows, because an archived or redacted memory must be told from an id
    that names no memory.

    Mutations: drop ``include_deleted`` in ``memory_rows_including_deleted`` (the deleted memory is taken for no memory and the
    words stay); look each row's ids up on its own (``wanted_memories`` dropped from ``_prefetch``: the batches are 1).
    """

    class Counting(_Store):
        def __init__(self) -> None:
            super().__init__()
            self.asked_for_deleted: list[bool] = []

        def get_memories_by_ids(self, ids, *, include_deleted=False):  # type: ignore[no-untyped-def]
            self.asked_for_deleted.append(include_deleted)
            return super().get_memories_by_ids(ids, include_deleted=include_deleted)

    store = Counting()
    source = store.add_source()
    archived = store.add_memory(deleted=True)
    rows = [
        _commit(str(uuid4()), [{"source_id": source, "origin": archived, "quote": _WORDS}], text=_WORDS) for _ in range(5)
    ]
    reader = _reader(store)
    shown = reader.memories(rows)
    assert store.memory_batches == [2], store.memory_batches
    assert store.asked_for_deleted == [True]
    assert all("ZQXSENTINEL" not in json.dumps(item) for item in shown)


# -- 7. what an entry that names a refused memory keeps ----------------------------------------------------------------


def test_an_entry_that_names_a_refused_memory_keeps_its_ids_and_loses_every_other_string() -> None:
    """The quote is not the only place an agent puts the words it took from a memory. Every string of an entry that names a
    refused memory goes (a ``text``, an ``excerpt``, a ``snippet``, a ``note``, a title, a sentence that has the id in it),
    at any depth and inside JSON text, and what stays is each id (a string that is an id or an id behind a marker, and
    anything under a reference key), the numbers and the booleans. An entry that names a readable memory is not touched.

    Mutations: drop ``_withhold_entry_text`` for ``_withhold_quote_text`` in ``_without_refused_refs`` (the ``text`` row keeps
    its words); treat a string that is not an id as an id (``_is_reference_text`` returns ``True``: the sentence row keeps
    them); null the strings under a reference key (``id_key`` ignored: ``memory_id`` reads ``null``).
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    readable = store.add_memory()
    words = f"{_WORDS} and more"
    refs: list[object] = [
        {"memory_id": refused, "text": words, "excerpt": words, "snippet": words, "note": words, "page": 3, "primary": True},
        {"memory_id": refused, "details": {"title": words, "lines": [words, 4], "origin": refused}},
        {"origin": refused, "label": "Atlas notes", "quote": words},
        f"memory:{refused}",
        f"Atlas said: {words} (memory {refused})",
        json.dumps({"memory_id": refused, "text": words, "page": 2}),
        {"memory_ids": [refused, f"memory:{refused}"], "text": words},
        {"memory_id": readable, "text": "words of a readable memory"},
        "https://example.test/doc",
    ]
    row = _commit(str(uuid4()), refs, text=words)
    before = copy.deepcopy(row)
    shown = _reader(store).memory(row)
    assert row == before
    kept = shown["metadata_json"]["agentic_memory"]["source_refs"]  # type: ignore[index]
    assert kept[0] == {"memory_id": refused, "text": None, "excerpt": None, "snippet": None, "note": None, "page": 3, "primary": True}
    assert kept[1] == {"memory_id": refused, "details": {"title": None, "lines": [None, 4], "origin": refused}}
    assert kept[2] == {"origin": refused, "label": None, "quote": None}
    assert kept[3] == f"memory:{refused}"
    assert kept[4] is None
    assert json.loads(kept[5]) == {"memory_id": refused, "text": None, "page": 2}
    assert kept[6] == {"memory_ids": [refused, f"memory:{refused}"], "text": None}
    assert kept[7] == refs[7] and kept[8] == refs[8]
    assert shown["value"]["source_refs"] == kept  # type: ignore[index]
    assert "ZQXSENTINEL" not in json.dumps(shown)


def test_a_row_whose_refs_hold_text_and_no_quote_still_has_its_memories_looked_up() -> None:
    """A row is checked for memories to refuse only when it holds words to withhold. Words are a quote or an excerpt and also
    any string of a ref that is not an id, so a ref of the shape ``{"memory_id": ..., "text": ...}`` with no quote is judged,
    and a list of bare references (the roll-up card) costs no read.

    Mutations: gate the lookup on a quote alone (``_holds_words`` made to look at the quote keys only: the first assertion
    fails); gate it on nothing (the roll-up row reads 300 memories).
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    row = _commit(str(uuid4()), [{"memory_id": refused, "text": _WORDS}], copy_kind="none")
    shown = _reader(store).memory(row)
    assert "ZQXSENTINEL" not in json.dumps(shown) and store.memory_batches == [1]
    rollup = _row(str(uuid4()), refs=[f"memory:{uuid4()}" for _ in range(300)], copy_kind="none")
    store.memory_batches.clear()
    assert _reader(store).memory(rollup) is rollup and store.memory_batches == []
    bare = _row(str(uuid4()), refs=[{"memory_id": refused, "page": 2}, f"memory:{refused}", {"chunk_id": str(uuid4())}], copy_kind="none")
    assert _reader(store).memory(bare) is bare and store.memory_batches == []


# -- 8. cases the guard must also hold ---------------------------------------------------------------------------------


def test_a_source_refs_value_that_is_not_a_list_and_names_a_refused_memory_loses_its_quote() -> None:
    """``source_refs`` is a list when a commit route writes it, and a client can store an object or a string there through a
    memory proposal. A value that is not a list is held to the same rule: the object keeps its id and loses its quote, a JSON
    string the same, and an object that names a readable memory is not touched.

    Mutation: delete the branch of ``_without_refused_refs`` that reads a value that is not a list (the words stay).
    """

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")
    readable = store.add_memory()
    row = _commit(str(uuid4()), [], copy_kind="none")
    row["metadata_json"]["agentic_memory"]["source_refs"] = {"memory_id": refused, "quote": _WORDS}  # type: ignore[index]
    row["value"]["source_refs"] = json.dumps({"origin": refused, "quote": _WORDS})  # type: ignore[index]
    shown = _reader(store).memory(row)
    assert shown["metadata_json"]["agentic_memory"]["source_refs"] == {"memory_id": refused, "quote": None}  # type: ignore[index]
    assert json.loads(shown["value"]["source_refs"]) == {"origin": refused, "quote": None}  # type: ignore[index]
    fine = _commit(str(uuid4()), [], copy_kind="none")
    fine["metadata_json"]["agentic_memory"]["source_refs"] = {"memory_id": readable, "quote": _WORDS}  # type: ignore[index]
    assert _reader(store).memory(fine) is fine


def test_a_quote_key_is_matched_in_any_case_in_the_entry_that_names_a_memory_and_beside_it() -> None:
    """``Quote`` and ``Conversation_Excerpt`` hold a quote as ``quote`` does. The key is matched in any case for the entry that
    names a refused memory and for the entry beside it that names nothing, and in a ref of a refused source.

    The value of a quote key is withheld whole, whatever its shape (a list, an object): the entry row holds a list and an object
    under the two keys, which the string rule alone would rebuild as lists and objects of ``null``.

    Mutations, each alone: compare the key without ``lower()`` in ``_withhold_quote_text`` (the sibling row keeps its words);
    in ``_withhold_entry_text`` (the entry row is rebuilt as ``[null, null]`` and ``{"text": null}`` and not as ``null``).
    """

    store = _Store()
    refused_memory = store.add_memory(sensitivity="confidential")
    refused_source = store.add_source(sensitivity="confidential")
    row = _commit(
        str(uuid4()),
        [
            {"memory_id": refused_memory, "Quote": [_WORDS, _WORDS], "Conversation_Excerpt": {"text": _WORDS}},
            f"memory:{refused_memory}",
            {"QUOTE": _WORDS},
            f"source:{refused_source}",
            {"Quote": _WORDS},
        ],
        copy_kind="none",
    )
    shown = _reader(store).memory(row)
    assert shown["metadata_json"]["agentic_memory"]["source_refs"] == [  # type: ignore[index]
        {"memory_id": refused_memory, "Quote": None, "Conversation_Excerpt": None},
        f"memory:{refused_memory}",
        {"QUOTE": None},
        {"Quote": None},
    ]
    only = _commit(str(uuid4()), [{"memory_id": refused_memory, "Quote": _WORDS}], copy_kind="none")
    assert "ZQXSENTINEL" not in json.dumps(_reader(store).memory(only))


def test_a_memory_whose_project_is_in_its_column_is_judged_by_the_scope_a_memory_is_read_by() -> None:
    """A memory row keeps its project in the ``project_id`` column and in ``metadata_json``, and the two can disagree. The
    reader judges a memory by the scope ``alice_explain`` reads it by (the column wins), not by the scope a source row is read
    by (the metadata wins). For a key bound to one project, a memory whose column names another project is refused when its
    metadata still names the key's project, and the quote is withheld.

    Mutation: call ``self._fence.admits`` instead of ``admits_memory`` in ``_judge_memories``: the quote stays.
    """

    store = _Store()
    column = store.add_memory(project=None)
    store.memories[column]["project_id"] = "beta"
    store.memories[column]["metadata_json"] = {"project_id": "alpha"}
    inside = store.add_memory(project=None)
    store.memories[inside]["project_id"] = "alpha"
    store.memories[inside]["metadata_json"] = {"project_id": "alpha"}
    row = _commit(str(uuid4()), [{"memory_id": column, "quote": _WORDS}], copy_kind="none")
    assert "ZQXSENTINEL" not in json.dumps(_reader(store).memory(row))
    readable = _commit(str(uuid4()), [{"memory_id": inside, "quote": _WORDS}], copy_kind="none")
    assert _reader(store).memory(readable) is readable


# -- 9. the events of a feed -------------------------------------------------------------------------------------------


def _event(target: str, refs: list[object], *, excerpt: str | None = _WORDS) -> dict[str, object]:
    agentic: dict[str, object] = {"kind": "agentic_memory_commit", "source_refs": refs}
    if excerpt is not None:
        agentic["conversation_excerpt"] = excerpt
    return {
        "id": str(uuid4()),
        "event_type": "memory.updated",
        "target_type": "memory",
        "target_id": target,
        "payload_json": {"operation": "update", "changes": {"status": "active", "metadata_json": {"agentic_memory": agentic}}},
    }


def test_the_events_of_a_feed_lose_the_quote_of_a_memory_or_source_the_caller_may_not_read() -> None:
    """A commit that is confirmed inline appends a ``memory.updated`` event whose payload holds the refs and the excerpt the
    commit was sent with. The feed admits the event by the commit it is about, so the reader holds the payload to the rule the
    audit holds the events of one memory to: the ref keeps its id and loses its quote, the excerpt goes, and the ref of a
    refused source goes whole. An event with nothing to withhold is returned as the same object, the owner and an unbound
    admin key are given the rows, and the ids of all the events are looked up together.

    Mutations: delete ``events`` from the reader (the words stay); read only the first event for ids (the second keeps
    them); return the events of an unbound admin key rebuilt (the identity assertion).
    """

    store = _Store()
    refused_memory = store.add_memory(sensitivity="confidential")
    refused_source = store.add_source(sensitivity="confidential")
    readable_memory = store.add_memory()
    first = _event(str(uuid4()), [{"memory_id": refused_memory, "quote": _WORDS}])
    second = _event(str(uuid4()), [f"source:{refused_source}", {"quote": _WORDS}, f"Memory {refused_memory}"], excerpt=None)
    fine = _event(str(uuid4()), [{"memory_id": readable_memory, "quote": _WORDS}])
    other = {"id": str(uuid4()), "event_type": "memory.created", "target_type": "memory", "payload_json": {"operation": "create"}}
    feed = [first, second, fine, other]
    before = copy.deepcopy(feed)
    store.memory_batches.clear()
    shown = _reader(store).events(feed)
    assert feed == before, "the stored events are not changed by the read"
    assert "ZQXSENTINEL" not in json.dumps([shown[0], shown[1]])
    refs = shown[0]["payload_json"]["changes"]["metadata_json"]["agentic_memory"]  # type: ignore[index]
    assert refs["source_refs"] == [{"memory_id": refused_memory, "quote": None}] and "conversation_excerpt" not in refs
    assert shown[1]["payload_json"]["changes"]["metadata_json"]["agentic_memory"]["source_refs"] == [  # type: ignore[index]
        {"quote": None},
        None,
    ], "the refused source goes whole, the quote beside it is withheld, and a sentence that names a refused memory is withheld"
    assert shown[2] is fine and shown[3] is other
    assert store.memory_batches == [3], "the two memories and the source id, looked up once for the four events"
    owner = SavedProvenanceReader(store, fence=SourceReadFence.unfenced()).events(feed)
    assert all(a is b for a, b in zip(owner, feed, strict=True))
    store.memory_batches.clear()
    admin = _reader(store, _unbound("admin_agent")).events(feed)
    assert "ZQXSENTINEL" in json.dumps(admin) and store.memory_batches == []


# -- 10. cost --------------------------------------------------------------------------------------------------------


def _memory_cost_cases(ids_count: int) -> dict[str, tuple[object, set[str], set[str]]]:
    """``(ref, named, incidental)``: a ref with ``ids_count`` ids, or runs of prefixes and words that scale with it."""

    from tests.unit.test_saved_quote_ref_reading import _new_ids

    ids = _new_ids(ids_count)
    first = ids[0]
    return {
        "ids joined by commas under origin": ({"origin": ",".join(ids)}, set(), set(ids)),
        "a sentence with a marker before each id": (" ".join("see memory: " + i for i in ids), set(), set(ids)),
        "memory: words": (",".join("memory:" + i for i in ids), set(ids), set()),
        "JSON text of ids under memory_ids": (json.dumps({"memory_ids": ids}), set(ids), set()),
        "a run of memory: prefixes": ("memory:" * (50 * ids_count), set(), set()),
        "a run of memory: prefixes and then an id": ("memory:" * (37 * ids_count) + first, {first}, set()),
        "a run of whitespace and then an id": (" " * (250 * ids_count) + first, set(), {first}),
        "ids as keys": ({i: "x" for i in ids}, set(), set(ids)),
    }


@pytest.mark.parametrize("label", list(_memory_cost_cases(1)))
def test_a_ref_is_read_for_memory_ids_in_time_that_grows_with_its_length(label: str) -> None:
    """Each shape of a long ref (8,000 ids, or megabytes of prefixes and whitespace) is read for the ids it may name as memories
    in time that grows with its length, and gives exactly the ids it holds, as the reader of source ids is held to.

    Mutation: add a rescan of the text before each id to ``_memory_ids_in_text`` (``text[:start]`` read again for every match):
    the shapes with thousands of ids fail on growth.
    """

    from tests.unit.test_saved_quote_ref_reading import _LARGE_IDS, _SMALL_IDS, assert_cost_grows_linearly

    ref, _named, _incidental = _memory_cost_cases(_SMALL_IDS)[label]
    large, named, incidental = _memory_cost_cases(_LARGE_IDS)[label]
    cited = cited_memory_refs(large)
    assert (set(cited.named), set(cited.incidental)) == (named, incidental)
    assert_cost_grows_linearly(
        lambda: cited_memory_refs(ref),
        lambda: cited_memory_refs(large),
        size_ratio=_LARGE_IDS // _SMALL_IDS,
        what=f"{label} ({len(str(large)):,} characters)",
    )


def test_an_entry_with_a_huge_text_is_withheld_in_time_that_grows_with_its_size() -> None:
    """The entry that names a refused memory is rebuilt with every string that is not an id set to ``None``. A sentence of
    thousands of words, a run of prefixes and a list of thousands of ids are read once each, so the rebuild grows linearly.

    Mutation: test each word of a string against the words before it in ``_is_reference_text`` (quadratic): the sentence fails
    on growth.
    """

    from tests.unit.test_saved_quote_ref_reading import _LARGE_IDS, _SMALL_IDS, assert_cost_grows_linearly

    store = _Store()
    refused = store.add_memory(sensitivity="confidential")

    def build(count: int) -> tuple[list[object], list[object]]:
        sentence = " ".join(f"word{i}" for i in range(count * 5))
        prefixes = "memory:" * (30 * count) + str(uuid4())
        ids = " ".join(str(uuid4()) for _ in range(count))
        refs: list[object] = [{"memory_id": refused, "text": sentence, "run": prefixes, "ids": ids, "note": sentence}]
        return refs, [_commit(str(uuid4()), refs, copy_kind="none")]

    small_refs, small_rows = build(_SMALL_IDS)
    large_refs, large_rows = build(_LARGE_IDS)
    shown = _reader(store).memory(large_rows[0])
    entry = shown["metadata_json"]["agentic_memory"]["source_refs"][0]  # type: ignore[index]
    assert entry["memory_id"] == refused and entry["text"] is None and entry["note"] is None
    assert entry["run"] == large_refs[0]["run"] and entry["ids"] == large_refs[0]["ids"]  # type: ignore[index]
    assert_cost_grows_linearly(
        lambda: _reader(store).memory(small_rows[0]),
        lambda: _reader(store).memory(large_rows[0]),
        size_ratio=_LARGE_IDS // _SMALL_IDS,
        what="an entry that names a refused memory and holds thousands of words",
    )
