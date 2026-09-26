"""Write host MCP config for ``alice-memory install``.

Install writes host files. It does not import a vault, start an agent
runtime, or call ``openclaw``. Import stays a source. Commit stays a fact.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alicebot_api import __version__
from alicebot_api.host_launcher import (
    DOCS_DATA_DIR_PLACEHOLDER,
    FIRST_SESSION_START_VERSION,
    HIDDEN,
    MCP_COMMAND,
    MCP_SCRIPT,
    SESSION_START_COMMAND,
    UVX_LAUNCHER,
    UVX_MISSING_WARNING_PREFIX,
    UV_TEMP_ENV_WARNING,
    HookDataDir,
    Launcher,
    LauncherSearch,
    find_launcher,
    format_command,
    hook_command,
    hook_script_problem,
    is_install_shaped_entry,
    is_session_start_command,
    last_option,
    launcher_in_uv_cache,
    launcher_problem,
    mask_text,
    masked_args,
    parse_launcher,
    read_hook_data_dir,
    replace_hook_data_dir,
    shell_path,
    shown_hook_words,
    split_command,
)

ALICE_MEMORY_DATA_DIR_ENV = "ALICE_MEMORY_DATA_DIR"
DEFAULT_DATA_DIR = "~/.alice"
MCPB_SUFFIX = ".mcpb"
MCPB_MANIFEST_NAME = "manifest.json"
MCPB_HOMEPAGE = "https://www.alicememory.com"
MCPB_AUTHOR_NAME = "Sami Rusani"
BRIEF_HINT = (
    "Run alice-memory brief or alice-memory-session-start --format markdown"
)
INSTALL_HOSTS = (
    "claude-desktop",
    "claude-code",
    "cursor",
    "openclaw",
    "hermes",
    "opencode",
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


def host_file_map(
    home: Path, platform: str | None = None, *, config_home: Path | None = None
) -> dict[str, dict[str, Path]]:
    """Host config paths under ``home``.

    ``config_home`` is OpenCode's config directory parent. It defaults to
    ``home / ".config"`` on every platform, including Windows.
    """

    opencode_config = home / ".config" if config_home is None else config_home
    opencode_dir = opencode_config / "opencode"
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
        "opencode": {
            "mcp": opencode_dir / "opencode.json",
            "jsonc": opencode_dir / "opencode.jsonc",
            "legacy": opencode_dir / "config.json",
            "home_json": home / ".opencode" / "opencode.json",
            "home_jsonc": home / ".opencode" / "opencode.jsonc",
        },
    }


def mcp_server_payload(
    data_dir: str, *, with_env: bool, launcher: Launcher = UVX_LAUNCHER
) -> dict[str, object]:
    payload: dict[str, object] = {
        "command": launcher.command,
        "args": [*launcher.prefix, "--data-dir", data_dir],
    }
    if with_env:
        payload["env"] = {ALICE_MEMORY_DATA_DIR_ENV: data_dir}
    return payload


def openclaw_add_line(
    data_dir: str, launcher: Launcher = UVX_LAUNCHER, *, hide_values: bool = False
) -> str:
    """The one-line ``openclaw mcp add``, quoted for the shell like the hooks.

    On Windows it follows the hook rules: forward slashes, and no line at
    all when a word could not be written the same for cmd, PowerShell and
    Git Bash; a note with the argv takes its place. ``hide_values`` shows
    every URL after its scheme and secret flag values as <hidden>, for a receipt.
    """

    prefix = masked_args(launcher.prefix)[0] if hide_values else list(launcher.prefix)
    command = mask_text(launcher.command) if hide_values else launcher.command
    argv = ["openclaw", "mcp", "add", "alice", "--command", shell_path(command)]
    for arg in (*prefix, "--data-dir", shell_path(data_dir)):
        argv += ["--arg", arg]
    text, problem = format_command(argv)
    if problem is None:
        return text
    return (
        f"note: install did not print an openclaw mcp add line ({problem}); add the server "
        f"with this argv: {json.dumps(argv)}"
    )


def session_start_hook_command(data_dir: str, launcher: Launcher = UVX_LAUNCHER) -> str:
    """SessionStart command that still works after ``uvx alice-memory install`` exits."""

    text, problem = hook_command(launcher, data_dir)
    if problem is not None:
        raise InstallError(problem)
    return text


def _cursor_session_start_item(command: str) -> dict[str, str]:
    return {"command": command}


def session_start_hook_entry(data_dir: str) -> dict[str, str]:
    """Cursor's ``hooks.sessionStart`` item: a flat ``{"command": ...}`` object."""

    return _cursor_session_start_item(session_start_hook_command(data_dir))


def claude_code_session_start_handler(command: str) -> dict[str, str]:
    """One Claude Code hook handler that runs ``command``. Claude Code requires ``type``."""

    return {"type": "command", "command": command}


def claude_code_session_start_group(command: str) -> dict[str, object]:
    """Claude Code's ``hooks.SessionStart`` item: a matcher group.

    Claude Code nests handlers under ``hooks``. It ignores Cursor's flat
    ``{"command": ...}`` item with ``Hook matcher "hooks" must be an array of
    hook entries``. ``matcher`` is omitted, which Claude Code documents as
    matching every SessionStart source.
    """

    return {"hooks": [claude_code_session_start_handler(command)]}


def _is_session_start_command(command: str) -> bool:
    return is_session_start_command(command)


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
    if host == "opencode":
        return ("mcp", "alice")
    return ("mcpServers", "alice")


def _read_host_file(path: Path) -> bytes | None:
    """The file's bytes, or None when it does not exist. OSError propagates."""

    if not path.exists():
        return None
    return path.read_bytes()


def _parse_json_host(raw: bytes | None) -> dict[str, Any]:
    """A JSON host file as a dict. An absent or empty file is ``{}``.

    Any ValueError from decoding or parsing, and RecursionError from JSON
    nested too deeply, make the file malformed, which refuses only its host.
    """

    if not raw:
        return {}
    try:
        loaded = json.loads(raw.decode("utf-8"))
    except RecursionError as exc:
        raise _MalformedHostFile("the file is nested too deeply to read") from exc
    except ValueError as exc:
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
    entry = _cursor_session_start_item(command)
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
                if isinstance(item, Mapping) and isinstance(item.get("command"), str):
                    updated = dict(item)
                    updated["command"] = command
                    kept.append(updated)
                else:
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
    desired = claude_code_session_start_handler(command)
    group = claude_code_session_start_group(command)
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
        return {"version": 1, "hooks": {"sessionStart": [_cursor_session_start_item(command)]}}
    return {"hooks": {"SessionStart": [claude_code_session_start_group(command)]}}


# --- JSON hosts: re-runs keep what the user set --------------------------------
#
# An alice entry install wrote (see host_launcher.parse_launcher) is kept key
# for key on a re-run: env, type, timeout, cwd all stay. Its data dir is the
# one alice-memory mcp opens: the last --data-dir in its args, else ~/.alice.
# Its launcher stays while it can run here; a dead one is swapped for the
# working launcher install found, unless it is pinned or customised. Any
# other alice entry is not install's to rewrite, so that file is left alone
# and the host is refused.


def _kept_keys(entry: Mapping[str, Any], *, command_kept: bool) -> list[str]:
    """The user's keys in an install-shaped entry, for the receipt."""

    kept = ["command"] if command_kept and entry.get("command") != MCP_COMMAND else []
    for key, value in entry.items():
        if key in {"command", "args"}:
            continue
        kept.append(f"{key} ({len(value)} keys)" if isinstance(value, Mapping) else str(key))
    return kept


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


def _alice_hook_items(doc: Mapping[str, Any], host: str) -> list[object]:
    """Only Alice's SessionStart items, for a dry-run receipt."""

    hooks = doc.get("hooks")
    items = hooks.get("SessionStart" if host == "claude-code" else "sessionStart") if isinstance(
        hooks, Mapping
    ) else None
    found: list[object] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        handlers = item.get("hooks")
        if host == "claude-code" and isinstance(handlers, list):
            alice = [handler for handler in handlers if _is_alice_hook_item(handler)]
            if alice:
                found.append({**item, "hooks": alice})
        elif _is_alice_hook_item(item):
            found.append(item)
    return found


_HIDDEN = HIDDEN
# Keys whose values install prints as they are; every other value from the
# user's entry is hidden.
_SHOWN_KEYS = frozenset({"command", "type", "timeout", "cwd"})


def _masked(
    entry: Mapping[str, Any], *, own_env: Mapping[str, str] | None = None
) -> tuple[dict[str, Any], list[str]]:
    """``entry`` as install prints it, and the list of what it hid.

    Shown: command, type, timeout and cwd; args with every URL after its
    scheme and secret flag values hidden (host_launcher.masked_args); the
    names under env, headers and other maps. Every other value is hidden,
    except ``own_env``: the ALICE_MEMORY_DATA_DIR install writes itself,
    which equals the --data-dir in args.
    """

    shown: dict[str, Any] = {}
    hidden: list[str] = []
    for key, value in entry.items():
        if key == "command" and isinstance(value, list) and all(isinstance(arg, str) for arg in value):
            args, what = masked_args(value)
            shown[key] = args
            hidden.extend(f"command ({item})" for item in what)
        elif key in _SHOWN_KEYS:
            shown[key] = value
        elif key == "args" and isinstance(value, list) and all(isinstance(arg, str) for arg in value):
            args, what = masked_args(value)
            shown[key] = args
            hidden.extend(f"args ({item})" for item in what)
        elif isinstance(value, Mapping):
            inner: dict[str, Any] = {}
            for name, item in value.items():
                if key == "env" and own_env is not None and own_env.get(name) == item:
                    inner[name] = item
                else:
                    inner[name] = _HIDDEN
                    hidden.append(f"{key}.{name}")
            shown[key] = inner
        else:
            shown[key] = _HIDDEN
            hidden.append(str(key))
    return shown, list(dict.fromkeys(hidden))


def _keep_line(hidden: Sequence[str]) -> str:
    return (
        f"keep: install printed these values from your file as <hidden>: {', '.join(hidden)}; "
        "copy them from your existing alice entry"
    )


def _hidden_label_in_entry(label: str, file_keys: frozenset[str]) -> bool:
    """True when ``label`` names a value the existing entry has.

    ``file_keys`` stores ``args``. ``_masked`` records a URL inside that
    list as ``args (a URL (everything after its scheme))``, and a secret
    flag value as ``args (the value of --flag)``. The text before `` (``
    is the key.
    """

    if label in file_keys:
        return True
    key, opened, _detail = label.partition(" (")
    return bool(opened) and label.endswith(")") and key in file_keys


def _hidden_line(hidden: Sequence[str]) -> str:
    return f"hidden: install printed these values from your file as <hidden>: {', '.join(hidden)}"


def _masked_hook_command(command: str, hidden: list[str]) -> str:
    """A hook command for printing: only allowlisted words shown
    (host_launcher.shown_hook_words), every other word <hidden>."""

    shown, count = shown_hook_words(split_command(command))
    if count:
        hidden.append(f"hook command ({count} word{'s' if count != 1 else ''} install does not print)")
    return mask_text(" ".join(shown))


def _masked_hook_item(item: object, hidden: list[str]) -> object:
    """An Alice hook item for printing: its command masked, and every key but
    command, type and hooks shown as <hidden>."""

    if isinstance(item, Mapping):
        shown: dict[str, object] = {}
        for key, value in item.items():
            if key == "command" and isinstance(value, str):
                shown[key] = _masked_hook_command(value, hidden)
            elif key == "hooks":
                shown[key] = _masked_hook_item(value, hidden)
            elif key == "type":
                shown[key] = value
            else:
                shown[key] = _HIDDEN
                hidden.append(f"hook {key}")
        return shown
    if isinstance(item, list):
        return [_masked_hook_item(value, hidden) for value in item]
    return item


def _own_env(payload: Mapping[str, object]) -> dict[str, str]:
    """The env value install writes for ``payload``: ALICE_MEMORY_DATA_DIR = its --data-dir."""

    args = payload.get("args")
    if isinstance(args, list) and all(isinstance(arg, str) for arg in args):
        value = last_option(args, "--data-dir")
        if value is not None:
            return {ALICE_MEMORY_DATA_DIR_ENV: value}
    return {}


def _host_target(path: Path) -> Path:
    """Where writes to ``path`` go: the path itself, or a symlink's target.

    Writing through keeps a dotfiles symlink intact. A dangling or looping
    link, or one that points at something other than a regular file, makes
    the host file malformed.
    """

    if not path.is_symlink():
        return path
    try:
        target = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _MalformedHostFile(
            f"{path.name} is a symbolic link whose target is missing or loops"
        ) from exc
    if not target.is_file():
        raise _MalformedHostFile(f"{path.name} links to something that is not a regular file")
    return target


@dataclass
class _JsonFile:
    """One JSON host file: where writes go, what is there, and the planned doc."""

    path: Path
    target: Path
    raw: bytes | None
    doc: dict[str, Any]
    original: dict[str, Any]

    def changed(self) -> bool:
        """True when the planned doc differs, as parsed JSON, from the file."""

        return self.raw is None or self.doc != self.original


def _load_json_file(path: Path) -> _JsonFile:
    target = _host_target(path)
    raw = _read_host_file(target)
    return _JsonFile(path, target, raw, _parse_json_host(raw), _parse_json_host(raw))


@dataclass
class _EntryPlan:
    """What install will do with one alice entry."""

    entry: dict[str, Any] | None  # None: leave the entry as it is (refused, or kept)
    launcher: Launcher  # what the entry runs after install
    data_dir: str  # the dir alice-memory mcp opens after install
    db: str | None  # the entry's --db, when it has one
    refusal: str | None
    details: list[str]
    used_fallback: bool  # the entry runs a launcher install could not confirm here
    # How the SessionStart hook follows: "follow" (launcher and data dir),
    # "own-dir" (launcher only; a --db entry), or "repair" (shape only).
    hook_mode: str = "follow"
    # For a refusal: the entry to paste by hand, and what to do next, when
    # install's own entry on data_dir is the wrong thing to offer.
    paste: dict[str, Any] | None = None
    next_step: str | None = None
    # The launcher the entry ran before install replaced it, if it did.
    replaced: Launcher | None = None


_FOREIGN_NEXT = (
    "Rename or remove that alice entry, or add the entry above under a different name by hand."
)


@dataclass(frozen=True)
class _Store:
    """The store an entry's ``alice-memory mcp`` opens, read with the server's own parser."""

    data_dir: str | None  # absolute and resolved like the server; None with --db or a problem
    raw_dir: str | None  # the --data-dir value as written, None when there is none
    db: str | None  # the --db value as written
    count: int  # how many args bind to --data-dir
    problem: str | None = None
    relative: bool = False


def _mcp_option_strings() -> list[str]:
    """Every option string ``alice-memory mcp`` accepts, from the real parser."""

    from alicebot_api.onramp import build_parser  # onramp imports this module

    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            mcp = action.choices["mcp"]
            return [option for sub in mcp._actions for option in sub.option_strings]
    raise AssertionError("alice-memory has no mcp subcommand")


def _option_spans(tokens: Sequence[str], options: Sequence[str]) -> list[tuple[str, int, int]]:
    """(option, start, stop) for args that parsed: argparse's exact, = and prefix rules.

    Only called on args ``alice-memory mcp`` accepted, where every word is an
    option with its value, joined by = or in the next word.
    """

    spans: list[tuple[str, int, int]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name = token.split("=", 1)[0] if token.startswith("--") else token
        if name in options:
            option = name
        else:
            matches = [candidate for candidate in options if candidate.startswith(name)]
            option = matches[0] if len(matches) == 1 else token
        step = 1 if "=" in token and token.startswith("--") else 2
        spans.append((option, index, index + step))
        index += step
    return spans


def _store_args(
    server_args: Sequence[str], *, data_dir: str | None, drop_db: bool = False
) -> list[str]:
    """``server_args`` with every --data-dir (any spelling) replaced by one ``data_dir``."""

    out: list[str] = []
    insert_at: int | None = None
    for option, start, stop in _option_spans(server_args, _mcp_option_strings()):
        if option == "--data-dir":
            if insert_at is None:
                insert_at = len(out)
            continue
        if drop_db and option == "--db":
            continue
        out.extend(server_args[start:stop])
    if data_dir is not None:
        position = len(out) if insert_at is None else insert_at
        out[position:position] = ["--data-dir", data_dir]
    return out


def _expand_user(raw: str, home: Path) -> str:
    if raw == "~":
        return str(home)
    if raw.startswith("~/") or raw.startswith("~\\"):
        return str(home / raw[2:])
    return raw


def _entry_store(server_args: Sequence[str], home: Path) -> _Store:
    """Parse ``server_args`` the way ``alice-memory mcp`` does, and resolve its store.

    The real parser and resolve_db_path decide, so abbreviations
    (``--data``), ``=`` forms, repeated flags and ``--db`` read as the server
    reads them. ``~`` expands against ``home``. A relative --data-dir is a
    problem: the server resolves it against the host's working directory,
    which install cannot know.
    """

    from alicebot_api.onramp import build_parser, resolve_db_path

    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            args = build_parser().parse_args(["mcp", *server_args])
    except SystemExit:
        # The args are the user's: print them the way every receipt line is printed.
        shown = " ".join(masked_args(server_args)[0]) or "(none)"
        return _Store(None, None, None, 0, f"alice-memory mcp would not start with the args {shown}")
    spans = _option_spans(server_args, _mcp_option_strings())
    count = sum(1 for option, _, _ in spans if option == "--data-dir")
    raw_dir = args.data_dir if count else None
    if args.db is not None:
        return _Store(None, raw_dir, args.db, count)
    expanded = _expand_user(args.data_dir, home)
    if not Path(expanded).is_absolute():
        return _Store(
            None,
            raw_dir,
            None,
            count,
            f"--data-dir {args.data_dir} is a relative path, which alice-memory mcp resolves "
            "against the host's working directory; install cannot know that directory",
            relative=True,
        )
    data_dir = str(resolve_db_path(data_dir=expanded, db=None).parent)
    return _Store(data_dir, raw_dir, None, count)


def _visible_dir(entry: object, home: Path) -> str | None:
    """The data dir an entry's ``mcp`` args open, when the server's parser can read them."""

    if not isinstance(entry, Mapping):
        return None
    args = entry.get("args")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return None
    if "mcp" not in args:
        return None
    store = _entry_store(args[args.index("mcp") + 1 :], home)
    return store.data_dir if store.problem is None and store.db is None else None


def _plan_entry(
    existing: object,
    *,
    key_label: str,
    explicit_dir: str | None,
    new_entry_dir: str,
    default_dir: str,
    home: Path,
    search: LauncherSearch,
    with_env: bool,
    needs_hook: bool = True,
    problem_of: Callable[[Launcher], str | None] | None = None,
) -> _EntryPlan:
    """Plan one alice entry: shape, store, launcher. Shared by every host.

    ``new_entry_dir`` is the data dir for a new entry when --data-dir was
    not passed (an existing Alice hook's, else ~/.alice). ``needs_hook``
    says the host runs a SessionStart hook, so a script launcher needs its
    alice-memory-session-start beside it to count as alive.
    """

    if problem_of is None:

        def problem_of(launcher: Launcher) -> str | None:
            return launcher_problem(launcher, needs_hook=needs_hook)

    details: list[str] = []
    working = search.launcher
    if existing is None:
        launcher = working or UVX_LAUNCHER
        data_dir = explicit_dir or new_entry_dir
        problem = None if working is not None else problem_of(launcher)
        note = f" (cannot run here: {problem})" if problem else ""
        details.append(f"launcher: {launcher.describe()}{note}")
        entry = mcp_server_payload(data_dir, with_env=with_env, launcher=launcher)
        return _EntryPlan(entry, launcher, data_dir, None, None, details, problem is not None)

    parsed = parse_launcher(existing)
    if parsed is None:
        visible = None if explicit_dir is not None else _visible_dir(existing, home)
        return _EntryPlan(
            None,
            working or UVX_LAUNCHER,
            explicit_dir or visible or new_entry_dir,
            None,
            f"{key_label} exists and install did not write it ({_describe_entry(existing)})",
            details,
            False,
            hook_mode="repair",
            next_step=_FOREIGN_NEXT,
        )
    launcher, server_args = parsed
    assert isinstance(existing, Mapping)  # nosec B101 # narrows the type for mypy; the existing is None branch above returned
    store = _entry_store(server_args, home)

    if store.problem is not None and not store.relative:
        if explicit_dir is not None:
            return _EntryPlan(
                None,
                launcher,
                explicit_dir,
                None,
                f"{key_label}: {store.problem}, so install cannot move its data dir",
                details,
                False,
                hook_mode="repair",
                next_step="Fix that entry's args, or replace the entry with the one above.",
            )
        details.append(
            f"warning: {store.problem}; install left this entry, and its SessionStart hook, "
            "as they were"
        )
        return _EntryPlan(None, launcher, default_dir, None, None, details, False, "repair")

    if store.relative and explicit_dir is None:
        assert store.raw_dir is not None  # nosec B101 # narrows the type for mypy; _entry_store builds a relative store from its raw dir
        marker = f"<an absolute path for {store.raw_dir}>"
        paste = dict(existing)
        paste["args"] = [*launcher.prefix, *_store_args(server_args, data_dir=marker)]
        if with_env:
            paste["env"] = {ALICE_MEMORY_DATA_DIR_ENV: marker}
        return _EntryPlan(
            None,
            launcher,
            default_dir,
            None,
            f"{key_label}: {store.problem}",
            details,
            False,
            hook_mode="repair",
            paste=paste,
            next_step=(
                "Put an absolute --data-dir in that entry (the entry above marks where), "
                "or run install with --data-dir."
            ),
        )

    if store.db is not None and explicit_dir is not None:
        # The paste is this entry on the dir the user asked for, never
        # install's entry on ~/.alice, which may be an empty store.
        paste = dict(existing)
        paste["args"] = [
            *launcher.prefix,
            *_store_args(server_args, data_dir=explicit_dir, drop_db=True),
        ]
        if with_env:
            env_map = existing.get("env")
            paste["env"] = {
                **(dict(env_map) if isinstance(env_map, Mapping) else {}),
                ALICE_MEMORY_DATA_DIR_ENV: explicit_dir,
            }
        return _EntryPlan(
            None,
            launcher,
            default_dir,
            store.db,
            f"{key_label} opens the database --db {store.db}, which --data-dir does not move; "
            "run install without --data-dir, or remove --db from the entry",
            details,
            False,
            hook_mode="repair",
            paste=paste,
            next_step=(
                f"Run install without --data-dir to keep --db {store.db}, or replace the alice "
                f"entry with the one above to use {explicit_dir}."
            ),
        )

    # The docs' example data dir, pasted as is, names no store: treat it as unset.
    placeholder = store.raw_dir == DOCS_DATA_DIR_PLACEHOLDER
    current = None if placeholder else store.data_dir
    data_dir = explicit_dir or current or default_dir
    if placeholder or (explicit_dir is not None and (explicit_dir != current or store.count > 1)):
        server_args = _store_args(server_args, data_dir=data_dir)
        if placeholder:
            details.append(
                f"data_dir: {DOCS_DATA_DIR_PLACEHOLDER} (the placeholder from the docs) -> "
                f"{data_dir}"
            )
        else:
            before = store.raw_dir if store.raw_dir is not None else f"{DEFAULT_DATA_DIR} (no --data-dir)"
            details.append(f"data_dir: {before} -> {explicit_dir}")
        if store.count > 1:
            details.append(
                f"note: the entry had {store.count} --data-dir options; install left one"
            )

    env = existing.get("env")
    env_value = env.get(ALICE_MEMORY_DATA_DIR_ENV) if isinstance(env, Mapping) else None
    if store.db is None and isinstance(env_value, str) and _resolved_dir(env_value, home) != data_dir:
        if with_env:
            details.append(
                f"note: env {ALICE_MEMORY_DATA_DIR_ENV} was {_HIDDEN}; install set it to "
                f"{data_dir}, the dir alice-memory mcp opens (it reads --data-dir, not this env)"
            )
        else:
            details.append(
                f"note: env {ALICE_MEMORY_DATA_DIR_ENV} is set to another dir ({_HIDDEN}), but "
                f"alice-memory mcp opens {data_dir} (it reads --data-dir, not this env); "
                "install left the env"
            )

    problem = problem_of(launcher)
    final = launcher
    used_fallback = False
    if problem is None:
        details.append(f"launcher: kept {launcher.describe()}")
    elif launcher_in_uv_cache(launcher):
        # A program in a uv cache is dead with no exceptions, pinned or not:
        # the entry gets the launcher a new entry would get.
        final = working or UVX_LAUNCHER
        used_fallback = working is None
        still = f"; {final.command} is not on PATH here" if working is None else ""
        details.append(f"launcher: {launcher.describe()} -> {final.describe()} ({problem}{still})")
    elif launcher.customised:
        used_fallback = True
        details.append(
            f"warning: {problem}; install kept {launcher.describe()} because it "
            f"{launcher.customisation}"
        )
    elif working is not None:
        final = working
        details.append(f"launcher: {launcher.describe()} -> {working.describe()} ({problem})")
    else:
        used_fallback = True
        details.append(
            f"warning: {problem}, and install found no working launcher, so it kept "
            f"{launcher.describe()}"
        )

    entry = dict(existing)
    entry["command"] = final.command
    entry["args"] = [*final.prefix, *server_args]
    if with_env and store.db is None:
        entry["env"] = {ALICE_MEMORY_DATA_DIR_ENV: data_dir}
    kept = _kept_keys(entry, command_kept=final is launcher)
    if kept and not with_env:
        details.append("kept: " + ", ".join(kept))
    if store.db is not None:
        details.append(
            f"note: this entry opens --db {store.db}; install left it, and the SessionStart "
            "hook's store, as they were"
        )
    return _EntryPlan(
        entry,
        final,
        data_dir,
        store.db,
        None,
        details,
        used_fallback,
        hook_mode="own-dir" if store.db is not None else "follow",
        replaced=None if final is launcher else launcher,
    )


def _alice_container(doc: dict[str, Any], host: str) -> dict[str, Any] | None:
    """The object that holds (or would hold) ``alice``; None when it is absent."""

    current: object = doc
    keys = _alice_server_keys(host)
    for depth, key in enumerate(keys[:-1]):
        assert isinstance(current, dict)  # nosec B101 # narrows the type for mypy; doc is a dict and each child is checked below
        child = current.get(key)
        if child is None:
            return None
        if not isinstance(child, dict):
            raise _MalformedHostFile(f"{'.'.join(keys[: depth + 1])} is not an object")
        current = child
    assert isinstance(current, dict)  # nosec B101 # narrows the type for mypy; the loop only descends into dicts
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
            return f"its command is {masked_args([command])[0][0]}"
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
    write and will not carry. The receipt says install refuses while those
    keys are present. ``file_keys`` names keys the entry actually has, as
    ``env.NAME`` for a mapping, so a ``keep:`` line is not printed for a key
    install invented. A label ``_masked`` records inside one of those keys,
    such as ``args (a URL (everything after its scheme))``, is kept with
    that label. The snippet is ``payload`` when set; else install's
    entry on ``data_dir``; else, with ``placeholder``, install's entry with a
    placeholder where the existing entry's data dir goes, so a paste never
    points at an empty store.
    """

    def __init__(
        self,
        reason: str,
        line: int | None = None,
        *,
        extra_keys: Sequence[str] = (),
        data_dir: str | None = None,
        payload: Mapping[str, object] | None = None,
        placeholder: bool = False,
        located: bool = False,
        next_step: str | None = None,
        file_keys: frozenset[str] | None = None,
    ) -> None:
        detail = reason if line is None else f"{reason} (line {line})"
        super().__init__(detail)
        self.detail = detail
        self.extra_keys = tuple(extra_keys)
        self.data_dir = data_dir
        self.payload = payload
        self.placeholder = placeholder
        self.next_step = next_step
        self.file_keys = file_keys
        # True once the scanner found mcp_servers.alice: its snippet is set here.
        self.located = located


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
    dashes: int = 0


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
    dashes = 0
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
            dashes += 1
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
        dashes=dashes,
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


def _hermes_alice_lines(
    data_dir: str, indent: int, launcher: Launcher = UVX_LAUNCHER
) -> list[str]:
    """Install's own mcp_servers.alice block at ``indent``."""

    return _hermes_block_lines(
        mcp_server_payload(data_dir, with_env=True, launcher=launcher), indent
    )


def _hermes_block_lines(
    payload: Mapping[str, object],
    indent: int,
    raw_env: Mapping[str, str] | None = None,
) -> list[str]:
    """An alice block at ``indent`` for ``payload``, every value double-quoted.

    ``raw_env`` is the original scalar text of env values install carries
    over. Those lines keep that text. Every other value is double-quoted.
    """

    carried = raw_env or {}
    pad = " " * indent
    lines = [f"{pad}alice:"]
    for key, value in payload.items():
        if isinstance(value, list):
            lines.append(f"{pad}  {key}:")
            lines.extend(f"{pad}    - {_yaml_double_quoted(str(item))}" for item in value)
        elif isinstance(value, dict):
            lines.append(f"{pad}  {key}:")
            for name, item in value.items():
                if key == "env" and name in carried:
                    rendered = carried[name]
                else:
                    rendered = _yaml_double_quoted(str(item))
                lines.append(f"{pad}    {name}: {rendered}")
        else:
            lines.append(f"{pad}  {key}: {_yaml_double_quoted(str(value))}")
    return lines


def hermes_snippet(data_dir: str, launcher: Launcher = UVX_LAUNCHER) -> str:
    """What to paste into config.yaml by hand."""

    return "\n".join(["mcp_servers:", *_hermes_alice_lines(data_dir, 2, launcher)]) + "\n"


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
# Documented host-env keys install may carry on a Hermes re-run. Each name
# is one our docs tell users to put in a host env map:
# ALICE_MCP_FULL_TOOLS: docs/release/v0.16.0-release-notes.md (host env map);
#   docs/integrations/mcp.md
# ALICE_MCP_LEGACY_TOOLS: docs/integrations/mcp.md;
#   docs/alpha/hermes-dogfood-ubuntu.md
# ALICE_AGENT_API_KEY: docs/alpha/mcp-tools.md (server env);
#   docs/integrations/hermes.md
# ALICE_LEGACY_SURFACES: docs/integrations/mcp.md (MCP process flag; Hermes
#   does not inherit the shell, so it belongs in the same env map)
# ALICE_EMBEDDINGS_BASE_URL: docs/integrations/mcp.md (MCP process env;
#   Hermes does not inherit the shell, so it belongs in the same env map);
#   docs/alpha/mcp-tools.md
# ALICE_EMBEDDINGS_MODEL: docs/integrations/mcp.md;
#   docs/alpha/mcp-tools.md
# ALICE_EMBEDDINGS_API_KEY: docs/integrations/mcp.md;
#   docs/alpha/mcp-tools.md (masked like ALICE_AGENT_API_KEY, never printed)
# Dropping this tuple makes a re-run refuse ALICE_MCP_FULL_TOOLS,
# ALICE_AGENT_API_KEY, ALICE_EMBEDDINGS_BASE_URL, ALICE_EMBEDDINGS_MODEL,
# and ALICE_EMBEDDINGS_API_KEY.
HERMES_DOCUMENTED_ENV_KEYS = (
    "ALICE_MCP_FULL_TOOLS",
    "ALICE_MCP_LEGACY_TOOLS",
    "ALICE_AGENT_API_KEY",
    "ALICE_LEGACY_SURFACES",
    "ALICE_EMBEDDINGS_BASE_URL",
    "ALICE_EMBEDDINGS_MODEL",
    "ALICE_EMBEDDINGS_API_KEY",
)
_DOCUMENTED_ENV_NAMES = frozenset(HERMES_DOCUMENTED_ENV_KEYS)


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


@dataclass(frozen=True)
class _ReadContext:
    """How the entry reader treats anchors and aliases.

    ``aliases`` maps anchor names defined on plain or quoted scalars in the
    file to their values, so ``*name`` reads as that value; any other alias
    stays opaque. ``lenient`` skips ``&name`` properties instead of refusing,
    which is only used to recover a data dir for a refusal snippet.
    """

    aliases: Mapping[str, object] = field(default_factory=dict)
    lenient: bool = False


_STRICT = _ReadContext()
_ANCHOR_THEN_SCALAR = re.compile(r"&([^\s,\[\]{}]+)\s+(\S.*)\Z")


def _strip_anchors(text: str) -> str:
    text = text.strip(" ")
    while text.startswith("&"):
        text = text.split(" ", 1)[1].strip(" ") if " " in text else ""
    return text


def _read_scalar_text(text: str, context: _ReadContext = _STRICT) -> object:
    """One scalar or single-line flow value, as PyYAML would read it for our keys.

    Plain scalars are read as their text (install only needs strings back),
    YAML nulls as None, a known alias as its anchor's value, other aliases
    and tags as opaque.
    """

    text = _strip_anchors(text) if context.lenient else text.strip(" ")
    if text in _YAML_NULLS:
        return None
    if text[0] == "*":
        return context.aliases.get(text[1:].strip(" "), _OPAQUE)
    if text[0] == "!":
        return _OPAQUE
    if text[0] in "&?|>":
        raise _UnreadableEntry
    if text[0] in "[{":
        value, end = _read_flow_node(text, 0, context)
        if text[end:].strip(" "):
            raise _UnreadableEntry
        return value
    if text[0] == "'":
        return text[1:-1].replace("''", "'")
    if text[0] == '"':
        return _decode_double_quoted(text[1:-1])
    return text


def _read_flow_node(text: str, index: int, context: _ReadContext = _STRICT) -> tuple[object, int]:
    while index < len(text) and text[index] == " ":
        index += 1
    if index >= len(text):
        raise _UnreadableEntry
    char = text[index]
    if char == "&" and context.lenient:
        while index < len(text) and text[index] not in " ,[]{}":
            index += 1
        return _read_flow_node(text, index, context)
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
            node, index = _read_flow_node(text, index, context)
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
                        value, index = _read_flow_node(text, index + 1, context)
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
    if char == "*":
        return context.aliases.get(text[index + 1 : end].strip(" "), _OPAQUE), end
    if char == "!":
        return _OPAQUE, end
    plain = text[index:end].strip(" ")
    return (None if plain in _YAML_NULLS else plain), end


def _node_column(raw: str, line: _YamlLine) -> int:
    """The column a value on this line is indented against: the key's, or the dash's."""

    if not line.dash or line.colon < 0:
        return line.indent
    column = line.indent
    while raw[column : column + 1] == "-" and raw[column + 1 : column + 2] in (" ", "\t"):
        column += 1
        while raw[column : column + 1] in (" ", "\t"):
            column += 1
    return column


def _continues_on_next_line(doc: _YamlText, index: int, column: int) -> bool:
    """True when the next non-blank line is indented past ``column``.

    A plain scalar goes on over such lines (PyYAML folds them into one
    value), so the text on the anchor's own line is not the whole value.
    Comment lines count too, which can only refuse more, never read wrong.
    """

    for raw in doc.lines[index + 1 :]:
        if raw.strip(" \t"):
            return _leading_spaces(raw) > column
    return False


def _anchor_values(doc: _YamlText, lexed: Mapping[int, _YamlLine]) -> dict[str, object]:
    """Anchors defined on one-line plain or quoted scalars (``key: &a value``, ``- &a value``).

    An anchor whose plain scalar continues onto the next line is left out,
    so an alias to it stays unreadable and the entry is refused.
    """

    values: dict[str, object] = {}
    for index, line in lexed.items():
        if not line.anchor:
            continue
        if _continues_on_next_line(doc, index, _node_column(doc.lines[index], line)):
            continue
        text = line.value if line.colon >= 0 else line.item
        match = _ANCHOR_THEN_SCALAR.match(text.strip(" "))
        if match is None:
            continue
        try:
            value = _read_scalar_text(match.group(2))
        except _UnreadableEntry:
            continue
        if isinstance(value, str):
            values[match.group(1)] = value
    return values


def _read_alice_block(
    doc: _YamlText,
    lexed: Mapping[int, _YamlLine],
    start: int,
    last: int,
    context: _ReadContext = _STRICT,
) -> dict[str, object]:
    """The old mcp_servers.alice entry as a dict, or _UnreadableEntry.

    Reads what install and a hand edit plausibly leave: a block or flow
    mapping of scalars, block or flow lists of scalars, and one nested
    mapping (env). Anything else, including block scalar text, is
    unreadable.
    """

    head = lexed[start]
    head_value = _strip_anchors(head.value) if context.lenient else head.value
    body = [
        index
        for index in range(start + 1, last + 1)
        if doc.info[index] is None or index in lexed
    ]
    if head_value not in _YAML_NULLS:
        if body or not head_value.startswith("{"):
            raise _UnreadableEntry
        parsed = _read_scalar_text(head_value, context)
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
        value = _strip_anchors(line.value) if context.lenient else line.value
        if value:
            if nested:
                raise _UnreadableEntry
            result[line.key] = _read_scalar_text(value, context)
        elif not nested:
            result[line.key] = None
        elif nested[0].dash:
            values: list[object] = []
            for item in nested:
                if (
                    item.indent != nested[0].indent
                    or not item.dash
                    or item.dashes != 1
                    or item.colon >= 0
                    or not item.item
                ):
                    raise _UnreadableEntry
                values.append(_read_scalar_text(item.item, context))
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
                entries[item.key] = _read_scalar_text(item.value, context)
            result[line.key] = entries
    return result


def _alice_block_extra_keys(
    block: Mapping[str, object], *, carried_env: frozenset[str] = frozenset()
) -> list[str]:
    """Keys in an old alice entry that install has never written.

    ``carried_env`` names documented host-env keys whose values install keeps.
    """

    extra = [key for key in block if key not in _ALICE_INSTALL_KEYS]
    env = block.get("env")
    allowed = {ALICE_MEMORY_DATA_DIR_ENV, *carried_env}
    if isinstance(env, Mapping):
        extra += [f"env.{key}" for key in env if key not in allowed]
    elif env is not None:
        extra.append("env")
    return extra


def _entry_print_keys(block: Mapping[str, object]) -> frozenset[str]:
    """Keys a refusal may mention: ``env.NAME`` for a mapping, else the key."""

    keys: set[str] = set()
    for key, value in block.items():
        if isinstance(value, Mapping):
            keys.update(f"{key}.{name}" for name in value)
        else:
            keys.add(str(key))
    return frozenset(keys)


def _flow_documented_keys(text: str) -> list[str]:
    """Documented env names inside a one-line flow mapping, which install will not carry."""

    try:
        parsed = _read_scalar_text(text)
    except _UnreadableEntry:
        return []
    if not isinstance(parsed, dict):
        return []
    nested = parsed.get("env")
    if isinstance(nested, dict):
        mapping = nested
    elif "env" not in parsed:
        mapping = parsed
    else:
        mapping = None
    if not isinstance(mapping, dict):
        return []
    return [f"env.{name}" for name in mapping if name in _DOCUMENTED_ENV_NAMES]


def _plain_env_scalar(doc: _YamlText, index: int, line: _YamlLine) -> str | None:
    """The line's scalar text when it is one bare, single-quoted, or double-quoted value.

    None for an anchor, alias, tag, block scalar, flow value, or a scalar
    that continues onto the next line. The returned text is the value as
    written, not the decoded string.
    """

    if line.anchor or line.block_owner is not None or line.dash or line.key is None:
        return None
    if _continues_on_next_line(doc, index, _node_column(doc.lines[index], line)):
        return None
    value = line.value
    if not value or value[0] in "*!&|>[{?":
        return None
    if value[0] in "'\"":
        if len(value) < 2 or value[-1] != value[0]:
            return None
        return value
    try:
        decoded = _read_scalar_text(value)
    except _UnreadableEntry:
        return None
    if not isinstance(decoded, str):
        return None
    return value


def _documented_env_carry(
    doc: _YamlText,
    lexed: Mapping[int, _YamlLine],
    start: int,
    last: int,
) -> tuple[dict[str, str], list[str]]:
    """Documented env scalars to keep, and documented env keys that still refuse.

    A value is kept only when it is its own one-line plain, single-quoted,
    or double-quoted scalar. The dict maps the env name to that scalar text.
    """

    carry: dict[str, str] = {}
    unsafe: list[str] = []
    head = lexed[start]
    if head.value and head.value not in _YAML_NULLS:
        return carry, _flow_documented_keys(head.value)

    indexes = [index for index in range(start + 1, last + 1) if index in lexed]
    if not indexes:
        return carry, unsafe
    child_indent = lexed[indexes[0]].indent
    position = 0
    while position < len(indexes):
        line = lexed[indexes[position]]
        position += 1
        if line.indent != child_indent or line.dash or line.key is None:
            continue
        nested: list[int] = []
        while position < len(indexes) and lexed[indexes[position]].indent > child_indent:
            nested.append(indexes[position])
            position += 1
        if line.key != "env":
            continue
        if line.value and line.value not in _YAML_NULLS:
            unsafe.extend(_flow_documented_keys(line.value))
            continue
        if not nested:
            continue
        env_indent = lexed[nested[0]].indent
        cursor = 0
        while cursor < len(nested):
            env_index = nested[cursor]
            env_line = lexed[env_index]
            cursor += 1
            if env_line.indent != env_indent or env_line.key not in _DOCUMENTED_ENV_NAMES:
                continue
            deeper = False
            while cursor < len(nested) and lexed[nested[cursor]].indent > env_indent:
                deeper = True
                cursor += 1
            raw = None if deeper else _plain_env_scalar(doc, env_index, env_line)
            if raw is None:
                unsafe.append(f"env.{env_line.key}")
            else:
                carry[env_line.key] = raw
    return carry, list(dict.fromkeys(unsafe))


def _merge_carried_env(
    payload: Mapping[str, object],
    parsed_env: Mapping[str, object],
    carried: Mapping[str, str],
) -> dict[str, object]:
    """``payload`` plus the documented env values install is keeping."""

    if not carried:
        return dict(payload)
    current = payload.get("env")
    env = dict(current) if isinstance(current, Mapping) else {}
    for name in carried:
        env[name] = parsed_env[name]
    merged = dict(payload)
    merged["env"] = env
    return merged


_HERMES_KEYS_STAY_NEXT = (
    "Install refuses while those keys are present. "
    "Edit the alice entry by hand instead, then check it with: hermes mcp list"
)


_HERMES_DIR_PLACEHOLDER = "<the data dir your existing alice entry uses>"
_MENTIONS_ALICE = re.compile(r"(?m)^[ \t]+['\"]?alice['\"]?[ \t]*:")


def _has_opaque(value: object) -> bool:
    if value is _OPAQUE:
        return True
    if isinstance(value, list):
        return any(_has_opaque(item) for item in value)
    return False


def _recover_alice_dir(
    doc: _YamlText,
    lexed: Mapping[int, _YamlLine],
    start: int,
    last: int,
    aliases: Mapping[str, object],
    home: Path,
    default_dir: str,
) -> str | None:
    """The data dir an entry the strict reader refused runs with, if visible."""

    try:
        block = _read_alice_block(doc, lexed, start, last, _ReadContext(aliases, lenient=True))
    except (_UnreadableEntry, RecursionError):
        return None
    return _visible_dir(block, home)


@dataclass(frozen=True)
class HermesPlan:
    """What install --host hermes would write, and what the receipt says about it."""

    text: str | None
    data_dir: str
    payload: Mapping[str, object]
    details: tuple[str, ...] = ()
    used_fallback: bool = False


def _hermes_payload(plan: _EntryPlan) -> dict[str, object]:
    """The alice block install writes for ``plan``: command, args, and env when it has one.

    _plan_entry sets env to ALICE_MEMORY_DATA_DIR = the data dir, except for
    a --db entry, whose env stays as the user wrote it.
    """

    assert plan.entry is not None  # nosec B101 # narrows the type for mypy; each caller passes a plan that has an entry
    payload: dict[str, object] = {"command": plan.entry["command"], "args": plan.entry["args"]}
    if "env" in plan.entry:
        payload["env"] = plan.entry["env"]
    return payload


def _plan_hermes(
    text: str,
    explicit_dir: str | None,
    default_dir: str,
    *,
    home: Path,
    search: LauncherSearch,
    problem_of: Callable[[Launcher], str | None] | None = None,
) -> HermesPlan:
    """Plan mcp_servers.alice in ``text``; see plan_hermes_config.

    ``explicit_dir`` is --data-dir when it was passed. Without it, an old
    alice entry keeps the data dir it runs with, else ``default_dir``.
    """

    def plan_entry(existing: object) -> _EntryPlan:
        return _plan_entry(
            existing,
            key_label="mcp_servers.alice",
            explicit_dir=explicit_dir,
            new_entry_dir=default_dir,
            default_dir=default_dir,
            home=home,
            search=search,
            with_env=True,
            needs_hook=False,
            problem_of=problem_of,
        )

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
        fresh = plan_entry(None)
        payload = _hermes_payload(fresh)
        return HermesPlan(
            _render_yaml_text(
                doc, [*lines, "mcp_servers:", *_hermes_block_lines(payload, 2)], True
            ),
            fresh.data_dir,
            payload,
            tuple(fresh.details),
            fresh.used_fallback,
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
        aliases = _anchor_values(doc, lexed)
        carried, unsafe = _documented_env_carry(doc, lexed, start, last)
        if unsafe:
            recovered = explicit_dir or _recover_alice_dir(
                doc, lexed, start, last, aliases, home, default_dir
            )
            raise HermesConfigRefused(
                "mcp_servers.alice has keys install did not write",
                start + 1,
                extra_keys=unsafe,
                data_dir=recovered,
                placeholder=recovered is None,
                located=True,
                next_step=_HERMES_KEYS_STAY_NEXT,
            )

        def refuse_unread(reason: str) -> HermesConfigRefused:
            recovered = explicit_dir or _recover_alice_dir(
                doc, lexed, start, last, aliases, home, default_dir
            )
            return HermesConfigRefused(
                reason, start + 1, data_dir=recovered, placeholder=recovered is None, located=True
            )

        if any(lexed[index].anchor for index in range(start, last + 1) if index in lexed):
            raise refuse_unread("mcp_servers.alice defines an anchor that other keys may use")
        try:
            old = _read_alice_block(doc, lexed, start, last, _ReadContext(aliases))
        except _UnreadableEntry:
            raise refuse_unread(
                "the existing mcp_servers.alice uses YAML this writer does not read"
            ) from None
        if _has_opaque(old.get("command")) or _has_opaque(old.get("args")):
            raise refuse_unread(
                "the command or args of the existing mcp_servers.alice cannot be read"
            )
        entry_plan = plan_entry(old)
        if entry_plan.entry is None and entry_plan.refusal is None:
            # alice-memory mcp would not start with these args: leave the entry.
            return HermesPlan(
                None, entry_plan.data_dir, dict(old), tuple(entry_plan.details), False
            )
        if entry_plan.refusal is not None:
            raise HermesConfigRefused(
                entry_plan.refusal,
                start + 1,
                data_dir=entry_plan.data_dir,
                payload=entry_plan.paste,
                located=True,
                next_step=entry_plan.next_step,
                file_keys=_entry_print_keys(old),
            )
        payload = _hermes_payload(entry_plan)
        parsed_env = old.get("env")
        if isinstance(parsed_env, Mapping):
            carried = {
                name: raw
                for name, raw in carried.items()
                if isinstance(parsed_env.get(name), str)
            }
        else:
            carried = {}
        extra = _alice_block_extra_keys(old, carried_env=frozenset(carried))
        if extra:
            raise HermesConfigRefused(
                "mcp_servers.alice has keys install did not write",
                start + 1,
                extra_keys=extra,
                data_dir=entry_plan.data_dir,
                payload=payload,
                located=True,
                next_step=_HERMES_KEYS_STAY_NEXT,
                file_keys=_entry_print_keys(old),
            )
        if isinstance(parsed_env, Mapping):
            payload = _merge_carried_env(payload, parsed_env, carried)
        details = list(entry_plan.details)
        if carried:
            details.append("kept: " + ", ".join(f"env.{name}" for name in carried))
        block = _hermes_block_lines(payload, child_indent, carried)
        plan = HermesPlan(
            None,
            entry_plan.data_dir,
            payload,
            tuple(details),
            entry_plan.used_fallback,
        )
        if lines[start : last + 1] == block:
            return plan
        lines[start : last + 1] = block
        final_newline = doc.final_newline or last == len(doc.lines) - 1
        return replace(plan, text=_render_yaml_text(doc, lines, final_newline))

    fresh = plan_entry(None)
    payload = _hermes_payload(fresh)
    block = _hermes_block_lines(payload, child_indent)

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
    return HermesPlan(
        _render_yaml_text(doc, lines, doc.final_newline or at_eof),
        fresh.data_dir,
        payload,
        tuple(fresh.details),
        fresh.used_fallback,
    )


def plan_hermes_config(text: str, data_dir: str) -> str | None:
    """Return ``text`` with mcp_servers.alice on ``data_dir``, or None when it already is.

    This is install with --data-dir passed, uvx on PATH, and every existing
    launcher treated as able to run, so the result does not depend on the
    machine. Only the alice lines are added or replaced; every other byte
    is kept. The one other edit is an empty ``mcp_servers: {}`` / ``~`` /
    ``null`` value, which becomes ``mcp_servers:`` so the block can go
    under it. An old alice entry is replaced only when it is of a shape
    install writes and its keys are within command, args,
    env.ALICE_MEMORY_DATA_DIR, and the documented host env keys in
    HERMES_DOCUMENTED_ENV_KEYS whose values are one-line plain or quoted
    scalars. Those env values are copied byte for byte. Its command and
    args stay apart from the data dir. Raises HermesConfigRefused for a
    file outside the subset this scanner reads.
    """

    home = Path.home()
    return _plan_hermes(
        text,
        data_dir,
        str(resolve_user_path(DEFAULT_DATA_DIR, home)),
        home=home,
        search=LauncherSearch(UVX_LAUNCHER, None, True),
        problem_of=lambda _launcher: None,
    ).text


def _backup_dir(data_dir: str) -> Path:
    """Where install keeps host-config backups: one private directory in the data dir."""

    return Path(data_dir) / "backups" / "host-configs"


class _BackupFailed(Exception):
    """The backup directory could not be created or written; the host file was not touched."""

    def __init__(self, directory: Path) -> None:
        super().__init__(str(directory))
        self.directory = directory

    def reason(self) -> str:
        return (
            f"the backup directory {self.directory} could not be created or written, so "
            "install did not change the file"
        )


def _backup_host_file(path: Path, original: bytes, *, backup_dir: Path, host: str) -> Path:
    """Write ``original`` into ``backup_dir`` as a new, private, timestamped file.

    Never next to the host file or a symlink's target, which may sit in a
    dotfiles repo: ``backup_dir`` is <data dir>/backups/host-configs, 0700.
    The bytes go to a temp file first and are fsynced; only then does the
    backup name appear, as a hard link to the complete file. A write that
    fails partway leaves no file under a backup name, and the temp file is
    removed on every path.
    """

    try:
        # Missing levels are created 0700; of the existing ones, only
        # host-configs, install's own directory, is tightened.
        _ensure_private_parents(backup_dir / "placeholder")
        backup_dir.chmod(0o700)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = f"{host}-{path.name}{HERMES_BACKUP_MARKER}{stamp}"
        descriptor, temp_name = tempfile.mkstemp(
            dir=backup_dir, prefix=f".{base}.", suffix=".alice-install.tmp"
        )
    except OSError as problem:
        raise _BackupFailed(backup_dir) from problem
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(1, 100):
            candidate = backup_dir / (base if attempt == 1 else f"{base}-{attempt}")
            try:
                os.link(temp_path, candidate)
            except FileExistsError:
                continue
            return candidate
        raise _BackupFailed(backup_dir)
    except OSError as problem:
        raise _BackupFailed(backup_dir) from problem
    finally:
        temp_path.unlink(missing_ok=True)


def _plan_hermes_file(
    path: Path,
    explicit_dir: str | None,
    default_dir: str,
    *,
    home: Path,
    search: LauncherSearch,
) -> tuple[HermesPlan, bytes | None, Path]:
    """The plan, the original bytes (None when there is no file), and the write target."""

    try:
        target = _host_target(path)
    except _MalformedHostFile as problem:
        raise HermesConfigRefused(str(problem)) from None
    if not target.exists():
        fresh = _plan_entry(
            None,
            key_label="mcp_servers.alice",
            explicit_dir=explicit_dir,
            new_entry_dir=default_dir,
            default_dir=default_dir,
            home=home,
            search=search,
            with_env=True,
            needs_hook=False,
        )
        payload = _hermes_payload(fresh)
        text = "\n".join(["mcp_servers:", *_hermes_block_lines(payload, 2)]) + "\n"
        plan = HermesPlan(text, fresh.data_dir, payload, tuple(fresh.details), fresh.used_fallback)
        return plan, None, target
    if not target.is_file():
        raise HermesConfigRefused(f"{path.name} is not a regular file")
    original = target.read_bytes()
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HermesConfigRefused(f"{path.name} is not UTF-8") from exc
    try:
        return (
            _plan_hermes(text, explicit_dir, default_dir, home=home, search=search),
            original,
            target,
        )
    except RecursionError:
        raise HermesConfigRefused(
            "the existing mcp_servers.alice is nested too deeply to read",
            placeholder=explicit_dir is None,
            data_dir=explicit_dir,
        ) from None
    except HermesConfigRefused as refusal:
        if (
            not refusal.located
            and refusal.payload is None
            and refusal.data_dir is None
            and explicit_dir is None
            and _MENTIONS_ALICE.search(text)
        ):
            # The scanner stopped before it reached an alice entry that may
            # exist: never offer a snippet on ~/.alice in its place.
            refusal.placeholder = True
        raise


@dataclass(frozen=True)
class _HostResult:
    receipt: str
    status: str  # "ok", "refused" or "failed"
    used_fallback: bool = False  # an entry runs a launcher install could not confirm here


_FAILED_REASON = "the file could not be read or written"
_DRY_RUN_REFUSAL = "dry run: install would refuse this file; nothing was attempted"


def _refusal_action(dry_run: bool) -> str:
    return "would-refuse" if dry_run else "refused"


def _hermes_payload_snippet(payload: Mapping[str, object]) -> str:
    return "\n".join(["mcp_servers:", *_hermes_block_lines(payload, 2)]) + "\n"


def _install_hermes_host(
    *,
    home: Path,
    explicit_dir: str | None,
    default_dir: str,
    dry_run: bool,
    search: LauncherSearch,
) -> _HostResult:
    path = host_file_map(home)["hermes"]["mcp"]
    data_dir = explicit_dir or default_dir
    launcher = search.launcher or UVX_LAUNCHER
    target: Path | None = None

    def receipt(action: str, details: Sequence[str], **extra: Any) -> str:
        return _format_host_receipt(
            host="hermes",
            mcp_path=path,
            hooks_path=None,
            action=action,
            details=details,
            session_start="none",
            data_dir=data_dir,
            target=target,
            **extra,
        )

    details: list[str] = []
    try:
        plan, original, target = _plan_hermes_file(
            path, explicit_dir, default_dir, home=home, search=search
        )
    except HermesConfigRefused as refusal:
        extra_keys = ", ".join(refusal.extra_keys)
        trailer: list[str] = []
        if refusal.payload is not None:
            shown, hidden = _masked(refusal.payload, own_env=_own_env(refusal.payload))
            if refusal.file_keys is not None:
                invented = [
                    item
                    for item in hidden
                    if not _hidden_label_in_entry(item, refusal.file_keys)
                ]
                hidden = [
                    item for item in hidden if _hidden_label_in_entry(item, refusal.file_keys)
                ]
                env_shown = shown.get("env")
                payload_env = refusal.payload.get("env")
                if isinstance(env_shown, dict) and isinstance(payload_env, Mapping):
                    for item in invented:
                        name = item.removeprefix("env.")
                        if item.startswith("env.") and name in payload_env:
                            env_shown[name] = payload_env[name]
            snippet = _hermes_payload_snippet(shown)
            if hidden:
                trailer.append(_keep_line(hidden))
        elif refusal.placeholder:
            snippet = hermes_snippet(_HERMES_DIR_PLACEHOLDER, launcher)
            trailer.append(
                "keep: replace the placeholder with the data dir your existing alice entry "
                "uses; install could not read it, and ~/.alice may be an empty store"
            )
        else:
            snippet = hermes_snippet(refusal.data_dir or data_dir, launcher)
        if extra_keys:
            trailer.append(f"extra_keys: {extra_keys}")
        if dry_run:
            trailer.append(_DRY_RUN_REFUSAL)
        elif refusal.next_step:
            trailer.append(f"next: {path.name} was not changed. {refusal.next_step}")
        else:
            trailer.append(
                f"next: {path.name} was not changed. Add the alice entry above under "
                "mcp_servers by hand, then check it with: hermes mcp list"
            )
        return _HostResult(
            receipt(
                _refusal_action(dry_run),
                (f"reason: {refusal.detail}",),
                snippet=snippet,
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
    details.extend(plan.details)
    if dry_run:
        if new_text is None:
            details.append("planned: unchanged")
        elif original is None:
            details.append("planned: create")
        else:
            details.append("planned: edit, with a backup first")
        shown, hidden = _masked(plan.payload, own_env=_own_env(plan.payload))
        snippet = _hermes_payload_snippet(shown)
        dry_trailer = (_hidden_line(hidden),) if hidden else ()
        return _HostResult(
            receipt("dry-run", details, snippet=snippet, trailer=dry_trailer),
            "ok",
            plan.used_fallback,
        )
    if new_text is None:
        return _HostResult(receipt("unchanged", details, snippet=None), "ok", plan.used_fallback)
    try:
        if original is not None:
            backup = _backup_host_file(
                target, original, backup_dir=_backup_dir(plan.data_dir), host="hermes"
            )
            details.append(f"backup: {backup}")
        _write_text(target, new_text, newline="")
    except (_BackupFailed, OSError, InstallError) as problem:
        reason = problem.reason() if isinstance(problem, _BackupFailed) else _FAILED_REASON
        return _HostResult(
            receipt(
                "failed",
                (f"file: {target}", f"reason: {reason}", *details),
                snippet=None,
            ),
            "failed",
            plan.used_fallback,
        )
    return _HostResult(receipt("written", details, snippet=None), "ok", plan.used_fallback)


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
    launcher: Launcher = UVX_LAUNCHER,
    target: Path | None = None,
    hooks_target: Path | None = None,
    hook_details: Sequence[str] = (),
    file_format: str | None = None,
) -> str:
    lines = [f"host: {host}", f"path: {mcp_path}"]
    if target is not None and target != mcp_path:
        lines.append(f"target: {target}")
    # Every line install composes passes through mask_text, so no URL reaches
    # the terminal past its scheme; snippets are masked whole.
    lines.append(f"action: {action}")
    if file_format is not None:
        lines.append(f"format: {file_format}")
    lines += [*map(mask_text, details), f"session_start: {session_start}"]
    if hooks_path is not None:
        lines.append(f"session_start_path: {hooks_path}")
        if hooks_target is not None and hooks_target != hooks_path:
            lines.append(f"session_start_target: {hooks_target}")
    lines.extend(map(mask_text, hook_details))
    if host in {"openclaw", "hermes", "opencode"}:
        lines.append(f"note: {BRIEF_HINT}")
    if snippet is not None:
        lines.append("snippet:")
        lines.append(snippet.rstrip())
    lines.extend(map(mask_text, trailer))
    if host == "openclaw":
        shown_line = openclaw_add_line(data_dir, launcher, hide_values=True)
        lines.append(shown_line)
        if shown_line != openclaw_add_line(data_dir, launcher):
            lines.append(
                "note: the line above shows <hidden> in place of values from your entry; "
                "put them back before running it"
            )
    return "\n".join(lines)


def _alice_entry_snippet(host: str, payload: Mapping[str, object]) -> str:
    """The JSON a user would paste to add alice by hand."""

    doc: dict[str, Any] = {}
    _set_nested(doc, _alice_server_keys(host), dict(payload))
    return _dump_json(doc)


def _resolved_dir(raw: str | None, home: Path) -> str | None:
    return None if raw is None else str(resolve_user_path(raw, home))


_INDEX_ADVICE = (
    "Move the index into your user-level uv config, ~/.config/uv/uv.toml as [[index]] "
    "(%APPDATA%\\uv\\uv.toml on Windows), not a project uv.toml, since the hook runs from "
    "the project's directory; keep its credentials in a keyring, .netrc, or "
    "UV_INDEX_<NAME>_USERNAME and UV_INDEX_<NAME>_PASSWORD. Then remove the option from the "
    "entry's args and run install again"
)


def _hook_block(launcher: Launcher) -> str | None:
    """Why install must not write this launcher into a hook, or None.

    Install never writes a URL into a hook file, so an entry whose uvx
    options name an index or hold a URL gets no new hook; nor does one with
    any option off the carry list (host_launcher.CARRY_OPTIONS_WITH_VALUE and
    CARRY_FLAGS), which could make the hook resolve a different
    alice-memory than the server. A spec that can only resolve below 0.16.0
    has no alice-memory-session-start, so a hook would fail on every session.
    """

    if launcher.kind == "uvx":
        index_words, off_list = launcher.uncarried_options
        if index_words:
            return (
                f"the entry's uvx options name an index or a URL ({', '.join(index_words)}), "
                f"and install never writes a URL into a hook file. {_INDEX_ADVICE}"
            )
        if off_list:
            return (
                f"the entry's uvx options include {', '.join(off_list)}, which install does not "
                "carry into a hook (it carries only --prerelease, --python and "
                "--python-preference, and the flags --native-tls, --offline, --no-cache and "
                "--refresh), so the hook could resolve a different alice-memory than the server. "
                "Remove the option from the entry's args and run install again"
            )
    if launcher.package_has_url:
        return (
            f"the entry's package spec is a URL ({launcher.shown_package}), and install never "
            "writes a URL into a hook file. Use a plain alice-memory spec, with its index in your "
            "user-level uv config, ~/.config/uv/uv.toml as [[index]], if it needs one, then run "
            "install again"
        )
    support = launcher.session_start_support
    if support == "no":
        return (
            f"{launcher.shown_package} has no alice-memory-session-start (it first shipped in "
            f"alice-memory {FIRST_SESSION_START_VERSION}); pin alice-memory>=0.16 or remove "
            "the pin, then run install again"
        )
    if support == "unknown":
        return (
            f"install cannot tell whether {launcher.shown_package} has alice-memory-session-start "
            f"(it first shipped in alice-memory {FIRST_SESSION_START_VERSION})"
        )
    return None


def _argv_after_change(plan: _EntryPlan) -> str | None:
    """The plain hook argv install would add once the entry's uncarried options are gone.

    Carried options only, so no URL and nothing to hide; None when the
    entry's spec still has no session-start script, or the hook keeps its
    own store (a --db entry).
    """

    launcher = plan.launcher
    if launcher.kind != "uvx" or plan.hook_mode != "follow":
        return None
    index_words, off_list = launcher.uncarried_options
    if not (index_words or off_list) or launcher.session_start_support != "yes":
        return None
    argv = [
        launcher.command,
        *launcher.carry_options,
        "--from",
        launcher.package,
        SESSION_START_COMMAND,
        "--data-dir",
        plan.data_dir,
    ]
    return f"session_start_argv_after_change: {json.dumps(argv)}"


def _refuse_kept_hook(
    old_command: str,
    old: HookDataDir | None,
    data_dir: str,
    reason: str | None,
    details: list[str],
    block: str | None = None,
) -> tuple[str, str]:
    """Refuse a hook install must keep but cannot point at the entry's new dir.

    The receipt names both dirs and prints the hook's argv, with the new
    --data-dir, to add by hand; values that may be secrets are hidden.
    """

    argv = split_command(old_command)
    located = None
    for index, word in enumerate(argv):
        if word == "--data-dir" and index + 1 < len(argv):
            located = (index + 1, False)
        elif word.startswith("--data-dir="):
            located = (index, True)
    if located is None:
        argv += ["--data-dir", data_dir]
    else:
        index, equals = located
        argv[index] = f"--data-dir={data_dir}" if equals else data_dir
    old_raw = old.raw if old is not None and old.raw is not None else "(none)"
    details.append(
        f"warning: the MCP entry now opens {data_dir}, but the SessionStart hook's --data-dir "
        f"{old_raw} cannot be moved safely ({reason}), so install did not change the hook; "
        f"change its --data-dir to {data_dir} by hand"
        + (f". It keeps its own command because {block}" if block else "")
    )
    shown, hidden = shown_hook_words(argv)
    if not hidden:
        # Never a masked argv: one could only be used by pasting the hidden
        # values back into the hooks file by hand.
        details.append(f"session_start_argv: {json.dumps(shown)}")
    return "refused", f"the SessionStart hook still points at {old_raw}, not {data_dir}"


def _plan_hook(
    hooks: _JsonFile,
    host: str,
    plan: _EntryPlan,
    old_command: str | None,
    old: HookDataDir | None,
    old_dir: str | None,
    explicit: bool,
    details: list[str],
) -> tuple[str, str | None]:
    """Plan the Alice SessionStart hook from the entry's plan: (status, refusal).

    "follow": the hook takes the entry's launcher and data dir. "own-dir"
    (a --db entry): the launcher, keeping its own data dir. "repair": only
    the shape of an existing hook is repaired. A hook keeps its own command,
    shape repaired, and none is added, when the launcher must not go into a
    hook (_hook_block), and in "follow" when its --data-dir is not read
    literally by the shell and --data-dir was not passed. When a kept
    command sits beside an entry whose launcher install replaced, the
    receipt says so.
    """

    command: str | None = None
    built = False  # the command runs the entry's launcher, as install wrote it
    if plan.hook_mode == "repair":
        if old_command is None:
            return "none", None
        command = old_command
    else:
        script_problem = hook_script_problem(plan.launcher)
        block = _hook_block(plan.launcher)
        if plan.hook_mode == "own-dir" and old_command is None:
            details.append("note: the entry opens --db, so install added no SessionStart hook")
            return "none", None
        if script_problem is not None:
            if old_command is None or plan.hook_mode == "follow":
                details.append(
                    f"warning: {script_problem}, so install left the SessionStart hook as it was"
                )
                return "skipped", None
            details.append(
                f"warning: {script_problem}, so install kept the SessionStart hook's command"
            )
            command = old_command
        elif block is not None:
            after = _argv_after_change(plan)
            if old_command is None:
                details.append(f"warning: install added no SessionStart hook: {block}")
                if after is not None:
                    details.append(after)
                return "skipped", None
            if after is not None:
                details.append(after)
            command = old_command
            # A --db entry's hook keeps its own store ("own-dir"): only a
            # hook that follows the entry's data dir is moved or refused.
            follows = plan.hook_mode == "follow"
            if follows and old is not None and old.trusted and old_dir != plan.data_dir:
                # Keep the launcher text; move only the --data-dir value.
                moved, problem = replace_hook_data_dir(old_command, plan.data_dir)
                if moved is None:
                    return _refuse_kept_hook(old_command, old, plan.data_dir, problem, details)
                command = moved
                details.append(
                    "warning: install kept the SessionStart hook's launcher and changed only "
                    f"its --data-dir: {block}"
                )
                details.append(f"session_start_data_dir: {old.raw} -> {plan.data_dir}")
            elif follows and explicit and (old is None or not old.trusted):
                reason = old.reason if old is not None and old.reason else "it has no --data-dir"
                return _refuse_kept_hook(old_command, old, plan.data_dir, reason, details, block)
            else:
                details.append(
                    f"warning: install left the SessionStart hook's command as it was: {block}"
                )
        elif plan.hook_mode == "own-dir":
            assert old_command is not None  # nosec B101 # narrows the type for mypy; own-dir with no command returned above
            if old is None or not old.trusted:
                if old is not None and old.raw is not None:
                    details.append(
                        f"warning: the SessionStart hook's --data-dir {old.raw} cannot be relied "
                        f"on ({old.reason}), so install kept the hook's command"
                    )
                command = old_command
            else:
                assert old.raw is not None  # nosec B101 # narrows the type for mypy; only HookDataDir(None, False) lacks raw, and it is not trusted
                text, problem = hook_command(plan.launcher, old.raw)
                if problem is not None:
                    return "refused", problem
                command, built = text, True
        elif old is not None and old.shell and not explicit:
            assert old_command is not None  # nosec B101 # narrows the type for mypy; old is read from old_command, so it implies one
            details.append(
                f"warning: the SessionStart hook's --data-dir {old.raw} is not read literally "
                "by the shell, so install left the hook's command as it was"
            )
            command = old_command
        else:
            text, problem = hook_command(plan.launcher, plan.data_dir)
            if problem is not None:
                argv = [*plan.launcher.hook_argv(), "--data-dir", plan.data_dir]
                details.append(f"session_start_argv: {json.dumps(masked_args(argv)[0])}")
                return "refused", problem
            command, built = text, True
            if old is not None and old.raw is not None:
                if old.trusted and old_dir != plan.data_dir:
                    details.append(f"session_start_data_dir: {old.raw} -> {plan.data_dir}")
                elif not old.trusted:
                    details.append(
                        f"warning: the SessionStart hook's --data-dir {old.raw} could not be "
                        f"relied on ({old.reason}); the hook now uses {plan.data_dir}"
                    )
        if built:
            details.append(
                "session_start_launcher: "
                + " ".join(masked_args(plan.launcher.hook_argv())[0])
            )
        elif plan.replaced is not None:
            details.append(
                f"warning: the MCP entry's launcher changed from {plan.replaced.describe()} to "
                f"{plan.launcher.describe()}, but the SessionStart hook kept its own command, "
                "which may still run the old one"
            )
    assert command is not None  # nosec B101 # narrows the type for mypy; every branch above sets it or returns
    if not hooks.doc:
        hooks.doc.update(_new_hooks_document(host, command))
        return "added", None
    return _merge_session_start(hooks.doc, host, command), None


def _dry_run_json_snippet(
    host: str, entry: Mapping[str, Any] | None, hooks: _JsonFile | None
) -> tuple[str, list[str]]:
    """Only the alice entry and only the Alice hook, values from the file hidden."""

    parts: list[str] = []
    hidden: list[str] = []
    if entry is not None:
        shown, hidden = _masked(entry)
        parts.append(_alice_entry_snippet(host, shown))
    if hooks is not None:
        items = _alice_hook_items(hooks.doc, host)
        if items:
            key = "SessionStart" if host == "claude-code" else "sessionStart"
            parts.append(_dump_json({"hooks": {key: _masked_hook_item(items, hidden)}}))
    return "\n---\n".join(part.rstrip() for part in parts) + "\n", list(dict.fromkeys(hidden))


def _install_json_host(
    host: str,
    *,
    home: Path,
    explicit_dir: str | None,
    default_dir: str,
    dry_run: bool,
    search: LauncherSearch,
) -> _HostResult:
    """Plan both files of one JSON host, then write them; never raise for one host.

    A file that does not parse, or whose structure is malformed, refuses the
    whole host and nothing is written for it. An alice entry that install did
    not write refuses the MCP file; an existing Alice hook is still repaired
    with its own --data-dir, and no hook is added. An OSError fails the host
    with a static message naming the file. The receipt says what happened
    to each file.
    """

    files = host_file_map(home)[host]
    mcp_path = files["mcp"]
    hooks_path = files.get("hooks") if host in _SESSION_START_HOSTS else None
    key_label = ".".join(_alice_server_keys(host))
    details: list[str] = []
    hook_details: list[str] = []
    session_start = "none"
    data_dir = explicit_dir or default_dir
    launcher = search.launcher or UVX_LAUNCHER
    mcp: _JsonFile | None = None
    hooks: _JsonFile | None = None

    def receipt(action: str, **extra: Any) -> str:
        return _format_host_receipt(
            host=host,
            mcp_path=mcp_path,
            hooks_path=hooks_path,
            action=action,
            details=details,
            hook_details=hook_details,
            session_start=session_start,
            data_dir=data_dir,
            launcher=launcher,
            target=mcp.target if mcp is not None else None,
            hooks_target=hooks.target if hooks is not None else None,
            **extra,
        )

    current = mcp_path
    hook_problem: str | None = None
    try:
        mcp = _load_json_file(mcp_path)
        container = _alice_container(mcp.doc, host)
        if hooks_path is not None:
            current = hooks_path
            hooks = _load_json_file(hooks_path)
            _check_hooks_file(hooks.doc, host)
        existing = container.get("alice") if container is not None else None
        old_hook = _existing_alice_hook_command(hooks.doc, host) if hooks is not None else None
        old_read = read_hook_data_dir(old_hook) if old_hook is not None else None
        old_hook_dir = (
            _resolved_dir(old_read.raw, home)
            if old_read is not None and old_read.trusted
            else None
        )
        plan = _plan_entry(
            existing,
            key_label=key_label,
            explicit_dir=explicit_dir,
            new_entry_dir=old_hook_dir or default_dir,
            default_dir=default_dir,
            home=home,
            search=search,
            with_env=False,
            needs_hook=host in _SESSION_START_HOSTS,
        )
        data_dir, launcher = plan.data_dir, plan.launcher
        details.extend(plan.details)
        if (
            existing is None
            and explicit_dir is None
            and old_read is not None
            and old_read.raw is not None
            and not old_read.trusted
        ):
            details.append(
                f"warning: the existing SessionStart hook's --data-dir {old_read.raw} cannot "
                f"be relied on ({old_read.reason}), so the new entry uses {plan.data_dir}"
            )
        if plan.entry is not None:
            _set_nested(mcp.doc, _alice_server_keys(host), plan.entry)
        if hooks is not None:
            session_start, hook_problem = _plan_hook(
                hooks,
                host,
                plan,
                old_hook,
                old_read,
                old_hook_dir,
                explicit_dir is not None,
                hook_details,
            )
        mcp_changed = plan.entry is not None and mcp.changed()
        hooks_changed = (
            hooks is not None and session_start in {"added", "updated"} and hooks.changed()
        )
    except (_MalformedHostFile, RecursionError) as problem:
        reason = (
            str(problem)
            if isinstance(problem, _MalformedHostFile)
            else "the file is nested too deeply to read"
        )
        details[:] = [f"reason: {reason}", f"file: {current}"]
        hook_details.clear()
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
        hook_details.clear()
        session_start = "none"
        return _HostResult(receipt("failed", snippet=None), "failed")

    assert mcp is not None  # nosec B101 # narrows the type for mypy; the failure paths above return
    refused = plan.refusal is not None or hook_problem is not None
    if plan.refusal is not None:
        details.insert(0, f"reason: {plan.refusal}")
    if hook_problem is not None:
        hook_details.insert(0, f"session_start_reason: {hook_problem}")
    paste_entry = plan.paste or mcp_server_payload(
        plan.data_dir, with_env=False, launcher=plan.launcher
    )
    shown_paste, paste_hidden = _masked(paste_entry)
    paste = _alice_entry_snippet(host, shown_paste)
    paste_keep: tuple[str, ...] = (_keep_line(paste_hidden),) if paste_hidden else ()
    status = "refused" if refused else "ok"
    backup_dir = _backup_dir(
        plan.data_dir if plan.hook_mode == "follow" else (explicit_dir or default_dir)
    )

    if dry_run:
        if hooks_changed:
            session_start = f"planned ({session_start})"
        if plan.refusal is not None:
            return _HostResult(
                receipt("would-refuse", snippet=paste, trailer=(*paste_keep, _DRY_RUN_REFUSAL)),
                status,
                plan.used_fallback,
            )
        snippet, hidden = _dry_run_json_snippet(host, plan.entry, hooks)
        dry_trailer: tuple[str, ...] = (
            *((_hidden_line(hidden),) if hidden else ()),
            *((_DRY_RUN_REFUSAL,) if hook_problem is not None else ()),
        )
        return _HostResult(
            receipt("dry-run", snippet=snippet, trailer=dry_trailer), status, plan.used_fallback
        )

    action = "refused" if plan.refusal is not None else "unchanged"
    if mcp_changed:
        try:
            if mcp.raw is not None:
                backup = _backup_host_file(mcp.target, mcp.raw, backup_dir=backup_dir, host=host)
                details.append(f"backup: {backup}")
            _write_text(mcp.target, _dump_json(mcp.doc))
        except _BackupFailed as failure:
            details[:0] = [f"file: {mcp.target}", f"reason: {failure.reason()}"]
            if hooks_changed:
                session_start = "not attempted"
            return _HostResult(receipt("failed", snippet=None), "failed", plan.used_fallback)
        except (OSError, InstallError):
            details[:0] = [f"file: {mcp.target}", f"reason: {_FAILED_REASON}"]
            if hooks_changed:
                session_start = "not attempted"
            return _HostResult(receipt("failed", snippet=None), "failed", plan.used_fallback)
        action = "written"
    if hooks_changed:
        assert hooks is not None  # nosec B101 # narrows the type for mypy; hooks_changed is set only with hooks read
        try:
            if hooks.raw is not None:
                backup = _backup_host_file(
                    hooks.target, hooks.raw, backup_dir=backup_dir, host=host
                )
                hook_details.append(f"session_start_backup: {backup}")
            _write_text(hooks.target, _dump_json(hooks.doc))
        except (_BackupFailed, OSError, InstallError) as problem:
            reason = problem.reason() if isinstance(problem, _BackupFailed) else _FAILED_REASON
            hook_details[:0] = [
                f"session_start_file: {hooks.target}",
                f"session_start_reason: {reason}",
            ]
            session_start = "failed"
            return _HostResult(receipt(action, snippet=None), "failed", plan.used_fallback)

    if plan.refusal is not None:
        return _HostResult(
            receipt(
                "refused",
                snippet=paste,
                trailer=(
                    *paste_keep,
                    f"next: {mcp_path.name} was not changed. {plan.next_step or _FOREIGN_NEXT}",
                ),
            ),
            status,
            plan.used_fallback,
        )
    if hook_problem is not None:
        return _HostResult(
            receipt(
                action,
                snippet=None,
                trailer=(
                    "next: install did not write the SessionStart hook. Add a hook that runs "
                    "session_start_argv above, quoted for the shell your host uses.",
                ),
            ),
            status,
            plan.used_fallback,
        )
    return _HostResult(receipt(action, snippet=None), status, plan.used_fallback)


_MCPB_UVX_WARNING = (
    "warning: the bundle runs uvx, which is not on PATH here; Claude Desktop cannot "
    "start it until uv is installed: https://docs.astral.sh/uv/"
)


def _install_mcpb(path: Path, *, dry_run: bool, uvx_on_path: bool) -> _HostResult:
    try:
        receipt = write_mcpb_bundle(path, dry_run=dry_run)
    except InstallError as problem:
        reason = str(problem)
    except OSError:
        reason = _FAILED_REASON
    else:
        if not uvx_on_path:
            receipt = f"{receipt}\n{_MCPB_UVX_WARNING}"
        return _HostResult(receipt, "ok")
    return _HostResult("\n".join((f"mcpb: {path}", "action: failed", f"reason: {reason}")), "failed")


_OPENCODE_SCHEMA = "https://opencode.ai/config.json"
_OPENCODE_DIR_PLACEHOLDER = "<the data dir your existing alice entry uses>"
_OPENCODE_SECOND_NEXT = (
    "next: nothing was written for opencode. Keep one alice entry, then run install again."
)
_OPENCODE_HAND_NEXT = (
    "Add the alice entry above under mcp by hand, then check it with: opencode mcp list"
)
_OPENCODE_CONFIG_ENVS = (
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_CONFIG_CONTENT",
)


def _opencode_config_home(home: Path) -> tuple[Path, str | None]:
    """``XDG_CONFIG_HOME`` when it is absolute, else ``home / ".config"``."""

    raw = os.environ.get("XDG_CONFIG_HOME")
    if raw is None or raw == "":
        return home / ".config", None
    if raw.startswith("~") or not Path(raw).is_absolute():
        return home / ".config", f"XDG_CONFIG_HOME {raw} is not an absolute path"
    return Path(raw), None


def _parse_opencode_json(raw: bytes) -> dict[str, Any] | None:
    """Strict JSON, or None.

    UTF-8, no BOM, no duplicate keys, no non-finite float. ``parse_constant``
    raises, so ``NaN`` and ``Infinity`` are refused.
    """

    if raw.startswith(b"\xef\xbb\xbf"):
        return None

    def reject_constant(name: str) -> None:
        raise ValueError(name)

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, value in pairs:
            if key in parsed:
                raise ValueError("duplicate key")
            parsed[key] = value
        return parsed

    try:
        text = raw.decode("utf-8")
        loaded = json.loads(text, parse_constant=reject_constant, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(loaded, dict):
        return None
    if _opencode_non_finite(loaded):
        return None
    return loaded


def _opencode_non_finite(value: object) -> bool:
    """True when ``value`` holds a NaN or an infinity, including ``1e999``."""

    if isinstance(value, float):
        return value != value or value in {float("inf"), float("-inf")}
    if isinstance(value, dict):
        return any(_opencode_non_finite(item) for item in value.values())
    if isinstance(value, list):
        return any(_opencode_non_finite(item) for item in value)
    return False


def _opencode_blank(raw: bytes | None) -> bool:
    return raw is None or not raw.strip()


def _opencode_alice_count(doc: Mapping[str, Any]) -> int:
    mcp = doc.get("mcp")
    if not isinstance(mcp, dict):
        return 0
    count = 1 if "alice" in mcp else 0
    servers = mcp.get("servers")
    if isinstance(servers, dict) and "alice" in servers:
        count += 1
    return count


def _opencode_command_entry(data_dir: str, launcher: Launcher) -> dict[str, object]:
    return {
        "type": "local",
        "command": [launcher.command, *launcher.prefix, "--data-dir", data_dir],
    }


def _opencode_as_install_entry(entry: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """OpenCode's array ``command`` as the string ``command`` plus ``args``.

    ``type`` is held aside. Key order of the original entry is not copied:
    ``_plan_entry`` only needs the launcher shape.
    """

    if entry is None:
        return None
    if parse_launcher(entry, command_array=True) is None:
        return dict(entry)
    words = entry.get("command")
    assert isinstance(words, list)  # nosec B101 # command_array parse accepts only a string list
    return {"command": words[0], "args": list(words[1:])}


def _install_entry_as_opencode(
    planned: Mapping[str, Any], original: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The planned install entry back in OpenCode shape, original keys kept."""

    command = planned.get("command")
    args = planned.get("args")
    if not isinstance(command, str) or not isinstance(args, list):
        raise _MalformedHostFile("the planned OpenCode entry has no command array")
    if not all(isinstance(arg, str) for arg in args):
        raise _MalformedHostFile("the planned OpenCode command is not a list of strings")
    words = [command, *args]
    if any("{env:" in word or "{file:" in word for word in words):
        raise _MalformedHostFile("a command word holds {env: or {file:")
    if original is None:
        return {"type": "local", "command": words}
    rebuilt: dict[str, Any] = {}
    saw_type = False
    saw_command = False
    for key, value in original.items():
        if key == "args":
            continue
        if key == "type":
            rebuilt["type"] = "local"
            saw_type = True
            continue
        if key == "command":
            rebuilt["command"] = words
            saw_command = True
            continue
        rebuilt[key] = value
    if not saw_type:
        rebuilt = {"type": "local", **rebuilt}
    if not saw_command:
        rebuilt["command"] = words
    return rebuilt


def _opencode_env_notes() -> list[str]:
    notes: list[str] = []
    for name in _OPENCODE_CONFIG_ENVS:
        if os.environ.get(name):
            notes.append(
                f"note: {name} is set, so OpenCode may load config that install did not write"
            )
    return notes


def _install_opencode_host(
    *,
    home: Path,
    config_home: Path,
    explicit_dir: str | None,
    default_dir: str,
    dry_run: bool,
    search: LauncherSearch,
    xdg_problem: str | None,
) -> _HostResult:
    """Write OpenCode's strict JSON file. A ``.jsonc`` file is not edited."""

    files = host_file_map(home, config_home=config_home)["opencode"]
    launcher = search.launcher or UVX_LAUNCHER
    data_dir = explicit_dir or default_dir
    details = _opencode_env_notes()
    session_start = "none"

    def receipt(action: str, path: Path, **extra: Any) -> str:
        return _format_host_receipt(
            host="opencode",
            mcp_path=path,
            hooks_path=None,
            action=action,
            details=details,
            session_start=session_start,
            data_dir=data_dir,
            launcher=launcher,
            file_format="json",
            **extra,
        )

    def refused(path: Path, reason: str, next_line: str, *, placeholder: bool = False) -> _HostResult:
        shown_dir = _OPENCODE_DIR_PLACEHOLDER if placeholder else data_dir
        shown, hidden = _masked(_opencode_command_entry(shown_dir, launcher))
        trailer = []
        if hidden:
            trailer.append(_keep_line(hidden))
        if dry_run:
            trailer.append(_DRY_RUN_REFUSAL)
        trailer.append(next_line)
        details.insert(0, f"reason: {reason}")
        return _HostResult(
            receipt(
                _refusal_action(dry_run),
                path,
                snippet=_alice_entry_snippet("opencode", shown),
                trailer=tuple(trailer),
            ),
            "refused",
        )

    if xdg_problem is not None:
        return refused(files["mcp"], xdg_problem, "next: nothing was written for opencode.")

    legacy_config = config_home / "opencode" / "config"
    if legacy_config.exists():
        return refused(
            files["mcp"],
            f"{legacy_config.name} exists and install does not migrate it",
            _OPENCODE_SECOND_NEXT,
        )

    jsonc_paths = (files["jsonc"], files["home_jsonc"])
    for path in jsonc_paths:
        if path.is_file() and not _opencode_blank(_read_host_file(path)):
            return refused(
                path,
                "an opencode.jsonc file is present, and install does not edit JSONC",
                f"next: nothing was written for opencode. {_OPENCODE_HAND_NEXT}",
                placeholder=True,
            )

    scanned: list[tuple[Path, bytes, dict[str, Any]]] = []
    alice_total = 0
    for key in ("mcp", "legacy", "home_json"):
        path = files[key]
        if not path.is_file():
            continue
        try:
            target = _host_target(path)
            raw = _read_host_file(target)
        except (_MalformedHostFile, OSError) as problem:
            reason = str(problem) if isinstance(problem, _MalformedHostFile) else _FAILED_REASON
            return refused(path, reason, f"next: nothing was written for opencode. Fix {path.name}, then run install again.")
        if _opencode_blank(raw):
            continue
        assert raw is not None  # nosec B101 # a non-blank read returned bytes
        doc = _parse_opencode_json(raw)
        if doc is None:
            return refused(
                path,
                f"{path.name} is not strict JSON",
                f"next: nothing was written for opencode. Fix {path.name}, then run install again.",
            )
        alice_total += _opencode_alice_count(doc)
        scanned.append((path, raw, doc))

    if alice_total > 1:
        return refused(files["mcp"], "alice appears more than once", _OPENCODE_SECOND_NEXT)

    target_path = files["mcp"]
    target_raw: bytes | None = None
    target_doc: dict[str, Any] = {}
    for path, raw, doc in scanned:
        if _opencode_alice_count(doc):
            target_path = path
            target_raw = raw
            target_doc = doc
            break
    else:
        for path, raw, doc in scanned:
            if path == files["mcp"]:
                target_path = path
                target_raw = raw
                target_doc = doc
                break

    try:
        write_target = _host_target(target_path) if target_path.exists() else target_path
    except _MalformedHostFile as problem:
        return refused(target_path, str(problem), f"next: nothing was written for opencode. Fix {target_path.name}, then run install again.")

    mcp_value = target_doc.get("mcp")
    if mcp_value is not None and not isinstance(mcp_value, dict):
        return refused(
            target_path,
            "mcp is not an object",
            f"next: nothing was written for opencode. Fix {target_path.name}, then run install again.",
        )
    existing = mcp_value.get("alice") if isinstance(mcp_value, dict) else None
    if existing is not None and not isinstance(existing, dict):
        return refused(
            target_path,
            "mcp.alice is not an object install wrote",
            f"next: {target_path.name} was not changed. {_OPENCODE_HAND_NEXT}",
        )
    install_existing = _opencode_as_install_entry(existing)
    if isinstance(existing, dict) and parse_launcher(existing, command_array=True) is None:
        install_existing = existing
    try:
        plan = _plan_entry(
            install_existing,
            key_label="mcp.alice",
            explicit_dir=explicit_dir,
            new_entry_dir=default_dir,
            default_dir=default_dir,
            home=home,
            search=search,
            with_env=False,
            needs_hook=False,
        )
    except _MalformedHostFile as problem:
        return refused(target_path, str(problem), f"next: nothing was written for opencode. Fix {target_path.name}, then run install again.")

    data_dir = plan.data_dir
    launcher = plan.launcher
    details.extend(plan.details)
    if plan.refusal is not None:
        return refused(
            target_path,
            plan.refusal,
            f"next: {target_path.name} was not changed. {plan.next_step or _OPENCODE_HAND_NEXT}",
        )
    if plan.entry is None:
        return refused(
            target_path,
            "install did not plan an OpenCode entry",
            f"next: {target_path.name} was not changed. {_OPENCODE_HAND_NEXT}",
        )
    try:
        opencode_entry = _install_entry_as_opencode(
            plan.entry, existing if isinstance(existing, dict) else None
        )
    except _MalformedHostFile as problem:
        return refused(target_path, str(problem), f"next: {target_path.name} was not changed. {_OPENCODE_HAND_NEXT}")

    doc = target_doc
    if not doc and target_raw is None:
        doc = {"$schema": _OPENCODE_SCHEMA}
    elif not doc and _opencode_blank(target_raw):
        doc = {"$schema": _OPENCODE_SCHEMA}
    _set_nested(doc, ("mcp", "alice"), opencode_entry)
    changed = target_raw is None or doc != target_doc or _opencode_blank(target_raw)
    # target_doc may be the same object as doc after _set_nested. Compare to a copy.
    # The scan stored target_doc and then we mutated it. Re-parse the raw for equality.
    if target_raw is not None and not _opencode_blank(target_raw):
        original_doc = _parse_opencode_json(target_raw)
        changed = original_doc != doc
    elif target_raw is not None:
        changed = True

    shown, hidden = _masked(opencode_entry)
    snippet = _alice_entry_snippet("opencode", shown)
    if dry_run:
        trailer = ((_hidden_line(hidden),) if hidden else ())
        return _HostResult(
            receipt("dry-run", target_path, snippet=snippet, trailer=trailer),
            "ok",
            plan.used_fallback,
        )
    if not changed:
        return _HostResult(receipt("unchanged", target_path, snippet=None), "ok", plan.used_fallback)

    backup_dir = _backup_dir(plan.data_dir)
    try:
        if target_raw is not None:
            backup = _backup_host_file(write_target, target_raw, backup_dir=backup_dir, host="opencode")
            details.append(f"backup: {backup}")
        _write_text(write_target, _dump_json(doc))
    except _BackupFailed as failure:
        details[:0] = [f"file: {write_target}", f"reason: {failure.reason()}"]
        return _HostResult(receipt("failed", target_path, snippet=None), "failed", plan.used_fallback)
    except (OSError, InstallError):
        details[:0] = [f"file: {write_target}", f"reason: {_FAILED_REASON}"]
        return _HostResult(receipt("failed", target_path, snippet=None), "failed", plan.used_fallback)
    return _HostResult(receipt("written", target_path, snippet=None), "ok", plan.used_fallback)


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
    planned = _plan_hosts(hosts)
    search = find_launcher()
    results: list[_HostResult] = []
    for host in planned:
        if host == "hermes":
            results.append(
                _install_hermes_host(
                    home=resolved_home,
                    explicit_dir=explicit_dir,
                    default_dir=default_dir,
                    dry_run=dry_run,
                    search=search,
                )
            )
        elif host == "opencode":
            config_home = resolved_home / ".config"
            xdg_problem = None
            if home is None:
                config_home, xdg_problem = _opencode_config_home(resolved_home)
            results.append(
                _install_opencode_host(
                    home=resolved_home,
                    config_home=config_home,
                    explicit_dir=explicit_dir,
                    default_dir=default_dir,
                    dry_run=dry_run,
                    search=search,
                    xdg_problem=xdg_problem,
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
                    search=search,
                )
            )
    if write_mcpb:
        results.append(
            _install_mcpb(Path(write_mcpb), dry_run=dry_run, uvx_on_path=search.uvx_on_path)
        )
    blocks = [result.receipt for result in results]
    if search.warning is not None and any(result.used_fallback for result in results):
        # A warning, not a refusal: the files are still right once uv is installed.
        blocks.insert(0, search.warning)
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
    "Launcher",
    "MCP_SCRIPT",
    "SESSION_START_COMMAND",
    "UVX_LAUNCHER",
    "UVX_MISSING_WARNING_PREFIX",
    "UV_TEMP_ENV_WARNING",
    "build_mcpb_manifest",
    "find_launcher",
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
