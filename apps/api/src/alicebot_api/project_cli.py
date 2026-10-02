"""``alice-memory project``: show, report and scoping (spec 8.2, 8.4, 5.3).

- ``show [--project-dir PATH]`` prints what a folder resolves to: the project
  label, the id or ids, how the project was found, which start-folder source
  was used and whether scoping is on. It prints the folder the owner typed back
  to them, never a path it found and never a remote URL. It does not write.
- ``report`` counts the notes in the vault by project: global notes, notes under
  free-form names and notes under Alice project ids, for memories, sources and
  open loops. It does not write.
- ``scoping on|off|status`` reads or writes the per-project scoping switch
  (``project_scoping.py``).

No other command reads any of this yet. ``show`` and ``report`` open the vault
read-only and never create it.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alicebot_api.credential_floor import credential_verdict
from alicebot_api.project_identity import (
    Detection,
    detect_project,
    is_alice_project_id,
    sanitize_label,
)
from alicebot_api.project_scoping import (
    SCOPING_ENV,
    ScopingState,
    ScopingValue,
    read_vault_setting,
    resolve_scoping,
    set_vault_setting,
)
from alicebot_api.vnext_project_scope import (
    project_scope_identity,
    resolve_project_scope,
    resolve_source_metadata_project_scope,
)
from alicebot_api.vnext_recall_visibility import MEMORY_SEARCHABLE_STATUSES

PROJECT_DIR_ENV = "ALICE_PROJECT_DIR"
#: Longest typed folder, or stored name, printed back.
_PRINT_LIMIT = 200
_NAME_LIMIT = 80
#: Most projects and names listed. The rest are counted in one line.
_LIST_LIMIT = 50

_REASON_TEXT = {
    "no_usable_start_folder": (
        "none of --project-dir, ALICE_PROJECT_DIR or the working folder is an "
        "absolute folder that exists"
    ),
    "reached_home_folder": "the walk up reached the home folder without finding a git repository",
    "reached_filesystem_root": (
        "the walk up reached the top of the filesystem without finding a git repository"
    ),
    "walk_limit_reached": "no git repository within 32 folders above the start folder",
    "git_entry_unreadable": "a .git entry was found and could not be read",
    "git_file_unreadable": "the .git file could not be read",
    "git_file_too_large": "the .git file is larger than 4 KiB",
    "git_file_malformed": "the .git file does not start with a gitdir line",
    "git_dir_unreadable": "the git directory could not be read",
    "common_dir_unreadable": "the common git directory could not be read",
    "config_missing_or_unreadable": "the git config is missing or could not be read",
    "config_too_large": "the git config is larger than 256 KiB",
    "config_malformed": "the git config could not be parsed",
    "config_uses_include": "the git config uses include, which is not followed",
    "unexpected_error": "the project could not be worked out",
}
_START_TEXT = {
    "argument": "--project-dir",
    "env": f"{PROJECT_DIR_ENV}",
    "hook": "the host's session payload",
    "cwd": "the working folder",
}
_FOUND_FROM_TEXT = {
    "remote": "the git remote",
    "repo_path": "the repository path (the repository has no usable remote)",
}
_ORIGIN_SHORT = {
    "environment": SCOPING_ENV,
    "vault": "vault setting",
    "default": "release default",
}
_ORIGIN_LONG = {
    "environment": f"{SCOPING_ENV} in this environment",
    "vault": "this vault's setting",
    "default": "the release default (no vault setting and no valid environment value)",
}


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def add_project_parser(
    subparsers: Any, *, add_database_arguments: Callable[[argparse.ArgumentParser], None]
) -> None:
    """Add ``project`` and its three subcommands to the ``alice-memory`` parser."""

    project_parser = subparsers.add_parser(
        "project",
        help="Show what folder resolves to which project, report notes by project, switch scoping.",
        description=(
            "Per-project memory. 'show' prints what a folder resolves to and 'report' "
            "counts notes by project; neither writes. 'scoping' reads or sets the "
            "per-project scoping switch."
        ),
    )
    project_subparsers = project_parser.add_subparsers(dest="project_command", required=True)

    show_parser = project_subparsers.add_parser(
        "show",
        help="Print the project a folder resolves to. Writes nothing.",
    )
    add_database_arguments(show_parser)
    show_parser.add_argument(
        "--project-dir",
        default=None,
        help=(
            "Folder to resolve. Without it, ALICE_PROJECT_DIR, then the working folder. "
            "It must be absolute and exist, or the next source is used."
        ),
    )
    show_parser.add_argument("--json", action="store_true", dest="as_json", help="Print JSON.")

    report_parser = project_subparsers.add_parser(
        "report",
        help="Count the vault's notes by project. Writes nothing.",
    )
    add_database_arguments(report_parser)
    report_parser.add_argument("--json", action="store_true", dest="as_json", help="Print JSON.")

    scoping_parser = project_subparsers.add_parser(
        "scoping",
        help="Read or set the per-project scoping switch for this vault.",
    )
    add_database_arguments(scoping_parser)
    scoping_parser.add_argument(
        "action",
        choices=("on", "off", "status"),
        help="'status' prints the switch and where it comes from. 'on' and 'off' save it in the vault.",
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _printable(text: str, limit: int = _PRINT_LIMIT) -> str:
    """One short printable line: control characters escaped, cut with ``...``."""

    shown: list[str] = []
    used = 0
    for char in text:
        piece = char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        if used + len(piece) > limit:
            shown.append("...")
            break
        shown.append(piece)
        used += len(piece)
    return "".join(shown)


def _print_json(record: object) -> None:
    print(json.dumps(record, ensure_ascii=True, sort_keys=True))


def _emit(code: str) -> int:
    from alicebot_api.onramp import _emit_error

    _emit_error(code)
    return 1


def _scoping_state(db_path: Path) -> tuple[ScopingState, bool]:
    """The effective switch, and whether the vault setting could be read."""

    vault = read_vault_setting(db_path)
    return resolve_scoping(environ=os.environ, vault_value=vault.value), vault.readable


def _scoping_record(state: ScopingState) -> dict[str, object]:
    return {"enabled": state.enabled, "origin": state.origin, "value": state.value}


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _typed_folder(detection: Detection, *, argument: str | None, env_value: str | None) -> str | None:
    """The folder the owner typed or set, only when that source was the one used."""

    if detection.start == "argument" and argument is not None:
        return _printable(argument)
    if detection.start == "env" and env_value is not None:
        return _printable(env_value)
    return None


def _run_show(args: argparse.Namespace) -> int:
    from alicebot_api.onramp import resolve_db_path

    env_value = os.environ.get(PROJECT_DIR_ENV) or None
    try:
        process_cwd: str | None = os.getcwd()
    except OSError:
        process_cwd = None
    detection = detect_project(
        argument=args.project_dir,
        env_project_dir=env_value,
        process_cwd=process_cwd,
    )
    db_path = resolve_db_path(data_dir=args.data_dir, db=args.db)
    state, readable = _scoping_state(db_path)
    typed = _typed_folder(detection, argument=args.project_dir, env_value=env_value)
    context = detection.context

    if args.as_json:
        _print_json(
            {
                "folder_given": typed,
                "outcome": detection.outcome,
                "project": (
                    None
                    if context is None
                    else {
                        "found_from": context.source,
                        "ids": list(context.ids),
                        "label": context.label,
                    }
                ),
                "reason": detection.reason,
                "scoping": {**_scoping_record(state), "vault_readable": readable},
                "start_source": detection.start,
            }
        )
        return 0

    if context is not None:
        print(f"Project: {json.dumps(context.label)}")
        print("Ids: " + ", ".join(context.ids) + " (the first is where a new note would be saved)")
        print(f"Found from: {_FOUND_FROM_TEXT[context.source]}")
    elif detection.outcome == "failed":
        print("Project: detection failed")
    else:
        print("Project: none")
    if context is None:
        print(f"Why: {_REASON_TEXT.get(detection.reason or '', 'the project could not be worked out')}")
    if detection.start is not None:
        print(f"Start folder source: {_START_TEXT[detection.start]}")
    if typed is not None:
        print(f"Folder given: {typed}")
    print(f"Scoping: {state.value} ({_ORIGIN_SHORT[state.origin]})")
    if not readable:
        print("Note: the vault setting could not be read, so it was ignored.")
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@dataclass
class _KindCounts:
    total: int = 0
    empty_scope: int = 0
    free_form_only: int = 0
    in_project: int = 0
    names: Counter[str] = field(default_factory=Counter)
    spellings: dict[str, Counter[str]] = field(default_factory=dict)
    project_rows: Counter[str] = field(default_factory=Counter)
    labels: dict[str, Counter[str]] = field(default_factory=dict)

    def add(self, values: tuple[str, ...], metadata: Mapping[str, object]) -> None:
        self.total += 1
        identities = project_scope_identity(values)
        ids = [value for value in identities if is_alice_project_id(value)]
        # Every stored string that is not an Alice id counts as a name, even on a
        # row that also belongs to a project.
        for value in values:
            identity = project_scope_identity(value)
            key = identity[0] if identity else value
            if is_alice_project_id(key):
                continue
            self.names[key] += 1
            self.spellings.setdefault(key, Counter())[value] += 1
        if ids:
            self.in_project += 1
            label = _detected_label(metadata)
            for project_id in ids:
                self.project_rows[project_id] += 1
                if label is not None:
                    self.labels.setdefault(project_id, Counter())[label] += 1
        elif values:
            self.free_form_only += 1
        else:
            self.empty_scope += 1


@dataclass(frozen=True)
class _ProjectRow:
    id: str
    label: str | None
    rows: dict[str, int]


@dataclass(frozen=True)
class _NameRow:
    name: str
    rows: dict[str, int]


def _detected_label(metadata: Mapping[str, object]) -> str | None:
    detected = metadata.get("project_detected")
    if not isinstance(detected, Mapping):
        return None
    label = detected.get("label")
    return sanitize_label(label) if isinstance(label, str) and label else None


def _decode_metadata(text: object) -> dict[str, object]:
    if isinstance(text, (str, bytes)):
        try:
            decoded = json.loads(text)
        except ValueError:
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def _count_kind(
    connection: sqlite3.Connection,
    query: str,
    params: tuple[object, ...],
    scope_of: Callable[[sqlite3.Row | tuple[Any, ...]], tuple[tuple[str, ...], dict[str, object]]],
) -> _KindCounts:
    counts = _KindCounts()
    cursor = connection.execute(query, params)
    while rows := cursor.fetchmany(1000):
        for row in rows:
            values, metadata = scope_of(row)
            counts.add(values, metadata)
    return counts


def _memory_or_loop_scope(row: Any) -> tuple[tuple[str, ...], dict[str, object]]:
    metadata = _decode_metadata(row[0])
    resolution = resolve_project_scope({"metadata_json": metadata, "project_id": row[1]})
    return resolution.values, metadata


def _source_scope(row: Any) -> tuple[tuple[str, ...], dict[str, object]]:
    metadata = _decode_metadata(row[0])
    resolution = resolve_source_metadata_project_scope(metadata)
    return resolution.values, metadata


def _display_name(kinds: Mapping[str, _KindCounts], key: str) -> str | None:
    """A stored name as one quoted token, or ``None`` when it must be withheld.

    A name is untrusted text an agent stored. One that holds a path separator,
    a URL, an ``@`` or credential-shaped text is not printed, because the
    report must not echo a home path, a remote URL or a token.
    """

    spellings: Counter[str] = Counter()
    for counts in kinds.values():
        spellings.update(counts.spellings.get(key, Counter()))
    name = spellings.most_common(1)[0][0]
    if "/" in name or "\\" in name or "@" in name or "://" in name or credential_verdict(name) is not None:
        return None
    return _printable(name, _NAME_LIMIT)


def _kind_record(counts: _KindCounts) -> dict[str, object]:
    return {
        "empty_scope": counts.empty_scope,
        "free_form_only": counts.free_form_only,
        "global": counts.empty_scope + counts.free_form_only,
        "in_project": counts.in_project,
        "total": counts.total,
    }


def _read_kinds(db_path: Path, user_id: str) -> dict[str, _KindCounts] | None:
    """Count the three kinds of row. ``None`` when the vault could not be read."""

    from alicebot_api.onramp import _BackupError, _read_only_sqlite_connection

    statuses = json.dumps(sorted(MEMORY_SEARCHABLE_STATUSES))
    try:
        connection = _read_only_sqlite_connection(db_path)
    except (_BackupError, sqlite3.Error, OSError):
        return None
    try:
        return {
            "memories": _count_kind(
                connection,
                "SELECT metadata_json, project_id FROM memories "
                "WHERE user_id = ? AND deleted_at IS NULL "
                "AND status IN (SELECT value FROM json_each(?))",
                (user_id, statuses),
                _memory_or_loop_scope,
            ),
            "sources": _count_kind(
                connection,
                "SELECT metadata_json FROM sources WHERE user_id = ? AND deleted_at IS NULL",
                (user_id,),
                _source_scope,
            ),
            "open_loops": _count_kind(
                connection,
                "SELECT metadata_json, project_id FROM open_loops "
                "WHERE user_id = ? AND status = 'open'",
                (user_id,),
                _memory_or_loop_scope,
            ),
        }
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def _run_report(args: argparse.Namespace) -> int:
    from alicebot_api.onramp import resolve_db_path

    db_path = resolve_db_path(data_dir=args.data_dir, db=args.db)
    vault_found = db_path.is_file()
    if vault_found:
        read = _read_kinds(db_path, str(args.user_id))
        if read is None:
            return _emit("project_report_failed")
        kinds = read
    else:
        kinds = {"memories": _KindCounts(), "sources": _KindCounts(), "open_loops": _KindCounts()}

    def project_total(project_id: str) -> int:
        return sum(counts.project_rows[project_id] for counts in kinds.values())

    def name_total(key: str) -> int:
        return sum(counts.names[key] for counts in kinds.values())

    def project_label(project_id: str) -> str | None:
        labels: Counter[str] = Counter()
        for counts in kinds.values():
            labels.update(counts.labels.get(project_id, Counter()))
        return labels.most_common(1)[0][0] if labels else None

    project_ids = sorted(
        {pid for counts in kinds.values() for pid in counts.project_rows},
        key=lambda pid: (-project_total(pid), pid),
    )
    projects = [
        _ProjectRow(
            id=project_id,
            label=project_label(project_id),
            rows={name: counts.project_rows[project_id] for name, counts in kinds.items()},
        )
        for project_id in project_ids[:_LIST_LIMIT]
    ]

    names: list[_NameRow] = []
    withheld = 0
    more_names = 0
    for key in sorted(
        {key for counts in kinds.values() for key in counts.names},
        key=lambda key: (-name_total(key), key),
    ):
        display = _display_name(kinds, key)
        if display is None:
            withheld += 1
        elif len(names) < _LIST_LIMIT:
            names.append(
                _NameRow(name=display, rows={name: counts.names[key] for name, counts in kinds.items()})
            )
        else:
            more_names += 1
    more_projects = max(len(project_ids) - _LIST_LIMIT, 0)

    if args.as_json:
        _print_json(
            {
                "free_form_names": [{"name": entry.name, "rows": entry.rows} for entry in names],
                "free_form_names_more": more_names,
                "free_form_names_withheld": withheld,
                "kinds": {name: _kind_record(counts) for name, counts in kinds.items()},
                "projects": [
                    {"id": project.id, "label": project.label, "rows": project.rows} for project in projects
                ],
                "projects_more": more_projects,
                "vault_found": vault_found,
            }
        )
        return 0

    print("Project report (read only; nothing in the vault was changed)")
    if not vault_found:
        print("No vault file exists in this data directory yet.")
    print(
        "Counted: committed memories (active, accepted), sources that are not deleted, "
        "and open loops that are open."
    )
    headings = {"memories": "Memories", "sources": "Sources", "open_loops": "Open loops"}
    for name, counts in kinds.items():
        print("")
        print(f"{headings[name]}: {counts.total}")
        print(f"  global (no Alice project id): {counts.empty_scope + counts.free_form_only}")
        print(f"    empty project scope: {counts.empty_scope}")
        print(f"    free-form names only: {counts.free_form_only}")
        print(f"  in an Alice project: {counts.in_project}")
    print("")
    print("Alice projects (ids derived from a git repository):")
    if not projects:
        print("  none")
    for project in projects:
        label = json.dumps(project.label) if project.label else "(no label recorded)"
        print(
            f"  {project.id} {label}: memories {project.rows['memories']}, "
            f"sources {project.rows['sources']}, open loops {project.rows['open_loops']}"
        )
    if more_projects:
        print(f"  and {more_projects} more")
    print("")
    print(
        "Free-form names (not Alice project ids; a note that carries only these is global). "
        "A row can carry more than one name, and counts are rows:"
    )
    if not names and not withheld and not more_names:
        print("  none")
    for entry in names:
        print(
            f"  {json.dumps(entry.name)}: memories {entry.rows['memories']}, "
            f"sources {entry.rows['sources']}, open loops {entry.rows['open_loops']}"
        )
    if withheld:
        print(f"  {withheld} name(s) not shown: they look like a path, a URL or a credential")
    if more_names:
        print(f"  and {more_names} more")
    return 0


# ---------------------------------------------------------------------------
# scoping
# ---------------------------------------------------------------------------


def _run_scoping(args: argparse.Namespace) -> int:
    from alicebot_api.onramp import bootstrap_database, resolve_db_path
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection

    db_path = resolve_db_path(data_dir=args.data_dir, db=args.db)
    if args.action == "status":
        state, readable = _scoping_state(db_path)
        print(f"Project scoping: {state.value}")
        print(f"Set by: {_ORIGIN_LONG[state.origin]}")
        print(f"Vault setting: {state.vault_value if state.vault_value is not None else 'none'}")
        if state.environment_value is not None:
            print(f"Environment: {SCOPING_ENV}={state.environment_value}")
        elif state.environment_ignored:
            print(f"Environment: {SCOPING_ENV} is set to a value that is not on or off, so it is ignored")
        else:
            print(f"Environment: {SCOPING_ENV} is not set")
        if not readable:
            print("Note: the vault setting could not be read, so it was ignored.")
        return 0

    value: ScopingValue = "on" if args.action == "on" else "off"
    bootstrap_database(
        db_path,
        user_id=args.user_id,
        user_email=args.user_email,
        secure_parent=args.db is None,
    )
    with sqlite_user_connection(db_path, args.user_id) as connection:
        store = SQLiteVNextStore(connection, args.user_id)
        change = set_vault_setting(connection, store, value)
    if change.changed:
        print(f"Project scoping is saved as {value} for this vault.")
    else:
        print(f"Project scoping was already saved as {value} for this vault. Nothing changed.")
    state, _readable = _scoping_state(db_path)
    if state.origin == "environment" and state.value != value:
        print(
            f"Note: {SCOPING_ENV}={state.value} in this environment overrides the saved "
            "setting here."
        )
    elif state.environment_ignored:
        print(f"Note: {SCOPING_ENV} is set to a value that is not on or off, so it is ignored.")
    return 0


def run_project(args: argparse.Namespace) -> int:
    """Run one ``project`` subcommand. A failure prints one fixed error record."""

    try:
        if args.project_command == "show":
            return _run_show(args)
        if args.project_command == "report":
            return _run_report(args)
        return _run_scoping(args)
    except (OSError, sqlite3.Error):
        return _emit("project_failed")


__all__ = ["PROJECT_DIR_ENV", "add_project_parser", "run_project"]
