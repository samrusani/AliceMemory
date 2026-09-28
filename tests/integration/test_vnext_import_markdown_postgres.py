"""Postgres twin of alice-memory import-markdown dedupe."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_capture import VNextCaptureService
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
