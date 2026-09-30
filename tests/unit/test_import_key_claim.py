"""A backup file cannot prove that an agent API key wrote a row.

The export footer is an unkeyed SHA-256 over the canonical record lines.
Anyone can edit a record and recompute it, so it shows integrity and says
nothing about authorship. Import therefore restores a stored key claim as an
unverified imported claim, keeps the original value as ``claimed_auth``, and
counts the rows it changed in the receipt. These tests build hostile files by
editing a real export and re-signing it, then read the restored vault through
the same tools an agent uses.

Every test names the edit that makes it fail.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from alicebot_api import recall_framing
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext, _sqlite_path_from_url
from alicebot_api.onramp import bootstrap_database, main as onramp_main, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key

USER_ID = "00000000-0000-0000-0000-000000000001"
TOKEN = "falconforge"
FORGED_ID = "forger"
KEY_CLAIM = {"agent_id": FORGED_ID, "auth": "agent_api_key"}
VERIFIED = "verified_by_key"
UNVERIFIED = "declared_on_keyless_install"
RECEIPT = "provenance claims restored as unverified: "


def _vault(tmp_path: Path, name: str) -> tuple[Path, MCPRuntimeContext]:
    database = tmp_path / name / "memory.db"
    database.parent.mkdir(parents=True)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return database, MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _call(context: MCPRuntimeContext, name: str, **arguments: object) -> dict:
    return call_mcp_tool(context, name=name, arguments=arguments)


def _commit_note(context: MCPRuntimeContext, **extra: object) -> dict:
    arguments: dict[str, object] = {
        "title": f"Note {TOKEN}",
        "canonical_text": f"Plain owner note about {TOKEN}.",
        "memory_type": "decision",
        "domain": "personal",
        "sensitivity": "private",
        "confidence": 0.95,
        "source_type": "direct_user_instruction",
        **extra,
    }
    result = _call(context, "alice_memory_commit", **arguments)
    if result["status"] == "confirmation_required":
        confirm = {key: arguments[key] for key in ("agent_id", "agent_type", "permission_profile") if key in arguments}
        result = _call(
            context,
            "alice_memory_commit",
            confirmation_id=result["confirmation_id"],
            confirmation_action="confirm",
            **confirm,
        )
    assert result["status"] == "committed", result
    return result


def _canonical_line(record_type: str, record: object) -> str:
    """The canonical line form, written out here and not taken from the product."""
    return json.dumps(
        {"record_type": record_type, "record": record},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


class _Export:
    """An export parsed into editable records, written back with a fresh footer."""

    def __init__(self, path: Path) -> None:
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.header, self.body, self.footer = lines[0], lines[1:-1], lines[-1]

    def records(self, record_type: str) -> list[dict]:
        return [item["record"] for item in self.body if item["record_type"] == record_type]

    def write(self, path: Path) -> Path:
        digest = hashlib.sha256()
        counts = {key: 0 for key in self.footer["record"]["record_counts"]}
        out = [_canonical_line("export_header", self.header["record"])]
        for item in self.body:
            line = _canonical_line(item["record_type"], item["record"])
            out.append(line)
            digest.update((line + "\n").encode("utf-8"))
            counts[item["record_type"]] += 1
        footer = {
            **self.footer["record"],
            "record_count": len(self.body),
            "record_counts": counts,
            "sha256": digest.hexdigest(),
        }
        out.append(_canonical_line("export_footer", footer))
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        return path


def _export(database: Path, out: Path) -> _Export:
    assert onramp_main(["export", "--db", str(database), "--user-id", USER_ID, "--out", str(out)]) == 0
    return _Export(out)


def _forge_memory(record: dict, *, auth: object = "agent_api_key", agent_id: str = FORGED_ID) -> None:
    record["created_by_agent_id"] = agent_id
    record["metadata_json"]["agent_id"] = agent_id
    record["metadata_json"].setdefault("agentic_memory", {})["agent_identity"] = {"agent_id": agent_id, "auth": auth}


def _forge_event(record: dict, *, auth: object = "agent_api_key", agent_id: str = FORGED_ID) -> None:
    payload = record.get("payload_json") or {}
    payload["agent_identity"] = {"agent_id": agent_id, "auth": auth}
    record["payload_json"] = payload
    record["actor_type"], record["actor_id"] = "agent", agent_id


def _forged_backup(tmp_path: Path, *, memory: bool = True, events: bool = True, auth: object = "agent_api_key") -> Path:
    database, context = _vault(tmp_path, "origin")
    _commit_note(context)
    export = _export(database, tmp_path / "origin.jsonl")
    for record in export.records("memory") if memory else ():
        _forge_memory(record, auth=auth)
    for record in export.records("event") if events else ():
        _forge_event(record, auth=auth)
    return export.write(tmp_path / "forged.jsonl")


def _import(database: Path, backup: Path, *extra: str) -> int:
    return onramp_main(["import", "--in", str(backup), "--db", str(database), "--user-id", USER_ID, *extra])


def _restore(tmp_path: Path, backup: Path, *, name: str = "target") -> tuple[Path, MCPRuntimeContext]:
    database, context = _vault(tmp_path, name)
    assert _import(database, backup) == 0
    return database, context


def _recall_writers(context: MCPRuntimeContext) -> list[dict]:
    hits = _call(context, "alice_recall", query=TOKEN, limit=5)["results"]
    assert hits, "the restored note must be found"
    return [hit["writer"] for hit in hits]


def _stored(database: Path, table: str, column: str) -> list[object]:
    connection = sqlite3.connect(database)
    try:
        return [row[0] for row in connection.execute(f"SELECT {column} FROM {table} ORDER BY id")]
    finally:
        connection.close()


def _receipt_count(out: str) -> int:
    lines = [line for line in out.splitlines() if line.startswith(RECEIPT)]
    assert len(lines) == 1, out
    return int(lines[0].removeprefix(RECEIPT))


def _keyed_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, MCPRuntimeContext]:
    """A vault holding one note that a real agent key wrote."""
    database, context = _vault(tmp_path, "origin")
    with sqlite_user_connection(_sqlite_path_from_url(context.database_url), USER_ID) as conn:
        _record, raw_key = create_agent_key(
            SQLiteVNextStore(conn, USER_ID),
            user_id=USER_ID,
            agent_id="hermes-keyed",
            permission_profile="trusted_local_agent",
        )
    monkeypatch.setenv(AGENT_API_KEY_ENV, raw_key)
    _commit_note(
        context,
        agent_id="hermes-keyed",
        agent_type="personal_assistant",
        permission_profile="trusted_local_agent",
    )
    monkeypatch.delenv(AGENT_API_KEY_ENV, raising=False)
    assert _recall_writers(context) == [{"id": "hermes-keyed", "established": VERIFIED}]
    return database, context


def test_forged_key_claim_on_a_memory_is_not_verified(tmp_path: Path) -> None:
    """Delete the downgrade call in _import_records and recall reads verified_by_key."""
    _database, context = _restore(tmp_path, _forged_backup(tmp_path, events=False))
    assert _recall_writers(context) == [{"id": FORGED_ID, "established": UNVERIFIED}]
    assert _call(context, "alice_resume", query=TOKEN)["brief"]["last_decision"]["writer"] == {
        "id": FORGED_ID,
        "established": UNVERIFIED,
    }


def test_forged_key_claim_on_event_rows_is_not_verified(tmp_path: Path) -> None:
    """Downgrade metadata_json only and skip payload_json: the recent changes stay verified."""
    _database, context = _restore(tmp_path, _forged_backup(tmp_path, memory=False))
    brief = _call(context, "alice_resume", query=TOKEN, max_open_loops=5, max_recent_changes=10)["brief"]
    changes = brief["recent_changes"]
    assert changes
    assert {change["writer"]["established"] for change in changes} == {UNVERIFIED}
    pack = _call(context, "alice_context_pack", query=TOKEN, max_items=10)
    assert pack["memories"]
    assert {row["writer"]["established"] for row in pack["memories"]} == {UNVERIFIED}


def test_a_changed_event_loses_its_integrity_hash_and_an_unchanged_one_keeps_it(tmp_path: Path) -> None:
    """The hash covers the payload. Do not clear it and a changed event carries a hash of the old
    payload; clear it on every event and unchanged rows lose theirs."""
    database, context = _vault(tmp_path, "origin")
    _commit_note(context)
    export = _export(database, tmp_path / "origin.jsonl")
    events = export.records("event")
    assert len(events) >= 2 and all(event["integrity_hash"] for event in events)
    _forge_event(events[0])
    forged_id = events[0]["id"]
    target, _context = _restore(tmp_path, export.write(tmp_path / "forged.jsonl"))
    connection = sqlite3.connect(target)
    try:
        hashes = dict(connection.execute("SELECT id, integrity_hash FROM event_log"))
    finally:
        connection.close()
    assert hashes[forged_id] is None
    for event in events[1:]:
        assert hashes[event["id"]] == event["integrity_hash"]


def test_forged_key_claim_on_a_source_and_an_open_loop_is_not_verified(tmp_path: Path) -> None:
    """A source and an open loop take their writer label from metadata_json too."""
    database, context = _vault(tmp_path, "origin")
    _call(
        context,
        "alice_capture",
        raw_text=f"Harbour survey notes about {TOKEN} and the tide table.",
        title="Harbour survey",
        domain="personal",
        sensitivity="private",
    )
    with sqlite_user_connection(_sqlite_path_from_url(context.database_url), USER_ID) as conn:
        SQLiteVNextStore(conn, USER_ID).create_open_loop(
            {
                "title": f"Follow up on {TOKEN}",
                "description": f"Follow up on {TOKEN}",
                "domain": "personal",
                "sensitivity": "private",
                "metadata_json": {"agent_identity": {"agent_id": "loop-agent", "auth": "unauthenticated_local"}},
            }
        )
    export = _export(database, tmp_path / "origin.jsonl")
    for record in export.records("source"):
        record["metadata_json"]["agent_identity"] = dict(KEY_CLAIM)
    for record in export.records("open_loop"):
        record["metadata_json"]["agent_identity"] = {"agent_id": "loop-agent", "auth": "agent_api_key"}
    forged = export.write(tmp_path / "forged.jsonl")

    _target, restored = _restore(tmp_path, forged)
    recall = _call(restored, "alice_recall", query=TOKEN, limit=5)
    assert recall["sources"]
    assert {source["writer"]["established"] for source in recall["sources"]} == {UNVERIFIED}
    loops = _call(restored, "alice_resume", query=TOKEN, max_open_loops=5)["brief"]["open_loops"]
    assert loops
    assert {loop["writer"]["established"] for loop in loops} == {UNVERIFIED}


def test_a_restored_vault_imports_again_in_skip_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Downgrade at insert but not in the skip comparison: the second import fails with restore_failed."""
    forged = _forged_backup(tmp_path)
    database, _context = _restore(tmp_path, forged)
    capsys.readouterr()
    assert _import(database, forged, "--mode", "skip") == 0
    out = capsys.readouterr().out
    assert "imported 0 records" in out
    assert _receipt_count(out) == 0


def test_a_keyed_vault_can_import_its_own_export_in_skip_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rows already in the vault carry a real key claim. Compare only the rewritten row and the
    second import aborts on the vault's own rows; nothing is restored, so nothing is rewritten."""
    origin, context = _keyed_origin(tmp_path, monkeypatch)
    backup = _export(origin, tmp_path / "origin.jsonl").write(tmp_path / "backup.jsonl")
    before = _stored(origin, "memories", "metadata_json")
    capsys.readouterr()
    assert _import(origin, backup, "--mode", "skip") == 0
    out = capsys.readouterr().out
    assert "imported 0 records" in out
    assert _receipt_count(out) == 0
    assert _stored(origin, "memories", "metadata_json") == before
    assert _recall_writers(context) == [{"id": "hermes-keyed", "established": VERIFIED}]


@pytest.mark.parametrize(
    "auth",
    [
        pytest.param("AGENT_API_KEY", id="upper-case"),
        pytest.param("Agent_Api_Key", id="mixed-case"),
        pytest.param("agent_api_key ", id="trailing-space"),
        pytest.param(" agent_api_key", id="leading-space"),
        pytest.param("agent_api_key\n", id="trailing-newline"),
        pytest.param(" agent_api_key ", id="no-break-space"),
    ],
)
def test_a_near_miss_auth_value_is_unverified_and_stored_unchanged(tmp_path: Path, auth: str) -> None:
    """A case-insensitive match in the downgrade rewrites the stored value. A case-insensitive or
    whitespace-trimming match in the reader makes the row verified_by_key."""
    _database, context = _restore(tmp_path, _forged_backup(tmp_path, auth=auth))
    assert _recall_writers(context) == [{"id": FORGED_ID, "established": UNVERIFIED}]
    target = tmp_path / "target" / "memory.db"
    metadata = json.loads(str(_stored(target, "memories", "metadata_json")[0]))
    assert metadata["agentic_memory"]["agent_identity"] == {"agent_id": FORGED_ID, "auth": auth}
    payloads = [json.loads(str(value)) for value in _stored(target, "event_log", "payload_json")]
    forged_payloads = [payload for payload in payloads if "agent_identity" in payload]
    assert forged_payloads
    assert all(payload["agent_identity"] == {"agent_id": FORGED_ID, "auth": auth} for payload in forged_payloads)


def test_the_reader_compares_the_auth_value_exactly() -> None:
    """Trim or case-fold the value in any of the three readers and a near miss reads verified_by_key."""
    for auth in ("AGENT_API_KEY", " agent_api_key", "agent_api_key\t", "agent api_key", "agent_api_key "):
        row = {"created_by_agent_id": FORGED_ID, "metadata_json": {"agent_identity": {"agent_id": FORGED_ID, "auth": auth}}}
        assert recall_framing.writer_attribution(row) == {"id": FORGED_ID, "established": UNVERIFIED}, auth
        event = {
            "event_type": "memory.committed",
            "actor_type": "agent",
            "actor_id": FORGED_ID,
            "payload_json": {"agent_identity": {"agent_id": FORGED_ID, "auth": auth}},
        }
        assert recall_framing.writer_for_recent_change(object(), event)["established"] == UNVERIFIED, auth
        policy_event = {
            "event_type": "policy.decision",
            "target_id": "m1",
            "actor_id": FORGED_ID,
            "occurred_at": "2026-09-01T00:00:00Z",
            "payload_json": {
                "policy_decision": {"action": "memory.correct"},
                "agent_identity": {"agent_id": FORGED_ID, "auth": auth},
            },
        }
        revision = {
            "memory_id": "m1",
            "actor_type": "agent",
            "actor_id": FORGED_ID,
            "action": "agentic_memory_correct",
            "created_at": "2026-09-01T00:00:00Z",
        }
        assert recall_framing._key_presented_for_revision(revision, [policy_event]) is False, auth
    exact = {"created_by_agent_id": FORGED_ID, "metadata_json": {"agent_identity": KEY_CLAIM}}
    assert recall_framing.writer_attribution(exact) == {"id": FORGED_ID, "established": VERIFIED}


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("identity_text", id="identity-as-json-text"),
        pytest.param("agentic_text", id="agentic-memory-as-json-text"),
        pytest.param("column_text", id="metadata-column-as-json-text"),
        pytest.param("all_text", id="every-level-as-json-text"),
    ],
)
def test_a_key_claim_carried_as_json_text_is_downgraded(tmp_path: Path, shape: str) -> None:
    """The readers decode JSON text where they expect a mapping. A walk over mappings alone leaves
    the claim in place."""
    database, context = _vault(tmp_path, "origin")
    _commit_note(context)
    export = _export(database, tmp_path / "origin.jsonl")
    identity = {"agent_id": FORGED_ID, "auth": "agent_api_key"}
    for record in export.records("memory"):
        record["created_by_agent_id"] = FORGED_ID
        if shape == "identity_text":
            record["metadata_json"] = {"agentic_memory": {"agent_identity": json.dumps(identity)}}
        elif shape == "agentic_text":
            record["metadata_json"] = {"agentic_memory": json.dumps({"agent_identity": identity})}
        elif shape == "column_text":
            record["metadata_json"] = json.dumps({"agentic_memory": {"agent_identity": identity}})
        else:
            record["metadata_json"] = json.dumps(
                {"agentic_memory": json.dumps({"agent_identity": json.dumps(identity)})}
            )
    forged = export.write(tmp_path / "forged.jsonl")
    target, restored = _restore(tmp_path, forged)
    assert _recall_writers(restored) == [{"id": FORGED_ID, "established": UNVERIFIED}]
    stored_text = json.dumps(json.loads(str(_stored(target, "memories", "metadata_json")[0])))
    assert "imported_claim" in stored_text and "claimed_auth" in stored_text


def test_an_event_payload_carried_as_json_text_is_downgraded(tmp_path: Path) -> None:
    """The payload column is read the same way: a text payload is decoded, so it is rewritten too."""
    database, context = _vault(tmp_path, "origin")
    _commit_note(context)
    export = _export(database, tmp_path / "origin.jsonl")
    for record in export.records("event"):
        record["payload_json"] = json.dumps({"agent_identity": KEY_CLAIM})
        record["actor_type"], record["actor_id"] = "agent", FORGED_ID
    _target, restored = _restore(tmp_path, export.write(tmp_path / "forged.jsonl"))
    brief = _call(restored, "alice_resume", query=TOKEN, max_open_loops=5, max_recent_changes=10)["brief"]
    assert brief["recent_changes"]
    assert {change["writer"]["established"] for change in brief["recent_changes"]} == {UNVERIFIED}


def test_a_document_that_quotes_an_identity_is_stored_as_written(tmp_path: Path) -> None:
    """The walk reads identity records, not prose. Decode every JSON-looking string and a captured
    JSON file that happens to hold an identity is rewritten."""
    database, context = _vault(tmp_path, "origin")
    quoted = json.dumps({"agent_identity": KEY_CLAIM}, sort_keys=True)
    _call(
        context,
        "alice_capture",
        raw_text=quoted,
        title="A saved record",
        domain="personal",
        sensitivity="private",
    )
    export = _export(database, tmp_path / "origin.jsonl")
    assert any(record["metadata_json"].get("raw_text") == quoted for record in export.records("source"))
    target, _context = _restore(tmp_path, export.write(tmp_path / "again.jsonl"))
    stored = [json.loads(str(value)) for value in _stored(target, "sources", "metadata_json")]
    assert any(item.get("raw_text") == quoted for item in stored)
    assert "imported_claim" not in json.dumps(stored)


def test_other_auth_values_and_other_rows_are_left_alone(tmp_path: Path) -> None:
    """Only a claim of key authentication is rewritten. Downgrade every identity and the local,
    unauthenticated one changes too."""
    database, context = _vault(tmp_path, "origin")
    _commit_note(context)
    _commit_note(context, title="Second", canonical_text=f"Second plain note about {TOKEN}.")
    export = _export(database, tmp_path / "origin.jsonl")
    first, second = export.records("memory")
    _forge_memory(first, auth="unauthenticated_local", agent_id="local-agent")
    _forge_memory(second)
    target, _context = _restore(tmp_path, export.write(tmp_path / "forged.jsonl"))
    stored = {
        row_id: json.loads(str(text))
        for row_id, text in sqlite3.connect(target).execute("SELECT id, metadata_json FROM memories")
    }
    assert stored[first["id"]] == first["metadata_json"]
    assert stored[second["id"]]["agentic_memory"]["agent_identity"]["auth"] == "imported_claim"


def test_a_genuinely_keyed_note_restores_as_unverified_and_the_receipt_counts_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The honest cost: a note a key really wrote reads unverified after a restore. Drop the receipt
    count and the owner is not told."""
    origin, _context = _keyed_origin(tmp_path, monkeypatch)
    backup = _export(origin, tmp_path / "origin.jsonl").write(tmp_path / "backup.jsonl")
    target, restored = _vault(tmp_path, "target")
    capsys.readouterr()
    assert _import(target, backup) == 0
    out = capsys.readouterr().out
    assert _receipt_count(out) >= 2, out
    assert _recall_writers(restored) == [{"id": "hermes-keyed", "established": UNVERIFIED}]
    claimed = json.loads(str(_stored(target, "memories", "metadata_json")[0]))["agentic_memory"]["agent_identity"]
    assert claimed["auth"] == "imported_claim"
    assert claimed["claimed_auth"] == "agent_api_key"
    assert claimed["agent_id"] == "hermes-keyed"


def test_restoring_into_a_keyed_vault_reads_like_an_owner_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pins the wording on a vault that holds a key. The label is the existing not-verified-by-key
    value, the one an owner write already carries on a keyed vault, and the claim stays readable as
    claimed_auth. A separate value would change a field agents read."""
    forged = _forged_backup(tmp_path)
    target, context = _vault(tmp_path, "target")
    with sqlite_user_connection(target, USER_ID) as conn:
        create_agent_key(
            SQLiteVNextStore(conn, USER_ID),
            user_id=USER_ID,
            agent_id="someone-else",
            permission_profile="trusted_local_agent",
        )
    capsys.readouterr()
    assert _import(target, forged) == 0
    assert _receipt_count(capsys.readouterr().out) >= 1
    assert _recall_writers(context) == [{"id": FORGED_ID, "established": UNVERIFIED}]
    identity = json.loads(str(_stored(target, "memories", "metadata_json")[0]))["agentic_memory"]["agent_identity"]
    assert identity["claimed_auth"] == "agent_api_key"


def test_the_receipt_counts_rows_not_claims(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two claims in one row count once, and a row without one does not count."""
    database, context = _vault(tmp_path, "origin")
    _commit_note(context)
    _commit_note(context, title="Second", canonical_text=f"Second plain note about {TOKEN}.")
    export = _export(database, tmp_path / "origin.jsonl")
    memories = export.records("memory")
    assert len(memories) == 2
    _forge_memory(memories[0])
    memories[0]["metadata_json"]["agent_identity"] = dict(KEY_CLAIM)
    target, _context = _vault(tmp_path, "target")
    capsys.readouterr()
    assert _import(target, export.write(tmp_path / "forged.jsonl")) == 0
    assert _receipt_count(capsys.readouterr().out) == 1


def test_a_clean_restore_prints_a_zero_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The line is printed every time, so a zero shows the check ran."""
    database, context = _vault(tmp_path, "origin")
    _commit_note(context)
    backup = _export(database, tmp_path / "origin.jsonl").write(tmp_path / "clean.jsonl")
    target, _context = _vault(tmp_path, "target")
    capsys.readouterr()
    assert _import(target, backup) == 0
    assert _receipt_count(capsys.readouterr().out) == 0


def _undo_downgrade(value: object) -> object:
    """A record as it was before import rewrote its claims, for comparison."""
    if isinstance(value, dict):
        restored = {key: _undo_downgrade(item) for key, item in value.items()}
        if restored.get("auth") == "imported_claim" and "claimed_auth" in restored:
            restored["auth"] = restored.pop("claimed_auth")
        return restored
    if isinstance(value, list):
        return [_undo_downgrade(item) for item in value]
    return value


def test_export_import_export_differs_only_on_the_rows_that_carried_a_claim(tmp_path: Path) -> None:
    """The one exception to export, import, export equality. A downgrade that reaches another row, or
    changes another field, breaks it."""
    database, context = _vault(tmp_path, "origin")
    _commit_note(context)
    _commit_note(context, title="Second", canonical_text=f"Second plain note about {TOKEN}.")
    export = _export(database, tmp_path / "origin.jsonl")
    memories = export.records("memory")
    _forge_memory(memories[0])
    events = export.records("event")
    _forge_event(events[0])
    forged = export.write(tmp_path / "forged.jsonl")
    target, _context = _restore(tmp_path, forged)
    second = _export(target, tmp_path / "second.jsonl")

    def by_id(parsed: _Export) -> dict[str, tuple[str, dict]]:
        return {item["record"]["id"]: (item["record_type"], item["record"]) for item in parsed.body}

    before, after = by_id(_Export(forged)), by_id(second)
    assert before.keys() == after.keys()
    differing = {row_id for row_id in before if before[row_id] != after[row_id]}
    assert differing == {memories[0]["id"], events[0]["id"]}
    for row_id in differing:
        original = copy.deepcopy(before[row_id][1])
        restored = _undo_downgrade(copy.deepcopy(after[row_id][1]))
        if "integrity_hash" in original:
            original["integrity_hash"] = None
        assert restored == original, row_id


def test_a_second_export_import_export_is_a_fixed_point(tmp_path: Path) -> None:
    """After one downgrade the vault is stable: importing its own export changes nothing more."""
    target, _context = _restore(tmp_path, _forged_backup(tmp_path))
    second = _export(target, tmp_path / "second.jsonl").write(tmp_path / "second-copy.jsonl")
    again, _ctx = _restore(tmp_path, second, name="again")
    third = _export(again, tmp_path / "third.jsonl")
    assert {item["record"]["id"]: item for item in _Export(second).body} == {
        item["record"]["id"]: item for item in third.body
    }


def test_a_claim_replaced_by_quarantine_is_gone_and_not_counted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Quarantine replaces the named memory's metadata first, so its claim is removed, not rewritten.
    The other memory is rewritten and counted."""
    database, context = _vault(tmp_path, "origin")
    named = _commit_note(context)["memory"]["id"]
    _commit_note(context, title="Second", canonical_text=f"Second plain note about {TOKEN}.")
    export = _export(database, tmp_path / "origin.jsonl")
    for record in export.records("memory"):
        _forge_memory(record)
    target, _context = _vault(tmp_path, "target")
    capsys.readouterr()
    assert _import(target, export.write(tmp_path / "forged.jsonl"), "--quarantine", named) == 0
    assert _receipt_count(capsys.readouterr().out) == 1
    stored = {
        row_id: json.loads(str(text))
        for row_id, text in sqlite3.connect(target).execute("SELECT id, metadata_json FROM memories")
    }
    assert stored[named] == {"quarantined": True}
    assert "imported_claim" in json.dumps(stored)


def test_a_record_nested_past_the_walk_limit_is_refused_not_passed_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A walk that stopped early would store whatever sat below it. Remove the depth limit and a deep
    column overflows the stack instead of being refused."""
    database, context = _vault(tmp_path, "origin")
    _commit_note(context)
    export = _export(database, tmp_path / "origin.jsonl")
    nested: object = {"agent_identity": dict(KEY_CLAIM)}
    for _ in range(900):
        nested = {"deeper": nested}
    export.records("memory")[0]["metadata_json"]["nested"] = nested
    # The footer digest is built here from the same canonical form the importer checks.
    forged = export.write(tmp_path / "forged.jsonl")
    target = tmp_path / "target" / "memory.db"
    capsys.readouterr()
    assert _import(target, forged) == 1
    captured = capsys.readouterr()
    assert "restore_failed" in captured.err
    assert "Traceback" not in captured.err
    assert not target.exists()
