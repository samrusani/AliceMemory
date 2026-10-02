"""Filesystem containment for importer source trees.

Importers walk a root the operator selected and read whatever they find under
it. Two ordinary habits break that boundary. ``Path.rglob`` descends directory
symlinks and ``Path.is_file`` is true for a link whose target is a regular
file, so one link planted inside the root can pull in bytes from anywhere the
server process can read. And re-opening a listed path for each pass lets the
name be swapped between passes, so the bytes that get archived as evidence
need not be the bytes that were parsed.

Everything here refuses to follow a link out of the selected root, and hands
back the exact text it read so callers parse and archive one snapshot.

A file is also read only up to a size limit the caller states. The size is
taken from the descriptor the file was opened as, before any byte is read, and
the read itself stops one byte past the limit, so a file that grows after the
check or reports a size of zero while holding more cannot get past it.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import stat


ErrorFactory = Callable[[str], Exception]

MIB = 1024 * 1024
_READ_CHUNK_BYTES = MIB

# One Markdown note or one OpenClaw JSON file. Measured on a synthetic vault,
# a single 16 MiB note peaked at about 335 MB of resident memory and took about
# 23 seconds to import, so the limit sits well above any note a person writes
# and well below a file that is not one.
DEFAULT_MAX_TEXT_FILE_BYTES = 16 * MIB

# A ChatGPT export is one JSON file, ``conversations.json``: an array holding
# every conversation, each with a ``mapping`` of its messages. It cannot be read
# a conversation at a time, because the importer has to parse the whole array
# to find where one conversation ends. Peak memory was measured at 6 to 9 times
# the file (377 MB for a 52 MB export, 535 MB when one character outside the
# Basic Multilingual Plane makes Python store the whole text at four bytes a
# character), so 512 MiB is about 3 to 4.6 GB. That is a file a long-time user
# can have and a laptop can import, and it refuses a disk image or database
# dump that was named by mistake before any of it is read.
DEFAULT_MAX_CHATGPT_EXPORT_BYTES = 512 * MIB


def parse_max_file_mib(value: str) -> int:
    """The ``--max-file-mib`` argument: a whole number of MiB, at least 1.

    Both CLIs take it, so the units and the floor are stated once. There is no
    value that means no limit. A number larger than the file is how a person who
    has the memory for it says so.
    """

    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid whole number of MiB: {value}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("--max-file-mib must be at least 1")
    return parsed


class ImportFileTooLargeError(ValueError):
    """A selected import file is over the per-file size limit.

    Raised directly and not through the importer's ``error_factory``, so a
    caller can tell this refusal from every other one by its type and its
    ``reason_code``. It is raised before the file is read when the size on the
    open descriptor is already over the limit.
    """

    reason_code = "import_file_too_large"

    def __init__(self, *, file_path: Path, size_bytes: int, limit_bytes: int, exact: bool) -> None:
        self.file_path = file_path
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        # False when the size is only known to be over the limit: the file
        # passed the size check and then held more than the limit when read.
        self.exact = exact
        size_phrase = f"is {size_bytes} bytes" if exact else f"is more than {limit_bytes} bytes"
        super().__init__(
            f"import source file is too large: {file_path} {size_phrase}, "
            f"the limit is {limit_bytes} bytes"
        )

# BSD kernels report O_NOFOLLOW on a symlink as EMLINK rather than ELOOP.
_SYMLINK_OPEN_ERRNOS = frozenset({errno.ELOOP, errno.EMLINK})


@dataclass(frozen=True, slots=True)
class ImportSourceFile:
    """One import file opened once, with the bytes that were read."""

    path: Path
    relative_path: str
    text: str


def _relative_source_file(source_root: Path, file_path: Path) -> str:
    if source_root.is_dir():
        return str(file_path.relative_to(source_root))
    return file_path.name


def contained_source_files(
    source_root: Path,
    *,
    suffixes: Iterable[str],
    recursive: bool,
    error_factory: ErrorFactory,
) -> list[Path]:
    """List import candidates under ``source_root`` without following links.

    Walks with ``followlinks=False`` so no directory symlink is descended, and
    refuses any symlinked directory or candidate file instead of silently
    importing bytes from outside the root or silently skipping content the
    operator expected to import.
    """

    matches: list[Path] = []
    normalized_suffixes = {suffix.casefold() for suffix in suffixes}
    for raw_directory, directory_names, file_names in os.walk(source_root, followlinks=False):
        current_directory = Path(raw_directory)
        if recursive:
            for directory_name in sorted(directory_names):
                candidate_directory = current_directory / directory_name
                if candidate_directory.is_symlink():
                    raise error_factory(
                        f"import source must not contain symlinked directories: {candidate_directory}"
                    )
        for file_name in sorted(file_names):
            candidate = current_directory / file_name
            if candidate.suffix.casefold() not in normalized_suffixes:
                continue
            if candidate.is_symlink():
                raise error_factory(f"import source must not contain symlinked files: {candidate}")
            matches.append(candidate)
        if not recursive:
            directory_names[:] = []
    return sorted(matches)


def read_contained_source_text(
    file_path: Path,
    *,
    source_root: Path | None = None,
    max_bytes: int,
    error_factory: ErrorFactory,
) -> str:
    """Open one import file once and return the exact text that was read.

    ``O_NOFOLLOW`` fails the open when the final path component is a symlink,
    so a link swapped in after the listing cannot redirect the read.
    ``O_NONBLOCK`` keeps the open itself from parking forever on a FIFO that
    has no writer: the descriptor is checked to be a regular file before any
    bytes are consumed, and that check is worthless if the process never gets
    to it. The flag is cleared once the descriptor is known to be a regular
    file, for which it has no defined effect anyway.

    ``source_root``, when given, is enforced: a candidate carrying ``..`` or
    otherwise resolving outside the selected root is refused rather than read.

    ``max_bytes`` is required, so no caller can read a file without saying how
    large a file it will take. A file over it is refused with
    ``ImportFileTooLargeError`` before any of it is read: the size comes from
    ``fstat`` on the descriptor that was just opened. The read then asks for
    one byte more than the limit and refuses if it gets it, because a size on
    the descriptor is not a promise about what the file holds (a file being
    appended to, or a file under ``/proc`` that reports a size of zero).

    Known limitation: a hard link is not a reference to a file, it is the
    file, so a hard link planted inside the root to content elsewhere is
    indistinguishable from ordinary content and is read. Nothing at this layer
    can separate the two.
    """

    if max_bytes < 1:
        raise ValueError("max_bytes must be at least 1")
    if ".." in file_path.parts:
        raise error_factory(f"import source path must not traverse upward: {file_path}")
    if source_root is not None and not file_path.is_relative_to(source_root):
        raise error_factory(f"import source path escapes the selected root: {file_path}")

    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(file_path, open_flags)
    except OSError as exc:
        if exc.errno in _SYMLINK_OPEN_ERRNOS:
            raise error_factory(
                f"import source must not contain symlinked files: {file_path}"
            ) from exc
        raise error_factory(f"import source file is not readable: {file_path}") from exc

    try:
        # Checked on the descriptor rather than the path, so a swap between the
        # listing and the open cannot change what is measured. Without this a
        # FIFO under the root imports as an empty document, and on a platform
        # where the read-open parks, it hangs the import instead.
        opened_status = os.fstat(descriptor)
        if not stat.S_ISREG(opened_status.st_mode):
            raise error_factory(
                f"import source file is not a regular file: {file_path}"
            )
        # Before a byte is read. A file over the limit never reaches memory.
        if opened_status.st_size > max_bytes:
            raise ImportFileTooLargeError(
                file_path=file_path,
                size_bytes=opened_status.st_size,
                limit_bytes=max_bytes,
                exact=True,
            )
        descriptor_flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        fcntl.fcntl(descriptor, fcntl.F_SETFL, descriptor_flags & ~os.O_NONBLOCK)
        # One buffer, sized from the descriptor, with one spare byte so a file
        # that is longer than it reported shows up as a full buffer. It is made
        # here so a failure to allocate it still closes the descriptor. Reading
        # in chunks and joining them would hold the chunks and their join at
        # once, which for a 500 MiB export is another 500 MiB in the peak.
        buffer = bytearray(min(opened_status.st_size, max_bytes) + 1)
    except BaseException:
        os.close(descriptor)
        raise

    filled = 0
    with os.fdopen(descriptor, "rb", buffering=0) as raw:
        while True:
            if filled == len(buffer):
                if filled > max_bytes:
                    # The spare byte was filled, or the file kept growing past
                    # the limit while it was being read.
                    raise ImportFileTooLargeError(
                        file_path=file_path,
                        size_bytes=filled,
                        limit_bytes=max_bytes,
                        exact=False,
                    )
                # ``fstat`` reported less than the file holds. Take the rest a
                # chunk at a time, and never more than one byte past the limit.
                buffer.extend(bytes(min(_READ_CHUNK_BYTES, max_bytes + 1 - filled)))
            count = raw.readinto(memoryview(buffer)[filled:])
            if not count:
                break
            filled += count
    # Decoded as the text-mode open it replaces decoded: strict UTF-8, no byte
    # order mark handling, and the universal newline translation, so ``\r\n``
    # and a lone ``\r`` arrive as ``\n``. The text is the same as before the
    # limit existed.
    try:
        text = str(memoryview(buffer)[:filled], "utf-8")
    except UnicodeDecodeError as exc:
        # Decoding is part of reading the file, so a bad byte has to leave
        # as the caller's own validation error naming the file. Raised bare
        # it surfaces as a byte offset with no path attached, which tells an
        # operator nothing about which file to go and look at.
        raise error_factory(
            f"import source file is not valid UTF-8 text: {file_path}"
        ) from exc
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


class ContainedReadRefused(Exception):
    """One file was refused by ``read_text_beneath``. ``reason`` is a stable code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
_READ_BLOCK_BYTES = 1 << 20


def read_text_beneath(root: Path, relative: Path, *, max_bytes: int) -> tuple[str, os.stat_result]:
    """Read one regular file at ``root/relative`` without ever leaving ``root``.

    Nothing is opened by its full path. ``root`` is opened as a directory, each
    ancestor is opened relative to the descriptor of the one before it, and the
    file is opened relative to the last of them, every one with ``O_NOFOLLOW``.
    A file or an ancestor swapped for a symlink after the caller listed it
    therefore fails the open instead of redirecting it, and swapping the
    ancestor after the walk reaches it cannot move the final open, because that
    open is anchored to a descriptor and not to a name.

    ``O_NONBLOCK`` keeps the open from parking on a FIFO that has no writer.
    The opened descriptor is then required to be a regular file of at most
    ``max_bytes``, so the check is made on the object that is read and not on
    a name. The read itself takes at most ``max_bytes + 1`` bytes, so a file
    that grows, or that reports a size it does not have, is never held whole.
    The size and times in the returned status come from the same descriptor
    as the text. Any failure raises ``ContainedReadRefused`` with a reason:
    ``bad_path``, ``symlink_or_moved``, ``unreadable``, ``not_regular``,
    ``too_large`` or ``not_utf8``.

    Known limitation: a hard link is the file itself, so a hard link planted
    inside the root to a file elsewhere reads as ordinary content, as it does
    in ``read_contained_source_text``. Nothing at this layer can tell them
    apart.
    """

    parts = relative.parts
    if not parts or any(not part or part in {".", ".."} or os.sep in part or "\x00" in part for part in parts):
        raise ContainedReadRefused("bad_path")
    directory: int | None = None
    descriptor: int | None = None
    try:
        try:
            directory = os.open(root, _DIRECTORY_OPEN_FLAGS)
            for part in parts[:-1]:
                child = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=directory)
                previous, directory = directory, child
                os.close(previous)
            descriptor = os.open(parts[-1], _FILE_OPEN_FLAGS, dir_fd=directory)
        except OSError as exc:
            # Linux reports a symlink opened with O_DIRECTORY | O_NOFOLLOW as ENOTDIR.
            if exc.errno in _SYMLINK_OPEN_ERRNOS or exc.errno == errno.ENOTDIR:
                raise ContainedReadRefused("symlink_or_moved") from exc
            raise ContainedReadRefused("unreadable") from exc
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode):
                raise ContainedReadRefused("not_regular")
            if status.st_size > max_bytes:
                raise ContainedReadRefused("too_large")
            fcntl.fcntl(descriptor, fcntl.F_SETFL, fcntl.fcntl(descriptor, fcntl.F_GETFL) & ~os.O_NONBLOCK)
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                block = os.read(descriptor, min(remaining, _READ_BLOCK_BYTES))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
        except OSError as exc:
            raise ContainedReadRefused("unreadable") from exc
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise ContainedReadRefused("too_large")
        try:
            return data.decode("utf-8"), status
        except UnicodeDecodeError as exc:
            raise ContainedReadRefused("not_utf8") from exc
    finally:
        for opened in (descriptor, directory):
            if opened is not None:
                try:
                    os.close(opened)
                except OSError:
                    pass


def snapshot_source_files(
    source_root: Path,
    files: Iterable[Path],
    *,
    max_bytes: int,
    error_factory: ErrorFactory,
) -> list[ImportSourceFile]:
    """Read every selected file once, in listing order.

    ``max_bytes`` is the limit for each file. It is not a limit on the folder:
    the snapshot holds every selected file in memory at once.
    """

    return [
        ImportSourceFile(
            path=file_path,
            relative_path=_relative_source_file(source_root, file_path),
            text=read_contained_source_text(
                file_path,
                source_root=source_root,
                max_bytes=max_bytes,
                error_factory=error_factory,
            ),
        )
        for file_path in files
    ]


__all__ = [
    "ContainedReadRefused",
    "DEFAULT_MAX_CHATGPT_EXPORT_BYTES",
    "DEFAULT_MAX_TEXT_FILE_BYTES",
    "ImportFileTooLargeError",
    "ImportSourceFile",
    "MIB",
    "parse_max_file_mib",
    "contained_source_files",
    "read_contained_source_text",
    "read_text_beneath",
    "snapshot_source_files",
]
