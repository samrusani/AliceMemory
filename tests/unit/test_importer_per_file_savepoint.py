"""P2a: each file of an import is one unit that lands whole or leaves nothing.

Before this change a chunk write that failed part way left the file live with
some of its chunks, and a second import of the fixed file reported it as a
duplicate for good. Every test here names the change that must fail it.

Postgres has no server in the unit job. Its savepoint is checked here against a
connection that records the SQL it is sent, and by the integration twin in
``tests/integration/test_vnext_import_markdown_postgres.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import closing
from io import StringIO
import json
from pathlib import Path
import sqlite3
import sys

import psycopg
import pytest

from alicebot_api.onramp import bootstrap_database, main as onramp_main, resolve_db_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_capture import VNextCaptureService, VNextCaptureStore, chunk_text
from alicebot_api.vnext_store import PostgresVNextStore

from tests.unit.test_vnext_capture import InMemoryVNextCaptureStore


USER_ID = "00000000-0000-0000-0000-000000000001"
COUNTED_TABLES = (
    "sources",
    "source_chunks",
    "memories",
    "memory_revisions",
    "provenance_links",
    "graph_edges",
    "open_loops",
    "vnext_entities",
    "entity_relationship_events",
)


def _note(word: str, sections: int = 4) -> str:
    """A Markdown note whose headings give it one chunk per section."""
    return (
        "\n\n".join(
            f"## Section {index} of {word}\nFact: {word} section {index} holds one durable claim about the {word} plan."
            for index in range(sections)
        )
        + "\n"
    )


def _vault(tmp_path: Path) -> Path:
    database = resolve_db_path(data_dir=str(tmp_path / "data"), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return database


def _folder(tmp_path: Path, **notes: str) -> Path:
    folder = tmp_path / "notes"
    folder.mkdir(exist_ok=True)
    for name, text in notes.items():
        (folder / f"{name}.md").write_text(text, encoding="utf-8")
    return folder


def _import(database: Path, folder: Path) -> object:
    with sqlite_user_connection(database, USER_ID) as conn:
        return VNextCaptureService(SQLiteVNextStore(conn, USER_ID)).import_markdown_folder(folder)


def _read(database: Path, sql: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(database)) as conn:
        return [tuple(row) for row in conn.execute(sql).fetchall()]


def _counts(database: Path) -> dict[str, int]:
    return {table: int(_read(database, f"SELECT count(*) FROM {table}")[0][0]) for table in COUNTED_TABLES}


def _event_types(database: Path) -> dict[str, int]:
    return {
        str(name): int(count)
        for name, count in _read(database, "SELECT event_type, count(*) FROM event_log GROUP BY event_type")
    }


def _chunks_by_title(database: Path) -> dict[str, int]:
    return {
        str(title): int(count)
        for title, count in _read(
            database,
            "SELECT s.title, count(c.id) FROM sources s LEFT JOIN source_chunks c ON c.source_id = s.id "
            "WHERE s.deleted_at IS NULL GROUP BY s.id",
        )
    }


def _failure_metadata(database: Path) -> list[dict[str, object]]:
    """The ``metadata_json`` of every stored ``source.import_failed`` event."""
    return [
        json.loads(str(payload))["metadata_json"]
        for (payload,) in _read(database, "SELECT payload_json FROM event_log WHERE event_type = 'source.import_failed'")
    ]


def _fail_chunk_write(monkeypatch: pytest.MonkeyPatch, marker: str, *, chunk_index: int = 1):
    """Make the chunk write of one file fail on its second chunk. Returns the undo."""
    real = SQLiteVNextStore.create_source_chunk

    def flaky(self, chunk, **kwargs):  # type: ignore[no-untyped-def]
        if chunk["chunk_index"] == chunk_index and marker in chunk["text"]:
            raise sqlite3.OperationalError("injected chunk write failure")
        return real(self, chunk, **kwargs)

    monkeypatch.setattr(SQLiteVNextStore, "create_source_chunk", flaky)
    return lambda: monkeypatch.setattr(SQLiteVNextStore, "create_source_chunk", real)


def test_a_failed_chunk_write_leaves_the_vault_as_if_the_file_was_never_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TS1. The failed file leaves zero rows, and the other files import.

    The control vault imports only the two good files. The failed vault must
    hold the same number of rows in every counted table, and its events differ
    by the one failure event. Fails if the savepoint around one file is removed
    from ``import_markdown_folder``: the file then stays live with its first
    chunk, so the sources and chunks counts are one and one above the control.
    """

    assert len(chunk_text(_note("zeta"))) == 4
    control = _vault(tmp_path / "control")
    _import(control, _folder(tmp_path / "control", alpha=_note("alpha"), gamma=_note("gamma")))

    failed = _vault(tmp_path / "failed")
    folder = _folder(tmp_path / "failed", alpha=_note("alpha"), beta=_note("zeta"), gamma=_note("gamma"))
    _fail_chunk_write(monkeypatch, "zeta")
    result = _import(failed, folder)

    assert (result.status, result.imported_count, result.failed_count) == ("partial", 2, 1)
    assert _counts(failed) == _counts(control)
    assert _chunks_by_title(failed) == _chunks_by_title(control) == {"alpha": 4, "gamma": 4}
    extra = {
        name: _event_types(failed).get(name, 0) - _event_types(control).get(name, 0)
        for name in set(_event_types(failed)) | set(_event_types(control))
    }
    assert {name: count for name, count in extra.items() if count} == {"source.import_failed": 1}


def test_a_second_import_of_the_fixed_file_imports_it_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TS1. The retry after a failure is an import, not a duplicate.

    Fails if the savepoint is removed: the half-built file then matches its own
    content hash and the retry reports a duplicate with one chunk of four.
    """

    database = _vault(tmp_path)
    folder = _folder(tmp_path, alpha=_note("alpha"), beta=_note("zeta"))
    undo = _fail_chunk_write(monkeypatch, "zeta")
    first = _import(database, folder)
    undo()
    second = _import(database, folder)

    assert (first.status, first.failed_count) == ("partial", 1)
    assert (second.status, second.imported_count, second.duplicate_count, second.failed_count) == ("ok", 1, 1, 0)
    assert _chunks_by_title(database) == {"alpha": 4, "beta": 4}


def test_the_batch_is_still_one_commit(tmp_path: Path) -> None:
    """TS1. Nothing is committed until the batch ends, as before.

    A second connection reads while the batch connection is still open. Fails if
    ``SQLiteVNextStore.savepoint`` stops beginning the transaction when none is
    open: the first file's savepoint is then the outermost one, its release
    commits, and every file commits on its own, so the reader sees rows early.
    """

    database = _vault(tmp_path)
    folder = _folder(tmp_path, alpha=_note("alpha"), beta=_note("beta"), gamma=_note("gamma"))
    with sqlite_user_connection(database, USER_ID) as conn:
        result = VNextCaptureService(SQLiteVNextStore(conn, USER_ID)).import_markdown_folder(folder)
        visible_before_the_commit = _read(database, "SELECT count(*) FROM sources")[0][0]
    visible_after_the_commit = _read(database, "SELECT count(*) FROM sources")[0][0]

    assert result.imported_count == 3
    assert visible_before_the_commit == 0
    assert visible_after_the_commit == 3


def test_a_late_failure_rolls_back_entity_rows_and_the_counter_of_a_shared_entity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TS2, the rewrite-an-existing-row shape.

    The second file names a person the first file already stored, then fails
    after its entity links are written. The link bumps the person's mention
    count and adds an edge, an update of a row that was there before the file.
    Both must be undone. Fails if the savepoint is removed: the count reads 2
    and the failed file's edge stays.
    """

    database = _vault(tmp_path)
    folder = _folder(
        tmp_path,
        alpha="Fact: We met Jane Doe of Northwind Capital.\n",
        beta="Fact: Jane Doe confirmed the quarterly plan zeta.\n",
    )
    real = VNextCaptureService._link_captured_entities

    def link_then_fail(self, **kwargs):  # type: ignore[no-untyped-def]
        real(self, **kwargs)
        if "zeta" in kwargs["raw_text"]:
            raise RuntimeError("injected failure after the entity links")

    monkeypatch.setattr(VNextCaptureService, "_link_captured_entities", link_then_fail)
    result = _import(database, folder)

    assert (result.status, result.imported_count, result.failed_count) == ("partial", 1, 1)
    assert _read(database, "SELECT mention_count FROM vnext_entities WHERE normalized_name = 'jane doe'") == [(1,)]
    assert _read(database, "SELECT count(*) FROM graph_edges") == [(2,)]
    assert _chunks_by_title(database) == {"alpha": 1}


def test_a_failed_reimport_of_an_edited_file_leaves_the_old_version_live_and_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TS2, as far as it can go before a re-import retires anything.

    Replace on re-import is a later change, so the old version is only ever left
    alone here. The edit fails on its second chunk. The first version must keep
    all four chunks, and nothing of the edit may exist. Fails if the savepoint
    is removed: the edit is then live beside the old version with one chunk.
    The retire half of TS2 comes with the change that adds the retire, and the
    store level test below pins the shape it needs.
    """

    database = _vault(tmp_path)
    folder = _folder(tmp_path, notes=_note("alpha"))
    _import(database, folder)
    before = (_counts(database), _chunks_by_title(database))

    (folder / "notes.md").write_text(_note("zeta"), encoding="utf-8")
    _fail_chunk_write(monkeypatch, "zeta")
    result = _import(database, folder)

    assert (result.status, result.failed_count) == ("failed", 1)
    assert (_counts(database), _chunks_by_title(database)) == before
    assert _read(database, "SELECT count(*) FROM sources WHERE deleted_at IS NULL") == [(1,)]


def test_the_command_line_commits_the_good_files_and_none_of_the_failed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TS1 through ``alice-memory import-markdown``, the way a person runs it.

    Fails if the savepoint is removed: the vault then holds a source for the
    failed file after the command exits.
    """

    database = _vault(tmp_path)
    folder = _folder(tmp_path, alpha=_note("alpha"), beta=_note("zeta"), gamma=_note("gamma"))
    _fail_chunk_write(monkeypatch, "zeta")
    stdout = StringIO()
    with contextlib.redirect_stdout(stdout):
        code = onramp_main(["import-markdown", "--from", str(folder), "--db", str(database), "--user-id", USER_ID])

    receipt = json.loads(stdout.getvalue())
    assert code == 0
    assert (receipt["status"], receipt["imported_count"], receipt["failed_count"]) == ("partial", 2, 1)
    assert _chunks_by_title(database) == {"alpha": 4, "gamma": 4}


def test_the_failure_event_names_a_file_by_its_path_when_two_files_share_a_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one surviving failure event says which README failed.

    The capture's own event carried the path and is rolled back with the file,
    so the importer's event is the only record. Two files called README.md sit
    in different subfolders and the first fails. Fails if ``relative_path`` is
    dropped from the metadata the Markdown importer passes to ``_log_failure``:
    the event then holds only the folder and the bare name, which both files
    share.
    """

    database = _vault(tmp_path)
    folder = tmp_path / "notes"
    (folder / "a").mkdir(parents=True)
    (folder / "b").mkdir()
    (folder / "a" / "README.md").write_text(_note("zeta"), encoding="utf-8")
    (folder / "b" / "README.md").write_text(_note("alpha"), encoding="utf-8")
    _fail_chunk_write(monkeypatch, "zeta")
    result = _import(database, folder)

    assert (result.status, result.imported_count, result.failed_count) == ("partial", 1, 1)
    metadata = _failure_metadata(database)
    assert len(metadata) == 1
    assert metadata[0]["relative_path"] == "a/README.md"
    assert metadata[0]["title"] == "README.md"
    assert metadata[0]["folder"] == str(folder)


def _conversation(identifier: str, word: str) -> dict[str, object]:
    return {
        "id": identifier,
        "title": f"Chat about {word}",
        "messages": [
            {"author": {"role": "user"}, "content": {"parts": [f"Fact: {word} message {index} " + (word + " ") * 30]}}
            for index in range(6)
        ],
    }


def test_a_chatgpt_conversation_that_fails_part_way_leaves_nothing_and_imports_in_full_next_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TS1 for the ChatGPT importer, where one conversation is the unit.

    Fails if the savepoint is removed from ``import_chatgpt_export_file``: the
    failed conversation then stays live with one chunk and the retry reports a
    duplicate. It also pins the one failure event that survives the rollback:
    it fails if the importer's own ``_log_failure`` call is removed (no event is
    left, since the capture's is rolled back) or if it stops passing the
    conversation index and id (two chats can share a title).
    """

    database = _vault(tmp_path)
    export = tmp_path / "conversations.json"
    export.write_text(json.dumps([_conversation("c-1", "alpha"), _conversation("c-2", "zeta")]), encoding="utf-8")

    def run() -> object:
        with sqlite_user_connection(database, USER_ID) as conn:
            service = VNextCaptureService(SQLiteVNextStore(conn, USER_ID), chunk_max_chars=300)
            return service.import_chatgpt_export_file(export)

    undo = _fail_chunk_write(monkeypatch, "zeta")
    first = run()
    after_failure = _chunks_by_title(database)
    failure_metadata = _failure_metadata(database)
    undo()
    second = run()
    after_retry = _chunks_by_title(database)

    assert (first.status, first.imported_count, first.failed_count) == ("partial", 1, 1)
    assert list(after_failure) == ["Chat about alpha"]
    assert len(failure_metadata) == 1
    assert failure_metadata[0]["conversation_index"] == 2
    assert failure_metadata[0]["conversation_id"] == "c-2"
    assert failure_metadata[0]["title"] == "Chat about zeta"
    assert (second.status, second.imported_count, second.duplicate_count) == ("ok", 1, 1)
    assert set(after_retry) == {"Chat about alpha", "Chat about zeta"}
    assert after_retry["Chat about zeta"] == after_retry["Chat about alpha"] > 2


def test_each_file_and_each_conversation_runs_in_its_own_savepoint(tmp_path: Path) -> None:
    """The importers open one savepoint per unit, and only per unit.

    Counted on the shared in-memory store, which rolls back as the real ones do.
    Fails if the savepoint is removed from the Markdown importer (the first count
    is 0) or from the ChatGPT importer (the second count is 0).
    """

    folder = _folder(tmp_path, alpha=_note("alpha"), beta=_note("beta"), gamma=_note("gamma"))
    markdown_store = InMemoryVNextCaptureStore()
    VNextCaptureService(markdown_store).import_markdown_folder(folder)

    export = tmp_path / "conversations.json"
    export.write_text(json.dumps([_conversation("c-1", "alpha"), _conversation("c-2", "beta")]), encoding="utf-8")
    chatgpt_store = InMemoryVNextCaptureStore()
    VNextCaptureService(chatgpt_store).import_chatgpt_export_file(export)

    assert markdown_store.savepoints_opened == 3
    assert chatgpt_store.savepoints_opened == 2


def test_the_in_memory_store_leaves_no_row_for_a_failed_file(tmp_path: Path) -> None:
    """The shared test store honours the contract the importers now rely on.

    Fails if its ``savepoint`` stops truncating on an exception, which would let
    every importer test that uses it pass against a store that cannot roll back.
    It also fails if the double keeps its source hash index across a rollback:
    the retry of the fixed file then reports a duplicate of a source that no
    longer exists, which no real store does.
    """

    folder = _folder(tmp_path, alpha=_note("alpha"), beta=_note("zeta"))
    store = InMemoryVNextCaptureStore()
    real = store.create_source_chunk

    def flaky(chunk, **kwargs):  # type: ignore[no-untyped-def]
        if chunk["chunk_index"] == 1 and "zeta" in chunk["text"]:
            raise RuntimeError("injected")
        return real(chunk, **kwargs)

    store.create_source_chunk = flaky  # type: ignore[method-assign]
    result = VNextCaptureService(store).import_markdown_folder(folder)

    assert (result.imported_count, result.failed_count) == (1, 1)
    assert [source["title"] for source in store.sources] == ["alpha"]
    assert all("zeta" not in str(chunk["text"]) for chunk in store.chunks)
    assert [event["event_type"] for event in store.events].count("source.import_failed") == 1

    store.create_source_chunk = real  # type: ignore[method-assign]
    retry = VNextCaptureService(store).import_markdown_folder(folder)

    assert (retry.status, retry.imported_count, retry.duplicate_count, retry.failed_count) == ("ok", 1, 1, 0)
    assert sorted(str(source["title"]) for source in store.sources) == ["alpha", "beta"]


def test_a_clean_import_and_its_replay_write_the_events_they_always_wrote(tmp_path: Path) -> None:
    """The success path is untouched: no failure, no new event, no new row.

    These are the counts v0.20.0 writes for three clean files and for the replay
    of the same folder (measured on the v0.20.0 code, one ``source.duplicate_skipped``
    for each replayed file and one batch event for each import). Fails if the
    savepoint writes an event of its own (a ``source.savepoint`` row, say), or
    if the success path stops releasing and keeps a second copy of any write.
    """

    database = _vault(tmp_path)
    folder = _folder(tmp_path, alpha=_note("alpha"), beta=_note("beta"), gamma=_note("gamma"))
    first = _import(database, folder)

    assert (first.status, first.imported_count, first.failed_count) == ("ok", 3, 0)
    assert _event_types(database) == {
        "source.batch_import_completed": 1,
        "source.captured": 3,
        "source.chunked": 3,
        "source.created": 3,
        "source_chunk.created": 12,
    }
    assert _counts(database)["sources"] == 3 and _counts(database)["source_chunks"] == 12

    replay = _import(database, folder)

    assert (replay.status, replay.duplicate_count, replay.imported_count) == ("duplicate", 3, 0)
    assert _event_types(database) == {
        "source.batch_import_completed": 2,
        "source.captured": 3,
        "source.chunked": 3,
        "source.created": 3,
        "source.duplicate_skipped": 3,
        "source_chunk.created": 12,
    }


def test_both_stores_have_a_savepoint_and_the_capture_protocol_requires_it() -> None:
    """The importers rely on ``savepoint``, so no store may lack it quietly.

    Fails if ``savepoint`` is removed from ``VNextCaptureStore`` (a store type
    that forgot it would then still satisfy the protocol and turn a failed file
    back into a half-built source), or from either real store class.
    """

    assert "savepoint" in VNextCaptureStore.__dict__
    for store_class in (SQLiteVNextStore, PostgresVNextStore):
        assert callable(getattr(store_class, "savepoint", None)), store_class.__name__


# Store level: the savepoint itself.


def _sqlite_store(database: Path, **connect_arguments: object) -> tuple[sqlite3.Connection, SQLiteVNextStore]:
    conn = sqlite3.connect(database, **connect_arguments)  # type: ignore[arg-type]
    return conn, SQLiteVNextStore(conn, USER_ID)


def _source(name: str) -> dict[str, object]:
    return {"source_type": "markdown", "title": name, "content_hash": f"hash-{name}", "dedupe_key": f"key-{name}"}


def test_a_savepoint_undoes_the_retire_of_an_existing_row_with_the_inserts_that_follow(tmp_path: Path) -> None:
    """TS2 at the store, in the shape the later retire step needs.

    One block rewrites a row that existed before it (the old version's
    ``deleted_at``), inserts the new rows and fails. The old row must read live
    again and no new row or event may remain. Fails if ``savepoint`` stops
    rolling back to its mark (a release alone keeps every write of the block).
    """

    database = _vault(tmp_path)
    with sqlite_user_connection(database, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        old = store.create_source(_source("old"))
    with sqlite_user_connection(database, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        with pytest.raises(RuntimeError, match="injected"):
            with store.savepoint():
                conn.execute("UPDATE sources SET deleted_at = '2026-01-01T00:00:00Z' WHERE id = ?", (old["id"],))
                new = store.create_source(_source("new"))
                store.append_event(
                    {
                        "event_type": "probe.inside",
                        "actor_type": "system",
                        "target_type": "source",
                        "target_id": new["id"],
                    }
                )
                raise RuntimeError("injected")

    assert _read(database, "SELECT title, deleted_at IS NULL FROM sources") == [("old", 1)]
    assert "probe.inside" not in _event_types(database)


def test_a_savepoint_keeps_its_rows_and_a_failed_inner_block_keeps_the_outer_ones(tmp_path: Path) -> None:
    """Success keeps every row and commits once, and blocks nest.

    Fails if the rollback of a failed inner block is made to end the whole
    transaction (``conn.rollback()`` in place of ``ROLLBACK TO``): the outer
    block's first row is then lost with the inner one.
    """

    database = _vault(tmp_path)
    with sqlite_user_connection(database, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        with store.savepoint():
            store.create_source(_source("outer-one"))
            with pytest.raises(RuntimeError):
                with store.savepoint():
                    store.create_source(_source("inner"))
                    raise RuntimeError("injected")
            store.create_source(_source("outer-two"))
        assert _read(database, "SELECT count(*) FROM sources") == [(0,)]

    assert _read(database, "SELECT title FROM sources ORDER BY title") == [("outer-one",), ("outer-two",)]


def test_the_sqlite_savepoint_sends_begin_then_release_or_rollback_to_then_release(tmp_path: Path) -> None:
    """The transaction statements the SQLite store sends, in order, for both outcomes.

    Rows alone cannot show a savepoint that is never released, and an import of
    thousands of files would stack one open savepoint per file. Fails if the
    success path stops sending ``RELEASE``, if the failure path stops sending
    ``ROLLBACK TO`` or the ``RELEASE`` after it, if the transaction is no longer
    begun first, or if the block sends a ``COMMIT`` of its own.
    """

    database = _vault(tmp_path)
    sent: list[str] = []
    with sqlite_user_connection(database, USER_ID) as conn:
        conn.set_trace_callback(sent.append)
        store = SQLiteVNextStore(conn, USER_ID)
        with store.savepoint():
            store.create_source(_source("kept"))
        with pytest.raises(RuntimeError, match="injected"):
            with store.savepoint():
                store.create_source(_source("dropped"))
                raise RuntimeError("injected")
        conn.set_trace_callback(None)

    statements = [sql for sql in sent if sql.startswith(("BEGIN", "SAVEPOINT", "RELEASE", "ROLLBACK", "COMMIT"))]
    first, second = statements[1].split()[1], statements[3].split()[1]
    assert statements == [
        "BEGIN IMMEDIATE",
        f"SAVEPOINT {first}",
        f"RELEASE SAVEPOINT {first}",
        f"SAVEPOINT {second}",
        f"ROLLBACK TO SAVEPOINT {second}",
        f"RELEASE SAVEPOINT {second}",
    ]


def test_nested_sqlite_savepoints_use_different_names(tmp_path: Path) -> None:
    """Each block gets its own name, so an inner block whose rollback could not run
    never leaves the outer block's release closing the inner mark instead.

    Fails if every block uses one fixed name.
    """

    database = _vault(tmp_path)
    sent: list[str] = []
    with sqlite_user_connection(database, USER_ID) as conn:
        conn.set_trace_callback(sent.append)
        store = SQLiteVNextStore(conn, USER_ID)
        with store.savepoint():
            with store.savepoint():
                pass
        conn.set_trace_callback(None)

    opened = [sql.split()[1] for sql in sent if sql.startswith("SAVEPOINT ")]
    assert len(opened) == 2 and opened[0] != opened[1]


def test_a_savepoint_on_a_connection_that_commits_each_statement_commits_the_block(tmp_path: Path) -> None:
    """The connection's own meaning of a commit is kept.

    ``isolation_level=None`` commits every statement. The block must not open a
    transaction nobody commits: a second connection sees the rows at once, and a
    failed block leaves none. Fails if ``savepoint`` begins a transaction on such
    a connection (the rows are then invisible and are lost when it closes).
    """

    database = _vault(tmp_path)
    conn, store = _sqlite_store(database, isolation_level=None)
    with closing(conn):
        with store.savepoint():
            store.create_source(_source("kept"))
        assert _read(database, "SELECT title FROM sources") == [("kept",)]
        with pytest.raises(RuntimeError):
            with store.savepoint():
                store.create_source(_source("dropped"))
                raise RuntimeError("injected")
        assert _read(database, "SELECT title FROM sources") == [("kept",)]
        assert not conn.in_transaction


@pytest.mark.skipif(sys.version_info < (3, 12), reason="the autocommit attribute is new in Python 3.12")
def test_a_savepoint_on_an_autocommit_true_connection_commits_the_block(tmp_path: Path) -> None:
    """Same contract for ``autocommit=True``, which ignores ``isolation_level``.

    Fails if ``savepoint`` looks at ``isolation_level`` alone: the default of
    ``""`` then begins a transaction that this connection never commits.
    """

    database = _vault(tmp_path)
    conn, store = _sqlite_store(database, autocommit=True)
    with closing(conn):
        with store.savepoint():
            store.create_source(_source("kept"))
        assert _read(database, "SELECT title FROM sources") == [("kept",)]
        assert not conn.in_transaction


def test_a_savepoint_reports_the_callers_error_when_sqlite_has_already_ended_the_transaction(
    tmp_path: Path,
) -> None:
    """SQLite ends the whole transaction for some failures, such as a full disk.

    Then there is no savepoint to roll back to. The caller must still see its own
    error, not "no such savepoint". Fails if the rollback is not guarded: the
    ``OperationalError`` of the missing savepoint replaces the real failure.
    """

    database = _vault(tmp_path)
    with sqlite_user_connection(database, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        with pytest.raises(RuntimeError, match="the real failure"):
            with store.savepoint():
                store.create_source(_source("doomed"))
                conn.rollback()
                raise RuntimeError("the real failure")


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, asyncio.CancelledError])
def test_the_sqlite_savepoint_rolls_back_on_an_interrupt_and_on_a_cancellation(
    tmp_path: Path, interruption: type[BaseException]
) -> None:
    """A ``BaseException`` rolls the block back too, not only an ``Exception``.

    Ctrl-C raises ``KeyboardInterrupt`` and a cancelled task raises
    ``asyncio.CancelledError``; neither is an ``Exception``. The block's row must
    be gone inside the still-open transaction (a caller that catches the
    interruption and goes on would otherwise commit a half-built unit), the
    outer block's row must stay, the savepoint must be released, and the
    connection must take the next write. Fails if the handler is
    ``except Exception``: the row is still visible, and neither ``ROLLBACK TO``
    nor ``RELEASE`` is sent.
    """

    database = _vault(tmp_path)
    sent: list[str] = []
    with sqlite_user_connection(database, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        with store.savepoint():
            store.create_source(_source("outer"))
            conn.set_trace_callback(sent.append)
            with pytest.raises(interruption):
                with store.savepoint():
                    store.create_source(_source("interrupted"))
                    raise interruption()
            conn.set_trace_callback(None)
            assert [row["title"] for row in conn.execute("SELECT title FROM sources ORDER BY title")] == ["outer"]
            store.create_source(_source("after"))

    statements = [sql for sql in sent if sql.startswith(("SAVEPOINT", "RELEASE", "ROLLBACK", "COMMIT"))]
    name = statements[0].split()[1]
    assert statements == [
        f"SAVEPOINT {name}",
        f"ROLLBACK TO SAVEPOINT {name}",
        f"RELEASE SAVEPOINT {name}",
    ]
    assert _read(database, "SELECT title FROM sources ORDER BY title") == [("after",), ("outer",)]


# The Postgres store, against a connection that records the SQL it is sent.


class _RecordingConnection:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.statements: list[str] = []
        self.fail_on = fail_on

    def execute(self, sql: str) -> None:
        self.statements.append(sql)
        if self.fail_on is not None and sql.startswith(self.fail_on):
            raise psycopg.OperationalError("connection lost")


def test_the_postgres_savepoint_releases_on_success_and_rolls_back_then_releases_on_failure() -> None:
    """The SQL the Postgres store sends, in order, for both outcomes.

    Fails if the failure path stops sending ``ROLLBACK TO SAVEPOINT`` before the
    release: a released savepoint keeps the writes of the failed block.
    """

    conn = _RecordingConnection()
    store = PostgresVNextStore(conn)  # type: ignore[arg-type]
    with store.savepoint():
        pass
    with pytest.raises(RuntimeError, match="injected"):
        with store.savepoint():
            raise RuntimeError("injected")

    assert [sql.split()[0:2] for sql in conn.statements] == [
        ["SAVEPOINT", conn.statements[0].split()[1]],
        ["RELEASE", "SAVEPOINT"],
        ["SAVEPOINT", conn.statements[2].split()[1]],
        ["ROLLBACK", "TO"],
        ["RELEASE", "SAVEPOINT"],
    ]
    first, second = conn.statements[0].split()[1], conn.statements[2].split()[1]
    assert conn.statements[1] == f"RELEASE SAVEPOINT {first}"
    assert conn.statements[3] == f"ROLLBACK TO SAVEPOINT {second}"
    assert conn.statements[4] == f"RELEASE SAVEPOINT {second}"


def test_nested_postgres_savepoints_use_different_names() -> None:
    """Each block gets its own name, so an inner block whose rollback could not run
    never leaves the outer block's release closing the inner mark instead.

    Fails if every block uses one fixed name.
    """

    conn = _RecordingConnection()
    store = PostgresVNextStore(conn)  # type: ignore[arg-type]
    with store.savepoint():
        with store.savepoint():
            pass

    opened = [sql.split()[1] for sql in conn.statements if sql.startswith("SAVEPOINT ")]
    assert len(opened) == 2 and opened[0] != opened[1]


def test_the_postgres_savepoint_reports_the_callers_error_when_the_connection_is_gone() -> None:
    """Fails if the rollback is not guarded: the driver error replaces the real failure."""

    conn = _RecordingConnection(fail_on="ROLLBACK TO")
    store = PostgresVNextStore(conn)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="the real failure"):
        with store.savepoint():
            raise RuntimeError("the real failure")


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, asyncio.CancelledError])
def test_the_postgres_savepoint_rolls_back_then_releases_on_an_interrupt_and_on_a_cancellation(
    interruption: type[BaseException],
) -> None:
    """A ``BaseException`` rolls the block back too, not only an ``Exception``.

    Ctrl-C raises ``KeyboardInterrupt`` and a cancelled task raises
    ``asyncio.CancelledError``; neither is an ``Exception``. A pooled connection
    handed back to the pool after one must not carry the block's writes, so the
    interruption is re-raised only after ``ROLLBACK TO`` and ``RELEASE``. Fails
    if the handler is ``except Exception``: the interruption leaves the block
    with no rollback and no release sent.
    """

    conn = _RecordingConnection()
    store = PostgresVNextStore(conn)  # type: ignore[arg-type]
    with pytest.raises(interruption):
        with store.savepoint():
            raise interruption()

    name = conn.statements[0].split()[1]
    assert conn.statements == [
        f"SAVEPOINT {name}",
        f"ROLLBACK TO SAVEPOINT {name}",
        f"RELEASE SAVEPOINT {name}",
    ]


# The words that say what changed, on the one docs page and in the CHANGELOG.

REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCS_LEAD = "Unreleased (on main, not in v0.20.0): each file of a Markdown import"
_CHANGELOG_LEAD = "- A file that fails part way through `alice-memory import-markdown`"


def _docs_paragraph() -> str:
    page = (REPO_ROOT / "docs/integrations/importers.md").read_text(encoding="utf-8")
    matching = [paragraph for paragraph in page.split("\n\n") if paragraph.startswith(_DOCS_LEAD)]
    assert len(matching) == 1
    return " ".join(matching[0].split())


def _changelog_entry() -> str:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = text.split("\n## ", 2)[1]
    assert unreleased.startswith("Unreleased")
    matching = [line for line in unreleased.splitlines() if line.startswith(_CHANGELOG_LEAD)]
    assert len(matching) == 1
    return matching[0]


def test_the_importers_page_marks_the_new_failure_rule_and_says_what_v0200_did() -> None:
    """The paragraph is marked as main only, counted inside its own paragraph.

    Fails if the marker is deleted from the paragraph (the lead no longer
    matches), if the paragraph stops saying what v0.20.0 did with a failed file,
    if the v0.20.0 sentences lose their scope to SQLite or the Postgres sentence
    stops saying it was read from the code and not run, or if it stops naming
    the receipt field and the event a person will look for.
    """

    paragraph = _docs_paragraph()
    assert paragraph.count("Unreleased (on main, not in v0.20.0)") == 1
    assert "In v0.20.0 on SQLite the failed file stayed live with the chunks written before the failure" in paragraph
    assert "On Postgres, a failure at the SQL level went differently in v0.20.0" in paragraph
    assert "read from the code, not run against a live server" in paragraph
    assert "names a Markdown file by its path under the folder (`relative_path`)" in paragraph
    assert "reported `duplicate` and never completed it" in paragraph
    assert "two `source.import_failed` events were written for it" in paragraph
    assert "`failed_count`" in paragraph and "`source.import_failed`" in paragraph
    assert "Nothing repairs it" in paragraph


def test_the_changelog_entry_states_the_v0200_behaviour_and_what_stays() -> None:
    """The entry sits in Unreleased and compares the new rule with v0.20.0.

    Fails if the entry stops saying what v0.20.0 did (stayed live, reported a
    duplicate, wrote two failure events), if those sentences lose their scope to
    SQLite or the Postgres sentence stops saying it was read from the code and
    not run, if the no-switch sentence stops being a statement about this change
    alone (other Unreleased entries, and sibling changes of the same work such as
    the typed MCP error codes, also change behaviour with no switch, so any claim
    that this is "the one change" of its kind is false), or if an old half-built
    source is not said to stay as it is.
    """

    entry = _changelog_entry()
    assert "In v0.20.0 on SQLite a failed chunk write left the file live with the chunks written before the failure" in entry
    assert "reported `duplicate` and never completed it" in entry
    assert "two `source.import_failed` events" in entry
    assert (
        "No setting turns this off: every import now handles a failing file this way, "
        "and what v0.20.0 did instead is set out below." in entry
    )
    assert "the one change" not in entry
    assert "alters behaviour with nothing to switch it off" not in entry
    assert "search quality" not in entry
    assert "On Postgres, a failure at the SQL level went differently in v0.20.0" in entry
    assert "read from the code and was not run against a live server" in entry
    assert "names a Markdown file by its path under the folder (`relative_path`)" in entry
    assert "stays as it is: nothing repairs it" in entry


def test_the_new_prose_uses_no_em_or_en_dash() -> None:
    """Repository prose carries no em dash and no en dash.

    Fails if either text gains one: the CHANGELOG release heading is the only
    place the repository allows an em dash, and this entry is not a heading.
    """

    for text in (_docs_paragraph(), _changelog_entry()):
        assert "—" not in text and "–" not in text
