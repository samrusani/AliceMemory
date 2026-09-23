"""Owner recovery for a backup that holds a credential.

``alice-memory import --quarantine`` stores the named memory as rejected and
replaces its text. It does not import the credential. These tests assert that
at the SQLite store.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import alicebot_api.onramp as onramp_module
from alicebot_api.mcp_tools import MCPRuntimeContext, call_mcp_tool
from alicebot_api.onramp import bootstrap_database, main as onramp_main, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MODEL_ENV,
)
from tests.unit.test_sqlite_onramp import (
    USER_ID,
    _assert_onramp_error,
    _export_to,
)

SECRET = "sk-live-QUARANTINE-SECRET-9f3a"
PLACEHOLDER = "[quarantined on import]"
KEPT_TEXT = "The harbour watch note stays intact after the restore."
QUOTE = "Harbour chunk sentence."
LOOP_TITLE = "Harbour follow-up"
EDGE_EXPLANATION = "Harbour graph note"

_JSON_COLUMNS = frozenset(
    {
        "aliases",
        "candidate",
        "metadata_json",
        "new_value",
        "payload_json",
        "previous_value",
        "project_scope_json",
        "source_event_ids",
        "value",
    }
)

_TABLE_BY_RECORD = {
    "source": "sources",
    "source_chunk": "source_chunks",
    "memory": "memories",
    "entity": "vnext_entities",
    "graph_edge": "graph_edges",
    "memory_revision": "memory_revisions",
    "provenance_link": "provenance_links",
    "open_loop": "open_loops",
    "event": "event_log",
}


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _event_belongs(record: dict[str, object], memory_id: str) -> bool:
    if record.get("target_type") == "memory" and str(record.get("target_id") or "") == memory_id:
        return True
    payload = record.get("payload_json")
    if isinstance(payload, dict):
        for key in ("memory_id", "candidate_memory_id"):
            if str(payload.get(key) or "") == memory_id:
                return True
    return False


def _count_phrase(count: int, singular: str, plural: str) -> str:
    noun = singular if count == 1 else plural
    return f"{count} {noun}"


def _seed_credential_backup(db_path: Path) -> dict[str, dict[str, object]]:
    """Two memories. One holds the secret in the memory, a revision, and an event."""
    with sqlite_user_connection(db_path, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        kept = store.create_memory(
            {
                "memory_key": "decision.keep-me",
                "status": "active",
                "memory_type": "decision",
                "title": "Keep the harbour watch",
                "canonical_text": KEPT_TEXT,
                "summary": "Harbour watch stays.",
                "domain": "project",
                "sensitivity": "internal",
                "value": {"text": KEPT_TEXT},
            }
        )
        secret = store.create_memory(
            {
                "memory_key": "decision.credential-floor",
                "status": "active",
                "memory_type": "decision",
                "title": f"Title {SECRET}",
                "canonical_text": f"Canonical {SECRET}",
                "summary": f"Summary {SECRET}",
                "trust_reason": f"Reason {SECRET}",
                "domain": "project",
                "sensitivity": "internal",
                "commit_digest": "sha256:not-the-credential",
                "value": {"text": f"Value {SECRET}", "nested": [SECRET, {"n": 1}]},
                "metadata_json": {
                    "agentic_memory": {
                        "corrections": [
                            {
                                "corrected_at": "2026-09-01T00:00:00Z",
                                "reason": "edit",
                                "previous_text": SECRET,
                            }
                        ]
                    }
                },
            }
        )
        store.update_memory_fact_keys(memory_id=str(secret["id"]), fact_keys=f"token {SECRET}")
        source = store.create_source(
            {
                "source_type": "note",
                "title": "Harbour source",
                "content_hash": "hash-harbour",
                "domain": "project",
                "sensitivity": "internal",
            }
        )
        chunk = store.create_source_chunk(
            {
                "source_id": source["id"],
                "chunk_index": 0,
                "text": QUOTE,
                "token_count": 3,
            }
        )
        link = store.create_provenance_link(
            {
                "target_type": "memory",
                "target_id": str(secret["id"]),
                "source_id": source["id"],
                "source_chunk_id": chunk["id"],
                "quote": QUOTE,
                "evidence_role": "quoted_from",
                "confidence": 0.8,
            }
        )
        entity = store.create_entity(
            {
                "entity_type": "organization",
                "name": "Harbour Labs",
                "aliases": ["harbour"],
            }
        )
        edge = store.create_graph_edge(
            {
                "from_type": "memory",
                "from_id": str(secret["id"]),
                "to_type": "entity",
                "to_id": str(entity["id"]),
                "edge_type": "mentions",
                "explanation": EDGE_EXPLANATION,
            }
        )
        loop = store.create_open_loop(
            {
                "title": LOOP_TITLE,
                "memory_id": secret["id"],
                "source_id": source["id"],
                "domain": "project",
                "sensitivity": "internal",
            }
        )
        revision = store.append_revision(
            {
                "memory_id": secret["id"],
                "memory_key": "decision.credential-floor",
                "revision_type": "corrected",
                "text_before": f"Before {SECRET}",
                "text_after": f"After {SECRET}",
                "reason": f"Because {SECRET}",
                "previous_value": {"text": f"Previous {SECRET}"},
                "new_value": {"text": f"Next {SECRET}"},
                "candidate": {"canonical_text": SECRET, "kept": True},
                "metadata_json": {"previous_text": SECRET, "kept_number": 1},
            }
        )
        event = store.append_event(
            {
                "event_type": "memory.note",
                "actor_type": "user",
                "target_type": "memory",
                "target_id": str(secret["id"]),
                "payload_json": {
                    "memory_id": str(secret["id"]),
                    "canonical_text": SECRET,
                    "previous_text": SECRET,
                    "kept_number": 2,
                },
                "integrity_hash": "sha256:" + ("ab" * 32),
            }
        )
    return {
        "kept": kept,
        "secret": secret,
        "source": source,
        "source_chunk": chunk,
        "provenance_link": link,
        "entity": entity,
        "graph_edge": edge,
        "open_loop": loop,
        "memory_revision": revision,
        "event": event,
    }


def _prepare_backup(tmp_path: Path) -> tuple[Path, dict[str, list[dict[str, object]]], dict[str, dict[str, object]]]:
    origin = tmp_path / "origin.db"
    bootstrap_database(origin, user_id=USER_ID, user_email="local@alice")
    seeded = _seed_credential_backup(origin)
    exported = _export_to(origin, tmp_path / "backup.jsonl")
    return tmp_path / "backup.jsonl", exported, seeded


def _stored_portable(conn: sqlite3.Connection, table: str, exported: dict[str, object]) -> dict[str, object]:
    columns = list(exported)
    row = conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} WHERE id = ?",
        (str(exported["id"]),),
    ).fetchone()
    assert row is not None, table
    decoded: dict[str, object] = {}
    for key in columns:
        value = row[key]
        if key in _JSON_COLUMNS and isinstance(value, str):
            decoded[key] = json.loads(value)
        else:
            decoded[key] = value
    return decoded


def _assert_secret_absent(db_path: Path) -> None:
    """The secret is absent from every column of every table, and from the file bytes."""
    connection = sqlite3.connect(db_path)
    try:
        names = [
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'
                """
            )
        ]
        for name in names:
            try:
                info = connection.execute(f"PRAGMA table_info({_quote_ident(name)})").fetchall()
            except sqlite3.Error:
                continue
            for column in info:
                column_name = column[1]
                query = (
                    f"SELECT CAST({_quote_ident(column_name)} AS TEXT) FROM {_quote_ident(name)}"
                )
                try:
                    values = connection.execute(query).fetchall()
                except sqlite3.Error:
                    continue
                for (value,) in values:
                    if isinstance(value, str) and SECRET in value:
                        raise AssertionError(f"{SECRET} found in {name}.{column_name}")
    finally:
        connection.close()
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(f"{db_path}{suffix}")
        if path.is_file():
            assert SECRET.encode("utf-8") not in path.read_bytes(), suffix or "main"


def _quarantine_counts(exported: dict[str, list[dict[str, object]]], memory_id: str) -> tuple[int, int, int]:
    memories = sum(1 for row in exported.get("memory", []) if str(row["id"]) == memory_id)
    revisions = sum(1 for row in exported.get("memory_revision", []) if str(row.get("memory_id")) == memory_id)
    events = sum(1 for row in exported.get("event", []) if _event_belongs(row, memory_id))
    return memories, revisions, events


def _import_quarantine(dump: Path, db_path: Path, memory_id: str, *extra: str) -> int:
    return onramp_main(
        [
            "import",
            "--in",
            str(dump),
            "--db",
            str(db_path),
            "--user-id",
            str(USER_ID),
            "--quarantine",
            f"{memory_id},{memory_id}",
            *extra,
        ]
    )


@pytest.fixture
def _no_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_name in (EMBEDDINGS_BASE_URL_ENV, EMBEDDINGS_MODEL_ENV, EMBEDDINGS_API_KEY_ENV):
        monkeypatch.delenv(env_name, raising=False)


def test_import_quarantine_rejects_the_memory_and_removes_the_secret(tmp_path, capsys, _no_embeddings) -> None:
    assert onramp_module._QUARANTINE_PLACEHOLDER == PLACEHOLDER
    dump, exported, seeded = _prepare_backup(tmp_path)
    secret_id = str(seeded["secret"]["id"])
    kept_id = str(seeded["kept"]["id"])
    assert SECRET in json.dumps(exported)
    memories, revisions, events = _quarantine_counts(exported, secret_id)
    assert (memories, revisions >= 1, events >= 1) == (1, True, True)

    fresh = tmp_path / "fresh.db"
    assert _import_quarantine(dump, fresh, secret_id) == 0
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert (
        "quarantine: "
        + ", ".join(
            (
                _count_phrase(memories, "memory", "memories"),
                _count_phrase(revisions, "revision", "revisions"),
                _count_phrase(events, "event", "events"),
            )
        )
    ) in captured.out
    assert captured.out.count(secret_id) == 1
    assert f"quarantined memory ids: {secret_id}" in captured.out

    _assert_secret_absent(fresh)
    with sqlite_user_connection(fresh, USER_ID) as conn:
        secret_row = _stored_portable(conn, "memories", next(row for row in exported["memory"] if row["id"] == secret_id))
        assert secret_row["status"] == "rejected"
        assert secret_row["memory_key"] == "decision.credential-floor"
        assert secret_row["title"] == PLACEHOLDER
        assert secret_row["canonical_text"] == PLACEHOLDER
        assert secret_row["summary"] == PLACEHOLDER
        assert secret_row["trust_reason"] == PLACEHOLDER
        assert secret_row["fact_keys"] == PLACEHOLDER
        exported_secret = next(row for row in exported["memory"] if row["id"] == secret_id)
        assert exported_secret["commit_digest"] == "sha256:not-the-credential"
        assert secret_row["commit_digest"] == exported_secret["commit_digest"]
        assert SECRET not in str(secret_row["commit_digest"])
        assert secret_row["value"] == {"text": PLACEHOLDER, "nested": [PLACEHOLDER, {"n": 1}]}
        corrections = secret_row["metadata_json"]["agentic_memory"]["corrections"]
        assert corrections[0]["previous_text"] == PLACEHOLDER
        assert corrections[0]["corrected_at"] == PLACEHOLDER
        assert "previous_text" in corrections[0]

        kept_export = next(row for row in exported["memory"] if row["id"] == kept_id)
        assert _stored_portable(conn, "memories", kept_export) == kept_export

        exported_revisions = [row for row in exported["memory_revision"] if row["memory_id"] == secret_id]
        stored_revision_ids = {
            row["id"]
            for row in conn.execute("SELECT id FROM memory_revisions WHERE memory_id = ?", (secret_id,))
        }
        assert stored_revision_ids == {row["id"] for row in exported_revisions}
        for exported_revision in exported_revisions:
            stored_revision = _stored_portable(conn, "memory_revisions", exported_revision)
            assert stored_revision["memory_id"] == secret_id
            assert stored_revision["text_before"] == PLACEHOLDER
            assert stored_revision["text_after"] == PLACEHOLDER
            assert stored_revision["reason"] == PLACEHOLDER
            assert stored_revision["previous_value"] == {"text": PLACEHOLDER}
            assert stored_revision["new_value"] == {"text": PLACEHOLDER}
            assert stored_revision["candidate"] == {"canonical_text": PLACEHOLDER, "kept": True}
            assert stored_revision["metadata_json"] == {"previous_text": PLACEHOLDER, "kept_number": 1}
            assert stored_revision["id"] == exported_revision["id"]

        for exported_event in exported["event"]:
            stored_event = _stored_portable(conn, "event_log", exported_event)
            if _event_belongs(exported_event, secret_id):
                assert SECRET not in json.dumps(stored_event)
                assert stored_event["integrity_hash"] is None
                assert stored_event["target_id"] == exported_event["target_id"]
            else:
                assert stored_event == exported_event
        secret_event = _stored_portable(conn, "event_log", next(row for row in exported["event"] if row["id"] == seeded["event"]["id"]))
        assert secret_event["payload_json"] == {
            "memory_id": PLACEHOLDER,
            "canonical_text": PLACEHOLDER,
            "previous_text": PLACEHOLDER,
            "kept_number": 2,
        }

        for record_type in ("source", "source_chunk", "provenance_link", "open_loop", "graph_edge", "entity"):
            for exported_row in exported[record_type]:
                assert _stored_portable(conn, _TABLE_BY_RECORD[record_type], exported_row) == exported_row
        assert _stored_portable(conn, "provenance_links", next(row for row in exported["provenance_link"] if row["id"] == seeded["provenance_link"]["id"]))["quote"] == QUOTE
        assert _stored_portable(conn, "open_loops", next(row for row in exported["open_loop"] if row["id"] == seeded["open_loop"]["id"]))["title"] == LOOP_TITLE
        assert _stored_portable(conn, "graph_edges", next(row for row in exported["graph_edge"] if row["id"] == seeded["graph_edge"]["id"]))["explanation"] == EDGE_EXPLANATION

    context = MCPRuntimeContext(database_url=sqlite_url_for_path(fresh), user_id=USER_ID)
    recall = call_mcp_tool(context, name="alice_recall", arguments={"query": "harbour watch"})
    assert SECRET not in json.dumps(recall)
    assert [row["id"] for row in recall["results"]] == [kept_id]
    hidden = call_mcp_tool(context, name="alice_recall", arguments={"query": "quarantined"})
    assert all(row["id"] != secret_id for row in hidden["results"])
    assert SECRET not in json.dumps(hidden)
    pack = call_mcp_tool(context, name="alice_context_pack", arguments={"query": "harbour watch"})
    assert SECRET not in json.dumps(pack)
    assert kept_id in {row["id"] for row in pack["memories"]}
    assert secret_id not in {row["id"] for row in pack["memories"]}
    resumed = call_mcp_tool(context, name="alice_resume", arguments={})
    assert SECRET not in json.dumps(resumed)
    assert resumed["brief"]["last_decision"]["id"] == kept_id
    assert secret_id not in json.dumps(resumed)


def _tamper_kept_memory(dump: Path, destination: Path) -> None:
    lines = dump.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        payload = json.loads(line)
        if payload["record_type"] == "memory" and payload["record"]["memory_key"] == "decision.keep-me":
            payload["record"]["canonical_text"] = "tampered harbour text"
            lines[index] = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            break
    else:
        raise AssertionError("kept memory was not in the export")
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_import_quarantine_rejects_a_tampered_file_before_redaction(tmp_path, capsys) -> None:
    dump, _exported, seeded = _prepare_backup(tmp_path)
    secret_id = str(seeded["secret"]["id"])
    tampered = tmp_path / "tampered.jsonl"
    _tamper_kept_memory(dump, tampered)

    target = tmp_path / "target.db"
    assert (
        onramp_main(
            [
                "import",
                "--in",
                str(tampered),
                "--db",
                str(target),
                "--user-id",
                str(USER_ID),
                "--quarantine",
                secret_id,
            ]
        )
        == 1
    )
    _assert_onramp_error(capsys.readouterr().err, code="import_validation_failed")
    assert not target.exists()

    unknown_target = tmp_path / "unknown-target.db"
    assert (
        onramp_main(
            [
                "import",
                "--in",
                str(tampered),
                "--db",
                str(unknown_target),
                "--user-id",
                str(USER_ID),
                "--quarantine",
                "not-a-memory-in-the-file",
            ]
        )
        == 1
    )
    _assert_onramp_error(capsys.readouterr().err, code="import_validation_failed")
    assert not unknown_target.exists()


def test_import_quarantine_unknown_id_writes_nothing(tmp_path, capsys) -> None:
    dump, _exported, seeded = _prepare_backup(tmp_path)
    secret_id = str(seeded["secret"]["id"])
    fresh = tmp_path / "fresh.db"
    assert (
        onramp_main(
            [
                "import",
                "--in",
                str(dump),
                "--db",
                str(fresh),
                "--user-id",
                str(USER_ID),
                "--quarantine",
                f"{secret_id},not-a-memory-in-the-file",
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    _assert_onramp_error(err, code="import_quarantine_unknown")
    assert SECRET not in err
    assert not fresh.exists()

    existing = tmp_path / "existing.db"
    bootstrap_database(existing, user_id=USER_ID, user_email="local@alice")
    with sqlite_user_connection(existing, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        marker = store.create_memory(
            {
                "memory_key": "decision.already-here",
                "status": "active",
                "memory_type": "decision",
                "canonical_text": "Already stored, and must stay.",
                "domain": "project",
                "sensitivity": "internal",
            }
        )
    before = existing.read_bytes()
    assert (
        onramp_main(
            [
                "import",
                "--in",
                str(dump),
                "--db",
                str(existing),
                "--user-id",
                str(USER_ID),
                "--quarantine",
                "not-a-memory-in-the-file",
            ]
        )
        == 1
    )
    _assert_onramp_error(capsys.readouterr().err, code="import_quarantine_unknown")
    assert existing.read_bytes() == before
    with sqlite_user_connection(existing, USER_ID) as conn:
        row = conn.execute("SELECT canonical_text, status FROM memories WHERE id = ?", (str(marker["id"]),)).fetchone()
        assert row["canonical_text"] == "Already stored, and must stay."
        assert row["status"] == "active"
        assert conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 1


def test_import_quarantine_empty_flag_writes_nothing(tmp_path, capsys) -> None:
    target = tmp_path / "target.db"
    assert (
        onramp_main(
            [
                "import",
                "--in",
                str(tmp_path / "missing.jsonl"),
                "--db",
                str(target),
                "--user-id",
                str(USER_ID),
                "--quarantine",
                ",",
            ]
        )
        == 2
    )
    _assert_onramp_error(capsys.readouterr().err, code="invalid_request")
    assert not target.exists()


def test_second_import_of_the_same_quarantine_file_skips_identical_rows(tmp_path, capsys) -> None:
    """A second import with the same --quarantine list skips identical redacted rows."""
    dump, exported, seeded = _prepare_backup(tmp_path)
    secret_id = str(seeded["secret"]["id"])
    fresh = tmp_path / "fresh.db"
    assert _import_quarantine(dump, fresh, secret_id) == 0
    capsys.readouterr()
    with sqlite_user_connection(fresh, USER_ID) as conn:
        before = conn.execute(
            "SELECT status, canonical_text FROM memories WHERE id = ?",
            (secret_id,),
        ).fetchone()
    assert before["status"] == "rejected"
    assert before["canonical_text"] == PLACEHOLDER

    assert _import_quarantine(dump, fresh, secret_id) == 0
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    total = sum(len(rows) for rows in exported.values())
    assert "imported 0 records" in captured.out
    assert f"({total} skipped)" in captured.out
    memories, revisions, events = _quarantine_counts(exported, secret_id)
    assert (
        "quarantine: "
        + ", ".join(
            (
                _count_phrase(memories, "memory", "memories"),
                _count_phrase(revisions, "revision", "revisions"),
                _count_phrase(events, "event", "events"),
            )
        )
    ) in captured.out
    _assert_secret_absent(fresh)
    with sqlite_user_connection(fresh, USER_ID) as conn:
        after = conn.execute(
            "SELECT status, canonical_text FROM memories WHERE id = ?",
            (secret_id,),
        ).fetchone()
        assert dict(after) == dict(before)
        assert conn.execute("SELECT COUNT(*) AS n FROM memory_revisions WHERE memory_id = ?", (secret_id,)).fetchone()["n"] == revisions


def test_second_import_without_quarantine_aborts_and_leaves_the_rejected_row(tmp_path, capsys) -> None:
    dump, _exported, seeded = _prepare_backup(tmp_path)
    secret_id = str(seeded["secret"]["id"])
    kept_id = str(seeded["kept"]["id"])
    fresh = tmp_path / "fresh.db"
    assert _import_quarantine(dump, fresh, secret_id) == 0
    capsys.readouterr()

    assert (
        onramp_main(
            ["import", "--in", str(dump), "--db", str(fresh), "--user-id", str(USER_ID)]
        )
        == 1
    )
    _assert_onramp_error(capsys.readouterr().err, code="restore_failed")
    _assert_secret_absent(fresh)
    with sqlite_user_connection(fresh, USER_ID) as conn:
        secret_row = conn.execute(
            "SELECT status, canonical_text FROM memories WHERE id = ?",
            (secret_id,),
        ).fetchone()
        kept_row = conn.execute(
            "SELECT status, canonical_text FROM memories WHERE id = ?",
            (kept_id,),
        ).fetchone()
    assert secret_row["status"] == "rejected"
    assert secret_row["canonical_text"] == PLACEHOLDER
    assert kept_row["status"] == "active"
    assert kept_row["canonical_text"] == KEPT_TEXT


def test_second_quarantine_import_with_mode_fail_aborts_and_leaves_the_row(tmp_path, capsys) -> None:
    dump, _exported, seeded = _prepare_backup(tmp_path)
    secret_id = str(seeded["secret"]["id"])
    fresh = tmp_path / "fresh.db"
    assert _import_quarantine(dump, fresh, secret_id) == 0
    capsys.readouterr()
    assert _import_quarantine(dump, fresh, secret_id, "--mode", "fail") == 1
    _assert_onramp_error(capsys.readouterr().err, code="restore_failed")
    _assert_secret_absent(fresh)
    with sqlite_user_connection(fresh, USER_ID) as conn:
        row = conn.execute(
            "SELECT status, canonical_text FROM memories WHERE id = ?",
            (secret_id,),
        ).fetchone()
    assert row["status"] == "rejected"
    assert row["canonical_text"] == PLACEHOLDER
