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

Every test below goes through the real door the probe used (an agent proposal through the MCP handler) or plants the same
row shape in the vault, then runs the real importer or the real ``sources`` commands.
"""

from __future__ import annotations

import json
import random
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
from alicebot_api.vnext_stores.sqlite.source_retirement import _holds_digits_in_other_script, citing_memories
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
    compared). In the SQL of the narrowing: skip the text half of ``citing_memories`` (``if canonical is not None:`` made
    ``if False:``: every case fails); drop ``lower(`` from the searched text (the cases ``upper case``, ``selected ids``
    and ``fullwidth digits`` fail); stop removing hyphens from it (the ten cases whose spelling has hyphens fail); search
    for the hyphenated id in the raw text, so the compact form is gone (the case ``no hyphens`` fails); make
    ``retire_dependents`` read no memories (every case here and in the delete and prune tests fails); drop both user
    conditions of the text query (the ``m.user_id = ?`` of the ``WITH`` clause and the one of the join: every case fails,
    because the second user's memory is rejected and listed). The two conditions say the same thing, since a memory id is
    a primary key, so dropping only one of them changes nothing a test can see. For the digits of other scripts: make
    ``_holds_digits_in_other_script`` return ``0``, or take its condition out of the query (the eight cases that write
    the id in other digits fail); narrow its digit class to the Arabic-Indic digits (the six cases in the other scripts
    fail); stop reading the pair of escapes JSON writes for a character beyond U+FFFF (the case with escaped
    mathematical digits fails); stop collapsing runs of backslashes, stop decoding escapes, or skip every text that has an
    escape (the two cases with a JSON ref of escaped digits fail).
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

    Mutations: the ones of the replacement test, and ``_preview`` in ``source_commands.py`` reading only the provenance
    links (``citing_memories`` swapped for a query on ``provenance_links``: every case of this test and of the prune test
    fails on the preview, and the receipt, which scrubs through ``retire_dependents``, is not what fails).
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

    Mutations: make the check ``canonical in memory_cited_source_ids(row)`` always true (the memory ref, the longer run
    and the quote rows are found, in fullwidth digits too: the narrowing keeps those rows and the reader is what rejects
    them); read quotes and excerpts as references (``_TEXT_KEYS`` made empty: the quote and excerpt rows are found); skip
    the JSON decode of the reader (``_json_container`` returning ``None``: the JSON quote rows are found).
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
    compared): drop the Python confirm (the longer run of digits, the memory ref and the quote rows are found); drop
    ``lower(`` (the upper case rows are missed); stop removing hyphens (every hyphenated row is missed); stop removing
    underscores (the zero spellings with an underscore are missed); stop removing ``urn:`` (the spelling with ``urn:``
    inside the digits is missed); stop removing ``uuid:`` (the spelling with ``uuid:`` inside the digits is missed);
    search for the digits with their zeros (``.lstrip('0')`` removed: the zero spellings where whitespace, ``0x``
    or a sign takes the place of the zeros are missed); drop the escape test (the JSON text with escapes is missed);
    narrow the escape test to the escapes of digits (``\\u003[0-9]``: the hex letters and hyphens containers are
    missed); narrow it to ``\\u00[3-7]`` (the hyphens container), to ``\\u00[2-6]`` (the ``u`` and ``r`` container) or to
    ``\\u00[2-7][0-9]`` (the hyphens container: ``\\u002d``); take the metadata out of the searched text; read only the
    reader's own fields in ``memory_cited_source_ids``. The mutations of the digits of other scripts are in the tests below. In
    ``vnext_source_fence.py``: skip the JSON decode (``_json_container`` returning ``None``: the JSON text with escapes
    is missed and the JSON quote row is found); read quotes as references (``_TEXT_KEYS`` made empty).
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
    for source_id in ids:
        returned = found(store, source_id)
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


def test_the_digit_test_reads_the_characters_and_escapes_that_int_reads_as_digits():
    """The part of the narrowing that sees an id written in other scripts (``_holds_digits_in_other_script``), checked on
    every code point up to U+2FFFF (the Unicode decimal digits all lie below it) and a sample of the rest: a character
    that ``int(..., 16)`` reads as a digit, written as it is or as the JSON escape ``json.dumps`` writes for it (one
    escape, or a pair for a character beyond U+FFFF), is found when the text holds the digit it stands for, and is not
    found for any other digit; a character it does not read as a digit never is.

    Mutations: take the digit class to the Arabic-Indic digits alone (``_OTHER_SCRIPT_DIGIT`` made ``[\\u0660-\\u0669]``:
    every other script fails); stop reading the pair of escapes (the first alternative of ``_JSON_ESCAPE`` made
    ``(?!)``: the mathematical digits fail in their escaped form); map every digit to ``0`` (``str(int(...))`` made
    ``'0'``: the found-for-its-own-digit check fails).
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
            for spelled in (char, escape, "x" + char + "x"):
                assert _holds_digits_in_other_script(spelled, str(digit)) == 1, hex(code)
                assert _holds_digits_in_other_script(spelled, str((digit + 1) % 10)) == 0, hex(code)
        else:
            for spelled in (char, escape):
                assert _holds_digits_in_other_script(spelled, "1") == 0 and _holds_digits_in_other_script(spelled, "") == 0, hex(code)
    assert _holds_digits_in_other_script("١", "1") == 1 and _holds_digits_in_other_script("\\ud835\\udfce", "0") == 1


def test_the_digit_test_reads_nested_escapes_and_ignores_what_the_id_ignores():
    """The same function on the texts the rest of the lookup hands it: escapes at any depth of JSON text (the pair
    ``json.dumps`` writes for the mathematical digits has its backslashes doubled at each depth), what ``uuid.UUID``
    ignores inside an id (``urn:``, ``uuid:``, hyphens, underscores) taken out, text that is ASCII with no escape never
    found, and an escape that is not a digit never found.

    Mutations: skip the collapse of backslash runs (``_BACKSLASH_RUN.sub(...)`` taken out: the nested pairs fail); take
    ``"-"`` out of the ignored characters (the hyphen cases fail); take ``"uuid:"`` out of them (the ``uuid:`` case
    fails); return ``0`` for every text that holds an escape (the ``escaped`` guard made ``not escaped and
    text.isascii()`` into ``text.isascii()``: every escaped case fails).
    """

    mathematical = "\U0001d7ce\U0001d7cf\U0001d7d0"
    nested = json.dumps(mathematical)
    for depth in range(1, 6):
        assert _holds_digits_in_other_script(nested, "012") == 1, depth
        nested = json.dumps(nested)
    assert _holds_digits_in_other_script("a١-b٢_c٣", "a1b2c3") == 1
    assert _holds_digits_in_other_script("urn:uuid:١٢", "12") == 1
    assert _holds_digits_in_other_script("١urn:٢uuid:٣", "123") == 1
    assert _holds_digits_in_other_script("\\u0661\\u0662", "12") == 1
    assert _holds_digits_in_other_script("ascii 12 only \\u00e9 and \\u0041", "12") == 0
    assert _holds_digits_in_other_script("plain ascii 123 without any escape", "123") == 0
    assert _holds_digits_in_other_script("١٢", "13") == 0
    assert _holds_digits_in_other_script(None, "1") == 0 and _holds_digits_in_other_script("١", None) == 0


# -- cost -----------------------------------------------------------------------------------------------------------


def test_delete_reads_only_the_candidates_and_stays_fast_on_a_large_vault(tmp_path, monkeypatch, capsys):
    """A vault of 10,000 memories, two of which cite the source (one in fullwidth digits). Half of the others are written
    in a language with digits of its own: dates and refs to other sources in Arabic-Indic and fullwidth digits. The delete
    finishes in under a second, and the reader is asked about the memories the SQL narrowing keeps (the two, not the vault
    and not the half that holds digits of another script). The count of reads is the guard that a slower machine cannot
    flatter: on a development machine the whole delete takes about a third of a second, and reading all 10,000
    memories in Python takes about half a second on its own, so the time alone would not fail a lookup that read
    everything.

    Mutations: drop the text condition of the second query in ``citing_memories`` (``(? IS NOT NULL OR ? IS NOT NULL)``
    in its place: every memory is a candidate): the reader is asked about all 10,000 memories and the count fails. Keep
    every memory that holds a digit of another script, whatever the id (the last line of ``_holds_digits_in_other_script``
    made ``return 1``): the reader is asked about the 5,000 of them and the count fails.
    """

    db, _folder_path, sid = seeded(tmp_path)
    arabic, fullwidth = DIGIT_SCRIPTS["arabic-indic"], DIGIT_SCRIPTS["fullwidth"]
    rows = []
    for index in range(10_000):
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
        store = SQLiteVNextStore(conn, USER_ID)
        candidate = plant(store, [in_script(sid.upper(), fullwidth)])
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
