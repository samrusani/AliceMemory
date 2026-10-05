"""An open loop that names a source only in its metadata is scrubbed with the source, on SQLite.

``list_open_loops_referencing_source`` (the reader) finds a loop by its ``source_id`` column or by a source id or a
``source:<id>`` text under one of the keys of ``SOURCE_REFERENCE_KEYS`` anywhere in its metadata. The delete preview, the
scrub and the replacement path (``import-markdown --supersede``) once counted and blanked the column only, so a loop with
a NULL column and the id in its metadata kept its title, description and metadata, and they reached a new export. All
three now use the one rule of the reader (``open_loop_source_reference_sql``, in ``vnext_stores/sqlite/open_loop_source_reference.py``).

Mutations, each alone, each named by the test that fails:

* ``retire_dependents`` blanks by the ``source_id`` column only (the old statement): the cases that delete and
  replace fail (``test_delete_blanks_every_loop_the_reader_names``, ``test_replacement_blanks_every_loop_the_reader_names``,
  ``test_each_key_and_shape_is_blanked_when_it_is_the_only_reference``), and so does the one-rule test.
* the preview counts the column only: ``test_delete_blanks_every_loop_the_reader_names`` fails on the preview count.
* ``blank_open_loops`` drops the ``user_id`` filter: ``test_another_users_loop_that_names_the_source_is_left_alone``.
* ``blank_open_loops`` stops clearing ``description``, ``resolution_note`` or ``metadata_json``: the delete and
  replacement tests fail on the blank row.
* the shared SQL drops a key (``selected_source_ids``), the ``source:`` spelling or the column test: the per-key cases,
  ``test_every_case_is_a_loop_the_reader_names`` and ``test_the_shared_rule_reads_exactly_the_reference_keys`` fail.
* ``list_open_loops_referencing_source`` goes back to its own copy of the rule, and the copy drops a key:
  ``test_the_scrub_matches_exactly_what_the_reader_lists`` fails, and so does the one-rule test.
* the preview count drops its ``user_id`` filter: ``test_another_users_loop_that_names_the_source_is_left_alone`` fails on
  the preview count (the other user's loop names the source).
* the scrub blanks only loops that are still ``open``: the delete and replacement tests fail on the resolved and the
  dismissed loops, which name the source only in metadata (or, for one resolved loop, by the column).
* the scrub keeps the old ``closed_at``, ``resolved_at`` or ``updated_at`` of a loop it blanks: the delete and
  replacement tests fail in ``_assert_scrubbed``, which reads each stamp before and after.
* a caller builds its own statement again (an f-string over the rule): ``test_the_lookup_the_preview_and_the_scrub_share_one_rule``.
"""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import closing
from uuid import UUID, uuid4

import pytest

from alicebot_api.onramp import _write_export, main as cli
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_source_fence import SOURCE_REFERENCE_KEYS
from alicebot_api.vnext_stores.sqlite.source_retirement import REMOVAL_MARKER
from tests.unit.test_importer_per_file_savepoint import USER_ID, _folder, _read, _vault
from tests.unit.test_source_supersede import run_import

KEYS = sorted(SOURCE_REFERENCE_KEYS)
SHAPES = {
    "bare": lambda key, sid: {key: sid},
    "prefixed": lambda key, sid: {key: f"source:{sid}"},
    "nested": lambda key, sid: {"trace": {"inputs": [{"note": "x"}, {key: sid}]}},
}
CASES = [(key, shape) for key in KEYS for shape in SHAPES]


def _marker(key: str, shape: str) -> str:
    return f"zebracobalt{KEYS.index(key)}{list(SHAPES).index(shape)}"


def _command(db, *arguments):
    return cli(["sources", *arguments, "--db", str(db), "--user-id", USER_ID])


def _create_loop(store, marker, *, source_id=None, metadata=None):
    return str(
        store.create_open_loop(
            {
                "title": marker,
                "description": f"{marker} description",
                "resolution_note": None,
                "source_id": source_id,
                "status": "open",
                "metadata_json": {**(metadata or {}), "note": f"{marker} metadata"},
            }
        )["id"]
    )


OLD_STAMP = "2020-01-01T00:00:00.000000Z"
STAMPS = "closed_at, resolved_at, updated_at"


def _create_closed_loop(store, marker, *, status, source_id=None, metadata=None):
    """A resolved or dismissed loop, which carries ``resolved_at`` and ``closed_at`` from before the scrub."""

    return str(
        store.create_open_loop(
            {
                "title": marker,
                "description": f"{marker} description",
                "resolution_note": f"{marker} note",
                "source_id": source_id,
                "status": status,
                "resolved_at": OLD_STAMP,
                "closed_at": OLD_STAMP,
                "metadata_json": {**(metadata or {}), "note": f"{marker} metadata"},
            }
        )["id"]
    )


def _seed(db, sid):
    """One open loop for every key and shape, one by the column, a resolved and a dismissed loop that name the source
    only in metadata, a resolved one by the column, and three open loops that the source must not touch."""

    loops = {}
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        for key, shape in CASES:
            marker = _marker(key, shape)
            loops[marker] = _create_loop(store, marker, metadata=SHAPES[shape](key, sid))
        loops["zebracolumn"] = _create_loop(store, "zebracolumn", source_id=sid)
        loops["zebraresolved"] = _create_closed_loop(
            store, "zebraresolved", status="resolved", metadata={"source_id": sid}
        )
        loops["zebradismissed"] = _create_closed_loop(
            store, "zebradismissed", status="dismissed", metadata={"selected_source_ids": f"source:{sid}"}
        )
        loops["zebraresolvedcolumn"] = _create_closed_loop(
            store, "zebraresolvedcolumn", status="resolved", source_id=sid
        )
        controls = {
            "zebraother": _create_loop(store, "zebraother", metadata={"source_id": str(uuid4())}),
            "zebraplain": _create_loop(store, "zebraplain"),
            "zebraprose": _create_loop(store, "zebraprose", metadata={"unrelated": f"mentions {sid} in prose"}),
        }
    # Every loop was written within a moment of the scrub. Age them all to one fixed stamp, so a stamp the scrub fails to
    # write cannot look changed by the clock alone.
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE open_loops SET updated_at = ?", (OLD_STAMP,))
        conn.commit()
    return loops, controls


def _stamps(db) -> dict[str, tuple]:
    """``(closed_at, resolved_at, updated_at)`` of every loop, by id."""

    return {row[0]: tuple(row[1:]) for row in _read(db, f"SELECT id, {STAMPS} FROM open_loops")}


def _whole_database(db) -> str:
    """Every cell of every table, as text, so a marker anywhere in the file's tables is found."""

    pieces = []
    with closing(sqlite3.connect(db)) as conn:
        names = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        ]
        for name in names:
            try:
                pieces.append(json.dumps([list(row) for row in conn.execute(f'SELECT * FROM "{name}"')], default=str))
            except sqlite3.DatabaseError:
                continue
    return "\n".join(pieces).casefold()


def _export_text(db) -> str:
    stream = io.StringIO()
    _write_export(stream, db_path=db, user_id=UUID(USER_ID))
    return stream.getvalue().casefold()


def _titles(db):
    return dict(_read(db, "SELECT title, id FROM open_loops"))


def _assert_scrubbed(db, loops, controls, before):
    """``before`` is ``_stamps(db)`` taken after the seed: the scrub must write a new ``closed_at``, ``resolved_at`` and
    ``updated_at`` on every loop it blanks, and leave those of the controls as they were."""

    text = _whole_database(db)
    export = _export_text(db)
    for marker in loops:
        assert marker not in text, marker
        assert marker not in export, marker
    titles = _titles(db)
    for marker in controls:
        assert marker in titles, marker
    after = _stamps(db)
    for marker, loop_id in loops.items():
        row = _read(
            db,
            "SELECT title, description, resolution_note, status, metadata_json FROM open_loops WHERE id = '%s'" % loop_id,
        )[0]
        assert row == (REMOVAL_MARKER, REMOVAL_MARKER, REMOVAL_MARKER, "dismissed", "{}"), (marker, row)
        closed_at, resolved_at, updated_at = after[loop_id]
        old_closed, old_resolved, old_updated = before[loop_id]
        assert closed_at is not None and closed_at != old_closed, (marker, "closed_at", closed_at, old_closed)
        assert resolved_at is not None and resolved_at != old_resolved, (marker, "resolved_at", resolved_at, old_resolved)
        assert updated_at != old_updated and updated_at > OLD_STAMP, (marker, "updated_at", updated_at, old_updated)
    for marker, loop_id in controls.items():
        row = _read(db, "SELECT description, status FROM open_loops WHERE id = '%s'" % loop_id)[0]
        assert row == (f"{marker} description", "open"), (marker, row)
        assert after[loop_id] == before[loop_id], (marker, "a loop that does not name the source was touched")


def test_every_case_is_a_loop_the_reader_names(tmp_path):
    """The cases below are the reader's own rule: it finds each of them and none of the controls."""

    db = _vault(tmp_path)
    sid = run_import(db, _folder(tmp_path, note="The cobalt door opens on Monday.")).source_ids[0]
    loops, controls = _seed(db, sid)
    with sqlite_user_connection(db, USER_ID) as conn:
        found = {str(row["id"]) for row in SQLiteVNextStore(conn, USER_ID).list_open_loops_referencing_source(source_id=sid)}
    assert found == set(loops.values())
    assert found.isdisjoint(controls.values())
    assert len(loops) == len(KEYS) * len(SHAPES) + 4


def test_delete_blanks_every_loop_the_reader_names(tmp_path, capsys):
    db = _vault(tmp_path)
    sid = run_import(db, _folder(tmp_path, note="The cobalt door opens on Monday.")).source_ids[0]
    loops, controls = _seed(db, sid)
    before = _stamps(db)
    assert _command(db, "delete", sid) == 2
    preview = json.loads(capsys.readouterr().out)["would_delete"][0]
    assert preview["open_loops"] == len(loops)
    assert _command(db, "delete", sid, "--yes") == 0
    receipt = json.loads(capsys.readouterr().out)["deleted"][0]
    assert receipt["open_loops"] == len(loops) == preview["open_loops"]
    _assert_scrubbed(db, loops, controls, before)


def test_replacement_blanks_every_loop_the_reader_names(tmp_path):
    db = _vault(tmp_path)
    folder = _folder(tmp_path, note="The cobalt door opens on Monday.")
    sid = run_import(db, folder).source_ids[0]
    loops, controls = _seed(db, sid)
    before = _stamps(db)
    (folder / "note.md").write_text("The cobalt door opens on Friday.")
    replaced = run_import(db, folder, supersede=True)
    assert replaced.to_record()["superseded_count"] == 1
    payload = json.loads(
        _read(db, "SELECT payload_json FROM event_log WHERE event_type = 'source.superseded' ORDER BY rowid DESC")[0][0]
    )
    assert payload["open_loops"] == len(loops)
    _assert_scrubbed(db, loops, controls, before)


@pytest.mark.parametrize("path", ("delete", "replace"))
def test_a_resolved_and_a_dismissed_loop_named_only_in_metadata_are_counted_and_blanked(tmp_path, capsys, path):
    """A loop that is no longer open still carries the text, so the scrub reaches every status. Two loops, one resolved
    and one dismissed, with an empty column and the id only in metadata: both are in the preview and in the receipt (or
    the replacement event), both lose their text, and each gets a new ``closed_at``, ``resolved_at`` and ``updated_at``.

    Mutation: the scrub or the count is limited to ``open`` loops.
    """

    db = _vault(tmp_path)
    folder = _folder(tmp_path, note="The cobalt door opens on Monday.")
    sid = run_import(db, folder).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        loops = {
            "zebraresolvedonly": _create_closed_loop(
                store, "zebraresolvedonly", status="resolved", metadata={"source_refs": sid}
            ),
            "zebradismissedonly": _create_closed_loop(
                store, "zebradismissedonly", status="dismissed", metadata={"trace": {"source_id": f"source:{sid}"}}
            ),
        }
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE open_loops SET updated_at = ?", (OLD_STAMP,))
        conn.commit()
    assert _read(db, "SELECT status, source_id FROM open_loops ORDER BY title") == [("dismissed", None), ("resolved", None)]
    before = _stamps(db)
    if path == "delete":
        assert _command(db, "delete", sid) == 2
        assert json.loads(capsys.readouterr().out)["would_delete"][0]["open_loops"] == 2
        assert _command(db, "delete", sid, "--yes") == 0
        assert json.loads(capsys.readouterr().out)["deleted"][0]["open_loops"] == 2
    else:
        (folder / "note.md").write_text("The cobalt door opens on Friday.")
        run_import(db, folder, supersede=True)
        payload = json.loads(
            _read(db, "SELECT payload_json FROM event_log WHERE event_type = 'source.superseded' ORDER BY rowid DESC")[0][0]
        )
        assert payload["open_loops"] == 2
    _assert_scrubbed(db, loops, {}, before)


@pytest.mark.parametrize(("key", "shape"), CASES)
def test_each_key_and_shape_is_blanked_when_it_is_the_only_reference(tmp_path, capsys, key, shape):
    """One loop, named by one key in one shape and nowhere else (NULL column), deleted and then replaced."""

    marker = _marker(key, shape)
    for path in ("delete", "replace"):
        root = tmp_path / path
        root.mkdir()
        db = _vault(root)
        folder = _folder(root, note="The cobalt door opens on Monday.")
        sid = run_import(db, folder).source_ids[0]
        with sqlite_user_connection(db, USER_ID) as conn:
            loop = _create_loop(SQLiteVNextStore(conn, USER_ID), marker, metadata=SHAPES[shape](key, sid))
        assert _read(db, "SELECT source_id FROM open_loops") == [(None,)]
        if path == "delete":
            assert _command(db, "delete", sid) == 2
            assert json.loads(capsys.readouterr().out)["would_delete"][0]["open_loops"] == 1
            assert _command(db, "delete", sid, "--yes") == 0
            assert json.loads(capsys.readouterr().out)["deleted"][0]["open_loops"] == 1
        else:
            (folder / "note.md").write_text("The cobalt door opens on Friday.")
            run_import(db, folder, supersede=True)
        assert marker not in _whole_database(db) and marker not in _export_text(db), (path, key, shape)
        assert _read(db, "SELECT title, metadata_json FROM open_loops WHERE id = '%s'" % loop) == [(REMOVAL_MARKER, "{}")]


def test_another_users_loop_that_names_the_source_is_left_alone(tmp_path, capsys):
    """The preview counts, and the scrub blanks, the loops of the acting user only.

    Mutation: ``source_open_loop_count`` drops its ``user_id`` filter (the preview then counts the neighbour's loop), or
    ``blank_open_loops`` does (the neighbour's loop is blanked).
    """

    db = _vault(tmp_path)
    sid = run_import(db, _folder(tmp_path, note="The cobalt door opens on Monday.")).source_ids[0]
    other = str(uuid4())
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("INSERT INTO users (id, email) VALUES (?, ?)", (other, "other@alice"))
        conn.execute(
            "INSERT INTO open_loops (id, user_id, title, status, metadata_json) VALUES (?, ?, ?, 'open', ?)",
            (str(uuid4()), other, "zebraneighbour", json.dumps({"source_id": sid})),
        )
        conn.commit()
    with sqlite_user_connection(db, USER_ID) as conn:
        _create_loop(SQLiteVNextStore(conn, USER_ID), "zebramine", metadata={"source_id": sid})
    neighbour = _read(db, "SELECT * FROM open_loops WHERE user_id = '%s'" % other)
    assert _command(db, "delete", sid) == 2
    assert json.loads(capsys.readouterr().out)["would_delete"][0]["open_loops"] == 1
    assert _command(db, "delete", sid, "--yes") == 0
    assert json.loads(capsys.readouterr().out)["deleted"][0]["open_loops"] == 1
    assert _read(db, "SELECT * FROM open_loops WHERE user_id = '%s'" % other) == neighbour
    assert _read(db, "SELECT title FROM open_loops WHERE user_id = '%s'" % other) == [("zebraneighbour",)]
    assert _read(db, "SELECT title FROM open_loops WHERE user_id = '%s'" % USER_ID) == [(REMOVAL_MARKER,)]


# Spellings other than the id as stored or ``source:<id>``. The rule of the open-loop reader compares the text under a key
# to those two, exactly, so none of these is read by the reader or the scrub, whatever any other reader does with them
# (the shared reference reader of memories and saved quotes, ``cited_source_ids``, reads many). The scrub follows the
# reader and no further.
UNREAD_SPELLINGS = {
    "upper": lambda sid: {"source_id": sid.upper()},
    "compact": lambda sid: {"source_id": sid.replace("-", "")},
    "upper_prefix": lambda sid: {"source_id": f"SOURCE:{sid}"},
    "in_a_list": lambda sid: {"source_ids": [sid]},
    "json_text": lambda sid: {"source_refs": json.dumps({"source_id": sid})},
    "braced": lambda sid: {"source_id": "{" + sid + "}"},
    "urn": lambda sid: {"source_id": f"urn:uuid:{sid}"},
    "spaced": lambda sid: {"source_id": f" {sid} "},
}


def test_the_scrub_matches_exactly_what_the_reader_lists(tmp_path, capsys):
    """One rule, not two: a loop is blanked by a delete if and only if the reader lists it for the source."""

    db = _vault(tmp_path)
    sid = run_import(db, _folder(tmp_path, note="The cobalt door opens on Monday.")).source_ids[0]
    ids = {}
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        for name, metadata in UNREAD_SPELLINGS.items():
            ids[name] = _create_loop(store, f"zebra{name}", metadata=metadata(sid))
        read = {key: _create_loop(store, f"zebrakey{index}", metadata={key: sid}) for index, key in enumerate(KEYS)}
        listed = {str(row["id"]) for row in store.list_open_loops_referencing_source(source_id=sid)}
    assert _command(db, "delete", sid, "--yes") == 0
    receipt = json.loads(capsys.readouterr().out)["deleted"][0]
    blanked = {loop for loop, in _read(db, "SELECT id FROM open_loops WHERE title = '%s'" % REMOVAL_MARKER)}
    assert blanked == listed == set(read.values())
    assert receipt["open_loops"] == len(listed) == len(KEYS)


def _code(function):
    """``(names, strings, has_fstring)`` of the code of ``function``, without its docstring: the names it uses, the string
    constants it holds and whether it builds a string with an f-string. A docstring that mentions the rule cannot stand
    in for code that uses it."""

    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    names, strings, fstring = set(), [], False
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                strings.append(node.value)
            elif isinstance(node, ast.JoinedStr):
                fstring = True
    return names, strings, fstring


def test_the_lookup_the_preview_and_the_scrub_share_one_rule():
    """No second copy of the rule can drift: the callers hold no statement of their own about a loop's metadata, and the
    two statements of the preview and the scrub are the fixed text around the one condition.

    Mutation: give any caller a private ``json_tree`` condition, a ``source_id =`` column test or a statement of its own,
    or build one with an f-string over the rule at the call site.
    """

    from alicebot_api import source_commands
    from alicebot_api.vnext_stores.sqlite import graph_open_loops, open_loop_source_reference as rule, source_retirement

    expected = {
        graph_open_loops.list_open_loops_referencing_source: {"open_loop_source_reference_sql"},
        source_retirement.source_open_loop_count: {"OPEN_LOOP_SOURCE_COUNT_SQL", "open_loop_source_reference_params"},
        source_retirement.blank_open_loops: {"OPEN_LOOP_SOURCE_BLANK_SQL", "open_loop_source_reference_params"},
    }
    for function, uses in expected.items():
        names, strings, fstring = _code(function)
        assert uses <= names, (function.__name__, uses - names)
        text = "\n".join(strings)
        assert "json_tree" not in text and "source_id =" not in text, function.__name__
        if function is not graph_open_loops.list_open_loops_referencing_source:
            # The reader keeps its column list in its own f-string; the preview and the scrub build no SQL at all.
            assert not fstring and "open_loops" not in text, function.__name__
    # The two statements are the one condition inside fixed text, with the user test before it.
    for statement in (rule.OPEN_LOOP_SOURCE_COUNT_SQL, rule.OPEN_LOOP_SOURCE_BLANK_SQL):
        assert statement.count(rule.OPEN_LOOP_SOURCE_REFERENCE_SQL) == 1
        assert statement.index("user_id = ?") < statement.index(rule.OPEN_LOOP_SOURCE_REFERENCE_SQL)
    names, strings, _ = _code(source_retirement.retire_dependents)
    assert "blank_open_loops" in names and not any("UPDATE open_loops" in text for text in strings)
    names, strings, _ = _code(source_commands._preview)
    assert "source_open_loop_count" in names and not any("FROM open_loops" in text for text in strings)


def test_the_shared_rule_reads_exactly_the_reference_keys():
    """The keys of the shared statement are the keys of ``SOURCE_REFERENCE_KEYS``, no fewer and no more.

    Mutation: drop a key from the statement, or add a seventh to it.
    """

    from alicebot_api.vnext_stores.sqlite.open_loop_source_reference import (
        OPEN_LOOP_SOURCE_REFERENCE_SQL,
        open_loop_source_reference_sql,
    )

    text = OPEN_LOOP_SOURCE_REFERENCE_SQL
    inside = text[text.index("ref.key IN (") + len("ref.key IN (") : text.index(")", text.index("ref.key IN ("))]
    assert {part.strip().strip("'") for part in inside.split(",")} == SOURCE_REFERENCE_KEYS
    sql, params = open_loop_source_reference_sql(UUID(int=5))
    assert sql == text and params == (str(UUID(int=5)), str(UUID(int=5)), f"source:{UUID(int=5)}")
