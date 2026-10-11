"""The derived-label closure finds the rows a record cites by the parser the saved-quote reader uses.

A consolidation report of v0.20.0 copies the ``source_refs`` of its members as their writers sent them, so a report holds a
citation of a memory as ``memory:<id>`` in any case, as an ``alice://`` URL, as JSON text that quotes the memory, with the id
in braces, without hyphens or in capitals. The closure used to read the plain ``memory:<id>`` string only, and a report that
cited a confidential memory any other way kept the label it was stored with. These tests state every shape, on the three kinds
of row that hold refs (a report, a memory, an open loop), and ask the saved-quote reader itself what it names, so the closure
cannot drift from it.

Mutations, each one alone (``scripts/derived_label_mutations.json`` replays them):

* in ``_derived_dependency_record`` drop ``found |= citations``: the closure finds nothing but the ids it found before, and the
  shape tests fail;
* in ``cited_inputs`` drop the memory half or the source half: the shapes of that kind fail;
* in ``cited_inputs`` stop wrapping a mapping: an ``id`` or ``ref`` key at the top of the metadata is read as a citation;
* in ``_derived_dependency_record`` drop the ``unreadable_citation`` problem, or in ``has_unreadable_citation`` drop the broken
  JSON rule, the marked word rule or the rule that a quote is text: the unreadable-citation tests fail;
* in ``dependency_syntax_key`` drop the citations: two rows that differ only in a citation share one answer.
"""

from __future__ import annotations

import ast
import json
import random
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from alicebot_api import vnext_citations, vnext_derived_labels, vnext_source_fence
from alicebot_api.vnext_citations import cited_inputs, has_unreadable_citation
from alicebot_api.vnext_derived_labels import (
    dependencies_of,
    dependency_record,
    dependency_syntax_key,
    record_citations,
    settle_labels,
)
from alicebot_api.vnext_source_fence import cited_memory_refs, cited_source_ids
from tests.unit.legacy_citation_support import (
    SPELLINGS,
    candidate_memory,
    memory_shapes,
    open_loop,
    report,
    source_shapes,
)

ROOT = Path(__file__).resolve().parents[2]
USER = "user-1"
QUOTE = "Violet kiln code is 4417"
MEMORY = str(uuid4())
SOURCE = str(uuid4())
# The source a loop came from. It is an input of the loop whatever the refs say.
LOOP_SOURCE = str(uuid4())
# Entries the reader names nothing in: its incidental class. The closure reads what the reader names, so these name no input.
# Each is listed so a shape that stops being named is a failure here and not a silent gap.
NAMES_NOTHING = {
    ("memory", "prefix-fragment", "padded"),
    ("memory", "prefix-in-a-sentence", "padded"),
    ("memory", "url-in-a-sentence", "padded"),
    ("source", "prefix-fragment", "padded"),
    ("source", "url", "padded"),
    ("source", "url-capitals", "padded"),
    ("source", "json-in-json", "canonical"),
    ("source", "json-in-json", "capitals"),
    ("source", "json-in-json", "no-hyphens"),
    ("source", "json-in-json", "capitals-no-hyphens"),
    ("source", "json-in-json", "braces"),
    ("source", "json-in-json", "urn"),
    ("source", "json-in-json", "padded"),
}


def _node(kind: str, row_id: str, **fields: object) -> dict[str, object]:
    row: dict[str, object] = {
        "kind": kind,
        "id": row_id,
        "user_id": USER,
        "domain": "unknown",
        "sensitivity": "internal",
        "metadata_json": {},
    }
    row.update(fields)
    return row


def _memory(row_id: str, **fields: object) -> dict[str, object]:
    fields = {"memory_key": row_id, "canonical_text": "The kiln code is 4417", "status": "active", **fields}
    return _node("memory", row_id, **fields)


def _held(kind: str, entries: list[object], position: str = "source_refs") -> dict[str, object]:
    """The row that holds ``entries``, in the shape the version before derived labels stored."""

    if kind == "artifact":
        return _node("artifact", str(uuid4()), **report(entries, position=position))
    if kind == "memory":
        return _node("memory", str(uuid4()), **candidate_memory(entries, position=position))
    return _node("open_loop", str(uuid4()), **open_loop(entries, source_id=LOOP_SOURCE))


def _named_by_the_reader(entry: object) -> set[tuple[str, str]]:
    """What the saved-quote reader names in one entry of ``source_refs``. It is the oracle."""

    return {("source", item) for item in cited_source_ids([entry]).named} | {
        ("memory", item) for item in cited_memory_refs([entry]).named
    }


def _cases():
    for kind, shapes, ident in (("memory", memory_shapes(QUOTE), MEMORY), ("source", source_shapes(QUOTE), SOURCE)):
        for shape, build in shapes.items():
            for spelling, spell in SPELLINGS.items():
                yield kind, shape, spelling, build(spell(ident)), ident


CASES = list(_cases())


def _id(case: tuple) -> str:
    return "-".join(case[:3])


@pytest.mark.parametrize("case", CASES, ids=_id)
@pytest.mark.parametrize("holder", ["artifact", "memory", "open_loop"])
def test_the_closure_names_exactly_what_the_saved_quote_reader_names(case: tuple, holder: str) -> None:
    kind, shape, spelling, entry, ident = case
    baseline = dependencies_of(holder, _held(holder, []))
    row = _held(holder, [entry])
    found = dependencies_of(holder, row) - baseline
    named = _named_by_the_reader(entry)
    assert found == named, (shape, spelling)
    unreadable = record_citations(row)[1]
    if named:
        assert (kind, ident) in found and not unreadable
    elif (kind, shape, spelling) in NAMES_NOTHING:
        # The reader reads these ids as incidental: they are judged only when they name a stored row, and are not inputs.
        assert not unreadable
    else:
        # A marker with an id the reader cannot take (``memory:{<id>}#chunk-1``): the row is unverified.
        assert unreadable, "the corpus holds only shapes the reader names, or lists them in NAMES_NOTHING"


@pytest.mark.parametrize("position", ["source_refs", "proposal-source_refs", "agentic_memory", "provenance"])
@pytest.mark.parametrize("holder", ["artifact", "memory"])
def test_every_position_a_ref_can_stand_in_is_read(holder: str, position: str) -> None:
    for shape, build in memory_shapes(QUOTE).items():
        if shape.startswith("object") or shape == "json-object-spaced":
            continue
        entry = build(MEMORY)
        row = _held(holder, [entry], position)
        assert ("memory", MEMORY) in dependencies_of(holder, row), (holder, position, shape)


def test_a_citation_in_the_value_of_a_memory_is_read() -> None:
    row = _held("memory", [json.dumps({"memory_id": MEMORY, "quote": QUOTE})], "value")
    assert ("memory", MEMORY) in dependencies_of("memory", row)


@pytest.mark.parametrize(
    "entry",
    [
        f"memory:{MEMORY}",
        f"MEMORY:{MEMORY}",
        f"alice://memories/{MEMORY}",
        json.dumps({"memory_id": MEMORY, "quote": QUOTE}),
        json.dumps([{"memory_id": MEMORY.upper(), "quote": QUOTE}]),
        json.dumps({"memory_id": MEMORY.replace("-", "")}),
        json.dumps({"memory_id": "{" + MEMORY + "}"}),
        json.dumps({"memory_id": "urn:uuid:" + MEMORY}),
        json.dumps({"ref": f"memory:{MEMORY}"}),
    ],
    ids=["prefix", "prefix-capitals", "url", "json", "json-list-capitals", "json-no-hyphens", "json-braces", "json-urn", "json-prefixed"],
)
def test_the_shapes_the_reader_is_known_to_take_are_found(entry: str) -> None:
    """The oracle above is the reader. This list keeps the shapes named in the report, so a regression of the reader fails here."""

    assert ("memory", MEMORY) in dependencies_of("artifact", _held("artifact", [entry]))


def test_a_report_is_raised_to_the_memory_it_cites_in_every_shape() -> None:
    confidential = _memory(MEMORY, domain="health", sensitivity="confidential")
    for kind, shape, spelling, entry, ident in CASES:
        if kind != "memory" or (kind, shape, spelling) in NAMES_NOTHING or shape == "json-repeated-key":
            continue
        row = _held("artifact", [entry])
        if record_citations(row)[1]:
            continue
        settled = settle_labels([confidential, row]).by_stored("artifact", str(row["id"]))
        assert (settled.unverified, settled.domain, settled.sensitivity) == (False, "health", "confidential"), (shape, spelling)


def test_a_report_is_raised_to_the_source_it_cites_in_every_shape() -> None:
    source = _node("source", SOURCE, domain="health", sensitivity="confidential")
    for kind, shape, spelling, entry, ident in CASES:
        if kind != "source" or (kind, shape, spelling) in NAMES_NOTHING:
            continue
        row = _held("artifact", [entry])
        if record_citations(row)[1]:
            continue
        settled = settle_labels([source, row]).by_stored("artifact", str(row["id"]))
        assert (settled.unverified, settled.domain, settled.sensitivity) == (False, "health", "confidential"), (shape, spelling)


@pytest.mark.parametrize("holder", ["artifact", "memory", "open_loop"])
def test_a_row_that_cites_a_redacted_memory_is_unverified(holder: str) -> None:
    redacted = _memory(MEMORY, metadata_json={"redacted": True}, canonical_text="[redacted]")
    row = _held(holder, [json.dumps({"memory_id": MEMORY, "quote": QUOTE})])
    settled = settle_labels([redacted, _node("source", LOOP_SOURCE), row]).by_stored(holder, str(row["id"]))
    assert (settled.unverified, settled.reason) == (True, "input_redacted")


def test_a_repeated_key_in_json_text_names_every_value() -> None:
    """The reader keeps every value of a key the text writes twice, so a decoy id beside the real one is read, and judged."""

    decoy = str(uuid4())
    entry = '{"memory_id": "%s", "memory_id": "%s"}' % (decoy, MEMORY)
    row = _held("artifact", [entry])
    assert {("memory", decoy), ("memory", MEMORY)} <= dependencies_of("artifact", row)
    confidential = _memory(MEMORY, domain="health", sensitivity="confidential")
    low = _memory(decoy)
    settled = settle_labels([confidential, low, row]).by_stored("artifact", str(row["id"]))
    assert (settled.unverified, settled.domain, settled.sensitivity) == (False, "health", "confidential")
    assert settle_labels([confidential, row]).by_stored("artifact", str(row["id"])).reason == "missing_dependency"


def test_a_row_that_cites_a_memory_that_is_not_stored_is_unverified() -> None:
    row = _held("artifact", [json.dumps({"memory_id": MEMORY, "quote": QUOTE})])
    settled = settle_labels([row]).by_stored("artifact", str(row["id"]))
    assert (settled.unverified, settled.reason) == (True, "missing_dependency")


def test_regenerating_without_the_citation_makes_a_readable_report() -> None:
    confidential = _memory(MEMORY, domain="health", sensitivity="confidential")
    cited = _held("artifact", [json.dumps({"memory_id": MEMORY, "quote": QUOTE})])
    regenerated = _held("artifact", [f"source:{SOURCE}"])
    source = _node("source", SOURCE)
    result = settle_labels([confidential, source, cited, regenerated])
    assert result.by_stored("artifact", str(cited["id"])).sensitivity == "confidential"
    assert (result.by_stored("artifact", str(regenerated["id"])).sensitivity, result.by_stored("artifact", str(regenerated["id"])).unverified) == ("internal", False)


# --- prose is not read --------------------------------------------------------------------------------------------------


def test_the_prose_of_a_text_key_is_not_read_for_a_citation() -> None:
    """A promoted copy of a report holds the whole report as its ``text``, with ``[memory:<id>]`` after each line. Those words
    are the report's, and the copy is derived from the report, so the markers in them are not inputs of the copy."""

    prose = f"- Fact: the kiln code is 4417. [memory:{MEMORY}]\n- Fact: fired on Mondays. [source:{SOURCE}]"
    row = _node(
        "memory", str(uuid4()), memory_key="promoted", canonical_text=prose, status="active",
        metadata_json={"source_artifact_id": str(uuid4()), "summary": prose, "description": json.dumps({"memory_id": MEMORY})},
        value={"kind": "promoted_artifact", "text": prose, "artifact_id": str(uuid4())},
    )
    found = dependencies_of("memory", row)
    assert {kind for kind, _row_id in found} == {"artifact"}
    assert not has_unreadable_citation(row["metadata_json"], row["value"])


def test_a_structure_under_a_text_key_is_read_like_any_other() -> None:
    row = _node(
        "memory", str(uuid4()), memory_key="structured", canonical_text="x", status="active",
        metadata_json={"candidate_kind": "memory_rollup", "summary": {"memory_id": MEMORY}, "description": [f"memory:{SOURCE}"]},
    )
    assert {("memory", MEMORY), ("memory", SOURCE)} <= dependencies_of("memory", row)


# --- a saved citation the parser cannot read ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        '{"memory_id": "%s", "quote": "cut off in the middle' % MEMORY,
        '[{"memory_id": "%s"' % MEMORY,
        '{"memory_id": "%s",}' % MEMORY,
        "{'memory_id': '%s'}" % MEMORY,
        "memory:{%s}#chunk-1" % MEMORY,
        "source:{%s}/page/2" % SOURCE,
        "alice://memories/{%s}#x" % MEMORY,
        json.dumps({"entry": '{"memory_id": "%s", "quote": "cut' % MEMORY}),
        "x" * 40 + " memory:{%s}#chunk-1" % MEMORY,
    ],
    ids=["cut", "cut-list", "trailing-comma", "single-quotes", "prefix-braces-fragment", "source-braces-path", "url-braces", "cut-inside-json", "in-a-sentence"],
)
@pytest.mark.parametrize("holder", ["artifact", "memory", "open_loop"])
def test_a_citation_the_parser_cannot_read_leaves_the_row_unverified(entry: str, holder: str) -> None:
    confidential = _memory(MEMORY, domain="health", sensitivity="confidential")
    source = _node("source", SOURCE, domain="health", sensitivity="confidential")
    row = _held(holder, [entry])
    assert has_unreadable_citation(row["metadata_json"])
    deps, problem = dependency_record(holder, row)
    assert problem == "unreadable_citation"
    settled = settle_labels([confidential, source, row]).by_stored(holder, str(row["id"]))
    assert (settled.unverified, settled.reason) == (True, "unreadable_citation")


@pytest.mark.parametrize(
    "entry",
    [
        "https://example.test/kiln",
        "Source: Wikipedia, the article on kilns and glazes, 2024 edition",
        "[1] Smith 2020, a long enough title with no id in it at all",
        "{braces and a long note with no id in it, written by hand}",
        "memory: notes about the kiln, kept for the owner of the vault",
        "{" + MEMORY + "}",
        f"memory:{MEMORY}",
        "x" * 40,
    ],
)
def test_text_that_cites_nothing_is_not_an_unreadable_citation(entry: str) -> None:
    assert not has_unreadable_citation({"source_refs": [entry]})


def test_a_bracket_outside_a_reference_is_not_a_citation() -> None:
    assert not has_unreadable_citation({"title": '{"memory_id": "%s", "cut' % MEMORY, "note": "memory:{%s}#x" % MEMORY})


def test_a_quote_is_text_and_names_nothing() -> None:
    quote = {"source_refs": [{"memory_id": MEMORY, "quote": '{"memory_id": "%s", "cut' % MEMORY}]}
    assert not has_unreadable_citation(quote)
    assert cited_inputs(quote) == {("memory", MEMORY)}


def test_json_nested_beyond_the_recursion_limit_is_unreadable_not_ignored() -> None:
    deep = "[" * 100_000 + json.dumps(MEMORY) + "]" * 100_000
    assert has_unreadable_citation({"source_refs": [deep]})


def test_an_unreadable_citation_is_reported_by_the_doctor_text() -> None:
    from alicebot_api.vnext_label_repair import UNREADABLE_CITATION_ADVICE, format_label_check

    text = format_label_check([], {"unreadable_citation": ["row-1"]})
    assert "unreadable_citation 1" in text and UNREADABLE_CITATION_ADVICE in text


# --- one parser ---------------------------------------------------------------------------------------------------------


def test_the_closure_and_the_reader_call_the_same_parser_functions() -> None:
    assert vnext_citations.cited_source_ids is vnext_source_fence.cited_source_ids
    assert vnext_citations.cited_memory_refs is vnext_source_fence.cited_memory_refs
    assert vnext_derived_labels.cited_inputs is vnext_citations.cited_inputs
    tree = ast.parse((ROOT / "apps/api/src/alicebot_api/vnext_derived_labels.py").read_text(encoding="utf-8"))
    called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert {"cited_inputs", "has_unreadable_citation", "cited_source_ids"} <= called


def test_every_repair_path_plans_with_the_one_planner() -> None:
    """The migrations, ``labels repair`` and the SQLite open pass call ``plan_label_repairs``; none has a reader of its own."""

    paths = {
        "apps/api/alembic/versions/20261005_0096_derived_label_floor.py",
        "apps/api/alembic/versions/20261011_0097_derived_label_citations.py",
        "apps/api/src/alicebot_api/cli/labels.py",
        "apps/api/src/alicebot_api/vnext_label_repair.py",
    }
    for path in paths:
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert "plan_label_repairs" in names, path
        assert not names & {"cited_source_ids", "cited_memory_refs", "_typed_refs", "_parse_source_token"}, path


@pytest.mark.parametrize("seed", range(40))
def test_random_refs_give_the_closure_the_reader_s_ids(seed: int) -> None:
    """Refs built at random from the shapes, with noise around them, name to the closure what they name to the reader."""

    rng = random.Random(seed)
    memory_ids = [str(uuid4()) for _ in range(3)]
    source_ids = [str(uuid4()) for _ in range(3)]
    entries: list[object] = []
    for _ in range(rng.randint(1, 6)):
        if rng.random() < 0.5:
            shape = rng.choice(list(memory_shapes(QUOTE).items()))
            ident = rng.choice(memory_ids)
        else:
            shape = rng.choice(list(source_shapes(QUOTE).items()))
            ident = rng.choice(source_ids)
        spell = rng.choice(list(SPELLINGS.values()))
        entry = shape[1](spell(ident))
        if isinstance(entry, str) and rng.random() < 0.3:
            entry = rng.choice(["see ", "copy of ", "", "ref: "]) + entry
        entries.append(entry)
        if rng.random() < 0.3:
            entries.append(rng.choice(["https://example.test/a", str(uuid4()), {"chunk_id": str(uuid4())}, f"chunk:{uuid4()}"]))
    rng.shuffle(entries)
    holder = rng.choice(["artifact", "memory", "open_loop"])
    baseline = dependencies_of(holder, _held(holder, []))
    row = _held(holder, entries)
    reader = {("source", item) for item in cited_source_ids(entries).named} | {
        ("memory", item) for item in cited_memory_refs(entries).named
    }
    _inputs, unreadable = record_citations(row)
    if unreadable:
        pytest.skip("a ref the reader cannot read is unverified, which the unreadable tests state")
    assert dependencies_of(holder, row) - baseline == reader


# --- the parsing key and the cache ---------------------------------------------------------------------------------------


def test_two_rows_that_differ_only_in_a_citation_do_not_share_a_parsing_key() -> None:
    """The guard shares one answer between rows with equal keys, so a citation under an unknown key must be in the key."""

    first = _node("artifact", str(uuid4()), artifact_type="memory_consolidation",
                  metadata_json={"workflow": "memory_consolidation", "evidence": json.dumps({"memory_id": MEMORY})})
    second = _node("artifact", str(uuid4()), artifact_type="memory_consolidation",
                   metadata_json={"workflow": "memory_consolidation", "evidence": json.dumps({"memory_id": str(uuid4())})})
    plain = _node("artifact", str(uuid4()), artifact_type="memory_consolidation",
                  metadata_json={"workflow": "memory_consolidation", "evidence": "nothing cited here"})
    keys = {dependency_syntax_key("artifact", row) for row in (first, second, plain)}
    assert len(keys) == 3


def test_rows_that_differ_only_in_incidental_text_still_share_a_parsing_key() -> None:
    def brief(trace: str) -> dict[str, object]:
        return _node("artifact", str(uuid4()), artifact_type="daily_brief",
                     metadata_json={"workflow": "daily_brief", "trace_id": trace, "note": "run " + trace,
                                    "source_refs": [f"source:{SOURCE}"]})

    assert dependency_syntax_key("artifact", brief(str(uuid4()))) == dependency_syntax_key("artifact", brief(str(uuid4())))


def test_a_decoded_row_and_a_raw_one_give_the_same_answer() -> None:
    entry = json.dumps({"memory_id": MEMORY.upper(), "quote": QUOTE})
    decoded = _held("artifact", [entry])
    raw = dict(decoded)
    raw["metadata_json"] = json.dumps(decoded["metadata_json"])
    assert record_citations(decoded) == record_citations(raw)
    assert dependencies_of("artifact", decoded) == dependencies_of("artifact", raw)


def test_an_id_or_ref_key_at_the_top_of_the_metadata_is_not_a_citation() -> None:
    """The reader reads ``id`` and ``ref`` as an id only inside an entry of a reference."""

    assert cited_inputs({"id": SOURCE, "ref": MEMORY}) == frozenset()
    assert cited_inputs([{"id": SOURCE}]) == {("source", SOURCE)}


def test_a_citation_names_the_canonical_id_whatever_the_spelling() -> None:
    for spelling in SPELLINGS.values():
        assert cited_inputs({"source_refs": [json.dumps({"memory_id": spelling(MEMORY)})]}) == {("memory", MEMORY)}
        assert UUID(next(iter(cited_inputs({"source_refs": [f"memory:{spelling(MEMORY)}"]}) or {("", MEMORY)}))[1]) == UUID(MEMORY)
