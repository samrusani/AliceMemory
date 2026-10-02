"""Gaps in the local folder scan tests, and two fixes.

1. The size, the times and the text of a scanned note come from the one descriptor the
   contained reader opened. Nothing at scan level pinned that, so a scan that took the
   size and the time from a second look at the path (``resolved_file.stat()``) passed.
2. The 100,000 entry listing cap counted every entry the walk produced, including the
   entries inside ``node_modules`` and ``.git``, so a watched folder with a large ignored
   tree was cut short before its notes were reached. The walk now does not enter a folder
   whose name is on the default ignore list, and the cap counts what is left.
3. ``alicebot vnext connectors local-folder sync`` and ``watch`` print ``refused_count``
   and ``truncated``. They were in the scan event and in connector health and not in the
   output of the command that did the scan.

Every test names the edit that makes it fail. Each edit was made by hand and the file was
restored from a saved copy.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import pytest

import alicebot_api.cli as cli_module
from alicebot_api import vnext_connectors as vc
from tests.unit.test_vnext_connectors import InMemoryConnectorSettingsStore


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(vc.LOCAL_FOLDER_ROOTS_ENV, str(tmp_path))
    watched = tmp_path / "watched"
    watched.mkdir()
    return watched


def _names(scan: vc.LocalFolderScan) -> list[str]:
    return [str(item["relative_path"]) for item in scan.items]


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# 1. size and time come from the descriptor that was read
# ---------------------------------------------------------------------------


def _replace_note(path: Path, text: bytes, *, mtime_ns: int) -> None:
    path.write_bytes(text)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_size_and_time_are_those_of_the_file_that_was_read_when_it_changed_before_the_read(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The note is replaced between the scan's checks and its read. The item describes what was read.

    The wrapper stands in for the moment after the scan resolved the path and before the
    reader opened it. The new file is longer and has a different time. The text, the size
    and the times in the item are all the new file's, because they all come from the one
    descriptor.

    Mutation: take ``file_size``, ``mtime`` and ``mtime_ns`` from ``resolved_file.stat()``
    called before the read (the code of v0.19.2) instead of from ``opened``.
    """

    note = root / "note.md"
    note.write_text("old and short", encoding="utf-8")
    old_ns = 1_700_000_000_000_000_000
    new_ns = 1_800_000_000_123_456_789
    os.utime(note, ns=(old_ns, old_ns))
    real_read = vc.read_text_beneath

    def replace_then_read(folder: Path, relative: Path, *, max_bytes: int) -> Any:
        _replace_note(folder / relative, b"new and clearly longer than the old text", mtime_ns=new_ns)
        return real_read(folder, relative, max_bytes=max_bytes)

    monkeypatch.setattr(vc, "read_text_beneath", replace_then_read)

    (item,) = vc.scan_local_folder([root]).items

    assert item["text"] == "new and clearly longer than the old text"
    assert item["file_size"] == len(b"new and clearly longer than the old text")
    assert item["mtime_ns"] == new_ns
    assert item["mtime"] == _iso(new_ns / 1e9)


def test_size_and_time_are_those_of_the_file_that_was_read_when_it_changed_after_the_read(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The note is replaced right after it was read. The item still describes what was read.

    Mutation: take ``file_size``, ``mtime`` and ``mtime_ns`` from ``resolved_file.stat()``
    called after the read. The item then carries the size and the time of a file whose
    text it does not hold.
    """

    note = root / "note.md"
    note.write_text("what was read", encoding="utf-8")
    old_ns = 1_700_000_000_000_000_000
    os.utime(note, ns=(old_ns, old_ns))
    real_read = vc.read_text_beneath

    def read_then_replace(folder: Path, relative: Path, *, max_bytes: int) -> Any:
        result = real_read(folder, relative, max_bytes=max_bytes)
        _replace_note(folder / relative, b"x" * 500, mtime_ns=1_900_000_000_000_000_000)
        return result

    monkeypatch.setattr(vc, "read_text_beneath", read_then_replace)

    (item,) = vc.scan_local_folder([root]).items

    assert item["text"] == "what was read"
    assert item["file_size"] == len(b"what was read")
    assert item["mtime_ns"] == old_ns
    assert item["mtime"] == _iso(old_ns / 1e9)


def test_the_size_is_the_byte_size_of_the_file_and_not_the_length_of_its_text(root: Path) -> None:
    """A note with accents and Windows line endings is longer on disk than as text.

    ``file_size`` is the descriptor's byte size. The text has its line endings translated
    as ``Path.read_text`` translated them, and its accented letters are one character each
    and two bytes each, so its length is shorter on both counts.

    Mutation: set ``file_size`` to ``len(text)``, before or after the line endings are
    translated.
    """

    raw = "h\u00e9llo w\u00f6rld\r\nline two\r\nline three\r\n".encode("utf-8")
    (root / "windows.md").write_bytes(raw)

    (item,) = vc.scan_local_folder([root]).items

    assert item["text"] == "h\u00e9llo w\u00f6rld\nline two\nline three\n"
    assert item["file_size"] == len(raw) == 37
    assert len(str(item["text"])) == 32


# ---------------------------------------------------------------------------
# 2. the listing cap counts what the scan would consider
# ---------------------------------------------------------------------------


def _fill(folder: Path, count: int, *, prefix: str = "f") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (folder / f"{prefix}{index}.md").write_text(f"note {index}", encoding="utf-8")


def test_entries_inside_ignored_folders_do_not_count_toward_the_listing_cap(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Notes next to thirty files in each of three ignored folders are all found under a cap of five.

    The ignored folders are ``node_modules``, ``.git`` and a ``Node_Modules`` nested in a
    subfolder (the comparison ignores case). The notes and the one subfolder are four
    entries, under the cap, so the scan is complete and not truncated.

    Mutation: list the folder with ``root.rglob("*")`` (the code of v0.19.2) in place of
    ``_walk_local_folder``, or drop the pruning of ignored folder names in it. The ignored
    entries then fill the cap, a note is missed, and the scan says ``truncated``.
    """

    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_LISTED", 5)
    (root / "a.md").write_text("alpha", encoding="utf-8")
    (root / "b.md").write_text("bravo", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "c.md").write_text("charlie", encoding="utf-8")
    _fill(root / "node_modules", 30)
    _fill(root / ".git", 30)
    _fill(root / "sub" / "Node_Modules", 30)

    scan = vc.scan_local_folder([root])

    assert sorted(_names(scan)) == ["a.md", "b.md", "sub/c.md"]
    assert scan.truncated is False
    assert scan.refused_count == 0


def test_an_ignored_folder_is_not_entered(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The walk never lists the inside of an ignored folder, so its size costs nothing.

    ``os.walk`` is wrapped to record every folder it enters. None of them is an ignored one.

    Mutation: delete the ``directory_names[:] = ...`` pruning in ``_walk_local_folder``. The
    walk then enters ``node_modules``, ``.git`` and the rest.
    """

    (root / "a.md").write_text("alpha", encoding="utf-8")
    for name in sorted(vc.DEFAULT_LOCAL_FOLDER_IGNORES):
        _fill(root / name, 3)
    _fill(root / "docs", 3)
    entered: list[str] = []
    real_walk = os.walk

    def recording_walk(top: Any, *args: Any, **kwargs: Any) -> Iterator[Any]:
        for directory, directory_names, file_names in real_walk(top, *args, **kwargs):
            entered.append(os.path.relpath(directory, top))
            yield directory, directory_names, file_names

    monkeypatch.setattr(os, "walk", recording_walk)

    scan = vc.scan_local_folder([root])

    assert sorted(entered) == [".", "docs"]
    assert sorted(_names(scan)) == ["a.md", "docs/f0.md", "docs/f1.md", "docs/f2.md"]


def test_a_folder_that_is_not_on_the_ignore_list_still_counts_toward_the_cap(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pruning is for the names on the list. Thirty notes in ``docs`` fill a cap of five.

    Mutation: prune every folder. The scan is then empty and not truncated.
    """

    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_LISTED", 5)
    _fill(root / "docs", 30)

    scan = vc.scan_local_folder([root])

    assert scan.truncated is True
    assert 0 < len(scan.items) <= 5


def test_a_folder_that_only_starts_like_an_ignored_name_is_entered(root: Path) -> None:
    """``.github``, ``node_modules_backup`` and ``generated notes`` are not on the list. Their notes are scanned.

    The list is matched on the whole name, as it always was for a file. ``.github`` is the one
    that matters: a prefix match on ``.git`` would drop every note in it.

    Mutation: compare a folder name by its prefix, or by a part of it, in ``_walk_local_folder``.
    """

    for folder in (".github", "node_modules_backup", "generated notes", "my.git"):
        (root / folder).mkdir()
        (root / folder / "note.md").write_text(f"a note in {folder}", encoding="utf-8")

    scan = vc.scan_local_folder([root])

    assert sorted(_names(scan)) == [
        ".github/note.md",
        "generated notes/note.md",
        "my.git/note.md",
        "node_modules_backup/note.md",
    ]
    assert scan.ignored_count == 0


def test_a_file_named_like_an_ignored_folder_is_still_read(root: Path) -> None:
    """The names on the list are matched for folders. A note called ``generated.md`` is a note.

    Mutation: drop files whose stem is an ignored name.
    """

    (root / "generated.md").write_text("a note", encoding="utf-8")
    (root / "git.md").write_text("another", encoding="utf-8")

    assert sorted(_names(vc.scan_local_folder([root]))) == ["generated.md", "git.md"]


def test_ignored_count_counts_what_was_listed_and_ignored_not_what_was_never_entered(root: Path) -> None:
    """A note an ignore pattern drops is counted. A note inside an ignored folder is not, because it was never listed.

    Mutation: count the files of an ignored folder, or stop counting files an ignore pattern drops.
    """

    (root / "keep.md").write_text("keep", encoding="utf-8")
    (root / "drop.skip.md").write_text("drop", encoding="utf-8")
    _fill(root / "node_modules", 7)

    scan = vc.scan_local_folder([root], ignore_patterns=["*.skip.md"])

    assert _names(scan) == ["keep.md"]
    assert scan.ignored_count == 1


def test_a_scan_that_is_not_recursive_lists_the_top_level_only(root: Path) -> None:
    """``recursive=False`` reads the notes in the folder and none below it.

    Mutation: delete the ``if not recursive: return`` in ``_walk_local_folder``.
    """

    (root / "top.md").write_text("top", encoding="utf-8")
    _fill(root / "sub", 2)

    assert _names(vc.scan_local_folder([root], recursive=False)) == ["top.md"]
    assert sorted(_names(vc.scan_local_folder([root], recursive=True))) == ["sub/f0.md", "sub/f1.md", "top.md"]


# ---------------------------------------------------------------------------
# 3. the commands show what the scan skipped
# ---------------------------------------------------------------------------


@pytest.fixture()
def connector_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryConnectorSettingsStore:
    store = InMemoryConnectorSettingsStore()

    @contextmanager
    def fake_vnext_store_context(_ctx: object) -> Iterator[InMemoryConnectorSettingsStore]:
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    return store


def _scan_with_a_refused_file_and_a_limit(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_FILES", 2)
    (root / "a.md").write_text("alpha", encoding="utf-8")
    (root / "b.md").write_bytes(b"\xff\xfe\x80\x81")
    (root / "c.md").write_text("charlie", encoding="utf-8")
    (root / "d.md").write_text("delta", encoding="utf-8")


def test_sync_prints_what_the_scan_refused_and_whether_it_stopped(
    root: Path, monkeypatch: pytest.MonkeyPatch, connector_store: InMemoryConnectorSettingsStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """One file is refused and a cap of two stops the scan. Both are in the output.

    Mutation: delete the line that sets ``refused_count`` or the line that sets
    ``truncated`` in ``_run_vnext_local_folder_sync``.
    """

    _scan_with_a_refused_file_and_a_limit(root, monkeypatch)

    assert cli_module.main(["vnext", "connectors", "local-folder", "sync", "--path", str(root)]) == 0

    record = json.loads(capsys.readouterr().out)
    assert record["status"] == "ok"
    assert record["refused_count"] == 1
    assert record["truncated"] is True
    assert record["imported_count"] == 2


def test_sync_prints_zero_and_false_for_a_clean_scan(
    root: Path, connector_store: InMemoryConnectorSettingsStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """The keys are always there, so a script can read them without asking whether they exist.

    Mutation: print the keys only when they are not zero and not false.
    """

    (root / "a.md").write_text("alpha", encoding="utf-8")

    assert cli_module.main(["vnext", "connectors", "local-folder", "sync", "--path", str(root)]) == 0

    record = json.loads(capsys.readouterr().out)
    assert record["refused_count"] == 0
    assert record["truncated"] is False


def test_the_output_of_a_failed_sync_still_carries_the_scan_numbers(
    root: Path, monkeypatch: pytest.MonkeyPatch, connector_store: InMemoryConnectorSettingsStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """A batch that ends ``partial`` or ``failed`` prints its record on stdout and exits 1. The numbers are in it.

    Mutation: add the keys after ``_checked_batch_output`` is called, or to a copy of the
    record. The record that is printed for a failing batch then lacks them.
    """

    (root / "a.md").write_text("alpha", encoding="utf-8")
    (root / "b.md").write_bytes(b"\xff\xfe\x80\x81")
    real_to_record = vc.ConnectorSyncResult.to_record

    def failing_record(self: vc.ConnectorSyncResult) -> Any:
        return {**real_to_record(self), "status": "partial"}

    monkeypatch.setattr(vc.ConnectorSyncResult, "to_record", failing_record)

    assert cli_module.main(["vnext", "connectors", "local-folder", "sync", "--path", str(root)]) == 1

    record = json.loads(capsys.readouterr().out)
    assert record["status"] == "partial"
    assert record["refused_count"] == 1
    assert record["truncated"] is False


@pytest.mark.parametrize("once", [True, False], ids=["watch --once", "watch polling"])
def test_watch_prints_what_each_scan_refused_and_whether_it_stopped(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    connector_store: InMemoryConnectorSettingsStore,
    capsys: pytest.CaptureFixture[str],
    once: bool,
) -> None:
    """``watch --once`` prints the sync record. A polling watch prints one in each run.

    Mutation: build the ``watch`` output from anything but the sync handler's output, or
    delete the keys from the sync handler. The runs then lack them.
    """

    _scan_with_a_refused_file_and_a_limit(root, monkeypatch)
    arguments = ["vnext", "connectors", "local-folder", "watch", "--path", str(root), "--interval-seconds", "0"]
    arguments.append("--once" if once else "--max-runs=2")

    assert cli_module.main(arguments) == 0

    output = json.loads(capsys.readouterr().out)
    runs = [output] if once else output["runs"]
    assert len(runs) == (1 if once else 2)
    for run in runs:
        assert run["refused_count"] == 1
        assert run["truncated"] is True
