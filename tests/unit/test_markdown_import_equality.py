"""Row-digest equality for the Markdown import: the speed-up stores exactly what main stores.

How the pinned digests were computed: on main (commit 115433fa0c8a8645cc00fd8dc58b9982afa798d8,
before any change of this branch), by running this module's own helpers in a scratch
process that imported ``test_markdown_import_equality`` and called
``build_equality_folder`` plus ``import_and_digest`` for every stage below, on a
machine-local temp directory. The digest function normalises away ids and
timestamps (see ``row_digest``) but covers every other column of every table the
vault holds, so any change in what the import stores fails the pinned value.

Each test names the guard whose removal must fail it; the PR body lists each
guard, its removal edit, and the observed failure.
"""

from __future__ import annotations

import hashlib
import re
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from alicebot_api.onramp import bootstrap_database, resolve_db_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_capture import VNextCaptureService, VNextCaptureValidationError

USER_ID = "00000000-0000-0000-0000-000000000001"

DIGEST_TABLES = (
    # embedding_stamp is excluded: the import never writes it and its token is
    # a per-vault random stamp, not an import result.
    "alice_schema_state", "users", "redaction_mode",
    "agent_identities", "agent_api_keys", "browser_clip_capabilities",
    "sources", "source_chunks", "memories", "memory_revisions", "provenance_links",
    "graph_edges", "open_loops", "event_log", "vnext_entities", "entity_relationship_events",
)
_UUID_36 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_UUID_EMBEDDED = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ISO_EMBEDDED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
_TIME_COLUMN = re.compile(r"(_at|_at_ms|_until)$")
_TIME_COLUMNS = {
    "valid_from", "valid_to", "occurred_at", "expires_at", "due_at", "opened_at",
    "closed_at", "last_used_at", "revoked_at", "first_seen_at", "last_seen_at",
    "changed_at", "source_created_at", "source_modified_at", "integrity_hash",
}


def _normalise(value: object, column: str, ids: dict[str, str], roots: dict[str, str]) -> object:
    if value is None:
        return None
    if _TIME_COLUMN.search(column) or column in _TIME_COLUMNS:
        return "set" if value else "unset"
    if isinstance(value, str):
        for root, placeholder in roots.items():
            value = value.replace(root, placeholder)
        if _UUID_36.match(value):
            if value not in ids:
                ids[value] = f"uuid{len(ids):07d}"
            return ids[value]
        text = _ISO_EMBEDDED.sub("T", value)
        if _UUID_EMBEDDED.search(text):
            text = _UUID_EMBEDDED.sub(lambda m: ids.setdefault(m.group(0), f"uuid{len(ids):07d}"), text)
        return text
    return value


def row_digest(database: Path, roots: dict[str, str] | None = None) -> str:
    """sha256 over every row of every table, ids and timestamps set aside.

    ``roots`` maps absolute folder paths baked into stored rows (the imported
    folder, the label folder) to stable placeholders, so the digest covers the
    stored relative paths and folder labels without the temp directory names.
    """
    roots = roots or {}
    ordered_roots = {root: roots[root] for root in sorted(roots, key=len, reverse=True)}
    ids: dict[str, str] = {}
    lines: list[str] = []
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        for table in DIGEST_TABLES:
            columns = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
            if not columns:
                continue
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                normed = tuple(_normalise(v, c, ids, ordered_roots) for c, v in zip(columns, row))
                lines.append(f"{table}\x1f{normed!r}")
    lines.sort()
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode("utf-8", "surrogatepass"))
        h.update(b"\x1e")
    return h.hexdigest()


def _section(index: int, token: str) -> str:
    return (
        f"## Section {index} of {token}\n"
        f"The {token} plan keeps point {index} stable and records the {token} detail {index} "
        f"for the workspace archive without any credential shape at all."
    )


def _note_text(token: str) -> str:
    body = [f"# Plan {token}", ""]
    body += [_section(i, f"{token}p{i}") for i in range(3)]
    body += ["", f"- first {token} item", f"- second {token} item", "", "```text", f"step 01 of {token} done", f"log line for {token}", "```", ""]
    return "\n".join(body)


def build_equality_folder(folder: Path) -> None:
    """Deterministic small folder: 300 unique notes, a duplicate pair, front matter,
    a key split over two lines (refused whole by the commit door), and a credential
    line caught by the unit filter. Every paragraph carries a unique token."""
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(300):
        sub = folder / f"sub{index % 5}"
        sub.mkdir(exist_ok=True)
        (sub / f"note_{index:03d}.md").write_text(_note_text(f"eqtok{index:03d}"), encoding="utf-8")
    (folder / "dup_a.md").write_text(_note_text("eqdup"), encoding="utf-8")
    (folder / "dup_b.md").write_text(_note_text("eqdup"), encoding="utf-8")
    (folder / "front.md").write_text(
        "---\ntitle: Front Note\ncreated: 2024-01-01T00:00:00Z\n---\n\n" + _note_text("eqfront"),
        encoding="utf-8",
    )
    # An assignment that only exists once the lines are joined: every line is
    # clean, the whole text is not, so the whole-source check must run.
    (folder / "split_key.md").write_text(
        "# Split\n\napi key\n = hunter2value and the note continues.\n",
        encoding="utf-8",
    )
    # A single flagged line: withheld in place, the file still imports.
    (folder / "line_key.md").write_text(
        "# Line\n\nFact: the access id is AKIAAAAAAAAAAAAA inline in the sentence.\n",
        encoding="utf-8",
    )
    for path in sorted(folder.rglob("*.md")):
        os.utime(path, (1704067200, 1704067200))


def build_label_folder(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(10):
        (folder / f"label_{index:02d}.md").write_text(_note_text(f"lbltok{index:02d}"), encoding="utf-8")
    for path in sorted(folder.rglob("*.md")):
        os.utime(path, (1704067200, 1704067200))


def _vault(tmp_path: Path) -> Path:
    database = resolve_db_path(data_dir=str(tmp_path / "data"), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return database


def _import(database: Path, folder: Path, **kwargs: object) -> object:
    with sqlite_user_connection(database, USER_ID) as conn:
        return VNextCaptureService(SQLiteVNextStore(conn, USER_ID)).import_markdown_folder(folder, **kwargs)


D0_BOOTSTRAP = "9b31f3175f62179798fb8b92da693f48831e56672c8002fd299b6d661f6e81e9"
D1_FIRST_IMPORT = "10892cfffd7038e1125da8c7039e36d2935f6c7dce19d208768d49892101a7eb"
D2_REIMPORT_DUPLICATES = "5214045556d86c47539e95648cc0df171354eddc63c116a03c7f6b7da9f5ec42"
D3_SUPERSEDE = "51260e106b3b14aaaaa8525edc4df946b111aeccea8e46f71429d99b8b385639"
D4_LABELS_IMPORTED = "07a6bd9c18fc84489e584a3ab6542827310bbff6f2b154e772d71004bcd47419"
D5_LOOSER_REFUSED = "9bb5e068713777605c65f7719b9d3636d65eb903fcbab88ebb9576ec94130672"
D6_LOOSER_ALLOWED = "591aabc3907314deddc757277e94cd3ca32f5b0ca1594998d6d158b385ef3ba8"
D7_DIFFERENT_LABELS_LIVE = "4a4247b1ea81005a969bb4763136e4ae0e6db0245a52def02214d9940274a42a"


def test_import_stores_the_pinned_rows(tmp_path: Path) -> None:
    """Guard: the whole import pipeline (any change to what is stored must fail this)."""
    folder = tmp_path / "eq"
    build_equality_folder(folder)
    database = _vault(tmp_path / "v1")
    roots = {str(folder): "<corpus>"}
    assert row_digest(database, roots) == D0_BOOTSTRAP
    result = _import(database, folder)
    assert result.status == "ok"  # the split key is skipped, the rest imports
    assert result.skipped_count == 1
    assert row_digest(database, roots) == D1_FIRST_IMPORT


def test_reimport_collapses_duplicates_and_pins_the_rows(tmp_path: Path) -> None:
    """Guard: duplicate collapse (a second import of the same folder must not add rows)."""
    folder = tmp_path / "eq"
    build_equality_folder(folder)
    database = _vault(tmp_path / "v2")
    roots = {str(folder): "<corpus>"}
    _import(database, folder)
    second = _import(database, folder)
    assert second.duplicate_count >= 300
    assert row_digest(database, roots) == D2_REIMPORT_DUPLICATES

def test_supersede_run_pins_the_rows(tmp_path: Path) -> None:
    """Guard: --supersede retires the old sources and keeps the same new rows."""
    folder = tmp_path / "eq"
    build_equality_folder(folder)
    database = _vault(tmp_path / "v3")
    roots = {str(folder): "<corpus>"}
    _import(database, folder)
    _import(database, folder, supersede=True)
    assert row_digest(database, roots) == D3_SUPERSEDE


def test_looser_reimport_refusal_and_allowance(tmp_path: Path) -> None:
    """Guard: the label rules on a re-import (same rows refused, then allowed)."""
    folder = tmp_path / "labels"
    build_label_folder(folder)
    database = _vault(tmp_path / "v4")
    roots = {str(folder): "<labels>"}
    _import(database, folder, domain="project", sensitivity="private")
    assert row_digest(database, roots) == D4_LABELS_IMPORTED
    refused = _import(
        database, folder, supersede=True, domain="project", sensitivity="public",
        allow_looser_classification=False,
    )
    assert refused.refused
    assert row_digest(database, roots) == D5_LOOSER_REFUSED
    allowed = _import(
        database, folder, supersede=True, domain="project", sensitivity="public",
        allow_looser_classification=True,
    )
    assert not allowed.refused
    assert row_digest(database, roots) == D6_LOOSER_ALLOWED


def test_same_text_under_different_labels_stays_two_sources(tmp_path: Path) -> None:
    """Guard: dedupe identity (same text under different labels must not collapse)."""
    eq = tmp_path / "eq"
    build_equality_folder(eq)
    labels = tmp_path / "labels"
    build_label_folder(labels)
    # The label notes repeat the equality notes' first file text: same text, other labels.
    (labels / "label_00.md").write_text(_note_text("eqtok000"), encoding="utf-8")
    os.utime(labels / "label_00.md", (1704067200, 1704067200))
    database = _vault(tmp_path / "v5")
    roots = {str(eq): "<corpus>", str(labels): "<labels>"}
    _import(database, eq)
    _import(database, labels, domain="project", sensitivity="private")
    with closing(sqlite3.connect(database)) as conn:
        rows = conn.execute(
            "SELECT count(*) FROM sources WHERE content_hash IN "
            "(SELECT content_hash FROM sources WHERE title = 'note_000') AND deleted_at IS NULL"
        ).fetchone()
    assert rows[0] == 2
    assert row_digest(database, roots) == D7_DIFFERENT_LABELS_LIVE


def test_invalid_file_refuses_the_whole_import_before_any_write(tmp_path: Path) -> None:
    """Guard: the pre-flight pass (a file that fails validation mid-folder stores nothing).

    On main the folder is read up front, so one unreadable file refuses the whole
    import before a single row is written. The branch keeps that: a pre-flight
    validation pass runs before the import pass. The last file sits in the final
    partial validation group, so a batch that skipped its last partial group
    would let the import start and this test would fail.
    """
    folder = tmp_path / "bad"
    build_equality_folder(folder)
    (folder / "zz_last_bad.md").write_bytes(b"# Broken\n\n\xff\xfe not utf-8\n")
    database = _vault(tmp_path / "v6")
    with pytest.raises(VNextCaptureValidationError):
        _import(database, folder)
    assert row_digest(database, {str(folder): "<corpus>"}) == D0_BOOTSTRAP
