"""Owner recovery for a backup that holds a credential.

``alice-memory import --quarantine`` stores the named memory as rejected and
removes the credential from that memory and from the records derived from it.
Shared copies are reported and left in place. These tests assert that at the
SQLite store.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import alicebot_api.onramp as onramp_module
from alicebot_api.mcp_tools import MCPRuntimeContext, call_mcp_tool
from alicebot_api.onramp import (
    bootstrap_database,
    main as onramp_main,
    resolve_db_path,
    sqlite_url_for_path,
)
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MODEL_ENV,
)
from alicebot_api.vnext_memory_commit import (
    MemoryCommitRequest,
    VNextMemoryCommitService,
)
from tests.unit.test_sqlite_onramp import (
    USER_ID,
    _assert_onramp_error,
    _export_to,
)

def _credential() -> str:
    """Throwaway vendor-shaped token built from parts. Not a live secret."""
    return "".join(("sk", "-live-", "QUARANTINE", "-SECRET-", "9f3a"))


def _store_searchable_secret(
    conn: sqlite3.Connection,
    store: SQLiteVNextStore,
    memory: dict[str, object],
) -> dict[str, object]:
    """A searchable row that holds credential text, as a vault from before the floor.

    The store refuses to create that row directly in a searchable status.
    Create it as a candidate, then set the status in SQL.
    """

    planted = store.create_memory({**memory, "status": "candidate"})
    status = str(memory.get("status") or "active")
    conn.execute("UPDATE memories SET status = ? WHERE id = ?", (status, str(planted["id"])))
    return {**planted, "status": status}


SECRET = _credential()
PLACEHOLDER = "[quarantined on import]"
QUARANTINE_JSON = {"quarantined": True}
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
        for key in ("memory_id", "candidate_memory_id", "replacement_memory_id"):
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
        secret = _store_searchable_secret(
            conn,
            store,
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
                "confidence": 0.5,
                "extracted_by_model": SECRET,
                "commit_digest": "sha256:not-the-credential",
                "value": {SECRET: "pin", "text": f"Value {SECRET}", "nested": [SECRET, {"n": 1}]},
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
            },
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


def _quarantine_counts(exported: dict[str, list[dict[str, object]]], memory_id: str) -> dict[str, int]:
    """Receipt counts for one named memory, read off the export."""
    memories = [row for row in exported.get("memory", []) if str(row["id"]) == memory_id]
    entity_links: dict[str, set[str]] = {}
    for row in exported.get("graph_edge", []):
        ends = (
            (str(row.get("from_type") or ""), str(row.get("from_id") or "")),
            (str(row.get("to_type") or ""), str(row.get("to_id") or "")),
        )
        entity_id = next((endpoint for kind, endpoint in ends if kind == "entity" and endpoint), "")
        linked_memory = next((endpoint for kind, endpoint in ends if kind == "memory" and endpoint), "")
        if entity_id and linked_memory:
            entity_links.setdefault(entity_id, set()).add(linked_memory)
    quarantine_ids = {memory_id}
    return {
        "memory": len(memories),
        "memory_revision": sum(
            1 for row in exported.get("memory_revision", []) if str(row.get("memory_id") or "") == memory_id
        ),
        "event": sum(1 for row in exported.get("event", []) if _event_belongs(row, memory_id)),
        "provenance_link": sum(
            1
            for row in exported.get("provenance_link", [])
            if row.get("target_type") == "memory"
            and str(row.get("target_id") or "") == memory_id
            and isinstance(row.get("quote"), str)
        ),
        "open_loop": sum(1 for row in exported.get("open_loop", []) if str(row.get("memory_id") or "") == memory_id),
        "graph_edge": sum(
            1
            for row in exported.get("graph_edge", [])
            if (row.get("from_type") == "memory" and str(row.get("from_id") or "") == memory_id)
            or (row.get("to_type") == "memory" and str(row.get("to_id") or "") == memory_id)
        ),
        "entity": sum(
            1
            for memory_ids in entity_links.values()
            if memory_ids and memory_ids <= quarantine_ids
        ),
        "rollup_instance": sum(
            1
            for row in exported.get("memory", [])
            if str(row.get("id") or "") != memory_id
            for instance in (
                ((row.get("value") or {}).get("rollup") or {}).get("instances") or []
                if isinstance(row.get("value"), dict)
                else []
            )
            if isinstance(instance, dict) and str(instance.get("memory_id") or "") == memory_id
        ),
        "successor": sum(
            1
            for row in exported.get("memory", [])
            if str(row.get("id") or "") != memory_id
            and (
                str(row.get("supersedes") or "") == memory_id
                or (
                    isinstance(row.get("metadata_json"), dict)
                    and str(row["metadata_json"].get("supersedes") or "") == memory_id
                )
                or any(
                    str(named.get("superseded_by") or "") == str(row.get("id") or "")
                    or (
                        isinstance(named.get("metadata_json"), dict)
                        and str(named["metadata_json"].get("superseded_by") or "") == str(row.get("id") or "")
                    )
                    for named in memories
                )
            )
        ),
    }


def _receipt_line(counts: dict[str, int]) -> str:
    labels = (
        ("memory", "memory", "memories"),
        ("memory_revision", "revision", "revisions"),
        ("event", "event", "events"),
        ("provenance_link", "provenance quote", "provenance quotes"),
        ("open_loop", "open loop", "open loops"),
        ("graph_edge", "graph edge", "graph edges"),
        ("entity", "entity", "entities"),
        ("rollup_instance", "rollup instance", "rollup instances"),
        ("successor", "successor", "successors"),
    )
    return "quarantine: " + ", ".join(
        _count_phrase(counts[key], singular, plural) for key, singular, plural in labels
    )


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
    counts = _quarantine_counts(exported, secret_id)
    assert (counts["memory"], counts["memory_revision"] >= 1, counts["event"] >= 1) == (1, True, True)
    assert counts["provenance_link"] == 1
    assert counts["open_loop"] == 1
    assert counts["graph_edge"] == 1
    assert counts["entity"] == 1

    fresh = tmp_path / "fresh.db"
    assert _import_quarantine(dump, fresh, secret_id) == 0
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert _receipt_line(counts) in captured.out
    assert captured.out.count(secret_id) == 1
    assert f"quarantined memory ids: {secret_id}" in captured.out

    _assert_secret_absent(fresh)
    with sqlite_user_connection(fresh, USER_ID) as conn:
        secret_row = _stored_portable(conn, "memories", next(row for row in exported["memory"] if row["id"] == secret_id))
        assert secret_row["status"] == "rejected"
        assert secret_row["memory_key"] == f"quarantined.{secret_id}"
        assert secret_row["title"] == PLACEHOLDER
        assert secret_row["canonical_text"] == PLACEHOLDER
        assert secret_row["summary"] == PLACEHOLDER
        assert secret_row["trust_reason"] == PLACEHOLDER
        assert secret_row["fact_keys"] == PLACEHOLDER
        assert secret_row["extracted_by_model"] == PLACEHOLDER
        exported_secret = next(row for row in exported["memory"] if row["id"] == secret_id)
        assert exported_secret["commit_digest"] == "sha256:not-the-credential"
        assert secret_row["commit_digest"] is None
        assert secret_row["confidence"] == exported_secret["confidence"]
        assert secret_row["created_at"] == exported_secret["created_at"]
        assert secret_row["id"] == secret_id
        assert secret_row["value"] == QUARANTINE_JSON
        assert secret_row["metadata_json"] == QUARANTINE_JSON

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
            assert stored_revision["memory_key"] == f"quarantined.{secret_id}"
            assert stored_revision["text_before"] == PLACEHOLDER
            assert stored_revision["text_after"] == PLACEHOLDER
            assert stored_revision["reason"] == PLACEHOLDER
            assert stored_revision["previous_value"] == QUARANTINE_JSON
            assert stored_revision["new_value"] == QUARANTINE_JSON
            assert stored_revision["candidate"] == QUARANTINE_JSON
            assert stored_revision["metadata_json"] == QUARANTINE_JSON
            assert stored_revision["id"] == exported_revision["id"]
            assert stored_revision["created_at"] == exported_revision["created_at"]

        for exported_event in exported["event"]:
            stored_event = _stored_portable(conn, "event_log", exported_event)
            if _event_belongs(exported_event, secret_id):
                assert SECRET not in json.dumps(stored_event)
                assert stored_event["integrity_hash"] is None
                assert stored_event["target_id"] == exported_event["target_id"]
            else:
                assert stored_event == exported_event
        secret_event = _stored_portable(conn, "event_log", next(row for row in exported["event"] if row["id"] == seeded["event"]["id"]))
        assert secret_event["payload_json"] == {"quarantined": True, "memory_id": secret_id}
        assert secret_event["target_id"] == secret_id

        for record_type in ("source", "source_chunk"):
            for exported_row in exported[record_type]:
                assert _stored_portable(conn, _TABLE_BY_RECORD[record_type], exported_row) == exported_row
        quote_row = _stored_portable(
            conn,
            "provenance_links",
            next(row for row in exported["provenance_link"] if row["id"] == seeded["provenance_link"]["id"]),
        )
        assert quote_row["quote"] == PLACEHOLDER
        assert quote_row["target_id"] == secret_id
        loop_row = _stored_portable(
            conn,
            "open_loops",
            next(row for row in exported["open_loop"] if row["id"] == seeded["open_loop"]["id"]),
        )
        assert loop_row["title"] == PLACEHOLDER
        assert loop_row["memory_id"] == secret_id
        edge_row = _stored_portable(
            conn,
            "graph_edges",
            next(row for row in exported["graph_edge"] if row["id"] == seeded["graph_edge"]["id"]),
        )
        assert edge_row["explanation"] == PLACEHOLDER
        assert edge_row["metadata_json"] == QUARANTINE_JSON
        entity_row = _stored_portable(
            conn,
            "vnext_entities",
            next(row for row in exported["entity"] if row["id"] == seeded["entity"]["id"]),
        )
        assert entity_row["name"] == PLACEHOLDER
        assert entity_row["normalized_name"] == f"quarantined.{seeded['entity']['id']}"
        assert entity_row["aliases"] == []

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
    counts = _quarantine_counts(exported, secret_id)
    assert _receipt_line(counts) in captured.out
    _assert_secret_absent(fresh)
    with sqlite_user_connection(fresh, USER_ID) as conn:
        after = conn.execute(
            "SELECT status, canonical_text FROM memories WHERE id = ?",
            (secret_id,),
        ).fetchone()
        assert dict(after) == dict(before)
        assert conn.execute("SELECT COUNT(*) AS n FROM memory_revisions WHERE memory_id = ?", (secret_id,)).fetchone()["n"] == counts["memory_revision"]


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


def test_later_commit_with_the_same_idempotency_key_does_not_replay_the_quarantined_row(
    tmp_path, _no_embeddings
) -> None:
    """A later commit creates a fresh row. It does not replay the rejected one."""
    idempotency_key = "harbour-watch-commit-1"
    original = MemoryCommitRequest(
        user_id=str(USER_ID),
        title="Keep the harbour watch",
        canonical_text=KEPT_TEXT,
        memory_type="decision",
        domain="project",
        sensitivity="internal",
        idempotency_key=idempotency_key,
    )
    origin = tmp_path / "origin.db"
    bootstrap_database(origin, user_id=USER_ID, user_email="local@alice")
    with sqlite_user_connection(origin, USER_ID) as conn:
        first = VNextMemoryCommitService(SQLiteVNextStore(conn, USER_ID)).commit(
            identity=None,
            request=original,
        )
    memory_id = str(first["memory"]["id"])
    fingerprint = first["memory"]["metadata_json"]["agentic_memory"]["request_fingerprint"]
    assert isinstance(fingerprint, str) and fingerprint != PLACEHOLDER
    assert first["memory"]["commit_digest"] == idempotency_key
    memory_key = f"agentic_memory.decision.{idempotency_key}"
    assert first["memory"]["memory_key"] == memory_key

    dump = tmp_path / "backup.jsonl"
    _export_to(origin, dump)
    fresh = tmp_path / "fresh.db"
    assert _import_quarantine(dump, fresh, memory_id) == 0

    with sqlite_user_connection(fresh, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        rejected = store.get_memory(memory_id)
        assert rejected is not None
        assert rejected["status"] == "rejected"
        assert rejected["title"] == PLACEHOLDER
        assert rejected["canonical_text"] == PLACEHOLDER
        assert rejected["commit_digest"] is None
        assert rejected["memory_key"] == f"quarantined.{memory_id}"
        assert rejected["metadata_json"] == QUARANTINE_JSON
        assert store.get_memory_by_commit_digest(idempotency_key) is None
        second = VNextMemoryCommitService(store).commit(identity=None, request=original)
        assert second.get("idempotent_replay") is not True
        assert second["status"] == "committed"
        created = second["memory"]
        assert str(created["id"]) != memory_id
        assert created["status"] == "active"
        assert created["canonical_text"] == KEPT_TEXT
        assert created["title"] == "Keep the harbour watch"
        assert created["commit_digest"] == idempotency_key
        assert created["memory_key"] == memory_key
        assert store.get_memory_by_commit_digest(idempotency_key)["id"] == created["id"]
        still_rejected = store.get_memory(memory_id)
        assert still_rejected["status"] == "rejected"
        assert still_rejected["canonical_text"] == PLACEHOLDER
        assert still_rejected["commit_digest"] is None
        listed = {row["id"] for row in store.list_memories(status=None)}
        assert listed == {memory_id, str(created["id"])}


def _text_hits(db_path: Path, needle: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
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
                query = f"SELECT CAST({_quote_ident(column_name)} AS TEXT) FROM {_quote_ident(name)}"
                try:
                    values = connection.execute(query).fetchall()
                except sqlite3.Error:
                    continue
                if any(isinstance(value, str) and needle in value for (value,) in values):
                    found.add((name, column_name))
    finally:
        connection.close()
    return found


def test_event_membership_through_payload_ids(tmp_path, capsys) -> None:
    """An event belongs when only a payload id names the quarantined memory."""
    origin = tmp_path / "origin.db"
    bootstrap_database(origin, user_id=USER_ID, user_email="local@alice")
    with sqlite_user_connection(origin, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        secret = store.create_memory(
            {
                "memory_key": "decision.payload-secret",
                "status": "active",
                "memory_type": "decision",
                "title": "Payload link",
                "canonical_text": "Harmless decision text.",
                "domain": "project",
                "sensitivity": "internal",
            }
        )
        kept = store.create_memory(
            {
                "memory_key": "decision.payload-kept",
                "status": "active",
                "memory_type": "decision",
                "title": "Kept payload target",
                "canonical_text": KEPT_TEXT,
                "domain": "project",
                "sensitivity": "internal",
            }
        )
        source = store.create_source(
            {
                "source_type": "note",
                "title": "Payload source",
                "content_hash": "hash-payload",
                "domain": "project",
                "sensitivity": "internal",
            }
        )
        secret_id = str(secret["id"])
        by_memory = store.append_event(
            {
                "event_type": "memory.note",
                "actor_type": "user",
                "target_type": "source",
                "target_id": str(source["id"]),
                "payload_json": {"memory_id": secret_id, "note": SECRET},
            }
        )
        by_candidate = store.append_event(
            {
                "event_type": "memory.note",
                "actor_type": "user",
                "target_type": "source",
                "target_id": str(source["id"]),
                "payload_json": {"candidate_memory_id": secret_id, "note": SECRET},
            }
        )
        by_replacement = store.append_event(
            {
                "event_type": "memory.reviewed",
                "actor_type": "user",
                "target_type": "memory",
                "target_id": str(kept["id"]),
                "payload_json": {"replacement_memory_id": secret_id, "rationale": SECRET},
            }
        )
    dump = tmp_path / "backup.jsonl"
    _export_to(origin, dump)
    fresh = tmp_path / "fresh.db"
    assert _import_quarantine(dump, fresh, secret_id) == 0
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    _assert_secret_absent(fresh)
    with sqlite_user_connection(fresh, USER_ID) as conn:
        memory_event = _stored_portable(conn, "event_log", by_memory)
        candidate_event = _stored_portable(conn, "event_log", by_candidate)
        replacement_event = _stored_portable(conn, "event_log", by_replacement)
    assert memory_event["payload_json"] == {"quarantined": True, "memory_id": secret_id}
    assert candidate_event["payload_json"] == {"quarantined": True, "candidate_memory_id": secret_id}
    assert replacement_event["payload_json"] == QUARANTINE_JSON
    assert replacement_event["target_id"] == str(kept["id"])


def test_redact_after_quarantine_import_updates_the_event_log(tmp_path, capsys) -> None:
    """In-product redact still works when a quarantined event payload keeps memory_id."""
    dump, _exported, seeded = _prepare_backup(tmp_path)
    secret_id = str(seeded["secret"]["id"])
    fresh = tmp_path / "fresh.db"
    assert _import_quarantine(dump, fresh, secret_id) == 0
    capsys.readouterr()
    with sqlite_user_connection(fresh, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        stored_event = _stored_portable(conn, "event_log", seeded["event"])
        assert stored_event["payload_json"].get("memory_id") == secret_id
        failure = None
        try:
            result = store.redact_memory_bundle(memory_id=secret_id, project_update_artifacts=())
        except sqlite3.DatabaseError as exc:
            failure = str(exc)
            result = None
        assert failure is None
        assert result is not None
        assert result["memory"]["status"] == "archived"
        assert result["memory"]["canonical_text"] == "[REDACTED]"
        assert result["memory"]["memory_key"] == f"redacted.{secret_id}"
        assert result["memory"]["commit_digest"] is None
    _assert_secret_absent(fresh)


def test_derived_records_lose_the_secret_and_shared_copies_are_reported(tmp_path, capsys) -> None:
    """Copies that exist only for the named memory are replaced. Shared copies are reported."""
    origin = tmp_path / "origin.db"
    bootstrap_database(origin, user_id=USER_ID, user_email="local@alice")
    with sqlite_user_connection(origin, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        kept = store.create_memory(
            {
                "memory_key": "decision.derived-kept",
                "status": "active",
                "memory_type": "decision",
                "title": "Kept derived note",
                "canonical_text": KEPT_TEXT,
                "domain": "project",
                "sensitivity": "internal",
                "value": {"text": KEPT_TEXT},
            }
        )
        secret = _store_searchable_secret(
            conn,
            store,
            {
                "memory_key": "decision.derived-secret",
                "status": "active",
                "memory_type": "decision",
                "title": f"Title {SECRET}",
                "canonical_text": f"Canonical {SECRET}",
                "domain": "project",
                "sensitivity": "internal",
                "value": {"text": f"Value {SECRET}"},
                "metadata_json": {"note": SECRET},
            },
        )
        secret_id = str(secret["id"])
        kept_id = str(kept["id"])
        source = store.create_source(
            {
                "source_type": "note",
                "title": "Derived source",
                "content_hash": "hash-derived",
                "domain": "project",
                "sensitivity": "internal",
            }
        )
        shared_chunk = store.create_source_chunk(
            {
                "source_id": source["id"],
                "chunk_index": 0,
                "text": f"Shared chunk {SECRET}",
                "token_count": 4,
            }
        )
        private_chunk = store.create_source_chunk(
            {
                "source_id": source["id"],
                "chunk_index": 1,
                "text": "Private harbour chunk.",
                "token_count": 3,
            }
        )
        secret_quote = store.create_provenance_link(
            {
                "target_type": "memory",
                "target_id": secret_id,
                "source_id": source["id"],
                "source_chunk_id": shared_chunk["id"],
                "quote": f"Quote {SECRET}",
                "evidence_role": "quoted_from",
                "confidence": 0.8,
            }
        )
        kept_quote = store.create_provenance_link(
            {
                "target_type": "memory",
                "target_id": kept_id,
                "source_id": source["id"],
                "source_chunk_id": shared_chunk["id"],
                "quote": "Kept harbour quote.",
                "evidence_role": "quoted_from",
                "confidence": 0.4,
            }
        )
        store.create_provenance_link(
            {
                "target_type": "memory",
                "target_id": secret_id,
                "source_id": source["id"],
                "source_chunk_id": private_chunk["id"],
                "quote": "Private quote without the token.",
                "evidence_role": "quoted_from",
                "confidence": 0.4,
            }
        )
        exclusive = store.create_entity(
            {
                "entity_type": "organization",
                "name": SECRET,
                "aliases": [f"alias {SECRET}"],
            }
        )
        shared_entity = store.create_entity(
            {
                "entity_type": "person",
                "name": f"Shared {SECRET}",
                "aliases": [SECRET],
            }
        )
        store.create_graph_edge(
            {
                "from_type": "memory",
                "from_id": secret_id,
                "to_type": "entity",
                "to_id": str(exclusive["id"]),
                "edge_type": "mentions",
                "explanation": f"Edge {SECRET}",
                "metadata_json": {"detail": SECRET},
            }
        )
        store.create_graph_edge(
            {
                "from_type": "memory",
                "from_id": secret_id,
                "to_type": "entity",
                "to_id": str(shared_entity["id"]),
                "edge_type": "mentions",
                "explanation": f"Shared edge {SECRET}",
                "metadata_json": {"detail": SECRET},
            }
        )
        store.create_graph_edge(
            {
                "from_type": "memory",
                "from_id": kept_id,
                "to_type": "entity",
                "to_id": str(shared_entity["id"]),
                "edge_type": "mentions",
                "explanation": "Kept edge stays.",
            }
        )
        loop = store.create_open_loop(
            {
                "title": f"Loop {SECRET}",
                "description": f"Description {SECRET}",
                "resolution_note": f"Resolved {SECRET}",
                "status": "resolved",
                "resolved_at": "2026-09-01T00:00:00Z",
                "memory_id": secret_id,
                "domain": "project",
                "sensitivity": "internal",
            }
        )
        rollup = store.create_memory(
            {
                "memory_key": "decision.derived-rollup",
                "status": "active",
                "memory_type": "semantic",
                "title": "Rollup title stays",
                "canonical_text": "Rollup text stays",
                "domain": "project",
                "sensitivity": "internal",
                "value": {
                    "kind": "rollup",
                    "text": "Rollup text stays",
                    "rollup": {
                        "instances": [
                            {
                                "memory_id": secret_id,
                                "label": f"Label {SECRET}",
                                "text": f"Instance {SECRET}",
                                "amounts": [SECRET],
                            },
                            {
                                "memory_id": kept_id,
                                "label": "Kept instance",
                                "text": "Kept instance text",
                            },
                        ]
                    },
                },
            }
        )
        successor = store.create_memory(
            {
                "memory_key": "decision.derived-successor",
                "status": "active",
                "memory_type": "decision",
                "title": "Successor title stays",
                "canonical_text": "Successor text stays",
                "domain": "project",
                "sensitivity": "internal",
                "supersedes": secret_id,
                "value": {"text": "Successor text stays"},
                "metadata_json": {
                    "supersedes": secret_id,
                    "agentic_memory": {
                        "rationale": f"Because {SECRET}",
                        "idempotency_key": f"key-{SECRET}",
                        "request_fingerprint": f"fp-{SECRET}",
                        "status": "committed",
                    },
                },
            }
        )
        store.update_memory(
            memory_id=secret_id,
            patch={"superseded_by": str(successor["id"])},
        )
    dump = tmp_path / "backup.jsonl"
    exported = _export_to(origin, dump)
    fresh = tmp_path / "fresh.db"
    assert _import_quarantine(dump, fresh, secret_id) == 0
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    counts = _quarantine_counts(exported, secret_id)
    assert counts["provenance_link"] >= 1
    assert counts["open_loop"] == 1
    assert counts["graph_edge"] == 2
    assert counts["entity"] == 1
    assert counts["rollup_instance"] == 1
    assert counts["successor"] == 1
    assert _receipt_line(counts) in captured.out
    shared_entity_id = str(shared_entity["id"])
    shared_chunk_id = str(shared_chunk["id"])
    for column in ("aliases", "name", "normalized_name"):
        assert f"quarantine report: vnext_entities {shared_entity_id} {column}" in captured.out
    assert f"quarantine report: source_chunks {shared_chunk_id} text" in captured.out
    assert captured.out.count("no command removes this today") == 4
    assert f"quarantine report: vnext_entities {exclusive['id']}" not in captured.out
    assert f"quarantine report: source_chunks {private_chunk['id']}" not in captured.out

    folded = SECRET.casefold()
    original_hits = _text_hits(fresh, SECRET)
    folded_hits = _text_hits(fresh, folded)
    allowed_original = {
        ("source_chunks", "text"),
        ("vnext_entities", "name"),
        ("vnext_entities", "aliases"),
    }
    allowed_folded = {("vnext_entities", "normalized_name")}
    unexpected_original = {
        hit
        for hit in original_hits
        if hit not in allowed_original and not hit[0].startswith("source_chunks_fts")
    }
    unexpected_folded = {
        hit
        for hit in folded_hits
        if hit not in allowed_folded
        and hit not in allowed_original
        and not hit[0].startswith("source_chunks_fts")
    }
    assert unexpected_original == set(), unexpected_original
    assert ("source_chunks", "text") in original_hits
    assert ("vnext_entities", "name") in original_hits
    assert ("vnext_entities", "aliases") in original_hits
    assert ("vnext_entities", "normalized_name") in folded_hits
    assert unexpected_folded == set(), unexpected_folded

    with sqlite_user_connection(fresh, USER_ID) as conn:
        quote_row = _stored_portable(conn, "provenance_links", secret_quote)
        assert quote_row["quote"] == PLACEHOLDER
        kept_quote_row = _stored_portable(conn, "provenance_links", kept_quote)
        assert kept_quote_row["quote"] == "Kept harbour quote."
        loop_row = _stored_portable(conn, "open_loops", loop)
        assert loop_row["title"] == PLACEHOLDER
        assert loop_row["description"] == PLACEHOLDER
        assert loop_row["resolution_note"] == PLACEHOLDER
        exclusive_row = _stored_portable(conn, "vnext_entities", exclusive)
        assert exclusive_row["name"] == PLACEHOLDER
        assert exclusive_row["aliases"] == []
        assert exclusive_row["normalized_name"] == f"quarantined.{exclusive['id']}"
        shared_row = _stored_portable(conn, "vnext_entities", shared_entity)
        assert shared_row["name"] == f"Shared {SECRET}"
        chunk_row = _stored_portable(conn, "source_chunks", shared_chunk)
        assert chunk_row["text"] == f"Shared chunk {SECRET}"
        private_row = _stored_portable(conn, "source_chunks", private_chunk)
        assert private_row["text"] == "Private harbour chunk."
        rollup_export = next(row for row in exported["memory"] if row["id"] == rollup["id"])
        rollup_row = _stored_portable(conn, "memories", rollup_export)
        assert rollup_row["title"] == "Rollup title stays"
        assert rollup_row["canonical_text"] == "Rollup text stays"
        instances = rollup_row["value"]["rollup"]["instances"]
        assert instances[0] == {"quarantined": True, "memory_id": secret_id}
        assert instances[1]["memory_id"] == kept_id
        assert instances[1]["text"] == "Kept instance text"
        successor_export = next(row for row in exported["memory"] if row["id"] == successor["id"])
        successor_row = _stored_portable(conn, "memories", successor_export)
        assert successor_row["title"] == "Successor title stays"
        assert successor_row["canonical_text"] == "Successor text stays"
        assert successor_row["supersedes"] == secret_id
        agentic = successor_row["metadata_json"]["agentic_memory"]
        assert agentic["rationale"] == PLACEHOLDER
        assert agentic["idempotency_key"] == PLACEHOLDER
        assert agentic["request_fingerprint"] == PLACEHOLDER
        assert agentic["status"] == "committed"
        assert successor_row["metadata_json"]["supersedes"] == secret_id


def test_quarantine_import_runs_credential_verdict_on_text_it_did_not_replace(tmp_path, capsys) -> None:
    """Leftover text is reported by credential_verdict, as table, id, and column.

    The private chunk and the source title are not rewritten and are not shared
    copies, so the structural report does not list them. Skipping
    ``_quarantine_credential_reports`` drops these lines and this test fails
    on the assertions below.
    """

    origin = tmp_path / "origin.db"
    bootstrap_database(origin, user_id=USER_ID, user_email="local@alice")
    with sqlite_user_connection(origin, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        secret = _store_searchable_secret(
            conn,
            store,
            {
                "memory_key": "decision.scan-secret",
                "status": "active",
                "memory_type": "decision",
                "title": f"Title {SECRET}",
                "canonical_text": f"Canonical {SECRET}",
                "domain": "project",
                "sensitivity": "internal",
            },
        )
        secret_id = str(secret["id"])
        source = store.create_source(
            {
                "source_type": "note",
                "title": f"Source {SECRET}",
                "content_hash": "hash-scan",
                "domain": "project",
                "sensitivity": "internal",
                "metadata_json": {"note": SECRET},
            }
        )
        private_chunk = store.create_source_chunk(
            {
                "source_id": source["id"],
                "chunk_index": 0,
                "text": f"Private chunk {SECRET}",
                "token_count": 3,
            }
        )
        store.create_provenance_link(
            {
                "target_type": "memory",
                "target_id": secret_id,
                "source_id": source["id"],
                "source_chunk_id": private_chunk["id"],
                "quote": f"Quote {SECRET}",
                "evidence_role": "quoted_from",
                "confidence": 0.5,
            }
        )
    dump = tmp_path / "backup.jsonl"
    _export_to(origin, dump)
    fresh = tmp_path / "fresh.db"
    assert _import_quarantine(dump, fresh, secret_id) == 0
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    source_id = str(source["id"])
    chunk_id = str(private_chunk["id"])
    for table, row_id, column in (
        ("source_chunks", chunk_id, "text"),
        ("sources", source_id, "title"),
        ("sources", source_id, "metadata_json"),
    ):
        assert f"quarantine report: {table} {row_id} {column}\nno command removes this today" in captured.out
    assert f"quarantine report: memories {secret_id} " not in captured.out
    assert f"quarantine report: provenance_links " not in captured.out
    with sqlite_user_connection(fresh, USER_ID) as conn:
        stored_chunk = conn.execute("SELECT text FROM source_chunks WHERE id = ?", (chunk_id,)).fetchone()
        assert stored_chunk["text"] == f"Private chunk {SECRET}"
        stored_source = conn.execute("SELECT title FROM sources WHERE id = ?", (source_id,)).fetchone()
        assert stored_source["title"] == f"Source {SECRET}"


def test_quarantine_import_refuses_a_memory_the_rewrite_does_not_clean(tmp_path, capsys) -> None:
    """A second memory that still holds a credential is refused and nothing is written."""

    origin = tmp_path / "origin.db"
    bootstrap_database(origin, user_id=USER_ID, user_email="local@alice")
    with sqlite_user_connection(origin, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        named = _store_searchable_secret(
            conn,
            store,
            {
                "memory_key": "decision.named-secret",
                "status": "active",
                "memory_type": "decision",
                "title": f"Named {SECRET}",
                "canonical_text": f"Named body {SECRET}",
                "domain": "project",
                "sensitivity": "internal",
            },
        )
        other = _store_searchable_secret(
            conn,
            store,
            {
                "memory_key": "decision.other-secret",
                "status": "active",
                "memory_type": "decision",
                "title": "Other title stays",
                "canonical_text": f"Other body {SECRET}",
                "domain": "project",
                "sensitivity": "internal",
            },
        )
    dump = tmp_path / "backup.jsonl"
    _export_to(origin, dump)
    fresh = tmp_path / "fresh.db"
    assert _import_quarantine(dump, fresh, str(named["id"])) == 1
    err = capsys.readouterr().err
    _assert_onramp_error(err, code="import_credential_material")
    assert SECRET not in err
    assert f"memory {other['id']} carries credential material" in err
    assert not fresh.exists()


@pytest.mark.parametrize(
    "url",
    (
        "postgresql://example.test/alice-quarantine-db-refused",
        "postgres://example.test/alice-quarantine-db-refused",
    ),
)
def test_postgres_url_passed_as_db_is_refused(tmp_path, capsys, monkeypatch, url: str) -> None:
    monkeypatch.chdir(tmp_path)
    dump, _exported, seeded = _prepare_backup(tmp_path)
    refused_path = Path(url).resolve()
    try:
        with pytest.raises(ValueError, match="SQLite file path"):
            resolve_db_path(data_dir=str(tmp_path), db=url)
        code = onramp_main(
            [
                "import",
                "--in",
                str(dump),
                "--db",
                url,
                "--user-id",
                str(USER_ID),
                "--quarantine",
                str(seeded["secret"]["id"]),
            ]
        )
        captured = capsys.readouterr()
        assert code == 2
        _assert_onramp_error(captured.err, code="sqlite_db_path_required")
        assert not refused_path.exists()
        assert SECRET not in captured.err
        assert SECRET not in captured.out
    finally:
        if refused_path.is_file():
            refused_path.unlink()
