"""``alice-memory import --mode skip`` skips a legacy row that equals the stored one in meaning.

The schema bootstrap fills a few columns from the rest of a row on every open
(``apply_row_backfills``): a source's ``dedupe_key``, a memory's
``created_by_agent_id`` and ``run_id``, and the legacy nested project scope in
``metadata_json`` and ``project_id``. Import stores a file's row as the file
gives it, so a file from an older vault, or a hand-made one, that leaves such a
column empty or does not carry it yet is stored with the column empty. The next
open of the vault fills it in, and a second import of the same file then found
the stored row different and refused with ``restore_failed`` (v0.19.2).

Skip now ignores a column the file row does not carry, and compares a memory or
source that gave a derived column empty as the bootstrap will leave it. A
column the file gives with a value is compared as before, so a row that
really differs is still refused.

Every test names the edit that makes it fail.
"""

from __future__ import annotations

import inspect
import json
import re
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from alicebot_api import onramp as onramp_module
from alicebot_api import sqlite_schema
from alicebot_api.sqlite_schema import ROW_BACKFILL_TABLES, apply_row_backfills, bootstrap_sqlite_schema
from tests.unit.fixtures_backup_import import origin_export, write_legacy
from tests.unit.test_import_key_claim import _Export, _import, _vault

AGENT = "agent-legacy"
RUN = "run-legacy-1"
SCOPE_PROJECT = "proj-legacy"
KEY_CLAIM = {"agent_id": AGENT, "auth": "agent_api_key"}


def _agent_metadata() -> dict[str, object]:
    """What a writer from before the attribution columns kept: the agent only in metadata_json."""
    return {
        "agent_id": AGENT,
        "agent_run_id": RUN,
        "agent_identity": {"agent_id": AGENT, "agent_run_id": RUN, "auth": "unauthenticated_local"},
    }


def _legacy_file(
    tmp_path: Path,
    edit: Callable[[_Export], None] | None = None,
    *,
    name: str = "legacy.jsonl",
) -> Path:
    export = origin_export(tmp_path)
    if edit is not None:
        edit(export)
    return write_legacy(export, tmp_path / name)


def _receipt(out: str) -> tuple[int, int]:
    """(imported, skipped) from the first line of the receipt."""
    match = re.search(r"imported (\d+) records from .* \((\d+) skipped\)", out)
    assert match, out
    return int(match.group(1)), int(match.group(2))


def _skip_again(tmp_path: Path, backup: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str, Path]:
    """First import, then the second with --mode skip. Returns the second's exit code, stdout, stderr, and the vault."""
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()
    assert _import(database, backup, "--mode", "skip") == 0, capsys.readouterr().err
    capsys.readouterr()
    code = _import(database, backup, "--mode", "skip")
    captured = capsys.readouterr()
    return code, captured.out, captured.err, database


def _total(backup: Path) -> int:
    return sum(1 for line in backup.read_text(encoding="utf-8").splitlines() if line.strip())


def _columns(database: Path, table: str, *columns: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(database)
    try:
        return list(connection.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY id"))
    finally:
        connection.close()


def _assert_all_skipped(code: int, out: str, err: str, backup: Path) -> None:
    assert code == 0, err
    assert _receipt(out) == (0, _total(backup)), out


def test_legacy_file_without_the_columns_added_since_is_skipped_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A headerless file from before sources had a dedupe_key and memories had fact_keys carries neither.
    Import stores NULL, the next open fills the dedupe_key, a fact key backfill fills the fact_keys, and
    v0.19.2 refused the second import. Compare every column and not only the ones the file carries (drop
    the `compared` filter in _import_records) and it is refused again, on fact_keys, which no bootstrap
    step derives."""

    def edit(export: _Export) -> None:
        for record in export.records("source"):
            del record["dedupe_key"]
        for record in export.records("memory"):
            del record["fact_keys"]

    backup = _legacy_file(tmp_path, edit)
    database, _context = _vault(tmp_path, "target")
    capsys.readouterr()
    assert _import(database, backup, "--mode", "skip") == 0, capsys.readouterr().err
    assert _columns(database, "memories", "fact_keys") == [(None,)]
    connection = sqlite3.connect(database)
    try:
        # What `scripts/backfill_memory_fact_keys.py` does to a vault that holds memories without keys.
        connection.execute("UPDATE memories SET fact_keys = 'roundtrip ownership backup restore'")
        connection.commit()
    finally:
        connection.close()
    capsys.readouterr()
    code = _import(database, backup, "--mode", "skip")
    captured = capsys.readouterr()
    _assert_all_skipped(code, captured.out, captured.err, backup)
    # The vault holds what the first import, the bootstrap and the backfill left; nothing from the file replaced it.
    assert _columns(database, "sources", "dedupe_key") == [("hash-round-trip",)]
    assert _columns(database, "memories", "fact_keys") == [("roundtrip ownership backup restore",)]


def test_memory_that_keeps_its_agent_only_in_metadata_is_skipped_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The file gives created_by_agent_id and run_id empty and the metadata carries the agent. The
    bootstrap fills both columns on the next open. Skip the probe in _stored_row_matches and the second
    import is refused, which is what v0.19.2 did."""

    def edit(export: _Export) -> None:
        for record in export.records("memory"):
            record["created_by_agent_id"] = None
            record["run_id"] = None
            record["metadata_json"] = _agent_metadata()

    backup = _legacy_file(tmp_path, edit)
    code, out, err, database = _skip_again(tmp_path, backup, capsys)
    _assert_all_skipped(code, out, err, backup)
    assert _columns(database, "memories", "created_by_agent_id", "run_id") == [(AGENT, RUN)]


def test_memory_with_its_project_scope_only_under_agentic_memory_is_skipped_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Early writers kept the scope at metadata_json.agentic_memory.project_scope. The bootstrap adds the
    canonical project_scope and sets project_id, which rewrites metadata_json too. Take
    _backfill_legacy_memory_project_scopes out of apply_row_backfills and this row is refused."""

    def edit(export: _Export) -> None:
        for record in export.records("memory"):
            record["project_id"] = None
            record["metadata_json"] = {"agentic_memory": {"project_scope": [SCOPE_PROJECT]}}

    backup = _legacy_file(tmp_path, edit)
    code, out, err, database = _skip_again(tmp_path, backup, capsys)
    _assert_all_skipped(code, out, err, backup)
    (project_id,) = _columns(database, "memories", "project_id")[0]
    assert project_id == SCOPE_PROJECT


def test_source_given_without_a_dedupe_key_is_skipped_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The file carries the column and gives it NULL. The bootstrap derives the key from the content on
    the next open. Take _backfill_source_dedupe_keys out of apply_row_backfills and the row is refused."""

    def edit(export: _Export) -> None:
        for record in export.records("source"):
            record["dedupe_key"] = None

    backup = _legacy_file(tmp_path, edit)
    code, out, err, database = _skip_again(tmp_path, backup, capsys)
    _assert_all_skipped(code, out, err, backup)
    assert _columns(database, "sources", "dedupe_key") == [("hash-round-trip",)]


def test_a_versioned_file_with_an_empty_derived_column_is_skipped_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same rows in a versioned, re-signed file: the footer check and the schema check pass, and every
    column is present, so only the settled comparison can match. Skip the probe in _stored_row_matches
    (return False before the second loop) and the second import is refused."""
    export = origin_export(tmp_path)
    for record in export.records("memory"):
        record["created_by_agent_id"] = None
        record["run_id"] = None
        record["metadata_json"] = _agent_metadata()
    backup = export.write(tmp_path / "versioned.jsonl")
    code, out, err, _database = _skip_again(tmp_path, backup, capsys)
    assert code == 0, err
    assert _receipt(out)[0] == 0


def test_legacy_row_that_also_carries_a_key_claim_is_skipped_against_a_row_with_the_claim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A vault imports its own legacy file. The stored row holds a real key claim and the file row holds
    the same claim with an empty derived column, so only the file's row as it was given, settled, matches.
    Settle only the rewritten row (`candidates[:1]` in _stored_row_matches) and it is refused."""
    export = origin_export(tmp_path)
    metadata = _agent_metadata()
    metadata["agent_identity"] = dict(KEY_CLAIM)
    memory = export.records("memory")[0]
    memory["metadata_json"] = metadata
    memory["created_by_agent_id"] = None
    memory["run_id"] = None
    backup = write_legacy(export, tmp_path / "legacy.jsonl")
    database, _context = _vault(tmp_path, "target")
    assert _import(database, backup, "--mode", "skip") == 0
    # The vault's own row carries the claim, as a key's writes do.
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE memories SET metadata_json = ? WHERE id = ?", (json.dumps(metadata), memory["id"])
        )
        connection.commit()
    finally:
        connection.close()
    capsys.readouterr()
    code = _import(database, backup, "--mode", "skip")
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert _receipt(captured.out) == (0, _total(backup))


@pytest.mark.parametrize(
    "case",
    [
        "different_text",
        "file_gives_another_agent",
        "stored_agent_is_not_what_the_metadata_names",
        "stored_dedupe_key_is_not_what_the_content_gives",
    ],
)
def test_a_legacy_row_that_really_differs_is_still_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    """Equal in meaning is not a wildcard. Treat an empty column in the file as matching anything (skip
    the column when `expected_value is None` in _collision_is_identical) and the last two cases are
    accepted; compare no column past the id and the first two are too. Each case is a row the vault must
    not merge, and the vault is left as it was."""
    export = origin_export(tmp_path)
    memory = export.records("memory")[0]
    source = export.records("source")[0]
    memory["created_by_agent_id"] = AGENT
    memory["run_id"] = RUN
    memory["metadata_json"] = _agent_metadata()
    first = write_legacy(export, tmp_path / "first.jsonl")
    database, _context = _vault(tmp_path, "target")
    assert _import(database, first, "--mode", "skip") == 0

    def stored_update(statement: str, *parameters: object) -> None:
        connection = sqlite3.connect(database)
        try:
            connection.execute(statement, parameters)
            connection.commit()
        finally:
            connection.close()

    # The file leaves both derived columns empty in every case but the second, so the settled
    # comparison is the one that runs.
    memory["created_by_agent_id"] = None
    source["dedupe_key"] = None
    if case == "different_text":
        memory["canonical_text"] = "A different sentence under the same id."
    elif case == "file_gives_another_agent":
        memory["created_by_agent_id"] = "somebody-else"
    elif case == "stored_agent_is_not_what_the_metadata_names":
        stored_update("UPDATE memories SET created_by_agent_id = 'somebody-else' WHERE id = ?", memory["id"])
    else:
        stored_update("UPDATE sources SET dedupe_key = 'capture-md5:other' WHERE id = ?", source["id"])
    second = write_legacy(export, tmp_path / "second.jsonl")
    before = {table: _columns(database, table, "*") for table in ("memories", "sources")}
    capsys.readouterr()
    assert _import(database, second, "--mode", "skip") == 1
    err = capsys.readouterr().err
    assert '"code":"restore_failed"' in err
    assert before == {table: _columns(database, table, "*") for table in ("memories", "sources")}


def test_mode_fail_still_aborts_on_any_collision_with_a_legacy_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--mode fail is not softened. Run the legacy comparison in fail mode (move the `mode == "fail"`
    check after it) and an equal row no longer aborts."""
    backup = _legacy_file(tmp_path)
    database, _context = _vault(tmp_path, "target")
    assert _import(database, backup, "--mode", "skip") == 0
    capsys.readouterr()
    assert _import(database, backup, "--mode", "fail") == 1
    assert '"code":"restore_failed"' in capsys.readouterr().err


def test_a_clean_reimport_never_builds_the_scratch_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe is for the rare row that does not match as it is. Build the scratch database eagerly
    (in _BackfillProbe.__init__) or settle before comparing as given, and every import pays for a
    bootstrap it does not need."""
    export = origin_export(tmp_path)
    backup = export.write(tmp_path / "clean.jsonl")
    database, _context = _vault(tmp_path, "target")
    assert _import(database, backup, "--mode", "skip") == 0

    def refuse(self: object) -> sqlite3.Connection:
        raise AssertionError("the scratch database was built for a row that matches")

    monkeypatch.setattr(onramp_module._BackfillProbe, "_connection", refuse)
    capsys.readouterr()
    assert _import(database, backup, "--mode", "skip") == 0
    assert _receipt(capsys.readouterr().out) == (0, len(export.body))


def test_a_mismatch_on_a_table_the_bootstrap_does_not_touch_is_refused_without_the_scratch_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bootstrap fills columns on memories and sources only (ROW_BACKFILL_TABLES). Settle every table
    (drop the table check in _BackfillProbe.settled_values) and an open loop that differs builds the
    scratch database for a row it cannot change, and this fails on the AssertionError it raises."""
    assert ROW_BACKFILL_TABLES == {"memories", "sources"}
    export = origin_export(tmp_path)
    first = write_legacy(export, tmp_path / "first.jsonl")
    database, _context = _vault(tmp_path, "target")
    assert _import(database, first, "--mode", "skip") == 0
    export.records("open_loop")[0]["title"] = "changed"
    second = write_legacy(export, tmp_path / "second.jsonl")

    def refuse(self: object) -> sqlite3.Connection:
        raise AssertionError("the scratch database was built for a table the bootstrap does not touch")

    monkeypatch.setattr(onramp_module._BackfillProbe, "_connection", refuse)
    capsys.readouterr()
    assert _import(database, second, "--mode", "skip") == 1
    assert '"code":"restore_failed"' in capsys.readouterr().err


def test_the_scratch_database_is_empty_after_every_row(tmp_path: Path) -> None:
    """Each probe runs in a transaction it rolls back. Leave the transaction open or the row behind
    (drop the ROLLBACK in settled_values) and the second call, for the same id, cannot insert and
    answers None."""
    export = origin_export(tmp_path)
    memory = export.records("memory")[0]
    memory["created_by_agent_id"] = None
    memory["metadata_json"] = _agent_metadata()
    columns = onramp_module._RECORD_SPECS["memory"][1]
    values = tuple(
        onramp_module._encode_column_value(column, memory.get(column)) for column in columns
    )
    probe = onramp_module._BackfillProbe()
    try:
        first = probe.settled_values("memories", columns, values)
        second = probe.settled_values("memories", columns, values)
    finally:
        probe.close()
    assert first is not None and first == second
    assert first[columns.index("created_by_agent_id")] == AGENT


def test_the_probe_answers_none_for_a_row_it_cannot_store(tmp_path: Path) -> None:
    """A row the schema would refuse cannot be settled, so it is compared as given and refused. Return
    the unsettled values when the insert fails (catch the error and return `values`) and a row that is
    invalid on its own would be accepted when it equals the stored one."""
    export = origin_export(tmp_path)
    memory = export.records("memory")[0]
    columns = onramp_module._RECORD_SPECS["memory"][1]
    values = list(onramp_module._encode_column_value(column, memory.get(column)) for column in columns)
    values[columns.index("status")] = "not-a-status"
    probe = onramp_module._BackfillProbe()
    try:
        assert probe.settled_values("memories", columns, tuple(values)) is None
        assert probe.settled_values("graph_edges", columns, tuple(values)) is None
    finally:
        probe.close()


def _legacy_shaped_vault(tmp_path: Path, name: str) -> Path:
    """A vault whose memories and sources hold what a legacy file leaves: every derived column empty."""
    export = origin_export(tmp_path / f"{name}-source")
    memory = export.records("memory")[0]
    memory["created_by_agent_id"] = None
    memory["run_id"] = None
    memory["project_id"] = None
    memory["metadata_json"] = {**_agent_metadata(), "agentic_memory": {"project_scope": [SCOPE_PROJECT]}}
    export.records("source")[0]["dedupe_key"] = None
    backup = write_legacy(export, tmp_path / f"{name}.jsonl")
    database, _context = _vault(tmp_path, name)
    assert _import(database, backup, "--mode", "skip") == 0
    return database


def test_apply_row_backfills_leaves_what_the_bootstrap_leaves(tmp_path: Path) -> None:
    """The scratch database must answer what the next open will store. Run the bootstrap on one copy of
    a vault holding legacy-shaped rows and apply_row_backfills on another: the rows match. Drop any one
    of the three steps from apply_row_backfills and a column differs. The first assertion pins that the
    bootstrap changes these rows at all, so the comparison is not between two untouched copies."""
    database = _legacy_shaped_vault(tmp_path, "legacy")
    raw = {"memories": _columns(database, "memories", "*"), "sources": _columns(database, "sources", "*")}
    by_bootstrap = tmp_path / "by-bootstrap.db"
    by_helper = tmp_path / "by-helper.db"
    for copy in (by_bootstrap, by_helper):
        shutil.copy(database, copy)
    connection = sqlite3.connect(by_bootstrap)
    try:
        bootstrap_sqlite_schema(connection)
        connection.commit()
    finally:
        connection.close()
    connection = sqlite3.connect(by_helper)
    try:
        apply_row_backfills(connection)
        connection.commit()
    finally:
        connection.close()
    settled = {table: _columns(by_bootstrap, table, "*") for table in ("memories", "sources")}
    assert settled != raw, "the bootstrap must change the legacy-shaped rows for this test to mean anything"
    assert {table: _columns(by_helper, table, "*") for table in ("memories", "sources")} == settled


# Every step the bootstrap calls on the connection, sorted into the ones that fill a column of a row
# from the row itself (apply_row_backfills runs them) and the ones that do not.
_FILLS_A_COLUMN_FROM_THE_ROW = {
    "_repair_source_dedupe_identity",
    "_backfill_legacy_memory_project_scopes",
    "_backfill_memory_agent_attribution",
}
_DOES_NOT_FILL_A_COLUMN_FROM_THE_ROW = {
    # A one-time cross-row repair from recorded inputs, not a row-local import backfill.
    "_relabel_derived_domains",
    "_relabel_derived_labels",
    # Rebuild or add schema, or index existing content.
    "_ensure_current_memories_status_constraint",
    "_ensure_additive_columns",
    "_drop_outdated_fts_tables",
    "_missing_fts_tables",
    # Move an identifier between rows that already hold it, with a pointer, and only when two rows
    # collide; the import compares the rows as they are.
    "_deduplicate_memory_lookup_values",
    "_repair_tombstone_lookup_value_holders",
}


def test_every_bootstrap_step_is_classified() -> None:
    """A step added to the bootstrap that fills a column from its row must reach apply_row_backfills, or
    skip compares a row the next open will change as different. This lists the steps by name: a new one
    fails here until it is put in one set, with the reason, and (for the first) in apply_row_backfills.
    Add a call to the bootstrap and leave this file alone and it fails."""
    called = set(re.findall(r"\b(_[a-z_]+)\(conn\)", inspect.getsource(sqlite_schema.bootstrap_sqlite_schema)))
    assert called == _FILLS_A_COLUMN_FROM_THE_ROW | _DOES_NOT_FILL_A_COLUMN_FROM_THE_ROW
    helper = inspect.getsource(sqlite_schema.apply_row_backfills)
    for step in _FILLS_A_COLUMN_FROM_THE_ROW - {"_repair_source_dedupe_identity"}:
        assert f"{step}(conn)" in helper
    # The source dedupe key is filled by the state-versioned repair at open and by its backfill here.
    assert "_backfill_source_dedupe_keys(conn)" in helper
