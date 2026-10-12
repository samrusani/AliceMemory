"""The derived-label closure resolves the ids a record may cite, found by the parser the saved-quote reader uses.

A consolidation report of v0.20.0 copies the ``source_refs`` of its members as their writers sent them, so a report holds a
citation of a memory as ``memory:<id>`` in any case, as an ``alice://`` URL, as JSON text that quotes the memory, with the id
in braces, without hyphens or in capitals, in a sentence, under ``origin``, in a Markdown link. The closure read the plain
``memory:<id>`` string only, and a report that cited a confidential memory any other way kept the label it was stored with.

The rule is resolve or ignore. Every id the saved-quote reader finds in a reference position (``source_refs``,
``provenance``, ``replacement_provenance``), the ones it names and the incidental ones, and every id it names anywhere else
in the record, is an input when it names a stored memory or source of the user, and is ignored when it names none. What the closure
always took from ``source_refs`` stays a hard input (a row that cites a row that is not stored is unverified, as it was). A word that
marks itself as a citation and holds an id the scan cannot take apart makes the row unverified only when that id names a
stored row; syntax alone never does.

Mutations (``scripts/derived_label_mutations.json`` replays them): in ``_derived_dependency_record`` drop the candidates; in
``settle_labels`` drop the resolution of the candidates or of the unreadable ids; in ``scan_citations`` stop reading the
reference positions with the reader, or the keys that name a citation; in ``dependency_syntax_key`` drop the scan.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from uuid import uuid4

import pytest

from alicebot_api import vnext_citations, vnext_derived_labels, vnext_source_fence
from alicebot_api.vnext_citations import clear_reading_cache, read_reference, scan_citations
from alicebot_api.vnext_derived_labels import (
    _SKIP_RECURSE,
    dependencies_of,
    dependency_label_signature,
    dependency_record,
    dependency_syntax_key,
    label_metadata_cache,
    record_scan,
    settle_labels,
    soft_inputs,
)
from alicebot_api.vnext_source_fence import cited_memory_refs, cited_source_ids
from tests.unit.legacy_citation_support import (
    SPELLINGS,
    candidate_memory,
    corpus_entries,
    memory_shapes,
    open_loop,
    positions,
    report,
    source_shapes,
)

ROOT = Path(__file__).resolve().parents[2]
USER = "user-1"
QUOTE = "Violet kiln code is 4417"
MEMORY = str(uuid4())
SOURCE = str(uuid4())
PUBLIC_MEMORY = str(uuid4())
PUBLIC_SOURCE = str(uuid4())
GHOST = str(uuid4())
# The source a loop came from. It is an input of the loop whatever the refs say.
LOOP_SOURCE = str(uuid4())
HOLDERS = ("artifact", "memory", "open_loop")


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


def _confidential_memory() -> dict[str, object]:
    return _memory(MEMORY, domain="health", sensitivity="confidential")


def _confidential_source() -> dict[str, object]:
    return _node("source", SOURCE, domain="health", sensitivity="confidential")


def _stored() -> list[dict[str, object]]:
    """The rows a record can cite: a confidential memory and source, a public memory and source, and the loop's source."""

    return [
        _confidential_memory(),
        _confidential_source(),
        _memory(PUBLIC_MEMORY, domain="project", sensitivity="public"),
        _node("source", PUBLIC_SOURCE, domain="project", sensitivity="public"),
        _node("source", LOOP_SOURCE, domain="project", sensitivity="public"),
    ]


def _held(kind: str, entries: list[object], position: str = "source_refs") -> dict[str, object]:
    """The row that holds ``entries``, in the shape the version before derived labels stored."""

    if kind == "artifact":
        return _node("artifact", str(uuid4()), **report(entries, position=position))
    if kind == "memory":
        return _node("memory", str(uuid4()), **candidate_memory(entries, position=position))
    return _node("open_loop", str(uuid4()), **open_loop(entries, source_id=LOOP_SOURCE))


def _settled(row: dict[str, object], stored: list[dict[str, object]] | None = None):
    nodes = [*(_stored() if stored is None else stored), row]
    return settle_labels(nodes).by_stored(str(row["kind"]), str(row["id"]))


def _looked_up(kind: str, row: dict[str, object]) -> set[str]:
    """Every id the closure looks up for ``row``: the hard inputs, the candidates and the unreadable ids."""

    hard = {row_id for _kind, row_id in dependencies_of(kind, row)}
    candidates, unreadable = soft_inputs(kind, row)
    return hard | {row_id for _kind, row_id in candidates} | {row_id for _kind, row_id in unreadable}


def _ids(citations) -> set[str]:
    return {row_id for _kind, row_id in citations.refs}


def _reader_finds(entry: object) -> set[str]:
    """Every id the saved-quote reader finds in one entry of ``source_refs``: the oracle."""

    return set(cited_source_ids([entry]).every) | set(cited_memory_refs([entry]).every)


def _cases():
    for kind, shapes, ident in (("memory", memory_shapes(QUOTE), MEMORY), ("source", source_shapes(QUOTE), SOURCE)):
        for shape, build in shapes.items():
            for spelling, spell in SPELLINGS.items():
                yield kind, shape, spelling, build(spell(ident)), ident


CASES = list(_cases())
# The shapes the closure always read as a hard input; a ghost id in them leaves the row unverified, as it always did.
HARD = ("prefix", "bare")


def _id(case: tuple) -> str:
    return "-".join(case[:3])


# --- the reader is the oracle ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=_id)
@pytest.mark.parametrize("holder", HOLDERS)
def test_the_closure_looks_up_every_id_the_saved_quote_reader_finds(case: tuple, holder: str) -> None:
    kind, shape, spelling, entry, ident = case
    row = _held(holder, [entry])
    found = _reader_finds(entry)
    assert found, "every shape of the table holds an id the reader finds"
    assert found <= _looked_up(holder, row), (shape, spelling)
    assert ident in found


@pytest.mark.parametrize("name,entry", sorted(corpus_entries(MEMORY, SOURCE, QUOTE).items()))
@pytest.mark.parametrize("holder", HOLDERS)
def test_the_closure_looks_up_the_incidental_ids_of_a_reference_too(name: str, entry: object, holder: str) -> None:
    row = _held(holder, [entry])
    assert _reader_finds(entry) <= _looked_up(holder, row), name


@pytest.mark.parametrize("position", sorted(positions("x")))
@pytest.mark.parametrize("holder", HOLDERS)
def test_every_position_a_reference_is_copied_to_is_read(holder: str, position: str) -> None:
    entry = f"copied from memory: {MEMORY} on Monday"
    row = _held(holder, [])
    row["metadata_json"] = {**row["metadata_json"], **positions(entry)[position]}  # type: ignore[arg-type]
    assert MEMORY in _looked_up(holder, row), position


# --- resolve or ignore ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=_id)
def test_a_report_is_raised_to_the_row_it_cites_in_every_shape(case: tuple) -> None:
    kind, shape, spelling, entry, ident = case
    row = _held("artifact", [entry])
    settled = _settled(row)
    assert (settled.unverified, settled.domain, settled.sensitivity) == (False, "health", "confidential"), (shape, spelling)


@pytest.mark.parametrize("name,entry", sorted(corpus_entries(MEMORY, SOURCE, QUOTE).items()))
def test_a_report_is_raised_to_the_row_it_cites_in_a_wording_no_marker_covers(name: str, entry: object) -> None:
    """An id in a sentence, under ``origin``, in a URL or a Markdown link names a row, and the report holds its words."""

    settled = _settled(_held("artifact", [entry]))
    if name.startswith("memory-bare-"):
        # A bare id was always read as a source: it names no stored source, so the row is unverified.
        assert (settled.unverified, settled.reason) == (True, "missing_dependency"), name
    elif name == "source-json-quote-has-id":
        # The reader reads the text of a quote as naming no source, and a memory only if the store holds one under the id.
        assert (settled.unverified, settled.sensitivity) == (False, "internal"), name
    else:
        assert (settled.unverified, settled.domain, settled.sensitivity) == (False, "health", "confidential"), name


@pytest.mark.parametrize("holder", HOLDERS)
def test_a_citation_of_a_row_that_is_not_stored_is_ignored(holder: str) -> None:
    for name, entry in corpus_entries(GHOST, GHOST, QUOTE).items():
        if isinstance(entry, str) and (
            re.fullmatch(r"(memory|source)-prefix-(canonical|capitals|no-hyphens|capitals-no-hyphens|braces|padded|space)", name)
            or "-bare-" in name or cited_source_ids([entry]).named
        ):
            continue  # a string that was always read as an input that must be stored: a marker and an id, a bare id, a source the reader names
        row = _held(holder, [entry])
        settled = _settled(row)
        assert (settled.unverified, settled.sensitivity) == (False, "internal"), name


def test_a_record_that_cites_only_where_no_producer_writes_is_judged_by_the_rows_it_names() -> None:
    """Refs under a key no producer writes are read like the rest: the ids that name a stored row are inputs, and the ids
    that name none are ignored."""

    def nested(entry: object) -> dict[str, object]:
        row = _held("artifact", [entry])
        meta = dict(row["metadata_json"])  # type: ignore[call-overload]
        meta.pop("source_refs", None)
        meta["x"] = {"y": {"source_refs": [entry]}}
        row["metadata_json"] = meta
        return row

    raised = _settled(nested(f"copied from memory: {MEMORY} on Monday"))
    assert (raised.unverified, raised.domain, raised.sensitivity) == (False, "health", "confidential")
    public = _settled(nested(f"[the note](alice://sources/{PUBLIC_SOURCE})"))
    assert (public.unverified, public.sensitivity) == (False, "internal")
    ghost = _settled(nested(f"copied from memory: {GHOST} on Monday"))
    assert (ghost.unverified, ghost.sensitivity) == (False, "internal")


def test_the_strings_a_producer_writes_stay_hard_inputs() -> None:
    """``memory:<id>`` and ``source:<id>`` are what every producer writes, and a row that cites a row that is not stored by
    one of them is unverified. A bare id names nothing it can be told to name, and is ignored."""

    for entry in (f"memory:{GHOST}", f"source:{GHOST}", f"memory: {GHOST}", f"memory:{GHOST.upper()}", "memory:{%s}" % GHOST):
        settled = _settled(_held("artifact", [entry]))
        assert (settled.unverified, settled.reason) == (True, "missing_dependency"), entry
    # A bare id is read as a source, as it always was.
    assert _settled(_held("artifact", [GHOST])).reason == "missing_dependency"


@pytest.mark.parametrize("holder", HOLDERS)
def test_a_citation_of_a_public_row_leaves_the_row_readable(holder: str) -> None:
    """A bracket, a trailing comma, a cut-off JSON text, a list repr or a Markdown link that names only public rows is
    syntax, and syntax alone never makes a row unverified."""

    ident = PUBLIC_SOURCE
    for entry in (
        f"['{ident}']",
        f"[{ident}]",
        f"[the note](alice://sources/{ident})",
        f"[the note](https://host.example/n/{ident})",
        '{"source_id": "%s",}' % ident,
        '{"source_id": "%s", "quote": "cut off in the middle' % ident,
        "{'source_id': '%s'}" % ident,
        "{%s}" % ident,
        f"source:'{ident}'",
        f"(source:{ident})",
        f"source:{ident}/page/2",
        f"source:{ident}#chunk-1",
        "[1] Smith 2024, a long enough title with no id in it at all",
        "{braces and a long note with no id in it, written by hand}",
    ):
        settled = _settled(_held(holder, [entry]))
        assert (settled.unverified, settled.sensitivity) == (False, "internal"), entry


def test_a_cut_off_json_text_that_names_a_confidential_memory_raises_the_report() -> None:
    entry = '{"memory_id": "%s", "quote": "cut off in the middle' % MEMORY
    settled = _settled(_held("artifact", [entry]))
    assert (settled.unverified, settled.domain, settled.sensitivity) == (False, "health", "confidential")


def test_a_repeated_key_in_json_text_names_every_value() -> None:
    """The reader keeps every value of a key the text writes twice, so a decoy beside the real id is read, and ignored."""

    entry = '{"memory_id": "%s", "memory_id": "%s"}' % (GHOST, MEMORY)
    row = _held("artifact", [entry])
    assert {GHOST, MEMORY} <= _looked_up("artifact", row)
    settled = _settled(row)
    assert (settled.unverified, settled.domain, settled.sensitivity) == (False, "health", "confidential")


@pytest.mark.parametrize("key", ["provenance", "replacement_provenance"])
@pytest.mark.parametrize("entry", [
    f"source:{SOURCE}", [f"source:{SOURCE}"], [SOURCE], SOURCE, {"source_id": SOURCE, "quote": QUOTE}, [{"origin": SOURCE}],
    f"memory:{MEMORY}", [MEMORY], {"memory_id": MEMORY}, json.dumps({"memory_id": MEMORY}), f"see {MEMORY}",
], ids=lambda value: value if isinstance(value, str) else json.dumps(value)[:40])
def test_provenance_and_replacement_provenance_are_reference_positions(key: str, entry: object) -> None:
    row = _node("memory", str(uuid4()), **candidate_memory([]))
    row["metadata_json"] = {**row["metadata_json"], key: entry}  # type: ignore[arg-type]
    settled = _settled(row)
    assert (settled.unverified, settled.sensitivity) == (False, "confidential"), (key, entry)


# --- a word the scan cannot take apart --------------------------------------------------------------------------------------


def _odd(ident: str) -> str:
    """The id with its hyphens in places ``uuid.UUID`` accepts and the reader's pattern does not."""

    bare = ident.replace("-", "")
    return "-".join((bare[:5], bare[5:11], bare[11:16], bare[16:24], bare[24:]))


@pytest.mark.parametrize("holder", HOLDERS)
def test_a_marked_word_with_an_id_the_scan_cannot_take_apart_is_unverified_when_the_id_names_a_stored_row(holder: str) -> None:
    for marker, target in (("memory:", MEMORY), ("alice://memories/", MEMORY), ("source:", SOURCE), ("alice://sources/", SOURCE)):
        entry = f"{marker}{_odd(target)}#chunk-1"
        row = _held(holder, [entry])
        assert target in {row_id for _kind, row_id in (*soft_inputs(holder, row)[0], *soft_inputs(holder, row)[1])}, entry
        settled = _settled(row)
        assert (settled.unverified, settled.reason) == (True, "unreadable_citation"), entry
    # The marker says which kind it is: a memory marker with the id of a source names no memory, and nothing is held back.
    settled = _settled(_held(holder, [f"memory:{_odd(SOURCE)}#chunk-1"]))
    assert (settled.unverified, settled.sensitivity) == (False, "internal")


@pytest.mark.parametrize("holder", HOLDERS)
def test_a_marked_word_with_an_id_the_scan_cannot_take_apart_is_ignored_when_it_names_nothing(holder: str) -> None:
    for marker in ("memory:", "source:", "alice://memories/", "alice://sources/"):
        settled = _settled(_held(holder, [f"{marker}{_odd(GHOST)}#chunk-1"]))
        assert (settled.unverified, settled.sensitivity) == (False, "internal"), marker


def test_a_marked_word_with_an_id_the_scan_cannot_take_apart_names_a_public_row_and_is_still_unverified() -> None:
    """The scan cannot say what the word cites, so it is held back until the id is known to name nothing."""

    settled = _settled(_held("artifact", [f"source:{_odd(PUBLIC_SOURCE)}#chunk-1"]))
    assert (settled.unverified, settled.reason) == (True, "unreadable_citation")


# --- anywhere else in the record ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["agent_run_id", "task_id", "agent_type", "output_type", "label", "run"])
@pytest.mark.parametrize("make", [
    lambda ident: f"memory:{ident}",
    lambda ident: f"alice://sources/{ident}",
    lambda ident: json.dumps({"memory_id": ident}),
    lambda ident: f"[{ident}]",
], ids=["prefix", "url", "json", "bracket"])
def test_an_id_that_names_no_stored_row_in_an_ordinary_field_changes_nothing(field: str, make) -> None:
    """A writer who puts a random id where an agent run id goes cannot make the report unverified."""

    row = _held("artifact", [f"source:{PUBLIC_SOURCE}"])
    row["metadata_json"] = {**row["metadata_json"], field: make(GHOST)}  # type: ignore[arg-type]
    settled = _settled(row)
    assert (settled.unverified, settled.sensitivity) == (False, "internal")


def test_an_id_a_marker_names_in_an_ordinary_field_is_an_input_when_it_names_a_stored_row() -> None:
    for text in (f"memory:{MEMORY}", f"alice://memories/{MEMORY.upper()}", json.dumps({"memory_id": MEMORY})):
        row = _held("artifact", [f"source:{PUBLIC_SOURCE}"])
        row["metadata_json"] = {**row["metadata_json"], "agent_run_id": text}  # type: ignore[arg-type]
        assert _settled(row).sensitivity == "confidential", text


@pytest.mark.parametrize("key", ["notes", "evidence", "extra"])
@pytest.mark.parametrize("make", [
    lambda: json.dumps({"memory_id": MEMORY, "quote": QUOTE}),
    lambda: "  \n" + json.dumps({"memory_id": MEMORY, "quote": QUOTE}),
    lambda: [json.dumps({"memory_id": MEMORY})],
    lambda: {"inner": json.dumps({"memory_id": MEMORY})},
    lambda: f"memory:{MEMORY}",
    lambda: f"alice://memories/{MEMORY}",
], ids=["json", "padded-json", "json-list", "json-nested", "marker", "url"])
def test_json_text_and_a_marker_under_any_key_name_the_ids_they_name(key: str, make) -> None:
    row = _held("artifact", [f"source:{PUBLIC_SOURCE}"])
    row["metadata_json"] = {**row["metadata_json"], key: make()}  # type: ignore[arg-type]
    assert MEMORY in _looked_up("artifact", row)
    assert _settled(row).sensitivity == "confidential"


@pytest.mark.parametrize("position", ["provenance", "replacement_provenance"])
def test_a_memory_word_is_a_memory_and_never_a_source(position: str) -> None:
    """A source stored under the id of a ``memory:<id>`` word is not what the word cites: the reader looks the two kinds up
    apart, and reads that word as naming a memory only."""

    clash = str(uuid4())
    assert not cited_source_ids([f"memory:{clash}"]).every
    stored = [_node("source", clash, domain="health", sensitivity="confidential"), *_stored()]
    row = _held("artifact", [])
    row["metadata_json"] = {**row["metadata_json"], position: [f"memory:{clash}"]}  # type: ignore[arg-type]
    settled = _settled(row, stored)
    assert (settled.unverified, settled.sensitivity) == (False, "internal")
    # The words of a source: or a bare id are read as a source, and the reader also reads them as an incidental memory.
    row["metadata_json"] = {**row["metadata_json"], position: [f"source:{clash}"]}  # type: ignore[arg-type]
    assert _settled(row, stored).sensitivity == "confidential"


def test_a_bare_id_in_an_ordinary_field_is_not_an_input() -> None:
    """A trace id, a run id or the id of an output is not a citation, and the report that holds one is not raised."""

    row = _held("artifact", [f"source:{PUBLIC_SOURCE}"])
    row["metadata_json"] = {**row["metadata_json"], "trace_id": MEMORY, "candidate_memory_ids": [MEMORY], "scheduler_run_id": SOURCE}  # type: ignore[arg-type]
    settled = _settled(row)
    assert (settled.unverified, settled.sensitivity) == (False, "internal")


def test_the_prose_of_a_text_key_is_not_read_for_a_citation() -> None:
    """A promoted copy of a report holds the whole report as its ``text``, with ``[memory:<id>]`` after each line. Those words
    are the report's, and the copy is derived from the report, so the markers in them are not inputs of the copy."""

    prose = f"- Fact: the kiln code is 4417. [memory:{MEMORY}]\n- Fact: fired on Mondays. [source:{SOURCE}]"
    row = _node(
        "memory", str(uuid4()), memory_key="promoted", canonical_text=prose, status="active",
        metadata_json={"source_artifact_id": str(uuid4()), "summary": prose, "description": json.dumps({"memory_id": MEMORY})},
        value={"kind": "promoted_artifact", "text": prose, "artifact_id": str(uuid4())},
    )
    assert {kind for kind, _row_id in dependencies_of("memory", row)} == {"artifact"}
    assert soft_inputs("memory", row) == (frozenset(), frozenset())


def test_a_structure_under_a_text_key_is_read_like_any_other() -> None:
    row = _node(
        "memory", str(uuid4()), memory_key="structured", canonical_text="x", status="active",
        metadata_json={"candidate_kind": "memory_rollup", "summary": {"memory_id": MEMORY}, "description": [f"memory:{SOURCE}"]},
    )
    assert {MEMORY, SOURCE} <= _looked_up("memory", row)


# --- redaction and the rest of the kernel ------------------------------------------------------------------------------------


@pytest.mark.parametrize("holder", HOLDERS)
def test_a_row_that_cites_a_redacted_memory_is_unverified(holder: str) -> None:
    redacted = _memory(MEMORY, metadata_json={"redacted": True}, canonical_text="[redacted]")
    for entry in (json.dumps({"memory_id": MEMORY, "quote": QUOTE}), f"copied from memory: {MEMORY}", {"origin": MEMORY}):
        row = _held(holder, [entry])
        settled = settle_labels([redacted, _node("source", LOOP_SOURCE), row]).by_stored(holder, str(row["id"]))
        assert (settled.unverified, settled.reason) == (True, "input_redacted"), entry


def test_regenerating_without_the_citation_makes_a_readable_report() -> None:
    cited = _held("artifact", [json.dumps({"memory_id": MEMORY, "quote": QUOTE})])
    regenerated = _held("artifact", [f"source:{PUBLIC_SOURCE}"])
    result = settle_labels([*_stored(), cited, regenerated])
    assert result.by_stored("artifact", str(cited["id"])).sensitivity == "confidential"
    settled = result.by_stored("artifact", str(regenerated["id"]))
    assert (settled.sensitivity, settled.unverified) == ("internal", False)


def test_a_row_does_not_resolve_to_itself() -> None:
    row = _node("memory", MEMORY, **candidate_memory([f"copied from memory: {MEMORY}"]))
    settled = settle_labels([row]).by_stored("memory", MEMORY)
    assert (settled.unverified, settled.reason) == (False, None)
    assert soft_inputs("memory", row) == (frozenset(), frozenset()), "its own id is not a row to look up"


def test_a_candidate_resolves_only_within_the_tenant() -> None:
    other = _memory(MEMORY, domain="health", sensitivity="confidential")
    other["user_id"] = "user-2"
    settled = _settled(_held("artifact", [f"copied from memory: {MEMORY}"]), [other])
    assert (settled.unverified, settled.sensitivity) == (False, "internal")


# --- one parser, read once ---------------------------------------------------------------------------------------------------


def test_the_closure_and_the_reader_call_the_same_parser_functions() -> None:
    assert vnext_citations.cited_source_ids is vnext_source_fence.cited_source_ids
    assert vnext_citations.cited_memory_refs is vnext_source_fence.cited_memory_refs
    assert vnext_derived_labels.scan_citations is vnext_citations.scan_citations
    tree = ast.parse((ROOT / "apps/api/src/alicebot_api/vnext_derived_labels.py").read_text(encoding="utf-8"))
    called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert {"scan_citations", "cited_source_ids"} <= called


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
        assert not names & {"cited_source_ids", "cited_memory_refs", "scan_citations", "_typed_refs", "_parse_source_token"}, path


@pytest.fixture(autouse=True)
def _fresh_readings() -> None:
    """What the reader found in a string is kept for the process; every test starts without it."""

    clear_reading_cache()


class _Spy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.scans = 0
        self.reads = 0
        scan, source, memory = vnext_derived_labels.scan_citations, vnext_citations.cited_source_ids, vnext_citations.cited_memory_refs

        def scanned(*args, **kwargs):
            self.scans += 1
            return scan(*args, **kwargs)

        def read_source(value):
            self.reads += 1
            return source(value)

        def read_memory(value):
            self.reads += 1
            return memory(value)

        monkeypatch.setattr(vnext_derived_labels, "scan_citations", scanned)
        monkeypatch.setattr(vnext_citations, "cited_source_ids", read_source)
        monkeypatch.setattr(vnext_citations, "cited_memory_refs", read_memory)


def _every_view(kind: str, row: dict[str, object]) -> None:
    dependency_syntax_key(kind, row)
    dependency_label_signature(kind, row)
    dependency_record(kind, row)
    soft_inputs(kind, row)
    dependencies_of(kind, row)


def test_a_record_is_scanned_once_per_request_however_many_times_it_is_asked_about(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(monkeypatch)
    row = _held("artifact", [f"memory:{PUBLIC_MEMORY}", f"source:{PUBLIC_SOURCE}", "https://example.test/kiln"])
    with label_metadata_cache({}):
        for _ in range(3):
            _every_view("artifact", row)
        # Another projection of the same stored row (the list door's copy and the guard's copy) is the same record.
        again = {**row, "metadata_json": json.loads(json.dumps(row["metadata_json"])), "sensitivity": "regulated"}
        _every_view("artifact", again)
    assert spy.scans == 1
    assert spy.reads == 0, "a record in the one spelling every producer writes does not go to the reader"


def test_a_projection_with_a_different_record_is_scanned_again(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(monkeypatch)
    row = _held("artifact", [f"source:{PUBLIC_SOURCE}"])
    other = {**row, "metadata_json": {**row["metadata_json"], "source_refs": [f"copied from memory: {MEMORY} today"]}}  # type: ignore[arg-type]
    with label_metadata_cache({}):
        assert _ids(record_scan(row)) == {PUBLIC_SOURCE}
        assert MEMORY in _ids(record_scan(other))
    assert spy.scans == 2


def test_each_distinct_string_that_needs_the_reader_goes_to_it_once(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(monkeypatch)
    entry = json.dumps({"memory_id": MEMORY, "quote": QUOTE})
    row = _held("artifact", [entry, entry, f"memory:{PUBLIC_MEMORY}"])
    row["metadata_json"] = {**row["metadata_json"], **positions(entry)["proposal"]}  # type: ignore[arg-type]
    with label_metadata_cache({}):
        _every_view("artifact", row)
    assert spy.reads == 2, "one call of each half of the reader, for the one string"


def test_a_string_the_reader_has_read_is_not_read_again_by_the_next_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A report is read at every request and its strings do not change, so what the reader found is kept per string."""

    spy = _Spy(monkeypatch)
    entry = json.dumps({"memory_id": MEMORY, "quote": QUOTE})
    first = _held("artifact", [entry])
    second = _held("artifact", [entry, f"memory:{PUBLIC_MEMORY}"])  # another row, the same string
    with label_metadata_cache({}):
        _every_view("artifact", first)
    assert spy.reads == 2
    with label_metadata_cache({}):
        _every_view("artifact", second)
    assert spy.reads == 2, "the string was read by the first request"
    assert MEMORY in _looked_up("artifact", second)


def test_what_the_reader_finds_in_a_string_does_not_depend_on_what_the_string_sits_next_to() -> None:
    """The kept reading is a function of the string, so it must equal the reading of the list the string is part of."""

    entries = [value for value in corpus_entries(MEMORY, SOURCE, QUOTE).values() if isinstance(value, str)]
    for start in range(0, len(entries), 7):
        group = entries[start : start + 7]
        wanted_source = set(cited_source_ids(group).every)
        wanted_memory = set(cited_memory_refs(group).every)
        got = [read_reference(text) for text in group]
        assert {row_id for reading in got for kind, row_id in reading.refs if kind == "source"} == wanted_source
        assert {row_id for reading in got for kind, row_id in reading.refs if kind == "memory"} == wanted_memory
        assert {row_id for reading in got for row_id in reading.named_sources} == set(cited_source_ids(group).named)


def test_a_string_too_short_to_hold_an_id_never_goes_to_the_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(monkeypatch)
    assert read_reference("memory:abc") == vnext_citations.Reading(frozenset(), frozenset(), frozenset())
    assert spy.reads == 0


def test_a_record_with_nothing_that_can_hold_a_citation_never_goes_to_the_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(monkeypatch)
    row = _held("artifact", [])
    row["metadata_json"] = {**row["metadata_json"], "trace_id": str(uuid4()), "note": "x" * 80, "scheduler_run_id": str(uuid4()),  # type: ignore[arg-type]
                            "summary": f"memory:{MEMORY}"}
    with label_metadata_cache({}):
        assert _ids(record_scan(row)) == set()
    assert spy.reads == 0


# --- the parsing key and the cache --------------------------------------------------------------------------------------------


def test_two_rows_that_cite_different_ids_do_not_share_a_parsing_key() -> None:
    """The guard shares one answer between rows with equal keys, so the ids a record may cite must be in the key."""

    def report_citing(text: str) -> dict[str, object]:
        # Under a key the parsing key does not carry, so only the scan tells the rows apart.
        return _node("artifact", str(uuid4()), artifact_type="memory_consolidation",
                     metadata_json={"workflow": "memory_consolidation", "evidence": text})

    keys = {dependency_syntax_key("artifact", report_citing(text)) for text in (
        json.dumps({"memory_id": MEMORY}), json.dumps({"memory_id": str(uuid4())}), f"memory:{SOURCE}", f"alice://memories/{PUBLIC_MEMORY}", "nothing cited here")}
    assert len(keys) == 5


def test_rows_that_differ_only_in_incidental_text_still_share_a_parsing_key() -> None:
    def brief(trace: str) -> dict[str, object]:
        return _node("artifact", str(uuid4()), artifact_type="daily_brief",
                     metadata_json={"workflow": "daily_brief", "trace_id": trace, "note": "run " + trace,
                                    "source_refs": [f"source:{PUBLIC_SOURCE}"]})

    assert dependency_syntax_key("artifact", brief(str(uuid4()))) == dependency_syntax_key("artifact", brief(str(uuid4())))


def test_the_signature_carries_the_ids_a_row_may_cite() -> None:
    first = _held("artifact", [f"see {MEMORY}"])
    second = _held("artifact", [f"see {SOURCE}"])
    assert dependency_label_signature("artifact", first) != dependency_label_signature("artifact", second)


def test_a_decoded_row_and_a_raw_one_give_the_same_answer() -> None:
    entry = json.dumps({"memory_id": MEMORY.upper(), "quote": QUOTE})
    decoded = _held("artifact", [entry])
    raw = dict(decoded)
    raw["metadata_json"] = json.dumps(decoded["metadata_json"])
    assert record_scan(decoded) == record_scan(raw)
    assert soft_inputs("artifact", decoded) == soft_inputs("artifact", raw)


def test_an_id_or_ref_key_at_the_top_of_the_metadata_is_not_a_citation() -> None:
    """The reader reads ``id`` and ``ref`` as an id only inside an entry of a reference."""

    assert _ids(scan_citations(({"id": SOURCE, "ref": MEMORY},))) == set()
    assert _ids(scan_citations(({"source_refs": [{"id": SOURCE}]},))) == {SOURCE}


def test_the_scan_names_the_canonical_id_whatever_the_spelling() -> None:
    for spelling in SPELLINGS.values():
        assert _ids(scan_citations(({"source_refs": [json.dumps({"memory_id": spelling(MEMORY)})]},))) == {MEMORY}
        assert _ids(scan_citations(({"memory_ids": [spelling(MEMORY)]},))) == {MEMORY}


def test_the_scan_leaves_out_the_prose_it_is_told_about() -> None:
    record = {"summary": f"memory:{MEMORY}", "title": json.dumps({"memory_id": SOURCE}), "note": f"memory:{PUBLIC_MEMORY}"}
    assert _ids(scan_citations((record,), prose=_SKIP_RECURSE)) == {PUBLIC_MEMORY}
    assert _ids(scan_citations((record,))) == {MEMORY, SOURCE, PUBLIC_MEMORY}
