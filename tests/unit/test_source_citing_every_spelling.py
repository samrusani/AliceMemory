"""A memory cites a source in every spelling the shared reference reader accepts.

The review of #551 and #552 reproduced this on main. ``citing_memories`` found a memory only when one of its
text nodes was the source id or ``source:<id>`` as written, so a proposal that cited the source as an upper case id,
an id without hyphens, a JSON ref or ``source:<id>#chunk-0`` was missed. After ``import-markdown --supersede`` the old
source was retired, but that proposal stayed pending, kept its stale text, stayed in the review queue and was left out of
``memories_citing_replaced``. ``sources delete`` and ``sources prune`` use the same function, so they reported success
while the proposal kept the deleted text.

Now a memory cites a source exactly when the shared reference reader says so
(``vnext_source_fence.memory_cited_source_ids``), after a SQL narrowing that never drops a memory the reader would accept.

Every test below goes through the real door the probe used (an agent proposal through the MCP handler) or plants the same
row shape in the vault, then runs the real importer or the real ``sources`` commands.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from uuid import uuid4

import pytest

from alicebot_api.mcp import memories as mcp_memories
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vnext_source_fence import cited_source_ids
from alicebot_api.vnext_stores.sqlite import source_retirement
from alicebot_api.vnext_stores.sqlite.source_retirement import citing_memories
from tests.unit.test_importer_per_file_savepoint import USER_ID, _folder, _vault
from tests.unit.test_saved_quote_ref_reading import _zero_led_id, _zero_led_spellings
from tests.unit.test_source_commands import command
from tests.unit.test_source_review_fixes import _read
from tests.unit.test_source_supersede import run_import

TEXT = "Orveth Zaltrix keeps the vespnork record."
WORD = "vespnork"

# The six shapes of the reviewer's probe, then the three more the owner commands must find. Each builds the ref string a
# proposal carries in ``source_refs``. The first two are the controls that passed on main.
PROBE_SHAPES = {
    "bare id": lambda sid: sid,
    "source prefix": lambda sid: "source:" + sid,
    "upper case": lambda sid: sid.upper(),
    "no hyphens": lambda sid: sid.replace("-", ""),
    "JSON ref": lambda sid: json.dumps({"source_id": sid}),
    "chunk suffix": lambda sid: "source:" + sid + "#chunk-0",
}
MORE_SHAPES = {
    "braces": lambda sid: "{" + sid + "}",
    "urn uuid": lambda sid: "urn:uuid:" + sid,
    "alice url": lambda sid: "alice://sources/" + sid,
}
ALL_SHAPES = {**PROBE_SHAPES, **MORE_SHAPES}
# Refs that are not text: the nested keys the reader reads, planted as the objects they are.
NESTED_SHAPES = {
    "nested ref keys": lambda sid: {"sources": [{"ref": sid}]},
    "selected ids": lambda sid: {"selected_source_ids": [sid.upper()]},
}


def seeded(tmp_path):
    db = _vault(tmp_path)
    folder = _folder(tmp_path, note="The older amber statement.")
    return db, folder, run_import(db, folder).source_ids[0]


def propose(db, monkeypatch, ref, *, text=TEXT):
    """A proposal through the real MCP door, as the probe made it. A ref that is not text cannot go through that door (it
    takes a list of strings), so it is written as the same row the door writes."""

    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        if not isinstance(ref, str):
            return plant(store, [ref], text=text)

        @contextmanager
        def fake_store_context(_context):
            yield store

        monkeypatch.setattr(mcp_memories, "_vnext_store_context", fake_store_context)
        payload = mcp_memories._handle_alice_vnext_propose_memory(
            MCPRuntimeContext(database_url="postgresql://localhost/alicebot", user_id=uuid4()),
            {"agent_id": "probe_agent", "permission_profile": "memory_proposal_agent", "canonical_text": text, "title": "Amber record",
             "domain": "professional", "sensitivity": "internal", "source_refs": [ref]},
        )
    assert payload["proposal"]["status"] == "candidate"
    return str(payload["proposal"]["id"])


def plant(store, refs, *, status="candidate", text=TEXT):
    memory = store.create_memory({
        "memory_key": "plant." + str(uuid4()), "memory_type": "semantic", "status": status,
        "canonical_text": text, "value": {"text": text, "source_refs": refs}, "metadata_json": {"source_refs": refs},
    })
    return str(memory["id"])


def status_of(db, memory_id):
    return _read(db, "SELECT status FROM memories WHERE id=?", (memory_id,))[0][0]


def stored_words(db):
    with sqlite_user_connection(db, USER_ID) as conn:
        return json.dumps([
            [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
            for table in ("memories", "memory_revisions")
        ], default=str).lower()


# -- replacement -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("shape", [*ALL_SHAPES, *NESTED_SHAPES])
def test_replace_rejects_and_lists_a_proposal_that_cites_the_source_in_any_spelling(tmp_path, monkeypatch, shape):
    """The probe. A proposal cites the old source in one spelling, the file is edited, and the importer replaces the
    source: the proposal is rejected and listed in ``memories_citing_replaced``. A proposal that cites an unrelated id
    is left alone, so a pass is not a blanket rejection. On main the first two shapes pass and the next four, with
    braces, ``urn:uuid:``, ``alice://sources/`` and the selected ids, fail.

    Mutations, each alone, in ``source_retirement.py`` (the saved original is copied back after each, and the file is
    compared): skip the text half of ``citing_memories`` (``if canonical is not None:`` made ``if False:``: every case
    fails); drop ``lower(`` from the searched text (the upper case and selected ids cases fail); stop removing hyphens
    from it (every case with hyphens fails); search for the hyphenated id in the raw text, so the compact form is gone
    (the cases without hyphens fail); make ``retire_dependents`` read no memories (every case here and in the delete
    and prune tests fails).
    """

    db, folder, sid = seeded(tmp_path)
    build = {**ALL_SHAPES, **NESTED_SHAPES}[shape]
    cited = propose(db, monkeypatch, build(sid))
    unrelated = propose(db, monkeypatch, build(str(uuid4())), text="The bronze statement.")
    (folder / "note.md").write_text("The current copper statement.")
    result = run_import(db, folder, supersede=True)
    assert result.failed_count == 0 and result.superseded
    assert status_of(db, cited) == "rejected"
    assert cited in result.memories_citing_replaced
    assert status_of(db, unrelated) == "candidate"
    assert unrelated not in result.memories_citing_replaced


# -- delete and prune ------------------------------------------------------------------------------------------------


def assert_gone(db, memory_id):
    with sqlite_user_connection(db, USER_ID) as conn:
        assert SQLiteVNextStore(conn, USER_ID).memory_redaction_bundle_is_exact(memory_id, [])
    assert WORD not in stored_words(db)


@pytest.mark.parametrize("shape", [*ALL_SHAPES, *NESTED_SHAPES])
def test_delete_previews_scrubs_and_lists_memories_that_cite_the_source_in_any_spelling(tmp_path, monkeypatch, capsys, shape):
    """``sources delete`` for a source cited in one spelling: the preview and the receipt both count the pending
    candidate, the candidate is scrubbed (its bundle is exact and its words are gone from the memories and revisions),
    and an active memory that cites the source in the same spelling is listed in both and keeps its text.

    Mutations: the ones of the replacement test, and ``_preview`` in ``source_commands.py`` reading only the
    provenance links (``citing_memories`` swapped for a query on ``provenance_links``: the preview assertions fail, and
    the receipt, which scrubs through ``retire_dependents``, is not what fails).
    """

    db, _folder_path, sid = seeded(tmp_path)
    build = {**ALL_SHAPES, **NESTED_SHAPES}[shape]
    candidate = propose(db, monkeypatch, build(sid))
    with sqlite_user_connection(db, USER_ID) as conn:
        kept = plant(SQLiteVNextStore(conn, USER_ID), [build(sid)], status="active", text="The amber record is active.")
    assert command(db, "delete", sid) == 2
    preview = json.loads(capsys.readouterr().out)["would_delete"][0]
    assert command(db, "delete", sid, "--yes") == 0
    receipt = json.loads(capsys.readouterr().out)["deleted"][0]
    for counts in (preview, receipt):
        assert counts["candidate_memories"] == 1
        assert counts["memories_citing_replaced"] == [kept]
    assert_gone(db, candidate)
    assert _read(db, "SELECT canonical_text FROM memories WHERE id=?", (kept,)) == [("The amber record is active.",)]


@pytest.mark.parametrize("shape", [*ALL_SHAPES, *NESTED_SHAPES])
def test_prune_previews_scrubs_and_lists_memories_that_cite_a_replaced_source_in_any_spelling(tmp_path, monkeypatch, capsys, shape):
    """``sources prune --superseded``: the memories are written after the source was replaced, so the prune is what
    finds them and not the replacement. Preview and receipt count the candidate and list the active memory, and the
    candidate is scrubbed.

    Mutations: the ones of the replacement and delete tests.
    """

    db, folder, sid = seeded(tmp_path)
    (folder / "note.md").write_text("The current copper statement.")
    assert run_import(db, folder, supersede=True).superseded
    build = {**ALL_SHAPES, **NESTED_SHAPES}[shape]
    candidate = propose(db, monkeypatch, build(sid))
    with sqlite_user_connection(db, USER_ID) as conn:
        kept = plant(SQLiteVNextStore(conn, USER_ID), [build(sid)], status="active", text="The amber record is active.")
    assert command(db, "prune", "--superseded") == 2
    preview = json.loads(capsys.readouterr().out)["would_delete"][0]
    assert command(db, "prune", "--superseded", "--yes") == 0
    receipt = json.loads(capsys.readouterr().out)["deleted"][0]
    for counts in (preview, receipt):
        assert counts["id"] == sid and counts["candidate_memories"] == 1
        assert counts["memories_citing_replaced"] == [kept]
    assert_gone(db, candidate)


# -- what the lookup reads --------------------------------------------------------------------------------------------


def memory_store():
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    ensure_sqlite_user(conn, USER_ID, "citing@example.com")
    return SQLiteVNextStore(conn, USER_ID)


def insert_memory(store, *, metadata=None, value=None, events=None):
    memory_id = str(uuid4())
    store.conn.execute(
        "INSERT INTO memories (id, user_id, memory_key, value, status, source_event_ids, metadata_json, canonical_text) "
        "VALUES (?, ?, ?, ?, 'candidate', ?, ?, 'A record.')",
        (memory_id, USER_ID, "key." + memory_id, json.dumps(value or {"text": "A record."}),
         json.dumps(events or []), json.dumps(metadata or {})))
    return memory_id


def found(store, source_id):
    return {str(row["id"]) for row in citing_memories(store, source_id)}


def test_every_stored_field_the_product_keeps_a_source_in_is_read():
    """A memory names its source in the reference fields the saved-quote reader judges, in the places other code of
    the product reads (``source_id``, ``source_ids`` and ``selected_source_ids`` at the top of the metadata, as the
    retrieval and review routes read them), and in free text under another key, which the reader counts as a citation
    of a stored source. Each placement is found in a spelling that main did not find.

    Mutations: take the metadata out of the searched text (``lower(coalesce(m.metadata_json, '')`` made ``lower('')``:
    the metadata placements fail); take the value out of it (``coalesce(m.value, '')`` made ``''``: the value
    placements fail); read only ``_source_ids_named_by_memory_copies`` in ``memory_cited_source_ids`` (the top-level
    keys fail); answer with ``cited.named`` and not ``cited.every`` (the free text placement fails).
    """

    store = memory_store()
    sid = str(uuid4())
    spelled = sid.upper()
    placements = {
        "metadata source_refs": {"metadata": {"source_refs": [spelled]}},
        "value source_refs": {"value": {"text": "A record.", "source_refs": [spelled]}},
        "agentic source_refs": {"metadata": {"agentic_memory": {"source_refs": [spelled]}}},
        "provenance": {"metadata": {"provenance": {"source_id": spelled}}},
        "replacement provenance": {"metadata": {"replacement_provenance": {"source_id": spelled}}},
        "top-level source_id": {"metadata": {"source_id": spelled}},
        "top-level source_ids": {"metadata": {"source_ids": [spelled]}},
        "top-level selected_source_ids": {"metadata": {"selected_source_ids": [spelled]}},
        "value source_ids": {"value": {"text": "A record.", "source_ids": [spelled]}},
        "a sentence under another key": {"metadata": {"rationale": "Derived from " + spelled + " last week."}},
    }
    ids = {label: insert_memory(store, **row) for label, row in placements.items()}
    assert found(store, sid) == set(ids.values())


def test_text_that_only_looks_like_a_citation_is_not_one():
    """Not a citation: another source's id, the id inside a quote or an excerpt (at any depth, in text or in a JSON
    text), a ``memory:`` ref, and a longer run of hex digits that holds the digits. The SQL narrowing keeps all but
    the first, so the reader is what rejects them. (An id in a sentence under another key is a citation: see the test
    above.)

    Mutations: make the check ``canonical in memory_cited_source_ids(row)`` always true (the memory ref, the longer run
    and the quote rows are found); read quotes and excerpts as references (``_TEXT_KEYS`` made empty: the quote and
    excerpt rows are found); skip the JSON decode of the reader (``_json_container`` returning ``None``: the JSON
    quote row is found).
    """

    store = memory_store()
    sid = str(uuid4())
    digits = sid.replace("-", "")
    rows = {
        "another source": {"metadata": {"source_refs": [str(uuid4())]}},
        "provenance quote": {"metadata": {"provenance": {"quote": "see " + sid, "source_id": str(uuid4())}}},
        "excerpt": {"metadata": {"agentic_memory": {"conversation_excerpt": sid, "source_refs": []}}},
        "value quote": {"value": {"text": "A record.", "quote": sid}},
        "JSON quote": {"metadata": {"source_refs": [json.dumps({"quote": sid, "source_id": str(uuid4())})]}},
        "memory ref": {"metadata": {"source_refs": ["memory:" + sid]}},
        "longer hex run": {"metadata": {"source_refs": ["ab" + digits]}},
    }
    inserted = {label: insert_memory(store, **row) for label, row in rows.items()}
    wrongly_found = found(store, sid)
    assert {label for label, memory_id in inserted.items() if memory_id in wrongly_found} == set()
    cited = insert_memory(store, metadata={"source_refs": ["source:" + sid]})
    assert found(store, sid) == {cited}


# -- the lookup agrees with the reader --------------------------------------------------------------------------------


def spellings(source_id):
    """Each string a ref can name ``source_id`` with, and strings that hold its digits without naming it."""

    digits = source_id.replace("-", "")
    forms = {
        "hyphenated": source_id,
        "upper": source_id.upper(),
        "no hyphens": digits,
        "no hyphens upper": digits.upper(),
        "mixed case": "".join(char.upper() if index % 3 == 0 else char for index, char in enumerate(source_id)),
        "braces": "{" + source_id + "}",
        "urn uuid": "urn:uuid:" + source_id,
        "uuid prefix": "uuid:" + source_id.upper(),
        "hyphens in groups of four": "-".join(digits[index:index + 4] for index in range(0, 32, 4)),
        "a hyphen after every digit": "-".join(digits),
        "a leading hyphen": "-" + digits,
        "urn inside the digits": digits[:10] + "urn:" + digits[10:],
        "uuid inside the digits": digits[:12] + "uuid:" + digits[12:],
        "source prefix": "source:" + source_id,
        "source prefix upper": "SOURCE: " + digits.upper(),
        "source twice": "source:source:" + source_id,
        "alice url": "alice://sources/" + source_id,
        "chunk suffix": "source:" + source_id + "#chunk-0",
        "alice url chunk": "alice://sources/" + digits + "#chunk-3",
        "a list of ids": f"{uuid4()}, {source_id}",
        "a list with semicolons": f"{source_id};{uuid4()}",
        "a sentence": "copied from source: " + source_id,
        "a url": "https://host.example/sources/" + source_id,
        "parentheses": "(" + source_id + ")",
        "a memory ref": "memory:" + source_id,
        "a longer run of digits": "ab" + digits,
        "digits cut short": digits[:-1],
    }
    for index, spelling in enumerate(_zero_led_spellings(source_id)):
        forms[f"zero spelling {index}"] = spelling
    return forms


def containers(ref):
    escaped = "".join("\\u%04x" % ord(char) for char in ref[:5]) + ref[5:]
    return {
        "list entry": {"source_refs": [ref]},
        "own key": {"source_id": ref},
        "nested object": {"source_refs": [{"sources": [{"ref": ref}]}]},
        "selected ids": {"selected_source_ids": [ref]},
        "JSON text": {"source_refs": [json.dumps({"source_id": ref})]},
        "JSON text with escapes": {"source_refs": ['{"source_id": "' + escaped + '"}']},
        "JSON text in a list": {"source_refs": [json.dumps([{"ref": ref}, "x"])]},
        "JSON quote": {"source_refs": [json.dumps({"quote": ref})]},
        "other key": {"origin": ref},
    }


def test_the_lookup_finds_a_memory_exactly_when_the_reader_names_the_source():
    """The agreement behind the whole change: for ids with no, one, two and three leading zeros, in each spelling the
    reader accepts, in nine containers (refs of a list, own key, nested objects, JSON text with and without escapes), the
    memories the lookup returns are the memories whose ref the reader (``cited_source_ids``) says names the id, no
    more and no fewer. The grid is not vacuous: about 1,300 of its 1,600 rows name the id and about 300 hold its digits
    without naming it, every spelling is accepted in some container (except the three that never name it), and the JSON
    quote container is never accepted.

    Mutations, each alone, in ``source_retirement.py`` (the saved original is copied back after each, and the file is
    compared): drop the Python confirm (the longer run of digits, the memory ref and the quote rows are found); drop
    ``lower(`` (the upper case rows are missed); stop removing hyphens (every hyphenated row is missed); stop removing
    underscores (the zero spellings with an underscore are missed); stop removing ``urn:`` (the spelling with ``urn:``
    inside the digits is missed); stop removing ``uuid:`` (the spelling with ``uuid:`` inside the digits is missed);
    search for the digits with their zeros (``.lstrip('0')`` removed: the zero spellings where whitespace, ``0x``
    or a sign takes the place of the zeros are missed); drop the escape test (the JSON text with escapes is missed);
    take the metadata out of the searched text; read only the reader's own fields in ``memory_cited_source_ids``.
    In ``vnext_source_fence.py``: skip the JSON decode (``_json_container`` returning ``None``: the JSON text with
    escapes is missed and the JSON quote row is found); read quotes as references (``_TEXT_KEYS`` made empty).
    """

    store = memory_store()
    ids = [str(uuid4()), str(uuid4()), _zero_led_id(1), _zero_led_id(2), _zero_led_id(3)]
    rows: dict[str, tuple[str, str, str, dict]] = {}
    for source_id in ids:
        for spelling_label, spelling in spellings(source_id).items():
            for container_label, container in containers(spelling).items():
                memory_id = insert_memory(store, metadata=container)
                rows[memory_id] = (source_id, spelling_label, container_label, container)
    accepted: set[tuple[str, str]] = set()
    rejected: set[tuple[str, str]] = set()
    for source_id in ids:
        returned = found(store, source_id)
        for memory_id, (row_id, spelling_label, container_label, container) in rows.items():
            if row_id != source_id:
                continue
            reader_names = source_id in cited_source_ids(container).every
            (accepted if reader_names else rejected).add((spelling_label, container_label))
            assert (memory_id in returned) == reader_names, (source_id, spelling_label, container_label, container)
    assert len(accepted) > 100 and len(rejected) > 50
    never_naming = {"a memory ref", "a longer run of digits", "digits cut short"}
    for label in {pair[0] for pair in accepted | rejected}:
        assert (label in never_naming) != any(pair[0] == label for pair in accepted), label
    for label in containers("x"):
        assert (label == "JSON quote") != any(pair[1] == label for pair in accepted), label
    assert any(pair[0].startswith("zero spelling") for pair in accepted)


# -- cost -----------------------------------------------------------------------------------------------------------


def test_delete_reads_only_the_candidates_and_stays_fast_on_a_large_vault(tmp_path, monkeypatch, capsys):
    """A vault of 10,000 memories, two of which cite the source. The delete finishes in under a second, and the reader
    is asked about the memories the SQL narrowing keeps (the two, not the vault). The count of reads is the guard that
    a slower machine cannot flatter: on a development machine the whole delete takes about a fifth of a second, and
    reading all 10,000 memories in Python takes about half a second on its own, so the time alone would not fail a
    lookup that read everything.

    Mutation: drop the text condition of the second query in ``citing_memories`` (``(? IS NOT NULL OR ? IS NOT NULL)``
    in its place: every memory is a candidate): the reader is asked about all 10,000 memories and the count fails.
    """

    db, _folder_path, sid = seeded(tmp_path)
    rows = []
    for index in range(10_000):
        other = str(uuid4())
        metadata = {"proposal_id": str(uuid4()), "source_refs": ["source:" + other, other.upper()],
                    "rationale": "Said in the standup on Thursday about the deploy cadence. " * 3}
        value = {"text": "The team deploys on Thursdays after the review queue is empty. " * 4, "source_refs": ["source:" + other]}
        rows.append((str(uuid4()), USER_ID, f"bulk.{index}", json.dumps(value), json.dumps(metadata)))
    with sqlite_user_connection(db, USER_ID) as conn:
        conn.executemany(
            "INSERT INTO memories (id, user_id, memory_key, value, status, source_event_ids, metadata_json, canonical_text) "
            "VALUES (?, ?, ?, ?, 'active', '[]', ?, 'The team deploys on Thursdays.')", rows)
        store = SQLiteVNextStore(conn, USER_ID)
        candidate = plant(store, [sid.upper()])
        kept = plant(store, ["source:" + sid.replace("-", "")], status="active", text="The amber record is active.")
    reads = []
    original = source_retirement.memory_cited_source_ids
    monkeypatch.setattr(source_retirement, "memory_cited_source_ids", lambda row: reads.append(row["id"]) or original(row))
    started = time.perf_counter()
    assert command(db, "delete", sid, "--yes") == 0
    elapsed = time.perf_counter() - started
    receipt = json.loads(capsys.readouterr().out)["deleted"][0]
    assert receipt["candidate_memories"] == 1 and receipt["memories_citing_replaced"] == [kept]
    assert_gone(db, candidate)
    assert elapsed < 1.0, elapsed
    assert len(reads) <= 4, len(reads)
