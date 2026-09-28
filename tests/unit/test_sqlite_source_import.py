"""SQLite import commands that reach MCP recall.

T2, T6, T7, and T8 from the importer spec. Credentials are built at runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, main as onramp_main, resolve_db_path, sqlite_url_for_path
from alicebot_api.session_briefing import compile_local_session_brief
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vault_doctor import compile_local_vault_doctor
from alicebot_api.vnext_capture import VNextCaptureService
from alicebot_api.vnext_connectors import VNextConnectorService
import alicebot_api.cli as cli_module

from tests.unit.test_vnext_connectors import InMemoryVNextConnectorStore, _telegram_payload


USER_ID = "00000000-0000-0000-0000-000000000001"
CLEAN = "The indigo lighthouse stays on the hill for the weekly import."


def _token() -> str:
    return "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"


def _db(tmp_path: Path) -> Path:
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return database


def _context(database: Path) -> MCPRuntimeContext:
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _blob(database: Path) -> str:
    chunks: list[str] = []
    with sqlite_user_connection(database, USER_ID) as conn:
        names = [
            str(row[0] if isinstance(row, tuple) else row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        ]
        for name in names:
            if name.startswith("sqlite_"):
                continue
            try:
                rows = conn.execute(f"SELECT * FROM {name}").fetchall()
            except Exception:
                continue
            chunks.append(json.dumps(rows, default=str))
    return "\n".join(chunks)


def _key_block() -> str:
    body = "A" * 40
    return "\n".join(
        (
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            body,
            "-----END OPENSSH PRIVATE KEY-----",
        )
    )


def test_markdown_import_withholds_a_key_line_and_a_key_block(tmp_path: Path) -> None:
    """T2. Clean lines are recallable. The token is not stored.

    Fails if the filter is skipped, or if chunks are filtered and
    metadata_json.raw_text still holds the token.
    """

    token = _token()
    folder = tmp_path / "notes"
    folder.mkdir()
    note = "\n".join(
        (
            f"Fact: {CLEAN}",
            f"Decision: Use deploy key {token}",
            _key_block(),
            "Fact: The second clean line stays searchable.",
            "",
        )
    )
    (folder / "week.md").write_text(note, encoding="utf-8")
    database = _db(tmp_path / "data")
    from io import StringIO
    import contextlib

    stdout = StringIO()
    with contextlib.redirect_stdout(stdout):
        assert onramp_main(
            [
                "import-markdown",
                "--from",
                str(folder),
                "--db",
                str(database),
                "--user-id",
                USER_ID,
            ]
        ) == 0
    receipt = json.loads(stdout.getvalue())
    assert receipt["skipped_credentials"] == 2
    assert token not in json.dumps(receipt)
    assert any(item.get("line_number") == 2 for item in receipt["skipped_credential_items"])
    assert any(item.get("line_end") == 5 for item in receipt["skipped_credential_items"])
    assert all(item.get("file") == "week.md" for item in receipt["skipped_credential_items"])

    stored = _blob(database)
    assert token not in stored
    assert "BEGIN OPENSSH PRIVATE KEY" not in stored
    assert "A" * 40 not in stored
    assert CLEAN in stored
    with sqlite_user_connection(database, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        sources = conn.execute("SELECT metadata_json FROM sources").fetchall()
        assert sources
        for row in sources:
            metadata = row["metadata_json"] if not isinstance(row, tuple) else row[0]
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            assert token not in json.dumps(metadata)
            assert token not in str(metadata.get("raw_text"))
        chunks = conn.execute("SELECT text FROM source_chunks").fetchall()
        chunk_text = " ".join(str(row["text"] if not isinstance(row, tuple) else row[0]) for row in chunks)
        assert CLEAN in chunk_text
        assert token not in chunk_text

    recall = call_mcp_tool(_context(database), name="alice_recall", arguments={"query": "indigo lighthouse"})
    assert token not in json.dumps(recall)
    assert any(CLEAN in json.dumps(item) for item in recall.get("sources") or [])
    resume = call_mcp_tool(_context(database), name="alice_resume", arguments={})
    assert token not in json.dumps(resume)
    brief = compile_local_session_brief(database, user_id=USER_ID, query=None)
    assert token not in brief
    assert CLEAN in brief or "indigo lighthouse" in brief


def test_import_writes_no_memories(tmp_path: Path) -> None:
    """T6. An import does not extract candidates.

    Fails if extract_candidates stays true: a decision line becomes a memory
    and last_decision is set.
    """

    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "plan.md").write_text(
        "Decision: Ship the Falcon importer behind a feature flag.\n",
        encoding="utf-8",
    )
    database = _db(tmp_path / "data")
    assert onramp_main(
        ["import-markdown", "--from", str(folder), "--db", str(database), "--user-id", USER_ID]
    ) == 0
    with sqlite_user_connection(database, USER_ID) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()
        memories = int(count["c"] if not isinstance(count, tuple) else count[0])
    assert memories == 0
    resume = call_mcp_tool(
        _context(database),
        name="alice_resume",
        arguments={"max_open_loops": 0, "max_recent_changes": 0},
    )
    assert resume["brief"]["last_decision"] is None


def test_import_markdown_command_recalls_and_replays_as_duplicate(tmp_path: Path, capsys) -> None:
    """T7. The command reaches recall, and a second run is a duplicate."""

    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "note.md").write_text(f"Fact: {CLEAN}\n", encoding="utf-8")
    database = _db(tmp_path / "data")
    args = ["import-markdown", "--from", str(folder), "--db", str(database), "--user-id", USER_ID]
    assert onramp_main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "ok"
    assert first["imported_count"] == 1
    recall = call_mcp_tool(_context(database), name="alice_recall", arguments={"query": "indigo lighthouse"})
    assert recall.get("sources")
    assert onramp_main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "duplicate"
    assert second["duplicate_count"] >= 1


def test_a_flagged_filename_is_printed_withheld(tmp_path: Path, capsys) -> None:
    token = _token()
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / f"{token}.md").write_text(f"Fact: {CLEAN}\n", encoding="utf-8")
    database = _db(tmp_path / "data")
    assert onramp_main(
        ["import-markdown", "--from", str(folder), "--db", str(database), "--user-id", USER_ID]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert token not in json.dumps(receipt)
    assert token not in _blob(database)


def test_capture_producers_accept_clean_fixtures(tmp_path: Path) -> None:
    """T8. Clean producer inputs are not false refusals."""

    database = _db(tmp_path / "data")
    export = tmp_path / "chat.json"
    export.write_text(
        json.dumps(
            {
                "conversations": [
                    {
                        "title": "Weekly",
                        "messages": [{"author": {"role": "user"}, "content": {"parts": [CLEAN]}}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "note.md").write_text(f"Fact: {CLEAN}\n", encoding="utf-8")
    solo = tmp_path / "solo.md"
    solo.write_text("Fact: A single file capture stays clean.\n", encoding="utf-8")
    with sqlite_user_connection(database, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        service = VNextCaptureService(store)
        text = service.capture_text("Fact: Capture text stays a clean source.")
        file_result = service.capture_file(solo)
        folder = service.import_markdown_folder(notes)
        chat = service.import_chatgpt_export_file(export)
    assert text.status == "imported"
    assert file_result.status == "imported"
    assert folder.status == "ok"
    assert chat.status == "ok"
    connector = VNextConnectorService(InMemoryVNextConnectorStore())
    synced = connector.sync_telegram_updates([_telegram_payload(11)], allowed_chat_ids=("999001",))
    assert synced.status == "ok"
    assert synced.imported_count == 1


def test_doctor_lists_ids_of_sources_the_floor_flags(tmp_path: Path) -> None:
    database = _db(tmp_path / "data")
    token = _token()
    with sqlite_user_connection(database, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        stored = VNextCaptureService(store).capture_text(f"Use deploy key {token}", title="note")
        source_id = stored.source_id
    report = compile_local_vault_doctor(database, user_id=USER_ID)
    assert "flagged sources: 1" in report
    assert source_id is not None
    assert source_id in report
    assert token not in report


def test_alicebot_sqlite_import_names_the_alice_memory_commands(monkeypatch, capsys) -> None:
    from alicebot_api.config import Settings
    from uuid import uuid4

    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: Settings(database_url="postgresql://db", auth_user_id=str(uuid4())),
    )
    exit_code = cli_module.main(
        ["--database-url", "sqlite:///alice.db", "vnext", "sources", "import-markdown", "notes"]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    payload = json.loads(captured.err)
    assert payload["error"]["code"] == "sqlite_import_use_alice_memory"
    assert "alice-memory import-markdown" in payload["error"]["message"]
    assert "alice-memory import-chatgpt" in payload["error"]["message"]
