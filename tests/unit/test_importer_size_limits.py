"""The importers refuse a file over a size limit before they read it.

The Markdown and ChatGPT importers read each selected file whole into memory.
A file named by mistake (a disk image, a database dump, a log) was read to the
end before anything said no. Every read now states a limit. A file over it is
refused with ``ImportFileTooLargeError`` before any byte is read, and the read
itself stops one byte past the limit, so a file that is growing, or that
reports a size of zero, cannot get past the check either.

The limit is per file. The defaults are 16 MiB for a Markdown or OpenClaw file
and 512 MiB for a ChatGPT export, which is one JSON file that cannot be read
a conversation at a time.
"""

from __future__ import annotations

import builtins
import inspect
import json
import os
from pathlib import Path
import random
import stat
from typing import Any

import pytest

import alicebot_api.cli as cli_module
import alicebot_api.importer_paths as importer_paths
import alicebot_api.vnext_capture as vnext_capture
from alicebot_api.chatgpt_import import (
    _snapshot_chatgpt_source,
    import_chatgpt_source,
    load_chatgpt_payload,
)
from alicebot_api.importer_paths import (
    DEFAULT_MAX_CHATGPT_EXPORT_BYTES,
    DEFAULT_MAX_TEXT_FILE_BYTES,
    MIB,
    ImportFileTooLargeError,
    contained_source_files,
    read_contained_source_text,
    snapshot_source_files,
)
from alicebot_api.markdown_import import (
    MarkdownImportValidationError,
    _snapshot_markdown_source,
    import_markdown_source,
    load_markdown_payload,
)
from alicebot_api.openclaw_adapter import (
    list_openclaw_source_files,
    load_openclaw_payload,
    snapshot_openclaw_source,
)
from alicebot_api.openclaw_import import import_openclaw_source
from alicebot_api.onramp import bootstrap_database, main as onramp_main, resolve_db_path
from alicebot_api.sqlite_store import sqlite_user_connection
from alicebot_api.vnext_capture import (
    ImportFileTooLargeRefused,
    VNextCaptureService,
    VNextCaptureValidationError,
)
from tests.unit.test_vnext_capture import InMemoryVNextCaptureStore


USER_ID = "00000000-0000-0000-0000-000000000001"


def _token() -> str:
    return "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"


def _sparse_file(path: Path, size: int) -> Path:
    """A file of ``size`` NUL bytes that takes no disk, so no test pays to read it."""

    with open(path, "wb") as handle:
        handle.truncate(size)
    return path


def _forbid_importing_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a file that wrongly passes the size check fail fast, not import for minutes.

    The default-limit tests use files of 16 MiB or more. If a default were
    raised, the file would be filtered and stored, which takes a long time. The
    filter is the first thing the import does with a file's text.
    """

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a file over the default limit reached the import")

    monkeypatch.setattr(vnext_capture, "_filter_markdown_units", forbidden)


def _open_descriptors() -> int | None:
    for directory in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(directory):
            return len(os.listdir(directory))
    return None


def _v0192_read(path: Path) -> str:
    """The read ``read_contained_source_text`` made in v0.19.2: a text-mode stream."""

    with open(path, "r", encoding="utf-8") as stream:
        return stream.read()


# ---------------------------------------------------------------------------
# read_contained_source_text
# ---------------------------------------------------------------------------


def test_a_file_over_the_limit_is_refused_before_any_byte_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size comes from the open descriptor, and nothing is read after it.

    ``os.fdopen`` is how the bytes are read, so it is made to fail the test if
    it is reached.

    Mutation that must fail it: delete the ``st_size`` check after ``fstat``.
    The read then starts, ``os.fdopen`` is called, and the test fails on that
    call and not on the refusal.
    """

    source = tmp_path / "big.md"
    source.write_bytes(b"x" * 100)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the file was read before its size was checked")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fdopen", forbidden)
        with pytest.raises(ImportFileTooLargeError) as caught:
            read_contained_source_text(source, max_bytes=99, error_factory=ValueError)

    assert caught.value.exact is True
    assert caught.value.size_bytes == 100
    assert caught.value.limit_bytes == 99
    assert caught.value.file_path == source
    assert caught.value.reason_code == "import_file_too_large"
    assert isinstance(caught.value, ValueError)


@pytest.mark.parametrize(
    ("reported_size", "chunk_bytes"),
    [("true", None), ("zero", 3), ("zero", 1000)],
)
def test_the_limit_is_inclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reported_size: str, chunk_bytes: int | None
) -> None:
    """A file of exactly ``max_bytes`` is read. One byte more is refused.

    With ``zero`` the file reports no size, so the buffer grows a chunk at a
    time and passes through a length of exactly ``max_bytes`` on the way.

    Mutation that must fail it: compare with ``>=`` in the ``st_size`` check, or
    with ``>=`` against ``max_bytes`` where the grown buffer is checked.
    """

    if reported_size == "zero":
        _lying_fstat_reports_zero(monkeypatch)
    if chunk_bytes is not None:
        monkeypatch.setattr(importer_paths, "_READ_CHUNK_BYTES", chunk_bytes)
    exact = tmp_path / "exact.md"
    exact.write_bytes(b"a" * 40)
    over = tmp_path / "over.md"
    over.write_bytes(b"a" * 41)

    assert read_contained_source_text(exact, max_bytes=40, error_factory=ValueError) == "a" * 40
    with pytest.raises(ImportFileTooLargeError):
        read_contained_source_text(over, max_bytes=40, error_factory=ValueError)


def _lying_fstat_reports_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``os.fstat`` report a size of zero for every file, as ``/proc`` does."""

    real_fstat = os.fstat

    def lying_fstat(descriptor: int) -> os.stat_result:
        fields = list(real_fstat(descriptor))
        fields[stat.ST_SIZE] = 0
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", lying_fstat)


def test_a_file_that_reports_no_size_but_holds_more_is_stopped_at_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size on the descriptor is not trusted. The read stops one byte past the limit.

    ``fstat`` is made to report a size of zero for a file that holds 200 KB,
    which is what a file being appended to, or a file under ``/proc``, looks
    like. The reader is wrapped to count the bytes it hands back.

    Mutation that must fail it: grow the buffer by a whole chunk whatever room
    is left under the limit, ``buffer.extend(bytes(_READ_CHUNK_BYTES))``, or
    delete the ``filled > max_bytes`` refusal. The file is then read to its end
    and nothing is refused.
    """

    source = tmp_path / "grows.md"
    source.write_bytes(b"y" * 200_000)
    real_fdopen = os.fdopen
    returned: list[int] = []

    class Counting:
        def __init__(self, stream: Any) -> None:
            self._stream = stream

        def readinto(self, target: Any) -> int | None:
            count: int | None = self._stream.readinto(target)
            returned.append(count or 0)
            return count

        def __enter__(self) -> "Counting":
            return self

        def __exit__(self, *exc_info: Any) -> None:
            self._stream.close()

    def counting_fdopen(descriptor: int, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        stream = real_fdopen(descriptor, mode, *args, **kwargs)
        return Counting(stream) if "b" in mode else stream

    with monkeypatch.context() as patch:
        _lying_fstat_reports_zero(patch)
        patch.setattr(os, "fdopen", counting_fdopen)
        with pytest.raises(ImportFileTooLargeError) as caught:
            read_contained_source_text(source, max_bytes=1000, error_factory=ValueError)

    assert caught.value.exact is False
    assert caught.value.size_bytes == 1001
    assert "is more than 1000 bytes" in str(caught.value)
    assert sum(returned) == 1001


def test_a_refused_file_does_not_leak_its_descriptor(tmp_path: Path) -> None:
    """Fifty refusals leave the process with as many open files as it started with.

    Mutation that must fail it: raise ``ImportFileTooLargeError`` outside the
    ``try`` that closes the descriptor on any exception.
    """

    before = _open_descriptors()
    if before is None:
        pytest.skip("no way to list open descriptors on this platform")
    source = tmp_path / "big.md"
    source.write_bytes(b"x" * 100)
    for _ in range(50):
        with pytest.raises(ImportFileTooLargeError):
            read_contained_source_text(source, max_bytes=10, error_factory=ValueError)

    assert _open_descriptors() == before


def test_a_limit_below_one_byte_is_a_programming_error(tmp_path: Path) -> None:
    """``max_bytes`` of zero or less would refuse every file, so it is not accepted.

    Mutation that must fail it: delete the ``max_bytes < 1`` check.
    """

    source = tmp_path / "a.md"
    source.write_text("a", encoding="utf-8")

    with pytest.raises(ValueError, match="at least 1") as caught:
        read_contained_source_text(source, max_bytes=0, error_factory=MarkdownImportValidationError)

    assert not isinstance(caught.value, ImportFileTooLargeError)


def test_the_limit_is_a_required_keyword_on_every_reader() -> None:
    """A caller cannot read a file without saying how large a file it will take.

    Mutation that must fail it: give ``max_bytes`` or ``max_file_bytes`` a
    default on ``read_contained_source_text``, ``snapshot_source_files``,
    ``_snapshot_markdown_source`` or ``_snapshot_chatgpt_source``.
    """

    for function in (
        read_contained_source_text,
        snapshot_source_files,
        _snapshot_markdown_source,
        _snapshot_chatgpt_source,
    ):
        parameters = inspect.signature(function).parameters
        limit = parameters.get("max_bytes") or parameters["max_file_bytes"]
        assert limit.kind is limit.KEYWORD_ONLY
        assert limit.default is limit.empty, function.__name__


@pytest.mark.parametrize(
    ("reported_size", "chunk_bytes"),
    [("true", None), ("zero", 1), ("zero", 3), ("zero", 7)],
)
def test_decoding_is_unchanged_from_the_text_mode_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reported_size: str, chunk_bytes: int | None
) -> None:
    """Six hundred random byte strings read as the v0.19.2 text-mode stream read them.

    The palette has every case that text mode treats specially: ``\\r\\n``, a
    lone ``\\r``, a byte order mark, a NUL, characters of two, three and four
    bytes, a stray continuation byte and a multi-byte character cut off at the
    end. A text and an error position must both match. With ``zero`` the file
    reports a size of zero, so the whole file is read through the path that
    grows the buffer one chunk at a time, and the chunk sizes of 1, 3 and 7
    split the input through the middle of those characters and of a ``\\r\\n``.

    Mutation that must fail it: delete the ``\\r`` translation after the decode,
    or decode with ``errors="replace"``, which turns an undecodable byte into
    text and not into an error, or cut the last byte, ``buffer[:filled - 1]``.
    """

    if reported_size == "zero":
        _lying_fstat_reports_zero(monkeypatch)
    if chunk_bytes is not None:
        monkeypatch.setattr(importer_paths, "_READ_CHUNK_BYTES", chunk_bytes)
    valid = [
        b"a", b" ", b"\n", b"\r", b"\r\n", b"\n\r", b"\x00", b"\xef\xbb\xbf",
        "é".encode(), "漢".encode(), "😀".encode(),
    ]
    invalid = [b"\xff", b"\x80", b"\xc3", b"\xe6\xbc", b"\xf0\x9f\x98"]
    rng = random.Random(7 if chunk_bytes is None else chunk_bytes)
    source = tmp_path / "case.md"
    decoded = 0
    undecodable = 0
    for case in range(600):
        # Every other case holds only text that decodes, so both outcomes are
        # compared often, and the invalid cases are not all the same shape.
        palette = valid if case % 2 else valid + invalid
        source.write_bytes(b"".join(rng.choice(palette) for _ in range(rng.randint(0, 40))))
        try:
            expected: tuple[Any, ...] = ("text", _v0192_read(source))
        except UnicodeDecodeError as exc:
            expected = ("bad", exc.start, exc.reason)
        try:
            actual: tuple[Any, ...] = (
                "text",
                read_contained_source_text(source, max_bytes=10_000, error_factory=ValueError),
            )
        except ValueError as exc:
            cause = exc.__cause__
            assert isinstance(cause, UnicodeDecodeError)
            actual = ("bad", cause.start, cause.reason)
        assert actual == expected, source.read_bytes()
        decoded += expected[0] == "text"
        undecodable += expected[0] == "bad"
    assert decoded > 100
    assert undecodable > 100


def test_a_read_holds_the_file_and_its_text_and_no_third_copy(tmp_path: Path) -> None:
    """Peak traced memory for an 8 MiB file stays under 2.3 times its size.

    The bytes and the decoded text are both alive at once, which is two copies,
    the same as the text-mode read before the limit. A third copy, from a join
    of chunks or a ``bytes(buffer)`` copy, is another 8 MiB and fails this. The
    file holds no carriage return, so the newline translation makes no copy.

    Mutation that must fail it: decode from ``bytes(buffer[:filled])`` in place
    of the memory view of the buffer.
    """

    import tracemalloc

    source = tmp_path / "big.md"
    size = 8 * MIB
    source.write_bytes(b"a" * size)
    tracemalloc.start()
    try:
        text = read_contained_source_text(source, max_bytes=16 * MIB, error_factory=ValueError)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(text) == size
    assert peak < 2.3 * size, f"peak {peak / size:.2f} times the file"


def test_the_open_once_and_no_follow_properties_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ``os.open`` with ``O_NOFOLLOW``, no ``builtins.open``, a symlink refused.

    The file is opened once and by one call, and that call carries ``O_NOFOLLOW``.
    A path that is a symlink when it is read is refused. The swap after a listing,
    which is the case that matters, has its own test below.

    Mutation that must fail it: read the file with ``open(file_path, "rb")``
    in place of the descriptor that was measured, or open without ``O_NOFOLLOW``.
    """

    source = tmp_path / "notes.md"
    source.write_text("- Note: one open\n", encoding="utf-8")
    flags: list[int] = []
    path_opens: list[str] = []
    real_open = os.open
    real_builtin_open = builtins.open

    def recording_open(path: Any, open_flags: int, *args: Any, **kwargs: Any) -> int:
        if str(path) == str(source):
            flags.append(open_flags)
        return real_open(path, open_flags, *args, **kwargs)

    def recording_builtin_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, (str, os.PathLike)) and str(file) == str(source):
            path_opens.append("builtins.open")
        return real_builtin_open(file, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", recording_open)
        patch.setattr(builtins, "open", recording_builtin_open)
        text = read_contained_source_text(source, max_bytes=1000, error_factory=ValueError)

    assert text == "- Note: one open\n"
    assert len(flags) == 1
    assert flags[0] & os.O_NOFOLLOW
    assert path_opens == []

    link = tmp_path / "link.md"
    link.symlink_to(source)
    with pytest.raises(ValueError, match="symlinked files") as caught:
        read_contained_source_text(link, max_bytes=1000, error_factory=ValueError)
    assert not isinstance(caught.value, ImportFileTooLargeError)


def test_a_file_swapped_after_the_listing_is_refused_by_what_it_is_when_it_is_opened(tmp_path: Path) -> None:
    """List a folder, change a listed file, then read the listing.

    A listed file is replaced by a link to a file outside the folder: it is refused
    and the outside text is not read. Another is replaced by a file over the limit:
    it is refused by the size of the file that is opened, and not by the size it
    had when it was listed. A file that is still what it was listed as is read.

    Mutation that must fail it: open the file without ``O_NOFOLLOW`` (the link is
    then read and the outside text comes back), or delete the ``st_size`` check
    after ``fstat`` (the read cap still refuses the large file, but at the limit and
    not at its size, so ``size_bytes`` is 1001 and ``exact`` is False).
    """

    root = tmp_path / "root"
    root.mkdir()
    (root / "a-linked.md").write_text("- Note: listed as a note\n", encoding="utf-8")
    (root / "b-grown.md").write_text("- Note: small when listed\n", encoding="utf-8")
    (root / "c-same.md").write_text("- Note: unchanged\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("- Note: text from outside the folder\n", encoding="utf-8")

    listed = contained_source_files(root, suffixes=(".md",), recursive=True, error_factory=ValueError)
    assert [path.name for path in listed] == ["a-linked.md", "b-grown.md", "c-same.md"]

    (root / "a-linked.md").unlink()
    (root / "a-linked.md").symlink_to(outside)
    with pytest.raises(ValueError, match="symlinked files") as linked:
        snapshot_source_files(root, listed, max_bytes=1000, error_factory=ValueError)
    assert "outside the folder" not in str(linked.value)

    (root / "a-linked.md").unlink()
    (root / "a-linked.md").write_text("- Note: listed as a note\n", encoding="utf-8")
    (root / "b-grown.md").write_bytes(b"x" * 2000)
    with pytest.raises(ImportFileTooLargeError) as grown:
        snapshot_source_files(root, listed, max_bytes=1000, error_factory=ValueError)
    assert grown.value.file_path.name == "b-grown.md"
    assert grown.value.size_bytes == 2000
    assert grown.value.exact is True

    (root / "b-grown.md").write_text("- Note: small when listed\n", encoding="utf-8")
    snapshot = snapshot_source_files(root, listed, max_bytes=1000, error_factory=ValueError)
    assert [source_file.relative_path for source_file in snapshot] == ["a-linked.md", "b-grown.md", "c-same.md"]


# ---------------------------------------------------------------------------
# a folder
# ---------------------------------------------------------------------------


def test_the_limit_applies_to_each_file_and_not_to_the_folder(tmp_path: Path) -> None:
    """Three files of ten bytes pass a limit of twelve. A fourth of thirteen does not.

    Mutation that must fail it: keep a running total across files and compare
    it with ``max_bytes``. The three small files are then refused together.
    """

    root = tmp_path / "root"
    root.mkdir()
    small = []
    for name in ("a.md", "b.md", "c.md"):
        path = root / name
        path.write_bytes(b"0123456789")
        small.append(path)

    snapshot = snapshot_source_files(root, small, max_bytes=12, error_factory=ValueError)
    assert [source_file.relative_path for source_file in snapshot] == ["a.md", "b.md", "c.md"]

    big = root / "d.md"
    big.write_bytes(b"0123456789abc")
    with pytest.raises(ImportFileTooLargeError) as caught:
        snapshot_source_files(root, [*small, big], max_bytes=12, error_factory=ValueError)

    assert caught.value.file_path.name == "d.md"
    assert caught.value.size_bytes == 13


# ---------------------------------------------------------------------------
# the vNext service
# ---------------------------------------------------------------------------


def _markdown_folder(tmp_path: Path, *, big_name: str = "big.md", big_bytes: int = 2048) -> Path:
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "a-small.md").write_text("- Note: a small note\n", encoding="utf-8")
    _sparse_file(folder / big_name, big_bytes)
    return folder


def test_markdown_import_refuses_an_oversized_file_and_writes_nothing(tmp_path: Path) -> None:
    """The refusal is typed, names the file and both sizes, and stores nothing.

    The small file is listed first, so it is read before the big one is
    refused. Nothing from it is stored: the refusal comes before any write.

    Mutation that must fail it: delete the ``except ImportFileTooLargeError``
    clause in ``import_markdown_folder``. The raw error then leaves the service
    and is not a ``VNextCaptureValidationError``.
    """

    folder = _markdown_folder(tmp_path)
    store = InMemoryVNextCaptureStore()

    with pytest.raises(ImportFileTooLargeRefused) as caught:
        VNextCaptureService(store).import_markdown_folder(folder, max_file_bytes=1024)

    assert isinstance(caught.value, VNextCaptureValidationError)
    assert caught.value.reason_code == "import_file_too_large"
    assert isinstance(caught.value.__cause__, ImportFileTooLargeError)
    message = str(caught.value)
    assert "big.md" in message
    assert "is 2048 bytes" in message
    assert "the limit is 1024 bytes" in message
    assert str(tmp_path) not in message
    assert store.sources == []
    assert store.chunks == []
    assert store.events == []


def test_the_refusal_withholds_a_file_name_the_credential_check_flags(tmp_path: Path) -> None:
    """A token in a file name is not printed in the refusal.

    Mutation that must fail it: build the message from ``exc.file_path.name``
    without the ``_unit_verdict`` check.
    """

    folder = _markdown_folder(tmp_path, big_name=f"{_token()}.md")
    store = InMemoryVNextCaptureStore()

    with pytest.raises(ImportFileTooLargeRefused) as caught:
        VNextCaptureService(store).import_markdown_folder(folder, max_file_bytes=1024)

    assert _token() not in str(caught.value)
    assert "name is withheld" in str(caught.value)
    assert store.sources == []


def test_chatgpt_import_refuses_an_oversized_file_before_it_parses_it(tmp_path: Path) -> None:
    """The file is not JSON, so a parse would say so. The size is checked first.

    Mutation that must fail it: pass a limit no file can exceed to
    ``_snapshot_chatgpt_source`` in ``import_chatgpt_export_file``. The error
    is then ``ChatGPT export is not valid JSON``.
    """

    export = tmp_path / "conversations.json"
    export.write_bytes(b"not json at all" * 100)
    store = InMemoryVNextCaptureStore()

    with pytest.raises(ImportFileTooLargeRefused, match="too large"):
        VNextCaptureService(store).import_chatgpt_export_file(export, max_file_bytes=1000)

    assert store.sources == []
    assert store.events == []


def test_the_defaults_are_sixteen_mib_for_text_and_five_hundred_twelve_for_a_chatgpt_export() -> None:
    """The documented defaults. A change to either one has to change the docs too.

    Each entry point carries the default for its own kind of file: the service
    methods, the legacy loaders and both commands.

    Mutation that must fail it: change either constant, or give
    ``import_markdown_folder`` the ChatGPT default.
    """

    assert MIB == 1024 * 1024
    assert DEFAULT_MAX_TEXT_FILE_BYTES == 16 * MIB
    assert DEFAULT_MAX_CHATGPT_EXPORT_BYTES == 512 * MIB

    def default_of(function: Any, name: str) -> Any:
        return inspect.signature(function).parameters[name].default

    assert default_of(VNextCaptureService.import_markdown_folder, "max_file_bytes") == DEFAULT_MAX_TEXT_FILE_BYTES
    assert default_of(VNextCaptureService.import_chatgpt_export_file, "max_file_bytes") == DEFAULT_MAX_CHATGPT_EXPORT_BYTES
    assert default_of(load_markdown_payload, "max_file_bytes") == DEFAULT_MAX_TEXT_FILE_BYTES
    assert default_of(import_markdown_source, "max_file_bytes") == DEFAULT_MAX_TEXT_FILE_BYTES
    assert default_of(load_chatgpt_payload, "max_file_bytes") == DEFAULT_MAX_CHATGPT_EXPORT_BYTES
    assert default_of(import_chatgpt_source, "max_file_bytes") == DEFAULT_MAX_CHATGPT_EXPORT_BYTES
    assert default_of(load_openclaw_payload, "max_file_bytes") == DEFAULT_MAX_TEXT_FILE_BYTES
    assert default_of(list_openclaw_source_files, "max_file_bytes") == DEFAULT_MAX_TEXT_FILE_BYTES
    assert default_of(snapshot_openclaw_source, "max_file_bytes") == DEFAULT_MAX_TEXT_FILE_BYTES
    assert default_of(import_openclaw_source, "max_file_bytes") == DEFAULT_MAX_TEXT_FILE_BYTES


def test_a_markdown_file_over_sixteen_mib_is_refused_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """16 MiB passes the size check. One byte more is refused with no flag given.

    Mutation that must fail it: pass ``DEFAULT_MAX_CHATGPT_EXPORT_BYTES`` as the
    default of ``import_markdown_folder``.
    """

    _forbid_importing_content(monkeypatch)
    at_limit = _sparse_file(tmp_path / "at-limit.md", 16 * MIB)
    over = _sparse_file(tmp_path / "over.md", 16 * MIB + 1)

    _path, snapshot = _snapshot_markdown_source(at_limit, max_file_bytes=DEFAULT_MAX_TEXT_FILE_BYTES)
    assert len(snapshot[0].text) == 16 * MIB

    with pytest.raises(ImportFileTooLargeRefused, match=r"the limit is 16777216 bytes \(16.00 MiB\)"):
        VNextCaptureService(InMemoryVNextCaptureStore()).import_markdown_folder(over)


def test_a_chatgpt_export_is_refused_by_default_only_above_five_hundred_twelve_mib(tmp_path: Path) -> None:
    """A 20 MiB export passes the ChatGPT default and would not pass the text default.

    A real export is one file and can be larger than any note, so the ChatGPT
    default is thirty-two times the text default. A file of 512 MiB plus one
    byte is refused without being read, which a sparse file makes cheap to test.

    Mutation that must fail it: pass ``DEFAULT_MAX_TEXT_FILE_BYTES`` as the
    default of ``import_chatgpt_export_file``.
    """

    twenty_mib = _sparse_file(tmp_path / "twenty.json", 20 * MIB)
    _path, snapshot = _snapshot_chatgpt_source(twenty_mib, max_file_bytes=DEFAULT_MAX_CHATGPT_EXPORT_BYTES)
    assert len(snapshot[0].text) == 20 * MIB
    with pytest.raises(ImportFileTooLargeError):
        read_contained_source_text(twenty_mib, max_bytes=DEFAULT_MAX_TEXT_FILE_BYTES, error_factory=ValueError)

    store = InMemoryVNextCaptureStore()
    # Past the size check the content is NUL bytes, which is not JSON.
    with pytest.raises(VNextCaptureValidationError, match="not valid JSON"):
        VNextCaptureService(store).import_chatgpt_export_file(twenty_mib)

    over = _sparse_file(tmp_path / "over.json", 512 * MIB + 1)
    with pytest.raises(ImportFileTooLargeRefused, match=r"the limit is 536870912 bytes \(512.00 MiB\)"):
        VNextCaptureService(store).import_chatgpt_export_file(over)
    assert store.sources == []


# ---------------------------------------------------------------------------
# alice-memory
# ---------------------------------------------------------------------------


def _long_export(tmp_path: Path, *, at_least_bytes: int) -> Path:
    """One valid conversation, as large as asked, in a few thousand messages."""

    mapping: dict[str, object] = {}
    count = at_least_bytes // 700 + 1
    for position in range(count):
        node_id = f"n{position}"
        mapping[node_id] = {
            "id": node_id,
            "parent": f"n{position - 1}" if position else None,
            "children": [f"n{position + 1}"] if position + 1 < count else [],
            "message": {
                "author": {"role": "user" if position % 2 == 0 else "assistant"},
                "create_time": 1_700_000_000 + position,
                "content": {"parts": [("steady words " * 50).strip() + f" {position}"]},
            },
        }
    export = tmp_path / "conversations.json"
    export.write_text(
        json.dumps([{"id": "c1", "title": "Long", "mapping": mapping, "current_node": f"n{count - 1}"}]),
        encoding="utf-8",
    )
    assert export.stat().st_size >= at_least_bytes
    return export


def _error_record(stderr: str) -> dict[str, Any]:
    records = [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]
    assert len(records) == 1
    assert "Traceback" not in stderr
    error: dict[str, Any] = records[0]["error"]
    return error


def _source_count(data_dir: Path) -> int:
    database = resolve_db_path(data_dir=str(data_dir), db=None)
    with sqlite_user_connection(database, USER_ID) as conn:
        row = conn.execute("SELECT count(*) AS total FROM sources").fetchone()
    return int(row["total"])


def test_alice_memory_import_chatgpt_refuses_with_the_typed_code_and_the_flag_that_raises_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 1.1 MiB export under ``--max-file-mib 1`` is refused. Under 2 it imports.

    Mutation that must fail it: stop passing ``max_file_bytes`` from
    ``_run_import_chatgpt``, so the flag is accepted and ignored. The first
    command then imports, and the test fails on the exit code.
    """

    export = _long_export(tmp_path, at_least_bytes=MIB + 100_000)
    data_dir = tmp_path / "data"

    assert onramp_main(["import-chatgpt", "--from", str(export), "--data-dir", str(data_dir), "--max-file-mib", "1"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    error = _error_record(captured.err)
    assert error["code"] == "import_file_too_large"
    assert "conversations.json is" in error["message"]
    assert "the limit is 1048576 bytes (1.00 MiB)" in error["message"]
    assert "--max-file-mib" in error["message"]
    assert str(tmp_path) not in error["message"]
    assert _source_count(data_dir) == 0

    assert onramp_main(["import-chatgpt", "--from", str(export), "--data-dir", str(data_dir), "--max-file-mib", "2"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "ok"
    assert receipt["imported_count"] == 1
    assert _source_count(data_dir) == 1


def test_alice_memory_import_markdown_refuses_with_the_typed_code_and_the_flag_that_raises_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 1.1 MiB note under ``--max-file-mib 1`` is refused. Under 2 it imports.

    Mutation that must fail it: stop passing ``max_file_bytes`` from
    ``_run_import_markdown``.
    """

    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "long.md").write_text("- Note: " + ("steady words " * 90_000), encoding="utf-8")
    data_dir = tmp_path / "data"

    assert onramp_main(["import-markdown", "--from", str(folder), "--data-dir", str(data_dir), "--max-file-mib", "1"]) == 1
    error = _error_record(capsys.readouterr().err)
    assert error["code"] == "import_file_too_large"
    assert "long.md is" in error["message"]
    assert _source_count(data_dir) == 0

    assert onramp_main(["import-markdown", "--from", str(folder), "--data-dir", str(data_dir), "--max-file-mib", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["imported_count"] == 1


def test_alice_memory_applies_the_default_limit_when_no_flag_is_given(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A note of 16 MiB plus one byte is refused with no flag, and the message says 16 MiB.

    Mutation that must fail it: change the default that ``_file_limit_bytes``
    falls back to for the Markdown command.
    """

    _forbid_importing_content(monkeypatch)
    note = _sparse_file(tmp_path / "huge.md", 16 * MIB + 1)

    assert onramp_main(["import-markdown", "--from", str(note), "--data-dir", str(tmp_path / "data")]) == 1

    error = _error_record(capsys.readouterr().err)
    assert error["code"] == "import_file_too_large"
    assert "(16.00 MiB)" in error["message"]


@pytest.mark.parametrize("command", ["import-markdown", "import-chatgpt"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "lots"])
def test_alice_memory_rejects_a_limit_that_is_not_a_whole_number_of_mib(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str, value: str
) -> None:
    """There is no value that means no limit, and no fractional MiB.

    Mutation that must fail it: delete the ``parsed < 1`` check in
    ``parse_max_file_mib``, or parse with ``int(float(value))``.
    """

    assert onramp_main([command, "--from", str(tmp_path), "--data-dir", str(tmp_path / "d"), "--max-file-mib", value]) == 2

    error = _error_record(capsys.readouterr().err)
    assert error["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# alicebot (Postgres)
# ---------------------------------------------------------------------------


def _patched_cli_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryVNextCaptureStore:
    from contextlib import contextmanager

    store = InMemoryVNextCaptureStore()

    @contextmanager
    def fake_vnext_store_context(_ctx: object) -> Any:
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    return store


@pytest.mark.parametrize("subcommand", ["import-markdown", "import-chatgpt"])
def test_alicebot_import_commands_refuse_with_the_typed_code_and_take_the_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    subcommand: str,
) -> None:
    """``alicebot vnext sources import-*`` answers with the typed code, not ``invalid_request``.

    A file of 3 MiB is over ``--max-file-mib 1``. The file is sparse, so the
    size check is all that runs.

    Mutation that must fail it: delete the ``ImportFileTooLargeRefused`` branch
    in the CLI runner. The error is then the generic ``invalid_request``.
    """

    store = _patched_cli_store(monkeypatch)
    if subcommand == "import-markdown":
        target = tmp_path / "notes"
        target.mkdir()
        _sparse_file(target / "wide.md", 3 * MIB)
        named = "wide.md"
    else:
        target = _sparse_file(tmp_path / "conversations.json", 3 * MIB)
        named = "conversations.json"

    assert cli_module.main(["vnext", "sources", subcommand, str(target), "--max-file-mib", "1"]) == 1

    error = _error_record(capsys.readouterr().err)
    assert error["code"] == "import_file_too_large"
    assert f"{named} is 3145728 bytes (3.00 MiB)" in error["message"]
    assert "the limit is 1048576 bytes (1.00 MiB)" in error["message"]
    assert "--max-file-mib" in error["message"]
    assert str(tmp_path) not in error["message"]
    assert store.sources == []


def test_alicebot_import_markdown_refuses_a_note_over_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No flag: a note of 16 MiB plus one byte gets the typed code on the Postgres CLI too.

    Mutation that must fail it: change the fallback that ``_file_limit_bytes``
    in ``cli/capture.py`` passes for the Markdown command to the ChatGPT
    default.
    """

    store = _patched_cli_store(monkeypatch)
    _forbid_importing_content(monkeypatch)
    folder = tmp_path / "notes"
    folder.mkdir()
    _sparse_file(folder / "huge.md", 16 * MIB + 1)

    assert cli_module.main(["vnext", "sources", "import-markdown", str(folder)]) == 1

    error = _error_record(capsys.readouterr().err)
    assert error["code"] == "import_file_too_large"
    assert "huge.md is" in error["message"]
    assert "(16.00 MiB)" in error["message"]
    assert store.sources == []


@pytest.mark.parametrize("value", ["0", "1.5"])
def test_alicebot_rejects_a_limit_that_is_not_a_whole_number_of_mib(
    capsys: pytest.CaptureFixture[str], value: str
) -> None:
    """The same floor as ``alice-memory``: the parser type is shared.

    Mutation that must fail it: give the Postgres parser its own type that
    accepts ``0``.
    """

    assert cli_module.main(["vnext", "sources", "import-chatgpt", "x.json", "--max-file-mib", value]) == 2
    assert _error_record(capsys.readouterr().err)["code"] == "invalid_request"
