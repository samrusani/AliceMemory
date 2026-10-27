"""The folder import never holds the whole folder in memory at once.

Guard: the lazy snapshot. ``_snapshot_markdown_source(..., lazy=True)`` (the
SQLite on-ramp path) validates every file first in batches of 256, including
the final partial batch, and then reads each file again only while the import
iterates it. Removing the lazy read (returning the eager
``snapshot_source_files`` list) holds every file's text at once, and a batch
loop that dropped its final partial group would let a file that fails
validation late in the folder slip past validation and be read mid-import.
The live-count test tracks every text the import reads and fails when more
than a handful are alive at once; on main's eager snapshot they all are.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alicebot_api.markdown_import import MarkdownImportValidationError, _snapshot_markdown_source
from alicebot_api.vnext_capture import VNextCaptureValidationError

USER_ID = "00000000-0000-0000-0000-000000000001"

BATCH = 256


def _note(index: int) -> str:
    body = [f"# Plan {index}", ""]
    for section in range(6):
        body.append(
            f"## Section {section}\nThe membound{index:03d}s{section} paragraph records a durable claim "
            "about the workspace archive with a unique token and ordinary words."
        )
    return "\n".join(body) + "\n"


def _folder(root: Path, files: int, *, fill_bytes: int) -> Path:
    folder = root / "notes"
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        text = _note(index) + ("padding token\n" * ((fill_bytes - len(_note(index))) // 14))
        (folder / f"note_{index:03d}.md").write_text(text, encoding="utf-8")
    return folder


def test_lazy_snapshot_is_not_an_eager_list(tmp_path: Path) -> None:
    """Guard: the lazy read (reverting to the eager list holds the whole folder)."""
    folder = _folder(tmp_path, files=4, fill_bytes=1_000)
    _path, snapshot = _snapshot_markdown_source(folder, max_file_bytes=10_000_000, lazy=True)
    assert not isinstance(snapshot, list)
    files = list(snapshot)
    assert len(files) == 4
    assert all("membound" in file.text for file in files)


def test_file_that_fails_validation_in_the_last_partial_batch_refuses_the_import(
    tmp_path: Path,
) -> None:
    """Guard: the final partial validation batch (dropping it lets the file be read mid-import)."""
    folder = _folder(tmp_path, files=BATCH + 40, fill_bytes=1_000)
    # The bad file sorts last, so it sits in the final partial batch of 41 files.
    (folder / "zzz_last_bad.md").write_bytes(b"# Broken\n\n\xff\xfe not utf-8\n")
    # The pre-flight pass refuses the whole import here, before anything reads
    # the folder for import.
    with pytest.raises(MarkdownImportValidationError, match="not valid UTF-8"):
        _snapshot_markdown_source(folder, max_file_bytes=10_000_000, lazy=True)


def test_import_holds_only_one_file_text_at_a_time(tmp_path, monkeypatch) -> None:
    """Guard: the lazy read, measured on the texts themselves.

    Every file here is larger than the door memo's per-key bound (4,096
    characters), so nothing of the import may keep a file's text after its
    turn: the validation pass reads and drops it, the import pass holds only
    the file it is working on. Under the eager snapshot every file's text is
    alive at once and this fails. (A peak-RSS comparison is not used here:
    glibc's allocator raises its mmap threshold after the first large
    allocations and does not return freed blocks, so resident size does not
    separate the two paths on Linux.)
    """

    import weakref

    from alicebot_api import markdown_import
    from alicebot_api.vnext_capture import VNextCaptureService
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.onramp import bootstrap_database, resolve_db_path

    files = 40
    folder = _folder(tmp_path, files=files, fill_bytes=8_000)
    total = sum(path.stat().st_size for path in folder.rglob("*.md"))
    assert total > files * 4_096

    real = markdown_import.read_contained_source_text
    refs: list[weakref.ref] = []
    peak = [0]

    class Tracked(str):
        pass

    def tracked(*args, **kwargs):
        text = Tracked(real(*args, **kwargs))
        refs.append(weakref.ref(text))
        alive = sum(1 for ref in refs if ref() is not None)
        peak[0] = max(peak[0], alive)
        return text

    monkeypatch.setattr(markdown_import, "read_contained_source_text", tracked)

    database = resolve_db_path(data_dir=str(tmp_path / "data"), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    with sqlite_user_connection(database, USER_ID) as conn:
        result = VNextCaptureService(SQLiteVNextStore(conn, USER_ID)).import_markdown_folder(folder)
    assert result.status == "ok"
    assert result.imported_count == files
    assert peak[0] <= 4, peak[0]
