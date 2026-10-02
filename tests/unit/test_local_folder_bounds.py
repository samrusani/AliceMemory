"""The local-folder scan is bounded, and one bad file fails alone (DB-011).

v0.19.2 read every matching file whole, listed and sorted the whole walk, and let
one file that was not UTF-8 text, or that the process could not read, stop the
sync with an error. The scan now refuses a file over 2 MiB before it reads a
byte, reads no more than the cap plus one byte whatever the descriptor reports,
stops at 10,000 files or 64 MiB, lists at most 100,000 directory entries, skips
a bad file alone, and says what it skipped. The tests lower the caps with
``monkeypatch`` so each one runs on a handful of small files.

Mutation notes live on each test. A miss raises AssertionError.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

import pytest

from alicebot_api import importer_paths, vnext_connectors as vc
from alicebot_api.vnext_connectors import VNextConnectorService
from tests.unit.test_vnext_connectors import InMemoryConnectorSettingsStore


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(vc.LOCAL_FOLDER_ROOTS_ENV, str(tmp_path))
    watched = tmp_path / "watched"
    watched.mkdir()
    return watched


class _OsProxy:
    """Stands in for ``os`` inside ``importer_paths`` only, with some functions replaced."""

    def __init__(self, **overrides: Any) -> None:
        self.__dict__.update(overrides)

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


class _ZeroSize:
    """A status that reports a size of zero, as a file that grows or a pseudo file can."""

    def __init__(self, inner: os.stat_result) -> None:
        self._inner = inner
        self.st_size = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _report_zero_size(monkeypatch: pytest.MonkeyPatch, **more: Any) -> None:
    monkeypatch.setattr(
        importer_paths, "os", _OsProxy(fstat=lambda descriptor: _ZeroSize(os.fstat(descriptor)), **more)
    )


def _names(scan: vc.LocalFolderScan) -> list[str]:
    return [str(item["relative_path"]) for item in scan.items]


def test_the_caps_are_the_numbers_the_notes_state() -> None:
    """2 MiB a file, 10,000 files, 64 MiB in all, 100,000 directory entries listed.

    Mutation: change any of the four constants. This test fails.
    """

    assert vc.MAX_LOCAL_FOLDER_FILE_BYTES == 2 * 1024 * 1024
    assert vc.MAX_LOCAL_FOLDER_FILES == 10_000
    assert vc.MAX_LOCAL_FOLDER_TOTAL_BYTES == 64 * 1024 * 1024
    assert vc.MAX_LOCAL_FOLDER_LISTED == 100_000


def test_a_file_over_the_cap_is_refused_alone_and_the_scan_is_not_truncated(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The oversize file is counted as refused, the small file is returned, and the scan is complete.

    Mutation: let an oversize file through, or report it as ``truncated``. This
    test fails.
    """

    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_FILE_BYTES", 1024)
    (root / "small.md").write_text("ok", encoding="utf-8")
    (root / "big.md").write_text("x" * 2048, encoding="utf-8")

    scan = vc.scan_local_folder([root])

    assert _names(scan) == ["small.md"]
    assert scan.refused_count == 1
    assert scan.truncated is False


def test_a_file_over_the_cap_is_refused_before_any_byte_is_read(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size on the descriptor is enough to refuse, so nothing is read.

    Mutation: drop the ``st_size`` check in ``read_text_beneath``. This test
    fails, because the read then takes the cap plus one byte before it refuses.
    """

    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_FILE_BYTES", 1024)
    (root / "big.md").write_text("x" * 4096, encoding="utf-8")
    reads: list[int] = []

    def counting_read(descriptor: int, size: int) -> bytes:
        reads.append(size)
        return os.read(descriptor, size)

    monkeypatch.setattr(importer_paths, "os", _OsProxy(read=counting_read))
    scan = vc.scan_local_folder([root])

    assert scan.items == ()
    assert scan.refused_count == 1
    assert reads == [], "an oversize file must be refused from its size alone, without reading it"


def test_the_read_is_bounded_when_the_descriptor_reports_the_wrong_size(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that reports a size of zero is still refused for what it holds.

    Mutation: drop the length check after the read in ``read_text_beneath``.
    This test fails, because the cap plus one byte comes back as text.
    """

    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_FILE_BYTES", 1024)
    (root / "grower.md").write_text("x" * 4096, encoding="utf-8")
    _report_zero_size(monkeypatch)

    scan = vc.scan_local_folder([root])

    assert scan.items == ()
    assert scan.refused_count == 1


def test_bytes_consumed_never_exceed_the_cap_plus_one(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 1 MiB file with a zero size is read for no more than 1,025 bytes.

    The length check alone would let this pass after reading the whole file, so
    the bytes are counted. Mutation: read without the ``max_bytes + 1`` bound.
    This test fails.
    """

    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_FILE_BYTES", 1024)
    (root / "grower.md").write_bytes(b"x" * (1 << 20))
    consumed = {"bytes": 0}

    def counting_read(descriptor: int, size: int) -> bytes:
        block = os.read(descriptor, size)
        consumed["bytes"] += len(block)
        return block

    _report_zero_size(monkeypatch, read=counting_read)
    scan = vc.scan_local_folder([root])

    assert scan.items == ()
    assert 0 < consumed["bytes"] <= 1025


def test_the_file_count_cap_stops_the_scan_and_says_so(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ten files with a cap of three return three and report ``truncated``.

    Mutation: drop the file count check in ``scan_local_folder``. This test
    fails.
    """

    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_FILES", 3)
    for index in range(10):
        (root / f"n{index}.md").write_text(f"note {index}", encoding="utf-8")

    scan = vc.scan_local_folder([root])

    assert _names(scan) == ["n0.md", "n1.md", "n2.md"]
    assert scan.truncated is True
    assert scan.refused_count == 0


def test_a_folder_that_fits_every_cap_is_not_reported_truncated(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly as many files as the cap, and exactly as many bytes, is a complete scan.

    Mutation: set ``truncated`` whenever a cap is reached, or check the caps after
    the read instead of before it. This test fails.
    """

    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_FILES", 3)
    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_TOTAL_BYTES", 30)
    for index in range(3):
        (root / f"n{index}.md").write_text("0123456789", encoding="utf-8")

    scan = vc.scan_local_folder([root])

    assert len(scan.items) == 3
    assert scan.truncated is False
    assert scan.refused_count == 0


def test_the_total_byte_cap_stops_the_reading_and_says_so(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Five files of 60 bytes with a total of 100 return one, and the rest are not counted as refused.

    Mutation: drop the total byte cap, either the ``allowance`` or the check
    before each read. This test fails.
    """

    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_TOTAL_BYTES", 100)
    for index in range(5):
        (root / f"n{index}.md").write_text("y" * 60, encoding="utf-8")

    scan = vc.scan_local_folder([root])

    assert _names(scan) == ["n0.md"]
    assert sum(int(item["file_size"]) for item in scan.items) <= 100  # type: ignore[call-overload]
    assert scan.truncated is True
    assert scan.refused_count == 0


def test_the_scan_opens_no_further_file_once_the_total_is_reached(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the first file uses the whole total, the other four are not even opened.

    Mutation: drop the ``total_bytes`` check before each read in
    ``scan_local_folder``. This test fails, because the next file is opened
    before the reader refuses it.
    """

    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_TOTAL_BYTES", 60)
    for index in range(5):
        (root / f"n{index}.md").write_text("y" * 60, encoding="utf-8")
    file_opens: list[str] = []

    def recording_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if isinstance(path, str) and path.endswith(".md"):
            file_opens.append(path)
        return os.open(path, flags, *args, **kwargs)

    monkeypatch.setattr(importer_paths, "os", _OsProxy(open=recording_open))
    scan = vc.scan_local_folder([root])

    assert _names(scan) == ["n0.md"]
    assert scan.truncated is True
    assert file_opens == ["n0.md"]


def test_the_total_counts_the_bytes_read_not_the_size_the_descriptor_reports(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With every size reported as zero the total cap still stops the scan.

    Mutation: add ``opened.st_size`` to the total in place of the bytes read.
    This test fails, because the total stays at zero and all five files come back.
    """

    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_TOTAL_BYTES", 100)
    for index in range(5):
        (root / f"n{index}.md").write_text("y" * 60, encoding="utf-8")
    _report_zero_size(monkeypatch)

    scan = vc.scan_local_folder([root])

    assert _names(scan) == ["n0.md"]
    assert scan.truncated is True


@pytest.mark.parametrize("recursive", [True, False])
def test_the_listing_stops_consuming_the_walk_at_the_cap(
    root: Path, monkeypatch: pytest.MonkeyPatch, recursive: bool
) -> None:
    """Forty entries with a cap of five are walked for six, and the scan says it stopped.

    Mutation: drop the ``itertools.islice`` bound on the walk, so it is sorted
    whole. This test fails, because the wrapper counts all forty.
    """

    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_LISTED", 5)
    for index in range(40):
        (root / f"n{index}.md").write_text("n", encoding="utf-8")
    produced = {"count": 0}
    method = "rglob" if recursive else "glob"
    real_walk = getattr(Path, method)

    def counting_walk(self: Path, pattern: str, *args: Any, **kwargs: Any) -> Any:
        for entry in real_walk(self, pattern, *args, **kwargs):
            produced["count"] += 1
            yield entry

    monkeypatch.setattr(Path, method, counting_walk)
    scan = vc.scan_local_folder([root], recursive=recursive)

    assert produced["count"] == 6, "the walk must stop one entry past the listing cap, not be materialized first"
    assert len(scan.items) <= 5
    assert scan.truncated is True


def test_one_file_that_is_not_utf8_or_not_readable_does_not_stop_the_folder(root: Path) -> None:
    """A file of invalid UTF-8 and a file with no read permission are skipped beside a good file.

    Mutation: let ``UnicodeDecodeError`` out of ``read_text_beneath``, or let the
    ``OSError`` of the open out. This test fails, because the error ends the scan.
    """

    (root / "good.md").write_text("good", encoding="utf-8")
    (root / "bad_utf8.md").write_bytes(b"\xff\xfe\x80 not text")
    locked = root / "locked.md"
    locked.write_text("secret", encoding="utf-8")
    os.chmod(locked, 0)
    try:
        scan = vc.scan_local_folder([root])
    finally:
        os.chmod(locked, 0o600)

    assert _names(scan) == ["good.md"]
    # Root can read a file with mode 0, so only the invalid text is refused there.
    assert scan.refused_count == (1 if os.geteuid() == 0 else 2)


def test_a_read_error_on_one_file_does_not_stop_the_folder(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An I/O error while reading one file skips that file.

    Mutation: remove the ``OSError`` handling around ``fstat`` and ``read`` in
    ``read_text_beneath``. This test fails, because the error ends the scan.
    """

    (root / "a.md").write_text("alpha", encoding="utf-8")
    (root / "b.md").write_text("bravo", encoding="utf-8")
    first_read = {"done": False}

    def failing_read(descriptor: int, size: int) -> bytes:
        if not first_read["done"]:
            first_read["done"] = True
            raise OSError(errno.EIO, "Input/output error")
        return os.read(descriptor, size)

    monkeypatch.setattr(importer_paths, "os", _OsProxy(read=failing_read))
    scan = vc.scan_local_folder([root])

    assert _names(scan) == ["b.md"]
    assert scan.refused_count == 1


def test_a_sync_skips_a_bad_file_and_imports_the_rest(root: Path) -> None:
    """The sync that v0.19.2 ended with an error on one four byte file imports the others.

    Mutation: let a refused file raise out of ``scan_local_folder``. This test
    fails.
    """

    (root / "good.md").write_text("Fact: the good note is imported.", encoding="utf-8")
    (root / "bad.md").write_bytes(b"\xff\xfe\x80\x81")

    result = VNextConnectorService(InMemoryConnectorSettingsStore()).sync_local_folder((root,))

    assert result.status == "ok"
    assert result.imported_count == 1


def test_the_scan_event_records_what_the_scan_skipped(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``connector.local_folder_scan`` event carries ``refused_count`` and ``truncated``.

    Mutation: drop either key from the event payload. This test fails.
    """

    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_FILES", 2)
    (root / "a.md").write_text("alpha", encoding="utf-8")
    (root / "b.md").write_bytes(b"\xff\xfe\x80\x81")
    (root / "c.md").write_text("charlie", encoding="utf-8")
    (root / "d.md").write_text("delta", encoding="utf-8")
    store = InMemoryConnectorSettingsStore()

    VNextConnectorService(store).sync_local_folder((root,))

    events = [event for event in store.events if event["event_type"] == "connector.local_folder_scan"]
    assert len(events) == 1
    payload = events[0]["payload_json"]
    assert payload["refused_count"] == 1  # type: ignore[index]
    assert payload["truncated"] is True  # type: ignore[index]
    assert payload["file_count"] == 2  # type: ignore[index]


def test_connector_health_reports_the_last_scan_for_the_local_folder_only(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Health shows ``refused_count`` and ``truncated`` of the newest scan, and null for other connectors.

    A clean second scan replaces the first, so the report is the last scan and
    not a running total. Mutation: drop ``last_scan`` from the health item, read
    the oldest scan event, or fill it in for another connector. This test fails.
    """

    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_FILES", 2)
    (root / "a.md").write_text("alpha", encoding="utf-8")
    (root / "b.md").write_bytes(b"\xff\xfe\x80\x81")
    (root / "c.md").write_text("charlie", encoding="utf-8")
    (root / "d.md").write_text("delta", encoding="utf-8")
    store = InMemoryConnectorSettingsStore()
    service = VNextConnectorService(store)
    assert service.connector_health("local_folder")["last_scan"] is None

    service.sync_local_folder((root,))
    first = service.connector_health("local_folder")["last_scan"]
    assert isinstance(first, dict)
    assert first["refused_count"] == 1
    assert first["truncated"] is True
    assert "occurred_at" in first

    (root / "b.md").unlink()
    (root / "c.md").unlink()
    monkeypatch.setattr(vc, "MAX_LOCAL_FOLDER_FILES", 10)
    # Date the first scan's events long ago, so the second scan is unambiguously the newest.
    for event in store.events:
        event["occurred_at"] = "2026-01-01T00:00:00Z"
    service.sync_local_folder((root,))
    second = service.connector_health("local_folder")["last_scan"]

    assert isinstance(second, dict)
    assert second["refused_count"] == 0
    assert second["truncated"] is False
    assert service.connector_health("telegram")["last_scan"] is None
