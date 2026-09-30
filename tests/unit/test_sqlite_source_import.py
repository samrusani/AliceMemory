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
    assert receipt["skipped_count"] == 0
    assert token not in json.dumps(receipt)
    assert "file 1 (week.md) line 2" in receipt["skipped_credential_items"]
    assert "file 1 (week.md) lines 3 to 5" in receipt["skipped_credential_items"]

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


def test_a_flagged_filename_is_skipped_and_the_receipt_hides_it(tmp_path: Path, capsys) -> None:
    """A token in the file name skips the file. The receipt does not print it.

    Fails if the file is stored under a withheld title, and fails if the
    receipt includes the token.
    """

    token = _token()
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / f"{token}.md").write_text(f"Fact: {CLEAN}\n", encoding="utf-8")
    database = _db(tmp_path / "data")
    assert onramp_main(
        ["import-markdown", "--from", str(folder), "--db", str(database), "--user-id", USER_ID]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "skipped"
    assert receipt["skipped_count"] == 1
    assert receipt["imported_count"] == 0
    assert receipt["failed_count"] == 0
    assert "file 1 (name withheld)" in receipt["skipped_credential_items"]
    assert receipt["skipped_credentials"] >= 1
    assert token not in json.dumps(receipt)
    assert token not in _blob(database)
    assert CLEAN not in _blob(database)


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
    """A row already stored with a token is listed. New capture refuses it."""

    database = _db(tmp_path / "data")
    token = _token()
    with sqlite_user_connection(database, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        stored = store.create_source(
            {
                "source_type": "manual_text",
                "title": "note",
                "content_hash": "sha256:doctor-flagged-source",
                "metadata_json": {"raw_text": f"Use deploy key {token}"},
            }
        )
        source_id = str(stored["id"])
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
    assert exit_code == 2
    payload = json.loads(captured.err)
    assert payload["error"]["code"] == "sqlite_import_use_alice_memory"
    assert "alice-memory import-markdown" in payload["error"]["message"]
    assert "alice-memory import-chatgpt" in payload["error"]["message"]


def test_import_markdown_from_a_single_file(tmp_path: Path, capsys) -> None:
    """A markdown file is a valid --from path. A folder is not required."""

    note = tmp_path / "note.md"
    note.write_text(f"Fact: {CLEAN}\n", encoding="utf-8")
    database = _db(tmp_path / "data")
    assert onramp_main(
        ["import-markdown", "--from", str(note), "--db", str(database), "--user-id", USER_ID]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "ok"
    assert receipt["imported_count"] == 1
    recall = call_mcp_tool(_context(database), name="alice_recall", arguments={"query": "indigo lighthouse"})
    assert any(CLEAN in json.dumps(item) for item in recall.get("sources") or [])


def test_unmatched_begin_line_is_withheld_through_end_of_file(tmp_path: Path, capsys) -> None:
    """A BEGIN line with no END withholds the rest of the file.

    Fails if only the BEGIN line is replaced: the key body is stored.
    """

    body = "B" * 40
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "week.md").write_text(
        "\n".join(
            (
                f"Fact: {CLEAN}",
                "-----BEGIN OPENSSH PRIVATE KEY-----",
                body,
                "",
            )
        ),
        encoding="utf-8",
    )
    database = _db(tmp_path / "data")
    assert onramp_main(
        ["import-markdown", "--from", str(folder), "--db", str(database), "--user-id", USER_ID]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "ok"
    assert receipt["imported_count"] == 1
    assert receipt["skipped_count"] == 0
    assert "file 1 (week.md) lines 2 to 3" in receipt["skipped_credential_items"]
    stored = _blob(database)
    assert CLEAN in stored
    assert body not in stored
    assert "BEGIN OPENSSH PRIVATE KEY" not in stored


def test_a_secret_the_line_filter_cannot_isolate_skips_the_file(tmp_path: Path, capsys) -> None:
    """A value on the next line skips the whole file. The receipt says so."""

    secret = "Kd9" + "xoYWu83nq"
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "week.md").write_text(
        "\n".join((f"Fact: {CLEAN}", "password:", secret, "")),
        encoding="utf-8",
    )
    (folder / "other.md").write_text("Fact: The other note stays in the folder.\n", encoding="utf-8")
    database = _db(tmp_path / "data")
    assert onramp_main(
        ["import-markdown", "--from", str(folder), "--db", str(database), "--user-id", USER_ID]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "ok"
    assert receipt["imported_count"] == 1
    assert receipt["skipped_count"] == 1
    assert receipt["failed_count"] == 0
    assert any(item.startswith("file ") and "week.md" in item for item in receipt["skipped_credential_items"])
    stored = _blob(database)
    assert secret not in stored
    assert CLEAN not in stored
    assert "The other note stays" in stored


def test_a_flagged_folder_name_writes_nothing_and_hides_the_path(tmp_path: Path, capsys) -> None:
    token = _token()
    folder = tmp_path / token
    folder.mkdir()
    (folder / "week.md").write_text(f"Fact: {CLEAN}\n", encoding="utf-8")
    database = _db(tmp_path / "data")
    assert onramp_main(
        ["import-markdown", "--from", str(folder), "--db", str(database), "--user-id", USER_ID]
    ) == 1
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err
    assert "withheld" in captured.err
    assert "week.md" not in _blob(database)
    assert CLEAN not in _blob(database)


def test_non_utf8_markdown_refuses_the_folder_and_names_the_file(tmp_path: Path, capsys) -> None:
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "good.md").write_text(f"Fact: {CLEAN}\n", encoding="utf-8")
    (folder / "bad.md").write_bytes(b"\xff\xfe not utf8\n")
    database = _db(tmp_path / "data")
    assert onramp_main(
        ["import-markdown", "--from", str(folder), "--db", str(database), "--user-id", USER_ID]
    ) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "bad.md" in captured.err
    assert "not valid UTF-8" in captured.err
    assert CLEAN not in _blob(database)


def test_non_utf8_markdown_with_a_flagged_name_says_withheld(tmp_path: Path, capsys) -> None:
    token = _token()
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "good.md").write_text(f"Fact: {CLEAN}\n", encoding="utf-8")
    (folder / f"{token}.md").write_bytes(b"\xff\xfe not utf8\n")
    database = _db(tmp_path / "data")
    assert onramp_main(
        ["import-markdown", "--from", str(folder), "--db", str(database), "--user-id", USER_ID]
    ) == 1
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err
    assert "withheld" in captured.err
    assert CLEAN not in _blob(database)


def test_chatgpt_title_with_a_token_is_counted_and_stored_as_withheld(tmp_path: Path, capsys) -> None:
    """A conversation title that holds a token is stored as withheld and counted.

    Fails if the title is stored raw, or if skipped_credentials and
    skipped_credential_items ignore it.
    """

    token = _token()
    export = tmp_path / "chat.json"
    export.write_text(
        json.dumps(
            {
                "conversations": [
                    {
                        "id": "weekly",
                        "title": f"Weekly {token}",
                        "messages": [
                            {"author": {"role": "user"}, "content": {"parts": [CLEAN]}},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    database = _db(tmp_path / "data")
    assert onramp_main(
        ["import-chatgpt", "--from", str(export), "--db", str(database), "--user-id", USER_ID]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "ok"
    assert receipt["imported_count"] == 1
    assert receipt["skipped_count"] == 0
    assert receipt["skipped_credentials"] == 1
    assert receipt["skipped_credential_items"] == ["conversation weekly title"]
    stored = _blob(database)
    assert token not in stored
    assert token not in json.dumps(receipt)
    assert "withheld" in stored
    assert CLEAN in stored


def test_chatgpt_part_filter_keeps_the_clean_part(tmp_path: Path, capsys) -> None:
    """A flagged message part is withheld. The clean part in that message stays.

    Fails if the part filter is skipped: the backstop then refuses the
    conversation and the clean part is not stored.
    """

    token = _token()
    export = tmp_path / "chat.json"
    export.write_text(
        json.dumps(
            {
                "conversations": [
                    {
                        "id": "weekly",
                        "title": "Weekly",
                        "messages": [
                            {
                                "author": {"role": "user"},
                                "content": {"parts": [CLEAN, f"Decision: Use deploy key {token}"]},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    database = _db(tmp_path / "data")
    assert onramp_main(
        ["import-chatgpt", "--from", str(export), "--db", str(database), "--user-id", USER_ID]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "ok"
    assert receipt["imported_count"] == 1
    assert receipt["failed_count"] == 0
    assert "conversation weekly message 1" in receipt["skipped_credential_items"]
    stored = _blob(database)
    assert CLEAN in stored
    assert token not in stored
    assert token not in json.dumps(receipt)


def test_refused_chatgpt_conversation_leaves_the_batch_ok(tmp_path: Path, capsys) -> None:
    """A password the commit door flags is withheld. Both conversations import.

    Fails if that message is left in the store, or if the clean conversation
    is dropped with the flagged one.
    """

    secret = "Kd9" + "xoYWu83nq"
    export = tmp_path / "chat.json"
    export.write_text(
        json.dumps(
            {
                "conversations": [
                    {
                        "id": "weekly",
                        "title": "Weekly",
                        "messages": [
                            {
                                "author": {"role": "user"},
                                "content": {"parts": ["password:\n" + secret]},
                            }
                        ],
                    },
                    {
                        "id": "clean",
                        "title": "Clean",
                        "messages": [
                            {"author": {"role": "user"}, "content": {"parts": [CLEAN]}},
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    database = _db(tmp_path / "data")
    assert onramp_main(
        ["import-chatgpt", "--from", str(export), "--db", str(database), "--user-id", USER_ID]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "ok"
    assert receipt["failed_count"] == 0
    assert receipt["skipped_count"] == 0
    assert receipt["imported_count"] == 2
    assert "conversation weekly message 1" in receipt["skipped_credential_items"]
    stored = _blob(database)
    assert secret not in stored
    assert CLEAN in stored


def test_chatgpt_export_filename_token_skips_the_conversation_and_the_batch_stays_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A token in the export file name skips the conversation. The batch stays ok.

    The per-message filter cannot remove the file name. Fails if
    ``except CaptureCredentialRefused`` is removed: the import exits 1.
    """

    token = _token()
    export = tmp_path / f"{token}.json"
    export.write_text(
        json.dumps(
            {
                "conversations": [
                    {
                        "id": "weekly",
                        "title": "Weekly",
                        "messages": [
                            {"author": {"role": "user"}, "content": {"parts": [CLEAN]}},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    database = _db(tmp_path / "data")
    assert onramp_main(
        ["import-chatgpt", "--from", str(export), "--db", str(database), "--user-id", USER_ID]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "skipped"
    assert receipt["failed_count"] == 0
    assert receipt["skipped_count"] == 1
    assert receipt["imported_count"] == 0
    assert token not in json.dumps(receipt)
    assert "name withheld" in json.dumps(receipt["skipped_credential_items"])
    stored = _blob(database)
    assert token not in stored
    assert CLEAN not in stored


def test_refused_connector_item_is_skipped_and_the_cursor_advances() -> None:
    """A refused connector item is skipped, not failed, and the cursor moves.

    Fails if ``except CaptureCredentialRefused`` is removed: the item is
    failed and the cursor stays on the earlier item.
    """

    token = _token()
    store = InMemoryVNextConnectorStore()
    service = VNextConnectorService(store)
    result = service.sync_telegram_updates(
        [
            _telegram_payload(11, f"Fact: {CLEAN}"),
            _telegram_payload(12, f"Decision: Use deploy key {token}"),
        ],
        allowed_chat_ids=("999001",),
    )
    assert result.status == "ok"
    assert result.failed_count == 0
    assert result.skipped_count >= 1
    assert result.imported_count == 1
    assert result.sync_cursor == "12"
    blob = json.dumps(store.sources) + json.dumps(store.chunks)
    assert CLEAN in blob
    assert token not in blob
    assert token not in json.dumps(store.events)


def test_import_failed_event_hides_a_token_in_the_file_name(tmp_path: Path, monkeypatch, capsys) -> None:
    """A token in a file name does not land in source.import_failed."""

    token = _token()
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / f"{token}.md").write_text("Fact: The cedar note stays out of the log.\n", encoding="utf-8")
    database = _db(tmp_path / "data")

    def boom(_self, _source_input):
        raise RuntimeError("boom")

    monkeypatch.setattr(VNextCaptureService, "capture_source", boom)
    assert onramp_main(
        ["import-markdown", "--from", str(folder), "--db", str(database), "--user-id", USER_ID]
    ) == 1
    capsys.readouterr()
    stored = _blob(database)
    assert token not in stored
    assert "withheld" in stored


def test_batch_import_event_hides_a_token_in_the_folder_path(tmp_path: Path, capsys) -> None:
    """A token in a parent directory does not land in batch_import_completed.folder."""

    token = _token()
    folder = tmp_path / token / "notes"
    folder.mkdir(parents=True)
    (folder / "week.md").write_text(f"Fact: {CLEAN}\n", encoding="utf-8")
    database = _db(tmp_path / "data")
    assert onramp_main(
        ["import-markdown", "--from", str(folder), "--db", str(database), "--user-id", USER_ID]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["imported_count"] == 0
    assert receipt["skipped_count"] == 1
    stored = _blob(database)
    assert token not in stored
    assert token not in json.dumps(receipt)
    assert CLEAN not in stored


def _low_entropy_access_key() -> str:
    return "AK" + "IA" + ("A" * 16)


def _prose_password() -> str:
    return "my password is " + "84736251"


def test_markdown_folder_withholds_commit_door_lines_and_imports_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A line only the commit door flags is withheld. The other lines import.

    Fails if the file is skipped whole, the receipt names the wrong line,
    or a clean Decision line is dropped.
    """

    key = _low_entropy_access_key()
    password = _prose_password()
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "a.md").write_text("Decision: ship on Tuesdays.\n", encoding="utf-8")
    (folder / "b.md").write_text(
        "\n".join(
            (
                "Week notes.",
                f"The staging key is {key} for now.",
                "Decision: use Postgres.",
                "",
            )
        ),
        encoding="utf-8",
    )
    (folder / "c.md").write_text(
        "\n".join(
            (
                "Status: fine.",
                password,
                "Decision: keep the brief short.",
                "",
            )
        ),
        encoding="utf-8",
    )
    database = _db(tmp_path / "data")
    assert onramp_main(
        ["import-markdown", "--from", str(folder), "--db", str(database), "--user-id", USER_ID]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["imported_count"] == 3
    assert receipt["skipped_count"] == 0
    assert receipt["failed_count"] == 0
    items = receipt["skipped_credential_items"]
    assert "file 2 (b.md) line 2" in items
    assert "file 3 (c.md) line 2" in items
    assert "file 2 (b.md)" not in items
    assert "file 3 (c.md)" not in items
    rendered = json.dumps(receipt)
    assert key not in rendered
    assert password not in rendered
    stored = _blob(database)
    assert "Decision: ship on Tuesdays." in stored
    assert "Decision: use Postgres." in stored
    assert "Decision: keep the brief short." in stored
    assert key not in stored
    assert password not in stored
    assert "84736251" not in stored


def test_chatgpt_import_withholds_the_flagged_message_or_title(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A commit-door hit in one message or the title is withheld. The rest imports.

    The receipt names message 2 for the key in that message, and the title
    for a key in the title. Fails if the conversation is skipped whole or
    the receipt names a clean message.
    """

    key = _low_entropy_access_key()
    export = tmp_path / "chat.json"
    export.write_text(
        json.dumps(
            {
                "conversations": [
                    {
                        "id": "weekly",
                        "title": "Weekly",
                        "messages": [
                            {
                                "author": {"role": "user"},
                                "content": {"parts": ["Decision: use Postgres for the team server."]},
                            },
                            {
                                "author": {"role": "user"},
                                "content": {"parts": [f"The staging key is {key} for now."]},
                            },
                            {
                                "author": {"role": "user"},
                                "content": {"parts": ["Noted."]},
                            },
                        ],
                    },
                    {
                        "id": "weekly-title",
                        "title": f"Key {key}",
                        "messages": [
                            {
                                "author": {"role": "user"},
                                "content": {"parts": ["Decision: ship on Tuesdays."]},
                            }
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    database = _db(tmp_path / "data")
    assert onramp_main(
        ["import-chatgpt", "--from", str(export), "--db", str(database), "--user-id", USER_ID]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["imported_count"] == 2
    assert receipt["skipped_count"] == 0
    assert receipt["failed_count"] == 0
    items = receipt["skipped_credential_items"]
    assert "conversation weekly message 2" in items
    assert "conversation weekly message 3" not in items
    assert "conversation weekly-title title" in items
    assert "conversation weekly-title message 1" not in items
    rendered = json.dumps(receipt)
    assert key not in rendered
    stored = _blob(database)
    assert "Decision: use Postgres for the team server." in stored
    assert "Decision: ship on Tuesdays." in stored
    assert "Noted." in stored
    assert key not in stored
    assert "withheld" in stored
