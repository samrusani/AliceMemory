"""Postgres twin of alice-memory import-markdown dedupe, and of the per-file savepoint."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from alicebot_api.db import UserConnection, user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_capture import VNextCaptureService, chunk_text
from alicebot_api.vnext_store import PostgresVNextStore


def test_postgres_import_markdown_imports_then_replays_as_duplicate(
    migrated_database_urls: dict[str, str],
    tmp_path: Path,
) -> None:
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "note.md").write_text(
        "Fact: The indigo lighthouse stays on the hill for the postgres import.\n",
        encoding="utf-8",
    )
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "import-markdown@example.com", "Import markdown")
        service = VNextCaptureService(PostgresVNextStore(conn))
        first = service.import_markdown_folder(folder, domain="project", sensitivity="internal")
        second = service.import_markdown_folder(folder, domain="project", sensitivity="internal")

    assert first.status == "ok"
    assert first.imported_count == 1
    assert first.source_ids
    assert second.status == "duplicate"
    assert second.duplicate_count >= 1
    assert second.imported_count == 0


def _note(word: str) -> str:
    """A note whose headings give it four chunks."""
    return (
        "\n\n".join(
            f"## Section {index} of {word}\nFact: {word} section {index} holds one durable claim about the {word} plan."
            for index in range(4)
        )
        + "\n"
    )


def _live_chunk_counts(conn: UserConnection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT s.title AS title, count(c.id) AS chunks
        FROM sources s
        LEFT JOIN source_chunks c ON c.source_id = s.id
        WHERE s.deleted_at IS NULL
        GROUP BY s.id, s.title
        """
    ).fetchall()
    return {str(row["title"]): int(row["chunks"]) for row in rows}


def test_postgres_import_markdown_rolls_a_failed_file_back_and_imports_it_in_full_next_time(
    migrated_database_urls: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TS1 on Postgres. The twin of tests/unit/test_importer_per_file_savepoint.py.

    The failure is a real statement error, so the transaction is aborted and only
    the rollback to the file's savepoint makes the connection usable again.
    Fails if the savepoint around one file is removed from
    ``import_markdown_folder`` (the failed file stays live with one chunk, and the
    next statement raises on an aborted transaction), or if ``savepoint`` stops
    rolling back to its mark.
    """

    assert len(chunk_text(_note("zeta"))) == 4
    folder = tmp_path / "notes"
    folder.mkdir()
    for name, word in (("alpha", "alpha"), ("beta", "zeta"), ("gamma", "gamma")):
        (folder / f"{name}.md").write_text(_note(word), encoding="utf-8")

    real = PostgresVNextStore.create_source_chunk

    def failing(self, chunk, **kwargs):  # type: ignore[no-untyped-def]
        if chunk["chunk_index"] == 1 and "zeta" in chunk["text"]:
            self.conn.execute("SELECT 1 / 0")
        return real(self, chunk, **kwargs)

    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "import-savepoint@example.com", "Import savepoint")
        service = VNextCaptureService(PostgresVNextStore(conn))
        monkeypatch.setattr(PostgresVNextStore, "create_source_chunk", failing)
        first = service.import_markdown_folder(folder)
        after_failure = _live_chunk_counts(conn)
        failure_events = conn.execute(
            "SELECT count(*) AS n FROM event_log WHERE event_type = 'source.import_failed'"
        ).fetchone()
        monkeypatch.setattr(PostgresVNextStore, "create_source_chunk", real)
        second = service.import_markdown_folder(folder)
        after_retry = _live_chunk_counts(conn)

    assert (first.status, first.imported_count, first.failed_count) == ("partial", 2, 1)
    assert after_failure == {"alpha": 4, "gamma": 4}
    assert failure_events is not None and int(failure_events["n"]) == 1
    assert (second.status, second.imported_count, second.duplicate_count, second.failed_count) == ("ok", 1, 2, 0)
    assert after_retry == {"alpha": 4, "beta": 4, "gamma": 4}


def _conversation(identifier: str, word: str) -> dict[str, object]:
    return {
        "id": identifier,
        "title": f"Chat about {word}",
        "messages": [
            {"author": {"role": "user"}, "content": {"parts": [f"Fact: {word} message {index} " + (word + " ") * 30]}}
            for index in range(6)
        ],
    }


def test_postgres_import_chatgpt_rolls_a_failed_conversation_back_and_imports_it_in_full_next_time(
    migrated_database_urls: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TS1 on Postgres for the ChatGPT importer, where one conversation is the unit.

    The failure is a real statement error, so the transaction is aborted and only
    the rollback to the conversation's savepoint makes the connection usable
    again. Fails if the savepoint around one conversation is removed from
    ``import_chatgpt_export_file`` (the failed conversation stays live with one
    chunk, or the aborted transaction stops the whole batch), or if ``savepoint``
    stops rolling back to its mark.
    """

    export = tmp_path / "conversations.json"
    export.write_text(
        json.dumps([_conversation("c-1", "alpha"), _conversation("c-2", "zeta"), _conversation("c-3", "gamma")]),
        encoding="utf-8",
    )

    real = PostgresVNextStore.create_source_chunk

    def failing(self, chunk, **kwargs):  # type: ignore[no-untyped-def]
        if chunk["chunk_index"] == 1 and "zeta" in chunk["text"]:
            self.conn.execute("SELECT 1 / 0")
        return real(self, chunk, **kwargs)

    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, "import-chatgpt-savepoint@example.com", "Import chatgpt savepoint")
        service = VNextCaptureService(PostgresVNextStore(conn), chunk_max_chars=300)
        monkeypatch.setattr(PostgresVNextStore, "create_source_chunk", failing)
        first = service.import_chatgpt_export_file(export)
        after_failure = _live_chunk_counts(conn)
        failure_events = conn.execute(
            "SELECT count(*) AS n FROM event_log WHERE event_type = 'source.import_failed'"
        ).fetchone()
        monkeypatch.setattr(PostgresVNextStore, "create_source_chunk", real)
        second = service.import_chatgpt_export_file(export)
        after_retry = _live_chunk_counts(conn)

    assert (first.status, first.imported_count, first.failed_count) == ("partial", 2, 1)
    assert set(after_failure) == {"Chat about alpha", "Chat about gamma"}
    assert failure_events is not None and int(failure_events["n"]) == 1
    assert (second.status, second.imported_count, second.duplicate_count, second.failed_count) == ("ok", 1, 2, 0)
    assert set(after_retry) == {"Chat about alpha", "Chat about zeta", "Chat about gamma"}
    assert after_retry["Chat about zeta"] == after_retry["Chat about alpha"] > 2
