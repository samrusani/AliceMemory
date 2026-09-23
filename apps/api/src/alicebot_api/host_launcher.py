"""What the host entries and hooks that ``alice-memory install`` writes run.

An alice MCP entry runs a *launcher*: either uvx, which fetches the
alice-memory package itself (``uvx alice-memory mcp``), or the installed
``alice-memory`` console script (``/path/bin/alice-memory mcp``). The
SessionStart hook always follows the entry's launcher: uvx runs
``alice-memory-session-start`` with the same uvx options and package spec,
and a script launcher uses the ``alice-memory-session-start`` in the same
bin dir.

This module decides three things, with no host file I/O:

- whether an existing entry is one install wrote (its shape), and what it
  runs;
- whether that launcher can run on this machine, and which working
  launcher install can offer instead;
- how to write a hook command so the host's shell runs exactly the argv
  install means, and how far a hook read back can be trusted.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import sysconfig
import tomllib
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

MCP_COMMAND = "uvx"
MCP_SCRIPT = "alice-memory"
PACKAGE = "alice-memory"
SESSION_START_COMMAND = "alice-memory-session-start"
# alice-memory-session-start first shipped in this release.
FIRST_SESSION_START_VERSION = "0.16.0"
# The data dir the README and docs show in their example entry. Pasted as
# is, it names no real store, so install treats it as unset.
DOCS_DATA_DIR_PLACEHOLDER = "/ABSOLUTE/PATH/TO/.alice"
HIDDEN = "<hidden>"

_UVX_NAMES = frozenset({"uvx", "uvx.exe"})
_SCRIPT_NAMES = frozenset({MCP_SCRIPT, f"{MCP_SCRIPT}.exe"})
_SESSION_START_NAMES = frozenset({SESSION_START_COMMAND, f"{SESSION_START_COMMAND}.exe"})
# A PEP 508 name for alice-memory, optional extras, and an optional version
# constraint or uvx's @version. A comparison operator must be followed by a
# version, which starts with a digit; ``===`` and ``@`` take anything.
# "alice-memory>0.15" is in; "alice-memory-x" is out.
_ALICE_SPEC = re.compile(
    r"(?P<name>alice[-_.]+memory)(?P<extras>\[[^\]]*\])?"
    r"(?P<version>(?:===|@)\S+|(?:==|!=|~=|>=|<=|>|<)[0-9]\S*)?\Z",
    re.IGNORECASE,
)
# uvx options that take a separate value, so the value is not the package.
_UVX_VALUE_OPTIONS = frozenset(
    {
        "--with",
        "-w",
        "--with-editable",
        "--with-requirements",
        "--python",
        "-p",
        "--index",
        "--index-url",
        "-i",
        "--extra-index-url",
        "--default-index",
        "--find-links",
        "-f",
        "--constraints",
        "--constraint",
        "-c",
        "--overrides",
        "--override",
        "--build-constraints",
        "-b",
        "--cache-dir",
        "--directory",
        "--project",
        "--config-file",
        "--color",
        "--python-preference",
        "--resolution",
        "--prerelease",
        "--exclude-newer",
        "--exclude-newer-package",
        "--refresh-package",
        "--reinstall-package",
        "--upgrade-package",
        "-P",
        "--link-mode",
        "--keyring-provider",
        "--index-strategy",
        "--allow-insecure-host",
        "--config-setting",
        "-C",
        "--env-file",
    }
)
# The only uvx options install carries from an entry into its hook. Any other
# option could make the hook resolve a different alice-memory than the
# server, so an entry with one gets no new hook (see host_install._hook_block).
CARRY_OPTIONS_WITH_VALUE = frozenset({"--prerelease", "--python", "-p", "--python-preference"})
CARRY_FLAGS = frozenset({"--native-tls", "--offline", "--no-cache", "--refresh"})
# Options that name a package index or a place to find packages: their value
# may hold a URL, and a relative path would resolve against the hook's cwd.
INDEX_OPTIONS = frozenset(
    {"--index", "--index-url", "-i", "--extra-index-url", "--default-index", "--find-links", "-f"}
)
# uv keeps installed packages in buckets named archive-vN and environments-vN
# directly under the cache root, each holding one directory per id.
_UV_CACHE_BUCKET = re.compile(r"(?:archive|environments)-v\d+\Z")
# The first line of a CACHEDIR.TAG (bford.info/cachedir), which uv writes at
# the root of every cache it creates, wherever --cache-dir or uv.toml put it.
_CACHEDIR_TAG_SIGNATURE = "Signature: 8a477f597d28d172789f06886806bc55"

UV_TEMP_ENV_WARNING = (
    "warning: install ran from a temporary uv environment; uv looks installed but uvx is "
    "not on PATH, so the entries install writes run uvx by name. The hosts will start "
    "Alice once uvx is on PATH."
)
UVX_MISSING_WARNING_PREFIX = (
    "warning: uvx is not on PATH, and no directory holds both alice-memory and "
    "alice-memory-session-start"
)

# Tests patch this to exercise the Windows rules on any OS.
WINDOWS_HOOKS = sys.platform == "win32"


_VERSION = re.compile(
    r"v?(\d+(?:\.\d+)*)"
    r"(?:[-_.]?(dev|a|alpha|b|beta|c|rc|pre|preview|post|r|rev)[-_.]?(\d*))?"
    r"(?:\+[a-z0-9.]+)?\Z",
    re.IGNORECASE,
)
_PHASE_RANK = {
    "dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "c": 3, "rc": 3, "pre": 3,
    "preview": 3, "post": 5, "r": 5, "rev": 5,
}
_CLAUSE = re.compile(r"(===|==|!=|~=|>=|<=|>|<|@)\s*(\S+)\Z")


def _release(text: str) -> list[int]:
    return [int(part) for part in text.split(".")]


def _trimmed(release: Sequence[int]) -> tuple[int, ...]:
    parts = list(release)
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _version_key(text: str) -> tuple[tuple[int, ...], int, int] | None:
    """A sort key for a PEP 440 version: release, then dev < a < b < rc < final < post."""

    match = _VERSION.match(text.strip())
    if match is None:
        return None
    phase = match.group(2)
    rank = 4 if phase is None else _PHASE_RANK[phase.lower()]
    return (_trimmed(_release(match.group(1))), rank, int(match.group(3) or 0))


_FIRST = _version_key(FIRST_SESSION_START_VERSION)
_FIRST_RELEASE = tuple(_release(FIRST_SESSION_START_VERSION))


def _clause_allows_first(operator: str, value: str) -> bool | None:
    """Can a version meeting ``operator value`` be 0.16.0 or later? None: cannot tell."""

    assert _FIRST is not None  # nosec B101 # narrows the type for mypy; the module constant always parses
    if operator == "@":
        if value.lower() == "latest":
            return True
        operator = "=="
    if operator in ("==", "!=") and value.endswith(".*"):
        prefix = value[:-2]
        if operator == "!=" or not re.fullmatch(r"\d+(?:\.\d+)*", prefix):
            return None
        parts = tuple(_release(prefix))
        first = (_FIRST_RELEASE + (0,) * len(parts))[: len(parts)]
        return parts >= first
    key = _version_key(value)
    if key is None:
        return None
    if operator in (">=", ">", "!="):
        return True
    if operator in ("==", "===", "<="):
        return key >= _FIRST
    if operator == "<":
        return key > _FIRST
    if operator == "~=":
        match = _VERSION.match(value)
        assert match is not None  # nosec B101 # narrows the type for mypy; _version_key matched this value above
        release = _release(match.group(1))
        if len(release) < 2:
            return None
        upper = release[:-1]
        upper[-1] += 1
        return (_trimmed(upper), 4, 0) > _FIRST
    return None


def session_start_support(spec: str) -> str:
    """Whether ``spec`` can resolve to a release with alice-memory-session-start.

    "yes": some version it allows is 0.16.0 or later; "no": every version it
    allows is older; "unknown": install cannot read the constraint. Each
    comma-separated clause is read on its own; a != wildcard is "unknown".
    """

    match = _ALICE_SPEC.match(spec)
    if match is None:
        return "unknown"
    version = match.group("version")
    if version is None:
        return "yes"
    results: list[bool | None] = []
    for clause in version.split(","):
        parsed = _CLAUSE.match(clause.strip())
        if parsed is None:
            return "unknown"
        results.append(_clause_allows_first(parsed.group(1), parsed.group(2)))
    if any(result is False for result in results):
        return "no"
    if any(result is None for result in results):
        return "unknown"
    return "yes"


# --- URLs and secrets in printed words (round 7 ruling) ---------------------------------

# Where a URL starts inside an argv token; it runs to the end of the token.
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://")
# A URL in free text runs to the end of its whitespace-delimited word: RFC
# 3986 allows both ' and ) in user info, so neither may end it.
_TEXT_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
# A URL install already masked, followed only by punctuation from its own
# message: nothing is left to hide, so the punctuation stays.
_ALREADY_MASKED = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://<hidden>[),.;:'\"]*\Z")
_SECRET_FLAG_WORDS = ("key", "token", "secret", "password")
_URL_HIDDEN = "a URL (everything after its scheme)"


def mask_url(url: str) -> str:
    """``url`` as install prints it: its scheme, then <hidden>, host included.

    No content test can tell a token from a repo name or a host label, so
    everything after ``://`` is hidden.
    """

    match = _SCHEME.match(url)
    return f"{match.group(1)}://{HIDDEN}" if match else HIDDEN


def _url_in(token: str) -> tuple[str, str] | None:
    """(text before the URL, the URL to the end of the token), or None without one."""

    match = _SCHEME.search(token)
    return (token[: match.start()], token[match.start() :]) if match else None


def masked_args(tokens: Sequence[str]) -> tuple[list[str], list[str]]:
    """``tokens`` as install prints them, and a description of each thing hidden.

    Hidden: everything after a URL's scheme, and the value after any flag
    whose name holds key, token, secret or password (``--flag v`` or
    ``--flag=v``).
    """

    shown: list[str] = []
    hidden: list[str] = []
    secret_flag: str | None = None
    for token in tokens:
        if secret_flag is not None:
            shown.append(HIDDEN)
            hidden.append(f"the value of {secret_flag}")
            secret_flag = None
            continue
        if token.startswith("-"):
            name, equals, _value = token.partition("=")
            if any(word in name.lower() for word in _SECRET_FLAG_WORDS):
                if equals:
                    shown.append(f"{name}={HIDDEN}")
                    hidden.append(f"the value of {name}")
                else:
                    shown.append(token)
                    secret_flag = name
                continue
        found = _url_in(token)
        if found is not None:
            shown.append(found[0] + mask_url(found[1]))
            hidden.append(_URL_HIDDEN)
            continue
        shown.append(token)
    return shown, hidden


def _is_absolute(word: str) -> bool:
    return word.startswith("/") or PureWindowsPath(word).is_absolute()


def _plain_alice_spec(word: str) -> bool:
    """alice-memory, optionally with a version constraint, and never a URL."""

    return bool(_ALICE_SPEC.match(word)) and not _SCHEME.search(word)


def shown_hook_words(words: Sequence[str]) -> tuple[list[str], int]:
    """A hook command's words as install prints them, and how many it hid.

    An allowlist, like the carry allowlist: a word is shown only when it is
    uvx, an absolute path to uvx, alice-memory or alice-memory-session-start,
    the bare alice-memory-session-start uvx runs, ``--from`` with a plain
    alice-memory spec, ``--data-dir`` with its value, a plain alice-memory
    spec, or a carried uvx option (CARRY_OPTIONS_WITH_VALUE with its value,
    CARRY_FLAGS). No shown word holds a URL. Every other word, an assignment
    in any shell's syntax, a curl header, anything unknown, is <hidden>.
    """

    programs = _UVX_NAMES | _SCRIPT_NAMES | _SESSION_START_NAMES
    shown: list[str] = []
    hidden = 0
    pending: str | None = None
    for word in words:
        has_url = bool(_SCHEME.search(word))
        if pending is not None:
            kind, pending = pending, None
            ok = (kind == "spec" and _plain_alice_spec(word)) or (kind != "spec" and not has_url)
        elif word in ("--from", "--data-dir") or word in CARRY_OPTIONS_WITH_VALUE:
            ok = True
            pending = {"--from": "spec", "--data-dir": "dir"}.get(word, "value")
        elif word.startswith("--from="):
            ok = _plain_alice_spec(word.split("=", 1)[1])
        elif word.startswith("--data-dir=") or word.partition("=")[0] in CARRY_OPTIONS_WITH_VALUE:
            ok = not has_url
        elif not word.startswith("--") and len(word) > 2 and word[:2] in CARRY_OPTIONS_WITH_VALUE:
            ok = not has_url  # -p3.12
        else:
            ok = (
                word in CARRY_FLAGS
                or word in _UVX_NAMES
                or word.lower() in _SESSION_START_NAMES
                or (_is_absolute(word) and _basename(word) in programs)
                or _plain_alice_spec(word)
            )
        shown.append(word if ok else HIDDEN)
        hidden += 0 if ok else 1
    return shown, hidden


def mask_text(text: str) -> str:
    """Free text as install prints it: every URL shown as its scheme and <hidden>.

    A URL runs to the end of its whitespace-delimited word, so a quote or a
    parenthesis in its user info cannot end it early.
    """

    def replace(found: re.Match[str]) -> str:
        url = found.group(0)
        return url if _ALREADY_MASKED.match(url) else mask_url(url)

    return _TEXT_URL.sub(replace, text)


def spec_is_default(spec: str) -> bool:
    """True when ``spec`` asks for plain alice-memory: no extras, no version but @latest."""

    match = _ALICE_SPEC.match(spec)
    if match is None:
        return False
    version = match.group("version")
    return match.group("extras") is None and (version is None or version.lower() == "@latest")


@dataclass(frozen=True)
class Launcher:
    """What an alice entry runs: its command and the args up to ``mcp``.

    ``prefix`` is everything before the server's own options, so the entry's
    args are ``[*prefix, *server_args]``. For uvx, ``package`` is the
    alice-memory spec it runs and ``options`` are the uvx options before
    that spec (``--from`` aside); the hook reuses both, so it resolves the
    same version from the same index as the entry.
    """

    command: str
    prefix: tuple[str, ...]
    kind: str  # "uvx" or "script"
    package: str = PACKAGE
    options: tuple[str, ...] = ()

    @property
    def customisation(self) -> str | None:
        """What the user chose that install keeps, in receipt words, or None.

        Only uvx options and a spec that asks for something other than plain
        alice-memory count; ``--from alice-memory``, ``alice_memory`` and
        ``alice-memory@latest`` are the default spelled another way.
        """

        if self.kind != "uvx":
            return None
        parts: list[str] = []
        if self.options:
            parts.append("sets uvx options " + " ".join(masked_args(self.options)[0]))
        if not spec_is_default(self.package):
            parts.append(f"asks for {self.shown_package}")
        return " and ".join(parts) or None

    @property
    def customised(self) -> bool:
        return self.customisation is not None

    def _option_groups(self) -> list[tuple[str, list[str]]]:
        """Each uvx option before the spec as (name, its words), attached short forms split."""

        groups: list[tuple[str, list[str]]] = []
        tokens = list(self.options)
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if not token.startswith("--") and len(token) > 2 and token[:2] in _UVX_VALUE_OPTIONS:
                name, step = token[:2], 1  # a short option with its value attached: -fhttps://...
            else:
                name = token.partition("=")[0]
                step = 2 if "=" not in token and token in _UVX_VALUE_OPTIONS else 1
            groups.append((name, tokens[index : index + step]))
            index += step
        return groups

    @property
    def uncarried_options(self) -> tuple[list[str], list[str]]:
        """(options naming an index or a URL, other options off the carry list), masked.

        A word holding ``scheme://`` in any form (separate value, ``=``,
        attached short option), or an index option even with a local path,
        goes in the first list; any option not in CARRY_OPTIONS_WITH_VALUE
        or CARRY_FLAGS in the second.
        """

        index_words: list[str] = []
        off_list: list[str] = []
        for name, words in self._option_groups():
            shown = " ".join(masked_args(words)[0])
            if name in INDEX_OPTIONS or any(_SCHEME.search(word) for word in words):
                index_words.append(shown)
            elif name not in CARRY_OPTIONS_WITH_VALUE and name not in CARRY_FLAGS:
                off_list.append(shown)
        return index_words, off_list

    @property
    def carry_options(self) -> list[str]:
        """The words of the options install carries into a hook, as written."""

        return [
            word
            for name, words in self._option_groups()
            if (name in CARRY_OPTIONS_WITH_VALUE or name in CARRY_FLAGS)
            and not any(_SCHEME.search(item) for item in words)
            for word in words
        ]

    @property
    def shown_package(self) -> str:
        """The package spec as install prints it: a URL in it shows only its scheme."""

        return masked_args([self.package])[0][0]

    @property
    def package_has_url(self) -> bool:
        """True for a direct-URL spec such as alice-memory@https://..."""

        return bool(_SCHEME.search(self.package))

    @property
    def session_start_support(self) -> str:
        """"yes", "no" or "unknown": can the hook's alice-memory-session-start exist?"""

        return session_start_support(self.package) if self.kind == "uvx" else "yes"

    def describe(self) -> str:
        """The launcher for a receipt line: every URL after its scheme and secret flags hidden."""

        return " ".join((self.command, *masked_args(self.prefix)[0]))

    def hook_argv(self) -> list[str]:
        """The hook's argv before ``--data-dir``."""

        if self.kind == "uvx":
            return [self.command, *self.options, "--from", self.package, SESSION_START_COMMAND]
        return [sibling_script(self.command, SESSION_START_COMMAND)]


UVX_LAUNCHER = Launcher(MCP_COMMAND, (PACKAGE, "mcp"), "uvx")


def script_launcher(path: str) -> Launcher:
    return Launcher(path, ("mcp",), "script")


def _basename(command: str) -> str:
    # PureWindowsPath splits on both separators, so a Windows path and a
    # POSIX one both reduce to their basename.
    return PureWindowsPath(command).name.lower()


def _has_dir(command: str) -> bool:
    return "/" in command or "\\" in command


def sibling_script(command: str, name: str) -> str:
    """``name`` in the same directory as ``command``, with its .exe if it had one."""

    cut = max(command.rfind("/"), command.rfind("\\"))
    base = command[cut + 1 :]
    suffix = ".exe" if base.lower().endswith(".exe") else ""
    return command[: cut + 1] + name + suffix


def parse_launcher(entry: object) -> tuple[Launcher, list[str]] | None:
    """(launcher, server args) for an entry of a shape install writes, else None.

    uvx: the first non-option arg is an alice-memory package spec, or it is
    ``alice-memory`` after ``--from <alice-memory spec>``, and the arg right
    after it is ``mcp``. Script: the command's basename is ``alice-memory``
    (or ``.exe``) and the first arg is ``mcp``. Every arg must be a string.
    """

    if not isinstance(entry, Mapping):
        return None
    command = entry.get("command")
    args = entry.get("args")
    if not isinstance(command, str) or not isinstance(args, list):
        return None
    if not all(isinstance(arg, str) for arg in args):
        return None
    name = _basename(command)
    if name in _SCRIPT_NAMES:
        if args[:1] != ["mcp"]:
            return None
        return Launcher(command, ("mcp",), "script"), list(args[1:])
    if name not in _UVX_NAMES:
        return None
    index = 0
    from_spec: str | None = None
    options: list[str] = []
    while index < len(args):
        arg = args[index]
        if arg == "--from":
            if index + 1 >= len(args):
                return None
            from_spec = args[index + 1]
            index += 2
            continue
        if arg.startswith("--from="):
            from_spec = arg.split("=", 1)[1]
            index += 1
            continue
        if arg.startswith("-"):
            step = 2 if "=" not in arg and arg in _UVX_VALUE_OPTIONS else 1
            options.extend(args[index : index + step])
            index += step
            continue
        break
    if index >= len(args):
        return None
    first = args[index]
    if from_spec is not None:
        if not _ALICE_SPEC.match(from_spec) or _basename(first) != MCP_SCRIPT:
            return None
        package = from_spec
    else:
        if not _ALICE_SPEC.match(first):
            return None
        package = first
    if index + 1 >= len(args) or args[index + 1] != "mcp":
        return None
    prefix = tuple(args[: index + 2])
    return Launcher(command, prefix, "uvx", package, tuple(options)), list(args[index + 2 :])


def is_install_shaped_entry(entry: object) -> bool:
    """True for an alice entry of a shape install writes (see parse_launcher)."""

    return parse_launcher(entry) is not None


def last_option(tokens: Sequence[str], option: str) -> str | None:
    """The value of the last ``option X`` or ``option=X``; argparse keeps the last."""

    value: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == option:
            if index + 1 < len(tokens):
                value = tokens[index + 1]
            index += 2
            continue
        if token.startswith(f"{option}="):
            value = token.split("=", 1)[1]
        index += 1
    return value


# --- is a launcher alive here -------------------------------------------------------


def _home() -> Path:
    return Path.home()


def _running_prefix() -> str:
    return sys.prefix


def _expand_home(text: str) -> str:
    if text == "~":
        return str(_home())
    if text.startswith("~/") or text.startswith("~\\"):
        return str(_home() / text[2:])
    return text


def _uv_config_files() -> list[Path]:
    """uv's user and system uv.toml files, in the places uv reads them."""

    env = os.environ
    xdg = env.get("XDG_CONFIG_HOME")
    files = [(Path(xdg) if xdg else _home() / ".config") / "uv" / "uv.toml"]
    if env.get("APPDATA"):
        files.append(Path(env["APPDATA"]) / "uv" / "uv.toml")
    for directory in (env.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(os.pathsep):
        if directory:
            files.append(Path(directory) / "uv" / "uv.toml")
    files.append(Path("/etc/uv/uv.toml"))
    if env.get("PROGRAMDATA"):
        files.append(Path(env["PROGRAMDATA"]) / "uv" / "uv.toml")
    return files


def _uv_config_cache_dirs() -> list[Path]:
    """Every ``cache-dir`` set in a uv.toml install can read."""

    found: list[Path] = []
    for config in _uv_config_files():
        try:
            data = tomllib.loads(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        value = data.get("cache-dir")
        if isinstance(value, str) and value:
            path = Path(_expand_home(value))
            found.append(path if path.is_absolute() else config.parent / path)
    return found


def uv_cache_roots() -> list[Path]:
    env = os.environ
    home = _home()
    roots: list[Path] = []
    if env.get("UV_CACHE_DIR"):
        roots.append(Path(env["UV_CACHE_DIR"]))
    roots.extend(_uv_config_cache_dirs())
    roots.append(home / ".cache" / "uv")
    if env.get("XDG_CACHE_HOME"):
        roots.append(Path(env["XDG_CACHE_HOME"]) / "uv")
    roots.append(home / "Library" / "Caches" / "uv")
    if env.get("LOCALAPPDATA"):
        roots.append(Path(env["LOCALAPPDATA"]) / "uv" / "cache")
    return roots


def _has_cachedir_tag(directory: Path) -> bool:
    try:
        with open(directory / "CACHEDIR.TAG", encoding="utf-8", errors="replace") as handle:
            return handle.read(len(_CACHEDIR_TAG_SIGNATURE)) == _CACHEDIR_TAG_SIGNATURE
    except OSError:
        return False


def in_uv_cache(path: str | os.PathLike[str]) -> bool:
    """True when ``path`` sits in a uv cache, which uv may prune at any time.

    Either under a known cache root, or in uv's own layout: a bucket
    (``archive-vN`` or ``environments-vN``) followed by an id and more path,
    whose parent is a known root, is named ``uv``, or holds uv's
    CACHEDIR.TAG. A user's own ``Archive-V2`` folder, or a project venv
    under ``environments-v3``, is none of those.
    """

    target = Path(os.path.abspath(path))
    roots: set[Path] = set()
    for root in uv_cache_roots():
        roots.update({Path(os.path.abspath(root)), Path(os.path.realpath(root))})
    parts = target.parts
    for index in range(1, len(parts) - 2):
        if _UV_CACHE_BUCKET.match(parts[index].lower()):
            parent = Path(*parts[:index])
            if parent.name.lower() == "uv" or parent in roots or _has_cachedir_tag(parent):
                return True
    return any(target == root or root in target.parents for root in roots)


def _cached(path: str | os.PathLike[str]) -> bool:
    return in_uv_cache(path) or in_uv_cache(os.path.realpath(path))


def _is_executable_file(path: str | os.PathLike[str]) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _found(command: str) -> str | None:
    return command if _has_dir(command) else shutil.which(command)


def launcher_in_uv_cache(launcher: Launcher) -> bool:
    """True when the launcher's program, as found here, sits in a uv cache."""

    found = _found(launcher.command)
    return found is not None and _cached(found)


def launcher_problem(launcher: Launcher, *, needs_hook: bool = True) -> str | None:
    """Why ``launcher`` cannot run here, or None when it can.

    A program in a uv cache is dead whatever else holds: uv may delete it.
    On a host that runs a SessionStart hook (``needs_hook``), a script
    launcher is alive only with its pair, alice-memory-session-start as an
    executable file in the same directory and not in a uv cache, the test
    find_launcher applies. Hosts with no hook need only alice-memory.
    """

    command = launcher.command
    found = _found(command)
    if found is not None and _cached(found):
        return f"{command} is inside a uv cache, which uv may delete"
    if found is None:
        return f"{command} is not on PATH"
    if not _is_executable_file(found):
        return f"{command} is missing or not executable"
    if launcher.kind == "script" and needs_hook:
        sibling = sibling_script(found, SESSION_START_COMMAND)
        if not _is_executable_file(sibling):
            return f"{sibling} is missing or not executable"
        if _cached(sibling):
            return f"{sibling} is inside a uv cache, which uv may delete"
    return None


def hook_script_problem(launcher: Launcher) -> str | None:
    """For a script launcher, why its sibling session-start script cannot run."""

    if launcher.kind != "script":
        return None
    sibling = launcher.hook_argv()[0]
    found = _found(sibling)
    if found is None or not _is_executable_file(found):
        return f"{sibling} does not exist or is not executable"
    return None


# --- which working launcher can install offer ---------------------------------------


@dataclass(frozen=True)
class LauncherSearch:
    """The working launcher install found, or None with the warning to print."""

    launcher: Launcher | None
    warning: str | None
    uvx_on_path: bool


def _interpreter_scripts_dir() -> Path | None:
    path = sysconfig.get_path("scripts")
    return Path(path) if path else None


def _python_bin_dir() -> Path:
    return Path(sys.executable).parent


def _user_scripts_dir() -> Path | None:
    schemes: list[str] = []
    try:
        schemes.append(sysconfig.get_preferred_scheme("user"))
    except (AttributeError, KeyError):
        pass
    schemes.append(f"{os.name}_user")
    for scheme in schemes:
        try:
            path = sysconfig.get_path("scripts", scheme)
        except KeyError:
            continue
        if path:
            return Path(path)
    return None


def _path_script_dir() -> Path | None:
    found = shutil.which(MCP_SCRIPT)
    return Path(os.path.abspath(found)).parent if found else None


def candidate_script_dirs() -> list[Path]:
    """Where to look for the scripts: this install first, then pip --user, PATH last."""

    ordered: list[Path] = []
    for directory in (
        _interpreter_scripts_dir(),
        _python_bin_dir(),
        _user_scripts_dir(),
        _path_script_dir(),
    ):
        if directory is not None and directory not in ordered:
            ordered.append(directory)
    return ordered


def _script_pair(directory: Path) -> tuple[Path, Path] | None:
    for suffix in ("", ".exe"):
        mcp = directory / f"{MCP_SCRIPT}{suffix}"
        hook = directory / f"{SESSION_START_COMMAND}{suffix}"
        if _is_executable_file(mcp) and _is_executable_file(hook):
            return mcp, hook
    return None


def find_launcher() -> LauncherSearch:
    """uvx when it is on PATH; else both installed scripts from one dir; else None.

    A script path, its resolved path, or this Python's prefix inside a uv
    cache is never offered: uv may delete it.
    """

    if shutil.which(MCP_COMMAND) is not None:
        return LauncherSearch(UVX_LAUNCHER, None, True)
    # A prefix is <bucket>/<id>; its bin dir is the "more path" the layout needs.
    if in_uv_cache(os.path.join(_running_prefix(), "bin")):
        return LauncherSearch(None, UV_TEMP_ENV_WARNING, False)
    searched: list[str] = []
    rejected_for_cache = False
    for directory in candidate_script_dirs():
        searched.append(str(directory))
        pair = _script_pair(directory)
        if pair is None:
            continue
        if any(_cached(path) for path in pair):
            rejected_for_cache = True
            continue
        return LauncherSearch(script_launcher(str(pair[0])), None, False)
    if rejected_for_cache:
        return LauncherSearch(None, UV_TEMP_ENV_WARNING, False)
    warning = (
        f"{UVX_MISSING_WARNING_PREFIX} (looked in: {', '.join(searched) or 'nowhere'}). "
        "The entries install writes run uvx, so the hosts cannot start Alice until uv "
        "is installed: https://docs.astral.sh/uv/"
    )
    return LauncherSearch(None, warning, False)


# --- hook commands: write for the host's shell, read back -------------------------------

# The quotes PowerShell reads as quotes: single 2018 2019 201A 201B, double 201C 201D 201E.
_SMART_QUOTES = frozenset(chr(code) for code in (0x2018, 0x2019, 0x201A, 0x201B, 0x201C, 0x201D, 0x201E))
# Characters no single quoting keeps literal in cmd, PowerShell and Git Bash
# alike: quotes and expansions, bash brace expansion and globs ({ } , [ ]),
# line breaks, and the quotes PowerShell treats as quotes.
_WINDOWS_REFUSED = frozenset("\"$`%!'{},[]\n\r") | _SMART_QUOTES
_WINDOWS_QUOTED = frozenset(" \t<>|&;()^")
# A Windows-mode word: quoted and bare pieces with no space between, so
# --data-dir="C:/x y" is one word whose value is --data-dir=C:/x y.
_WINDOWS_WORD = re.compile(r'(?:"[^"]*"|[^\s"])+')
_QUOTED_PIECE = re.compile(r'"[^"]*"')


def format_command(
    argv: Sequence[str], *, windows: bool | None = None, strict: bool = True
) -> tuple[str, str | None]:
    """A command line for ``argv``, and why it cannot be written (None when it can).

    POSIX: ``shlex.join``, which leaves ordinary paths byte-identical.
    Windows, where install cannot know whether Git Bash, PowerShell or cmd
    runs the hook: tokens are written as-is and double-quoted only when they
    hold a space or a shell operator. A token holding a character that no
    one quoting keeps literal in all three (see ``_WINDOWS_REFUSED``), or a
    first token that would need quotes (PowerShell will not run a quoted
    path without ``&``), is refused when ``strict``.
    """

    if windows is None:
        windows = WINDOWS_HOOKS
    if not windows:
        return shlex.join(argv), None
    tokens: list[str] = []
    for index, token in enumerate(argv):
        refused = sorted(set(token) & _WINDOWS_REFUSED)
        if refused and strict:
            shown = " ".join(repr(char) for char in refused)
            return "", (
                f"{token!r} contains {shown}, which cmd, PowerShell and Git Bash do not "
                "all keep literal under one quoting"
            )
        if token == "" or set(token) & _WINDOWS_QUOTED or refused:  # nosec B105 # token is a command-line word; the empty-string test is not a password
            if index == 0 and strict:
                return "", (
                    f"{token!r} would need quotes, and PowerShell does not run a quoted "
                    "path without &"
                )
            tokens.append(f'"{token}"')
        else:
            tokens.append(token)
    return " ".join(tokens), None


def shell_path(path: str, windows: bool | None = None) -> str:
    """``path`` as install writes it into a command: forward slashes on Windows."""

    if windows is None:
        windows = WINDOWS_HOOKS
    return PureWindowsPath(path).as_posix() if windows else path


def hook_command(
    launcher: Launcher, data_dir: str, *, windows: bool | None = None
) -> tuple[str, str | None]:
    """The SessionStart command for ``launcher`` on ``data_dir``, or why not."""

    if windows is None:
        windows = WINDOWS_HOOKS
    argv = launcher.hook_argv()
    if _has_dir(argv[0]):
        argv[0] = shell_path(argv[0], windows)
    argv += ["--data-dir", shell_path(data_dir, windows)]
    return format_command(argv, windows=windows)


def hook_argv(launcher: Launcher, data_dir: str) -> list[str]:
    return [*launcher.hook_argv(), "--data-dir", data_dir]


def split_command(command: str, *, windows: bool | None = None) -> list[str]:
    """A hook command's tokens, read with the rules install writes it with.

    POSIX: ``shlex.split``, falling back to ``str.split`` only when shlex
    cannot read the text (an unclosed quote). Windows: double-quoted words
    and runs of non-space characters, which is how format_command writes
    them there.
    """

    if windows is None:
        windows = WINDOWS_HOOKS
    if windows:
        return [match.group(0).replace('"', "") for match in _WINDOWS_WORD.finditer(command)]
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def is_session_start_command(command: str, *, windows: bool | None = None) -> bool:
    """True when any word of ``command`` runs alice-memory-session-start.

    Judged by basename, on the words as the shell reads them and on the
    plain whitespace-split words, so a hook is recognised as Alice's even
    when its quoting does not read back.
    """

    return any(
        _basename(token) in _SESSION_START_NAMES
        for token in (*split_command(command, windows=windows), *command.split())
    )


@dataclass(frozen=True)
class _Word:
    value: str
    literal: bool  # the shell passes ``value`` exactly as written, no expansion
    start: int = 0  # where the word sits in the command text
    end: int = 0


_POSIX_ENDS = frozenset(";&|\n")


def _posix_words(command: str) -> list[_Word] | None:
    """A POSIX shell's words for the first command in ``command``, and whether each is literal.

    A word is literal when every $, backtick and backslash in it sits inside
    single quotes. Also not literal: an unquoted glob character (* ? [), a
    brace ({), a redirection or parenthesis, or a leading unquoted ~.
    Reading stops at an unquoted ;, &, |, a line break, or a word that
    starts with # (a comment). None when the text cannot be read (an
    unclosed quote).
    """

    words: list[_Word] = []
    index, size = 0, len(command)
    while index < size:
        while index < size and command[index] in " \t":
            index += 1
        if index >= size or command[index] in _POSIX_ENDS or command[index] == "#":
            break
        start, value, literal = index, [], True
        while index < size and command[index] not in " \t" and command[index] not in _POSIX_ENDS:
            char = command[index]
            if char == "'":
                end = command.find("'", index + 1)
                if end < 0:
                    return None
                value.append(command[index + 1 : end])
                index = end + 1
            elif char == '"':
                index += 1
                while True:
                    if index >= size:
                        return None
                    inner = command[index]
                    if inner == '"':
                        index += 1
                        break
                    if inner in "$`\\":
                        literal = False
                    if inner == "\\" and index + 1 < size and command[index + 1] in '$`"\\\n':
                        value.append(command[index + 1])
                        index += 2
                        continue
                    value.append(inner)
                    index += 1
            elif char == "\\":
                literal = False
                value.append(command[index + 1 : index + 2])
                index += 2
            else:
                if char in "$`*?[{<>()" or (char == "~" and index == start):
                    literal = False
                value.append(char)
                index += 1
        words.append(_Word("".join(value), literal, start, index))
    return words


def _windows_words(command: str) -> list[_Word]:
    """Windows-mode words for the first command, and whether each is literal.

    A word is literal with no $, backtick, % or {, and no leading ~. Reading
    stops at a bare word holding ;, & or |, or starting with #.
    """

    words: list[_Word] = []
    for match in _WINDOWS_WORD.finditer(command):
        raw = match.group(0)
        bare = _QUOTED_PIECE.sub("", raw)
        if set(bare) & set(";&|") or raw.startswith("#"):
            break
        token = raw.replace('"', "")
        literal = not (set(token) & set("$`%{")) and not raw.startswith("~")
        words.append(_Word(token, literal, match.start(), match.end()))
    return words


def _data_dir_word(words: Sequence[_Word]) -> tuple[_Word, bool] | None:
    """The last --data-dir value word, and whether it is written ``--data-dir=value``."""

    found: tuple[_Word, bool] | None = None
    for index, word in enumerate(words):
        if word.value == "--data-dir" and index + 1 < len(words):
            found = (words[index + 1], False)
        elif word.value.startswith("--data-dir="):
            found = (word, True)
    return found


def replace_hook_data_dir(
    command: str, data_dir: str, *, windows: bool | None = None
) -> tuple[str | None, str | None]:
    """``command`` with only its last --data-dir value changed, or why it cannot be.

    Every other character of the command, the launcher included, stays as
    written. The new value is quoted for the hook's shell.
    """

    if windows is None:
        windows = WINDOWS_HOOKS
    words = _windows_words(command) if windows else _posix_words(command)
    if words is None:
        return None, "the command cannot be read (an unclosed quote)"
    found = _data_dir_word(words)
    if found is None:
        return None, "the hook has no --data-dir"
    word, equals = found
    value = shell_path(data_dir, windows)
    text = f"--data-dir={value}" if equals else value
    if windows:
        written, problem = format_command(["x", text], windows=True)
        if problem is not None:
            return None, problem
        quoted = written[2:]
    else:
        quoted = shlex.quote(text)
    return command[: word.start] + quoted + command[word.end :], None


@dataclass(frozen=True)
class HookDataDir:
    """What a hook's ``--data-dir`` says, and whether install can rely on it.

    ``raw`` is the value as the shell would pass it, or None without one.
    ``shell`` means the shell does not take the value literally (see
    _posix_words): the host's shell decides the dir, not the text.
    ``trusted`` is False then, and also for a relative path, the docs
    placeholder, or a command install cannot read.
    """

    raw: str | None
    trusted: bool
    shell: bool = False
    reason: str | None = None


def read_hook_data_dir(command: str, *, windows: bool | None = None) -> HookDataDir:
    """Read a hook's ``--data-dir`` the way its shell would, trusting it only when literal."""

    if windows is None:
        windows = WINDOWS_HOOKS
    words = _windows_words(command) if windows else _posix_words(command)
    if words is None:
        raw = last_option(command.split(), "--data-dir")
        return HookDataDir(raw, False, reason="the command cannot be read (an unclosed quote)")
    located = _data_dir_word(words)
    if located is None:
        return HookDataDir(None, False)
    word, equals = located
    found = _Word(word.value.split("=", 1)[1], word.literal) if equals else word
    raw = found.value
    if not found.literal:
        return HookDataDir(raw, False, shell=True, reason="the shell does not read it literally")  # nosec B604 # shell is a dataclass flag (the shell does not read the word literally); nothing is run
    if raw == DOCS_DATA_DIR_PLACEHOLDER:
        return HookDataDir(raw, False, reason="it is the placeholder from the docs")
    absolute = PureWindowsPath(raw).is_absolute() if windows else raw.startswith("/")
    if not absolute:
        return HookDataDir(raw, False, reason="it is a relative path")
    return HookDataDir(raw, True)


__all__ = [
    "CARRY_FLAGS",
    "CARRY_OPTIONS_WITH_VALUE",
    "DOCS_DATA_DIR_PLACEHOLDER",
    "FIRST_SESSION_START_VERSION",
    "HIDDEN",
    "HookDataDir",
    "INDEX_OPTIONS",
    "Launcher",
    "LauncherSearch",
    "MCP_COMMAND",
    "MCP_SCRIPT",
    "PACKAGE",
    "SESSION_START_COMMAND",
    "UVX_LAUNCHER",
    "UVX_MISSING_WARNING_PREFIX",
    "UV_TEMP_ENV_WARNING",
    "WINDOWS_HOOKS",
    "candidate_script_dirs",
    "find_launcher",
    "format_command",
    "hook_command",
    "hook_script_problem",
    "in_uv_cache",
    "is_install_shaped_entry",
    "is_session_start_command",
    "last_option",
    "launcher_in_uv_cache",
    "launcher_problem",
    "mask_text",
    "mask_url",
    "masked_args",
    "shown_hook_words",
    "parse_launcher",
    "read_hook_data_dir",
    "replace_hook_data_dir",
    "script_launcher",
    "session_start_support",
    "shell_path",
    "sibling_script",
    "spec_is_default",
    "split_command",
    "uv_cache_roots",
]
