"""Project identity: resolve a project from a start folder, by reading files.

A project is the git repository a folder is in. This module turns a start
folder into a ``ProjectContext`` (one or two ids and a label) or says why there
is none. It is the first slice of per-project memory (the spec is
``wiki/specs/2026-10-01-per-project-memory.md``, sections 4 and 5). Nothing in
the read or write paths calls it yet.

What the resolver does, in order (spec 4.3):

1. The start folder must be absolute by the rules of the platform in use, and
   it is resolved to its real path.
2. It walks up at most 32 levels (the start folder counts as the first) looking
   for a ``.git`` entry. It stops with no project at the home folder and at the
   filesystem or drive root, and it never reads the ``.git`` of either. The
   nearest ``.git`` wins.
3. A ``.git`` directory is the git directory. A ``.git`` file (at most 4 KiB,
   first line ``gitdir: <path>``) names it. A ``commondir`` file in the git
   directory names the common directory, which is how a linked worktree agrees
   with its main checkout.
4. It reads ``config`` in the common directory (at most 256 KiB, no includes
   followed) for the remote URL and ``core.ignorecase``.
5. The primary id is a hash of the normalized remote URL. With a remote there
   is also a second id, a hash of the real path of the common directory, so
   notes written before the remote existed stay readable. With no remote the
   path hash is the only id.

It starts no process and opens no network connection. It reads one
environment variable, the home folder (``HOME``, or ``USERPROFILE`` on
Windows), and only when the caller passes no ``Platform``. The caller reads
``ALICE_PROJECT_DIR`` and the working folder. Every read is bounded, and any
failure means no project, never a path id and never an exception. The raw
remote URL and every path are hashed or dropped. They are never stored, logged
or printed, because an https URL can carry a token.

The platform and filesystem facts are parameters (``Platform`` and
``ProjectFileSystem``), so one test runner covers the Windows and the
case-insensitive rules.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import ntpath
import os
import posixpath
import re
import stat as stat_module
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import ModuleType
from typing import BinaryIO, Literal, Protocol

from alicebot_api.vnext_project_scope import is_alice_project_id

logger = logging.getLogger(__name__)

StartSource = Literal["argument", "env", "hook", "cwd"]
IdSource = Literal["remote", "repo_path"]
Outcome = Literal["found", "none", "failed", "off"]
EntryKind = Literal["missing", "dir", "file", "other"]

ID_DOMAIN = b"alice-project-v1"
ID_PREFIX = "prj_"
ID_HEX_CHARS = 16
#: Directories examined while walking up, counting the start folder.
MAX_WALK_LEVELS = 32
#: Largest ``.git`` file or ``commondir`` file read.
MAX_GIT_FILE_BYTES = 4 * 1024
#: Largest git ``config`` read.
MAX_CONFIG_BYTES = 256 * 1024
#: Largest hook payload parsed. A larger one is drained and ignored.
MAX_HOOK_PAYLOAD_BYTES = 64 * 1024
#: Longest start folder text accepted from any source.
MAX_START_FOLDER_CHARS = 4096
MAX_LABEL_CHARS = 40
HOME_ENV_POSIX = "HOME"
HOME_ENV_WINDOWS = "USERPROFILE"

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_LABEL_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._+-")
_FALLBACK_LABEL = "project"

# A drive letter followed by a slash or backslash, or a UNC path with a server
# and a share. Three leading separators, and the device prefixes ``\\?\`` and
# ``\\.\``, are not UNC paths.
_WINDOWS_DRIVE_PATH = re.compile(r"[A-Za-z]:[\\/]")
_WINDOWS_UNC_PATH = re.compile(r"[\\/]{2}(?![\\/])(?![?.](?:[\\/]|$))[^\\/]+[\\/]+[^\\/]+")


def _ascii_lower(text: str) -> str:
    """Fold ASCII letters only, as the project identity contract does."""

    return text.translate(_ASCII_LOWER)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectContext:
    """What was found. ``ids[0]`` is primary and the only id ever written."""

    ids: tuple[str, ...]
    label: str
    source: IdSource
    start: StartSource


@dataclass(frozen=True, slots=True)
class Detection:
    """A context together with why there is none, when there is none.

    ``found``: a project. ``none``: the folder is not in a git work tree (this
    includes the home folder, a drive root, a folder that does not exist and a
    folder that is not absolute). ``failed``: a git directory was found and
    could not be read within the caps, or the resolver raised. ``off``: scoping
    is off and the resolver was not called (set by the caller, never here).

    ``reason`` is a short fixed code for ``show``. It is never a path, a URL or
    text from the repository. ``start`` is the start-folder source that was
    used, when one was usable, so a failure can still say where it looked.
    """

    outcome: Outcome
    context: ProjectContext | None = None
    reason: str | None = None
    start: StartSource | None = None

    @classmethod
    def off(cls) -> Detection:
        return cls(outcome="off", context=None, reason="scoping_off")


@dataclass(frozen=True, slots=True)
class StartFolder:
    """A usable start folder: its real path and which source supplied it."""

    path: str
    source: StartSource


@dataclass(frozen=True, slots=True)
class Platform:
    """The platform facts the resolver depends on.

    ``windows`` selects the path rules. ``home`` is the home folder as the
    environment gave it (``HOME``, or ``USERPROFILE`` on Windows), or ``None``
    when it is unknown. Tests pass either value on any machine.
    """

    windows: bool
    home: str | None = None


def host_platform(
    environ: Mapping[str, str] | None = None, *, windows: bool | None = None
) -> Platform:
    """The facts of this process. Reads only the home folder variable.

    ``environ`` and ``windows`` exist so a test can pass either platform on any
    machine. The home folder is ``HOME``, or ``USERPROFILE`` on Windows.
    """

    environment = os.environ if environ is None else environ
    is_windows = os.name == "nt" if windows is None else windows
    home = environment.get(HOME_ENV_WINDOWS if is_windows else HOME_ENV_POSIX)
    return Platform(windows=is_windows, home=home or None)


# ---------------------------------------------------------------------------
# Filesystem seam
# ---------------------------------------------------------------------------


class ProjectFileSystem(Protocol):
    """The three file facts the resolver reads. Injected so tests need no disk."""

    def realpath(self, path: str) -> str:
        """The real path (symlinks and junctions followed)."""

    def entry_kind(self, path: str) -> EntryKind:
        """``missing`` if nothing is there, ``dir`` or ``file`` after following
        links, ``other`` for anything else (a dangling link, a pipe)."""

    def read_capped(self, path: str, limit: int) -> bytes:
        """Up to ``limit + 1`` bytes of a regular file, so a caller can tell
        that the file is larger than ``limit``. Raises ``OSError`` otherwise."""


class OsFileSystem:
    """The real filesystem. A pipe or device is refused before it is read."""

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def entry_kind(self, path: str) -> EntryKind:
        try:
            os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return "missing"
        try:
            mode = os.stat(path).st_mode
        except FileNotFoundError:
            return "other"
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return "other"
            raise
        if stat_module.S_ISDIR(mode):
            return "dir"
        if stat_module.S_ISREG(mode):
            return "file"
        return "other"

    def read_capped(self, path: str, limit: int) -> bytes:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(path, flags)
        try:
            if not stat_module.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("not a regular file")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining > 0:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)


# ---------------------------------------------------------------------------
# Paths by platform rules
# ---------------------------------------------------------------------------


def is_absolute_path(text: object, *, windows: bool) -> bool:
    """Absolute by the rules of the platform, whatever machine this runs on.

    POSIX: a leading slash. Windows: a drive letter then a slash or backslash,
    or a UNC path (a server and a share). A rooted path with no drive is not
    absolute on Windows, and a Windows path is not absolute on POSIX.
    """

    if not isinstance(text, str) or text == "" or "\0" in text:
        return False
    if not windows:
        return text.startswith("/")
    return bool(_WINDOWS_DRIVE_PATH.match(text) or _WINDOWS_UNC_PATH.match(text))


def _path_module(windows: bool) -> ModuleType:
    return ntpath if windows else posixpath


def _path_key(path: str, *, windows: bool) -> str:
    """A comparison key: case folded and slash normalized on Windows."""

    if windows:
        return ntpath.normcase(ntpath.normpath(path))
    return posixpath.normpath(path)


def _usable_start_folder(
    candidate: object, *, platform: Platform, fs: ProjectFileSystem
) -> str | None:
    """The real path of a candidate that is an absolute, existing directory."""

    if not isinstance(candidate, str) or len(candidate) > MAX_START_FOLDER_CHARS:
        return None
    if not is_absolute_path(candidate, windows=platform.windows):
        return None
    try:
        real = fs.realpath(candidate)
        if fs.entry_kind(real) != "dir":
            return None
    except (OSError, ValueError):
        return None
    return real


def _home_key(platform: Platform, fs: ProjectFileSystem) -> str | None:
    """The home folder as a comparable real path, or ``None`` if unknown."""

    home = platform.home
    if not isinstance(home, str) or not is_absolute_path(home, windows=platform.windows):
        return None
    try:
        return _path_key(fs.realpath(home), windows=platform.windows)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Start folder selection (spec 4.2)
# ---------------------------------------------------------------------------


def select_start_folder(
    *,
    argument: str | None,
    env_project_dir: str | None,
    hook_cwd: str | None,
    process_cwd: str | None,
    platform: Platform,
    fs: ProjectFileSystem,
) -> StartFolder | None:
    """The first source that yields an absolute, existing directory.

    The order is ``--project-dir``, ``ALICE_PROJECT_DIR``, the hook payload's
    ``cwd`` (the hook only, so other callers pass ``None``) and the process
    working folder. A host variable joins the list between the hook and the
    working folder only after S0 has verified it, and today the list is empty.
    A relative path, a non-string and an oversize string are skipped, and the
    next source is tried.
    """

    sources: tuple[tuple[StartSource, object], ...] = (
        ("argument", argument),
        ("env", env_project_dir),
        ("hook", hook_cwd),
        ("cwd", process_cwd),
    )
    for source, candidate in sources:
        real = _usable_start_folder(candidate, platform=platform, fs=fs)
        if real is not None:
            return StartFolder(path=real, source=source)
    return None


def read_hook_payload(stream: BinaryIO, *, limit: int = MAX_HOOK_PAYLOAD_BYTES) -> bytes | None:
    """Read a host's stdin payload, at most ``limit`` bytes of it.

    A payload larger than the limit returns ``None`` and is not parsed. The rest
    is read and thrown away either way, so the host's write never blocks on a
    full pipe. The read is the same as today's hook, which already waits for
    the host to close stdin.
    """

    data = stream.read(limit + 1)
    if len(data) <= limit:
        return data
    while stream.read(MAX_HOOK_PAYLOAD_BYTES):
        pass
    return None


def hook_payload_cwd(payload: bytes | str | None, *, platform: Platform) -> str | None:
    """The ``cwd`` string of a host payload, if it is an absolute path.

    Anything else is ignored: a payload that is not a JSON object, a missing key,
    a value that is not a string, a relative path or a path over 4096
    characters. S0 records which path-valued keys each host sends, and a later
    change adds a key here only when S0 shows it.
    """

    if payload is None:
        return None
    try:
        decoded = json.loads(payload)
    except (ValueError, RecursionError):
        return None
    if not isinstance(decoded, dict):
        return None
    value = decoded.get("cwd")
    if not isinstance(value, str) or len(value) > MAX_START_FOLDER_CHARS:
        return None
    if not is_absolute_path(value, windows=platform.windows):
        return None
    return value


# ---------------------------------------------------------------------------
# Git config (spec 4.3 step 5): the [remote] and [core] sections only
# ---------------------------------------------------------------------------


class _ConfigError(Exception):
    """The config is not syntax git reads. Never carries config text."""


@dataclass(frozen=True, slots=True)
class GitConfigFacts:
    """The only things read from a git config."""

    remote_urls: Mapping[str, str] = field(default_factory=dict)
    ignorecase: bool = False
    has_include: bool = False


_KEY_START = re.compile(r"[A-Za-z]")
_KEY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-")
_SECTION_NAME = re.compile(r"[A-Za-z0-9.-]+")
_INTEGER = re.compile(r"[+-]?[0-9]+")
_TRUE_WORDS = frozenset({"true", "yes", "on"})


def _config_bool(value: str | None) -> bool:
    if value is None:
        return True
    word = value.strip().lower()
    if word in _TRUE_WORDS:
        return True
    if _INTEGER.fullmatch(word):
        return int(word) != 0
    return False


def _skip_to_line_end(text: str, index: int) -> int:
    end = text.find("\n", index)
    return len(text) if end == -1 else end + 1


def _parse_header(text: str, index: int) -> tuple[int, tuple[str, str | None]]:
    """Parse ``[name]``, ``[name "sub"]`` or the legacy ``[name.sub]``."""

    length = len(text)
    index += 1
    match = _SECTION_NAME.match(text, index)
    if match is None:
        raise _ConfigError
    name = match.group(0)
    index = match.end()
    subsection: str | None = None
    while index < length and text[index] in " \t":
        index += 1
    if index < length and text[index] == '"':
        index += 1
        pieces: list[str] = []
        while True:
            if index >= length or text[index] == "\n":
                raise _ConfigError
            char = text[index]
            if char == '"':
                index += 1
                break
            if char == "\\":
                index += 1
                if index >= length or text[index] == "\n":
                    raise _ConfigError
                char = text[index]
            pieces.append(char)
            index += 1
        subsection = "".join(pieces)
        while index < length and text[index] in " \t":
            index += 1
    elif "." in name:
        name, _, legacy = name.partition(".")
        subsection = legacy.lower()
        if name == "" or subsection == "":
            raise _ConfigError
    if index >= length or text[index] != "]":
        raise _ConfigError
    return index + 1, (name.lower(), subsection)


_ESCAPES = {"n": "\n", "t": "\t", "b": "\b", "\\": "\\", '"': '"'}


def _parse_value(text: str, index: int) -> tuple[int, str]:
    """Parse a value to the end of its logical line, as git reads it.

    Leading and trailing unquoted whitespace is dropped, a quoted part keeps its
    whitespace, a backslash escapes only a backslash, a double quote, ``n``, ``t``
    and ``b``, a backslash before a newline continues the line, and an unquoted
    ``#`` or ``;`` starts a comment.
    """

    length = len(text)
    while index < length and text[index] in " \t":
        index += 1
    pieces: list[str] = []
    in_quote = False
    pending_space = ""
    while index < length:
        char = text[index]
        if char == "\n":
            if in_quote:
                raise _ConfigError
            index += 1
            break
        if char == "\\":
            index += 1
            if index >= length:
                raise _ConfigError
            escaped = text[index]
            if escaped == "\n":
                index += 1
                continue
            if escaped == "\r" and text[index + 1 : index + 2] == "\n":
                index += 2
                continue
            if escaped not in _ESCAPES:
                raise _ConfigError
            pieces.append(pending_space + _ESCAPES[escaped])
            pending_space = ""
            index += 1
            continue
        if char == '"':
            in_quote = not in_quote
            pieces.append(pending_space)
            pending_space = ""
            index += 1
            continue
        if not in_quote and char in "#;":
            index = _skip_to_line_end(text, index)
            break
        if not in_quote and char in " \t\r":
            pending_space += char
            index += 1
            continue
        pieces.append(pending_space + char)
        pending_space = ""
        index += 1
    if in_quote:
        raise _ConfigError
    return index, "".join(pieces)


def parse_git_config(data: bytes) -> GitConfigFacts:
    """Read the remote URLs, ``core.ignorecase`` and whether an include exists.

    Only ``[remote "name"]`` ``url`` and ``[core]`` ``ignorecase`` are used.
    Other sections are parsed for syntax and skipped. A syntax error raises
    ``_ConfigError`` and the caller reports a failed detection. The first
    non-empty ``url`` of a remote wins, as git uses the first for a fetch.
    """

    if b"\0" in data:
        raise _ConfigError
    text = data.decode("utf-8", errors="surrogateescape")
    if text.startswith("\ufeff"):
        text = text[1:]
    remote_urls: dict[str, str] = {}
    ignorecase = False
    has_include = False
    section: tuple[str, str | None] | None = None
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char in "#;":
            index = _skip_to_line_end(text, index)
            continue
        if char == "[":
            index, section = _parse_header(text, index)
            if section[0] == "include" or section[0] == "includeif":
                has_include = True
            continue
        if section is None or _KEY_START.match(text, index) is None:
            raise _ConfigError
        key_end = index + 1
        while key_end < length and text[key_end] in _KEY_CHARS:
            key_end += 1
        key = text[index:key_end].lower()
        index = key_end
        while index < length and text[index] in " \t":
            index += 1
        value: str | None
        if index < length and text[index] == "=":
            index, value = _parse_value(text, index + 1)
        elif index >= length or text[index] in "\r\n#;":
            index = _skip_to_line_end(text, index)
            value = None
        else:
            raise _ConfigError
        name, subsection = section
        if name == "remote" and subsection is not None and key == "url":
            if value and subsection not in remote_urls:
                remote_urls[subsection] = value
        elif name == "core" and subsection is None and key == "ignorecase":
            ignorecase = _config_bool(value)
    return GitConfigFacts(
        remote_urls=remote_urls, ignorecase=ignorecase, has_include=has_include
    )


# ---------------------------------------------------------------------------
# Remote normalization (spec 4.3 step 6)
# ---------------------------------------------------------------------------

_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*)://")
_TRANSPORT_HELPER = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*::")
_DRIVE_LETTER = re.compile(r"[A-Za-z]:")
_DEFAULT_PORTS = {
    "ssh": 22,
    "ssh+git": 22,
    "git+ssh": 22,
    "https": 443,
    "http": 80,
    "git": 9418,
}
_CASE_FOLDED_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org"})


class _NoRemote(Exception):
    """The URL counts as no remote."""


def _parse_port(text: str) -> int | None:
    """``None`` for an empty port. A number from 1 to 65535, else no remote."""

    if text == "":
        return None
    if not (text.isascii() and text.isdigit()):
        raise _NoRemote
    significant = text.lstrip("0")
    if significant == "" or len(significant) > 5:
        raise _NoRemote
    port = int(significant)
    if port > 65535:
        raise _NoRemote
    return port


def _split_host_port(hostport: str) -> tuple[str, int | None]:
    """Split a ``host[:port]`` of a URL. Brackets mark an IPv6 literal."""

    if hostport.startswith("["):
        close = hostport.find("]")
        if close == -1:
            raise _NoRemote
        inner = hostport[1:close]
        if inner == "":
            raise _NoRemote
        host = "[" + _ascii_lower(inner) + "]"
        after = hostport[close + 1 :]
        if after == "":
            return host, None
        if not after.startswith(":"):
            raise _NoRemote
        return host, _parse_port(after[1:])
    # An unbracketed host with more than one colon (an IPv6 address with no
    # brackets) puts a colon in the port text, and the port parse refuses it.
    host_text, _, port_text = hostport.partition(":")
    host = _ascii_lower(host_text)
    if host == "":
        raise _NoRemote
    return host, _parse_port(port_text)


def _scp_like_parts(text: str) -> tuple[str, str]:
    """Split ``[user@]host:path`` into the host and the path, as git reads it."""

    slash = text.find("/")
    head = text if slash == -1 else text[:slash]
    tail = "" if slash == -1 else text[slash:]
    at = head.find("@")
    hostpart = head[at + 1 :] if at != -1 else head
    if hostpart.startswith("["):
        close = hostpart.find("]")
        if close == -1:
            raise _NoRemote
        inner = hostpart[1:close]
        if inner == "" or not hostpart[close + 1 :].startswith(":"):
            raise _NoRemote
        return "[" + _ascii_lower(inner) + "]", hostpart[close + 2 :] + tail
    colon = hostpart.find(":")
    if colon == -1:
        # No colon before the first slash: a local path.
        raise _NoRemote
    host = _ascii_lower(hostpart[:colon])
    if host == "":
        raise _NoRemote
    return host, hostpart[colon + 1 :] + tail


def _normalized_path(path: str, host: str) -> str:
    text = path.lstrip("/").rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")].rstrip("/")
    if host in _CASE_FOLDED_HOSTS:
        text = _ascii_lower(text)
    return text


def normalize_remote_url(raw: str) -> str | None:
    """Normalize a remote URL to ``host[:port]/path``, or ``None`` for no remote.

    Drops the scheme, user, password, query and fragment, lowercases the host,
    keeps a port unless it is the default of the URL's own scheme, makes the
    scp-like and the url spellings of one repository the same string, drops a
    trailing ``.git`` and slashes, and lowercases the path for github.com,
    gitlab.com and bitbucket.org only. A URL with no host (a local path,
    ``file://``, a Windows drive path), a port that is not an integer from 1 to
    65535, an unbracketed host with several colons and an unclosed bracket count
    as no remote. ``insteadOf`` rewrites are not applied. The return value is the
    only form of the URL that is ever hashed.
    """

    url = raw.strip()
    if url == "" or any(ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        return None
    if _TRANSPORT_HELPER.match(url):
        return None
    try:
        scheme_match = _SCHEME.match(url)
        if scheme_match is not None:
            scheme = _ascii_lower(scheme_match.group(1))
            if scheme == "file":
                return None
            rest = url[scheme_match.end() :].split("#", 1)[0].split("?", 1)[0]
            slash = rest.find("/")
            authority = rest if slash == -1 else rest[:slash]
            path = "" if slash == -1 else rest[slash:]
            hostport = authority[authority.rfind("@") + 1 :]
            host, port = _split_host_port(hostport)
            if port is not None and port != _DEFAULT_PORTS.get(scheme):
                host_text = f"{host}:{port}"
            else:
                host_text = host
        else:
            if _DRIVE_LETTER.match(url):
                return None
            stripped = url.split("#", 1)[0].split("?", 1)[0]
            host, path = _scp_like_parts(stripped)
            host_text = host
    except _NoRemote:
        return None
    normalized_path = _normalized_path(path, host)
    return host_text if normalized_path == "" else f"{host_text}/{normalized_path}"


# ---------------------------------------------------------------------------
# Ids and labels (spec 4.3 steps 7 and 8)
# ---------------------------------------------------------------------------


def project_id_for(kind: Literal["remote", "path"], text: str) -> str:
    """``prj_`` plus 16 hex characters of SHA-256, domain separated.

    The hashed bytes are ``alice-project-v1``, a NUL, the kind, a NUL and the
    text. Text that is not valid UTF-8 (a path with stray bytes) hashes as the
    original bytes.
    """

    digest = hashlib.sha256(
        ID_DOMAIN + b"\0" + kind.encode("ascii") + b"\0" + text.encode("utf-8", "surrogateescape")
    ).hexdigest()
    return ID_PREFIX + digest[:ID_HEX_CHARS]


def sanitize_label(text: str) -> str:
    """A label is one token of ``[A-Za-z0-9._+-]``, at most 40 characters.

    Every other character, a space included, becomes ``-``. Text from a folder
    or repository name is untrusted: a name made of plain words survives as a
    hyphenated phrase, so a reader that prints a label quotes it.
    """

    cleaned = "".join(char if char in _LABEL_ALPHABET else "-" for char in text)
    return cleaned[:MAX_LABEL_CHARS] or _FALLBACK_LABEL


def _label_for(normalized_url: str | None, common_dir: str, *, windows: bool) -> str:
    if normalized_url is not None:
        segments = [segment for segment in normalized_url.split("/")[1:] if segment]
        if segments:
            return sanitize_label(segments[-1])
    path_module = _path_module(windows)
    name = path_module.basename(common_dir)
    if _ascii_lower(name) == ".git":
        name = path_module.basename(path_module.dirname(common_dir))
    elif _ascii_lower(name).endswith(".git"):
        name = name[: -len(".git")]
    return sanitize_label(name)


def _path_hash_text(common_dir: str, *, windows: bool, ignorecase: bool) -> str:
    text = common_dir.replace("/", "\\") if windows else common_dir
    if windows or ignorecase:
        text = _ascii_lower(text)
    return text


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


class _Failed(Exception):
    """A git directory was found and could not be read. Carries a fixed code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _none(reason: str) -> Detection:
    return Detection(outcome="none", context=None, reason=reason)


def _read_text_file(
    fs: ProjectFileSystem, path: str, limit: int, *, unreadable: str, too_large: str
) -> bytes:
    try:
        data = fs.read_capped(path, limit)
    except (OSError, ValueError) as exc:
        raise _Failed(unreadable) from exc
    if len(data) > limit:
        raise _Failed(too_large)
    return data


def _first_line(data: bytes) -> str:
    text = data.decode("utf-8", errors="surrogateescape")
    return text.split("\n", 1)[0].rstrip("\r")


def _git_dir_from_file(
    fs: ProjectFileSystem, folder: str, git_file: str, *, platform: Platform
) -> str:
    data = _read_text_file(
        fs, git_file, MAX_GIT_FILE_BYTES, unreadable="git_file_unreadable", too_large="git_file_too_large"
    )
    line = _first_line(data)
    if not line.startswith("gitdir:"):
        raise _Failed("git_file_malformed")
    target = line[len("gitdir:") :].strip()
    if target == "":
        raise _Failed("git_file_malformed")
    if is_absolute_path(target, windows=platform.windows):
        return target
    return _path_module(platform.windows).join(folder, target)


def _real_dir(fs: ProjectFileSystem, path: str, *, reason: str) -> str:
    try:
        real = fs.realpath(path)
        kind = fs.entry_kind(real)
    except (OSError, ValueError) as exc:
        raise _Failed(reason) from exc
    if kind != "dir":
        raise _Failed(reason)
    return real


def _common_dir(fs: ProjectFileSystem, git_dir: str, *, platform: Platform) -> str:
    path_module = _path_module(platform.windows)
    commondir_path = path_module.join(git_dir, "commondir")
    try:
        kind = fs.entry_kind(commondir_path)
    except OSError as exc:
        raise _Failed("common_dir_unreadable") from exc
    if kind == "missing":
        return git_dir
    if kind != "file":
        raise _Failed("common_dir_unreadable")
    data = _read_text_file(
        fs,
        commondir_path,
        MAX_GIT_FILE_BYTES,
        unreadable="common_dir_unreadable",
        too_large="common_dir_unreadable",
    )
    named = _first_line(data).strip()
    if named == "":
        raise _Failed("common_dir_unreadable")
    if not is_absolute_path(named, windows=platform.windows):
        named = path_module.join(git_dir, named)
    return _real_dir(fs, named, reason="common_dir_unreadable")


def _read_repository(
    fs: ProjectFileSystem,
    folder: str,
    git_entry: str,
    kind: EntryKind,
    *,
    platform: Platform,
    start: StartSource,
) -> Detection:
    if kind == "dir":
        git_dir_path = git_entry
    elif kind == "file":
        git_dir_path = _git_dir_from_file(fs, folder, git_entry, platform=platform)
    else:
        raise _Failed("git_entry_unreadable")
    git_dir = _real_dir(fs, git_dir_path, reason="git_dir_unreadable")
    common = _common_dir(fs, git_dir, platform=platform)

    path_module = _path_module(platform.windows)
    config_path = path_module.join(common, "config")
    try:
        config_kind = fs.entry_kind(config_path)
    except OSError as exc:
        raise _Failed("config_missing_or_unreadable") from exc
    if config_kind != "file":
        raise _Failed("config_missing_or_unreadable")
    config_bytes = _read_text_file(
        fs,
        config_path,
        MAX_CONFIG_BYTES,
        unreadable="config_missing_or_unreadable",
        too_large="config_too_large",
    )
    try:
        facts = parse_git_config(config_bytes)
    except _ConfigError as exc:
        raise _Failed("config_malformed") from exc
    if facts.has_include:
        # An included file could hold the remote, so a repository that has one
        # might get a different id by accident if the include were skipped.
        raise _Failed("config_uses_include")

    remote_url: str | None
    if "origin" in facts.remote_urls:
        remote_url = facts.remote_urls["origin"]
    elif len(facts.remote_urls) == 1:
        remote_url = next(iter(facts.remote_urls.values()))
    else:
        remote_url = None
    normalized = normalize_remote_url(remote_url) if remote_url is not None else None

    path_id = project_id_for(
        "path",
        _path_hash_text(common, windows=platform.windows, ignorecase=facts.ignorecase),
    )
    ids: tuple[str, ...]
    if normalized is not None:
        ids = (project_id_for("remote", normalized), path_id)
        source: IdSource = "remote"
    else:
        ids = (path_id,)
        source = "repo_path"
    context = ProjectContext(
        ids=ids,
        label=_label_for(normalized, common, windows=platform.windows),
        source=source,
        start=start,
    )
    return Detection(outcome="found", context=context)


def _walk(
    start: str, *, platform: Platform, fs: ProjectFileSystem, start_source: StartSource
) -> Detection:
    windows = platform.windows
    path_module = _path_module(windows)
    home_key = _home_key(platform, fs)
    current = start
    for _level in range(MAX_WALK_LEVELS):
        if home_key is not None and _path_key(current, windows=windows) == home_key:
            return _none("reached_home_folder")
        parent = path_module.dirname(current)
        if parent == current:
            return _none("reached_filesystem_root")
        git_entry = path_module.join(current, ".git")
        try:
            kind = fs.entry_kind(git_entry)
        except OSError as exc:
            raise _Failed("git_entry_unreadable") from exc
        if kind != "missing":
            return _read_repository(
                fs, current, git_entry, kind, platform=platform, start=start_source
            )
        current = parent
    return _none("walk_limit_reached")


def resolve_project(
    start: str,
    *,
    start_source: StartSource,
    platform: Platform,
    fs: ProjectFileSystem,
) -> Detection:
    """Resolve a start folder to a project, or say why there is none.

    Never raises. A relative or missing start folder is ``none``. A git
    directory that was found and could not be read within the caps, and any
    exception that is not a read failure, is ``failed``, and neither falls back
    to a path id.
    """

    try:
        real = _usable_start_folder(start, platform=platform, fs=fs)
        if real is None:
            return _none("no_usable_start_folder")
        try:
            detection = _walk(real, platform=platform, fs=fs, start_source=start_source)
        except _Failed as failure:
            detection = Detection(outcome="failed", context=None, reason=failure.reason)
        return replace(detection, start=start_source)
    except Exception as exc:
        # Only the type is logged: an exception message can hold a path.
        logger.debug("project detection raised %s", type(exc).__name__)
        return Detection(outcome="failed", context=None, reason="unexpected_error", start=start_source)


def detect_project(
    *,
    argument: str | None = None,
    env_project_dir: str | None = None,
    hook_cwd: str | None = None,
    process_cwd: str | None = None,
    platform: Platform | None = None,
    fs: ProjectFileSystem | None = None,
) -> Detection:
    """Pick the start folder (4.2) and resolve it. The edge calls this.

    Every input is a parameter: the caller reads ``ALICE_PROJECT_DIR`` and the
    working folder, and library code never calls this. Never raises.
    """

    try:
        platform = platform if platform is not None else host_platform()
        fs = fs if fs is not None else OsFileSystem()
        start = select_start_folder(
            argument=argument,
            env_project_dir=env_project_dir,
            hook_cwd=hook_cwd,
            process_cwd=process_cwd,
            platform=platform,
            fs=fs,
        )
        if start is None:
            return _none("no_usable_start_folder")
        return resolve_project(
            start.path, start_source=start.source, platform=platform, fs=fs
        )
    except Exception as exc:
        logger.debug("project detection raised %s", type(exc).__name__)
        return Detection(outcome="failed", context=None, reason="unexpected_error")


__all__ = [
    "Detection",
    "EntryKind",
    "GitConfigFacts",
    "ID_DOMAIN",
    "ID_PREFIX",
    "IdSource",
    "MAX_CONFIG_BYTES",
    "MAX_GIT_FILE_BYTES",
    "MAX_HOOK_PAYLOAD_BYTES",
    "MAX_LABEL_CHARS",
    "MAX_START_FOLDER_CHARS",
    "MAX_WALK_LEVELS",
    "OsFileSystem",
    "Outcome",
    "Platform",
    "ProjectContext",
    "ProjectFileSystem",
    "StartFolder",
    "StartSource",
    "detect_project",
    "hook_payload_cwd",
    "host_platform",
    "is_absolute_path",
    "is_alice_project_id",
    "normalize_remote_url",
    "parse_git_config",
    "project_id_for",
    "read_hook_payload",
    "resolve_project",
    "sanitize_label",
    "select_start_folder",
]
