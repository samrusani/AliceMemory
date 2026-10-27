"""A file that changes between the snapshot's two reads answers as a first-read failure.

The lazy snapshot validates every file first and reads each again while the
import iterates it. If a file changes in between, the second read fails inside
the import loop; these tests pin that the answer is then exactly what the same
bad file answers before the run (the public ``import_path`` /
``import_file_too_large`` record, exit code 1) and that nothing is stored.

Guard: ``_snapshot_read_errors_as_public`` (dropping the conversion lets the
raw read error surface as the generic ``source_import_failed`` failure record
instead of the public refusal).
"""

from __future__ import annotations

import io
import json
import pytest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
import sqlite3

from alicebot_api import markdown_import
from alicebot_api.onramp import main as onramp_main


def _notes(root: Path) -> Path:
    folder = root / "notes"
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        body = [f"# Note {index}", ""]
        for section in range(3):
            body.append(f"## Section {section}\nThe second{index}read{section} paragraph records one durable claim.")
        (folder / f"n{index}.md").write_text("\n".join(body) + "\n", encoding="utf-8")
    return folder


def _corrupt_between_passes(monkeypatch: pytest.MonkeyPatch, folder: Path, change) -> None:
    real = markdown_import._validated_snapshot_plan

    def plan(source_root, files, *, max_bytes, error_factory):
        result = real(source_root, files, max_bytes=max_bytes, error_factory=error_factory)
        change(folder)
        return result

    monkeypatch.setattr(markdown_import, "_validated_snapshot_plan", plan)


def _run(root: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> tuple[int, str]:
    database = root / "data" / "vault"
    monkeypatch.setattr(
        "sys.argv",
        ["alice-memory", "import-markdown", "--from", str(root / "notes"), "--db", str(database), *extra],
    )
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        code = onramp_main()
    return code or 0, err.getvalue()


def _stored_rows(root: Path) -> int:
    database = root / "data" / "vault"
    if not database.exists():
        return 0
    with closing(sqlite3.connect(database)) as conn:
        return int(conn.execute("SELECT count(*) FROM sources").fetchone()[0])


def _normalise(out: str, root: Path) -> str:
    return out.replace(str(root), "<tmp>")


def _first_read_answer(root: Path, monkeypatch: pytest.MonkeyPatch, change, *extra: str) -> tuple[int, str, int]:
    folder = _notes(root)
    change(folder)
    code, out = _run(root, monkeypatch, *extra)
    return code, out, _stored_rows(root)


def test_invalid_utf8_between_passes_answers_as_a_first_read_failure(tmp_path, monkeypatch) -> None:
    """Guard: _snapshot_read_errors_as_public (without it the raw error is a generic failure)."""

    def make_bad(folder: Path) -> None:
        (folder / "n2.md").write_bytes(b"# Broken\n\n\xff\xfe not utf-8\n")

    first_code, first_out, first_rows = _first_read_answer(tmp_path / "a", monkeypatch, make_bad)
    assert first_code == 1
    assert json.loads(first_out)["error"]["code"] == "import_path"
    assert "not valid UTF-8" in first_out
    assert first_rows == 0

    folder = _notes(tmp_path / "b")
    _corrupt_between_passes(monkeypatch, folder, make_bad)
    code, out = _run(tmp_path / "b", monkeypatch)
    assert code == 1
    assert json.loads(out)["error"]["code"] == "import_path"
    assert "not valid UTF-8" in out
    assert _normalise(out, tmp_path / "b") == _normalise(first_out, tmp_path / "a")
    assert _stored_rows(tmp_path / "b") == 0


def test_grown_file_between_passes_answers_as_a_first_read_failure(tmp_path, monkeypatch) -> None:
    """Guard: _snapshot_read_errors_as_public again, on the size limit."""

    def grow(folder: Path) -> None:
        (folder / "n2.md").write_text("# Grown\n\n" + "padding token\n" * 300_000, encoding="utf-8")

    first_code, first_out, first_rows = _first_read_answer(tmp_path / "a", monkeypatch, grow, "--max-file-mib", "1")
    assert first_code == 1
    assert json.loads(first_out)["error"]["code"] == "import_file_too_large"
    assert first_rows == 0

    folder = _notes(tmp_path / "b")
    _corrupt_between_passes(monkeypatch, folder, grow)
    code, out = _run(tmp_path / "b", monkeypatch, "--max-file-mib", "1")
    assert code == 1
    assert json.loads(out)["error"]["code"] == "import_file_too_large"
    assert _normalise(out, tmp_path / "b") == _normalise(first_out, tmp_path / "a")
    assert _stored_rows(tmp_path / "b") == 0


def test_link_between_passes_answers_as_a_first_read_failure(tmp_path, monkeypatch) -> None:
    """Guard: _snapshot_read_errors_as_public, on a file replaced by a link."""

    def replace_with_link(folder: Path) -> None:
        target = folder.parent / "outside.md"
        target.write_text("outside the root\n", encoding="utf-8")
        (folder / "n2.md").unlink()
        (folder / "n2.md").symlink_to(target)

    first_code, first_out, first_rows = _first_read_answer(tmp_path / "a", monkeypatch, replace_with_link)
    assert first_code == 1
    assert json.loads(first_out)["error"]["code"] == "import_path"
    assert first_rows == 0

    folder = _notes(tmp_path / "b")
    _corrupt_between_passes(monkeypatch, folder, replace_with_link)
    code, out = _run(tmp_path / "b", monkeypatch)
    assert code == 1
    assert json.loads(out)["error"]["code"] == "import_path"
    assert _normalise(out, tmp_path / "b") == _normalise(first_out, tmp_path / "a")
    assert _stored_rows(tmp_path / "b") == 0


def test_dry_run_between_passes_answers_the_same_and_stores_nothing(tmp_path, monkeypatch) -> None:
    """Guard: the conversion under --dry-run too."""

    def make_bad(folder: Path) -> None:
        (folder / "n2.md").write_bytes(b"# Broken\n\n\xff\xfe not utf-8\n")

    folder = _notes(tmp_path / "b")
    _corrupt_between_passes(monkeypatch, folder, make_bad)
    code, out = _run(tmp_path / "b", monkeypatch, "--dry-run")
    assert code == 1
    assert json.loads(out)["error"]["code"] == "import_path"
    assert "not valid UTF-8" in out
    assert _stored_rows(tmp_path / "b") == 0
