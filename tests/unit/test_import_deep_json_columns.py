"""A backup column nested too deeply for the JSON decoder is refused, not crashed on.

``json.loads`` raises RecursionError on text nested about ten thousand levels
(Python 3.12), and the importer's own recursive readers fail from about a
thousand. In v0.19.2 the first reached the generic ``alice_memory_failed`` out of
the credential scan, and the second did the same out of the digest line. Import
now refuses both with ``restore_failed`` after a stderr line that names the
file line, the table and the column, and writes nothing.

Only a JSON column is refused. A string in a JSON column is stored as the text
it is and every read of the row decodes it. A text column (a memory title, a
chunk's text) that looks like deeply nested JSON is only text and is restored.

Every test names the edit that makes it fail.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from alicebot_api import onramp as onramp_module
from alicebot_api.sqlite_store import _JSON_COLUMNS
from tests.unit.fixtures_backup_import import origin_export
from tests.unit.test_import_key_claim import _canonical_line, _Export, _import, _vault

# Past the decoder on any interpreter: 3.12 raises at about ten thousand levels.
DECODER_BOMB_DEPTH = 100_000
JSON_COLUMNS_BY_TYPE = [
    (record_type, column)
    for record_type, (_table, columns) in onramp_module._RECORD_SPECS.items()
    for column in columns
    if column in _JSON_COLUMNS
]


def _bomb(kind: str = "object", depth: int = DECODER_BOMB_DEPTH) -> str:
    if kind == "object":
        return '{"a":' * depth + "1" + "}" * depth
    return "[" * depth + "1" + "]" * depth


def _first_line_of(export: _Export, record_type: str) -> int:
    """The 1-based file line of the first record of that type in the file ``export.write`` makes."""
    for index, item in enumerate(export.body):
        if item["record_type"] == record_type:
            return index + 2
    raise AssertionError(f"no {record_type} record")


def _table(record_type: str) -> str:
    return onramp_module._RECORD_SPECS[record_type][0]


def _refusal(captured_err: str) -> list[str]:
    return [line for line in captured_err.splitlines() if line.startswith("alice-memory: line ")]


def _codes(captured_err: str) -> list[str]:
    return [json.loads(line)["error"]["code"] for line in captured_err.splitlines() if line.startswith("{")]


def _dump(database: Path) -> list[str]:
    connection = sqlite3.connect(database)
    try:
        return list(connection.iterdump())
    finally:
        connection.close()


def test_every_json_column_is_covered_by_the_parametrisation(tmp_path: Path) -> None:
    """The cases below come from the record specs, so a JSON column added later is covered without an
    edit here. Remove a record type from the seed graph and the cases for it stop being reachable;
    this fails first."""
    assert len(JSON_COLUMNS_BY_TYPE) >= 9
    export = origin_export(tmp_path)
    present = {item["record_type"] for item in export.body}
    assert {record_type for record_type, _column in JSON_COLUMNS_BY_TYPE} <= present


@pytest.mark.parametrize(("record_type", "column"), JSON_COLUMNS_BY_TYPE)
@pytest.mark.parametrize("kind", ["object", "array"])
def test_a_json_column_that_is_text_too_deep_to_decode_is_refused_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], record_type: str, column: str, kind: str
) -> None:
    """Delete the _refuse_undecodable_json_columns call in _validate_import_file and the columns no
    other check decodes (aliases, previous_value, source_event_ids and the like) are stored as the
    text they are, so a row recall cannot read lands in the vault and import exits 0. In v0.19.2 the
    code was alice_memory_failed."""
    export = origin_export(tmp_path)
    export.records(record_type)[0][column] = _bomb(kind)
    forged = export.write(tmp_path / "forged.jsonl")
    line_no = _first_line_of(export, record_type)
    database, _context = _vault(tmp_path, "target")
    before = _dump(database)
    capsys.readouterr()
    assert _import(database, forged) == 1
    err = capsys.readouterr().err
    assert _codes(err) == ["restore_failed"], err
    assert _refusal(err) == [
        f"alice-memory: line {line_no}: {_table(record_type)} column {column} is nested too deeply for import to read"
    ]
    assert "Traceback" not in err
    assert _dump(database) == before


def test_a_new_target_is_not_created_when_a_column_is_too_deep(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The refusal comes before the target is touched. Move it after the staging step and a new
    target file appears (or a stale temporary stays beside it)."""
    export = origin_export(tmp_path)
    export.records("event")[0]["payload_json"] = _bomb()
    forged = export.write(tmp_path / "forged.jsonl")
    target = tmp_path / "fresh" / "memory.db"
    capsys.readouterr()
    assert _import(target, forged) == 1
    assert _codes(capsys.readouterr().err) == ["restore_failed"]
    assert not target.exists()
    assert not target.parent.exists() or list(target.parent.iterdir()) == []


@pytest.mark.parametrize("mode", ["skip", "fail"])
def test_the_refusal_is_the_same_in_both_modes(tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str) -> None:
    """The check runs before the mode matters. Make it depend on --mode skip (guard the refusal with
    an existing-row lookup) and the two modes answer differently."""
    export = origin_export(tmp_path)
    export.records("source")[0]["metadata_json"] = _bomb()
    forged = export.write(tmp_path / "forged.jsonl")
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()
    assert _import(database, forged, "--mode", mode) == 1
    assert _codes(capsys.readouterr().err) == ["restore_failed"]


def test_a_deep_column_is_refused_under_quarantine_too(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--quarantine reads every column again (_quarantine_credential_reports, then the stored row). Skip
    the guard when --quarantine is given and the bomb is stored beside the redacted memory, or the
    scan crashes on it with alice_memory_failed."""
    export = origin_export(tmp_path)
    memory_id = str(export.records("memory")[0]["id"])
    export.records("graph_edge")[0]["metadata_json"] = _bomb()
    forged = export.write(tmp_path / "forged.jsonl")
    database, _context = _vault(tmp_path, "target")
    before = _dump(database)
    capsys.readouterr()
    assert _import(database, forged, "--quarantine", memory_id) == 1
    err = capsys.readouterr().err
    assert _codes(err) == ["restore_failed"], err
    assert any(f"{_table('graph_edge')} column metadata_json" in line for line in _refusal(err))
    assert _dump(database) == before


def test_a_text_column_that_looks_like_deep_json_is_restored_as_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only a JSON column is refused. Take the except RecursionError out of
    _column_replaced_by_quarantine and a chunk whose text is ten thousand brackets crashes the
    credential scan with alice_memory_failed, which v0.19.2 did for every record type but a memory.
    Refuse every column instead and the memory title, which v0.19.2 restored, is refused too."""
    export = origin_export(tmp_path)
    text = _bomb("array")
    export.records("source_chunk")[0]["text"] = text
    export.records("memory")[0]["title"] = text
    forged = export.write(tmp_path / "forged.jsonl")
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()
    assert _import(database, forged) == 0, capsys.readouterr().err
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT text FROM source_chunks").fetchone()[0] == text
        assert connection.execute("SELECT title FROM memories").fetchone()[0] == text
    finally:
        connection.close()


@pytest.mark.parametrize("depth", [1_500, 12_000])
@pytest.mark.parametrize(("record_type", "column"), [("memory", "metadata_json"), ("event", "payload_json"), ("source", "metadata_json")])
def test_a_mapping_nested_past_what_the_readers_take_is_refused_naming_its_column(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    record_type: str,
    column: str,
    depth: int,
) -> None:
    """The decoder takes the line (a mapping, not text) but the recursive readers fail from about a
    thousand levels, so v0.19.2 answered alice_memory_failed. Remove the except RecursionError around
    the credential scan and the digest line in _validate_import_file and that returns. 12,000 levels
    is past the decoder as well and is refused at the envelope (drop that except and it returns)."""
    export = origin_export(tmp_path)
    export.records(record_type)[0][column] = "__DEEP__"
    deep = '{"a":' * depth + "1" + "}" * depth
    lines = [_canonical_line("export_header", export.header["record"])]
    for item in export.body:
        lines.append(_canonical_line(item["record_type"], item["record"]).replace('"__DEEP__"', deep))
    lines.append(_canonical_line("export_footer", export.footer["record"]))
    forged = tmp_path / "forged.jsonl"
    forged.write_text("\n".join(lines) + "\n", encoding="utf-8")
    line_no = _first_line_of(export, record_type)
    database, _context = _vault(tmp_path, "target")
    before = _dump(database)
    capsys.readouterr()
    assert _import(database, forged) == 1
    err = capsys.readouterr().err
    assert _codes(err) == ["restore_failed"], err
    if depth > 10_000:
        # Past the decoder the column is not known: the line is.
        assert _refusal(err) == [f"alice-memory: line {line_no}: a record is nested too deeply for import to read"]
    else:
        assert _refusal(err) == [
            f"alice-memory: line {line_no}: {_table(record_type)} column {column} is nested too deeply for import to read"
        ]
    assert "Traceback" not in err
    assert _dump(database) == before


def test_the_key_claim_depth_refusal_names_its_table_and_column(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mapping past the 256-level key claim limit was refused with restore_failed and no reason. It
    now names the table and column too. Drop the exc.column assignment in _downgrade_record_key_claims
    (or the exc.table one in _import_records) and the line loses its name."""
    export = origin_export(tmp_path)
    nested: object = 1
    for _ in range(300):
        nested = {"deeper": nested}
    export.records("event")[0]["payload_json"]["nested"] = nested
    forged = export.write(tmp_path / "forged.jsonl")
    line_no = _first_line_of(export, "event")
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()
    assert _import(database, forged) == 1
    err = capsys.readouterr().err
    assert _codes(err) == ["restore_failed"]
    assert _refusal(err) == [
        f"alice-memory: line {line_no}: event_log column payload_json is nested too deeply for import to read"
    ]


def test_deep_text_under_an_identity_key_is_refused_naming_the_column(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON text under agentic_memory is decoded to look for a claim, and text too deep to decode is
    refused. The reason names metadata_json. Raise a bare _ImportError from _mapping_or_json_text
    (as v0.19.2 did) and the refusal prints nothing."""
    export = origin_export(tmp_path)
    export.records("memory")[0]["metadata_json"]["agentic_memory"] = _bomb()
    forged = export.write(tmp_path / "forged.jsonl")
    line_no = _first_line_of(export, "memory")
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()
    assert _import(database, forged) == 1
    err = capsys.readouterr().err
    assert _codes(err) == ["restore_failed"]
    assert _refusal(err) == [
        f"alice-memory: line {line_no}: memories column metadata_json is nested too deeply for import to read"
    ]


def test_a_stored_revision_with_a_column_too_deep_to_decode_is_named_under_skip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """v0.19.0 stored a memory revision whose previous_value was text too deep for the decoder, and a
    revision is append-only, so no later open rewrites it. --mode skip decodes the stored column to
    compare it. Let that RecursionError escape (drop the except in _collision_is_identical) and the
    code is alice_memory_failed."""
    export = origin_export(tmp_path)
    database, _context = _vault(tmp_path, "target")
    assert _import(database, export.write(tmp_path / "clean.jsonl")) == 0
    revision = dict(export.records("memory_revision")[0])
    revision["id"] = "00000000-0000-4000-8000-00000000d33b"
    revision["sequence_no"] = int(revision["sequence_no"]) + 1
    revision["revision_number"] = int(revision["revision_number"]) + 1
    export.body.append({"record_type": "memory_revision", "record": {**revision, "previous_value": {"a": 1}}})
    connection = sqlite3.connect(database)
    try:
        stored = {**revision, "previous_value": _bomb(), "user_id": "00000000-0000-0000-0000-000000000001"}
        columns = onramp_module._RECORD_SPECS["memory_revision"][1]
        values = [
            onramp_module._encode_column_value(column, stored.get(column)) for column in columns
        ]
        connection.execute(
            f"INSERT INTO memory_revisions ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
        connection.commit()
    finally:
        connection.close()
    second = export.write(tmp_path / "second.jsonl")
    revision_line = len(export.body) + 1  # the appended record is the last line before the footer
    before = _dump(database)
    capsys.readouterr()
    assert _import(database, second, "--mode", "skip") == 1
    err = capsys.readouterr().err
    assert _codes(err) == ["restore_failed"], err
    assert _refusal(err) == [
        f"alice-memory: line {revision_line}: the memory_revisions row already in the vault has previous_value "
        "nested too deeply to compare with the file"
    ]
    assert _dump(database) == before


def test_a_stored_column_too_deep_to_decode_is_named_by_the_comparison_itself() -> None:
    """The same refusal on the function, without a vault. Drop the except RecursionError for the stored
    value in _collision_is_identical and the RecursionError escapes."""
    with pytest.raises(onramp_module._ImportDepthError) as raised:
        onramp_module._collision_is_identical(
            {"metadata_json": _bomb()},
            ("metadata_json",),
            ('{"a":1}',),
            table="sources",
            line_no=7,
        )
    assert str(raised.value) == (
        "line 7: the sources row already in the vault has metadata_json nested too deeply to compare with the file"
    )
    # Text that decodes is compared as decoded JSON, as before.
    assert onramp_module._collision_is_identical(
        {"metadata_json": '{"a": 1}'}, ("metadata_json",), ('{"a":1}',), table="sources", line_no=7
    )


def test_json_column_helper_names_the_column_when_text_is_too_deep() -> None:
    """_json_column is the second check on the way to the same refusal for a memory. Drop its except
    RecursionError and the RecursionError escapes."""
    with pytest.raises(onramp_module._ImportDepthError) as raised:
        onramp_module._json_column(_bomb(), column="value")
    assert str(raised.value) == "memories column value is nested too deeply for import to read"
    assert onramp_module._json_column("not json", column="value") == "not json"
    assert onramp_module._json_column('{"a": 1}', column="value") == {"a": 1}


def test_quarantine_check_reads_deep_text_as_not_the_fixed_object() -> None:
    """Text too deep for the decoder is not {"quarantined": true}. Drop the except RecursionError in
    _column_replaced_by_quarantine and the call raises."""
    assert onramp_module._column_replaced_by_quarantine(_bomb()) is False
    assert onramp_module._column_replaced_by_quarantine('{"quarantined":true}') is True


def test_unreasoned_recursion_still_ends_as_restore_failed_with_a_fixed_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RecursionError that no check named still ends the import cleanly. Narrow the restore handler
    back to (_BackupError, _ImportError, OSError, sqlite3.Error) and it escapes as alice_memory_failed."""
    export = origin_export(tmp_path)
    forged = export.write(tmp_path / "forged.jsonl")
    database, _context = _vault(tmp_path, "target")

    def overflow(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(onramp_module, "_downgrade_record_key_claims", overflow)
    capsys.readouterr()
    assert _import(database, forged) == 1
    err = capsys.readouterr().err
    assert _codes(err) == ["restore_failed"]
    assert "alice-memory: a record is nested too deeply for import to read" in err.splitlines()


def test_no_value_from_the_file_reaches_the_refusal_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The line names a line number, a table and a column, never a value. Put the column's text in the
    message and the marker key planted in it is printed."""
    export = origin_export(tmp_path)
    export.records("memory")[0]["metadata_json"] = '{"zq9xk7":' * DECODER_BOMB_DEPTH + "1" + "}" * DECODER_BOMB_DEPTH
    forged = export.write(tmp_path / "forged.jsonl")
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()
    assert _import(database, forged) == 1
    err = capsys.readouterr().err
    assert _codes(err) == ["restore_failed"]
    assert "zq9xk7" not in err
