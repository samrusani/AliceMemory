"""Gaps the review of the deep JSON and skip import change found, and the fixes for them.

1. The scratch connection that ``--mode skip`` opens to ask what the schema bootstrap
   will leave in a row (``_BackfillProbe``) is closed when ``_import_records`` ends,
   whether it returns or raises. Nothing pinned that.
2. JSON text in the four ``memory_revisions`` JSON columns (``previous_value``,
   ``new_value``, ``source_event_ids``, ``candidate``) is stored as the text it is,
   and no reader decodes those columns while a file is validated. Text nested past the
   decoder (about ten thousand levels) was already refused. Text nested between the
   cap that the key claim walk uses (256 levels) and the decoder was stored, and
   ``alice-memory export`` then failed on it with the generic ``alice_memory_failed``.
   Import now refuses it with ``restore_failed``, so a vault made by import never holds
   what export cannot write. The same cap applies to ``memories.value`` (the credential
   scan takes text up to about a thousand levels, a little more than export can write),
   to ``memories.source_event_ids`` and to ``vnext_entities.aliases`` (a list of nested
   lists passes their shape check). A sweep of every JSON column pins the property.
3. A header whose extra key is nested past what the digest line takes ended with
   ``alice_memory_failed``. It is ``restore_failed`` with a reason line.
4. A vault that already holds such text (a v0.19.0 import could store it) answers
   ``export_failed`` after a line naming the table and the column, and never a value.

Every test names the edit that makes it fail.
"""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from alicebot_api import onramp as onramp_module
from tests.unit.fixtures_backup_import import origin_export, write_legacy
from tests.unit.test_import_deep_json_columns import JSON_COLUMNS_BY_TYPE
from tests.unit.test_import_key_claim import USER_ID, _canonical_line, _import, _vault

REVISION_COLUMNS = ("previous_value", "new_value", "source_event_ids", "candidate")
# 256 levels is the most the key claim walk takes. The decoder takes about ten thousand.
CAP = onramp_module._KEY_CLAIM_MAX_DEPTH
MARKER = "zq9xk7"


def _nested_text(levels: int, kind: str = "object", *, key: str = "a") -> str:
    """JSON text nested ``levels`` deep around one scalar."""
    if kind == "object":
        return f'{{"{key}":' * levels + "1" + "}" * levels
    return "[" * levels + "1" + "]" * levels


def _codes(err: str) -> list[str]:
    return [json.loads(line)["error"]["code"] for line in err.splitlines() if line.startswith("{")]


def _reasons(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.endswith("is nested too deeply for import to read")]


def _record_line(export: Any, record_type: str) -> int:
    for index, item in enumerate(export.body):
        if item["record_type"] == record_type:
            return index + 2
    raise AssertionError(f"no {record_type} record")


def _revision_line(export: Any) -> int:
    return _record_line(export, "memory_revision")


def _export_of(database: Path, out: Path, *extra: str) -> int:
    return onramp_module.main(["export", "--db", str(database), "--user-id", USER_ID, "--out", str(out), *extra])


def _store_row_with(database: Path, export: Any, record_type: str, **columns: object) -> None:
    """Insert a copy of an exported row, with ``columns`` replaced, straight into the vault.

    A revision and an event are append-only, so a row is added and never updated. This is what
    an import before this change could have left in a vault.
    """
    table, names = onramp_module._RECORD_SPECS[record_type]
    template = dict(export.records(record_type)[0])
    template["id"] = "00000000-0000-4000-8000-00000000d33b"
    if record_type == "memory_revision":
        template["sequence_no"] = int(template["sequence_no"]) + 1
        template["revision_number"] = int(template["revision_number"]) + 1
    stored = {**template, **columns, "user_id": USER_ID}
    values = [onramp_module._encode_column_value(name, stored.get(name)) for name in names]
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})", values
        )
        connection.commit()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 1. the scratch probe is closed
# ---------------------------------------------------------------------------


def _legacy_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Two headerless files. The first stores rows the bootstrap will fill in; the second also changes a title."""

    def agent_only(export: Any) -> None:
        for record in export.records("source"):
            record["dedupe_key"] = None
        for record in export.records("memory"):
            record["created_by_agent_id"] = None
            record["run_id"] = None
            record["metadata_json"] = {"agent_id": "agent-legacy", "agent_run_id": "run-legacy-1"}

    export = origin_export(tmp_path)
    agent_only(export)
    first = write_legacy(export, tmp_path / "first.jsonl")
    export.records("memory")[0]["title"] = "A title the vault does not hold"
    second = write_legacy(export, tmp_path / "second.jsonl")
    return first, second


class _RecordingProbe(onramp_module._BackfillProbe):
    instances: list["_RecordingProbe"] = []

    def __init__(self) -> None:
        super().__init__()
        self.opened: list[sqlite3.Connection] = []
        _RecordingProbe.instances.append(self)

    def _connection(self) -> sqlite3.Connection:
        connection = super()._connection()
        if not self.opened or self.opened[-1] is not connection:
            self.opened.append(connection)
        return connection


def _assert_closed(probe: _RecordingProbe) -> None:
    assert probe.opened, "the probe was never used, so the assertions below would be vacuous"
    assert probe._scratch is None
    for connection in probe.opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_the_scratch_probe_is_closed_when_the_import_returns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second skip import of legacy rows opens the probe's scratch database, and closes it.

    Mutation: delete ``probe.close()`` from the ``finally`` clause of
    ``_import_records``, or make ``_BackfillProbe.close`` leave the connection open.
    """

    first, _second = _legacy_pair(tmp_path)
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()
    assert _import(database, first, "--mode", "skip") == 0, capsys.readouterr().err
    _RecordingProbe.instances = []
    monkeypatch.setattr(onramp_module, "_BackfillProbe", _RecordingProbe)

    assert _import(database, first, "--mode", "skip") == 0, capsys.readouterr().err

    assert len(_RecordingProbe.instances) == 1
    _assert_closed(_RecordingProbe.instances[0])


def test_the_scratch_probe_is_closed_when_the_import_raises(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source rows use the probe, then a memory row that differs is refused. The probe is closed anyway.

    Mutation: move ``probe.close()`` out of the ``finally`` clause, after the loop. It is then
    skipped when the refusal raises, and the scratch database stays open.
    """

    first, second = _legacy_pair(tmp_path)
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()
    assert _import(database, first, "--mode", "skip") == 0, capsys.readouterr().err
    _RecordingProbe.instances = []
    monkeypatch.setattr(onramp_module, "_BackfillProbe", _RecordingProbe)
    capsys.readouterr()

    assert _import(database, second, "--mode", "skip") == 1

    err = capsys.readouterr().err
    assert _codes(err) == ["restore_failed"], err
    assert len(_RecordingProbe.instances) == 1
    _assert_closed(_RecordingProbe.instances[0])


# ---------------------------------------------------------------------------
# 2. text in a memory_revisions column
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("column", REVISION_COLUMNS)
@pytest.mark.parametrize("kind", ["object", "array"])
def test_revision_text_nested_past_the_cap_is_refused_naming_the_column(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], column: str, kind: str
) -> None:
    """257 levels is one past the cap. Each of the four columns is refused, and nothing is written.

    Mutation: delete the column from ``_TEXT_DEPTH_CAPPED_COLUMNS`` (the case for that column
    then imports and exits 0), or delete the depth check in ``_refuse_undecodable_json_columns``.
    """

    export = origin_export(tmp_path)
    export.records("memory_revision")[0][column] = _nested_text(CAP + 1, kind)
    forged = export.write(tmp_path / "forged.jsonl")
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()

    assert _import(database, forged) == 1

    err = capsys.readouterr().err
    assert _codes(err) == ["restore_failed"], err
    assert _reasons(err) == [
        f"alice-memory: line {_revision_line(export)}: memory_revisions column {column} is nested too deeply "
        "for import to read"
    ]
    assert "Traceback" not in err
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT count(*) FROM memory_revisions").fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.parametrize("column", REVISION_COLUMNS)
def test_revision_text_at_the_cap_is_restored_and_exports_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], column: str
) -> None:
    """256 levels is the most the cap takes. It imports, and the vault exports.

    This is the property the cap exists for: a vault that import made can be exported.

    Mutation: compare ``> CAP`` in place of ``> CAP + 1`` (the text at the cap is refused),
    or ``> CAP + 2`` (the text one past it is stored, which the test above catches).
    """

    export = origin_export(tmp_path)
    text = _nested_text(CAP)
    export.records("memory_revision")[0][column] = text
    forged = export.write(tmp_path / "forged.jsonl")
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()

    assert _import(database, forged) == 0, capsys.readouterr().err

    connection = sqlite3.connect(database)
    try:
        stored = connection.execute(f"SELECT {column} FROM memory_revisions").fetchone()[0]
    finally:
        connection.close()
    assert stored == text
    capsys.readouterr()
    assert _export_of(database, tmp_path / "again.jsonl") == 0, capsys.readouterr().err


@pytest.mark.parametrize("levels", [2_000, 9_000])
def test_revision_text_between_the_cap_and_the_decoder_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], levels: int
) -> None:
    """The decoder takes this text and the writer does not, which is the range that got through.

    Mutation: delete the depth check in ``_refuse_undecodable_json_columns``. The text is then
    stored, import exits 0, and the export of the vault fails with ``alice_memory_failed``.
    """

    export = origin_export(tmp_path)
    export.records("memory_revision")[0]["previous_value"] = _nested_text(levels)
    forged = export.write(tmp_path / "forged.jsonl")
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()

    assert _import(database, forged) == 1

    err = capsys.readouterr().err
    assert _codes(err) == ["restore_failed"], err
    assert _reasons(err)[0].endswith("memory_revisions column previous_value is nested too deeply for import to read")


def test_the_cap_reads_only_text_in_a_json_column(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The same depth in a text column is not refused by this rule.

    ``memories.title`` and ``memory_revisions.text_after`` are text columns, and a text
    column that only looks like JSON is only text, like a source chunk's text.

    Mutation: apply the cap to every column of ``memories`` or of ``memory_revisions`` (drop
    the ``_JSON_COLUMNS`` test and read each column of the table). The title, or the text,
    is then refused.
    """

    export = origin_export(tmp_path)
    export.records("memory")[0]["title"] = _nested_text(CAP + 44)
    export.records("memory_revision")[0]["text_after"] = _nested_text(CAP + 44)
    forged = export.write(tmp_path / "forged.jsonl")
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()

    assert _import(database, forged) == 0, capsys.readouterr().err


@pytest.mark.parametrize("levels", [CAP + 1, CAP + 44, 990, 1_000])
@pytest.mark.parametrize("kind", ["object", "array"])
def test_memory_value_text_nested_past_the_cap_is_refused_naming_the_column(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], levels: int, kind: str
) -> None:
    """``memories.value`` text past 256 levels is refused, including the 984 to 1,000 range.

    The credential scan takes that text and the export writer does not. In v0.19.2, and in
    this change before this test, text between about 984 and 1,000 levels (about 993 to
    1,001 outside a test run, the writer's limit depends on how deep the stack is) was
    stored, and the export of the vault ended with ``export_failed``.

    Mutation: delete ``"memories": frozenset({"value"})`` from ``_TEXT_DEPTH_CAPPED_COLUMNS``.
    The value is then stored and import exits 0.
    """

    export = origin_export(tmp_path)
    export.records("memory")[0]["value"] = _nested_text(levels, kind)
    forged = export.write(tmp_path / "forged.jsonl")
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()

    assert _import(database, forged) == 1

    err = capsys.readouterr().err
    assert _codes(err) == ["restore_failed"], err
    assert _reasons(err) == [
        f"alice-memory: line {_record_line(export, 'memory')}: memories column value is nested too deeply "
        "for import to read"
    ]
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.parametrize("kind", ["object", "array"])
def test_memory_value_text_at_the_cap_is_restored_and_exports_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    """256 levels is the most the cap takes in ``memories.value``. It imports, and the vault exports.

    Mutation: compare ``> CAP`` in place of ``> CAP + 1`` in ``_refuse_undecodable_json_columns``
    (the text at the cap is refused).
    """

    export = origin_export(tmp_path)
    text = _nested_text(CAP, kind)
    export.records("memory")[0]["value"] = text
    forged = export.write(tmp_path / "forged.jsonl")
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()

    assert _import(database, forged) == 0, capsys.readouterr().err

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT value FROM memories").fetchone()[0] == text
    finally:
        connection.close()
    capsys.readouterr()
    assert _export_of(database, tmp_path / "again.jsonl") == 0, capsys.readouterr().err


@pytest.mark.parametrize(("record_type", "column"), JSON_COLUMNS_BY_TYPE)
def test_a_vault_made_by_import_exports_for_text_in_every_json_column(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], record_type: str, column: str
) -> None:
    """For each JSON column of each record type, nested text is either refused or the vault exports.

    The depths run from well below the cap to the 1,000 levels where the credential scan stops
    taking text. The test asserts the property the docs state, that a vault made by import
    never holds a row that export cannot write, and not the reason a column is refused. For a
    column the cap covers, the cases at 200 levels are accepted, so the check is not vacuous.

    Mutation: delete one column from ``_TEXT_DEPTH_CAPPED_COLUMNS``: ``previous_value``,
    ``new_value``, ``source_event_ids`` or ``candidate`` of ``memory_revisions``, ``value`` or
    ``source_event_ids`` of ``memories``, or ``aliases`` of ``vnext_entities``. Each has a depth
    between 984 and 1,000 where import then accepts the text and export ends with
    ``export_failed``, and the case for that column fails.
    """

    violations: list[str] = []
    accepted_at_all = False
    for levels in (200, 300, 990, 1_000):
        for kind in ("object", "array"):
            case = tmp_path / f"{levels}-{kind}"
            case.mkdir()
            export = origin_export(case)
            export.records(record_type)[0][column] = _nested_text(levels, kind)
            forged = export.write(case / "forged.jsonl")
            database, _context = _vault(case, "target")
            capsys.readouterr()
            if _import(database, forged) != 0:
                continue
            accepted_at_all = accepted_at_all or levels <= CAP
            capsys.readouterr()
            if _export_of(database, case / "again.jsonl") != 0:
                violations.append(f"{levels} levels as {kind}: {_codes(capsys.readouterr().err)}")
    assert violations == []
    table = onramp_module._RECORD_SPECS[record_type][0]
    if column in onramp_module._TEXT_DEPTH_CAPPED_COLUMNS.get(table, frozenset()):
        assert accepted_at_all, "a column the cap covers still takes text below the cap"


def test_no_value_from_the_file_reaches_the_revision_refusal_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The line names a line number, a table and a column. A key planted in the text is not printed.

    Mutation: add the column's text to the message of ``_ImportDepthError``.
    """

    export = origin_export(tmp_path)
    export.records("memory_revision")[0]["candidate"] = _nested_text(CAP + 5, key=MARKER)
    forged = export.write(tmp_path / "forged.jsonl")
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()

    assert _import(database, forged) == 1

    assert MARKER not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 3. the header digest
# ---------------------------------------------------------------------------


def _forged_with_deep_header(tmp_path: Path, scope: str, levels: int) -> Path:
    """A file whose header carries an extra key nested ``levels`` deep, with the footer to match."""
    import hashlib

    export = origin_export(tmp_path)
    header = dict(export.header["record"])
    header["integrity"] = {**header["integrity"], "scope": scope}
    header["zz_extra"] = "__DEEP__"
    deep = _nested_text(levels, key=MARKER)
    lines = [_canonical_line("export_header", header).replace('"__DEEP__"', deep)]
    digest = hashlib.sha256()
    for item in export.body:
        line = _canonical_line(item["record_type"], item["record"])
        lines.append(line)
        digest.update((line + "\n").encode("utf-8"))
    footer = {**export.footer["record"], "sha256": digest.hexdigest()}
    lines.append(_canonical_line("export_footer", footer))
    path = tmp_path / f"deep-header-{scope}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "scope",
    [onramp_module._EXPORT_INTEGRITY_SCOPE, onramp_module._LEGACY_V2_INTEGRITY_SCOPE],
    ids=["current scope", "legacy scope"],
)
def test_a_header_nested_past_the_digest_line_is_refused_with_a_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], scope: str
) -> None:
    """The decoder takes the header and the digest line does not. It was ``alice_memory_failed``.

    The legacy scope puts the header line into the file digest too, so both calls that build
    a line from the header are covered.

    Mutation: remove the ``except RecursionError`` around the two ``_export_line`` calls on
    the header in ``_validate_import_file``. The refusal is then ``alice_memory_failed``.
    """

    forged = _forged_with_deep_header(tmp_path, scope, 1_500)
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()

    assert _import(database, forged) == 1

    err = capsys.readouterr().err
    assert _codes(err) == ["restore_failed"], err
    assert _reasons(err) == ["alice-memory: line 1: a record is nested too deeply for import to read"]
    assert "Traceback" not in err
    assert MARKER not in err


# ---------------------------------------------------------------------------
# 4. export of a vault that already holds deep text
# ---------------------------------------------------------------------------


def _vault_holding_a_clean_import(tmp_path: Path) -> tuple[Path, Any]:
    """A vault restored from a real export, and that export."""
    export = origin_export(tmp_path)
    database, _context = _vault(tmp_path, "holder")
    assert _import(database, export.write(tmp_path / "clean.jsonl")) == 0
    return database, export


@pytest.mark.parametrize("column", REVISION_COLUMNS)
@pytest.mark.parametrize("levels", [2_000, 100_000], ids=["decodable", "past the decoder"])
def test_export_names_the_table_and_column_of_deep_text_a_vault_already_holds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], column: str, levels: int
) -> None:
    """``export_failed`` after one line that names the table and the column, and no file is left.

    2,000 levels decode and then fail in the writer. 100,000 fail in the decoder itself.

    Mutation: delete the ``except RecursionError`` around the per-record work in
    ``_write_export`` (the 2,000 cases are then ``alice_memory_failed``), or the one around
    ``json.loads`` in ``_decoded_rows`` (the 100,000 cases are), or name the first JSON
    column of the row in place of ``_deepest_column``.
    """

    database, export = _vault_holding_a_clean_import(tmp_path)
    _store_row_with(database, export, "memory_revision", **{column: _nested_text(levels, key=MARKER)})
    out = tmp_path / "out" / "export.jsonl"
    capsys.readouterr()

    assert _export_of(database, out) == 1

    err = capsys.readouterr().err
    assert _codes(err) == ["export_failed"], err
    assert [line for line in err.splitlines() if line.endswith("is nested too deeply for export to write")] == [
        f"alice-memory: memory_revisions column {column} is nested too deeply for export to write"
    ]
    assert "Traceback" not in err
    assert MARKER not in err
    assert not out.exists()
    assert list(out.parent.glob(".*")) == [], "no temporary file is left beside the output"


def test_export_to_standard_output_answers_the_same(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The ``--out``-less path has its own handler, and it prints the same line and code.

    Mutation: leave the ``_ExportDepthError`` line out of the standard output handler in
    ``_run_export``. The code is still ``export_failed`` but the line is missing.
    """

    database, export = _vault_holding_a_clean_import(tmp_path)
    _store_row_with(database, export, "memory_revision", candidate=_nested_text(2_000))
    capsys.readouterr()

    assert onramp_module.main(["export", "--db", str(database), "--user-id", USER_ID]) == 1

    err = capsys.readouterr().err
    assert _codes(err) == ["export_failed"], err
    assert (
        "alice-memory: memory_revisions column candidate is nested too deeply for export to write" in err.splitlines()
    )


def test_the_decoder_error_names_the_table_of_the_record_type_it_was_given() -> None:
    """The table comes from the record type of the query, not from a fixed name.

    SQLite refuses JSON nested past a thousand levels in a column with a ``json_valid`` check, so
    a deep row in another table is built here on a scratch table, and only the decode step is run.

    Mutation: hard-code ``memory_revisions`` as the table in ``_decoded_rows``, or name no
    column. The entity row below then names the wrong table.
    """

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE scratch (id TEXT, aliases TEXT)")
        connection.execute("INSERT INTO scratch VALUES ('e1', ?)", (_nested_text(100_000, "array"),))
        with pytest.raises(onramp_module._ExportDepthError) as raised:
            list(onramp_module._decoded_rows(connection, "SELECT id, aliases FROM scratch", (), record_type="entity"))
    finally:
        connection.close()

    assert str(raised.value) == "vnext_entities column aliases is nested too deeply for export to write"


def test_the_writer_error_names_the_deepest_column_and_its_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nested past the writer and not the decoder: the column that nests deepest is named.

    A mapping row, as ``_decoded_rows`` returns it, holds the decoded value. The shallow
    columns are not named, and a value never is. The writer is made to overflow the stack
    on that row, which is what a row nested a thousand levels does. The record is an
    entity, so the table is not the revision table.

    Mutation: name the first JSON column of the row, or the last, in place of
    ``_deepest_column`` in the ``except RecursionError`` of ``_write_export``, or take the
    table from anything but the record type of the row.
    """

    row: dict[str, object] = {
        "id": "x",
        "aliases": ["a"],
        "metadata_json": json.loads(_nested_text(900)),
        "project_scope_json": ["p", {"q": 1}],
    }
    real_line = onramp_module._export_line

    class _Connection:
        def __enter__(self) -> "_Connection":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def overflow(record_type: str, record: object) -> str:
        if record_type == "entity":
            raise RecursionError("maximum recursion depth exceeded")
        return real_line(record_type, record)

    monkeypatch.setattr(onramp_module, "_export_rows", lambda _conn, _user_id: iter([("entity", row)]))
    monkeypatch.setattr(onramp_module, "_prepared_export_connection", lambda *_args, **_kwargs: _Connection())
    monkeypatch.setattr(onramp_module, "_export_line", overflow)

    with pytest.raises(onramp_module._ExportDepthError) as raised:
        onramp_module._write_export(io.StringIO(), db_path=Path("unused"), user_id=USER_ID)  # type: ignore[arg-type]

    assert str(raised.value) == "vnext_entities column metadata_json is nested too deeply for export to write"


def test_a_vault_with_text_at_the_cap_still_exports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The typed error is for rows the writer cannot take. Text 300 levels down exported before and does now.

    Mutation: refuse in the export what the import cap refuses (apply ``CAP`` in ``_write_export``).
    A vault holding this text, which exported without trouble, would then stop exporting.
    """

    database, export = _vault_holding_a_clean_import(tmp_path)
    _store_row_with(database, export, "memory_revision", previous_value=_nested_text(300))
    capsys.readouterr()

    assert _export_of(database, tmp_path / "out.jsonl") == 0, capsys.readouterr().err
