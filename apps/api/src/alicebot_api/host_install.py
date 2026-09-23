"""Write host MCP config for ``alice-memory install``.

Install writes host files. It does not import a vault, start an agent
runtime, or call ``openclaw``. Import stays a source. Commit stays a fact.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from alicebot_api import __version__

ALICE_MEMORY_DATA_DIR_ENV = "ALICE_MEMORY_DATA_DIR"
SESSION_START_COMMAND = "alice-memory-session-start"
MCP_COMMAND = "uvx"
MCP_ARGS_PREFIX = ("alice-memory", "mcp", "--data-dir")
DEFAULT_DATA_DIR = "~/.alice"
MCPB_SUFFIX = ".mcpb"
MCPB_MANIFEST_NAME = "manifest.json"
MCPB_HOMEPAGE = "https://www.alicememory.com"
MCPB_AUTHOR_NAME = "Sami Rusani"
BRIEF_HINT = (
    "Run alice-memory brief or alice-memory-session-start --format markdown"
)
UVX_MISSING_WARNING = (
    "warning: uvx is not on PATH. The host entries and hooks start Alice with uvx, "
    "so the hosts cannot start it until uv is installed: https://docs.astral.sh/uv/"
)

INSTALL_HOSTS = (
    "claude-desktop",
    "claude-code",
    "cursor",
    "openclaw",
    "hermes",
)
DEFAULT_INSTALL_HOSTS = (
    "claude-desktop",
    "claude-code",
    "cursor",
    "openclaw",
)

_SESSION_START_HOSTS = frozenset({"claude-code", "cursor"})


class InstallError(ValueError):
    """A user-facing install failure with a static CLI error code."""


class _MalformedHostFile(InstallError):
    """A host file whose structure install will not edit. The host is refused."""


def resolve_home(home: str | None) -> Path:
    """Resolve ``--home``. Omit it to use the process home."""

    if home is None:
        return Path.home()
    if home.startswith("~"):
        return Path(home).expanduser().resolve()
    path = Path(home)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def resolve_user_path(raw: str, home: Path) -> Path:
    """Expand ``~`` against ``home``. Do not call ``Path.expanduser``."""

    text = str(raw)
    if text == "~":
        return home.resolve()
    if text.startswith("~/") or text.startswith("~\\"):
        return (home / text[2:]).resolve()
    path = Path(text)
    if path.is_absolute():
        return path.resolve()
    return (home / path).resolve()


def claude_desktop_config_path(home: Path, platform: str | None = None) -> Path:
    plat = sys.platform if platform is None else platform
    if plat == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if plat == "win32":
        return home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    return home / ".config" / "Claude" / "claude_desktop_config.json"


def host_file_map(home: Path, platform: str | None = None) -> dict[str, dict[str, Path]]:
    """Host config paths under ``home``."""

    return {
        "claude-desktop": {"mcp": claude_desktop_config_path(home, platform)},
        "claude-code": {
            "mcp": home / ".claude.json",
            "hooks": home / ".claude" / "settings.json",
        },
        "cursor": {
            "mcp": home / ".cursor" / "mcp.json",
            "hooks": home / ".cursor" / "hooks.json",
        },
        "openclaw": {"mcp": home / ".openclaw" / "openclaw.json"},
        "hermes": {"mcp": home / ".hermes" / "config.yaml"},
    }


def mcp_server_payload(data_dir: str, *, with_env: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "command": MCP_COMMAND,
        "args": [MCP_ARGS_PREFIX[0], MCP_ARGS_PREFIX[1], MCP_ARGS_PREFIX[2], data_dir],
    }
    if with_env:
        payload["env"] = {ALICE_MEMORY_DATA_DIR_ENV: data_dir}
    return payload


def openclaw_add_line(data_dir: str) -> str:
    return (
        "openclaw mcp add alice --command uvx --arg alice-memory "
        f"--arg mcp --arg --data-dir --arg {data_dir}"
    )


def session_start_hook_command(data_dir: str) -> str:
    """SessionStart argv that still works after ``uvx alice-memory install`` exits."""

    return f"uvx --from alice-memory {SESSION_START_COMMAND} --data-dir {data_dir}"


def session_start_hook_entry(data_dir: str) -> dict[str, str]:
    """Cursor's ``hooks.sessionStart`` item: a flat ``{"command": ...}`` object."""

    return {"command": session_start_hook_command(data_dir)}


def claude_code_session_start_handler(data_dir: str) -> dict[str, str]:
    """One Claude Code hook handler. Claude Code requires ``type``."""

    return {"type": "command", "command": session_start_hook_command(data_dir)}


def claude_code_session_start_group(data_dir: str) -> dict[str, object]:
    """Claude Code's ``hooks.SessionStart`` item: a matcher group.

    Claude Code nests handlers under ``hooks``. It ignores Cursor's flat
    ``{"command": ...}`` item with ``Hook matcher "hooks" must be an array of
    hook entries``. ``matcher`` is omitted, which Claude Code documents as
    matching every SessionStart source.
    """

    return {"hooks": [claude_code_session_start_handler(data_dir)]}


def _is_session_start_command(command: str) -> bool:
    for part in command.split():
        if Path(part).name == SESSION_START_COMMAND:
            return True
    return False


def _contains_session_start(node: object) -> bool:
    if isinstance(node, Mapping):
        command = node.get("command")
        if isinstance(command, str) and _is_session_start_command(command):
            return True
        return any(_contains_session_start(value) for value in node.values())
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        return any(_contains_session_start(value) for value in node)
    return False


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _MalformedHostFile(f"{label} is not an object")
    return value


def _set_nested(doc: dict[str, Any], keys: tuple[str, ...], value: object) -> None:
    current: dict[str, Any] = doc
    for key in keys[:-1]:
        existing = current.get(key)
        if existing is None:
            nested: dict[str, Any] = {}
            current[key] = nested
            current = nested
            continue
        current = _require_mapping(existing, key)
    current[keys[-1]] = value


def _alice_server_keys(host: str) -> tuple[str, ...]:
    if host == "openclaw":
        return ("mcp", "servers", "alice")
    return ("mcpServers", "alice")


def _read_host_file(path: Path) -> bytes | None:
    """The file's bytes, or None when it does not exist. OSError propagates."""

    if not path.exists():
        return None
    return path.read_bytes()


def _parse_json_host(raw: bytes | None) -> dict[str, Any]:
    """A JSON host file as a dict. An absent or empty file is ``{}``."""

    if not raw:
        return {}
    try:
        loaded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _MalformedHostFile("the file is not valid JSON") from exc
    if not isinstance(loaded, dict):
        raise _MalformedHostFile("the top level is not an object")
    return loaded


def _dump_json(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=True, indent=2) + "\n"


def _ensure_private_parents(path: Path) -> None:
    missing: list[Path] = []
    cursor = path.parent
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if not missing:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory in missing:
        directory.chmod(0o700)


def _write_text(path: Path, text: str, *, newline: str | None = None) -> None:
    """Atomically replace ``path``. ``newline=""`` writes line breaks as given."""

    _ensure_private_parents(path)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline=newline,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".alice-install.tmp",
        delete=False,
    )
    tmp_path = Path(handle.name)
    try:
        handle.write(text)
        handle.close()
        tmp_path.replace(path)
    except Exception:
        handle.close()
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _hook_item_command(item: object) -> str | None:
    if isinstance(item, Mapping):
        command = item.get("command")
        if isinstance(command, str):
            return command
    return None


def _merge_session_start(doc: dict[str, Any], host: str, command: str) -> str:
    """Merge Alice's SessionStart hook. Return added, updated or already-present."""

    if host == "claude-code":
        return _merge_claude_code_session_start(doc, command)
    return _merge_cursor_session_start(doc, command)


def _merge_cursor_session_start(doc: dict[str, Any], command: str) -> str:
    hooks = _require_mapping(doc.get("hooks"), "hooks")
    doc["hooks"] = hooks
    key = "sessionStart"
    existing = hooks.get(key)
    entry = {"command": command}
    desired = command
    if existing is None:
        hooks[key] = [entry]
        return "added"
    if not isinstance(existing, list):
        raise _MalformedHostFile(f"hooks.{key} is not a list")

    alice_indexes = [
        index
        for index, item in enumerate(existing)
        if _contains_session_start(item)
    ]
    if not alice_indexes:
        existing.append(entry)
        return "added"
    if (
        len(alice_indexes) == 1
        and _hook_item_command(existing[alice_indexes[0]]) == desired
    ):
        return "already-present"

    kept: list[object] = []
    replaced = False
    for index, item in enumerate(existing):
        if index in alice_indexes:
            if not replaced:
                kept.append(entry)
                replaced = True
            continue
        kept.append(item)
    hooks[key] = kept
    return "updated"


def _is_alice_hook_item(item: object) -> bool:
    command = _hook_item_command(item)
    return command is not None and _is_session_start_command(command)


def _merge_claude_code_session_start(doc: dict[str, Any], command: str) -> str:
    """Leave exactly one Alice handler in Claude Code's nested shape.

    Alice items come in two forms. A legacy flat item, ``{"command": ...}``
    directly in ``SessionStart``, is what v0.16.0 wrote; Claude Code ignores
    it. A nested handler sits in a well-formed group, one whose ``hooks`` is
    a list. The first nested handler is updated in place, keeping its group,
    position and any extra keys. With no nested handler, the first flat item
    is replaced in place by a new group. Every other Alice item in those two
    forms is removed, and a group left empty by that removal goes too. A
    malformed group (``hooks`` not a list) is not read and not changed.
    Items that are not Alice's are never changed.
    """

    hooks = _require_mapping(doc.get("hooks"), "hooks")
    doc["hooks"] = hooks
    key = "SessionStart"
    existing = hooks.get(key)
    desired = {"type": "command", "command": command}
    group: dict[str, object] = {"hooks": [dict(desired)]}
    if existing is None:
        hooks[key] = [group]
        return "added"
    if not isinstance(existing, list):
        raise _MalformedHostFile(f"hooks.{key} is not a list")

    flat: list[int] = []
    nested: list[tuple[int, int]] = []
    for group_index, item in enumerate(existing):
        if not isinstance(item, Mapping):
            continue
        handlers = item.get("hooks")
        if handlers is None:
            if _is_alice_hook_item(item):
                flat.append(group_index)
            continue
        if not isinstance(handlers, list):
            continue
        for handler_index, handler in enumerate(handlers):
            if _is_alice_hook_item(handler):
                nested.append((group_index, handler_index))

    if not flat and not nested:
        existing.append(group)
        return "added"
    if not flat and len(nested) == 1:
        group_index, handler_index = nested[0]
        current = existing[group_index]["hooks"][handler_index]
        if current.get("type") == desired["type"] and current.get("command") == desired["command"]:
            return "already-present"

    keep = nested[0] if nested else None
    nested_set = set(nested)
    merged: list[object] = []
    for group_index, item in enumerate(existing):
        if group_index in flat:
            if keep is None and group_index == flat[0]:
                merged.append(group)
            continue
        touched = [pair for pair in nested if pair[0] == group_index]
        if not touched:
            merged.append(item)
            continue
        handlers_kept: list[object] = []
        for handler_index, handler in enumerate(item["hooks"]):
            if (group_index, handler_index) not in nested_set:
                handlers_kept.append(handler)
                continue
            if (group_index, handler_index) == keep:
                updated = dict(handler)
                updated["type"] = desired["type"]
                updated["command"] = desired["command"]
                handlers_kept.append(updated)
        if not handlers_kept:
            continue
        rebuilt = dict(item)
        rebuilt["hooks"] = handlers_kept
        merged.append(rebuilt)
    hooks[key] = merged
    return "updated"


def _new_hooks_document(host: str, command: str) -> dict[str, Any]:
    if host == "cursor":
        return {"version": 1, "hooks": {"sessionStart": [{"command": command}]}}
    return {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}


# --- JSON hosts: re-runs keep what the user set --------------------------------
#
# An alice entry install wrote (uvx running alice-memory mcp, pinned or not)
# is kept key for key on a re-run: env, type, timeout, cwd and an absolute
# uvx path all stay. Only the --data-dir value can change, and only when
# --data-dir is passed. Any other alice entry is not install's to rewrite,
# so that file is left alone and the host is refused.

_UVX_NAMES = frozenset({"uvx", "uvx.exe"})
_ALICE_PACKAGE_ARG = re.compile(r"alice-memory(?:\[[^\]]*\])?(?:(?:==|@|>=|<=|~=|!=).*)?\Z")


def is_install_shaped_entry(entry: object) -> bool:
    """True for an alice entry of the shape install writes: uvx alice-memory mcp.

    The command's basename is ``uvx`` or ``uvx.exe`` at any path, and the
    string args include the alice-memory package (pinned or not) and ``mcp``.
    """

    if not isinstance(entry, Mapping):
        return False
    command = entry.get("command")
    args = entry.get("args")
    if not isinstance(command, str) or PureWindowsPath(command).name.lower() not in _UVX_NAMES:
        return False
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return False
    return "mcp" in args and any(_ALICE_PACKAGE_ARG.match(arg) for arg in args)


def _entry_data_dir(entry: Mapping[str, Any]) -> str | None:
    """The data dir an install-shaped entry runs with: --data-dir, else its env."""

    args = entry.get("args")
    if isinstance(args, list):
        for index, arg in enumerate(args):
            if arg == "--data-dir" and index + 1 < len(args) and isinstance(args[index + 1], str):
                return str(args[index + 1])
            if isinstance(arg, str) and arg.startswith("--data-dir="):
                return arg.split("=", 1)[1]
    env = entry.get("env")
    if isinstance(env, Mapping) and isinstance(env.get(ALICE_MEMORY_DATA_DIR_ENV), str):
        return str(env[ALICE_MEMORY_DATA_DIR_ENV])
    return None


def _args_with_data_dir(args: Sequence[str], data_dir: str) -> list[str]:
    """``args`` with only the --data-dir value replaced, or the pair appended."""

    updated = list(args)
    for index, arg in enumerate(updated):
        if arg == "--data-dir":
            if index + 1 < len(updated):
                updated[index + 1] = data_dir
            else:
                updated.append(data_dir)
            return updated
        if arg.startswith("--data-dir="):
            updated[index] = f"--data-dir={data_dir}"
            return updated
    return [*updated, "--data-dir", data_dir]


def _kept_keys(entry: Mapping[str, Any]) -> list[str]:
    """The user's keys in an install-shaped entry, for the receipt."""

    kept = ["command"] if entry.get("command") != MCP_COMMAND else []
    for key, value in entry.items():
        if key in {"command", "args"}:
            continue
        kept.append(f"{key} ({len(value)} keys)" if isinstance(value, Mapping) else str(key))
    return kept


def _hook_command_data_dir(command: str) -> str | None:
    parts = command.split()
    for index, part in enumerate(parts):
        if part == "--data-dir" and index + 1 < len(parts):
            return parts[index + 1]
        if part.startswith("--data-dir="):
            return part.split("=", 1)[1]
    return None


def _existing_alice_hook_command(doc: Mapping[str, Any], host: str) -> str | None:
    """The command of the Alice hook a merge would keep, or None."""

    hooks = doc.get("hooks")
    if not isinstance(hooks, Mapping):
        return None
    items = hooks.get("SessionStart" if host == "claude-code" else "sessionStart")
    if not isinstance(items, list):
        return None
    if host == "claude-code":
        for item in items:
            handlers = item.get("hooks") if isinstance(item, Mapping) else None
            for handler in handlers if isinstance(handlers, list) else []:
                if _is_alice_hook_item(handler):
                    return _hook_item_command(handler)
        for item in items:
            if isinstance(item, Mapping) and "hooks" not in item and _is_alice_hook_item(item):
                return _hook_item_command(item)
        return None
    for item in items:
        if _is_alice_hook_item(item):
            return _hook_item_command(item)
    return None


def _alice_container(doc: dict[str, Any], host: str) -> dict[str, Any] | None:
    """The object that holds (or would hold) ``alice``; None when it is absent."""

    current: object = doc
    keys = _alice_server_keys(host)
    for depth, key in enumerate(keys[:-1]):
        assert isinstance(current, dict)
        child = current.get(key)
        if child is None:
            return None
        if not isinstance(child, dict):
            raise _MalformedHostFile(f"{'.'.join(keys[: depth + 1])} is not an object")
        current = child
    assert isinstance(current, dict)
    return current


def _check_hooks_file(doc: Mapping[str, Any], host: str) -> None:
    hooks = doc.get("hooks")
    if hooks is None:
        return
    if not isinstance(hooks, Mapping):
        raise _MalformedHostFile("hooks is not an object")
    key = "SessionStart" if host == "claude-code" else "sessionStart"
    if key in hooks and not isinstance(hooks[key], list):
        raise _MalformedHostFile(f"hooks.{key} is not a list")


def _describe_entry(entry: object) -> str:
    if isinstance(entry, Mapping):
        command = entry.get("command")
        if isinstance(command, str):
            return f"its command is {command}"
        return "it has no string command"
    return "it is not an object"


# --- Hermes config.yaml ------------------------------------------------------------
#
# Hermes reads ~/.hermes/config.yaml with PyYAML, and install must not change
# the meaning of anything already in that file. The wheel carries no YAML
# library, so install never writes the file back from a parse. It finds where
# mcp_servers.alice belongs with the line scanner below, inserts (or replaces)
# only those lines, and keeps every other byte. The scanner reads a narrow
# subset of YAML on purpose. A file that uses anything outside it is left
# alone: install prints the snippet and exits non-zero.

HERMES_BACKUP_MARKER = ".alice-backup-"
_BACKSLASH = chr(92)
_BYTE_ORDER_MARK = chr(0xFEFF)
# YAML 1.1 treats these as line breaks; the scanner only splits on \n / \r\n.
_UNICODE_LINE_BREAKS = (chr(0x85), chr(0x2028), chr(0x2029))
_EMPTY_MAPPING_VALUES = frozenset({"{}", "~", "null", "Null", "NULL"})
_BLOCK_SCALAR_HEADER = re.compile(r"[|>](?:[1-9][+-]?|[+-][1-9]?)?(?: +#.*)? *\Z")
_TAB_REFUSAL = "a tab outside a quoted value or comment"


class HermesConfigRefused(InstallError):
    """install left config.yaml untouched because it could not edit it safely.

    ``extra_keys`` names keys in an old alice entry that install did not
    write, so the receipt can tell the user to keep them when pasting.
    ``data_dir``, when set, is the data dir the pasted entry should use.
    """

    def __init__(
        self,
        reason: str,
        line: int | None = None,
        *,
        extra_keys: Sequence[str] = (),
        data_dir: str | None = None,
    ) -> None:
        detail = reason if line is None else f"{reason} (line {line})"
        super().__init__(detail)
        self.detail = detail
        self.extra_keys = tuple(extra_keys)
        self.data_dir = data_dir


class InstallRefused(InstallError):
    """A host file was left unchanged on purpose. ``output`` holds every receipt."""

    def __init__(self, output: str) -> None:
        super().__init__("a host config was left unchanged")
        self.output = output


class InstallFailed(InstallError):
    """A host file could not be read or written. ``output`` holds every receipt."""

    def __init__(self, output: str) -> None:
        super().__init__("a host config could not be written")
        self.output = output


@dataclass(frozen=True)
class _YamlLine:
    """What the scanner needs to know about one line outside block scalar text."""

    indent: int
    trivia: bool = False
    dash: bool = False
    colon: int = -1
    key: str | None = None
    value: str = ""
    comment: str = ""
    block_owner: int | None = None
    anchor: bool = False
    item: str = ""


@dataclass(frozen=True)
class _YamlText:
    prefix: str
    newline: str
    lines: list[str]
    final_newline: bool
    info: list[_YamlLine | None]
    ends_in_block: bool

    def substantive(self, index: int) -> bool:
        """True for a line that carries YAML content, block scalar text included."""

        item = self.info[index]
        if item is None:
            return bool(self.lines[index].strip(" \t"))
        return not item.trivia


def _scan_quoted(line: str, start: int) -> int:
    """Index just past the quoted scalar at ``start``, or -1 when it runs off the line."""

    quote = line[start]
    index = start + 1
    while index < len(line):
        char = line[index]
        if quote == '"' and char == _BACKSLASH:
            index += 2
            continue
        if char == quote:
            if quote == "'" and index + 1 < len(line) and line[index + 1] == "'":
                index += 2
                continue
            return index + 1
        index += 1
    return -1


def _scan_flow(line: str, start: int) -> tuple[int, bool]:
    """Scan the flow collection at ``start``.

    Returns the index just past it (-1 when it runs off the line, -2 at a tab
    outside a quoted value) and whether a node inside it carries an ``&``
    anchor.
    """

    depth = 0
    expect_node = True
    anchor = False
    index = start
    while index < len(line):
        char = line[index]
        if char == "\t":
            return -2, anchor
        if char in "[{":
            depth += 1
            expect_node = True
            index += 1
        elif char in "]}":
            depth -= 1
            index += 1
            expect_node = False
            if depth == 0:
                return index, anchor
        elif char == ",":
            expect_node = True
            index += 1
        elif char == " ":
            index += 1
        elif char == "#" and line[index - 1] == " ":
            return -1, anchor
        elif char in "&!" and expect_node:
            # A node property. An anchor here can be aliased from anywhere.
            anchor = anchor or char == "&"
            while index < len(line) and line[index] not in " \t,[]{}":
                index += 1
        elif char in "\"'" and expect_node:
            end = _scan_quoted(line, index)
            if end < 0:
                return -1, anchor
            index = end
            expect_node = False
        elif char == ":":
            expect_node = True
            index += 1
        else:
            end = index + 1
            while end < len(line) and line[end] not in ",[]{}":
                if line[end] == "\t":
                    return -2, anchor
                if line[end] == ":" and (end + 1 == len(line) or line[end + 1] in " \t,[]{}"):
                    break
                if line[end] == "#" and line[end - 1] == " ":
                    break
                end += 1
            index = end
            expect_node = False
    return -1, anchor


def _decode_yaml_key(text: str, kind: str, has_properties: bool) -> str | None:
    """The key as PyYAML would read it, or None when this scanner cannot tell."""

    if has_properties:
        return None
    if kind == "plain":
        return text.rstrip(" \t")
    if kind == "quoted":
        inner = text[1:-1]
        if text[0] == "'":
            return inner.replace("''", "'")
        if _BACKSLASH not in inner:
            return inner
    return None


def _lex_yaml_line(line: str, number: int) -> _YamlLine:
    """Read one line outside block scalar text. Refuse what this scanner cannot place."""

    stripped = line.lstrip(" ")
    indent = len(line) - len(stripped)
    if stripped.startswith("\t"):
        raise HermesConfigRefused("a line is indented with a tab", number)
    if not stripped.strip(" "):
        return _YamlLine(indent=indent, trivia=True)
    if stripped.startswith("#"):
        return _YamlLine(indent=indent, trivia=True)

    size = len(line)
    dash = False
    owner = indent
    colon = -1
    key: str | None = None
    value_start = -1
    comment_at = size
    block_owner: int | None = None
    has_properties = False
    anchor = False
    node_column = -1
    node_start = node_end = -1
    node_kind = ""
    after_node = False
    item_start = -1
    index = indent
    while index < size:
        char = line[index]
        if char == " ":
            index += 1
            continue
        if char == "\t":
            raise HermesConfigRefused(_TAB_REFUSAL, number)
        followed_by_space = index + 1 == size or line[index + 1] in " \t"
        if after_node:
            if char == ":" and followed_by_space:
                if colon >= 0:
                    raise HermesConfigRefused("two mapping keys on one line", number)
                colon = index
                owner = node_column
                if not dash:
                    key = _decode_yaml_key(line[node_start:node_end], node_kind, has_properties)
                value_start = index + 1
                has_properties = False
                after_node = False
                index += 1
                continue
            if char == "#" and line[index - 1] in " \t":
                comment_at = index
                break
            raise HermesConfigRefused("text follows a quoted or flow value", number)
        if char == "#":
            comment_at = index
            break
        if char == "-" and followed_by_space:
            if colon >= 0 or has_properties:
                raise HermesConfigRefused("a list entry follows a key on the same line", number)
            dash = True
            owner = index
            index += 1
            item_start = index
            continue
        if char == "?" and followed_by_space:
            raise HermesConfigRefused("an explicit '?' key", number)
        if char == ":" and followed_by_space:
            raise HermesConfigRefused("a mapping entry with no key", number)
        if char in "&!":
            if not has_properties:
                node_column = index
            has_properties = True
            anchor = anchor or char == "&"
            while index < size and line[index] not in " \t":
                index += 1
            continue
        if char in "|>":
            if colon < 0 and not dash:
                # The header's indent is not the indent that ends the scalar
                # here, so the scanner cannot tell where the text stops.
                raise HermesConfigRefused("a block scalar header on its own line", number)
            rest = line[index:]
            code = rest if " #" not in rest else rest[: rest.index(" #")]
            if "\t" in code:
                raise HermesConfigRefused(_TAB_REFUSAL, number)
            if _BLOCK_SCALAR_HEADER.match(line, index) is None:
                raise HermesConfigRefused("a block scalar header this writer does not read", number)
            block_owner = owner
            break
        if char in "\"'":
            end = _scan_quoted(line, index)
            if end < 0:
                raise HermesConfigRefused("a quoted value continues on the next line", number)
            node_kind = "quoted"
        elif char in "[{":
            end, flow_anchor = _scan_flow(line, index)
            if end == -2:
                raise HermesConfigRefused(_TAB_REFUSAL, number)
            if end < 0:
                raise HermesConfigRefused("a flow value continues on the next line", number)
            anchor = anchor or flow_anchor
            node_kind = "flow"
        elif char in "]},%@`":
            raise HermesConfigRefused("a YAML indicator this writer does not read", number)
        elif char == "*":
            end = index + 1
            while end < size and line[end] not in " \t,[]{}":
                end += 1
            node_kind = "alias"
        else:
            end = index + 1
            while end < size:
                if line[end] == "\t":
                    raise HermesConfigRefused(_TAB_REFUSAL, number)
                if line[end] == ":" and (end + 1 == size or line[end + 1] in " \t"):
                    break
                if line[end] == "#" and line[end - 1] == " ":
                    break
                end += 1
            node_kind = "plain"
        if not has_properties:
            node_column = index
        node_start, node_end = index, end
        after_node = True
        index = end

    value = ""
    if colon >= 0:
        value = "|" if block_owner is not None else line[value_start:comment_at].strip(" \t")
    comment = line[comment_at:].rstrip(" \t") if comment_at < size else ""
    item = ""
    if dash and colon < 0 and block_owner is None:
        item = line[item_start:comment_at].strip(" ")
    return _YamlLine(
        indent=indent,
        dash=dash,
        colon=colon,
        key=key,
        value=value,
        comment=comment,
        block_owner=block_owner,
        anchor=anchor,
        item=item,
    )


def _leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scan_yaml_text(text: str) -> _YamlText:
    prefix = _BYTE_ORDER_MARK if text.startswith(_BYTE_ORDER_MARK) else ""
    body = text[len(prefix):]
    if any(mark in body for mark in _UNICODE_LINE_BREAKS):
        raise HermesConfigRefused("a Unicode line separator")
    newline = "\n"
    if "\r" in body:
        pairs = body.count("\r\n")
        if body.count("\r") != pairs or body.count("\n") != pairs:
            raise HermesConfigRefused("mixed line endings")
        newline = "\r\n"
    lines = body.split(newline) if body else []
    final_newline = bool(lines) and lines[-1] == ""
    if final_newline:
        lines.pop()

    info: list[_YamlLine | None] = []
    block_owner: int | None = None
    seen_content = False
    seen_marker = False
    for number, line in enumerate(lines, start=1):
        if block_owner is not None:
            if not line.strip(" \t") or _leading_spaces(line) > block_owner:
                info.append(None)
                continue
            block_owner = None
        if line.startswith("%"):
            raise HermesConfigRefused("a YAML directive", number)
        if line.startswith("---") and (len(line) == 3 or line[3] in " \t"):
            if "\t" in line.split(" #", 1)[0]:
                raise HermesConfigRefused(_TAB_REFUSAL, number)
            rest = line[3:].strip(" ")
            if seen_content or seen_marker or (rest and not rest.startswith("#")):
                raise HermesConfigRefused(
                    "more than one YAML document, or content on a '---' line", number
                )
            seen_marker = True
            info.append(_YamlLine(indent=0, trivia=True))
            continue
        if line.startswith("...") and (len(line) == 3 or line[3] in " \t"):
            raise HermesConfigRefused("a YAML document end marker", number)
        item = _lex_yaml_line(line, number)
        info.append(item)
        if not item.trivia:
            seen_content = True
        block_owner = item.block_owner
    return _YamlText(
        prefix=prefix,
        newline=newline,
        lines=lines,
        final_newline=final_newline,
        info=info,
        ends_in_block=block_owner is not None,
    )


def _yaml_double_quoted(text: str) -> str:
    """A YAML double-quoted scalar that PyYAML reads back as exactly ``text``."""

    out = ['"']
    for char in text:
        code = ord(char)
        if char == _BACKSLASH:
            out.append(_BACKSLASH * 2)
        elif char == '"':
            out.append(_BACKSLASH + '"')
        elif 0x20 <= code <= 0x7E:
            out.append(char)
        elif (
            code < 0x20
            or 0x7F <= code <= 0x9F
            or 0xD800 <= code <= 0xDFFF
            or code in (0x2028, 0x2029, 0xFEFF, 0xFFFE, 0xFFFF)
        ):
            if code <= 0xFF:
                out.append(f"{_BACKSLASH}x{code:02x}")
            elif code <= 0xFFFF:
                out.append(f"{_BACKSLASH}u{code:04x}")
            else:
                out.append(f"{_BACKSLASH}U{code:08x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _hermes_alice_lines(data_dir: str, indent: int) -> list[str]:
    """Install's own mcp_servers.alice block at ``indent``."""

    return _hermes_block_lines(mcp_server_payload(data_dir, with_env=True), indent)


def _hermes_block_lines(payload: Mapping[str, object], indent: int) -> list[str]:
    """An alice block at ``indent`` for ``payload``, every value double-quoted."""

    pad = " " * indent
    lines = [f"{pad}alice:"]
    for key, value in payload.items():
        if isinstance(value, list):
            lines.append(f"{pad}  {key}:")
            lines.extend(f"{pad}    - {_yaml_double_quoted(str(item))}" for item in value)
        elif isinstance(value, dict):
            lines.append(f"{pad}  {key}:")
            lines.extend(
                f"{pad}    {name}: {_yaml_double_quoted(str(item))}" for name, item in value.items()
            )
        else:
            lines.append(f"{pad}  {key}: {_yaml_double_quoted(str(value))}")
    return lines


def hermes_snippet(data_dir: str) -> str:
    """What to paste into config.yaml by hand."""

    return "\n".join(["mcp_servers:", *_hermes_alice_lines(data_dir, 2)]) + "\n"


def _render_yaml_text(doc: _YamlText, lines: list[str], final_newline: bool) -> str:
    if not lines:
        return doc.prefix
    return doc.prefix + doc.newline.join(lines) + (doc.newline if final_newline else "")


class _Opaque:
    """A value the reader keeps opaque: an alias or a tagged node."""

    def __repr__(self) -> str:
        return "<opaque>"


_OPAQUE = _Opaque()
_YAML_NULLS = frozenset({"", "~", "null", "Null", "NULL"})
_DOUBLE_QUOTED_ESCAPES = {
    "0": chr(0),
    "a": chr(7),
    "b": chr(8),
    "t": chr(9),
    chr(9): chr(9),
    "n": chr(10),
    "v": chr(11),
    "f": chr(12),
    "r": chr(13),
    "e": chr(27),
    " ": " ",
    '"': '"',
    "/": "/",
    _BACKSLASH: _BACKSLASH,
    "N": chr(0x85),
    "_": chr(0xA0),
    "L": chr(0x2028),
    "P": chr(0x2029),
}
_HEX_ESCAPE_WIDTH = {"x": 2, "u": 4, "U": 8}
_ALICE_INSTALL_KEYS = frozenset({"command", "args", "env"})


class _UnreadableEntry(Exception):
    """The old alice entry uses YAML the reader does not decode."""


def _decode_double_quoted(inner: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(inner):
        char = inner[index]
        if char != _BACKSLASH:
            out.append(char)
            index += 1
            continue
        if index + 1 >= len(inner):
            raise _UnreadableEntry
        code = inner[index + 1]
        if code in _DOUBLE_QUOTED_ESCAPES:
            out.append(_DOUBLE_QUOTED_ESCAPES[code])
            index += 2
            continue
        width = _HEX_ESCAPE_WIDTH.get(code)
        digits = inner[index + 2 : index + 2 + width] if width else ""
        if not width or len(digits) != width or any(c not in "0123456789abcdefABCDEF" for c in digits):
            raise _UnreadableEntry
        out.append(chr(int(digits, 16)))
        index += 2 + width
    return "".join(out)


def _read_scalar_text(text: str) -> object:
    """One scalar or single-line flow value, as PyYAML would read it for our keys.

    Plain scalars are read as their text (install only needs strings back),
    YAML nulls as None, aliases and tags as opaque.
    """

    text = text.strip(" ")
    if text in _YAML_NULLS:
        return None
    if text[0] in "*!":
        return _OPAQUE
    if text[0] in "&?|>":
        raise _UnreadableEntry
    if text[0] in "[{":
        value, end = _read_flow_node(text, 0)
        if text[end:].strip(" "):
            raise _UnreadableEntry
        return value
    if text[0] == "'":
        return text[1:-1].replace("''", "'")
    if text[0] == '"':
        return _decode_double_quoted(text[1:-1])
    return text


def _read_flow_node(text: str, index: int) -> tuple[object, int]:
    while index < len(text) and text[index] == " ":
        index += 1
    if index >= len(text):
        raise _UnreadableEntry
    char = text[index]
    if char in "[{":
        closing = "]" if char == "[" else "}"
        items: list[object] = []
        mapping: dict[str, object] = {}
        index += 1
        while True:
            while index < len(text) and text[index] == " ":
                index += 1
            if index < len(text) and text[index] == closing:
                return (items if char == "[" else mapping), index + 1
            node, index = _read_flow_node(text, index)
            while index < len(text) and text[index] == " ":
                index += 1
            if char == "[":
                items.append(node)
            else:
                if not isinstance(node, str) or node in mapping:
                    raise _UnreadableEntry
                value: object = None
                if index < len(text) and text[index] == ":":
                    after = index + 1
                    while after < len(text) and text[after] == " ":
                        after += 1
                    if after < len(text) and text[after] in ",}":
                        index = after
                    else:
                        value, index = _read_flow_node(text, index + 1)
                        while index < len(text) and text[index] == " ":
                            index += 1
                mapping[node] = value
            if index < len(text) and text[index] == ",":
                index += 1
                continue
            if index < len(text) and text[index] == closing:
                return (items if char == "[" else mapping), index + 1
            raise _UnreadableEntry
    if char in "\"'":
        end = _scan_quoted(text, index)
        if end < 0:
            raise _UnreadableEntry
        return _read_scalar_text(text[index:end]), end
    if char in "&?":
        raise _UnreadableEntry
    end = index
    while end < len(text) and text[end] not in ",[]{}":
        if text[end] == ":" and (end + 1 == len(text) or text[end + 1] in " ,[]{}"):
            break
        end += 1
    if char in "*!":
        return _OPAQUE, end
    plain = text[index:end].strip(" ")
    return (None if plain in _YAML_NULLS else plain), end


def _read_alice_block(
    doc: _YamlText, lexed: Mapping[int, _YamlLine], start: int, last: int
) -> dict[str, object]:
    """The old mcp_servers.alice entry as a dict, or _UnreadableEntry.

    Reads what install and a hand edit plausibly leave: a block or flow
    mapping of scalars, block or flow lists of scalars, and one nested
    mapping (env). Anything else, including block scalar text, is
    unreadable.
    """

    head = lexed[start]
    body = [
        index
        for index in range(start + 1, last + 1)
        if doc.info[index] is None or index in lexed
    ]
    if head.value not in _YAML_NULLS:
        if body or not head.value.startswith("{"):
            raise _UnreadableEntry
        parsed = _read_scalar_text(head.value)
        if not isinstance(parsed, dict):
            raise _UnreadableEntry
        return parsed
    if any(doc.info[index] is None for index in body):
        raise _UnreadableEntry
    lines = [lexed[index] for index in body]
    result: dict[str, object] = {}
    if not lines:
        return result
    child_indent = lines[0].indent
    position = 0
    while position < len(lines):
        line = lines[position]
        if line.indent != child_indent or line.dash or line.key is None or line.key in result:
            raise _UnreadableEntry
        position += 1
        nested: list[_YamlLine] = []
        while position < len(lines) and (
            lines[position].indent > child_indent
            or (lines[position].indent == child_indent and lines[position].dash)
        ):
            nested.append(lines[position])
            position += 1
        if line.value:
            if nested:
                raise _UnreadableEntry
            result[line.key] = _read_scalar_text(line.value)
        elif not nested:
            result[line.key] = None
        elif nested[0].dash:
            values: list[object] = []
            for item in nested:
                if item.indent != nested[0].indent or not item.dash or item.colon >= 0 or not item.item:
                    raise _UnreadableEntry
                values.append(_read_scalar_text(item.item))
            result[line.key] = values
        else:
            entries: dict[str, object] = {}
            for item in nested:
                if (
                    item.indent != nested[0].indent
                    or item.dash
                    or item.key is None
                    or item.key in entries
                ):
                    raise _UnreadableEntry
                entries[item.key] = _read_scalar_text(item.value)
            result[line.key] = entries
    return result


def _alice_block_extra_keys(block: Mapping[str, object]) -> list[str]:
    """Keys in an old alice entry that install has never written."""

    extra = [key for key in block if key not in _ALICE_INSTALL_KEYS]
    env = block.get("env")
    if isinstance(env, Mapping):
        extra += [f"env.{key}" for key in env if key != ALICE_MEMORY_DATA_DIR_ENV]
    elif env is not None:
        extra.append("env")
    return extra


def _alice_block_data_dir(block: Mapping[str, object]) -> object:
    """The data dir an old alice entry runs with: a str, None, or _OPAQUE."""

    args = block.get("args")
    if isinstance(args, list):
        for index, arg in enumerate(args):
            if arg == "--data-dir":
                if index + 1 < len(args):
                    value = args[index + 1]
                    return value if isinstance(value, str) else _OPAQUE
                return None
            if isinstance(arg, str) and arg.startswith("--data-dir="):
                return arg.split("=", 1)[1]
        if any(not isinstance(arg, str) for arg in args):
            return _OPAQUE
    elif args is not None:
        return _OPAQUE
    env = block.get("env")
    if isinstance(env, Mapping):
        value = env.get(ALICE_MEMORY_DATA_DIR_ENV)
        if isinstance(value, str):
            return value
        if value is not None:
            return _OPAQUE
    return None


@dataclass(frozen=True)
class HermesPlan:
    """What install --host hermes would write, and with which data dir."""

    text: str | None
    data_dir: str
    previous_data_dir: str | None = None


def _plan_hermes(text: str, explicit_dir: str | None, default_dir: str) -> HermesPlan:
    """Plan mcp_servers.alice in ``text``; see plan_hermes_config.

    ``explicit_dir`` is --data-dir when it was passed. Without it, an old
    alice entry keeps the data dir it runs with, else ``default_dir``.
    """

    new_dir = explicit_dir or default_dir
    doc = _scan_yaml_text(text)
    lexed: dict[int, _YamlLine] = {
        index: item
        for index, item in enumerate(doc.info)
        if item is not None and not item.trivia
    }
    content = sorted(lexed)
    top_keys: list[int] = []
    last_value_empty = False
    for index in content:
        item = lexed[index]
        if index == content[0] and item.indent != 0:
            raise HermesConfigRefused(
                "the top level is not a mapping whose keys start in column 0", index + 1
            )
        if item.indent != 0:
            continue
        if item.dash:
            if not top_keys or not last_value_empty:
                raise HermesConfigRefused(
                    "the top level is not a mapping whose keys start in column 0", index + 1
                )
            continue
        if item.colon < 0:
            raise HermesConfigRefused("a top-level line that is not a key", index + 1)
        if item.key is None:
            raise HermesConfigRefused("a key this writer does not read", index + 1)
        if item.key == "<<":
            raise HermesConfigRefused("a merge key (<<) at the top level", index + 1)
        top_keys.append(index)
        last_value_empty = item.value == ""

    lines = list(doc.lines)
    mcp = [index for index in top_keys if lexed[index].key == "mcp_servers"]
    if len(mcp) > 1:
        raise HermesConfigRefused("mcp_servers appears more than once", mcp[1] + 1)
    if not mcp:
        if lines and not doc.final_newline and doc.ends_in_block:
            raise HermesConfigRefused(
                "the file ends inside a block scalar with no final line break", len(lines)
            )
        return HermesPlan(
            _render_yaml_text(
                doc, [*lines, "mcp_servers:", *_hermes_alice_lines(new_dir, 2)], True
            ),
            new_dir,
        )

    head_index = mcp[0]
    head = lexed[head_index]
    if head.block_owner is not None or (head.value and head.value not in _EMPTY_MAPPING_VALUES):
        raise HermesConfigRefused(
            "mcp_servers has an anchor, tag, alias or inline value", head_index + 1
        )
    end = next(
        (index for index in content if index > head_index and lexed[index].indent == 0),
        len(lines),
    )
    if end < len(lines) and lexed[end].dash:
        raise HermesConfigRefused("mcp_servers is not a mapping", end + 1)
    section = [index for index in content if head_index < index < end]
    if head.value and section:
        raise HermesConfigRefused(
            "mcp_servers has an inline value and nested lines", section[0] + 1
        )

    child_indent = lexed[section[0]].indent if section else 2
    children: list[int] = []
    last_child_value_empty = False
    for index in section:
        item = lexed[index]
        if item.indent < child_indent:
            raise HermesConfigRefused("mcp_servers is indented inconsistently", index + 1)
        if item.indent > child_indent:
            continue
        if item.dash:
            if not children or not last_child_value_empty:
                raise HermesConfigRefused("mcp_servers is not a mapping", index + 1)
            continue
        if item.colon < 0:
            raise HermesConfigRefused("a line under mcp_servers that is not a key", index + 1)
        if item.key is None:
            raise HermesConfigRefused("a key this writer does not read", index + 1)
        if item.key == "<<":
            raise HermesConfigRefused("a merge key (<<) under mcp_servers", index + 1)
        children.append(index)
        last_child_value_empty = item.value == ""

    alice = [index for index in children if lexed[index].key == "alice"]
    if len(alice) > 1:
        raise HermesConfigRefused("mcp_servers.alice appears more than once", alice[1] + 1)
    if alice:
        start = alice[0]
        stop = next((index for index in children if index > start), end)
        last = max(index for index in range(start, stop) if doc.substantive(index))
        if any(lexed[index].anchor for index in range(start, last + 1) if index in lexed):
            raise HermesConfigRefused(
                "mcp_servers.alice defines an anchor that other keys may use", start + 1
            )
        try:
            old = _read_alice_block(doc, lexed, start, last)
        except _UnreadableEntry:
            raise HermesConfigRefused(
                "the existing mcp_servers.alice uses YAML this writer does not read",
                start + 1,
                data_dir=explicit_dir,
            ) from None
        old_dir = _alice_block_data_dir(old)
        if explicit_dir is not None:
            new_dir = explicit_dir
        elif isinstance(old_dir, str):
            new_dir = old_dir
        elif old_dir is _OPAQUE:
            raise HermesConfigRefused(
                "the data dir of the existing mcp_servers.alice cannot be read; "
                "run install again with --data-dir",
                start + 1,
            )
        extra = _alice_block_extra_keys(old)
        if extra:
            raise HermesConfigRefused(
                "mcp_servers.alice has keys install did not write",
                start + 1,
                extra_keys=extra,
                data_dir=new_dir,
            )
        payload = mcp_server_payload(new_dir, with_env=True)
        if is_install_shaped_entry(old):
            assert isinstance(old["args"], list)
            payload["command"] = old["command"]
            payload["args"] = _args_with_data_dir(old["args"], new_dir)
        block = _hermes_block_lines(payload, child_indent)
        previous = old_dir if isinstance(old_dir, str) else None
        if lines[start : last + 1] == block:
            return HermesPlan(None, new_dir, previous)
        lines[start : last + 1] = block
        final_newline = doc.final_newline or last == len(doc.lines) - 1
        return HermesPlan(_render_yaml_text(doc, lines, final_newline), new_dir, previous)

    block = _hermes_alice_lines(new_dir, child_indent)

    if head.value:
        rebuilt = lines[head_index][: head.colon + 1]
        lines[head_index] = f"{rebuilt}  {head.comment}" if head.comment else rebuilt
    last = max(
        (index for index in range(head_index + 1, end) if doc.substantive(index)),
        default=head_index,
    )
    last_item = doc.info[last]
    in_block = last_item is None or last_item.block_owner is not None
    if in_block:
        # Blank lines after block scalar text can belong to it (``|+``), so
        # the block goes after them, before the next comment or key.
        insert_at = next(
            (index for index in range(last + 1, end) if lines[index].strip(" \t")), end
        )
    else:
        insert_at = last + 1
    at_eof = insert_at == len(lines)
    if at_eof and not doc.final_newline and in_block:
        raise HermesConfigRefused(
            "the file ends inside a block scalar with no final line break", len(lines)
        )
    lines[insert_at:insert_at] = block
    return HermesPlan(_render_yaml_text(doc, lines, doc.final_newline or at_eof), new_dir)


def plan_hermes_config(text: str, data_dir: str) -> str | None:
    """Return ``text`` with mcp_servers.alice on ``data_dir``, or None when it already is.

    This is install with --data-dir passed. Only the alice lines are added
    or replaced; every other byte is kept. The one other edit is an empty
    ``mcp_servers: {}`` / ``~`` / ``null`` value, which becomes
    ``mcp_servers:`` so the block can go under it. An old alice entry is
    replaced only when its keys are within what install writes (command,
    args, env.ALICE_MEMORY_DATA_DIR); an install-shaped one keeps its
    command and args apart from the data dir. Raises HermesConfigRefused
    for a file outside the subset this scanner reads.
    """

    return _plan_hermes(text, data_dir, data_dir).text


def _backup_host_file(path: Path, original: bytes) -> Path:
    """Write ``original`` next to ``path`` as a new, private, timestamped file.

    The bytes go to a temp file first and are fsynced; only then does the
    backup name appear, as a hard link to the complete file. A write that
    fails partway leaves no file under a backup name, and the temp file is
    removed on every path.
    """

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = f"{path.name}{HERMES_BACKUP_MARKER}{stamp}"
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{base}.", suffix=".alice-install.tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(1, 100):
            candidate = path.with_name(base if attempt == 1 else f"{base}-{attempt}")
            try:
                os.link(temp_path, candidate)
            except FileExistsError:
                continue
            return candidate
        raise InstallError("no free backup file name")
    finally:
        temp_path.unlink(missing_ok=True)


def _plan_hermes_file(
    path: Path, explicit_dir: str | None, default_dir: str
) -> tuple[HermesPlan, bytes | None]:
    """The plan, and the original bytes (None when there is no file)."""

    if path.is_symlink():
        raise HermesConfigRefused(f"{path.name} is a symbolic link")
    new_dir = explicit_dir or default_dir
    if not path.exists():
        return HermesPlan(hermes_snippet(new_dir), new_dir), None
    if not path.is_file():
        raise HermesConfigRefused(f"{path.name} is not a regular file")
    original = path.read_bytes()
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HermesConfigRefused(f"{path.name} is not UTF-8") from exc
    return _plan_hermes(text, explicit_dir, default_dir), original


@dataclass(frozen=True)
class _HostResult:
    receipt: str
    status: str  # "ok", "refused" or "failed"


_FAILED_REASON = "the file could not be read or written"
_DRY_RUN_REFUSAL = "dry run: install would refuse this file; nothing was attempted"


def _refusal_action(dry_run: bool) -> str:
    return "would-refuse" if dry_run else "refused"


def _install_hermes_host(
    *, home: Path, explicit_dir: str | None, default_dir: str, dry_run: bool
) -> _HostResult:
    path = host_file_map(home)["hermes"]["mcp"]
    data_dir = explicit_dir or default_dir

    def receipt(action: str, details: Sequence[str], **extra: Any) -> str:
        return _format_host_receipt(
            host="hermes",
            mcp_path=path,
            hooks_path=None,
            action=action,
            details=details,
            session_start="none",
            data_dir=data_dir,
            **extra,
        )

    details: list[str] = []
    try:
        plan, original = _plan_hermes_file(path, explicit_dir, default_dir)
    except HermesConfigRefused as refusal:
        extra_keys = ", ".join(refusal.extra_keys)
        trailer: list[str] = []
        if extra_keys:
            trailer.append(f"extra_keys: {extra_keys}")
        if dry_run:
            trailer.append(_DRY_RUN_REFUSAL)
        else:
            keep = f", keeping your {extra_keys}" if extra_keys else ""
            trailer.append(
                f"next: {path.name} was not changed. Add the alice entry above under "
                f"mcp_servers by hand{keep}, then check it with: hermes mcp list"
            )
        return _HostResult(
            receipt(
                _refusal_action(dry_run),
                (f"reason: {refusal.detail}",),
                snippet=hermes_snippet(refusal.data_dir or data_dir),
                trailer=trailer,
            ),
            "refused",
        )
    except OSError:
        return _HostResult(
            receipt("failed", (f"file: {path}", f"reason: {_FAILED_REASON}"), snippet=None),
            "failed",
        )

    new_text = plan.text
    data_dir = plan.data_dir
    snippet = hermes_snippet(data_dir)
    if plan.previous_data_dir is not None and plan.previous_data_dir != data_dir:
        details.append(f"data_dir: {plan.previous_data_dir} -> {data_dir}")
    if dry_run:
        if new_text is None:
            details.append("planned: unchanged")
        elif original is None:
            details.append("planned: create")
        else:
            details.append("planned: edit, with a backup first")
        return _HostResult(receipt("dry-run", details, snippet=snippet), "ok")
    if new_text is None:
        return _HostResult(receipt("unchanged", details, snippet=None), "ok")
    try:
        if original is not None:
            details.append(f"backup: {_backup_host_file(path, original)}")
        _write_text(path, new_text, newline="")
    except (OSError, InstallError):
        return _HostResult(
            receipt(
                "failed",
                (f"file: {path}", f"reason: {_FAILED_REASON}", *details),
                snippet=None,
            ),
            "failed",
        )
    return _HostResult(receipt("written", details, snippet=None), "ok")


def _package_version() -> str:
    if __version__ and "+source" not in __version__:
        return __version__
    for parent in Path(__file__).resolve().parents:
        pyproject = parent / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            loaded = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            break
        version = loaded.get("project", {}).get("version")
        if isinstance(version, str) and version:
            return version
        break
    committed = committed_mcpb_manifest_path()
    if committed is not None:
        try:
            loaded_manifest = json.loads(committed.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded_manifest = None
        if isinstance(loaded_manifest, Mapping):
            version = loaded_manifest.get("version")
            if isinstance(version, str) and version:
                return version
    return "0.0.0"


def committed_mcpb_manifest_path() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "packaging" / "mcpb" / MCPB_MANIFEST_NAME
        if candidate.is_file():
            return candidate
    return None


def build_mcpb_manifest(version: str | None = None) -> dict[str, object]:
    resolved = version or _package_version()
    return {
        "manifest_version": "0.3",
        "name": "alice-memory",
        "display_name": "Alice Memory",
        "version": resolved,
        "description": (
            "Local-first memory for AI agents. Import is a source. Commit is a fact."
        ),
        "author": {"name": MCPB_AUTHOR_NAME, "url": MCPB_HOMEPAGE},
        "homepage": MCPB_HOMEPAGE,
        "server": {
            "type": "binary",
            "entry_point": "uvx",
            "mcp_config": {
                "command": "uvx",
                "args": [
                    "alice-memory",
                    "mcp",
                    "--data-dir",
                    "${user_config.data_dir}",
                ],
            },
        },
        "user_config": {
            "data_dir": {
                "type": "directory",
                "title": "Alice data directory",
                "description": "Local SQLite vault directory. Defaults to ~/.alice.",
                "default": "${HOME}/.alice",
                "required": False,
            }
        },
    }


def write_mcpb_bundle(path: Path, *, dry_run: bool) -> str:
    target = Path(path)
    if target.suffix != MCPB_SUFFIX:
        raise InstallError("mcpb path must end in .mcpb")
    if target.exists() and target.is_dir():
        raise InstallError("mcpb path is a directory")
    manifest = build_mcpb_manifest()
    snippet = _dump_json(manifest)
    if dry_run:
        return "\n".join(
            (
                f"mcpb: {target}",
                "action: dry-run",
                "snippet:",
                snippet.rstrip(),
            )
        )
    _ensure_private_parents(target)
    try:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MCPB_MANIFEST_NAME, snippet)
    except OSError as exc:
        raise InstallError("mcpb zip could not be written") from exc
    return "\n".join((f"mcpb: {target}", "action: written"))


def _plan_hosts(hosts: Sequence[str] | None) -> list[str]:
    if not hosts:
        return list(DEFAULT_INSTALL_HOSTS)
    planned: list[str] = []
    seen: set[str] = set()
    for host in hosts:
        if host not in INSTALL_HOSTS:
            raise InstallError("unknown host")
        if host in seen:
            continue
        seen.add(host)
        planned.append(host)
    return planned


def _format_host_receipt(
    *,
    host: str,
    mcp_path: Path,
    hooks_path: Path | None,
    action: str,
    session_start: str,
    snippet: str | None,
    data_dir: str,
    details: Sequence[str] = (),
    trailer: Sequence[str] = (),
) -> str:
    lines = [
        f"host: {host}",
        f"path: {mcp_path}",
        f"action: {action}",
        *details,
        f"session_start: {session_start}",
    ]
    if hooks_path is not None:
        lines.append(f"session_start_path: {hooks_path}")
    if host in {"openclaw", "hermes"}:
        lines.append(f"note: {BRIEF_HINT}")
    if snippet is not None:
        lines.append("snippet:")
        lines.append(snippet.rstrip())
    lines.extend(trailer)
    if host == "openclaw":
        lines.append(openclaw_add_line(data_dir))
    return "\n".join(lines)


def _alice_entry_snippet(host: str, payload: Mapping[str, object]) -> str:
    """The JSON a user would paste to add alice by hand."""

    doc: dict[str, Any] = {}
    _set_nested(doc, _alice_server_keys(host), dict(payload))
    return _dump_json(doc)


def _resolved_dir(raw: str | None, home: Path) -> str | None:
    return None if raw is None else str(resolve_user_path(raw, home))


def _install_json_host(
    host: str,
    *,
    home: Path,
    explicit_dir: str | None,
    default_dir: str,
    dry_run: bool,
) -> _HostResult:
    """Plan both files of one JSON host, then write them; never raise for one host.

    A file that does not parse, or whose structure is malformed, refuses the
    whole host and nothing is written for it. An alice entry that install did
    not write refuses the MCP file; an existing Alice hook is still repaired
    with its own --data-dir, and no hook is added. An OSError fails the host
    with a static message naming the file.
    """

    files = host_file_map(home)[host]
    mcp_path = files["mcp"]
    hooks_path = files.get("hooks") if host in _SESSION_START_HOSTS else None
    details: list[str] = []
    session_start = "none"
    data_dir = explicit_dir or default_dir

    def receipt(action: str, **extra: Any) -> str:
        return _format_host_receipt(
            host=host,
            mcp_path=mcp_path,
            hooks_path=hooks_path,
            action=action,
            details=details,
            session_start=session_start,
            data_dir=data_dir,
            **extra,
        )

    current = mcp_path
    try:
        mcp_raw = _read_host_file(mcp_path)
        mcp_doc = _parse_json_host(mcp_raw)
        container = _alice_container(mcp_doc, host)
        hooks_raw: bytes | None = None
        hooks_doc: dict[str, Any] | None = None
        if hooks_path is not None:
            current = hooks_path
            hooks_raw = _read_host_file(hooks_path)
            hooks_doc = _parse_json_host(hooks_raw)
            _check_hooks_file(hooks_doc, host)

        existing = container.get("alice") if container is not None else None
        foreign = existing is not None and not is_install_shaped_entry(existing)
        existing_dir = _entry_data_dir(existing) if existing is not None and not foreign else None
        hook_command = (
            _existing_alice_hook_command(hooks_doc, host) if hooks_doc is not None else None
        )
        hook_dir = _hook_command_data_dir(hook_command) if hook_command else None
        data_dir = (
            explicit_dir
            or _resolved_dir(existing_dir, home)
            or _resolved_dir(hook_dir, home)
            or default_dir
        )

        refusal: str | None = None
        mcp_text: str | None = None
        if foreign:
            refusal = (
                f"{'.'.join(_alice_server_keys(host))} exists and install did not write it "
                f"({_describe_entry(existing)})"
            )
        elif existing is None:
            _set_nested(mcp_doc, _alice_server_keys(host), mcp_server_payload(data_dir, with_env=False))
            mcp_text = _dump_json(mcp_doc)
        else:
            assert container is not None and isinstance(existing, Mapping)
            entry = dict(existing)
            if explicit_dir is not None and _resolved_dir(existing_dir, home) != explicit_dir:
                entry["args"] = _args_with_data_dir(entry["args"], explicit_dir)
                details.append(f"data_dir: {existing_dir or '(not set)'} -> {explicit_dir}")
            kept = _kept_keys(entry)
            if kept:
                details.append("kept: " + ", ".join(kept))
            container["alice"] = entry
            mcp_text = _dump_json(mcp_doc)

        hooks_text: str | None = None
        if hooks_path is not None and hooks_doc is not None:
            command = hook_command if foreign else session_start_hook_command(data_dir)
            if command is not None:
                if not hooks_doc:
                    hooks_doc = _new_hooks_document(host, command)
                    session_start = "added"
                else:
                    session_start = _merge_session_start(hooks_doc, host, command)
                hooks_text = _dump_json(hooks_doc)
    except _MalformedHostFile as problem:
        details[:] = [f"reason: {problem}", f"file: {current}"]
        session_start = "none"
        trailer = (
            _DRY_RUN_REFUSAL
            if dry_run
            else (
                f"next: nothing was written for {host}. Fix {current.name}, "
                "then run install again."
            ),
        )
        return _HostResult(
            receipt(_refusal_action(dry_run), snippet=None, trailer=trailer), "refused"
        )
    except OSError:
        details[:] = [f"file: {current}", f"reason: {_FAILED_REASON}"]
        return _HostResult(receipt("failed", snippet=None), "failed")

    if refusal is not None:
        details.insert(0, f"reason: {refusal}")
    snippet_for_refusal = _alice_entry_snippet(host, mcp_server_payload(data_dir, with_env=False))

    if dry_run:
        if hooks_text is not None:
            session_start = "planned"
        if refusal is not None:
            return _HostResult(
                receipt(
                    _refusal_action(True),
                    snippet=snippet_for_refusal,
                    trailer=(_DRY_RUN_REFUSAL,),
                ),
                "refused",
            )
        assert mcp_text is not None
        snippet = mcp_text
        if hooks_text is not None:
            snippet = f"{mcp_text.rstrip()}\n---\n{hooks_text.rstrip()}\n"
        return _HostResult(receipt("dry-run", snippet=snippet), "ok")

    action = "refused" if refusal is not None else "unchanged"
    try:
        if mcp_text is not None and (mcp_raw is None or mcp_text.encode("utf-8") != mcp_raw):
            current = mcp_path
            if mcp_raw is not None:
                details.append(f"backup: {_backup_host_file(mcp_path, mcp_raw)}")
            _write_text(mcp_path, mcp_text)
            action = "written"
        if hooks_path is not None and hooks_text is not None and (
            hooks_raw is None or hooks_text.encode("utf-8") != hooks_raw
        ):
            current = hooks_path
            if hooks_raw is not None:
                details.append(f"session_start_backup: {_backup_host_file(hooks_path, hooks_raw)}")
            _write_text(hooks_path, hooks_text)
    except (OSError, InstallError):
        details[:0] = [f"file: {current}", f"reason: {_FAILED_REASON}"]
        return _HostResult(receipt("failed", snippet=None), "failed")

    if refusal is not None:
        return _HostResult(
            receipt(
                "refused",
                snippet=snippet_for_refusal,
                trailer=(
                    f"next: {mcp_path.name} was not changed. Rename or remove that alice "
                    "entry, or add the entry above under a different name by hand.",
                ),
            ),
            "refused",
        )
    return _HostResult(receipt(action, snippet=None), "ok")


def _uvx_on_path() -> bool:
    return shutil.which(MCP_COMMAND) is not None


def _install_mcpb(path: Path, *, dry_run: bool) -> _HostResult:
    try:
        return _HostResult(write_mcpb_bundle(path, dry_run=dry_run), "ok")
    except InstallError as problem:
        reason = str(problem)
    except OSError:
        reason = _FAILED_REASON
    return _HostResult("\n".join((f"mcpb: {path}", "action: failed", f"reason: {reason}")), "failed")


def run_host_install(
    *,
    home: str | None,
    data_dir: str | None,
    hosts: Sequence[str] | None,
    dry_run: bool,
    write_mcpb: str | None,
) -> str:
    """Install every planned host and return the receipts.

    ``data_dir`` is None when --data-dir was not passed; each host then keeps
    the data dir its existing Alice entry uses, else ~/.alice. One host that
    is refused or fails does not stop the others. Raises InstallFailed when
    any host failed, else InstallRefused when any host was refused; both
    carry every receipt.
    """

    resolved_home = resolve_home(home)
    explicit_dir = (
        str(resolve_user_path(data_dir, resolved_home)) if data_dir is not None else None
    )
    default_dir = str(resolve_user_path(DEFAULT_DATA_DIR, resolved_home))
    results: list[_HostResult] = []
    for host in _plan_hosts(hosts):
        if host == "hermes":
            results.append(
                _install_hermes_host(
                    home=resolved_home,
                    explicit_dir=explicit_dir,
                    default_dir=default_dir,
                    dry_run=dry_run,
                )
            )
        else:
            results.append(
                _install_json_host(
                    host,
                    home=resolved_home,
                    explicit_dir=explicit_dir,
                    default_dir=default_dir,
                    dry_run=dry_run,
                )
            )
    if write_mcpb:
        results.append(_install_mcpb(Path(write_mcpb), dry_run=dry_run))
    blocks = [result.receipt for result in results]
    if not _uvx_on_path():
        # A warning, not a refusal: the files are still right once uv is installed.
        blocks.insert(0, UVX_MISSING_WARNING)
    output = "\n\n".join(blocks)
    statuses = {result.status for result in results}
    if "failed" in statuses:
        raise InstallFailed(output)
    if "refused" in statuses:
        raise InstallRefused(output)
    return output


__all__ = [
    "ALICE_MEMORY_DATA_DIR_ENV",
    "BRIEF_HINT",
    "DEFAULT_INSTALL_HOSTS",
    "HERMES_BACKUP_MARKER",
    "HermesConfigRefused",
    "HermesPlan",
    "INSTALL_HOSTS",
    "InstallError",
    "InstallFailed",
    "InstallRefused",
    "SESSION_START_COMMAND",
    "UVX_MISSING_WARNING",
    "build_mcpb_manifest",
    "claude_code_session_start_group",
    "claude_code_session_start_handler",
    "claude_desktop_config_path",
    "committed_mcpb_manifest_path",
    "hermes_snippet",
    "host_file_map",
    "is_install_shaped_entry",
    "mcp_server_payload",
    "openclaw_add_line",
    "plan_hermes_config",
    "resolve_home",
    "resolve_user_path",
    "run_host_install",
    "session_start_hook_command",
    "session_start_hook_entry",
    "write_mcpb_bundle",
]
