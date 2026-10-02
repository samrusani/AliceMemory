"""The local-folder scan reads files through descriptors beneath the watched root (DB-010).

``scan_local_folder`` checks that a file is inside the watched folder and then
reads it. Before this change it read it by path, so a file or an ancestor
directory swapped for a symlink between the two steps was read from outside the
folder. The read now opens the root, each ancestor and the file one at a time,
each relative to the descriptor before it, with ``O_NOFOLLOW``. Every swap below
is made after the scan's checks and before its read, by a hook on the last
check, so no test depends on timing.

Mutation notes live on each test. A miss raises AssertionError.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import threading
from typing import Callable

import pytest

from alicebot_api import importer_paths, vnext_connectors as vc
from alicebot_api.importer_paths import ContainedReadRefused, read_text_beneath

OUTSIDE_FILE_TEXT = "OUTSIDE-SECRET"
OUTSIDE_SUB_TEXT = "OUTSIDE-SUB"


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A watched root and a sibling directory that holds text the root must never return."""

    monkeypatch.setenv(vc.LOCAL_FOLDER_ROOTS_ENV, str(tmp_path))
    root = tmp_path / "watched"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "private.md").write_text(OUTSIDE_FILE_TEXT, encoding="utf-8")
    (outside / "sub").mkdir()
    (outside / "sub" / "x.md").write_text(OUTSIDE_SUB_TEXT, encoding="utf-8")
    return root, outside


def _swap_after_checks(monkeypatch: pytest.MonkeyPatch, swap: Callable[[], None]) -> None:
    """Run ``swap`` once, after the scan's last check on the first file and before its read."""

    original = vc._is_ignored_local_file
    state = {"done": False}

    def hook(relative_path: Path, **kwargs: object) -> bool:
        result = original(relative_path, **kwargs)  # type: ignore[arg-type]
        if not state["done"]:
            state["done"] = True
            swap()
        return result

    monkeypatch.setattr(vc, "_is_ignored_local_file", hook)


def _texts(scan: vc.LocalFolderScan) -> list[str]:
    return [str(item["text"]) for item in scan.items]


def test_file_swapped_for_a_symlink_after_the_checks_is_refused_and_the_next_file_still_scans(
    tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link put where a checked file was is not followed, and it fails alone.

    Mutation: drop ``O_NOFOLLOW`` from the flags of the final file open in
    ``read_text_beneath``. This test fails, because the outside text is read.
    """

    root, outside = tree
    (root / "a.md").write_text("inside a", encoding="utf-8")
    (root / "b.md").write_text("inside b", encoding="utf-8")

    def swap() -> None:
        (root / "a.md").unlink()
        os.symlink(outside / "private.md", root / "a.md")

    _swap_after_checks(monkeypatch, swap)
    scan = vc.scan_local_folder([root])

    assert _texts(scan) == ["inside b"]
    assert scan.refused_count == 1


def test_ancestor_directory_swapped_for_a_symlink_after_the_checks_is_refused(
    tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link put where a checked directory was is not followed.

    Mutation: drop ``O_NOFOLLOW`` from the flags of the directory opens in
    ``read_text_beneath``. This test fails, because the outside text is read.
    """

    root, outside = tree
    (root / "sub").mkdir()
    (root / "sub" / "x.md").write_text("inside sub", encoding="utf-8")

    def swap() -> None:
        shutil.rmtree(root / "sub")
        os.symlink(outside / "sub", root / "sub")

    _swap_after_checks(monkeypatch, swap)
    scan = vc.scan_local_folder([root])

    assert scan.items == ()
    assert scan.refused_count == 1


def test_final_open_is_relative_to_the_directory_the_walk_already_opened(
    tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swap the ancestor after the walk has opened it and before the file is opened.

    The walk alone refuses a swap it sees, which is why the first two tests do
    not reach this case: a mutation that opens the file by its full path is
    caught only when the ancestor is swapped after the walk has passed it.
    Mutation: open the final file by ``root / relative`` instead of by its name
    with ``dir_fd``. This test fails, because the outside text is read.
    """

    root, outside = tree
    (root / "sub").mkdir()
    (root / "sub" / "x.md").write_text("inside sub", encoding="utf-8")
    real_open = os.open
    state = {"swapped": False}

    def open_then_swap(path: object, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type, call-overload]
        if path == "sub" and not state["swapped"]:
            state["swapped"] = True
            shutil.rmtree(root / "sub")
            os.symlink(outside / "sub", root / "sub")
        return descriptor

    monkeypatch.setattr(importer_paths.os, "open", open_then_swap)
    scan = vc.scan_local_folder([root])

    assert state["swapped"], "the swap hook never ran, so this test proves nothing"
    assert OUTSIDE_SUB_TEXT not in _texts(scan)


def test_fifo_swapped_in_after_the_checks_does_not_hang_the_scan(
    tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FIFO put where a checked file was is refused, and the scan returns.

    Mutations, each one alone: drop the ``S_ISREG`` check in
    ``read_text_beneath``, which returns the FIFO as an empty item and fails the
    refused count; drop ``O_NONBLOCK`` from the file flags, which parks the open
    on a FIFO with no writer and fails the five second join.
    """

    root, _outside = tree
    (root / "target.md").write_text("inside", encoding="utf-8")

    def swap() -> None:
        (root / "target.md").unlink()
        os.mkfifo(root / "target.md")

    _swap_after_checks(monkeypatch, swap)
    result: dict[str, vc.LocalFolderScan] = {}
    worker = threading.Thread(target=lambda: result.update(scan=vc.scan_local_folder([root])), daemon=True)
    worker.start()
    worker.join(timeout=5)
    hung = worker.is_alive()
    if hung:
        # Open the FIFO for writing as well so the parked reader's open returns and the thread can end.
        release = os.open(root / "target.md", os.O_RDWR)
        os.close(release)
        worker.join(timeout=5)
    assert not hung, "the scan blocked on a FIFO swapped in after the checks"
    assert result["scan"].items == ()
    assert result["scan"].refused_count == 1


def test_directory_swapped_in_for_a_file_after_the_checks_is_refused(
    tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory put where a checked file was is refused as not a regular file.

    Mutation: drop the ``S_ISREG`` check in ``read_text_beneath``. This test
    fails, because a read-only open of a directory succeeds, the read then
    fails with an I/O error, and the reader reports ``unreadable`` where this
    test expects ``not_regular``.
    """

    root, _outside = tree
    (root / "target.md").write_text("inside", encoding="utf-8")

    def swap() -> None:
        (root / "target.md").unlink()
        (root / "target.md").mkdir()

    _swap_after_checks(monkeypatch, swap)
    scan = vc.scan_local_folder([root])

    assert scan.items == ()
    assert scan.refused_count == 1
    with pytest.raises(ContainedReadRefused) as refused:
        read_text_beneath(root, Path("target.md"), max_bytes=1024)
    assert refused.value.reason == "not_regular"


def test_an_ordinary_tree_still_scans_with_the_same_item_shape(tree: tuple[Path, Path]) -> None:
    """Files, subfolders and an in-root link scan as before, with the item keys the importer reads.

    Mutation: make the contained read refuse a nested path, a link inside the
    root or a regular file. This test fails.
    """

    root, _outside = tree
    (root / "a").mkdir()
    (root / "a" / "deep.md").write_text("deep note", encoding="utf-8")
    (root / "top.txt").write_text("top note", encoding="utf-8")
    os.symlink(root / "top.txt", root / "alias.txt")

    scan = vc.scan_local_folder([root])

    by_relative = {str(item["relative_path"]): item for item in scan.items}
    assert {"a/deep.md", "top.txt"} <= set(by_relative)
    item = by_relative["a/deep.md"]
    assert item["text"] == "deep note"
    assert item["file_size"] == len("deep note")
    assert item["path"] == item["external_id"] == str((root / "a" / "deep.md").resolve())
    assert item["filename"] == "deep.md"
    assert item["extension"] == ".md"
    assert item["watched_root"] == str(root.resolve())
    assert isinstance(item["mtime_ns"], int) and str(item["mtime"]).endswith("Z")
    assert scan.refused_count == 0


def test_the_item_text_keeps_the_line_endings_the_old_read_gave(tree: tuple[Path, Path]) -> None:
    """CRLF and lone CR read as LF, and the size is the file's bytes, as in v0.19.2.

    The text decides the source's content hash. A scan that kept the raw line
    endings would import every note with a Windows line ending a second time.
    Mutation: return the decoded text without the line ending translation. This
    test fails.
    """

    root, _outside = tree
    note = root / "crlf.md"
    note.write_bytes(b"first\r\nsecond\rthird\n")

    scan = vc.scan_local_folder([root])

    assert _texts(scan) == [note.read_text(encoding="utf-8")] == ["first\nsecond\nthird\n"]
    assert scan.items[0]["file_size"] == len(b"first\r\nsecond\rthird\n")


def test_size_and_time_come_from_the_descriptor_that_was_read(tree: tuple[Path, Path]) -> None:
    """The returned status describes the file whose bytes were read, not the name.

    The name is pointed at a longer file right after the open. Mutation: return
    ``os.stat`` of ``root / relative`` in place of ``os.fstat`` of the
    descriptor in ``read_text_beneath``. This test fails, because the longer
    file's size and inode are reported with the older file's text.
    """

    root, _outside = tree
    note = root / "n.md"
    note.write_text("old text", encoding="utf-8")
    original_inode = note.stat().st_ino
    real_open = os.open
    state = {"replaced": False}

    def open_then_replace(path: object, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type, call-overload]
        if path == "n.md" and not state["replaced"]:
            state["replaced"] = True
            note.unlink()
            note.write_text("a much longer replacement body", encoding="utf-8")
        return descriptor

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(importer_paths.os, "open", open_then_replace)
        text, status = read_text_beneath(root, Path("n.md"), max_bytes=1024)

    assert state["replaced"], "the replacement hook never ran, so this test proves nothing"
    assert note.stat().st_ino != original_inode
    assert text == "old text"
    assert status.st_size == len("old text")
    assert status.st_ino == original_inode


@pytest.mark.parametrize(
    "relative",
    [
        Path("..") / "outside" / "private.md",
        Path("sub") / ".." / ".." / "outside" / "private.md",
        Path("/etc/hosts"),
        Path(""),
    ],
)
def test_the_reader_refuses_a_path_that_could_leave_the_root(tree: tuple[Path, Path], relative: Path) -> None:
    """A relative path with ``..``, an absolute path or no path at all is refused before anything is opened.

    The scan never builds such a path, because it takes the part below the
    root from a resolved name. The reader is shared, so it holds the rule
    itself. Mutation: drop the check of the path parts in ``read_text_beneath``.
    This test fails, because ``..`` walks out of the root.
    """

    root, _outside = tree
    (root / "sub").mkdir()

    with pytest.raises(ContainedReadRefused) as refused:
        read_text_beneath(root, relative, max_bytes=1024)

    assert refused.value.reason == "bad_path"


def test_a_hard_link_inside_the_root_is_still_read_and_the_docs_say_so(tree: tuple[Path, Path]) -> None:
    """A hard link is the file itself, so it is read. This is the documented residual.

    The importers carry the same limit. If this test starts to fail because the
    reader refuses linked files, the threat model, the known limitations and the
    changelog entry must change with it. Mutation: refuse a file whose link
    count is above one. This test fails.
    """

    root, outside = tree
    os.link(outside / "private.md", root / "hardlinked.md")

    scan = vc.scan_local_folder([root])

    assert _texts(scan) == [OUTSIDE_FILE_TEXT]
    assert scan.refused_count == 0
