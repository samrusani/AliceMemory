"""A memory cites a source in every spelling the shared reference reader accepts.

The review of #551 and #552 reproduced this on main. ``citing_memories`` found a memory only when one of its
text nodes was the source id or ``source:<id>`` as written, so a proposal that cited the source as an upper case id,
an id without hyphens, a JSON ref or ``source:<id>#chunk-0`` was missed. After ``import-markdown --supersede`` the old
source was retired, but that proposal stayed pending, kept its stale text, stayed in the review queue and was left out of
``memories_citing_replaced``. ``sources delete`` and ``sources prune`` use the same function, so they reported success
while the proposal kept the deleted text.

Now a memory cites a source exactly when the shared reference reader says so
(``vnext_source_fence.memory_cited_source_ids``), after a SQL narrowing that never drops a memory the reader would accept.

The next review found two things. The narrowing did not look for an id written with decimal digits of another script
(Arabic-Indic, fullwidth, Devanagari, mathematical digits): ``int(..., 16)`` reads those as 0 to 9, so the reader names the
id, and a proposal that cited its source so was left pending after a replacement and was missed by ``sources delete`` and
``sources prune``, which reported success while the text stayed stored. The narrowing now reads those digits as the digits
they stand for. And nothing tested the user fences of the new text query, so a version that scrubbed another user's pending
memory would have passed: each flow below now has a second user's memory that cites the same source and must stay as it is.

The review of the head after that measured the cost. One lookup in a vault of 10,000 memories took 132 ms in English and
240 ms with accented text, and 674 ms and 1,205 ms at 50,000. ``sources prune`` looked every memory over once for each
replaced source in its preview and once for each in its receipt, so a prune of 50 sources took tens of seconds at 50,000
memories. The narrowing is now one registered SQLite function, ``alice_citation_hits``, that reads each memory once for
every source asked about at once, and the commands ask for all their sources together: a prune reads the memories once
for its preview and once for its receipt. The test that bounded a delete at one second took 1.12 s on the CI runner
under coverage, so the unit job was red; no test below measures time. The cost tests count what is read (the function is
called once for each memory in each pass, and the reader is asked about the memories that cite a source and no others),
and a count does not move with the speed of the machine. The user fences of the new statements each have a test that
fails without that fence alone.

Every test below goes through the real door the probe used (an agent proposal through the MCP handler) or plants the same
row shape in the vault, then runs the real importer or the real ``sources`` commands.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from contextlib import closing, contextmanager
from uuid import uuid4

import pytest

from alicebot_api.mcp import memories as mcp_memories
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vnext_source_fence import cited_source_ids
from alicebot_api.vnext_stores.sqlite import source_retirement
from alicebot_api.vnext_stores.sqlite.source_retirement import (
    _Probes, _citation_hits, _in_ascii_digits, _memories_by_id, citing_memories, citing_memories_by_source,
)
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
# The decimal digits of other scripts, as the code point of the zero of each. ``int(..., 16)`` reads each as 0 to 9, so
# ``uuid.UUID`` accepts an id written with them and the reader names it. The mathematical digits lie beyond the first 65,536
# characters, which JSON writes as a pair of escapes.
DIGIT_SCRIPTS = {
    "arabic-indic": 0x0660, "persian": 0x06F0, "devanagari": 0x0966, "fullwidth": 0xFF10,
    "mathematical bold": 0x1D7CE, "mathematical monospace": 0x1D7F6,
}


def in_script(text, zero):
    return "".join(chr(zero + int(char)) if char in "0123456789" else char for char in text)


def in_mixed_scripts(text):
    zeros = list(DIGIT_SCRIPTS.values())
    return "".join(chr(zeros[index % len(zeros)] + int(char)) if char in "0123456789" else char
                   for index, char in enumerate(text))


SCRIPT_SHAPES = {
    "arabic-indic digits": lambda sid: in_script(sid, DIGIT_SCRIPTS["arabic-indic"]),
    "fullwidth digits": lambda sid: in_script(sid.upper(), DIGIT_SCRIPTS["fullwidth"]),
    "devanagari digits no hyphens": lambda sid: in_script(sid.replace("-", ""), DIGIT_SCRIPTS["devanagari"]),
    "mathematical digits": lambda sid: in_script(sid, DIGIT_SCRIPTS["mathematical bold"]),
    "mixed scripts": lambda sid: in_mixed_scripts(sid),
    "JSON ref with escaped digits": lambda sid: json.dumps({"source_id": in_script(sid, DIGIT_SCRIPTS["fullwidth"])}),
    "JSON ref with escaped mathematical digits": lambda sid: json.dumps(
        {"source_id": in_script(sid, DIGIT_SCRIPTS["mathematical monospace"])}),
}
ALL_SHAPES = {**PROBE_SHAPES, **MORE_SHAPES, **SCRIPT_SHAPES}
# Refs that are not text: the nested keys the reader reads, planted as the objects they are.
NESTED_SHAPES = {
    "nested ref keys": lambda sid: {"sources": [{"ref": sid}]},
    "selected ids": lambda sid: {"selected_source_ids": [sid.upper()]},
    "nested ref keys with script digits": lambda sid: {"sources": [{"ref": in_script(sid, DIGIT_SCRIPTS["arabic-indic"])}]},
}
OTHER_USER_ID = "00000000-0000-0000-0000-000000000002"
BYSTANDER_TEXT = "The amber record of someone else."


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


def everything_stored_for(db, memory_id):
    return (_read(db, "SELECT * FROM memories WHERE id=?", (memory_id,)),
            _read(db, "SELECT * FROM memory_revisions WHERE memory_id=?", (memory_id,)))


def bystander(db, ref):
    """A pending memory of a second user of the vault that cites the same source id in the same spelling, with everything
    stored for it as it was written. The source is the first user's, so nothing of the second user's is retired with it."""

    with sqlite_user_connection(db, OTHER_USER_ID) as conn:
        ensure_sqlite_user(conn, OTHER_USER_ID, "bystander@example.com")
        memory_id = plant(SQLiteVNextStore(conn, OTHER_USER_ID), [ref], text=BYSTANDER_TEXT)
    return memory_id, everything_stored_for(db, memory_id)


def assert_bystander_untouched(db, bystander_memory):
    memory_id, written = bystander_memory
    assert status_of(db, memory_id) == "candidate"
    assert everything_stored_for(db, memory_id) == written


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
    is left alone, so a pass is not a blanket rejection. So is a pending memory of a second user of the vault that cites
    the same id in the same spelling: the source is not that user's, so nothing of theirs is rejected or listed. The
    shapes are the six of the probe, braces, ``urn:uuid:``, ``alice://sources/``, the nested keys, and ids written with
    the decimal digits of other scripts, which the reader names as well. On main the first two shapes of the probe pass
    and the others fail.

    Mutations, each alone, in ``source_retirement.py`` (the saved original is copied back after each, and the file is
    compared byte for byte). In the pass: skip the text of the memory (``if len(hits) < len(probes.every):`` made
    ``if False:``: every case fails); drop ``.lower()`` from it (the three cases ``upper case``, ``selected ids`` and
    ``fullwidth digits`` fail); stop removing hyphens from it (the seventeen cases whose spelling has hyphens fail); never
    read the digits of other scripts, or read every one as ``0`` (the eight cases that write the id in other digits fail);
    make ``retire_dependents`` read no memories (every case here and in the delete and prune tests fails); drop both user
    conditions, the one of the pass and the one of the read by id (every case fails, because the second user's memory is
    rejected and listed). Dropping only one of the two changes nothing here, because a memory of the second user that
    comes out of one is dropped by the other: the last tests of this file hold each fence alone.
    """

    db, folder, sid = seeded(tmp_path)
    build = {**ALL_SHAPES, **NESTED_SHAPES}[shape]
    cited = propose(db, monkeypatch, build(sid))
    unrelated = propose(db, monkeypatch, build(str(uuid4())), text="The bronze statement.")
    other_users = bystander(db, build(sid))
    (folder / "note.md").write_text("The current copper statement.")
    result = run_import(db, folder, supersede=True)
    assert result.failed_count == 0 and result.superseded
    assert status_of(db, cited) == "rejected"
    assert cited in result.memories_citing_replaced
    assert status_of(db, unrelated) == "candidate"
    assert unrelated not in result.memories_citing_replaced
    assert other_users[0] not in result.memories_citing_replaced
    assert_bystander_untouched(db, other_users)


# -- delete and prune ------------------------------------------------------------------------------------------------


def assert_gone(db, memory_id):
    with sqlite_user_connection(db, USER_ID) as conn:
        assert SQLiteVNextStore(conn, USER_ID).memory_redaction_bundle_is_exact(memory_id, [])
    assert WORD not in stored_words(db)


@pytest.mark.parametrize("shape", [*ALL_SHAPES, *NESTED_SHAPES])
def test_delete_previews_scrubs_and_lists_memories_that_cite_the_source_in_any_spelling(tmp_path, monkeypatch, capsys, shape):
    """``sources delete`` for a source cited in one spelling: the preview and the receipt both count the pending
    candidate, the candidate is scrubbed (its bundle is exact and its words are gone from the memories and revisions),
    and an active memory that cites the source in the same spelling is listed in both and keeps its text. A pending
    memory of a second user of the vault that cites the same id is neither counted, nor listed, nor scrubbed: every row
    stored for it is as it was written.

    Mutations: the ones of the replacement test, and a ``_preview`` in ``source_commands.py`` that lists no citing
    memory (``memories=[...]`` made ``memories=[]``: every case of this test and of the prune test fails on the preview,
    and the receipt, which scrubs through ``retire_dependents``, is not what fails).
    """

    db, _folder_path, sid = seeded(tmp_path)
    build = {**ALL_SHAPES, **NESTED_SHAPES}[shape]
    candidate = propose(db, monkeypatch, build(sid))
    with sqlite_user_connection(db, USER_ID) as conn:
        kept = plant(SQLiteVNextStore(conn, USER_ID), [build(sid)], status="active", text="The amber record is active.")
    other_users = bystander(db, build(sid))
    assert command(db, "delete", sid) == 2
    preview = json.loads(capsys.readouterr().out)["would_delete"][0]
    assert command(db, "delete", sid, "--yes") == 0
    receipt = json.loads(capsys.readouterr().out)["deleted"][0]
    for counts in (preview, receipt):
        assert counts["candidate_memories"] == 1
        assert counts["memories_citing_replaced"] == [kept]
    assert_gone(db, candidate)
    assert _read(db, "SELECT canonical_text FROM memories WHERE id=?", (kept,)) == [("The amber record is active.",)]
    assert_bystander_untouched(db, other_users)


@pytest.mark.parametrize("shape", [*ALL_SHAPES, *NESTED_SHAPES])
def test_prune_previews_scrubs_and_lists_memories_that_cite_a_replaced_source_in_any_spelling(tmp_path, monkeypatch, capsys, shape):
    """``sources prune --superseded``: the memories are written after the source was replaced, so the prune is what
    finds them and not the replacement. Preview and receipt count the candidate and list the active memory, and the
    candidate is scrubbed. A pending memory of a second user of the vault that cites the same id is left as it was
    written.

    Mutations: the ones of the replacement and delete tests.
    """

    db, folder, sid = seeded(tmp_path)
    (folder / "note.md").write_text("The current copper statement.")
    assert run_import(db, folder, supersede=True).superseded
    build = {**ALL_SHAPES, **NESTED_SHAPES}[shape]
    candidate = propose(db, monkeypatch, build(sid))
    with sqlite_user_connection(db, USER_ID) as conn:
        kept = plant(SQLiteVNextStore(conn, USER_ID), [build(sid)], status="active", text="The amber record is active.")
    other_users = bystander(db, build(sid))
    assert command(db, "prune", "--superseded") == 2
    preview = json.loads(capsys.readouterr().out)["would_delete"][0]
    assert command(db, "prune", "--superseded", "--yes") == 0
    receipt = json.loads(capsys.readouterr().out)["deleted"][0]
    for counts in (preview, receipt):
        assert counts["id"] == sid and counts["candidate_memories"] == 1
        assert counts["memories_citing_replaced"] == [kept]
    assert_gone(db, candidate)
    assert_bystander_untouched(db, other_users)


# -- what the lookup reads --------------------------------------------------------------------------------------------


def memory_store():
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    ensure_sqlite_user(conn, USER_ID, "citing@example.com")
    return SQLiteVNextStore(conn, USER_ID)


def insert_memory(store, *, metadata=None, value=None, events=None, raw=False):
    """A pending memory row. ``raw`` writes the JSON columns as the store does, with characters beyond ASCII as they are
    (``ensure_ascii=False``); otherwise they are written as ``\\uXXXX`` escapes, as ``json.dumps`` writes them by default."""

    memory_id = str(uuid4())
    store.conn.execute(
        "INSERT INTO memories (id, user_id, memory_key, value, status, source_event_ids, metadata_json, canonical_text) "
        "VALUES (?, ?, ?, ?, 'candidate', ?, ?, 'A record.')",
        (memory_id, USER_ID, "key." + memory_id, json.dumps(value or {"text": "A record."}, ensure_ascii=not raw),
         json.dumps(events or []), json.dumps(metadata or {}, ensure_ascii=not raw)))
    return memory_id


def found(store, source_id):
    return {str(row["id"]) for row in citing_memories(store, source_id)}


def test_every_stored_field_the_product_keeps_a_source_in_is_read():
    """A memory names its source in the reference fields the saved-quote reader judges, in the places other code of
    the product reads (``source_id``, ``source_ids`` and ``selected_source_ids`` at the top of the metadata, as the
    retrieval and review routes read them), and in free text under another key, which the reader counts as a citation
    of a stored source. Each placement is found in a spelling that main did not find, and in fullwidth digits, with the
    JSON columns written as the store writes them (characters as they are) and as ``json.dumps`` writes them by default
    (escapes).

    Mutations: take the metadata out of the text the pass reads (``_text_of(metadata_json) + "\\n" + _text_of(value)`` made
    ``_text_of(value)``: the metadata placements fail); take the value out of it (made ``_text_of(metadata_json)``: the
    value placements fail); read only ``_source_ids_named_by_memory_copies`` in ``memory_cited_source_ids`` (the top-level
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
    script_spelled = in_script(sid, DIGIT_SCRIPTS["fullwidth"])
    placements |= {
        "metadata source_refs in fullwidth digits": {"metadata": {"source_refs": [script_spelled]}},
        "value source_refs in fullwidth digits": {"value": {"text": "A record.", "source_refs": [script_spelled]}},
        "top-level source_id in fullwidth digits": {"metadata": {"source_id": script_spelled}},
    }
    ids = {f"{label}, {'raw' if raw else 'escaped'}": insert_memory(store, **row, raw=raw)
           for raw in (False, True) for label, row in placements.items()}
    assert found(store, sid) == set(ids.values())


def test_text_that_only_looks_like_a_citation_is_not_one():
    """Not a citation: another source's id, the id inside a quote or an excerpt (at any depth, in text or in a JSON
    text), a ``memory:`` ref, and a longer run of hex digits that holds the digits. The SQL narrowing keeps all but
    the first, so the reader is what rejects them. (An id in a sentence under another key is a citation: see the test
    above.)

    Mutations: make the check ``if canonical[source_id] in named:`` of ``citing_memories_by_source`` always true (the
    memory ref, the longer run and the quote rows are found, in fullwidth digits too: the pass keeps those rows and the
    reader is what rejects them); read quotes and excerpts as references (``_TEXT_KEYS`` made empty: the quote and excerpt
    rows are found); skip the JSON decode of the reader (``_json_container`` returning ``None``: the JSON quote rows are
    found).
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
        "digits of another id in fullwidth digits": {"metadata": {"source_refs": [in_script(str(uuid4()), DIGIT_SCRIPTS["fullwidth"])]}},
        "quote in fullwidth digits": {"metadata": {"provenance": {"quote": in_script(sid, DIGIT_SCRIPTS["fullwidth"]),
                                                                   "source_id": str(uuid4())}}},
        "JSON quote in mathematical digits": {"metadata": {"source_refs": [json.dumps(
            {"quote": in_script(sid, DIGIT_SCRIPTS["mathematical bold"]), "source_id": str(uuid4())})]}},
        "memory ref in fullwidth digits": {"metadata": {"source_refs": ["memory:" + in_script(sid, DIGIT_SCRIPTS["fullwidth"])]}},
        "longer hex run in fullwidth digits": {"metadata": {"source_refs": ["ab" + in_script(digits, DIGIT_SCRIPTS["fullwidth"])]}},
    }
    inserted = {f"{label}, {'raw' if raw else 'escaped'}": insert_memory(store, **row, raw=raw)
                for raw in (False, True) for label, row in rows.items()}
    wrongly_found = found(store, sid)
    assert {label for label, memory_id in inserted.items() if memory_id in wrongly_found} == set()
    cited = insert_memory(store, metadata={"source_refs": ["source:" + sid]})
    assert found(store, sid) == {cited}


# -- the lookup agrees with the reader --------------------------------------------------------------------------------

# The spellings below that hold the digits of an id, or the id in a form the reader does not take, without naming it. Every
# other spelling names the id in at least one container, and the test checks that, so a spelling the reader never accepts
# cannot sit in the grid as if it were covered.
NEVER_NAMING = {"a memory ref", "a longer run of digits", "digits cut short", "script chunk suffix", "script memory ref",
                "script longer run of digits", "script digits cut short"}


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
    arabic, persian, devanagari = (DIGIT_SCRIPTS[name] for name in ("arabic-indic", "persian", "devanagari"))
    fullwidth, bold, mono = (DIGIT_SCRIPTS[name] for name in ("fullwidth", "mathematical bold", "mathematical monospace"))
    for script, zero in DIGIT_SCRIPTS.items():
        forms[f"{script} digits"] = in_script(source_id, zero)
    forms |= {
        "fullwidth digits upper": in_script(source_id.upper(), fullwidth),
        "devanagari digits no hyphens": in_script(digits, devanagari),
        "mathematical digits in braces": "{" + in_script(source_id, bold) + "}",
        "persian digits urn uuid": "urn:uuid:" + in_script(source_id, persian),
        "arabic-indic digits source prefix": "SOURCE: " + in_script(digits.upper(), arabic),
        "fullwidth digits alice url": "alice://sources/" + in_script(source_id, fullwidth),
        "arabic-indic digits in groups of four": "-".join(in_script(digits[index:index + 4], arabic) for index in range(0, 32, 4)),
        "fullwidth digits urn inside": in_script(digits[:10] + "urn:" + digits[10:], fullwidth),
        "devanagari digits uuid inside": in_script(digits[:12] + "uuid:" + digits[12:], devanagari),
        "arabic-indic digits in a list of ids": f"{uuid4()}, {in_script(source_id, arabic)}",
        "mixed scripts": in_mixed_scripts(source_id),
        "mixed scripts upper no hyphens": in_mixed_scripts(digits.upper()),
        "script chunk suffix": "source:" + in_script(source_id, persian) + "#chunk-0",
        "script memory ref": "memory:" + in_script(source_id, fullwidth),
        "script longer run of digits": "ab" + in_script(digits, mono),
        "script digits cut short": in_script(digits[:-1], arabic),
    }
    for index, spelling in enumerate(_zero_led_spellings(source_id)):
        forms[f"script zero spelling {index}"] = in_script(spelling, arabic)
    return forms


def escape_ascii(text, which=lambda char, index: True, *, upper=False):
    """``text`` with the printable ASCII characters that ``which(char, index)`` picks written as ``\\uXXXX`` escapes, so
    it is still the body of a JSON string: a quote, a backslash and a control character are left as they are."""

    template = "\\u%04X" if upper else "\\u%04x"
    return "".join(template % ord(char) if 32 <= ord(char) < 127 and char not in '"\\' and which(char, index) else char
                   for index, char in enumerate(text))


def json_text_of(ref, **options):
    return '{"source_id": "' + escape_ascii(ref, **options) + '"}'


def containers(ref):
    generator = random.Random(ref)
    inner = '{"ref": "' + escape_ascii(ref) + '"}'
    hidden = inner.replace("\\", "\\u005c").replace('"', "\\u0022")
    return {
        "list entry": {"source_refs": [ref]},
        "own key": {"source_id": ref},
        "nested object": {"source_refs": [{"sources": [{"ref": ref}]}]},
        "selected ids": {"selected_source_ids": [ref]},
        "JSON text": {"source_refs": [json.dumps({"source_id": ref})]},
        "JSON text with the first five characters escaped": {"source_refs": [json_text_of(ref, which=lambda char, index: index < 5)]},
        "JSON text with the hex letters escaped": {"source_refs": [json_text_of(ref, which=lambda char, index: char in "abcdefABCDEF")]},
        "JSON text with the hyphens escaped": {"source_refs": [json_text_of(ref, which=lambda char, index: char == "-")]},
        "JSON text with the u and r of urn: and uuid: escaped": {"source_refs": [json_text_of(ref, which=lambda char, index: char in "ur")]},
        "JSON text with every character escaped": {"source_refs": [json_text_of(ref)]},
        "JSON text with every character escaped in upper case": {"source_refs": [json_text_of(ref, upper=True)]},
        "JSON text with a random subset escaped": {"source_refs": [json_text_of(ref, which=lambda char, index: generator.random() < 0.4)]},
        "JSON text in JSON text": {"source_refs": [json.dumps({"sources": [json.dumps({"ref": ref})]})]},
        "JSON text in JSON text in JSON text": {"source_refs": [json.dumps(
            {"sources": [json.dumps({"sources": [json.dumps({"ref": ref})]})]})]},
        "JSON text with its escapes behind escaped backslashes": {"source_refs": ['{"sources": ["' + hidden + '"]}']},
        "JSON text in a list": {"source_refs": [json.dumps([{"ref": ref}, "x"])]},
        "JSON quote": {"source_refs": [json.dumps({"quote": ref})]},
        "other key": {"origin": ref},
    }


def test_the_lookup_finds_a_memory_exactly_when_the_reader_names_the_source():
    """The agreement behind the whole change: for ids with no, one, two and three leading zeros, in each spelling the
    reader accepts (the digits written in ASCII and in the decimal digits of six other scripts, alone and mixed), in
    eighteen containers (refs of a list, own key, nested objects, JSON text with the first five characters, the hex
    letters, the hyphens, the ``u`` and ``r`` of ``urn:`` and ``uuid:``, every character and a random subset of its ASCII
    characters escaped, JSON text inside JSON text two and three deep, and a JSON text whose own escapes are hidden behind
    escaped backslashes), stored both as the store
    stores them (characters as they are) and as ``json.dumps`` writes them by default (escapes), the memories the lookup
    returns are the memories whose ref the reader (``cited_source_ids``) says names the id, no more and no fewer. The grid
    is not vacuous: thousands of its rows name the id and thousands hold its digits without naming it, every spelling is
    accepted in some container (except the ones listed as never naming it), every container accepts some spelling (except
    the JSON quote), and the JSON quote container is never accepted.

    Mutations, each alone, in ``source_retirement.py`` (the saved original is copied back after each, and the file is
    compared byte for byte): drop the Python confirm (the longer run of digits, the memory ref and the quote rows are
    found); drop ``.lower()`` (the upper case rows are missed); stop removing hyphens (every hyphenated row is missed);
    stop removing underscores (the zero spellings with an underscore are missed); stop removing ``urn:`` (the spelling
    with ``urn:`` inside the digits is missed); stop removing ``uuid:`` (the spelling with ``uuid:`` inside the digits is
    missed); search for the digits with their zeros (``.lstrip('0')`` removed: the zero spellings where whitespace,
    ``0x`` or a sign takes the place of the zeros are missed); drop the rule for escaped ASCII characters (the JSON text
    with escapes is missed); narrow the class of that rule to the second digit ``[3-7]``, to ``[2-6]``, or to the first
    hex digit only (``[0-9]`` for ``[0-9a-f]``: the hyphens, the ``u`` and ``r`` and the hex letters containers are
    missed); take the metadata out of the text; read only the reader's own fields in ``memory_cited_source_ids``; map
    every kept memory to the first source (the lookup for all the ids is wrong where the lookup for each id alone is
    right, which the comparison with the lookup for all five ids at once sees). The mutations of the digits of other
    scripts are in the tests below. In ``vnext_source_fence.py``: skip the JSON decode (``_json_container`` returning
    ``None``: the JSON text with escapes is missed and the JSON quote row is found); read quotes as references
    (``_TEXT_KEYS`` made empty).
    """

    store = memory_store()
    ids = [str(uuid4()), str(uuid4()), _zero_led_id(1), _zero_led_id(2), _zero_led_id(3)]
    rows: dict[str, tuple[str, str, str, dict]] = {}
    for source_id in ids:
        for spelling_label, spelling in spellings(source_id).items():
            for container_label, container in containers(spelling).items():
                for raw in (False, True):
                    memory_id = insert_memory(store, metadata=container, raw=raw)
                    rows[memory_id] = (source_id, spelling_label, container_label, container)
    accepted: set[tuple[str, str]] = set()
    rejected: set[tuple[str, str]] = set()
    named = held_without_naming = 0
    together = citing_memories_by_source(store, ids)
    for source_id in ids:
        returned = found(store, source_id)
        assert {str(row["id"]) for row in together[source_id]} == returned, source_id
        for memory_id, (row_id, spelling_label, container_label, container) in rows.items():
            if row_id != source_id:
                continue
            reader_names = source_id in cited_source_ids(container).every
            (accepted if reader_names else rejected).add((spelling_label, container_label))
            named += reader_names
            held_without_naming += not reader_names
            assert (memory_id in returned) == reader_names, (source_id, spelling_label, container_label, container)
    assert len(accepted) > 1_000 and len(rejected) > 150 and named > 8_000 and held_without_naming > 1_500
    for label in {pair[0] for pair in accepted | rejected}:
        assert (label in NEVER_NAMING) != any(pair[0] == label for pair in accepted), label
    for label in containers("x"):
        assert (label == "JSON quote") != any(pair[1] == label for pair in accepted), label
    assert any(pair[0].startswith("zero spelling") for pair in accepted)
    assert any(pair[0].startswith("script zero spelling") for pair in accepted)
    for script in DIGIT_SCRIPTS:
        assert any(pair[0] == f"{script} digits" for pair in accepted), script


# -- the digits of other scripts ---------------------------------------------------------------------------------------


def text_hits(text, digits, *, in_value=False):
    """Whether the pass keeps a memory whose metadata (or value) is ``text`` for a source whose id has ``digits``. The
    pass reads the text as ``alice_citation_hits`` does: lower cased, once, for all the sources it is given."""

    probes = _Probes([("no-such-source", digits)])
    metadata, value = ("{}", text) if in_value else (text, "{}")
    return _citation_hits(metadata, value, "[]", probes) == "0"


def test_every_script_shape_names_the_source_to_the_reader():
    """The shapes of the replacement, delete and prune tests that write the id in the digits of another script are
    citations: the reader names the id in each and names nothing for another id, so a lookup that misses one is wrong
    and the shape is not a loose fit. (``source:<id>#chunk-0`` is not among them: the reader takes a ``#chunk-0`` suffix
    only after an id in 0 to 9, so it names nothing there and the lookup rightly does not find it.)

    Mutation: none to make, because this test reads the reader alone. It fails if a script shape stops naming the id.
    """

    sid = str(uuid4())
    for label, build in {**SCRIPT_SHAPES, **{key: value for key, value in NESTED_SHAPES.items() if "script" in key}}.items():
        assert sid in cited_source_ids(build(sid)).every, label
        assert sid not in cited_source_ids(build(str(uuid4()))).every, label


def test_the_pass_reads_the_characters_and_escapes_that_int_reads_as_digits():
    """The part of the pass that sees an id written in other scripts, checked on every code point up to U+2FFFF (the
    Unicode decimal digits all lie below it) and a sample of the rest: a character that ``int(..., 16)`` reads as a digit,
    written as it is, between two letters, or as the JSON escape ``json.dumps`` writes for it (one escape, or a pair for a
    character beyond U+FFFF), keeps a source whose digits are the digit it stands for, and no other; a character that is not
    read as a digit keeps nothing. A text of the escapes is repeated nine times, with nine digits to find, because the
    hex digits of an escape are ASCII digits and one of them could be the digit asked for: no run of nine is.

    Mutations: take the digit class to the Arabic-Indic digits alone (``_OTHER_SCRIPT_DIGIT`` narrowed to the ten of
    them: every other script fails); stop reading the pair of escapes (the pair alternative of ``_DIGIT_ESCAPE`` made
    never match: the mathematical digits fail in their escaped form); narrow the single escape to the blocks that start
    with ``0`` or ``1`` (``[01af]`` made ``[01]``: the fullwidth digits fail in their escaped form); map every digit to
    ``0`` (``_ASCII_DIGIT[match[0]]`` made ``'0'``: the check for the digit it stands for fails); never read an escape
    (the escape branch of ``_in_ascii_digits`` made false: every escaped digit fails); treat every text that is not ASCII
    as holding no such digit (the Latin-1 test of ``_in_ascii_digits`` made ``True``: every raw digit fails).
    """

    sample = 97
    for code in [*range(0x80, 0x30000), *range(0x30000, 0x110000, sample)]:
        if 0xD800 <= code <= 0xDFFF:
            continue
        char = chr(code)
        escape = json.dumps(char)[1:-1]
        try:
            value = int("1" + char + "1", 16) - 0x101
            reads_as_digit = value % 16 == 0 and 0 <= value // 16 <= 9
            digit = value // 16
        except ValueError:
            reads_as_digit = False
        if reads_as_digit:
            for spelled, copies in ((char, 1), ("x" + char + "x", 1), (escape, 9)):
                assert text_hits(spelled * copies, str(digit) * copies), hex(code)
                assert not text_hits(spelled * copies, str((digit + 1) % 10) * copies), hex(code)
                assert text_hits(spelled * copies, str(digit) * copies, in_value=True), hex(code)
        else:
            for spelled in (char, escape):
                assert not text_hits(spelled * 9, "1" * 9), hex(code)
    assert text_hits("١", "1") and text_hits("\\ud835\\udfce", "0")


def test_the_digit_reader_reads_nested_escapes_and_leaves_other_text_alone():
    """The function that writes other scripts' digits as 0 to 9 (``_in_ascii_digits``), on the texts the pass hands it:
    escapes at any depth of JSON text (the pair ``json.dumps`` writes for the mathematical digits has its backslashes
    doubled at each depth), text that is ASCII with no escape never read, and an escape that is not a digit left as it
    is. What ``uuid.UUID`` ignores inside an id (``urn:``, ``uuid:``, hyphens, underscores) is taken out by the pass
    after this, so a run of digits split by them is still found.

    Mutations: drop the run of backslashes from ``_DIGIT_ESCAPE`` (the nested pairs fail); take ``"-"`` out of
    ``_IGNORED_IN_AN_ID`` (the hyphen cases fail); take ``"uuid:"`` out of it (the ``uuid:`` case fails); never read an
    escape (the escape branch of ``_in_ascii_digits`` made false: every escaped case fails); read an escape that is not a
    digit as the character (``_digit_escape`` returning ``char`` for every escape: the letter escape is changed).
    """

    mathematical = "\U0001d7ce\U0001d7cf\U0001d7d0"
    nested = json.dumps(mathematical)
    for depth in range(1, 6):
        assert _in_ascii_digits(nested) is not None and "012" in _in_ascii_digits(nested), depth
        assert text_hits(nested, "012"), depth
        nested = json.dumps(nested)
    assert text_hits("a١-b٢_c٣", "a1b2c3")
    assert text_hits("urn:uuid:١٢", "12")
    assert text_hits("١urn:٢uuid:٣", "123")
    assert text_hits("\\u0661\\u0662", "12")
    assert _in_ascii_digits("a١-b٢") == "a1-b2"
    assert _in_ascii_digits("\\u0661\\u0662") == "12"
    assert _in_ascii_digits("plain ascii 123 without any escape") is None
    assert _in_ascii_digits("ascii 12 only \\u00e9 and \\u4e2d") is None
    assert _in_ascii_digits("١ and \\u00e9") == "1 and \\u00e9"
    assert not text_hits("ascii 12 only \\u00e9", "13")
    assert not text_hits("١٢", "13")
    assert not text_hits("", "1")



def test_a_long_run_of_backslashes_is_read_in_time_that_grows_with_its_length():
    """Memory text is written by agents, and each memory is read by every lookup, so the reader of escapes must not cost
    the square of a text's length. One escape and then 400,000 backslashes is read in well under five seconds (a
    linear reader takes a few milliseconds; a reader that starts a match at each backslash of the run reads the rest of
    the run from each, about a minute here). The whole pass over such a memory is held to the same bound.

    Mutation: let a match start inside a run of backslashes (the ``(?<!\\\\)\\\\+u`` of ``_DIGIT_ESCAPE`` made the
    ``\\\\\\\\*u`` it replaced: the read takes about a minute and fails the bound).
    """

    text = "\\u0000" + "\\" * 400_000
    started = time.perf_counter()
    assert _in_ascii_digits(text) is None
    assert not text_hits(text, "1" * 9) and not text_hits(text, "1" * 9, in_value=True)
    assert time.perf_counter() - started < 5


def test_what_an_id_ignores_is_taken_out_in_the_order_the_reader_takes_it_out():
    """``uuid.UUID`` takes ``urn:`` out of a string before ``uuid:``, so an id split by ``uurn:uid:`` is read as the id: the
    ``urn:`` goes and leaves ``uuid:``, which goes next. The reader names such a ref, and the pass keeps it, in an id
    with hyphens and without, after ``source:``, and in the value as in the metadata.

    Mutation: take ``uuid:`` out before ``urn:`` (the first two entries of ``_IGNORED_IN_AN_ID`` swapped: ``urn:`` goes
    last and leaves ``uuid:`` in the run of digits, so the pass drops the memory).
    """

    sid = str(uuid4())
    plain = sid.replace("-", "")
    for ref in (sid[:8] + "uurn:uid:" + sid[8:], "source:" + sid[:8] + "uurn:uid:" + sid[8:],
                plain[:10] + "uurn:uid:" + plain[10:]):
        row = {"id": str(uuid4()), "value": {"text": "x", "source_refs": [ref]}, "metadata_json": {"source_refs": [ref]},
               "source_event_ids": []}
        assert sid in cited_source_ids(row).every, ref
        digits = plain.lstrip("0")
        assert text_hits(json.dumps({"source_refs": [ref]}), digits), ref
        assert text_hits(json.dumps({"source_refs": [ref]}), digits, in_value=True), ref


def test_an_ascii_character_written_as_a_json_escape_keeps_every_source():
    """A ref that is JSON text can write any character of an id as an escape (``\\u0061``) and the reader decodes it, so a
    text with the escape of a printable ASCII character is kept for every source, whatever the digits. The escapes of
    control characters, of the character after the last ASCII one and of letters outside ASCII are what ordinary text has
    and keep nothing. The pass reads the metadata and the value alike.

    Mutations: narrow the class to the second digit ``[3-7]`` (the space and ``-`` fail), to ``[2-6]`` (DEL fails) or to
    ``[2-7][0-9]`` for the last two digits (the tilde and ``-`` fail); widen it to ``[0-7]`` (the control characters
    fail) or to ``[2-9]`` (the escapes just above ASCII fail); drop the rule (the ``if escaped and ...`` of
    ``_add_text_hits`` made ``if False``: every escape fails).
    """

    nine = "f" * 9
    for code in (0x20, 0x2D, 0x30, 0x41, 0x5F, 0x61, 0x7E, 0x7F):
        for text in ("\\u%04x" % code, "x \\u%04X y" % code, "{\"source_id\": \"\\u%04x\"}" % code):
            assert text_hits(text, nine), hex(code)
            assert text_hits(text, nine, in_value=True), hex(code)
    for code in (0x00, 0x09, 0x1F, 0x80, 0xE9, 0x4E2D):
        assert not text_hits("\\u%04x" % code, nine), hex(code)
    many = _Probes([("a", "f" * 9), ("b", None), ("c", "e" * 9)])
    assert _citation_hits("\\u0061", "{}", "[]", many) == "0,2"


# -- one pass, every source -------------------------------------------------------------------------------------------


@pytest.fixture
def reads(monkeypatch):
    """Records each memory the pass is asked about (``alice_citation_hits`` is called with its metadata) and each memory
    the reader is asked about."""

    passes, readers = [], []
    pass_original = source_retirement._citation_hits
    reader_original = source_retirement.memory_cited_source_ids
    monkeypatch.setattr(source_retirement, "_citation_hits",
                        lambda metadata, *rest: passes.append(metadata) or pass_original(metadata, *rest))
    monkeypatch.setattr(source_retirement, "memory_cited_source_ids",
                        lambda row: readers.append(str(row["id"])) or reader_original(row))
    return passes, readers


def add_filler(db, count):
    """``count`` active memories that cite nothing of the vault. Half are written in a language with digits of its own:
    dates and refs to other sources in Arabic-Indic and fullwidth digits."""

    arabic, fullwidth = DIGIT_SCRIPTS["arabic-indic"], DIGIT_SCRIPTS["fullwidth"]
    rows = []
    for index in range(count):
        other = str(uuid4())
        metadata = {"proposal_id": str(uuid4()), "source_refs": ["source:" + other, other.upper()],
                    "rationale": "Said in the standup on Thursday about the deploy cadence. " * 3}
        value = {"text": "The team deploys on Thursdays after the review queue is empty. " * 4, "source_refs": ["source:" + other]}
        if index % 2:
            metadata = {"proposal_id": str(uuid4()), "source_refs": ["source:" + in_script(other, fullwidth), in_script(other.upper(), arabic)],
                        "rationale": in_script("Said on 2026-10-05 at 14:30 in the standup about the deploy cadence of 7 teams. " * 3, arabic)}
            value = {"text": in_script("The team deploys on Thursdays, 12 times in 2026. " * 4, fullwidth),
                     "source_refs": [in_script("source:" + other, arabic)]}
        rows.append((str(uuid4()), USER_ID, f"bulk.{index}", json.dumps(value, ensure_ascii=False), json.dumps(metadata, ensure_ascii=False)))
    with sqlite_user_connection(db, USER_ID) as conn:
        conn.executemany(
            "INSERT INTO memories (id, user_id, memory_key, value, status, source_event_ids, metadata_json, canonical_text) "
            "VALUES (?, ?, ?, ?, 'active', '[]', ?, 'The team deploys on Thursdays.')", rows)


def memory_count(db):
    return _read(db, "SELECT count(*) FROM memories WHERE user_id=?", (USER_ID,))[0][0]


def replaced_sources(tmp_path, count):
    """A vault with ``count`` sources that an edit has replaced, as ``(db, ids in the order a prune takes them)``."""

    db = _vault(tmp_path)
    folder = _folder(tmp_path, **{f"note{index}": f"The older amber statement {index}." for index in range(count)})
    run_import(db, folder)
    for index in range(count):
        (folder / f"note{index}.md").write_text(f"The current copper statement {index}.")
    assert len(run_import(db, folder, supersede=True).superseded) == count
    ids = [row[0] for row in _read(db, "SELECT id FROM sources WHERE deleted_at IS NOT NULL ORDER BY deleted_at, id")]
    assert len(ids) == count
    return db, ids


def test_a_delete_reads_each_memory_once_for_the_preview_and_once_for_the_receipt(tmp_path, capsys, reads):
    """A vault of 10,000 memories, two of which cite the source (one in fullwidth digits). Half of the others hold digits
    of another script. ``sources delete`` reads every memory once for its preview and once more for its receipt, and the
    reader is asked about the two memories that cite the source and no others (not the vault, and not the half that holds
    digits of another script). The counts are what is checked, and no time: a count does not move with the speed of the
    machine, and one pass over the memories does the work that took one pass per source.

    Mutations: call the function twice for each memory (``_citation_function`` calling ``_citation_hits`` a second time:
    the count of passes is twice the memories); keep every memory that holds a digit of another script, whatever the id
    (``_add_text_hits`` made to add ``probes.every_text`` after it reads other digits: the reader is asked about the 5,000
    of them); keep every memory (``_citation_hits`` answering for every source whatever the memory: the reader is asked
    about all 10,000); look the source up again in the preview (``_preview`` calling ``citing_memories_by_source`` for
    each row besides the lookup for all of them: the preview takes two passes); give ``scrub_source`` no ``citing_ids``
    (each scrub looks again: the receipt takes two passes). The user fences have the tests at the end of the file.
    """

    db, _folder_path, sid = seeded(tmp_path)
    add_filler(db, 10_000)
    fullwidth = DIGIT_SCRIPTS["fullwidth"]
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        candidate = plant(store, [in_script(sid.upper(), fullwidth)])
        kept = plant(store, ["source:" + sid.replace("-", "")], status="active", text="The amber record is active.")
    total = memory_count(db)
    assert total >= 10_002
    passes, readers = reads
    assert command(db, "delete", sid) == 2
    preview = json.loads(capsys.readouterr().out)["would_delete"][0]
    assert len(passes) == total and sorted(readers) == sorted([candidate, kept])
    passes.clear()
    readers.clear()
    assert command(db, "delete", sid, "--yes") == 0
    receipt = json.loads(capsys.readouterr().out)["deleted"][0]
    assert len(passes) == total and sorted(readers) == sorted([candidate, kept])
    for counts in (preview, receipt):
        assert counts["candidate_memories"] == 1 and counts["memories_citing_replaced"] == [kept]
    assert_gone(db, candidate)


def test_a_prune_of_many_sources_reads_each_memory_once_for_the_preview_and_once_for_the_receipt(tmp_path, capsys, reads):
    """Twelve replaced sources in a vault of 10,000 memories. Each source is cited by one pending proposal, in a spelling
    that changes from source to source, and one more pending proposal cites two of them (the first and the last). ``sources
    prune --superseded`` reads every memory once for its preview and once for its receipt, not once per source, and the
    reader reads each proposal once, the one that cites two included.

    Mutations: look the sources up one at a time (``citing_memories_by_source`` made to call ``_citation_candidates`` for
    each source and merge the answers: the passes are twelve times the memories); look each source up again in the
    preview (``_preview`` in ``source_commands.py`` calling ``citing_memories_by_source`` for each row besides the lookup
    for all of them: the preview takes thirteen passes); give ``scrub_source`` no ``citing_ids`` (the argument in
    ``run_sources`` made ``citing_ids=None``: each scrub looks again, and the receipt takes thirteen passes); ask the
    reader once for each source a memory may cite (``named`` read inside the loop over its sources: the proposal that
    cites two is read twice).
    """

    db, ids = replaced_sources(tmp_path, 12)
    add_filler(db, 10_000)
    shapes = list(ALL_SHAPES.values())
    planted = []
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        for index, sid in enumerate(ids):
            planted.append(plant(store, [shapes[index % len(shapes)](sid)], text=f"Pending proposal {index}."))
        planted.append(plant(store, [shapes[1](ids[0]), shapes[2](ids[-1])], text="Pending proposal that cites two."))
    total = memory_count(db)
    passes, readers = reads
    assert command(db, "prune", "--superseded") == 2
    preview = json.loads(capsys.readouterr().out)["would_delete"]
    assert len(passes) == total and sorted(readers) == sorted(planted)
    passes.clear()
    readers.clear()
    assert command(db, "prune", "--superseded", "--yes") == 0
    receipt = json.loads(capsys.readouterr().out)["deleted"]
    assert len(passes) == total and sorted(readers) == sorted(planted)
    assert [row["id"] for row in preview] == [row["id"] for row in receipt] == ids
    # The preview counts the proposal that cites two sources under each of them, as a lookup for each source did. The
    # receipt counts it under the first, whose scrub redacts it before the last looks.
    assert sum(row["candidate_memories"] for row in preview) == 14 and sum(row["candidate_memories"] for row in receipt) == 13
    assert all(row["memories_citing_replaced"] == [] for row in preview + receipt)
    for memory_id in planted:
        assert_gone(db, memory_id)



@pytest.mark.parametrize("cited", ["a delete of a source nobody cites", "a prune where no source is cited",
                                   "a prune where some sources are cited"])
def test_a_source_nobody_cites_is_not_looked_up_again(tmp_path, capsys, reads, cited):
    """The common case: most sources a delete or prune removes are cited by no memory. Such a source has an empty list
    of citing memories from the lookup for all of them, and an empty list is an answer: the scrub reads the memories
    of the list (none) and does not look again. A delete of a source nobody cites, a prune of six replaced sources none
    of which is cited, and a prune of six where two are cited each read every memory once for the preview and once for
    the receipt, and the reader is asked about the citing memories and no others.

    Mutations: treat an empty list as no list in the scrub (``citing_ids is None`` made ``not citing_ids`` in
    ``retire_dependents``: each source nobody cites is looked up again, and the receipt takes one pass more for each);
    hand the scrub no list for such a source (``citing_ids=[...]`` in ``run_sources`` made ``citing_ids=[...] or None``:
    the same).
    """

    if cited.startswith("a delete"):
        db, _folder_path, sid = seeded(tmp_path)
        ids, cited_ids = [sid], []
    else:
        db, ids = replaced_sources(tmp_path, 6)
        cited_ids = [ids[1], ids[4]] if cited.endswith("some sources are cited") else []
    add_filler(db, 40)
    planted = []
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        for index, sid in enumerate(cited_ids):
            planted.append(plant(store, ["source:" + sid.replace("-", "")], text=f"Pending proposal {index}."))
    total = memory_count(db)
    arguments = ("delete", ids[0]) if cited.startswith("a delete") else ("prune", "--superseded")
    passes, readers = reads
    assert command(db, *arguments) == 2
    preview = json.loads(capsys.readouterr().out)["would_delete"]
    assert len(passes) == total and sorted(readers) == sorted(planted)
    passes.clear()
    readers.clear()
    assert command(db, *arguments, "--yes") == 0
    receipt = json.loads(capsys.readouterr().out)["deleted"]
    assert len(passes) == total and sorted(readers) == sorted(planted)
    assert [row["id"] for row in preview] == [row["id"] for row in receipt] == ids
    for rows in (preview, receipt):
        assert [row["candidate_memories"] for row in rows] == [int(sid in cited_ids) for sid in ids]
    for memory_id in planted:
        assert_gone(db, memory_id)


def stable_state(db):
    """What the vault holds of every memory and revision that a scrub could change, without the clock."""

    with sqlite_user_connection(db, USER_ID) as conn:
        memories = [tuple(row.values()) for row in conn.execute(
            "SELECT id, status, canonical_text, value, title, summary FROM memories ORDER BY id").fetchall()]
        revisions = [tuple(row.values()) for row in conn.execute(
            "SELECT memory_id, sequence_no, action, previous_value, new_value, text_before, text_after "
            "FROM memory_revisions ORDER BY memory_id, sequence_no").fetchall()]
    return memories, revisions


def test_a_prune_of_many_sources_does_what_a_delete_of_each_source_does(tmp_path, capsys):
    """Eight replaced sources whose pending proposals cite them in different spellings, a proposal that cites the first
    two (so the scrub of the first redacts it before the second looks), an active memory that cites the last two, and a
    second user's proposal that cites the first. The same vault is pruned in one command and then source by source with
    ``sources delete`` in the order a prune takes them: the receipts are the same (what each source scrubbed and listed)
    and so is everything stored for every memory and revision. That a memory the first scrub redacted is not counted again
    by the second is the part a lookup made once for all the sources could get wrong.

    Mutations: count a memory that an earlier scrub redacted (the ``is_redacted_memory`` test of ``retire_dependents`` made
    ``True``: the second source counts the redacted proposal again, and the receipts differ); map every kept memory to the
    first source (``ids[index]`` made ``ids[0]`` in ``citing_memories_by_source``: the sources after the first list
    nothing).
    """

    db, ids = replaced_sources(tmp_path, 8)
    shapes = list(ALL_SHAPES.values())
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        for index, sid in enumerate(ids):
            plant(store, [shapes[index](sid)], text=f"Pending proposal {index}.")
        plant(store, [shapes[0](ids[0]), shapes[3](ids[1])], text="Pending proposal that cites two.")
        plant(store, [ids[-2], ids[-1].upper()], status="active", text="Active memory that cites two.")
    other_users = bystander(db, ids[0])
    single = tmp_path / "single.db"
    with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(single)) as copy:
        source.backup(copy)
    assert command(db, "prune", "--superseded", "--yes") == 0
    batch = json.loads(capsys.readouterr().out)["deleted"]
    one_by_one = []
    for sid in ids:
        assert command(single, "delete", sid, "--yes") == 0
        one_by_one += json.loads(capsys.readouterr().out)["deleted"]
    assert batch == one_by_one
    assert sum(row["candidate_memories"] for row in batch) == 9 and batch[0]["candidate_memories"] == 2
    assert batch[1]["candidate_memories"] == 1 and len(batch[-2]["memories_citing_replaced"]) == 1
    assert stable_state(db) == stable_state(single)
    assert_bystander_untouched(db, other_users)


def test_the_lookup_for_many_sources_gives_each_the_answer_it_gives_alone():
    """Ten sources, memories that cite one of them, two of them, none, by an event, and by text in a spelling that holds a
    source's digits without naming it. One lookup for all ten returns for each source the memories the
    reader and the links name for it, which are the ones a lookup for that source alone returns.

    Mutations: map every kept memory to the first source (``ids[index]`` made ``ids[0]`` in ``citing_memories_by_source``:
    the others list nothing). The provenance link branches of the same function are held by the source suites
    (``test_chunk_only_links_are_counted_and_scrubbed``, ``test_replace_rejects_candidate_with_only_a_chunk_link`` and
    the retained status tests).
    """

    store = memory_store()
    ids = [str(uuid4()) for _ in range(10)]
    expected = {sid: set() for sid in ids}
    for index, sid in enumerate(ids):
        expected[sid].add(insert_memory(store, metadata={"source_refs": [sid.upper() if index % 2 else "source:" + sid]}))
    both = insert_memory(store, metadata={"source_refs": [ids[2], ids[7].replace("-", "")]})
    expected[ids[2]].add(both)
    expected[ids[7]].add(both)
    by_event = insert_memory(store, events=[ids[4], ids[5]])
    expected[ids[4]].add(by_event)
    expected[ids[5]].add(by_event)
    insert_memory(store, metadata={"source_refs": [str(uuid4())]})
    insert_memory(store, metadata={"source_refs": ["ab" + ids[3].replace("-", "")]})
    insert_memory(store, metadata={"source_refs": ["memory:" + ids[6]]})
    insert_memory(store, metadata={"provenance": {"quote": ids[8], "source_id": str(uuid4())}})
    found_together = citing_memories_by_source(store, ids)
    assert list(found_together) == ids
    for sid in ids:
        assert {str(row["id"]) for row in found_together[sid]} == expected[sid], sid
        assert [str(row["id"]) for row in found_together[sid]] == sorted(expected[sid])
        assert found_together[sid] == citing_memories(store, sid)
    assert citing_memories_by_source(store, []) == {}


def test_source_event_ids_and_a_string_that_is_no_id_are_matched_as_written():
    """The source event ids are matched by the id as written, and the stored list is read as the list it is: an id behind
    a JSON escape is the id. A string that is no id has no digits to look for in a text, so no text names it and its
    presence does not make the lookup keep every memory, but a list of source event ids that holds it is a citation.

    Mutations: take the backslash rule out of ``_citation_hits`` (``"\\\\" in events`` made false: the escaped list is
    missed); search the list for another spelling (``source_id in events`` made ``source_id.upper() in events``: the plain
    list is missed); let a source with no digits into the text search (``_Probes.digits`` made to hold every source:
    the lookup fails on ``None``).
    """

    store = memory_store()
    sid = str(uuid4())
    plain = insert_memory(store, events=[sid])
    escaped_id = "".join("\\u%04x" % ord(char) if index % 3 == 0 else char for index, char in enumerate(sid))
    escaped = str(uuid4())
    store.conn.execute(
        "INSERT INTO memories (id, user_id, memory_key, value, status, source_event_ids, metadata_json, canonical_text) "
        "VALUES (?, ?, ?, '{}', 'candidate', ?, '{}', 'A record.')", (escaped, USER_ID, "key." + escaped, '["' + escaped_id + '"]'))
    other_event = insert_memory(store, events=[str(uuid4())])
    assert found(store, sid) == {plain, escaped}
    assert other_event not in found(store, sid)
    no_id = "not an id"
    by_event = insert_memory(store, events=[no_id])
    in_text = insert_memory(store, metadata={"source_refs": [no_id], "note": "not an id"})
    result = citing_memories_by_source(store, [no_id, sid])
    assert [str(row["id"]) for row in result[no_id]] == [by_event] and in_text not in {str(row["id"]) for row in result[no_id]}
    assert {str(row["id"]) for row in result[sid]} == {plain, escaped}


def test_the_pass_reads_only_the_memories_of_the_user(reads):
    """A second user of the vault has memories that hold the source id in every spelling. The pass is never asked about
    one of them: its metadata is never handed to the function. (A memory of another user that came out of the pass would
    be dropped by the read by id below, so only this test sees the first fence alone.)

    Mutation: take ``AND m.user_id = ?`` out of the pass (``WHERE m.user_id = ?`` made ``WHERE ? IS NOT NULL``: the
    second user's memories are handed to the function).
    """

    passes, _readers = reads
    store = memory_store()
    sid = str(uuid4())
    own = insert_memory(store, metadata={"source_refs": [sid.upper()]})
    ensure_sqlite_user(store.conn, OTHER_USER_ID, "bystander@example.com")
    for index, spelled in enumerate((sid, sid.upper(), sid.replace("-", ""), "source:" + sid, in_script(sid, DIGIT_SCRIPTS["fullwidth"]))):
        store.conn.execute(
            "INSERT INTO memories (id, user_id, memory_key, value, status, source_event_ids, metadata_json, canonical_text) "
            "VALUES (?, ?, ?, '{}', 'candidate', ?, ?, 'A record.')",
            (str(uuid4()), OTHER_USER_ID, f"other.{index}", json.dumps([sid]),
             json.dumps({"marker": "second-user", "source_refs": [spelled]}, ensure_ascii=False)))
    assert found(store, sid) == {own}
    assert passes and not any("second-user" in metadata for metadata in passes)


def test_a_read_by_id_returns_only_the_memories_of_the_user():
    """The read of the memories the pass kept holds the second fence on its own: asked for the ids of both users, it
    returns the user's.

    Mutation: take ``m.user_id = ?`` out of ``_MEMORIES_BY_ID`` (``m.user_id = ? AND`` made ``? IS NOT NULL AND``: the
    second user's memory comes back).
    """

    store = memory_store()
    own = insert_memory(store, metadata={"source_refs": ["x"]})
    ensure_sqlite_user(store.conn, OTHER_USER_ID, "bystander@example.com")
    other = str(uuid4())
    store.conn.execute(
        "INSERT INTO memories (id, user_id, memory_key, value, status, source_event_ids, metadata_json, canonical_text) "
        "VALUES (?, ?, ?, '{}', 'candidate', '[]', '{}', 'A record.')", (other, OTHER_USER_ID, "key." + other))
    assert [str(row["id"]) for row in _memories_by_id(store, [other, own])] == [own]
    assert _memories_by_id(store, []) == [] and _memories_by_id(store, [other]) == []


def test_the_function_is_registered_once_and_survives_a_statement_that_is_still_open():
    """SQLite will not replace a registered function while any statement of the connection is active, so the lookup
    registers ``alice_citation_hits`` once per connection and never again. A caller that left a cursor half read on the
    connection does not stop the next lookup, and the function is on the connection once.

    Mutation: register the function on every lookup (``if not registered:`` made ``if True:`` in
    ``_ensure_citation_function``: the second lookup fails with "Error creating function").
    """

    store = memory_store()
    sid = str(uuid4())
    cited = insert_memory(store, metadata={"source_refs": [sid]})
    half_read = store.conn.execute("SELECT id FROM memories")
    half_read.fetchone()
    assert found(store, sid) == {cited}
    assert found(store, sid) == {cited}
    registered = store.conn.execute("SELECT count(*) FROM pragma_function_list WHERE name = 'alice_citation_hits'").fetchone()
    assert registered[0] == 1
    half_read.close()


def test_a_lookup_leaves_no_pass_behind_when_it_ends_or_fails(monkeypatch):
    """Each lookup states what it looks for under a number that the registered function reads, and takes it out when it
    ends, so the registry does not grow with every delete and prune a long running server makes. It is taken out when
    the pass fails too.

    Mutation: leave the entry in the registry (the ``del _RUNNING_PASSES[pass_number]`` of ``_citation_candidates``
    removed: an entry stays after each lookup).
    """

    store = memory_store()
    sid = str(uuid4())
    cited = insert_memory(store, metadata={"source_refs": [sid]})
    assert source_retirement._RUNNING_PASSES == {}
    assert found(store, sid) == {cited}
    assert citing_memories_by_source(store, [sid, str(uuid4())])[sid][0]["id"] == cited
    assert source_retirement._RUNNING_PASSES == {}

    def failing(*_args):
        raise RuntimeError("the pass failed")

    monkeypatch.setattr(source_retirement, "_citation_hits", failing)
    with pytest.raises(sqlite3.OperationalError):
        found(store, sid)
    assert source_retirement._RUNNING_PASSES == {}
