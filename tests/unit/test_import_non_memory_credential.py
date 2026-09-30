"""Backup import reports credential-shaped text in every non-memory record, and doctor reads chunk text.

Import refuses a backup when a memory row holds credential material. The other
record types (sources, chunks, revisions, provenance quotes, loops, entities,
edges, relationship and event rows) are restored unchanged, and a vault from
before the floor legitimately holds a secret in a source that no SQLite command
removes. So import does not refuse them. It lists each table, id and column
that holds credential-shaped text in the receipt, never the text itself, and
exits 0. ``alice-memory export`` lists the same rows on stderr. The doctor
reads source chunk text, where a captured document keeps its body.

Every test names the edit that makes it fail.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import string
import uuid
from pathlib import Path

import pytest

from alicebot_api import onramp as onramp_module
from alicebot_api.credential_floor import credential_verdict
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, main as onramp_main, sqlite_url_for_path
from alicebot_api.sqlite_store import sqlite_user_connection
from tests.unit.test_sqlite_onramp import USER_ID, _seed_full_graph

USER = str(USER_ID)
HEADING = "credential-shaped text in non-memory records: "

# One case per column the importer stores verbatim and a model can read back.
COLUMNS = [
    ("source", "sources", "title"),
    ("source", "sources", "metadata_json"),
    ("source_chunk", "source_chunks", "text"),
    ("memory_revision", "memory_revisions", "text_after"),
    ("memory_revision", "memory_revisions", "reason"),
    ("provenance_link", "provenance_links", "quote"),
    ("open_loop", "open_loops", "title"),
    ("open_loop", "open_loops", "description"),
    ("entity", "vnext_entities", "name"),
    ("graph_edge", "graph_edges", "explanation"),
    ("entity_relationship_event", "entity_relationship_events", "metadata_json"),
    ("event", "event_log", "payload_json"),
]


def _token(seed: str) -> str:
    """A vendor-shaped throwaway token, built at run time. Not a live secret."""
    rnd = random.Random(seed)
    return "gh" + "p_" + "".join(rnd.choice(string.ascii_letters + string.digits) for _ in range(36))


def _canonical_line(record_type: str, record: object) -> str:
    return json.dumps(
        {"record_type": record_type, "record": record},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


class _Export:
    def __init__(self, path: Path) -> None:
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.header, self.body, self.footer = lines[0], lines[1:-1], lines[-1]

    def first(self, record_type: str) -> dict:
        for item in self.body:
            if item["record_type"] == record_type:
                return item["record"]
        raise AssertionError(f"the export holds no {record_type} record")

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
    assert onramp_main(["export", "--db", str(database), "--user-id", USER, "--out", str(out)]) == 0
    return _Export(out)


@pytest.fixture()
def seeded(tmp_path: Path) -> tuple[Path, _Export]:
    database = tmp_path / "origin" / "memory.db"
    database.parent.mkdir()
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    _seed_full_graph(database)
    return database, _export(database, tmp_path / "origin.jsonl")


def _import(tmp_path: Path, backup: Path, *extra: str, name: str = "target") -> tuple[int, Path]:
    target = tmp_path / name / "memory.db"
    code = onramp_main(["import", "--in", str(backup), "--db", str(target), "--user-id", USER, *extra])
    return code, target


def _hit_lines(out: str) -> list[str]:
    """The listed rows under the receipt heading, with the count checked against them."""
    lines = out.splitlines()
    heading = [index for index, line in enumerate(lines) if line.startswith(HEADING)]
    assert len(heading) == 1, out
    count = int(lines[heading[0]].removeprefix(HEADING))
    listed = lines[heading[0] + 1 : heading[0] + 1 + count]
    assert len(listed) == count and all(line.startswith("  ") for line in listed), out
    following = lines[heading[0] + 1 + count : heading[0] + 2 + count]
    assert not following or not following[0].startswith("  credential-shaped"), out
    return [line.strip() for line in listed]


def _plant(record: dict, column: str, token: str) -> None:
    record[column] = {"note": token} if column.endswith("_json") else f"Service login {token}"


@pytest.mark.parametrize(("record_type", "table", "column"), COLUMNS, ids=[f"{t}.{c}" for _r, t, c in COLUMNS])
def test_every_non_memory_text_column_is_reported_and_the_import_succeeds(
    tmp_path: Path,
    seeded: tuple[Path, _Export],
    capfd: pytest.CaptureFixture[str],
    record_type: str,
    table: str,
    column: str,
) -> None:
    """Skip a record type, read a name-based subset of columns, scan str columns only and skip
    JSON, or print the matched text, and one of these cases fails."""
    _database, export = seeded
    token = _token(f"{table}.{column}")
    assert credential_verdict(f"Service login {token}") is not None, "the planted text must be detectable"
    record = export.first(record_type)
    _plant(record, column, token)
    backup = export.write(tmp_path / "forged.jsonl")
    capfd.readouterr()
    code, target = _import(tmp_path, backup)
    captured = capfd.readouterr()
    assert code == 0, captured.err
    assert target.exists()
    assert token not in captured.out + captured.err
    assert f"{table} {record['id']} {column}" in _hit_lines(captured.out)


def test_a_clean_product_export_reports_zero(tmp_path: Path, seeded: tuple[Path, _Export], capfd) -> None:
    """Flag every column and the product's own export reports hits."""
    _database, export = seeded
    backup = export.write(tmp_path / "clean.jsonl")
    capfd.readouterr()
    code, _target = _import(tmp_path, backup)
    out = capfd.readouterr().out
    assert code == 0
    assert HEADING + "0" in out.splitlines()
    assert _hit_lines(out) == []


def test_a_token_in_an_id_column_is_reported_without_printing_the_id(
    tmp_path: Path, seeded: tuple[Path, _Export], capfd: pytest.CaptureFixture[str]
) -> None:
    """Drop the id withholding and the receipt prints the credential it found in the id."""
    _database, export = seeded
    token = _token("row-id")
    record = export.first("source_chunk")
    old_id, record["id"] = record["id"], f"{record['id']}-{token}"
    for item in export.body:
        if item["record_type"] == "provenance_link" and item["record"].get("source_chunk_id") == old_id:
            item["record"]["source_chunk_id"] = record["id"]
    backup = export.write(tmp_path / "forged.jsonl")
    capfd.readouterr()
    code, _target = _import(tmp_path, backup)
    captured = capfd.readouterr()
    assert code == 0, captured.err
    assert token not in captured.out + captured.err
    lines = _hit_lines(captured.out)
    link = next(item["record"] for item in export.body if item["record_type"] == "provenance_link")
    chunk_line = 2 + next(index for index, item in enumerate(export.body) if item["record"] is record)
    assert lines == [
        f"provenance_links {link['id']} source_chunk_id",
        f"source_chunks (id withheld, line {chunk_line}) id",
    ], lines


def test_an_id_with_a_newline_cannot_start_a_fake_receipt_line(
    tmp_path: Path, seeded: tuple[Path, _Export], capfd: pytest.CaptureFixture[str]
) -> None:
    """Print ids as they are and a hostile id forges receipt lines. The id is withheld when it is not printable."""
    _database, export = seeded
    record = export.first("source_chunk")
    old_id, record["id"] = record["id"], "chunk-x\nprovenance claims restored as unverified: 0"
    record["text"] = f"Service login {_token('newline-id')}"
    for item in export.body:
        if item["record_type"] == "provenance_link" and item["record"].get("source_chunk_id") == old_id:
            item["record"]["source_chunk_id"] = record["id"]
    backup = export.write(tmp_path / "forged.jsonl")
    capfd.readouterr()
    code, _target = _import(tmp_path, backup)
    out = capfd.readouterr().out
    assert code == 0
    assert [line for line in out.splitlines() if line.startswith("provenance claims restored")] == [
        "provenance claims restored as unverified: 0"
    ]
    assert "\nprovenance claims" not in "".join(line for line in out.splitlines() if line.startswith("  "))
    assert any(line.startswith("  source_chunks (id withheld, line ") for line in out.splitlines())


def test_a_quarantine_import_keeps_its_own_report_and_adds_no_second_one(
    tmp_path: Path, seeded: tuple[Path, _Export], capfd: pytest.CaptureFixture[str]
) -> None:
    """With --quarantine the existing scan already lists each leftover with its removal command. Print the
    new section there too and the same chunk is listed twice."""
    _database, export = seeded
    memory = export.first("memory")
    chunk = export.first("source_chunk")
    chunk["text"] = f"Service login {_token('quarantine-chunk')}"
    backup = export.write(tmp_path / "forged.jsonl")
    capfd.readouterr()
    code, _target = _import(tmp_path, backup, "--quarantine", str(memory["id"]))
    out = capfd.readouterr().out
    assert code == 0
    assert HEADING not in out
    assert f"quarantine report: source_chunks {chunk['id']} text" in out


def test_a_hash_column_with_a_token_is_reported(
    tmp_path: Path, seeded: tuple[Path, _Export], capfd: pytest.CaptureFixture[str]
) -> None:
    """Scan only the text columns a name list allows and a token in content_hash is missed."""
    _database, export = seeded
    token = _token("content-hash")
    record = export.first("source")
    record["content_hash"] = f"{hashlib.sha256(b'a digest').hexdigest()} {token}"
    backup = export.write(tmp_path / "forged.jsonl")
    capfd.readouterr()
    code, _target = _import(tmp_path, backup)
    captured = capfd.readouterr()
    assert code == 0
    assert token not in captured.out + captured.err
    assert f"sources {record['id']} content_hash" in _hit_lines(captured.out)


def test_the_credential_check_reports_nothing_for_an_id_a_hash_or_a_time() -> None:
    """The import skips a value that is wholly a UUID, a digest or a timestamp, to spare the many short
    columns of a large export. That is only sound while the check returns nothing for them. If it ever
    flags one, this fails and the skip has to go."""
    rnd = random.Random("structural")
    samples = []
    for _ in range(400):
        digest = "".join(rnd.choice("0123456789abcdef") for _ in range(64))
        samples += [
            str(uuid.UUID(int=rnd.getrandbits(128), version=4)),
            digest,
            digest[:32],
            digest[:40],
            f"sha256:{digest}",
            digest.upper(),
            f"2026-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}T{rnd.randint(0, 23):02d}:{rnd.randint(0, 59):02d}:00Z",
        ]
    assert all(onramp_module._is_structural_value(sample) for sample in samples)
    assert [sample for sample in samples if credential_verdict(sample) is not None] == []


def test_values_shaped_like_ids_hashes_and_times_are_not_read(
    tmp_path: Path, seeded: tuple[Path, _Export], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remove the structural skip and every id, digest and timestamp in the file goes to the check."""
    _database, export = seeded
    read: list[object] = []
    real = onramp_module.credential_verdict

    def spy(*values: object) -> object:
        read.extend(values)
        return real(*values)

    monkeypatch.setattr(onramp_module, "credential_verdict", spy)
    backup = export.write(tmp_path / "clean.jsonl")
    code, _target = _import(tmp_path, backup)
    assert code == 0
    structural = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|(?:sha256:)?[0-9a-f]{32,128}|\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-][\d:]+)?",
        re.IGNORECASE,
    )
    strings = [value for value in read if isinstance(value, str)]
    assert any("round-trip chunk text" in value for value in strings), "the chunk text is read"
    assert [value for value in strings if structural.fullmatch(value)] == []


def test_a_real_hash_and_uuid_ids_are_never_reported(tmp_path: Path, seeded: tuple[Path, _Export], capfd) -> None:
    """Scanning id and hash columns must not flag what the product writes there."""
    _database, export = seeded
    digest = hashlib.sha256(b"a real digest").hexdigest()
    export.first("source")["content_hash"] = digest
    export.first("event")["integrity_hash"] = f"sha256:{digest}"
    backup = export.write(tmp_path / "hashes.jsonl")
    capfd.readouterr()
    code, _target = _import(tmp_path, backup)
    assert code == 0
    assert _hit_lines(capfd.readouterr().out) == []


def test_the_file_user_id_is_not_scanned_because_import_does_not_store_it(tmp_path: Path, capfd) -> None:
    """Import rebinds user_id. A legacy file value that is never stored is not a finding; scanning
    user_id makes this case report a row that holds nothing."""
    token = _token("legacy-user")
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text(
        _canonical_line(
            "source",
            {
                "id": "11111111-2222-4333-8444-555555555555",
                "user_id": token,
                "source_type": "note",
                "title": "Legacy source",
                "content_hash": "legacy-hash",
                "captured_at": "2026-06-01T08:00:00Z",
                "domain": "project",
                "sensitivity": "internal",
                "metadata_json": {},
            },
        )
        + "\n",
        encoding="utf-8",
    )
    capfd.readouterr()
    code, _target = _import(tmp_path, legacy)
    captured = capfd.readouterr()
    assert code == 0, captured.err
    assert token not in captured.out + captured.err
    assert _hit_lines(captured.out) == []


def test_credential_shaped_text_in_a_memory_is_still_refused(tmp_path: Path, seeded: tuple[Path, _Export], capfd) -> None:
    """Report-only applies to non-memory records. Loosen the memory floor and this fails."""
    _database, export = seeded
    token = _token("memory")
    record = export.first("memory")
    record["canonical_text"] = f"Service login {token}"
    backup = export.write(tmp_path / "forged.jsonl")
    capfd.readouterr()
    code, target = _import(tmp_path, backup)
    captured = capfd.readouterr()
    assert code == 1
    assert not target.exists()
    assert token not in captured.out + captured.err
    assert "import_credential_material" in captured.err


def test_a_second_import_lists_the_same_rows_in_skip_mode(tmp_path: Path, seeded: tuple[Path, _Export], capfd) -> None:
    """The receipt describes the file, so skipped rows are listed too."""
    _database, export = seeded
    token = _token("chunk-skip")
    record = export.first("source_chunk")
    record["text"] = f"Service login {token}"
    backup = export.write(tmp_path / "forged.jsonl")
    code, target = _import(tmp_path, backup)
    assert code == 0
    capfd.readouterr()
    second = onramp_main(["import", "--in", str(backup), "--db", str(target), "--user-id", USER, "--mode", "skip"])
    out = capfd.readouterr().out
    assert second == 0
    assert _hit_lines(out) == [f"source_chunks {record['id']} text"]


def test_export_lists_the_same_rows_on_stderr_and_exits_zero(
    tmp_path: Path, seeded: tuple[Path, _Export], capfd: pytest.CaptureFixture[str]
) -> None:
    """Drop the export listing and an owner learns of a planted chunk only after restoring it."""
    database, _original = seeded
    token = _token("export-chunk")
    with sqlite_user_connection(database, USER_ID) as conn:
        chunk = conn.execute("SELECT id FROM source_chunks LIMIT 1").fetchone()
        conn.execute("UPDATE source_chunks SET text = ? WHERE id = ?", (f"Service login {token}", chunk["id"]))
    capfd.readouterr()
    out_path = tmp_path / "again.jsonl"
    assert onramp_main(["export", "--db", str(database), "--user-id", USER, "--out", str(out_path)]) == 0
    captured = capfd.readouterr()
    assert token not in captured.out + captured.err
    listed = [line for line in captured.err.splitlines() if "source_chunks" in line and chunk["id"] in line]
    assert len(listed) == 1 and "text" in listed[0], captured.err
    assert "will refuse" not in captured.err
    assert "unchanged" in captured.err
    assert onramp_main(["export", "--db", str(database), "--user-id", USER]) == 0
    stdout_run = capfd.readouterr()
    assert token in stdout_run.out, "the export itself still carries the row"
    assert any("source_chunks" in line and chunk["id"] in line for line in stdout_run.err.splitlines())
    assert token not in stdout_run.err


def test_a_clean_export_lists_no_non_memory_rows(tmp_path: Path, seeded: tuple[Path, _Export], capfd) -> None:
    """Flag every column and a clean export warns."""
    database, _original = seeded
    capfd.readouterr()
    assert onramp_main(["export", "--db", str(database), "--user-id", USER, "--out", str(tmp_path / "x.jsonl")]) == 0
    err = capfd.readouterr().err
    assert "credential-shaped" not in err
    assert "unchanged" not in err


def _doctor_flagged(database: Path, capfd: pytest.CaptureFixture[str]) -> tuple[int, list[str]]:
    capfd.readouterr()
    assert onramp_main(["doctor", "--db", str(database), "--user-id", USER]) == 0
    out = capfd.readouterr().out
    count = next(int(line.split(": ")[1]) for line in out.splitlines() if line.startswith("flagged sources: "))
    ids_line = next(line for line in out.splitlines() if line.startswith("flagged source ids:"))
    ids = [part.strip() for part in ids_line.removeprefix("flagged source ids:").split(",") if part.strip()]
    return count, ids


def test_doctor_reads_a_token_that_sits_only_in_chunk_text(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    """Drop the chunk scan in vault_doctor and this backup reads 'flagged sources: 0'."""
    origin = tmp_path / "origin" / "memory.db"
    origin.parent.mkdir()
    bootstrap_database(origin, user_id=USER_ID, user_email="local@alice")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(origin), user_id=USER)
    call_mcp_tool(
        context,
        name="alice_capture",
        arguments={
            "raw_text": "Harbour survey notes: the tide table for March is attached.",
            "title": "Harbour survey",
            "domain": "personal",
            "sensitivity": "private",
        },
    )
    export = _export(origin, tmp_path / "origin.jsonl")
    token = _token("doctor-chunk")
    chunk = export.first("source_chunk")
    chunk["text"] += f" Service login: {token}"
    source_id = chunk["source_id"]
    assert credential_verdict(chunk["text"]) is not None
    backup = export.write(tmp_path / "forged.jsonl")
    code, target = _import(tmp_path, backup)
    assert code == 0
    assert _doctor_flagged(origin, capfd) == (0, [])
    count, ids = _doctor_flagged(target, capfd)
    assert (count, ids) == (1, [source_id])


def test_doctor_counts_a_source_once_when_the_row_and_its_chunk_both_hold_a_token(
    tmp_path: Path, seeded: tuple[Path, _Export], capfd: pytest.CaptureFixture[str]
) -> None:
    """A source flagged through its row and through its chunk is listed once."""
    _database, export = seeded
    chunk = export.first("source_chunk")
    chunk["text"] = f"Service login {_token('both-chunk')}"
    source = next(item["record"] for item in export.body if item["record_type"] == "source" and item["record"]["id"] == chunk["source_id"])
    source["title"] = f"Login {_token('both-title')}"
    backup = export.write(tmp_path / "forged.jsonl")
    code, target = _import(tmp_path, backup)
    assert code == 0
    assert _doctor_flagged(target, capfd) == (1, [chunk["source_id"]])


def test_doctor_ignores_the_chunks_of_a_deleted_source(
    tmp_path: Path, seeded: tuple[Path, _Export], capfd: pytest.CaptureFixture[str]
) -> None:
    """Join chunks to their source without the deleted_at filter and a removed source is flagged."""
    _database, export = seeded
    chunk = export.first("source_chunk")
    chunk["text"] = f"Service login {_token('deleted')}"
    backup = export.write(tmp_path / "forged.jsonl")
    code, target = _import(tmp_path, backup)
    assert code == 0
    with sqlite_user_connection(target, USER_ID) as conn:
        conn.execute("UPDATE sources SET deleted_at = '2026-06-02T00:00:00Z' WHERE id = ?", (chunk["source_id"],))
    assert _doctor_flagged(target, capfd) == (0, [])


def test_import_reads_a_token_far_into_a_long_column(
    tmp_path: Path, seeded: tuple[Path, _Export], capfd: pytest.CaptureFixture[str]
) -> None:
    """A captured document chunk is long. Pass only the head of a column to the check and a token after
    4,000 characters of text is missed."""
    _database, export = seeded
    token = _token("deep-chunk")
    chunk = export.first("source_chunk")
    chunk["text"] = ("Harbour survey notes about the tide table. " * 120) + f"Service login: {token}"
    assert len(chunk["text"]) > 4000 and credential_verdict(chunk["text"]) is not None
    assert credential_verdict(chunk["text"][:1000]) is None, "the token must sit past the first 1,000 characters"
    backup = export.write(tmp_path / "forged.jsonl")
    capfd.readouterr()
    code, _target = _import(tmp_path, backup)
    captured = capfd.readouterr()
    assert code == 0
    assert token not in captured.out + captured.err
    assert f"source_chunks {chunk['id']} text" in _hit_lines(captured.out)


def test_doctor_reads_a_token_far_into_a_long_chunk(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    """Read only the head of a chunk in the doctor and this source prints flagged sources: 0."""
    origin = tmp_path / "origin" / "memory.db"
    origin.parent.mkdir()
    bootstrap_database(origin, user_id=USER_ID, user_email="local@alice")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(origin), user_id=USER)
    call_mcp_tool(
        context,
        name="alice_capture",
        arguments={
            "raw_text": "Harbour survey notes.",
            "title": "Harbour survey",
            "domain": "personal",
            "sensitivity": "private",
        },
    )
    export = _export(origin, tmp_path / "origin.jsonl")
    chunk = export.first("source_chunk")
    chunk["text"] = ("Tide tables for the harbour survey. " * 120) + f"Service login: {_token('doctor-deep')}"
    assert credential_verdict(chunk["text"][:1000]) is None
    backup = export.write(tmp_path / "forged.jsonl")
    code, target = _import(tmp_path, backup)
    assert code == 0
    assert _doctor_flagged(target, capfd) == (1, [chunk["source_id"]])


@pytest.mark.parametrize(("length", "withheld"), [(128, False), (129, True), (600, True)])
def test_an_over_long_printable_id_is_withheld_and_a_128_character_one_is_shown(
    tmp_path: Path, seeded: tuple[Path, _Export], capfd: pytest.CaptureFixture[str], length: int, withheld: bool
) -> None:
    """The documented limit is 128 characters. Remove the length clause, or raise the limit, and a
    hostile id prints whole on the receipt."""
    _database, export = seeded
    chunk = export.first("source_chunk")
    chunk["text"] = f"Service login {_token('long-id')}"
    old_id, chunk["id"] = chunk["id"], "c" * length
    for item in export.body:
        if item["record_type"] == "provenance_link" and item["record"].get("source_chunk_id") == old_id:
            item["record"]["source_chunk_id"] = chunk["id"]
    backup = export.write(tmp_path / "forged.jsonl")
    capfd.readouterr()
    code, _target = _import(tmp_path, backup)
    lines = _hit_lines(capfd.readouterr().out)
    assert code == 0
    mine = [line for line in lines if line.startswith("source_chunks ")]
    if withheld:
        assert mine and mine[0].startswith("source_chunks (id withheld, line ")
        assert "c" * 50 not in mine[0]
    else:
        assert mine == [f"source_chunks {'c' * length} text"]


def test_doctor_lists_flagged_source_ids_in_id_order(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    """The listing is sorted. Return the set as it comes and the order changes from one run to the
    next."""
    origin = tmp_path / "origin" / "memory.db"
    origin.parent.mkdir()
    bootstrap_database(origin, user_id=USER_ID, user_email="local@alice")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(origin), user_id=USER)
    for index in range(8):
        call_mcp_tool(
            context,
            name="alice_capture",
            arguments={
                "raw_text": f"Survey notes number {index} about tides.",
                "title": f"Survey {index}",
                "domain": "personal",
                "sensitivity": "private",
            },
        )
    export = _export(origin, tmp_path / "origin.jsonl")
    for chunk in (item["record"] for item in export.body if item["record_type"] == "source_chunk"):
        chunk["text"] += f" Service login: {_token(chunk['id'])}"
    code, target = _import(tmp_path, export.write(tmp_path / "forged.jsonl"))
    assert code == 0
    count, ids = _doctor_flagged(target, capfd)
    assert count == 8
    assert ids == sorted(ids)


def test_export_lists_a_credentialed_memory_once_and_not_as_a_restorable_record(
    tmp_path: Path, seeded: tuple[Path, _Export], capfd: pytest.CaptureFixture[str]
) -> None:
    """A memory row the floor refuses is listed under the memory warning only, and never with the note
    that import restores the record. Scan memory rows in the record pass with no exception for a
    refused row and it is listed twice."""
    database, _original = seeded
    token = _token("memory-export")
    with sqlite_user_connection(database, USER_ID) as conn:
        memory = conn.execute("SELECT id FROM memories LIMIT 1").fetchone()
        conn.execute("UPDATE memories SET canonical_text = ? WHERE id = ?", (f"Service login {token}", memory["id"]))
    capfd.readouterr()
    assert onramp_main(["export", "--db", str(database), "--user-id", USER, "--out", str(tmp_path / "x.jsonl")]) == 0
    err = capfd.readouterr().err
    assert len([line for line in err.splitlines() if memory["id"] in line]) == 1, err
    assert "restores these records" not in err
    assert "holds credential-shaped text" not in err
