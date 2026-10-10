"""``SavedProvenanceReader`` and the places that must use it.

Unreleased (on main, not in v0.20.0). ``tests/unit/test_saved_quotes_follow_the_source_fence.py`` runs the lifecycle on a
real vault. These tests hold the reader itself, with a stub store whose reads are counted, and pin the places that must
go through it: every context pack call names its fence, every memory verb that hands a row back is held to the
caller's fence, review by id reads provenance only through the reader, and every writer of a saved quote is on a list a
reader has been checked against.

Each test names the mutation that must fail it. The mutations were made by hand in a scratch edit and the file was
restored by copying the saved copy back.
"""

from __future__ import annotations

import ast
import copy
import inspect
from pathlib import Path
from uuid import uuid4

import pytest

from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, _held_to_the_callers_read_fence
from alicebot_api.vnext_retrieval import VNextRetrievalService
from alicebot_api.mcp import evidence_artifacts
from alicebot_api.vnext_source_fence import SavedProvenanceReader, SourceReadFence, cited_source_ids_in_memory_audit

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "apps" / "api" / "src" / "alicebot_api"
_QUOTE = "zinnwald-quote-8841 cone ten firing kiln log marlin-oxide-5520"


# -- a stub store whose reads are counted -----------------------------------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.sources: dict[str, dict[str, object]] = {}
        self.links: list[dict[str, object]] = []
        self.source_reads = 0
        self.link_reads = 0

    def add_source(self, *, sensitivity: str = "internal", domain: str = "project", project: str = "alpha") -> str:
        source_id = str(uuid4())
        self.sources[source_id] = {
            "id": source_id,
            "domain": domain,
            "sensitivity": sensitivity,
            "metadata_json": {"project_scope": [project]} if project else {},
            "deleted_at": None,
        }
        return source_id

    def add_link(self, memory_id: str, source_id: str | None, quote: str | None = _QUOTE) -> dict[str, object]:
        link = {
            "id": str(uuid4()),
            "target_type": "memory",
            "target_id": memory_id,
            "source_id": source_id,
            "source_chunk_id": None,
            "quote": quote,
            "evidence_role": "supports",
            "confidence": 0.8,
        }
        self.links.append(link)
        return link

    def get_sources_by_ids(self, ids: list[str]) -> list[dict[str, object]]:
        self.source_reads += 1
        return [self.sources[i] for i in ids if i in self.sources and self.sources[i]["deleted_at"] is None]

    def list_provenance_links_for_targets(self, *, target_type: str, target_ids: list[str]) -> list[dict[str, object]]:
        self.link_reads += 1
        return [link for link in self.links if link["target_type"] == target_type and link["target_id"] in target_ids]


def _memory(memory_id: str, *, sources: list[str | None], extra_refs: list[object] | None = None) -> dict[str, object]:
    """A memory row that holds every copy a door can save: the metadata ``provenance`` and ``replacement_provenance``,
    the commit's ``agentic_memory`` (excerpt and refs), ``value.source_refs`` and a top level ``source_refs``."""

    refs: list[object] = [source for source in sources if source] + list(extra_refs or [])
    first = next((source for source in sources if source), None)
    return {
        "id": memory_id,
        "memory_key": f"key.{memory_id}",
        "canonical_text": "The cone ten firing schedule is posted.",
        "value": {"text": "The cone ten firing schedule is posted.", "intent": "explicit_remember", "source_refs": list(refs)},
        "metadata_json": {
            "review_required": False,
            "provenance": {"source_id": first, "quote": _QUOTE},
            "replacement_provenance": {"source_id": first, "quote": _QUOTE},
            "source_refs": list(refs),
            "agentic_memory": {
                "kind": "agentic_memory_commit",
                "status": "committed",
                "rationale": "kept as is",
                "conversation_excerpt": _QUOTE,
                "source_refs": list(refs),
            },
        },
    }


def _revision(memory_id: str, refs: list[object]) -> dict[str, object]:
    return {
        "id": str(uuid4()),
        "memory_id": memory_id,
        "revision_number": 1,
        "revision_type": "created",
        "action": "agentic_memory_commit",
        "previous_value": None,
        "new_value": {"text": "x", "source_refs": list(refs)},
        "metadata_json": {"agentic_memory": True},
    }


def _identity(profile: str = "trusted_local_agent", project: str | None = "alpha") -> AgentIdentity:
    return AgentIdentity(
        agent_id=f"{profile}-{project}",
        permission_profile=profile,
        project_scope=(project,) if project else (),
        auth="agent_api_key",
        project_scope_locked=project is not None,
    )


def _reader(store: _Store, identity: AgentIdentity | None = None) -> SavedProvenanceReader:
    return SavedProvenanceReader(store, fence=SourceReadFence.for_identity(identity or _identity()))


# -- 1. the owner ------------------------------------------------------------------------------------------------


def test_the_owner_is_returned_everything_and_no_source_is_read() -> None:
    """A call with no agent identity is the owner's. Every method hands back the input object itself and reads no source,
    even for a memory whose source is archived, missing or above any ceiling, and the link list is the stored one.

    Mutation: make ``SourceReadFence.fenced`` return ``True`` (``vnext_source_fence.py``): the owner is then withheld from
    and the source reads are counted.
    """

    store = _Store()
    archived = store.add_source()
    store.sources[archived]["deleted_at"] = "2026-10-01T00:00:00Z"
    memory = _memory("m1", sources=[archived, str(uuid4())])
    store.add_link("m1", archived)
    store.add_link("m1", None)
    reader = SavedProvenanceReader(store, fence=SourceReadFence.unfenced())
    assert reader.memory(memory) is memory
    assert reader.memories([memory])[0] is memory
    payload = {"memory": memory, "other": [1, 2]}
    assert reader.tree(payload) is payload
    revision = _revision("m1", [archived])
    assert reader.revision(revision) is revision
    assert [link["source_id"] for link in reader.links("m1")] == [archived, None]
    assert store.source_reads == 0
    assert reader.admits_link({"source_id": None})


# -- 2. a key bound to a project --------------------------------------------------------------------------------


def test_a_memory_whose_sources_are_all_readable_is_returned_as_the_same_object() -> None:
    """Nothing is copied when nothing is withheld, so an authorized caller's answer is the stored row, byte for byte.

    Mutation: return the rebuilt copy even when ``withhold_quotes`` is false (``_memory`` in the reader): the identity
    assertion fails, and a copy that drops a key shows in the equality assertion.
    """

    store = _Store()
    own = store.add_source()
    memory = _memory("m1", sources=[own])
    store.add_link("m1", own)
    before = copy.deepcopy(memory)
    reader = _reader(store)
    assert reader.memory(memory) is memory
    assert memory == before
    assert [link["source_id"] for link in reader.links("m1")] == [own]
    revision = _revision("m1", [own])
    assert reader.revision(revision) is revision


_REFUSED = {
    "above the ceiling": {"sensitivity": "confidential"},
    "restricted domain": {"domain": "health"},
    "other project": {"project": "beta"},
    "global source": {"project": ""},
}


@pytest.mark.parametrize("kind", [*_REFUSED, "archived", "missing", "null id"])
def test_a_refused_source_is_withheld_with_every_copy_of_its_quote(kind: str) -> None:
    """For each way a source stops being readable (a raised ceiling, a restricted domain, another project, a global
    source, archived, deleted from the table, a link whose source id was set to null when its source was removed), the
    link is left out, the three copies of the quote are removed from the row, the refs that name the source are dropped
    from the four ref lists and from the revision, and everything else on the row is untouched. A key bound to a
    project is the reader.

    Mutations, each alone, in ``vnext_source_fence.py``: ``return True`` from ``SourceReadFence._admits`` (every kind
    except ``archived``, ``missing`` and ``null id``, which the store and ``admits_link`` decide); make ``_judge`` set
    ``self._admitted[source_id] = True`` for a missing row (``missing``); make ``admits_link`` return ``True`` for a link
    with no source id (``null id``); drop one of the copies that ``_without_quote_copies`` removes (the
    copy it names); drop one of the four ``_scrub_refs_key`` calls (that list); drop the revision filter.
    """

    store = _Store()
    own = store.add_source()
    if kind in _REFUSED:
        refused: str | None = store.add_source(**_REFUSED[kind])  # type: ignore[arg-type]
    elif kind == "archived":
        refused = store.add_source()
        store.sources[refused]["deleted_at"] = "2026-10-01T00:00:00Z"
    elif kind == "missing":
        refused = str(uuid4())
    else:
        refused = None
    memory = _memory("m1", sources=[own, refused], extra_refs=["https://example.test/doc"])
    store.add_link("m1", own, quote=None)
    refused_link = store.add_link("m1", refused)
    # A trusted key reads every domain, so the restricted domain is refused to a project scoped key.
    reader = _reader(store, _identity("project_scoped_agent") if kind == "restricted domain" else None)

    links = reader.links("m1")
    assert refused_link not in links
    assert [link["source_id"] for link in links] == [own]

    shown = reader.memory(memory)
    assert shown is not memory
    metadata = shown["metadata_json"]
    assert "provenance" not in metadata and "replacement_provenance" not in metadata  # type: ignore[operator]
    assert "conversation_excerpt" not in metadata["agentic_memory"]  # type: ignore[index]
    # The URL names nothing the caller may read, and it stands in a list that held a refused ref, so it is withheld with the rest of
    # an entry that names nothing: nothing says it is not the words of the source.
    # A link whose source id was set to null leaves no ref to refuse, so the list holds nothing refused and the URL stays.
    expected_refs = [own, None] if refused is not None else [own, "https://example.test/doc"]
    assert metadata["source_refs"] == expected_refs  # type: ignore[index]
    assert metadata["agentic_memory"]["source_refs"] == expected_refs  # type: ignore[index]
    assert shown["value"]["source_refs"] == expected_refs  # type: ignore[index]
    # What is not provenance is kept as it was.
    assert shown["canonical_text"] == memory["canonical_text"]
    assert metadata["review_required"] is False  # type: ignore[index]
    assert metadata["agentic_memory"]["rationale"] == "kept as is"  # type: ignore[index]
    assert metadata["agentic_memory"]["status"] == "committed"  # type: ignore[index]
    assert shown["value"]["intent"] == "explicit_remember"  # type: ignore[index]
    # The stored row is not changed by the read.
    assert "provenance" in memory["metadata_json"]  # type: ignore[operator]
    serialized = str(shown)
    assert "zinnwald" not in serialized and "marlin" not in serialized
    if refused is not None:
        assert refused not in serialized

    shown_revision = reader.revision(
        _revision("m1", [ref for ref in (own, refused, "https://example.test/doc") if ref is not None])
    )
    assert shown_revision["new_value"]["source_refs"] == expected_refs  # type: ignore[index]
    assert shown_revision["action"] == "agentic_memory_commit"


def test_one_refused_source_withholds_the_copies_but_keeps_the_link_of_a_readable_one() -> None:
    """A memory that cites two sources keeps the link, and the quote on it, of the one the caller may read, and loses the
    link of the other, when the two quotes say different things. The metadata copies carry one quote for the memory and
    cannot say which source it came from, so they are withheld as soon as any cited source is refused. A ref that names
    no source (a URL) stays. (A link that says the same text as the refused one is the next test.)

    Mutation: build ``refused`` in ``_verdict`` from the metadata copies only (drop ``link_ids`` from ``link_ids | cited.named``): the
    refused link no longer withholds the copies.
    """

    store = _Store()
    own = store.add_source()
    other = store.add_source(sensitivity="confidential")
    store.add_link("m1", own, quote="own quote")
    store.add_link("m1", other, quote="other quote")
    memory = _memory("m1", sources=[own])
    memory["metadata_json"]["source_refs"] = [own, "https://example.test/doc"]  # type: ignore[index]
    reader = _reader(store)
    shown_links = reader.links("m1")
    assert [(link["source_id"], link["quote"]) for link in shown_links] == [(own, "own quote")]
    assert shown_links[0] is store.links[0], "an authorized link is the stored one"
    shown = reader.memory(memory)
    assert "provenance" not in shown["metadata_json"]  # type: ignore[operator]
    assert shown["metadata_json"]["source_refs"] == [own, "https://example.test/doc"]  # type: ignore[index]


def test_a_link_that_says_the_same_text_as_a_refused_link_loses_its_quote_and_one_that_says_something_else_keeps_it() -> None:
    """The commit route saves one ``conversation_excerpt`` as the quote of every link it makes, so a memory committed
    with two sources, where the excerpt came from the first, has two links with the same text. When the caller may not
    read the first source, the link to the second is still shown (its id and role) but without the quote, which can be
    the refused source's bytes. The comparison ignores the whitespace between words. A link whose quote says something
    else, as the link from a captured candidate to its own source does, keeps it, and so does a link when the refused
    link has no quote or a blank one. The stored links are not changed.

    Mutations, each alone, in ``SavedProvenanceReader._shown``: return ``link`` whenever it is admitted (the same text
    stays); compare the quotes without ``_quote_text`` (``_quote_text`` returning ``str(value)``: the whitespace variant
    stays); withhold the quote of every link when any sibling is refused (the independent quote is lost); drop the
    ``quote is not None`` guard (a link with no quote is copied), or the blank test of ``_quote_text`` (``return
    text``: a blank quote is withheld as if it were text).
    """

    store = _Store()
    first, independent, blank, speaks, mute = (store.add_source() for _ in range(5))
    refused, refused_blank, refused_mute, refused_mute_too = (
        store.add_source(sensitivity="confidential") for _ in range(4)
    )
    store.add_link("m1", first, quote=_QUOTE)
    store.add_link("m1", independent, quote="independent quote")
    store.add_link("m1", refused, quote="  " + _QUOTE.replace(" ", "\n  ") + " ")
    store.add_link("m2", blank, quote="   ")
    store.add_link("m2", refused_blank, quote="   ")
    store.add_link("m3", speaks, quote="a quote")
    store.add_link("m3", refused_mute, quote=None)
    store.add_link("m4", mute, quote=None)
    store.add_link("m4", refused_mute_too, quote=None)
    reader = _reader(store)
    assert [(link["source_id"], link["quote"]) for link in reader.links("m1")] == [
        (first, None),
        (independent, "independent quote"),
    ]
    shown_first = reader.links("m1")[0]
    assert shown_first["evidence_role"] == "supports" and shown_first["target_id"] == "m1"
    assert store.links[0]["quote"] == _QUOTE, "the stored link is not changed by the read"
    assert [(link["source_id"], link["quote"]) for link in reader.links("m2")] == [(blank, "   ")]
    assert [(link["source_id"], link["quote"]) for link in reader.links("m3")] == [(speaks, "a quote")]
    (still_mute,) = reader.links("m4")
    assert still_mute is store.links[7], "a link with no quote is not copied for a sibling with no quote"


def test_the_quote_of_every_link_of_a_memory_is_kept_while_every_source_is_readable() -> None:
    """The control for the test above: when no link of a memory is left out, ``links`` and ``shown_link`` hand back the
    stored links themselves, quote included, even when two of them say the same text, and a refused source of another
    memory that says that text is not a sibling.

    Mutations, each alone, in ``SavedProvenanceReader``: compare a quote with the admitted siblings as well (drop the
    ``if not self.admits_link(other)`` filter of ``_shown``: every link equals itself and loses its quote); take every
    link the reader has loaded as the siblings of a link (``siblings=[link for links in self._links.values() for link in
    links]`` in ``shown_link``): the first memory loses its quote to the refused source of the second.
    """

    store = _Store()
    first, second = store.add_source(), store.add_source()
    refused = store.add_source(sensitivity="confidential")
    store.add_link("m1", first, quote="shared quote")
    store.add_link("m1", second, quote="second quote")
    store.add_link("m2", first, quote="shared quote")
    store.add_link("m2", refused, quote="shared quote")
    store.add_link("m3", first, quote="twin quote")
    store.add_link("m3", second, quote="twin quote")
    reader = _reader(store)
    assert reader.shown_link(store.links[3]) is None
    assert reader.shown_link(store.links[2]) == {**store.links[2], "quote": None}
    kept = reader.links("m1")
    assert [(link["source_id"], link["quote"]) for link in kept] == [(first, "shared quote"), (second, "second quote")]
    assert all(shown is stored for shown, stored in zip(kept, store.links[:2], strict=True))
    assert reader.shown_link(store.links[0]) is store.links[0], "the refused source of m2 is not a sibling of m1's link"
    twins = reader.links("m3")
    assert all(shown is stored for shown, stored in zip(twins, store.links[4:], strict=True)) and len(twins) == 2


def test_the_pack_reads_the_links_and_the_sources_of_every_memory_once_to_judge_the_quotes() -> None:
    """``supporting_evidence`` asks about each link of each packed memory. After ``judge_links`` has been given the links
    the pack read, ``shown_link`` costs no further read of the links or the sources, however many memories there are,
    because the siblings of a link are loaded in the same two reads.

    Mutation: make ``judge_links`` skip ``_load_links`` (or ``shown_link`` skip the cache and read the memory's links
    each time): the counters read the number of memories.
    """

    store = _Store()
    sources = [store.add_source(), store.add_source(sensitivity="confidential")]
    for index in range(6):
        for source in sources:
            store.add_link(f"m{index}", source, quote=f"quote {index}")
    reader = _reader(store)
    reader.judge_links(store.links)
    shown = [reader.shown_link(link) for link in store.links]
    assert (store.link_reads, store.source_reads) == (1, 1)
    assert [link is None for link in shown] == [False, True] * 6, "the link to the confidential source is left out"
    assert all(link["quote"] is None for link in shown if link is not None), "the sibling of a refused link"


def test_a_memory_with_no_link_is_judged_by_its_copies_and_a_row_scrubbed_first_shows_nothing_to_refuse() -> None:
    """A commit held for review, or confirmed inline, then approved has no provenance link, so the sources it cited are
    named only in the ref lists of its own copies (``agentic_memory.source_refs``, ``value.source_refs``). The reader
    refuses on those, and withholds the excerpt. The same row after a scrub has dropped the refs (the context pack's
    scope pass does this for a pack with a project scope) names no source, so the reader finds nothing to refuse and the
    excerpt stays. That is why the pack asks the reader before the scrubs, which
    ``test_the_pack_judges_the_memory_rows_before_any_scrub_of_their_references`` pins.

    Mutation: make ``SavedProvenanceReader._verdict`` ignore the ids the row's copies name (``cited = _NO_CITED_IDS``): the
    first assertion fails.
    """

    store = _Store()
    refused = store.add_source(sensitivity="confidential")
    memory = _memory("m1", sources=[refused])
    del memory["metadata_json"]["provenance"]  # type: ignore[attr-defined]
    del memory["metadata_json"]["replacement_provenance"]  # type: ignore[attr-defined]
    assert not store.links, "the memory has no link"
    shown = _reader(store).memory(memory)
    assert "conversation_excerpt" not in shown["metadata_json"]["agentic_memory"]  # type: ignore[index]
    assert refused not in str(shown) and "zinnwald" not in str(shown)
    scrubbed = copy.deepcopy(memory)
    scrubbed["metadata_json"]["source_refs"] = []  # type: ignore[index]
    scrubbed["metadata_json"]["agentic_memory"]["source_refs"] = []  # type: ignore[index]
    scrubbed["value"]["source_refs"] = []  # type: ignore[index]
    assert _reader(store).memory(scrubbed) is scrubbed, "a row with no ref left shows the reader nothing to refuse"


def test_the_same_source_is_asked_again_by_every_read() -> None:
    """The verdict is the source as it is when the call is made, never a remembered one. A source made confidential
    between two reads is readable to the first reader and refused to the second.

    Mutation: cache the verdict at module level (a ``functools.cache`` on ``_judge``).
    """

    store = _Store()
    source = store.add_source()
    store.add_link("m1", source)
    memory = _memory("m1", sources=[source])
    assert _reader(store).memory(memory) is memory
    store.sources[source]["sensitivity"] = "confidential"
    assert _reader(store).memory(memory) is not memory
    store.sources[source]["sensitivity"] = "internal"
    assert _reader(store).memory(memory) is memory


def test_a_payload_is_read_with_one_lookup_of_the_links_and_one_of_the_sources() -> None:
    """A pack of many memories that cite the same few sources costs one read of the links and one of the sources, not one
    per row, and a row that is not a memory is left alone, as is the payload around it.

    Mutation: look each id up on its own (``_judge`` calling ``_rows_by_id`` once per id, or ``_load_links`` once per
    memory): the counters read the number of rows.
    """

    store = _Store()
    sources = [store.add_source(), store.add_source(sensitivity="confidential")]
    memories = []
    for index in range(6):
        memory_id = f"m{index}"
        for source in sources:
            store.add_link(memory_id, source)
        memories.append(_memory(memory_id, sources=sources))
    payload = {"relevant_memories": memories, "decisions": memories[:2], "budget": {"max": 10}, "trace_id": "t"}
    shown = _reader(store).tree(payload)
    assert (store.link_reads, store.source_reads) == (1, 1)
    assert shown["budget"] is payload["budget"] and shown["trace_id"] == "t"  # type: ignore[index]
    assert all("provenance" not in row["metadata_json"] for row in shown["relevant_memories"])  # type: ignore[index]
    assert "provenance" not in shown["decisions"][0]["metadata_json"]  # type: ignore[index]


def test_a_payload_with_nothing_to_withhold_is_returned_as_the_same_object() -> None:
    """``tree`` copies only what it changes, so a pack or a verb answer for an authorized caller is the object it was.

    Mutation: rebuild every container (``return rebuilt`` in ``_rebuild``).
    """

    store = _Store()
    source = store.add_source()
    store.add_link("m1", source)
    payload = {"memory": _memory("m1", sources=[source]), "status": "ok", "items": [{"a": 1}, {"b": [2, 3]}]}
    assert _reader(store).tree(payload) is payload


def test_memories_holds_a_row_of_any_shape_to_the_fence_and_tree_reads_only_what_looks_like_a_memory() -> None:
    """The context pack knows its rows are memories, so ``memories`` takes a row that lacks a field a stored memory
    always has (a stub store's row, a row built by an adapter) and still withholds from it. ``tree`` walks a payload it
    knows nothing about and so recognises a memory by its fields.

    Mutation: make ``memories`` call ``_collect_rows`` and filter by ``_is_memory_row`` first: the bare row keeps its
    copies.
    """

    store = _Store()
    other = store.add_source(sensitivity="confidential")
    store.add_link("bare", other)
    bare = {"id": "bare", "metadata_json": {"provenance": {"source_id": other, "quote": _QUOTE}}}
    assert "provenance" not in _reader(store).memories([bare])[0]["metadata_json"]  # type: ignore[operator]
    assert _reader(store).tree({"memory": bare})["memory"] is bare  # type: ignore[index]


def test_the_owner_is_not_the_only_unfenced_reader_a_keyless_declared_profile_is_held_to_its_profile() -> None:
    """A keyless call that declares a profile has an identity, so it is fenced by that profile's domains and ceiling (its
    declared project is not enforced, as everywhere else). A read only identity cannot read a ``private`` source.

    Mutation: make ``SourceReadFence.for_identity`` return ``unfenced()`` for an identity whose ``auth`` is not an
    agent key.
    """

    store = _Store()
    private = store.add_source(sensitivity="private")
    store.add_link("m1", private)
    memory = _memory("m1", sources=[private])
    declared = AgentIdentity(agent_id="declared-read-only", permission_profile="read_only_agent")
    assert _reader(store, declared).memory(memory) is not memory
    trusted = AgentIdentity(agent_id="declared-trusted", permission_profile="trusted_local_agent")
    assert _reader(store, trusted).memory(memory) is memory


# -- 3. the shape of the arguments ------------------------------------------------------------------------------


def test_the_fence_is_a_required_keyword_only_argument_with_no_default() -> None:
    """The reader and the pack take the caller's fence by keyword, with no default, so a caller that forgets it fails
    when it runs and not by showing a quote.

    Mutation: give either parameter a default (``= SourceReadFence.unfenced()``).
    """

    for function in (SavedProvenanceReader.__init__, VNextRetrievalService.compile_context_pack):
        parameters = inspect.signature(function).parameters
        name = "fence" if function is SavedProvenanceReader.__init__ else "source_fence"
        parameter = parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty


def _python_files(*roots: str) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        files.extend(path for path in sorted((_ROOT / root).rglob("*.py")) if "node_modules" not in path.parts)
    return files


def _calls_named(name: str, *roots: str) -> list[tuple[str, str, ast.Call]]:
    found: list[tuple[str, str, ast.Call]] = []
    for path in _python_files(*roots):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if called != name:
                continue
            scope = node
            while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = parents[scope]
            found.append((str(path.relative_to(_ROOT)), getattr(scope, "name", "<module>"), node))
    return found


# The sites that read a pack for a caller with an agent identity. Each passes that caller's own fence.
_AGENT_FACING_PACK_SITES = {
    ("apps/api/src/alicebot_api/mcp/context.py", "_vnext_context_pack_payload"),
    ("apps/api/src/alicebot_api/routers/vnext_retrieval.py", "create_vnext_context_pack"),
}
# The sites that run for the owner: the command line on the owner's own vault, the smoke runs and the evaluation
# harnesses. Each passes ``SourceReadFence.unfenced()`` where a reviewer sees it.
_OWNER_PACK_SITES = {
    "apps/api/src/alicebot_api/cli/context.py",
    "apps/api/src/alicebot_api/cli/smokes.py",
    "apps/api/src/alicebot_api/vnext_evals.py",
    "eval/longmemeval/adapter.py",
    "eval/longmemeval/count_probe.py",
    "eval/longmemeval/coverage_probe.py",
    "eval/scale/harness.py",
}


def test_every_context_pack_call_names_its_fence_and_the_agent_facing_ones_pass_the_callers_own() -> None:
    """Every call of ``compile_context_pack`` outside the tests passes ``source_fence=``. The two doors an agent reaches
    (the MCP tool and ``POST /v0/vnext/context-packs``) pass ``SourceReadFence.for_identity(identity)``, and every
    other site is on the list of owner sites and passes ``SourceReadFence.unfenced()``. A new call that is on neither list
    fails here, which is where someone decides whose fence it takes.

    Mutations: pass ``SourceReadFence.unfenced()`` in the MCP pack call (``mcp/context.py``) or in the HTTP route
    (``routers/vnext_retrieval.py``); add a call to ``compile_context_pack`` in a new file.
    """

    calls = _calls_named("compile_context_pack", "apps", "eval", "scripts", "workers")
    assert calls, "the scan found no call at all"
    agent_facing = set()
    for path, function, call in calls:
        keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in call.keywords}
        assert "source_fence" in keywords, (path, function)
        if (path, function) in _AGENT_FACING_PACK_SITES:
            agent_facing.add((path, function))
            assert keywords["source_fence"] == "SourceReadFence.for_identity(identity)", (path, function)
        else:
            assert path in _OWNER_PACK_SITES, f"unclassified compile_context_pack call: {path} {function}"
            # The command line modules import the type under an underscore name, so that it does not become a public
            # name of the ``alicebot_api.cli`` facade, whose public names are pinned.
            assert keywords["source_fence"] in {"SourceReadFence.unfenced()", "_SourceReadFence.unfenced()"}, (
                path,
                function,
            )
    assert agent_facing == _AGENT_FACING_PACK_SITES


def test_the_pack_judges_the_memory_rows_before_any_scrub_of_their_references() -> None:
    """``compile_context_pack`` passes its ranked memory rows through the reader first, and feeds the result forward.
    The scrubs that follow (``_sanitize_memory_scope_pointers``, ``_drop_hidden_memory_ids_from_metadata`` and
    ``_sanitize_memory_scope_references``) remove refs from each row, and a memory with no provenance link names its
    sources only there: judged after them, the row shows the reader nothing to refuse and keeps its quote, for every key
    bound to a project (the scope pass runs only for a pack with a scope). This is the order pin for the behaviour that
    ``test_the_copies_of_a_memory_with_no_link_follow_its_source_when_the_source_is_reclassified`` measures.

    Mutation: move the ``ranked_memories = ... saved_provenance.memories(ranked_memories)`` statement below the
    ``_sanitize_memory_scope_references`` call (or rename its target so the scrubs do not receive its result).
    """

    tree = ast.parse((_SRC / "vnext_retrieval.py").read_text(encoding="utf-8"))
    pack = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "compile_context_pack"
    )
    reads = [
        node
        for node in ast.walk(pack)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "memories"
        and ast.unparse(node.func.value) == "saved_provenance"
    ]
    assert len(reads) == 1, "one judgement of the rows, and it is the first"
    assignment = next(
        node
        for node in ast.walk(pack)
        if isinstance(node, ast.Assign) and any(call is reads[0] for call in ast.walk(node.value))
    )
    assert [ast.unparse(target) for target in assignment.targets] == ["ranked_memories"]
    assert ast.unparse(reads[0].args[0]) == "ranked_memories"
    scrubs = {"_sanitize_memory_scope_pointers", "_sanitize_memory_scope_references", "_drop_hidden_memory_ids_from_metadata"}
    scrub_calls = [
        node
        for node in ast.walk(pack)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in scrubs
    ]
    assert {call.func.attr for call in scrub_calls} == scrubs  # type: ignore[attr-defined]
    for call in scrub_calls:
        assert call.lineno > assignment.lineno, ast.unparse(call.func)
        assert ast.unparse(call.args[0]) == "ranked_memories", "the scrub receives the judged rows"


# The public verbs of the commit service that return a memory row, and why each is held to the caller's read fence.
_HELD_VERBS = ("commit", "confirm", "undo", "correct", "forget", "accept_consolidation_candidate", "expire", "unexpire")
# Public methods that build a dict with a ``"memory"`` key and are not held, each with the reason.
_UNHELD_PUBLIC_METHODS = {
    # alice_explain authorizes every source the chain names and fails closed, and the HTTP audit route is the owner's.
    "audit": "authorized by the caller of audit()",
}


def test_every_memory_verb_that_hands_a_row_back_is_held_to_the_callers_read_fence() -> None:
    """The eight verbs of ``VNextMemoryCommitService`` that return a row carry the decorator, and a public method that
    builds a ``"memory"`` entry without it is on a list with its reason. A new verb that returns a memory fails here
    until it is decorated or listed.

    Mutation: remove the decorator from one verb; add a public method that returns ``{"memory": row}`` undecorated.
    """

    for name in _HELD_VERBS:
        assert getattr(getattr(VNextMemoryCommitService, name), "held_to_the_callers_read_fence", False), name
    tree = ast.parse((_SRC / "vnext_memory_commit.py").read_text(encoding="utf-8"))
    service = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "VNextMemoryCommitService")
    for method in service.body:
        if not isinstance(method, ast.FunctionDef) or method.name.startswith("_"):
            continue
        builds_memory = any(
            isinstance(node, ast.Dict)
            and any(isinstance(key, ast.Constant) and key.value == "memory" for key in node.keys)
            for node in ast.walk(method)
        )
        decorated = any(ast.unparse(decorator) == "_held_to_the_callers_read_fence" for decorator in method.decorator_list)
        if builds_memory:
            assert decorated or method.name in _UNHELD_PUBLIC_METHODS, f"unclassified verb: {method.name}"
        if method.name in _HELD_VERBS:
            assert decorated, method.name


def test_the_decorator_asks_for_the_fence_of_the_identity_the_verb_was_called_with() -> None:
    """The owner's call (no identity) gets the row it was returned, as the same object, and a key's call gets the row with
    the quote of a refused source withheld. A verb called without ``identity`` at all is the owner's.

    Mutation: read the identity from ``args`` instead of ``kwargs`` in the decorator (every verb is then the owner's, and
    the key's row keeps its quote). The ``if identity is None`` return is a shortcut: the owner's fence reads nothing
    either way, so dropping it changes no answer.
    """

    store = _Store()
    refused = store.add_source(sensitivity="confidential")
    store.add_link("m1", refused)
    row = _memory("m1", sources=[refused])

    class _Service:
        def __init__(self) -> None:
            self.store = store

        @_held_to_the_callers_read_fence
        def verb(self, *, identity: AgentIdentity | None = None) -> dict[str, object]:
            return {"status": "ok", "memory": row}

    service = _Service()
    owner = service.verb(identity=None)
    assert owner["memory"] is row
    assert service.verb()["memory"] is row
    shown = service.verb(identity=_identity())
    assert "provenance" not in shown["memory"]["metadata_json"]  # type: ignore[index]
    assert shown["status"] == "ok"


def test_review_by_id_reads_saved_provenance_only_through_the_reader() -> None:
    """``_vnext_memory_review`` builds its answer from ``SavedProvenanceReader`` and does not call the store's
    ``list_provenance_links`` or return the stored row itself.

    Mutation: call ``store.list_provenance_links(...)`` in ``_vnext_memory_review``.
    """

    tree = ast.parse((_SRC / "mcp" / "review.py").read_text(encoding="utf-8"))
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_vnext_memory_review")
    names = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(function) if isinstance(node, ast.Attribute)}
    assert "SavedProvenanceReader" in names
    assert {"memory", "revision", "links"} <= attributes
    assert "list_provenance_links" not in attributes


# -- 4. the writers of a saved quote ----------------------------------------------------------------------------

# Every place outside the tests that writes a quote on a link or a copy of one on a memory (a dict with a ``quote``,
# ``conversation_excerpt`` or ``replacement_provenance`` key, or a store to one). Each is listed with how its reader is
# held to the fence, so a new writer cannot be added without someone checking that its readers are.
_QUOTE_WRITERS = {
    # The link is read by review by id, the pack's supporting_evidence, explain; the metadata copies by every row reader.
    ("apps/api/src/alicebot_api/vnext_memory_commit.py", "_create_provenance_links"): "link quote (commit)",
    ("apps/api/src/alicebot_api/vnext_memory_commit.py", "_base_metadata"): "agentic_memory.conversation_excerpt",
    ("apps/api/src/alicebot_api/vnext_memory_commit.py", "_request_fingerprint"): "idempotency fingerprint, not shown",
    ("apps/api/src/alicebot_api/vnext_memory_commit.py", "<module>"): "the label table of the commit memory types",
    ("apps/api/src/alicebot_api/mcp/review.py", "_vnext_memory_correct"): "link quote, replacement_provenance",
    ("apps/api/src/alicebot_api/mcp/review.py", "_validated_review_provenance"): "the provenance object, then the link",
    ("apps/api/src/alicebot_api/mcp/memories.py", "_handle_alice_vnext_commit_memory"): "passes the argument to commit",
    ("apps/api/src/alicebot_api/cli/memories.py", "_run_vnext_memory_commit"): "passes the argument to commit",
    ("apps/api/src/alicebot_api/vnext_capture.py", "_capture_source"): "link quote = the candidate's own text",
    ("apps/api/src/alicebot_api/vnext_source_regeneration.py", "regenerate_source_inputs"): "fresh candidate quote; SavedProvenanceReader and row readers apply the source fence",
    ("apps/api/src/alicebot_api/vnext_connectors.py", "ingest_agent_output"): "link quote = the item title",
    ("apps/api/src/alicebot_api/vnext_retrieval.py", "_supporting_evidence"): "reads the link, fenced by admits_link",
    ("apps/api/src/alicebot_api/onramp.py", "_apply_import_quarantine"): "replaces a quote with a placeholder",
    ("apps/api/src/alicebot_api/mcp/definitions.py", "<module>"): "tool schemas",
    # Legacy continuity objects, not vNext memories.
    ("apps/api/src/alicebot_api/_contracts/continuity.py", "as_payload"): "continuity correction payload",
    ("apps/api/src/alicebot_api/continuity_review.py", "apply_continuity_correction"): "continuity correction",
    # A measurement script that builds a synthetic vault.
    ("scripts/measure_recall_source_lookup.py", "build_vault"): "synthetic vault",
}
_QUOTE_KEYS = {"quote", "conversation_excerpt", "replacement_provenance"}


def test_every_writer_of_a_saved_quote_is_on_the_list_a_reader_was_checked_against() -> None:
    """Scans every Python file outside the tests for a dict with a ``quote``, ``conversation_excerpt`` or
    ``replacement_provenance`` key and for a store to one, and compares the places found with the list above. A new
    writer fails here: whoever adds it checks the readers of what it saves against the fence, and lists it.

    Mutation: add ``{"quote": text}`` in a new function of any module under ``apps/api/src``.
    """

    found: set[tuple[str, str]] = set()
    for path in _python_files("apps/api/src", "scripts", "eval", "workers"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

        def scope_of(node: ast.AST) -> str:
            while node in parents:
                node = parents[node]
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return node.name
            return "<module>"

        for node in ast.walk(tree):
            keys: list[str] = []
            if isinstance(node, ast.Dict):
                keys = [key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)]
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.ctx, ast.Store)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                keys = [node.slice.value]
            if _QUOTE_KEYS.intersection(keys):
                found.add((str(path.relative_to(_ROOT)), scope_of(node)))
    assert found == set(_QUOTE_WRITERS), {
        "unlisted writers": sorted(found - set(_QUOTE_WRITERS)),
        "listed but gone": sorted(set(_QUOTE_WRITERS) - found),
    }


# -- 4. alice_explain and the sources a memory names without a link ---------------------------------------------


def test_the_sources_a_memory_audit_names_are_read_from_the_memory_the_revisions_and_the_events() -> None:
    """A memory audit (``VNextMemoryCommitService.audit``) holds the memory row, its revisions and its events, and each
    keeps the refs of the sources the memory was written with: the memory in ``agentic_memory.source_refs``,
    ``value.source_refs`` and the metadata, a revision in ``previous_value`` and ``new_value``, an event in the
    ``changes`` of a ``memory.updated`` payload. The helper returns every id in every form the commit accepts, and
    ignores a ref that is not a source id and a row that has none of the fields.

    Mutations, each alone: drop the ``revisions`` loop, the ``events`` loop, or the ``changes`` branch of
    ``cited_source_ids_in_memory_audit``: the id of that place is missing from the set.
    """

    ids = [str(uuid4()) for _ in range(8)]
    audit = {
        "memory": {
            "id": "m1",
            "metadata_json": {"agentic_memory": {"source_refs": [ids[0], "https://example.test/doc"]}, "source_refs": [ids[1]]},
            "value": {"source_refs": [f"source:{ids[2]}"]},
        },
        "revisions": [
            {"revision_number": 1, "new_value": {"source_refs": [ids[3]]}, "previous_value": {"source_refs": [ids[4]]}},
            {"revision_number": 2, "new_value": None, "previous_value": "text"},
            "not a row",
        ],
        "events": [
            {"payload_json": {"changes": {"metadata_json": {"agentic_memory": {"source_refs": [ids[5]]}}}}},
            {"payload_json": {"metadata_json": {"source_refs": ["{" + ids[6] + "}"]}}},
            {"payload_json": {"policy_decision": {"decision": "allowed"}}},
            {"payload_json": None},
            "not a row",
        ],
        "provenance_links": [{"source_id": ids[7]}],
    }
    assert set(cited_source_ids_in_memory_audit(audit).named) == set(ids[:7]), "the links are authorized on their own"
    assert set(cited_source_ids_in_memory_audit({}).named) == set()
    assert set(cited_source_ids_in_memory_audit({"memory": None, "revisions": "x", "events": 3}).named) == set()


class _ExplainStore:
    def __init__(self, sources: dict[str, dict[str, object]]) -> None:
        self.sources = sources
        self.reads: list[str] = []

    def get_source(self, source_id: str) -> dict[str, object] | None:
        self.reads.append(source_id)
        return self.sources.get(source_id)


@pytest.fixture
def authorized_ids(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the policy call of explain with one that refuses a confidential source, and record each source it is asked
    about. The policy itself is exercised by the lifecycle tests on a real vault."""

    asked: list[str] = []

    def authorize(
        store: object, *, identity: object, resource: dict[str, object], project_scope: object, target_type: str, target_id: str
    ) -> None:
        asked.append(target_id)
        assert target_type == "source"
        if resource.get("sensitivity") == "confidential":
            raise evidence_artifacts._ExplainAuthorizationError()

    monkeypatch.setattr(evidence_artifacts, "_authorize_explain_resource", authorize)
    return asked


def test_explain_authorizes_a_source_named_only_by_a_copy_and_refuses_the_call_for_one_it_may_not_read(
    authorized_ids: list[str],
) -> None:
    """A memory with no link names its sources only in its copies. For a key the audit authorizes each of them as it does
    a linked source: a readable one passes, one above the key's ceiling refuses the call, and one that does not exist (a
    deleted source, an id that was never a source) refuses it as a missing linked source does. A source that a link and a
    copy both name is asked about once.

    Mutations: remove the ``copied_source_ids`` loop of ``_authorize_memory_audit_provenance`` (the first two refusals
    return an id set); iterate ``copied_source_ids`` and not ``copied_source_ids - authorized_source_ids`` (the shared
    source is asked about twice).
    """

    readable, confidential, missing = str(uuid4()), str(uuid4()), str(uuid4())
    store = _ExplainStore({readable: {"id": readable, "sensitivity": "internal"}, confidential: {"sensitivity": "confidential"}})
    identity = _identity()
    authorize = evidence_artifacts._authorize_memory_audit_provenance

    assert authorize(store, identity=identity, provenance_links=[], copied_source_ids={readable}) == {readable}
    for refused in (confidential, missing):
        with pytest.raises(evidence_artifacts._ExplainAuthorizationError):
            authorize(store, identity=identity, provenance_links=[], copied_source_ids={readable, refused})
    authorized_ids.clear()
    linked = authorize(
        store, identity=identity, provenance_links=[{"source_id": readable}], copied_source_ids={readable}
    )
    assert linked == {readable} and authorized_ids == [readable], "one policy question for a source named twice"


def test_explain_asks_nothing_about_copies_for_a_call_that_is_not_key_bound(authorized_ids: list[str]) -> None:
    """A keyless call keeps its historical tolerance: nothing is read and nothing is refused, whatever the copies name.
    The parameter is required and keyword-only, so a caller cannot leave the copies out by accident.

    Mutation: remove the ``_is_key_bound_explain`` early return of ``_authorize_memory_audit_provenance``: the keyless
    call reads the store and is refused for a source it may not read.
    """

    store = _ExplainStore({})
    owner_like = AgentIdentity(agent_id="declared", permission_profile="read_only_agent")
    authorize = evidence_artifacts._authorize_memory_audit_provenance
    for identity in (None, owner_like):
        assert authorize(store, identity=identity, provenance_links=[], copied_source_ids={str(uuid4())}) == set()
    assert store.reads == [] and authorized_ids == []
    parameter = inspect.signature(authorize).parameters["copied_source_ids"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is inspect.Parameter.empty
