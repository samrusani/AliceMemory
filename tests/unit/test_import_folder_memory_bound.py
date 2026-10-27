"""The folder import never holds the whole folder in memory at once.

Guard: the lazy snapshot. ``_snapshot_markdown_source(..., lazy=True)`` (the
SQLite on-ramp path) validates every file first in batches of 256, including
the final partial batch, and then reads each file again only while the import
iterates it. Removing the lazy read (returning the eager
``snapshot_source_files`` list) holds every file's text at once, and a batch
loop that dropped its final partial group would let a file that fails
validation late in the folder slip past validation and be read mid-import.
"""

from __future__ import annotations

import os
import resource
import subprocess
import sys
from pathlib import Path

import pytest

from alicebot_api.markdown_import import MarkdownImportValidationError, _snapshot_markdown_source
from alicebot_api.vnext_capture import VNextCaptureValidationError

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


def test_import_peak_memory_stays_under_the_folder_size(tmp_path: Path) -> None:
    """Guard: the lazy snapshot again, end to end through the on-ramp import.

    Two child processes import the same 60 MB folder, one with the lazy
    snapshot and one forced back to the eager list. The eager child holds
    every file's text at once; the lazy child must peak at least half a
    folder below it, so the comparison is machine independent.
    """

    folder = _folder(tmp_path, files=300, fill_bytes=200_000)
    total = sum(path.stat().st_size for path in folder.rglob("*.md"))
    assert total > 50_000_000
    peaks: dict[str, int] = {}
    for mode in ("lazy", "eager"):
        template = (
            "import resource, sys\n"
            "mode = sys.argv[1]\n"
            "sys.path.insert(0, 'apps/api/src')\n"
            "from alicebot_api.onramp import bootstrap_database, resolve_db_path\n"
            "from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection\n"
            "from alicebot_api.vnext_capture import VNextCaptureService\n"
            "USER_ID = '00000000-0000-0000-0000-000000000001'\n"
            f"database = resolve_db_path(data_dir={str(tmp_path / mode)!r}, db=None)\n"
            "bootstrap_database(database, user_id=USER_ID, user_email='local@alice')\n"
            "import alicebot_api.markdown_import as mi\n"
            "if mode == 'eager':\n"
            "    from alicebot_api.importer_paths import snapshot_source_files\n"
            "    def eager(source, *, max_file_bytes, lazy=False):\n"
            "        source_path, files = mi._read_markdown_source(source)\n"
            "        return source_path, snapshot_source_files(source_path, files,\n"
            "            max_bytes=max_file_bytes, error_factory=mi.MarkdownImportValidationError)\n"
            "    mi._snapshot_markdown_source = eager\n"
            f"with sqlite_user_connection(database, USER_ID) as conn:\n"
            "    store = SQLiteVNextStore(conn, USER_ID)\n"
            f"    result = VNextCaptureService(store).import_markdown_folder({str(folder)!r})\n"
            "assert result.status == 'ok', result.status\n"
            "print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", template, mode],
            capture_output=True,
            text=True,
            cwd=os.getcwd(),
        )
        assert out.returncode == 0, (out.stderr[-2000:], out.stdout[-2000:])
        peaks[mode] = int(out.stdout.strip().splitlines()[-1])
    # ru_maxrss is KiB on Linux and bytes on macOS; both sides of the
    # comparison use the same unit, so the difference is a folder share.
    assert peaks["lazy"] + (total // 2) < peaks["eager"], (peaks, total)
