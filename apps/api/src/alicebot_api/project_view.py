"""The project view: what a read sees of the project dimension (spec 6.1, 6.2).

A ``ProjectView`` is resolved once, at the edge (the SessionStart hook,
``alice-memory brief`` and the MCP handlers), and handed to every reader. Library
code never detects a project: ``compile_session_brief``, the retrieval service
and the stores take a view or its tuple as an argument and nothing else, so the
eval harness and every unit test, which run with a git checkout as the working
folder, cannot pick up a project by accident.

The reserved marker
-------------------

``GLOBAL_PROJECT_MARKER`` (``"~global"``) exists only inside a request tuple. A
request that holds it asks for rows whose project scope holds no Alice project
id (``prj_`` and 16 lowercase hex characters). It is never stored on a note, a
source or an open loop, and it is refused as caller input. The SQL builders in
``vnext_stores.sqlite.query_predicates`` and ``project_scopes_overlap`` in
``vnext_project_scope`` are the only places that learn it.

Modes
-----

=============== ==========================================  ==========================
mode            meaning                                     ``scope``
=============== ==========================================  ==========================
``unscoped``    scoping off, or no project found            ``()`` (no fence)
``explicit``    the caller named projects (today's rule)    the caller's names
``project``     this project plus global (the default)      ``(*ids, "~global")``
``project_only`` this project's notes only                  ``ids``
``global``      notes that belong to no project             ``("~global",)``
``all``         every project and global                    ``()`` (no fence)
=============== ==========================================  ==========================

``outcome`` says why there is no project, for the plain status line of spec 6.4:
``found``, ``none`` (not in a git work tree), ``failed`` (a git directory was
found and could not be read) and ``off`` (scoping is off and the resolver was not
called). ``None`` means no view and nothing to report: a call site that has no
project view on purpose (the doctor, the demo, the eval harness, a legacy
reader) says so with ``ProjectView.unscoped()``, and it prints no status line.

Release default
---------------

Until the release that turns per-project memory on, ``RELEASE_DEFAULT`` in
``project_scoping`` is ``off``, and ``resolve_project_view`` returns
``ProjectView.unscoped()`` with no outcome when the switch is off by that default
alone. Main then behaves as the last release, byte for byte, in every output.
The status line ``Project scoping is off; searching all memory.`` is for an owner
who switched scoping off on purpose, with ``ALICE_PROJECT_SCOPING=off`` or
``alice-memory project scoping off``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

from alicebot_api.project_identity import (
    Detection,
    Outcome,
    Platform,
    ProjectContext,
    ProjectFileSystem,
    detect_project,
    host_platform,
)
from alicebot_api.project_scoping import ScopingState, read_vault_setting, resolve_scoping
from alicebot_api.vnext_project_scope import GLOBAL_PROJECT_MARKER

ViewMode = Literal["unscoped", "explicit", "project", "project_only", "global", "all"]
#: The values a ``--scope`` flag takes for a project that was found.
ViewChoice = Literal["project", "project_only", "global", "all"]
VIEW_CHOICES: tuple[ViewChoice, ...] = ("project", "project_only", "global", "all")

#: One plain line per reason there is no project (spec 6.4). Alice-authored text,
#: never a stored note, and each is a different sentence on purpose.
STATUS_LINE_NONE = "No project detected; searching all memory."
STATUS_LINE_FAILED = "Project detection failed; searching all memory."
STATUS_LINE_OFF = "Project scoping is off; searching all memory."

#: How the brief says where the id came from, keyed by ``ProjectContext.source``.
FOUND_FROM_WORDS = {
    "remote": "the git remote",
    "repo_path": "the repository folder",
}

_ROW = TypeVar("_ROW")


@dataclass(frozen=True, slots=True)
class ProjectView:
    mode: ViewMode
    #: The request tuple readers pass to the existing predicates.
    scope: tuple[str, ...]
    #: What was detected, for labelling results. ``None`` when nothing was found.
    project: ProjectContext | None
    #: Why ``project`` is ``None``, for the status line. ``None``: no view, no line.
    outcome: Outcome | None
    #: One id, never a tuple and never the marker. ``None`` when nothing was found.
    write_project: str | None

    def __post_init__(self) -> None:
        project = self.project
        ids = project.ids if project is not None else ()
        expected: tuple[str, ...] | None
        if self.mode == "project":
            expected = (*ids, GLOBAL_PROJECT_MARKER) if project is not None else None
        elif self.mode == "project_only":
            expected = ids if project is not None else None
        elif self.mode == "global":
            expected = (GLOBAL_PROJECT_MARKER,)
        elif self.mode == "all":
            expected = ()
        elif self.mode == "unscoped":
            expected = () if project is None else None
        else:
            expected = self.scope if self.scope else None
        if expected is None or self.scope != expected:
            raise ValueError(f"project view mode {self.mode!r} does not match its scope")
        if (self.outcome == "found") != (project is not None):
            raise ValueError("project view outcome 'found' needs a project, and a project needs it")
        if self.write_project is not None:
            if project is None or self.write_project != project.ids[0]:
                raise ValueError("write_project must be the primary id of the detected project")
        elif project is not None:
            raise ValueError("a detected project must carry its write_project")

    @classmethod
    def unscoped(cls, outcome: Outcome | None = None) -> ProjectView:
        """No project fence. ``outcome=None`` also means no status line."""

        if outcome == "found":
            raise ValueError("an unscoped view has no project, so its outcome is not 'found'")
        return cls("unscoped", (), None, outcome, None)

    @classmethod
    def for_project(cls, project: ProjectContext, choice: ViewChoice = "project") -> ProjectView:
        """A view over a project that was found, in one of the four view modes."""

        if choice == "project":
            scope = (*project.ids, GLOBAL_PROJECT_MARKER)
        elif choice == "project_only":
            scope = project.ids
        elif choice == "global":
            scope = (GLOBAL_PROJECT_MARKER,)
        elif choice == "all":
            scope = ()
        else:
            raise ValueError(f"unknown view choice {choice!r}")
        return cls(choice, scope, project, "found", project.ids[0])

    @classmethod
    def without_project(cls, outcome: Outcome, choice: ViewChoice | None = None) -> ProjectView:
        """A view when nothing was found: unscoped, or the global or all view on request."""

        if outcome == "found":
            raise ValueError("without_project needs an outcome other than 'found'")
        if choice == "global":
            return cls("global", (GLOBAL_PROJECT_MARKER,), None, outcome, None)
        if choice == "all":
            return cls("all", (), None, outcome, None)
        return cls("unscoped", (), None, outcome, None)

    @property
    def is_project_view(self) -> bool:
        """True for the default view: this project first, then global notes."""

        return self.mode == "project"


@dataclass(frozen=True, slots=True)
class ViewResolution:
    """A resolved view together with how it was reached, for ``project show`` style output."""

    view: ProjectView
    scoping: ScopingState
    detection: Detection | None


def resolve_project_view(
    *,
    scoping: ScopingState,
    argument_dir: str | None,
    env_project_dir: str | None,
    hook_cwd: str | None,
    process_cwd: str | None,
    choice: ViewChoice | None = None,
    platform: Platform | None = None,
    fs: ProjectFileSystem | None = None,
) -> ViewResolution:
    """Resolve the view at an edge. Never raises.

    Scoping off: the resolver is not called. Off by the release default alone
    gives ``ProjectView.unscoped()`` (no outcome, so no status line); off on
    purpose gives outcome ``off``. Scoping on: the resolver runs, and a project
    that was found gives the requested ``choice`` (``project`` by default). A
    folder with no project, or one that failed to read, gives an unscoped view
    that carries the outcome, or the ``global`` or ``all`` view when the caller
    asked for one. A ``project`` or ``project_only`` choice with no project is the
    caller's to refuse (``project_only``) or to read as the default (``project``).
    """

    if not scoping.enabled:
        if scoping.origin == "default":
            return ViewResolution(ProjectView.unscoped(), scoping, None)
        return ViewResolution(ProjectView.unscoped("off"), scoping, Detection.off())
    detection = detect_project(
        argument=argument_dir,
        env_project_dir=env_project_dir,
        hook_cwd=hook_cwd,
        process_cwd=process_cwd,
        platform=platform,
        fs=fs,
    )
    if detection.outcome == "found" and detection.context is not None:
        return ViewResolution(
            ProjectView.for_project(detection.context, choice or "project"), scoping, detection
        )
    outcome: Outcome = "failed" if detection.outcome == "failed" else "none"
    wanted = choice if choice in ("global", "all") else None
    return ViewResolution(ProjectView.without_project(outcome, wanted), scoping, detection)


PROJECT_DIR_ENV = "ALICE_PROJECT_DIR"


def working_folder() -> str | None:
    """The process working folder, or ``None`` when it cannot be read (a removed folder)."""

    try:
        return os.getcwd()
    except OSError:
        return None


def resolve_view_at_edge(
    *,
    db_path: Path | None,
    environ: Mapping[str, str],
    argument_dir: str | None,
    hook_cwd: str | None,
    process_cwd: str | None,
    choice: ViewChoice | None = None,
    platform: Platform | None = None,
    fs: ProjectFileSystem | None = None,
) -> ViewResolution:
    """Read the switch, detect the project and build the view. Never raises.

    The edge's one call: the hook, ``alice-memory brief`` and the MCP handlers
    use it, so all of them read the switch and the start folder the same way.
    A vault the switch cannot be read from falls back to the release default. A
    detection that raises is a failed detection and not an error, which the brief
    and the pack report in words (spec 4.4).
    """

    resolved_platform = platform if platform is not None else host_platform(environ)
    try:
        vault_value = read_vault_setting(db_path).value if db_path is not None else None
        scoping = resolve_scoping(environ=environ, vault_value=vault_value)
    except Exception:
        scoping = resolve_scoping(environ={}, vault_value=None)
    try:
        return resolve_project_view(
            scoping=scoping,
            argument_dir=argument_dir,
            env_project_dir=environ.get(PROJECT_DIR_ENV) or None,
            hook_cwd=hook_cwd,
            process_cwd=process_cwd,
            choice=choice,
            platform=resolved_platform,
            fs=fs,
        )
    except Exception:
        return ViewResolution(ProjectView.unscoped("failed"), scoping, None)


def status_line(view: ProjectView) -> str | None:
    """The plain line for a view with no project, or ``None`` when there is none to print.

    Printed for an unscoped or all view that carries an outcome. A global view
    searches a narrower set than the line says, and a view with a project has its
    own line, so neither prints one.
    """

    if view.mode not in ("unscoped", "all") or view.project is not None:
        return None
    if view.outcome == "none":
        return STATUS_LINE_NONE
    if view.outcome == "failed":
        return STATUS_LINE_FAILED
    if view.outcome == "off":
        return STATUS_LINE_OFF
    return None


def effective_scope_for_view(
    *,
    view: ProjectView,
    decision_scope: tuple[str, ...],
    requested_scope: tuple[str, ...],
    identity_scope: tuple[str, ...],
    identity_locked: bool,
) -> tuple[str, ...]:
    """Put the view's tuple where the policy engine left no project fence.

    A caller who named a project, an identity that declares a scope and a
    key-locked identity keep exactly what the policy engine decided: the view is
    a default and never a grant, and detection can neither widen nor narrow any
    of them. Only an empty decision, from a caller who named nothing, takes the
    view's tuple.
    """

    if decision_scope or requested_scope or identity_scope or identity_locked:
        return decision_scope
    return view.scope


def fill_counts(*, limit: int, project_available: int, global_available: int) -> tuple[int, int]:
    """How many project rows and how many global rows fill ``limit`` (spec 6.2).

    ``reserved = limit // 4`` slots are held for global rows when the project has
    more than enough rows and global has any. The project takes
    ``min(project rows, limit - min(reserved, global rows))`` and global takes what
    is left, up to its own row count. So limits 1 to 3 reserve nothing, limits 4 to
    7 reserve 1, limit 8 reserves 2 and limit 12 reserves 3.
    """

    if limit < 1:
        raise ValueError("limit must be positive")
    reserved = limit // 4
    project_take = min(project_available, limit - min(reserved, global_available))
    global_take = min(global_available, limit - project_take)
    return project_take, global_take


#: ``fetch(project_ids, exclude_global_domains, limit)`` returns the project's own
#: rows and the global rows, newest first, each at most ``limit`` long. The global
#: rows leave out ``exclude_global_domains`` before the limit, so a held-back row
#: never uses up a slot.
PartitionFetch = Callable[[tuple[str, ...], frozenset[str], int], tuple[Sequence[_ROW], Sequence[_ROW]]]


def project_first_fill(
    *,
    limit: int,
    view: ProjectView,
    exclude_global_domains: frozenset[str],
    fetch: PartitionFetch[_ROW],
) -> list[_ROW]:
    """Fill a list read with this project's rows first and global rows after (spec 6.2).

    The one helper for every read that has no query to rank by (the brief, resume,
    the open loop list). ``exclude_global_domains`` has no default, so a reader
    cannot forget the choice: the brief passes ``SENSITIVE_DOMAINS`` and every
    other caller passes an empty set where it can be seen.
    """

    if view.mode != "project" or view.project is None:
        raise ValueError("project_first_fill needs the project view")
    if not isinstance(exclude_global_domains, frozenset):
        raise TypeError("exclude_global_domains must be a frozenset")
    project_rows, global_rows = fetch(view.project.ids, exclude_global_domains, limit)
    project_take, global_take = fill_counts(
        limit=limit,
        project_available=len(project_rows),
        global_available=len(global_rows),
    )
    return [*project_rows[:project_take], *global_rows[:global_take]]


def fetch_in_two_queries(
    fetch_one: Callable[[tuple[str, ...], frozenset[str], int], Sequence[_ROW]],
) -> PartitionFetch[_ROW]:
    """A ``PartitionFetch`` built from one ordinary query run twice.

    The project query names the project's ids and leaves nothing out. The global
    query names the marker and leaves out the excluded domains. Each pays the base
    cost of a scan, which is why the brief, which has a budget, uses the
    single-scan readers of the SQLite store instead.
    """

    def fetch(
        project_ids: tuple[str, ...], exclude: frozenset[str], limit: int
    ) -> tuple[Sequence[_ROW], Sequence[_ROW]]:
        return (
            fetch_one(project_ids, frozenset(), limit),
            fetch_one((GLOBAL_PROJECT_MARKER,), exclude, limit),
        )

    return fetch


__all__ = [
    "FOUND_FROM_WORDS",
    "GLOBAL_PROJECT_MARKER",
    "PartitionFetch",
    "ProjectView",
    "STATUS_LINE_FAILED",
    "STATUS_LINE_NONE",
    "STATUS_LINE_OFF",
    "PROJECT_DIR_ENV",
    "VIEW_CHOICES",
    "ViewChoice",
    "ViewMode",
    "ViewResolution",
    "effective_scope_for_view",
    "fetch_in_two_queries",
    "fill_counts",
    "project_first_fill",
    "resolve_project_view",
    "resolve_view_at_edge",
    "status_line",
    "working_folder",
]
