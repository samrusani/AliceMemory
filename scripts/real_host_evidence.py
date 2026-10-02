#!/usr/bin/env python3
"""Dispatch-only evidence for per-project memory slice S0.

What does a real host hand a SessionStart hook, and what does it hand an MCP
server? The answer decides how a later release finds the folder a session is
about. This script runs the pinned Claude Code and the pinned Codex once each,
from a folder two levels below the root of a scratch git repository, against a
loopback stub API with a made-up key. Nothing paid is called, and no real key
is read.

``run`` builds a fresh home and repository per host, installs a hook command
and a stub MCP server the way ``alice-memory install`` writes them, starts the
host, and writes ``host-evidence.json`` and ``host-evidence.md``. ``hook`` is
the command the host runs for SessionStart. ``mcp`` is the stub MCP server the
host starts. Both record only what is safe to keep.

What is kept and what is not, in one place:

* A path becomes a placeholder that keeps its shape. A folder the run made is
  named (``<launch>``, ``<repo>``, ``<home>``, ``<artifacts>``, ``<scratch>``),
  any other absolute path is ``<abs>``, and every segment after the known part
  is ``<name>`` (a uuid is ``<uuid>``, an extension stays). So
  ``/work/repo/packages/app/x.jsonl`` reads ``<repo>/<name>/<name>/<name>.jsonl``
  or ``<launch>/<name>.jsonl`` and never shows a real name.
* An environment variable keeps its name. Its value is never kept, except for
  a known path variable (``HOME``, ``CLAUDE_PROJECT_DIR`` and the others in
  ``KNOWN_PATH_VARIABLES``) or a variable the host added whose value is a path.
  Those two kinds are reported as a path shape and a relation to the launch
  folder, never as the value.
* A JSON value is kept as it is when it is a number, a boolean or null, or a
  string with no space and at most 64 characters: a word such as ``startup``, an
  email address, or an unspaced run of letters. Three kinds of string are the
  exception. A path becomes a shape, a uuid becomes ``<uuid>``, and a string of
  32 or more characters that mixes letters and digits becomes
  ``<token:N chars>``. A string with a space, or longer than 64 characters,
  becomes ``<text:N chars>``. A string with a slash in it is scrubbed like a line
  of host output (see below). A dictionary key is treated as a string.
* A line of host output (the first stdout and stderr line, an error message, a
  version line) goes through ``scrub_text``: a literal the run knows is secret
  is removed, a URL keeps only its scheme, a path becomes a shape, and any run
  of 32 or more letters, digits and ``_-+/=.`` that mixes letters and digits
  becomes ``<token>``, wherever it sits in the line, JSON with no spaces
  included. The line is cut at 200 characters.
* The shims write only the redacted record. The raw stdin and the raw
  environment are never saved, so the artifact cannot hold them.
* A host process gets the runner's environment minus every name that looks like
  a credential, every ``ANTHROPIC_``, ``CLAUDE``, ``CODEX_``, ``OPENAI_`` and
  ``ALICE_`` name, and the five runner file-command variables (``GITHUB_ENV``,
  ``GITHUB_PATH``, ``GITHUB_OUTPUT``, ``GITHUB_STATE`` and
  ``GITHUB_STEP_SUMMARY``), so a host cannot write to the job summary or reach a
  later step through them.

The script uses the standard library only, except that ``run`` takes the hook
shapes from ``alicebot_api.host_install``, as the other trials do. Nothing here
runs on the owner's machine: ``run`` refuses to start a host outside GitHub
Actions.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import posixpath
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote

SCHEMA_VERSION = 1
HOSTS = ("claude-code", "codex")
PINNED_VERSIONS = {
    "claude-code": "2.1.281 (Claude Code)",
    "codex": "codex-cli 0.158.0",
}
SUMMARY_ENV = "GITHUB_STEP_SUMMARY"

LAUNCH_PROBE = ("ALICE_EVIDENCE_LAUNCH_PROBE", "launch-probe-value")
ENTRY_PROBE = ("ALICE_EVIDENCE_ENTRY_PROBE", "entry-probe-value")
_SERVER_NAME = "alice_evidence"

# Variables whose value is a folder or a file. A value is reported as a shape only
# when the variable is on this list or the host added it.
KNOWN_PATH_VARIABLES = frozenset(
    {
        "HOME",
        "PWD",
        "OLDPWD",
        "TMPDIR",
        "CLAUDE_PROJECT_DIR",
        "CLAUDE_PLUGIN_ROOT",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_ENV_FILE",
        "CODEX_HOME",
        "CODEX_SQLITE_HOME",
        "ALICE_PROJECT_DIR",
        "ALICE_MEMORY_DATA_DIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "INIT_CWD",
    }
)
# Names a real host session uses for its own folders. These segments are kept in a shape.
_STRUCTURAL_SEGMENTS = frozenset({".claude", ".codex", ".git", "projects", "sessions", "plugins", "hooks"})
_STRIPPED_PREFIXES = ("ANTHROPIC_", "CLAUDE", "CODEX_", "OPENAI_", "ALICE_")
# A host is third-party code. It gets no variable that looks like a credential, whatever the runner holds.
_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|APIKEY|API_KEY|PRIVATE|(^|_)KEY($|_)", re.IGNORECASE)
# The runner reads these files between steps: environment, PATH, step outputs, saved state and the job
# summary. A host that could write one could change a later step or the summary. None of them is a
# credential by name, so the name filter above does not catch them.
_RUNNER_FILE_COMMANDS = frozenset(
    {"GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT", "GITHUB_STATE", "GITHUB_STEP_SUMMARY"}
)
_NUMBER_LIMIT = 99

_STDIN_LIMIT = 1024 * 1024
_ROOTS_WAIT_SECONDS = 8.0
_API_WAIT_SECONDS = 45.0
_TOOLS_LIST_GRACE_SECONDS = 1.5
_INITIALIZED_WAIT_SECONDS = 10.0
_CLAUDE_TIMEOUT_SECONDS = 150
_CODEX_TIMEOUT_SECONDS = 240
_LIST_LIMIT = 100
_TEXT_LIMIT = 64
_TOKEN_MIN = 32
_LINE_LIMIT = 200
_ROOTS_REQUEST_ID = "alice-evidence-roots-1"
_NO_LINE = object()

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_PATH_START = re.compile(r"^(?:/|~(?:/|$)|\.{1,2}(?:/|$)|[A-Za-z]:[\\/]|\\\\|file://)")
_PATH_TOKEN = re.compile(r"(?<![\w.:/-])/[^\s\"'<>`)\]},;:/]+(?:/[^\s\"'<>`)\]},;:/]+)*")
_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>`)\]},;]+")
_TOKENISH = re.compile(r"^[A-Za-z0-9_\-+/=.]+$")
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_\-+/=.]{%d,}" % _TOKEN_MIN)
_NAMEISH = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


# --- paths --------------------------------------------------------------------------------


def _forms(path_text: str) -> list[str]:
    """The lexical and the real form of a folder, so /tmp and /private/tmp both match."""

    found: list[str] = []
    candidates = [path_text]
    try:
        candidates.append(os.path.realpath(path_text))
    except OSError:
        pass
    for candidate in candidates:
        norm = posixpath.normpath(candidate.replace("\\", "/"))
        if norm not in found and norm != "/":
            found.append(norm)
    return found


def _under(base: str, path: str) -> bool:
    return path == base or path.startswith(base + "/")


class Context:
    """The folders a run made, by placeholder name, and the environment names it launched with."""

    def __init__(self, folders: Mapping[str, str] | None = None, launch_env_names: Sequence[str] = ()) -> None:
        self.folders = {name: str(path) for name, path in (folders or {}).items()}
        self.launch_env_names = frozenset(launch_env_names)
        self._forms = {name: _forms(path) for name, path in self.folders.items()}
        pairs = [(f"<{name}>", form) for name, forms in self._forms.items() for form in forms]
        # The deepest folder wins, so a path under <launch> is not read as <repo> or <scratch>.
        self.ordered = sorted(pairs, key=lambda pair: (-pair[1].count("/"), -len(pair[1])))

    def forms(self, name: str) -> list[str]:
        return list(self._forms.get(name, []))

    def to_json(self) -> dict[str, object]:
        return {"folders": self.folders, "launch_env_names": sorted(self.launch_env_names)}

    @classmethod
    def load(cls, path: Path) -> Context:
        """The context a run wrote, or an empty one: an unreadable file never stops a shim."""

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            folders = data.get("folders")
            names = data.get("launch_env_names")
            if isinstance(folders, dict) and isinstance(names, list):
                return cls({str(k): str(v) for k, v in folders.items()}, [str(n) for n in names])
        except (OSError, ValueError, AttributeError):
            pass
        return cls()


def looks_like_path(text: str) -> bool:
    return bool(_PATH_START.match(text))


def _segment_shape(segment: str) -> str:
    if segment in (".", ".."):
        return segment
    if _UUID.match(segment):
        return "<uuid>"
    if segment in _STRUCTURAL_SEGMENTS:
        return segment
    extension = ""
    stem = segment
    found = re.match(r"^(.*[^.])(\.[A-Za-z0-9]{1,8})$", segment)
    if found:
        stem, extension = found.group(1), found.group(2)
    if _UUID.match(stem):
        return "<uuid>" + extension
    hidden = "." if segment.startswith(".") else ""
    return hidden + "<name>" + extension


def _tail(rest: str) -> str:
    return "".join("/" + _segment_shape(part) for part in rest.split("/") if part)


def path_shape(text: str, ctx: Context) -> str:
    """A placeholder for a path that keeps its depth, its known root and its extension."""

    if text.startswith("file://"):
        rest = unquote(text[len("file://") :])
        if not rest.startswith("/"):
            rest = "/" + rest.partition("/")[2]
        return "file://" + path_shape(rest, ctx)
    posix = text.replace("\\", "/")
    if posix.startswith("/"):
        norm = posixpath.normpath(posix)
        for placeholder, base in ctx.ordered:
            if _under(base, norm):
                return placeholder + _tail(norm[len(base) :])
        return "<abs>" + _tail(norm)
    if re.match(r"^[A-Za-z]:/", posix):
        return "<drive>" + _tail(posix[2:])
    if posix == "~" or posix.startswith("~/"):
        return "<tilde>" + _tail(posix[1:])
    return "<rel>" + _tail("/" + posixpath.normpath(posix))


def relation(text: str, ctx: Context) -> str:
    """Where a path sits against the launch folder, the repository root and the home folder."""

    posix = text.replace("\\", "/")
    if posix.startswith("file://"):
        posix = unquote(posix[len("file://") :])
    if not posix.startswith("/"):
        return "not_absolute"
    norm = posixpath.normpath(posix)
    launch, repo, home, scratch = (ctx.forms(name) for name in ("launch", "repo", "home", "scratch"))
    if norm in launch:
        return "launch_dir"
    if norm in repo:
        return "repo_root"
    if any(_under(base, norm) for base in launch):
        return "below_launch_dir"
    if any(_under(norm, base) for base in launch) and any(_under(base, norm) for base in repo):
        return "between_launch_dir_and_repo_root"
    if any(_under(base, norm) for base in repo):
        return "elsewhere_in_repo"
    if any(_under(norm, base) for base in repo):
        return "above_repo_root"
    if norm in home:
        return "home"
    if any(_under(base, norm) for base in home):
        return "below_home"
    if any(_under(base, norm) for base in scratch):
        return "elsewhere_in_scratch"
    return "elsewhere"


def git_levels_up(path_text: str, limit: int = 32) -> int | None:
    """How many folders above ``path_text`` the nearest ``.git`` is, or None when none is found."""

    current = posixpath.normpath(path_text.replace("\\", "/"))
    if not current.startswith("/"):
        return None
    for level in range(limit):
        if os.path.lexists(os.path.join(current, ".git")):
            return level
        parent = posixpath.dirname(current)
        if parent == current:
            return None
        current = parent
    return None


# --- redaction ----------------------------------------------------------------------------


def _url_shape(url: str, ctx: Context) -> str:
    """A file URL as a path shape. Any other URL as its scheme only, so a host or a remote never prints."""

    if url.startswith("file://"):
        return path_shape(url, ctx)
    return f"<url:{url.partition(':')[0].lower()}>"


def _token_run(match: re.Match[str]) -> str:
    run = match.group(0)
    mixed = re.search(r"\d", run) and re.search(r"[A-Za-z]", run)
    return "<token>" if mixed else run


def scrub_text(text: str, ctx: Context, literals: Sequence[str] = ()) -> str:
    """One line of host output with every path as a shape, no known secret, and a length cap.

    A token is found as a run of token characters wherever it sits, not as a space-separated word,
    so one inside a JSON line with no spaces (the first line of a Claude Code stream) is caught too.
    """

    scrubbed = text
    for literal in literals:
        if literal:
            scrubbed = scrubbed.replace(literal, "<redacted>")
    scrubbed = _URL.sub(lambda match: _url_shape(match.group(0), ctx), scrubbed)
    scrubbed = _PATH_TOKEN.sub(lambda match: path_shape(match.group(0), ctx), scrubbed)
    scrubbed = _TOKEN_RUN.sub(_token_run, scrubbed)
    if len(scrubbed) > _LINE_LIMIT:
        scrubbed = scrubbed[:_LINE_LIMIT] + "..."
    return scrubbed


def redact_string(text: str, ctx: Context) -> str:
    """Keep a short word, shape a path, and name the size of anything else."""

    if looks_like_path(text):
        return path_shape(text, ctx)
    if _UUID.match(text):
        return "<uuid>"
    if any(char.isspace() for char in text) or len(text) > _TEXT_LIMIT:
        return f"<text:{len(text)} chars>"
    if "/" in text or "\\" in text:
        return scrub_text(text, ctx)
    mixed = re.search(r"\d", text) and re.search(r"[A-Za-z]", text)
    if len(text) >= _TOKEN_MIN and mixed and _TOKENISH.match(text):
        return f"<token:{len(text)} chars>"
    return text


def redact_value(value: object, ctx: Context, depth: int = 0) -> object:
    if depth > 6:
        return "<too deep>"
    if isinstance(value, str):
        return redact_string(value, ctx)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        items = list(value.items())
        out = {redact_string(str(key), ctx): redact_value(item, ctx, depth + 1) for key, item in items[:_LIST_LIMIT]}
        if len(items) > _LIST_LIMIT:
            out["<more>"] = len(items) - _LIST_LIMIT
        return out
    if isinstance(value, list):
        listed = [redact_value(item, ctx, depth + 1) for item in value[:_LIST_LIMIT]]
        if len(value) > _LIST_LIMIT:
            listed.append(f"<{len(value) - _LIST_LIMIT} more>")
        return listed
    return f"<{type(value).__name__}>"


def _path_valued(value: object, ctx: Context, prefix: str = "", depth: int = 0) -> list[str]:
    """Dotted names of every string value that is a path, by name only."""

    found: list[str] = []
    if depth > 4 or not isinstance(value, dict):
        return found
    for key, item in list(value.items())[:_LIST_LIMIT]:
        name = prefix + redact_string(str(key), ctx)
        if isinstance(item, str) and looks_like_path(item):
            found.append(name)
        elif isinstance(item, dict):
            found.extend(_path_valued(item, ctx, name + ".", depth + 1))
    return found


def _safe_name(name: str) -> str:
    return name if _NAMEISH.match(name) else "<odd name>"


def env_record(environ: Mapping[str, str], ctx: Context) -> dict[str, object]:
    """Names of the variables a process was given. Values only as path shapes, for two kinds."""

    names = sorted(environ)
    launch = ctx.launch_env_names
    added = [name for name in names if name not in launch] if launch else []
    dropped = sorted(launch - set(names))
    added_set = set(added)
    path_variables: dict[str, object] = {}
    for name in names:
        value = environ[name]
        if not looks_like_path(value):
            continue
        if name in KNOWN_PATH_VARIABLES:
            basis = "known_path_variable"
        elif name in added_set:
            basis = "added_by_host"
        else:
            continue
        path_variables[_safe_name(name)] = {
            "basis": basis,
            "shape": path_shape(value, ctx),
            "relation": relation(value, ctx),
        }
    return {
        "count": len(names),
        "names": [_safe_name(name) for name in names],
        "added_by_host": [_safe_name(name) for name in added],
        "dropped_from_launch": [_safe_name(name) for name in dropped],
        "path_variables": path_variables,
        "values": "not recorded, except the path shapes above",
    }


def probe_record(environ: Mapping[str, str], *, server: bool) -> dict[str, bool]:
    """Whether a variable planted in the launch environment arrived and, for a server, whether one
    planted in the server's own entry did.

    A hook has no entry of its own, so its record carries the launch probe only.
    """

    record = {"launch_env_forwarded": environ.get(LAUNCH_PROBE[0]) == LAUNCH_PROBE[1]}
    if server:
        record["entry_env_forwarded"] = environ.get(ENTRY_PROBE[0]) == ENTRY_PROBE[1]
    return record


def cwd_record(path_text: str | None, ctx: Context) -> dict[str, object]:
    if path_text is None:
        return {"available": False}
    return {
        "available": True,
        "shape": path_shape(path_text, ctx),
        "relation": relation(path_text, ctx),
        "git_levels_up": git_levels_up(path_text),
    }


def _current_directory() -> str | None:
    try:
        return os.getcwd()
    except OSError:
        return None


# --- the SessionStart hook command ----------------------------------------------------------


def _stdin_record(raw: bytes, ctx: Context, truncated: bool) -> dict[str, object]:
    record: dict[str, object] = {"bytes": len(raw), "truncated": truncated}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        record["valid_json"] = False
        return record
    record["valid_json"] = True
    record["top_level"] = type(payload).__name__
    if not isinstance(payload, dict):
        return record
    record["keys"] = [redact_string(str(key), ctx) for key in list(payload)[:_LIST_LIMIT]]
    record["path_valued_keys"] = _path_valued(payload, ctx)
    record["values"] = redact_value(payload, ctx)
    cwd = payload.get("cwd")
    cwd_key: dict[str, object] = {"present": "cwd" in payload, "type": type(cwd).__name__}
    if isinstance(cwd, str):
        cwd_key["absolute"] = relation(cwd, ctx) != "not_absolute"
        if looks_like_path(cwd):
            cwd_key["shape"] = path_shape(cwd, ctx)
            cwd_key["relation"] = relation(cwd, ctx)
    record["cwd_key"] = cwd_key
    return record


def _open_numbered(artifacts: Path, stem: str) -> tuple[Path, int] | None:
    """Create the next free ``<stem>-N.json`` and return it open, or None when none can be made.

    The create is exclusive, so two processes that start together get two numbers and neither
    record replaces the other.
    """

    try:
        artifacts.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    for number in range(1, _NUMBER_LIMIT + 1):
        path = artifacts / f"{stem}-{number}.json"
        try:
            return path, os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        except OSError:
            return None
    return None


def _write_numbered(artifacts: Path, stem: str, record: object) -> None:
    claimed = _open_numbered(artifacts, stem)
    if claimed is None:
        return
    with os.fdopen(claimed[1], "w", encoding="utf-8") as handle:
        handle.write(json.dumps(record, indent=2) + "\n")


def hook_main(host: str, artifacts: Path, context_path: Path) -> int:
    """Record what the host sent a SessionStart hook, redacted. Print nothing. Always exit 0."""

    ctx = Context.load(context_path)
    record: dict[str, object] = {"host": host}
    try:
        raw = sys.stdin.buffer.read(_STDIN_LIMIT + 1)
        truncated = len(raw) > _STDIN_LIMIT
        if truncated:
            while sys.stdin.buffer.read(65536):
                pass
        record["stdin"] = _stdin_record(raw[:_STDIN_LIMIT], ctx, truncated)
        record["cwd"] = cwd_record(_current_directory(), ctx)
        record["env"] = env_record(os.environ, ctx)
        record["probes"] = probe_record(os.environ, server=False)
    except Exception as problem:  # noqa: BLE001 - a hook must never break the host
        record["error"] = type(problem).__name__
    try:
        _write_numbered(artifacts, f"{host}-hook", record)
    except OSError:
        pass
    return 0


# --- the stub MCP server --------------------------------------------------------------------

_EMPTY_LISTS = {
    "resources/list": "resources",
    "resources/templates/list": "resourceTemplates",
    "prompts/list": "prompts",
}


def _capability_tree(value: object, ctx: Context, depth: int = 0) -> object:
    """Capability names with their flags. A string becomes its size, never its text."""

    if depth > 4:
        return "<too deep>"
    if isinstance(value, dict):
        return {redact_string(str(k), ctx): _capability_tree(v, ctx, depth + 1) for k, v in list(value.items())[:50]}
    if isinstance(value, list):
        return f"<list of {len(value)}>"
    if isinstance(value, str):
        return f"<text:{len(value)} chars>"
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    return f"<{type(value).__name__}>"


def _short(value: object, ctx: Context) -> object:
    return redact_string(value, ctx) if isinstance(value, str) else value


def initialize_record(params: object, ctx: Context) -> dict[str, object]:
    """What an ``initialize`` request carries: the client, the version, and every capability."""

    body = params if isinstance(params, dict) else {}
    capabilities = body.get("capabilities") if isinstance(body.get("capabilities"), dict) else {}
    info = body.get("clientInfo") if isinstance(body.get("clientInfo"), dict) else {}
    roots = capabilities.get("roots")
    elicitation = capabilities.get("elicitation")
    return {
        "received": True,
        "params_keys": [redact_string(str(key), ctx) for key in list(body)[:_LIST_LIMIT]],
        "protocol_version": _short(body.get("protocolVersion"), ctx),
        "client_info": {
            "name": _short(info.get("name"), ctx),
            "version": _short(info.get("version"), ctx),
            "title": _short(info.get("title"), ctx),
        },
        "capability_names": [redact_string(str(key), ctx) for key in list(capabilities)[:_LIST_LIMIT]],
        "capabilities": _capability_tree(capabilities, ctx),
        "declares": {
            "roots": "roots" in capabilities,
            "roots_list_changed": roots.get("listChanged") if isinstance(roots, dict) else None,
            "elicitation": "elicitation" in capabilities,
            "elicitation_modes": (
                sorted(redact_string(str(key), ctx) for key in elicitation) if isinstance(elicitation, dict) else []
            ),
            "sampling": "sampling" in capabilities,
        },
    }


def roots_result_record(result: object, ctx: Context) -> dict[str, object]:
    """What a ``roots/list`` answer holds: each root as a shape and its place against the launch folder."""

    body = result if isinstance(result, dict) else {}
    roots = body.get("roots") if isinstance(body.get("roots"), list) else []
    items: list[dict[str, object]] = []
    for root in roots[:20]:
        item = root if isinstance(root, dict) else {}
        uri = item.get("uri")
        name = item.get("name")
        entry: dict[str, object] = {"uri_type": type(uri).__name__}
        if isinstance(uri, str):
            scheme = uri.partition(":")[0] if ":" in uri else ""
            entry["uri_scheme"] = redact_string(scheme, ctx) if scheme else ""
            if uri.startswith("file://"):
                entry["uri_shape"] = path_shape(uri, ctx)
                entry["relation"] = relation(uri, ctx)
        entry["name_kind"] = (
            "absent"
            if name is None
            else "basename"
            if isinstance(name, str) and isinstance(uri, str) and uri.rstrip("/").endswith("/" + name)
            else "other"
        )
        items.append(entry)
    return {"root_count": len(roots), "roots": items, "result_keys": [redact_string(str(k), ctx) for k in body]}


class _McpStub:
    """A minimal stdio MCP server that records what its client sends and asks for the client's roots."""

    def __init__(self, host: str, artifacts: Path, ctx: Context, roots_wait: float, out: BinaryIO) -> None:
        self.ctx = ctx
        self.roots_wait = roots_wait
        self.out = out
        # One numbered record per server start, like the hook records, so a second start (a retry or a
        # reconnect) never replaces the first. With no number free the server still serves and records nothing.
        claimed = _open_numbered(artifacts, f"{host}-mcp")
        self.path: Path | None = None
        if claimed is not None:
            self.path = claimed[0]
            os.close(claimed[1])
        self.record: dict[str, Any] = {
            "host": host,
            "started": True,
            "complete": False,
            "cwd": cwd_record(_current_directory(), ctx),
            "env": env_record(os.environ, ctx),
            "probes": probe_record(os.environ, server=True),
            "initialize": {"received": False},
            "client_requests": [],
            "roots_list": {"sent": False, "outcome": "not_sent"},
            "stdin_closed": False,
        }
        self.initialize_at: float | None = None
        self.initialized_at: float | None = None
        self.roots_sent_at: float | None = None
        self.saw_tools_list = False

    # --- output ---

    def save(self) -> None:
        if self.path is None:
            return
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(self.record, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    def _send(self, message: dict[str, object]) -> None:
        self.out.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        self.out.flush()

    def _reply(self, ident: object, result: dict[str, object]) -> None:
        self._send({"jsonrpc": "2.0", "id": ident, "result": result})

    def _error(self, ident: object, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}})

    # --- input ---

    def _note(self, method: str) -> None:
        names: list[str] = self.record["client_requests"]
        if len(names) < _LIST_LIMIT:
            names.append(redact_string(method, self.ctx))

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        ident = message.get("id")
        if isinstance(method, str):
            self._note(method)
            if method == "initialize":
                params = message.get("params")
                self.record["initialize"] = initialize_record(params, self.ctx)
                self.initialize_at = time.monotonic()
                version = params.get("protocolVersion") if isinstance(params, dict) else None
                self._reply(
                    ident,
                    {
                        "protocolVersion": version if isinstance(version, str) else "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "alice-host-evidence", "version": "0"},
                    },
                )
            elif method == "notifications/initialized":
                self.initialized_at = time.monotonic()
            elif method == "tools/list":
                self.saw_tools_list = True
                self._reply(ident, {"tools": []})
            elif method == "ping":
                self._reply(ident, {})
            elif method in _EMPTY_LISTS:
                self._reply(ident, {_EMPTY_LISTS[method]: []})
            elif ident is not None:
                self._error(ident, -32601, "method not found")
        elif ident == _ROOTS_REQUEST_ID and ("result" in message or "error" in message):
            self._roots_answer(message)
        self.save()

    def _roots_answer(self, message: dict[str, Any]) -> None:
        roots: dict[str, Any] = self.record["roots_list"]
        if isinstance(message.get("error"), dict):
            error = message["error"]
            code = error.get("code")
            roots["outcome"] = "error"
            roots["error"] = {
                "code": code if isinstance(code, int) else None,
                "message": scrub_text(str(error.get("message", "")), self.ctx)[:120],
            }
        else:
            roots["outcome"] = "answered"
            roots.update(roots_result_record(message.get("result"), self.ctx))
        self._finish_roots()

    def _finish_roots(self) -> None:
        roots: dict[str, Any] = self.record["roots_list"]
        if self.roots_sent_at is not None:
            roots["waited_seconds"] = round(time.monotonic() - self.roots_sent_at, 2)
        self.record["complete"] = True

    # --- timers ---

    def tick(self) -> None:
        now = time.monotonic()
        roots: dict[str, Any] = self.record["roots_list"]
        if self.record["complete"]:
            return
        if self.initialized_at is not None and self.roots_sent_at is None:
            if self.saw_tools_list or now - self.initialized_at >= _TOOLS_LIST_GRACE_SECONDS:
                # The probe goes out whether or not the client declared roots: the question is
                # what the client does with it. The record keeps the declaration beside the outcome.
                self._send({"jsonrpc": "2.0", "id": _ROOTS_REQUEST_ID, "method": "roots/list"})
                self.roots_sent_at = now
                roots["sent"] = True
                roots["outcome"] = "no_reply"
                self.save()
        elif self.roots_sent_at is not None and now - self.roots_sent_at >= self.roots_wait:
            roots["outcome"] = "no_reply"
            self._finish_roots()
            self.save()
        elif (
            self.initialized_at is None
            and self.initialize_at is not None
            and now - self.initialize_at >= _INITIALIZED_WAIT_SECONDS
        ):
            roots["reason"] = "the client never sent notifications/initialized"
            self.record["complete"] = True
            self.save()

    def serve(self, stdin: BinaryIO) -> None:
        self.save()
        lines: queue.Queue[bytes | None] = queue.Queue()

        def pump() -> None:
            for line in stdin:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        while True:
            try:
                line: object = lines.get(timeout=0.1)
            except queue.Empty:
                line = _NO_LINE
            if line is None:
                self.record["stdin_closed"] = True
                if not self.record["complete"]:
                    roots: dict[str, Any] = self.record["roots_list"]
                    roots["reason"] = "the client closed the connection first"
                    self.record["complete"] = True
                self.save()
                return
            if isinstance(line, bytes):
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    message = None
                if isinstance(message, dict):
                    self.handle(message)
            self.tick()


def mcp_main(
    host: str,
    artifacts: Path,
    context_path: Path,
    roots_wait: float = _ROOTS_WAIT_SECONDS,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> int:
    ctx = Context.load(context_path)
    artifacts.mkdir(parents=True, exist_ok=True)
    stub = _McpStub(host, artifacts, ctx, roots_wait, stdout if stdout is not None else sys.stdout.buffer)
    try:
        stub.serve(stdin if stdin is not None else sys.stdin.buffer)
    except Exception as problem:  # noqa: BLE001 - record the failure, then end the server
        stub.record["error"] = type(problem).__name__
        stub.record["complete"] = True
        try:
            stub.save()
        except OSError:
            pass
    return 0


# --- loopback API stubs ---------------------------------------------------------------------


def _sse(events: list[dict[str, Any]]) -> str:
    """Server-sent events the way the Codex real-host test writes them."""

    out: list[str] = []
    for event in events:
        out.append(f"event: {event['type']}\n")
        if len(event) == 1:
            out.append("\n")
        else:
            out.append(f"data: {json.dumps(event, separators=(',', ':'))}\n\n")
    return "".join(out)


def _codex_reply(index: int) -> bytes:
    response_id = f"resp-{index}"
    return _sse(
        [
            {"type": "response.created", "response": {"id": response_id}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "id": f"msg-{index}",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "usage": {
                        "input_tokens": 0,
                        "input_tokens_details": None,
                        "output_tokens": 0,
                        "output_tokens_details": None,
                        "total_tokens": 0,
                    },
                },
            },
        ]
    ).encode("utf-8")


_ANTHROPIC_ERROR = (
    b'{"type":"error","error":{"type":"invalid_request_error","message":"alice test stub"}}'
)


class _LoopbackApi:
    """A stand-in for the model API, on 127.0.0.1 only. It holds its first reply until ``gate`` is true.

    The hold keeps the host alive until the stub MCP server has finished its roots probe, so the
    host does not exit and close the server first. ``mode`` is ``anthropic`` (a 400 for every call,
    which ends a Claude Code run) or ``responses`` (one assistant message, as the Codex test does).
    """

    def __init__(self, mode: str, gate: Callable[[], bool], wait_seconds: float) -> None:
        self.mode = mode
        self.requests: list[tuple[str, str]] = []
        self._gate = gate
        self._wait = wait_seconds
        self._deadline: float | None = None
        self._lock = threading.Lock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1" if mode == "responses" else "HTTP/1.0"

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def _drain(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)

            def _answer(self, method: str) -> None:
                self._drain()
                owner.requests.append((method, self.path.split("?", 1)[0]))
                if mode == "responses":
                    if method != "POST" or not self.path.split("?", 1)[0].endswith("/responses"):
                        self.send_error(404)
                        return
                    owner._hold()
                    body = _codex_reply(len(owner.requests))
                    status, kind, close = 200, "text/event-stream", True
                else:
                    if method == "POST":
                        owner._hold()
                    body = _ANTHROPIC_ERROR
                    status, kind, close = 400, "application/json", False
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", kind)
                    if close:
                        self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    if close:
                        self.send_header("Connection", "close")
                        self.close_connection = True
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

            def do_POST(self) -> None:  # noqa: N802
                self._answer("POST")

            def do_GET(self) -> None:  # noqa: N802
                self._answer("GET")

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def _hold(self) -> None:
        with self._lock:
            if self._deadline is None:
                self._deadline = time.monotonic() + self._wait
            deadline = self._deadline
        while time.monotonic() < deadline and not self._gate():
            time.sleep(0.1)

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=10)


# --- running a host -------------------------------------------------------------------------


def _numbered_paths(artifacts: Path, host: str, kind: str) -> list[Path]:
    """The ``<host>-<kind>-N.json`` records, in the order the processes started."""

    def number(path: Path) -> int:
        found = re.search(r"-(\d+)\.json$", path.name)
        return int(found.group(1)) if found else 0

    return sorted(artifacts.glob(f"{host}-{kind}-*.json"), key=number)


def _mcp_complete(artifacts: Path, host: str) -> bool:
    """True once a server has started and every server start has finished its roots probe.

    A record that cannot be read yet counts as unfinished, so the host is not released early.
    """

    paths = _numbered_paths(artifacts, host, "mcp")
    if not paths:
        return False
    for path in paths:
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("complete") is not True:
                return False
        except (OSError, ValueError, AttributeError):
            return False
    return True


def _make_repo(repo: Path) -> None:
    """A scratch git repository whose root is two folders above the folder the host starts in."""

    repo.mkdir(parents=True, exist_ok=True)
    ok = False
    try:
        done = subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, check=False, timeout=60)
        ok = done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        (repo / ".git" / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (repo / ".git" / "objects").mkdir(parents=True, exist_ok=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (repo / ".git" / "config").write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")
    (repo / "packages" / "app").mkdir(parents=True, exist_ok=True)


def host_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The runner's environment as a third-party host may see it.

    Dropped: every name that looks like a credential, every name that starts with a host or Alice
    prefix (the run sets the few it needs itself), and the runner's file-command variables.
    """

    return {
        name: value
        for name, value in environ.items()
        if not name.startswith(_STRIPPED_PREFIXES)
        and name.upper() not in _RUNNER_FILE_COMMANDS
        and not _SECRET_NAME.search(name)
    }


class _Sandbox:
    """One host's home, scratch repository, launch folder and context file."""

    def __init__(self, host: str, artifacts: Path, root: Path) -> None:
        self.host = host
        self.artifacts = artifacts
        self.root = root
        self.home = root / "home"
        self.repo = root / "work" / "repo"
        self.launch = self.repo / "packages" / "app"
        self.context_path = root / "context.json"
        self.home.mkdir(parents=True, exist_ok=True)
        _make_repo(self.repo)

    def base_env(self) -> dict[str, str]:
        """The runner's environment as ``host_environment`` leaves it, plus the home and the probe."""

        env = host_environment(os.environ)
        env["HOME"] = str(self.home)
        env["PWD"] = str(self.launch)
        env[LAUNCH_PROBE[0]] = LAUNCH_PROBE[1]
        return env

    def context(self, env: Mapping[str, str] | None = None) -> Context:
        return Context(
            {
                "launch": str(self.launch),
                "repo": str(self.repo),
                "home": str(self.home),
                "artifacts": str(self.artifacts),
                "scratch": str(self.root),
            },
            sorted(env or {}),
        )

    def write_context(self, env: Mapping[str, str]) -> Context:
        ctx = self.context(env)
        self.context_path.write_text(json.dumps(ctx.to_json()), encoding="utf-8")
        return ctx

    def command(self, *words: str) -> list[str]:
        """argv for this script: the interpreter, the script, a subcommand and its arguments."""

        return [sys.executable, str(Path(__file__).resolve()), *words]

    def hook_command(self) -> str:
        return shlex.join(self.command("hook", self.host, str(self.artifacts), str(self.context_path)))

    def mcp_argv(self, roots_wait: float) -> list[str]:
        return self.command("mcp", self.host, str(self.artifacts), str(self.context_path), "--roots-wait", str(roots_wait))


def _toml(text: str) -> str:
    return json.dumps(text)


def _run_process(
    argv: list[str], cwd: Path, env: Mapping[str, str], timeout: int
) -> tuple[int | str, str, str, float]:
    started = time.perf_counter()
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as expired:
        out = expired.stdout if isinstance(expired.stdout, bytes) else b""
        err = expired.stderr if isinstance(expired.stderr, bytes) else b""
        return (
            "timeout",
            out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
            (time.perf_counter() - started) * 1000,
        )
    return (
        done.returncode,
        done.stdout.decode("utf-8", errors="replace"),
        done.stderr.decode("utf-8", errors="replace"),
        (time.perf_counter() - started) * 1000,
    )


def _first_line(text: str, ctx: Context, literals: Sequence[str]) -> str:
    for line in text.splitlines():
        if line.strip():
            return scrub_text(line.strip(), ctx, literals)
    return ""


def _stream_events(stdout: str, ctx: Context) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    """The ``system/init`` event of a Claude Code stream, redacted, and what each hook reported."""

    init: dict[str, object] | None = None
    hooks: list[dict[str, object]] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind, subtype = str(event.get("type", "")), str(event.get("subtype", ""))
        if kind == "system" and subtype == "init" and init is None:
            servers = event.get("mcp_servers") if isinstance(event.get("mcp_servers"), list) else []
            tools = event.get("tools") if isinstance(event.get("tools"), list) else []
            cwd = event.get("cwd")
            init = {
                "keys": [redact_string(str(key), ctx) for key in event],
                "cwd": cwd_record(cwd, ctx) if isinstance(cwd, str) else {"available": False},
                "mcp_servers": [
                    {
                        "name": _short(item.get("name"), ctx),
                        "status": _short(item.get("status"), ctx),
                    }
                    for item in servers[:20]
                    if isinstance(item, dict)
                ],
                "tool_count": len(tools),
                "mcp_tool_count": sum(1 for tool in tools if isinstance(tool, str) and tool.startswith("mcp__")),
            }
        elif "hook" in f"{kind} {subtype}".lower() and len(hooks) < 50:
            exit_code = event.get("exit_code")
            hooks.append(
                {
                    "type": _short(kind, ctx),
                    "subtype": _short(subtype, ctx),
                    "outcome": _short(event.get("outcome"), ctx),
                    "exit_code": exit_code if isinstance(exit_code, int) else None,
                }
            )
    return init, hooks


def _load_numbered(artifacts: Path, host: str, kind: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in _numbered_paths(artifacts, host, kind):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(loaded, dict):
            records.append(loaded)
    return records


def _collect(host: str, artifacts: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every hook record and every MCP server record the host's processes left, in start order."""

    return _load_numbered(artifacts, host, "hook"), _load_numbered(artifacts, host, "mcp")


def _settle(artifacts: Path, host: str, seconds: float = 5.0) -> None:
    """After the host exits, give the stub server a moment to write its last record."""

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and _numbered_paths(artifacts, host, "mcp"):
        if _mcp_complete(artifacts, host):
            return
        time.sleep(0.1)


def _result_shell(host: str, version: str, sandbox: _Sandbox) -> dict[str, Any]:
    return {
        "host": host,
        # The line a host prints for --version is host output like any other: scrubbed before it is kept.
        "version": scrub_text(version, sandbox.context()),
        "pinned": version == PINNED_VERSIONS[host],
        "launched_in": {
            "placeholder": "<launch>",
            "git_levels_up": git_levels_up(str(sandbox.launch)),
        },
        "run": None,
        "hook": {"fired": 0, "records": []},
        "mcp": {"starts": 0, "records": []},
        "problems": [],
    }


def _finish_host(
    result: dict[str, Any],
    sandbox: _Sandbox,
    ctx: Context,
    api: _LoopbackApi,
    outcome: tuple[int | str, str, str, float],
    literals: Sequence[str],
    init: dict[str, object] | None = None,
    hook_events: list[dict[str, object]] | None = None,
) -> None:
    exit_code, stdout, stderr, wall_ms = outcome
    _settle(sandbox.artifacts, sandbox.host)
    hooks, servers = _collect(sandbox.host, sandbox.artifacts)
    result["run"] = {
        "exit_code": exit_code,
        "wall_ms": int(wall_ms),
        "first_stdout_line": _first_line(stdout, ctx, literals),
        "first_stderr_line": _first_line(stderr, ctx, literals),
        "stub_requests": [[method, path] for method, path in api.requests],
        "init_event": init,
        "hook_events": hook_events,
    }
    result["hook"] = {"fired": len(hooks), "records": hooks}
    result["mcp"] = {"starts": len(servers), "records": servers}
    problems: list[str] = result["problems"]
    if not hooks:
        problems.append("the SessionStart hook did not fire")
    if not servers:
        problems.append("the MCP server was never started")
    for number, server in enumerate(servers, start=1):
        # Every start is held to the same bar, and says which one missed it when there is more than one.
        which = f" (start {number} of {len(servers)})" if len(servers) > 1 else ""
        if not server.get("initialize", {}).get("received"):
            problems.append("the MCP server never received initialize" + which)
        if server.get("complete") is not True:
            problems.append("the roots/list probe did not finish" + which)


def _which(name: str, env: Mapping[str, str]) -> str | None:
    return shutil.which(name, path=env.get("PATH"))


def _version(exe: str, env: Mapping[str, str], cwd: Path) -> str:
    code, stdout, _stderr, _ms = _run_process([exe, "--version"], cwd, env, 60)
    return stdout.strip().splitlines()[0] if code == 0 and stdout.strip() else ""


def _run_claude(
    artifacts: Path, root: Path, *, roots_wait: float, api_wait: float, executable: str | None
) -> dict[str, Any]:
    from alicebot_api.host_install import claude_code_session_start_group

    sandbox = _Sandbox("claude-code", artifacts, root)
    env = sandbox.base_env()
    exe = executable or _which("claude", env)
    if exe is None:
        shell = _result_shell("claude-code", "", sandbox)
        shell["problems"].append("claude is not on PATH")
        return shell
    version = _version(exe, {**env, "DISABLE_AUTOUPDATER": "1"}, sandbox.root)
    result = _result_shell("claude-code", version, sandbox)
    if not result["pinned"]:
        result["problems"].append(f"claude is not the pinned version {PINNED_VERSIONS['claude-code']}")
        return result
    api = _LoopbackApi("anthropic", lambda: _mcp_complete(artifacts, "claude-code"), api_wait)
    try:
        fake_key = "alice-test-" + secrets.token_hex(8)
        env.update(
            {
                "ANTHROPIC_BASE_URL": api.base_url,
                "ANTHROPIC_API_KEY": fake_key,
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
            }
        )
        (sandbox.home / ".claude").mkdir(parents=True, exist_ok=True)
        settings = {"hooks": {"SessionStart": [claude_code_session_start_group(sandbox.hook_command())]}}
        (sandbox.home / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        argv = sandbox.mcp_argv(roots_wait)
        servers = {
            _SERVER_NAME.replace("_", "-"): {
                "type": "stdio",
                "command": argv[0],
                "args": argv[1:],
                "env": {ENTRY_PROBE[0]: ENTRY_PROBE[1]},
            }
        }
        (sandbox.home / ".claude.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        ctx = sandbox.write_context(env)
        outcome = _run_process(
            [exe, "-p", "--output-format", "stream-json", "--verbose", "ok"],
            sandbox.launch,
            env,
            _CLAUDE_TIMEOUT_SECONDS,
        )
        init, hook_events = _stream_events(outcome[1], ctx)
        _finish_host(result, sandbox, ctx, api, outcome, [fake_key], init, hook_events)
    finally:
        api.close()
    return result


def codex_trust_hash(command: str, *, timeout: int | None = None, limit: int | None = None) -> str:
    """The hash Codex keeps for a command handler with no matcher and no status message.

    A copy of ``trust_hash`` in ``tests/unit/test_codex_hook_real_host.py``, which the real Codex
    run there checks against ``hooks/list``. A unit test keeps the two equal.
    """

    handler: dict[str, object] = {
        "type": "command",
        "command": command,
        "timeout": 600 if timeout is None else max(timeout, 1),
        "async": False,
    }
    if limit is not None and limit != 2500:
        handler["additionalContextLimit"] = limit
    identity = {"event_name": "session_start", "hooks": [handler]}
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _run_codex(
    artifacts: Path, root: Path, *, roots_wait: float, api_wait: float, executable: str | None
) -> dict[str, Any]:
    from alicebot_api.host_install import codex_session_start_group

    sandbox = _Sandbox("codex", artifacts, root)
    env = sandbox.base_env()
    exe = executable or _which("codex", env)
    if exe is None:
        shell = _result_shell("codex", "", sandbox)
        shell["problems"].append("codex is not on PATH")
        return shell
    version = _version(exe, env, sandbox.root)
    result = _result_shell("codex", version, sandbox)
    if not result["pinned"]:
        result["problems"].append(f"codex is not the pinned version {PINNED_VERSIONS['codex']}")
        return result
    api = _LoopbackApi("responses", lambda: _mcp_complete(artifacts, "codex"), api_wait)
    try:
        fake_key = "dummy-" + secrets.token_hex(8)
        codex_home = sandbox.home / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        (sandbox.root / "sqlite").mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "CODEX_HOME": str(codex_home),
                "CODEX_API_KEY": fake_key,
                "CODEX_SQLITE_HOME": str(sandbox.root / "sqlite"),
            }
        )
        group = codex_session_start_group(sandbox.hook_command())
        hooks_path = codex_home / "hooks.json"
        hooks_path.write_text(json.dumps({"hooks": {"SessionStart": [group]}}), encoding="utf-8")
        handlers = group["hooks"]
        assert isinstance(handlers, list) and isinstance(handlers[0], dict)
        handler: dict[str, Any] = handlers[0]
        trusted = codex_trust_hash(
            str(handler["command"]), timeout=handler.get("timeout"), limit=handler.get("additionalContextLimit")
        )
        argv = sandbox.mcp_argv(roots_wait)
        key = f"{hooks_path.resolve()}:session_start:0:0"
        (codex_home / "config.toml").write_text(
            "check_for_update_on_startup = false\n\n"
            f"[mcp_servers.{_SERVER_NAME}]\n"
            f"command = {_toml(argv[0])}\n"
            f"args = [{', '.join(_toml(item) for item in argv[1:])}]\n\n"
            f"[mcp_servers.{_SERVER_NAME}.env]\n"
            f"{ENTRY_PROBE[0]} = {_toml(ENTRY_PROBE[1])}\n\n"
            f"[hooks.state.{_toml(key)}]\n"
            f"trusted_hash = {_toml(trusted)}\n",
            encoding="utf-8",
        )
        ctx = sandbox.write_context(env)
        outcome = _run_process(
            [
                exe,
                "exec",
                "--skip-git-repo-check",
                "-m",
                "gpt-5.5",
                "-c",
                f'openai_base_url="{api.base_url}/v1"',
                "-c",
                'otel.metrics_exporter="none"',
                # The server is optional, and Codex waits a second for an optional one. Python is slower.
                "-c",
                "mcp_optional_startup_grace_ms=60000",
                "Say ok.",
            ],
            sandbox.launch,
            env,
            _CODEX_TIMEOUT_SECONDS,
        )
        _finish_host(result, sandbox, ctx, api, outcome, [fake_key])
    finally:
        api.close()
    return result


_RUNNERS = {"claude-code": _run_claude, "codex": _run_codex}


# --- the report -----------------------------------------------------------------------------


def _code(value: object) -> str:
    text = str(value).replace("`", "'").replace("|", "/").replace("\n", " ")
    return f"`{text}`"


def _yes(flag: object) -> str:
    return "yes" if flag is True else "no" if flag is False else "unknown"


def _names(names: object) -> str:
    return ", ".join(_code(name) for name in names) if isinstance(names, list) and names else "none"


def _env_lines(env: object) -> list[str]:
    if not isinstance(env, dict):
        return ["- Environment: not recorded."]
    dropped = env.get("dropped_from_launch") or []
    shown = _names(dropped) if len(dropped) <= 12 else f"{len(dropped)} names, listed in host-evidence.json"
    lines = [
        f"- Environment variable names: {env.get('count')} set. Values are not recorded.",
        f"- Added by the host, not in the launch environment: {_names(env.get('added_by_host'))}",
        f"- In the launch environment and not passed on: {shown}",
    ]
    variables = env.get("path_variables")
    if isinstance(variables, dict) and variables:
        lines.append("- Path variables, as shapes:")
        for name, info in variables.items():
            if isinstance(info, dict):
                lines.append(
                    f"  - {_code(name)} = {_code(info.get('shape'))}, {info.get('relation')}, {info.get('basis')}"
                )
    else:
        lines.append("- Path variables, as shapes: none.")
    return lines


def _cwd_text(cwd: object) -> str:
    if not isinstance(cwd, dict) or not cwd.get("available"):
        return "not available"
    levels = cwd.get("git_levels_up")
    where = f"git root {levels} level(s) up" if isinstance(levels, int) else "no git root above it"
    return f"{_code(cwd.get('shape'))}, {cwd.get('relation')}, {where}"


def _hook_lines(info: dict[str, Any]) -> list[str]:
    hook = info["hook"]
    lines = [f"#### SessionStart hook, fired {hook['fired']} time(s)", ""]
    if not hook["records"]:
        return [*lines, "The hook did not run, so there is nothing to record.", ""]
    first = hook["records"][0]
    stdin = first.get("stdin") if isinstance(first.get("stdin"), dict) else {}
    values = stdin.get("values")
    lines.append(f"stdin: {stdin.get('bytes')} bytes, valid JSON: {_yes(stdin.get('valid_json'))}.")
    if isinstance(values, dict) and values:
        lines += ["", "| stdin key | value, paths as placeholders |", "| --- | --- |"]
        for key, value in values.items():
            shown = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
            lines.append(f"| {_code(key)} | {_code(shown)} |")
    cwd_key = stdin.get("cwd_key") if isinstance(stdin.get("cwd_key"), dict) else {}
    lines.append("")
    lines.append(f"- Path-valued stdin keys: {_names(stdin.get('path_valued_keys'))}")
    if cwd_key.get("present"):
        lines.append(
            f"- `cwd` key: present, absolute: {_yes(cwd_key.get('absolute'))}, "
            f"{_code(cwd_key.get('shape'))}, {cwd_key.get('relation')}"
        )
    else:
        lines.append("- `cwd` key: absent.")
    lines.append(f"- Hook process working folder: {_cwd_text(first.get('cwd'))}")
    lines += _env_lines(first.get("env"))
    probes = first.get("probes") if isinstance(first.get("probes"), dict) else {}
    lines.append(
        f"- A variable set in the launch environment reached the hook: {_yes(probes.get('launch_env_forwarded'))}"
    )
    lines.append("")
    return lines


def _mcp_lines(info: dict[str, Any]) -> list[str]:
    records = [record for record in info["mcp"]["records"] if isinstance(record, dict)]
    lines = [f"#### MCP server, started {info['mcp']['starts']} time(s)", ""]
    if not records:
        return [*lines, "The server was never started, so there is nothing to record.", ""]
    for number, record in enumerate(records, start=1):
        if len(records) > 1:
            lines += [f"**Start {number} of {len(records)}**", ""]
        lines += _mcp_record_lines(record)
    return lines


def _mcp_record_lines(record: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    init = record.get("initialize") if isinstance(record.get("initialize"), dict) else {}
    declares = init.get("declares") if isinstance(init.get("declares"), dict) else {}
    client = init.get("client_info") if isinstance(init.get("client_info"), dict) else {}
    lines.append(f"- Started in: {_cwd_text(record.get('cwd'))}")
    lines.append(
        f"- initialize: protocol {_code(init.get('protocol_version'))}, client {_code(client.get('name'))} "
        f"version {_code(client.get('version'))}"
    )
    lines.append(f"- Capabilities declared: {_names(init.get('capability_names'))}")
    lines.append(
        f"- roots capability declared: {_yes(declares.get('roots'))}. "
        f"elicitation declared: {_yes(declares.get('elicitation'))}. "
        f"sampling declared: {_yes(declares.get('sampling'))}."
    )
    roots = record.get("roots_list") if isinstance(record.get("roots_list"), dict) else {}
    outcome = roots.get("outcome")
    lines.append(f"- roots/list sent after initialize, declared or not: outcome {_code(outcome)}")
    if outcome == "answered":
        lines.append(f"  - roots returned: {roots.get('root_count')}")
        for root in roots.get("roots") or []:
            if isinstance(root, dict):
                lines.append(
                    f"  - {_code(root.get('uri_shape'))}, {root.get('relation')}, name {root.get('name_kind')}"
                )
    elif outcome == "error":
        error = roots.get("error") if isinstance(roots.get("error"), dict) else {}
        lines.append(f"  - error code {_code(error.get('code'))}: {_code(error.get('message'))}")
    if roots.get("reason"):
        lines.append(f"  - note: {roots.get('reason')}")
    lines.append(f"- Requests the client sent, in order: {_names(record.get('client_requests'))}")
    lines += _env_lines(record.get("env"))
    probes = record.get("probes") if isinstance(record.get("probes"), dict) else {}
    lines.append(
        f"- A variable set in the launch environment reached the server: {_yes(probes.get('launch_env_forwarded'))}. "
        f"A variable set in the server's own entry reached it: {_yes(probes.get('entry_env_forwarded'))}."
    )
    lines.append("")
    return lines


def host_headline(info: dict[str, Any]) -> str:
    parts: list[str] = []
    records = info["hook"]["records"]
    if records:
        stdin = records[0].get("stdin") if isinstance(records[0].get("stdin"), dict) else {}
        cwd_key = stdin.get("cwd_key") if isinstance(stdin.get("cwd_key"), dict) else {}
        keys = ", ".join(str(key) for key in stdin.get("keys") or []) or "none"
        parts.append(f"hook fired {info['hook']['fired']}x, stdin keys {keys}")
        parts.append(f"stdin cwd {cwd_key.get('relation') if cwd_key.get('present') else 'absent'}")
        cwd = records[0].get("cwd") if isinstance(records[0].get("cwd"), dict) else {}
        parts.append(f"hook working folder {cwd.get('relation', 'unknown')}")
        variables = (records[0].get("env") or {}).get("path_variables") or {}
        host_added = sorted(n for n, i in variables.items() if isinstance(i, dict) and i.get("basis") == "added_by_host")
        project = variables.get("CLAUDE_PROJECT_DIR")
        if isinstance(project, dict):
            parts.append(f"CLAUDE_PROJECT_DIR {project.get('relation')}")
        if host_added:
            parts.append("host-added path variables " + ", ".join(host_added))
    else:
        parts.append("hook did not fire")
    servers = info["mcp"]["records"]
    record = servers[0] if servers else None
    if isinstance(record, dict):
        init = record.get("initialize") if isinstance(record.get("initialize"), dict) else {}
        declares = init.get("declares") if isinstance(init.get("declares"), dict) else {}
        cwd = record.get("cwd") if isinstance(record.get("cwd"), dict) else {}
        roots = record.get("roots_list") if isinstance(record.get("roots_list"), dict) else {}
        starts = info["mcp"]["starts"]
        parts.append(f"MCP server started {starts}x")
        which = "server working folder" if starts == 1 else "first start's server working folder"
        parts.append(f"{which} {cwd.get('relation', 'unknown')}")
        parts.append(
            f"roots declared {_yes(declares.get('roots'))}, elicitation declared {_yes(declares.get('elicitation'))}, "
            f"roots/list {roots.get('outcome')}"
        )
    else:
        parts.append("MCP server did not start")
    return "; ".join(parts) + "."


def render_summary(report: dict[str, Any]) -> str:
    lines = [
        "## Host evidence for per-project memory (slice S0)",
        "",
        "Pinned hosts, a loopback stub API, a made-up key, nothing paid. Each host starts in "
        f"{_code('<launch>')}, two folders below the root of a scratch git repository. Paths show as "
        "placeholders that keep their shape. Environment values are never recorded, and text with a space, "
        "or longer than 64 characters, is recorded as its size.",
        "",
        "### Headline",
        "",
    ]
    for host in HOSTS:
        info = report["hosts"].get(host)
        if info is not None:
            lines.append(f"- {host} {_code(info['version'] or 'not found')}: {host_headline(info)}")
    for host in HOSTS:
        info = report["hosts"].get(host)
        if info is None:
            continue
        ran = info.get("run")
        lines += ["", f"### {host} {_code(info['version'] or 'not found')}", ""]
        if isinstance(ran, dict):
            requests = ", ".join(f"{method} {path}" for method, path in ran.get("stub_requests") or []) or "none"
            lines.append(
                f"- Run: exit {_code(ran.get('exit_code'))}, {ran.get('wall_ms')} ms. "
                f"Stub API requests: {_code(requests)}."
            )
            if ran.get("first_stderr_line"):
                lines.append(f"- First stderr line: {_code(ran.get('first_stderr_line'))}")
            if ran.get("first_stdout_line"):
                lines.append(f"- First stdout line: {_code(ran.get('first_stdout_line'))}")
            init = ran.get("init_event")
            if isinstance(init, dict):
                servers = ", ".join(f"{s.get('name')}={s.get('status')}" for s in init.get("mcp_servers") or [])
                lines.append(f"- Claude Code init event keys: {_names(init.get('keys'))}")
                lines.append(
                    f"- Claude Code init event: cwd {_cwd_text(init.get('cwd'))}; "
                    f"MCP servers {_code(servers or 'none')}; {init.get('tool_count')} tools."
                )
            events = ran.get("hook_events")
            if isinstance(events, list) and events:
                shown = ", ".join(
                    f"{e.get('subtype')} {e.get('outcome')} exit {e.get('exit_code')}" for e in events[:6]
                )
                lines.append(f"- Hook events in the stream: {_code(shown)}")
        lines.append(
            f"- Started in {_code('<launch>')}; the git root is {info['launched_in']['git_levels_up']} level(s) up."
        )
        lines.append("")
        lines += _hook_lines(info)
        lines += _mcp_lines(info)
    problems = [(host, text) for host in HOSTS if host in report["hosts"] for text in report["hosts"][host]["problems"]]
    if problems:
        lines += ["### Problems", ""]
        lines += [f"- {host}: {text}" for host, text in problems]
    return "\n".join(lines) + "\n"


def build_report(results: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "pinned_versions": dict(PINNED_VERSIONS),
        "hosts": dict(results),
        "headline": {host: host_headline(info) for host, info in results.items()},
    }


def run(
    artifacts: Path,
    temp: Path,
    *,
    hosts: Sequence[str] = HOSTS,
    roots_wait: float = _ROOTS_WAIT_SECONDS,
    api_wait: float = _API_WAIT_SECONDS,
    require_ci: bool = True,
    executables: Mapping[str, str] | None = None,
) -> int:
    """Run each host once and write ``host-evidence.json`` and ``host-evidence.md`` into ``artifacts``."""

    if require_ci and os.environ.get("GITHUB_ACTIONS") != "true":
        print("refusing to start a real host outside GitHub Actions", file=sys.stderr)
        return 2
    if artifacts.exists() and (not artifacts.is_dir() or any(artifacts.iterdir())):
        print("refusing a non-empty artifacts directory", file=sys.stderr)
        return 1
    artifacts.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)
    # Resolved once, so every folder a host reports compares with the real path it was started in.
    artifacts, temp = artifacts.resolve(), temp.resolve()
    results: dict[str, dict[str, Any]] = {}
    for host in hosts:
        try:
            results[host] = _RUNNERS[host](
                artifacts,
                temp / host,
                roots_wait=roots_wait,
                api_wait=api_wait,
                executable=(executables or {}).get(host),
            )
        except Exception as problem:  # noqa: BLE001 - one host failing must not hide the other
            hooks, servers = _collect(host, artifacts)
            results[host] = {
                "host": host,
                "version": "",
                "pinned": False,
                "launched_in": {"placeholder": "<launch>", "git_levels_up": None},
                "run": None,
                "hook": {"fired": len(hooks), "records": hooks},
                "mcp": {"starts": len(servers), "records": servers},
                "problems": [f"the run failed with {type(problem).__name__}"],
            }
    report = build_report(results)
    (artifacts / "host-evidence.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    summary = render_summary(report)
    (artifacts / "host-evidence.md").write_text(summary, encoding="utf-8")
    target = os.environ.get(SUMMARY_ENV)
    if target:
        with Path(target).open("a", encoding="utf-8") as handle:
            handle.write(summary)
    broken = [(host, text) for host, info in results.items() for text in info["problems"]]
    for host, text in broken:
        print(f"{host}: {text}", file=sys.stderr)
    return 1 if broken else 0


def _usage() -> int:
    print("usage: real_host_evidence.py run <artifacts> <work>", file=sys.stderr)
    print("       real_host_evidence.py hook <host> <artifacts> <context>", file=sys.stderr)
    print("       real_host_evidence.py mcp <host> <artifacts> <context> [--roots-wait SECONDS]", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 3 and args[0] == "run":
        return run(Path(args[1]), Path(args[2]))
    if len(args) == 4 and args[0] == "hook" and args[1] in HOSTS:
        return hook_main(args[1], Path(args[2]), Path(args[3]))
    if len(args) in (4, 6) and args[0] == "mcp" and args[1] in HOSTS:
        wait = _ROOTS_WAIT_SECONDS
        if len(args) == 6:
            if args[4] != "--roots-wait":
                return _usage()
            try:
                wait = float(args[5])
            except ValueError:
                return _usage()
        return mcp_main(args[1], Path(args[2]), Path(args[3]), wait)
    return _usage()


if __name__ == "__main__":
    sys.exit(main())
