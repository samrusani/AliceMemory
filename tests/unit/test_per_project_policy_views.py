"""Per-project memory S2: the view is a required argument, and no reader forgets it (spec 6.1, tests 24 and 25).

``_policy_checked`` and ``_mcp_agent_policy_preflight`` are the choke points for
every MCP read. Each takes a required keyword-only ``project_view`` with no
default, so a handler that forgets it fails the type check and this scan, and a
reader cannot fall back to "no project" by accident. A call that deliberately has
no read view writes ``ProjectView.unscoped()`` at the call site, and the list
below holds every such site.

The list shrinks as later slices give the readers a view. A new site must not
appear in it without a reviewer reading why, so the scan compares the exact set.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from collections import Counter
from pathlib import Path

import pytest

from alicebot_api.mcp.policy import _mcp_agent_policy_preflight, _policy_checked
from alicebot_api.project_view import ProjectView, project_first_fill
from alicebot_api.session_briefing import (
    compile_local_session_brief,
    compile_session_brief,
    source_scope_from_project_scope,
)
from alicebot_api.vault_sleep import compile_sleep_proposal_listing
from alicebot_api.vnext_retrieval import VNextRetrievalService, _ResolvedRetrievalScope

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "apps" / "api" / "src" / "alicebot_api"
CHOKE_POINTS = frozenset({"_policy_checked", "_mcp_agent_policy_preflight"})

#: Every call that passes ``ProjectView.unscoped()`` on purpose, by file, function and callee.
#: S2 gives ``alice_resume`` a real view. S3 gives the other view-bearing readers one and
#: removes their lines here: ``alice_recall``, ``alice_recent_decisions``, the context pack,
#: the open loop list and the review list.
ALLOWED_UNSCOPED_SITES: Counter[tuple[str, str, str]] = Counter(
    {
        ("mcp/capture_automation.py", "_handle_alice_vnext_capture", "_policy_checked"): 1,
        ("mcp/capture_automation.py", "_handle_alice_vnext_ingest_agent_output", "_policy_checked"): 1,
        ("mcp/capture_automation.py", "_handle_alice_vnext_queue_task", "_policy_checked"): 1,
        ("mcp/capture_automation.py", "_handle_alice_vnext_generate_artifact", "_policy_checked"): 1,
        ("mcp/context.py", "_vnext_context_pack_payload", "_policy_checked"): 1,
        ("mcp/context.py", "_handle_alice_vnext_context_tree", "_policy_checked"): 1,
        ("mcp/evidence_artifacts.py", "_authorize_explain_resource", "_policy_checked"): 1,
        ("mcp/evidence_artifacts.py", "_authorize_vnext_artifact_target", "_policy_checked"): 1,
        ("mcp/memories.py", "_handle_alice_vnext_recent_memory_commits", "_policy_checked"): 1,
        ("mcp/memories.py", "authorize", "_policy_checked"): 1,
        ("mcp/projects.py", "_handle_alice_project_dashboard", "_mcp_agent_policy_preflight"): 1,
        ("mcp/projects.py", "_handle_alice_vnext_open_loops", "_mcp_agent_policy_preflight"): 1,
        ("mcp/projects.py", "_handle_alice_project_update_candidate", "_policy_checked"): 1,
        ("mcp/retrieval.py", "_handle_alice_recall", "_mcp_agent_policy_preflight"): 1,
        ("mcp/retrieval.py", "_handle_alice_recent_decisions", "_mcp_agent_policy_preflight"): 1,
        ("mcp/retrieval.py", "_handle_alice_recent_changes", "_mcp_agent_policy_preflight"): 1,
        ("mcp/review.py", "_vnext_memory_review", "_mcp_agent_policy_preflight"): 1,
        ("mcp/review.py", "_vnext_memory_correct", "_policy_checked"): 2,
        ("mcp/review.py", "_vnext_memory_review", "_policy_checked"): 1,
        ("mcp/scheduler.py", "_handle_alice_vnext_scheduler_run_now", "_policy_checked"): 1,
        ("mcp/scheduler.py", "_handle_alice_vnext_scheduler_run_due", "_policy_checked"): 1,
        ("mcp/scheduler.py", "_handle_alice_vnext_scheduler_pause", "_policy_checked"): 1,
        ("mcp/scheduler.py", "_handle_alice_vnext_scheduler_resume", "_policy_checked"): 1,
        ("mcp/synthesis.py", "_handle_alice_generate_connections", "_mcp_agent_policy_preflight"): 1,
        ("mcp/synthesis.py", "_handle_alice_generate_contradictions", "_mcp_agent_policy_preflight"): 1,
        # The legacy brain adapters preflight here and pass the resulting identity
        # and scope to the producer, which applies all-of admission.
        ("mcp/synthesis.py", "_authorized_brain_request", "_mcp_agent_policy_preflight"): 1,
    }
)

#: Calls that hand the choke point a view that is not the literal ``ProjectView.unscoped()``.
#: The preflight forwards the one its caller gave it, and ``alice_resume`` reads the project view.
ALLOWED_FORWARDED_SITES: Counter[tuple[str, str, str]] = Counter(
    {
        ("mcp/policy.py", "_mcp_agent_policy_preflight", "_policy_checked"): 1,
        ("mcp/retrieval.py", "_handle_alice_resume", "_mcp_agent_policy_preflight"): 1,
    }
)


def _choke_point_calls() -> list[tuple[str, str, str, ast.expr | None]]:
    """Every call of a choke point under ``alicebot_api``: file, enclosing function, callee, view."""

    found: list[tuple[str, str, str, ast.expr | None]] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            name = (
                callee.id
                if isinstance(callee, ast.Name)
                else callee.attr
                if isinstance(callee, ast.Attribute)
                else None
            )
            if name not in CHOKE_POINTS:
                continue
            owner: ast.AST = node
            while owner in parents and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = parents[owner]
            function = owner.name if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"
            view = next((keyword.value for keyword in node.keywords if keyword.arg == "project_view"), None)
            found.append((path.relative_to(SOURCE_ROOT).as_posix(), function, name, view))
    return found


def test_every_read_choke_point_call_names_its_view_and_every_unscoped_site_is_listed() -> None:
    """Mutation: drop ``project_view=`` from one handler, or pass ``ProjectView.unscoped()`` from a new one.

    The scan reads every call of ``_policy_checked`` and ``_mcp_agent_policy_preflight`` in the
    package. A call with no ``project_view`` keyword fails, and so does a call that passes the
    literal unscoped view from a function that is not on the list. Making ``alice_resume`` pass
    ``ProjectView.unscoped()`` changes the unscoped set and fails the second half.
    """

    calls = _choke_point_calls()
    assert len(calls) >= 25, "the scan found too few call sites to be reading the package"
    missing = [(file, function, callee) for file, function, callee, view in calls if view is None]
    assert missing == [], f"call sites with no project_view argument: {missing}"
    unscoped: Counter[tuple[str, str, str]] = Counter()
    forwarded: Counter[tuple[str, str, str]] = Counter()
    for file, function, callee, view in calls:
        assert view is not None
        key = (file, function, callee)
        if ast.unparse(view) == "ProjectView.unscoped()":
            unscoped[key] += 1
        else:
            forwarded[key] += 1
    assert unscoped == ALLOWED_UNSCOPED_SITES
    assert forwarded == ALLOWED_FORWARDED_SITES


@pytest.mark.parametrize(
    ("function", "names"),
    [
        (_policy_checked, ("project_view",)),
        (_mcp_agent_policy_preflight, ("project_view",)),
        (
            compile_session_brief,
            (
                "effective_domains",
                "effective_sensitivity_allowed",
                "effective_project_scope",
                "project_view",
                "exclude_global_domains",
                "query",
            ),
        ),
        (compile_local_session_brief, ("project_view", "exclude_global_domains", "query")),
        (
            compile_sleep_proposal_listing,
            ("effective_project_scope", "project_view", "exclude_global_domains"),
        ),
        (source_scope_from_project_scope, ("exclude_global_domains",)),
        (VNextRetrievalService.search_source_excerpts, ("scope",)),
        (project_first_fill, ("limit", "view", "exclude_global_domains", "fetch")),
    ],
    ids=[
        "policy_checked",
        "policy_preflight",
        "compile_session_brief",
        "compile_local_session_brief",
        "sleep_proposal_listing",
        "source_scope_helper",
        "search_source_excerpts",
        "project_first_fill",
    ],
)
def test_the_view_and_the_exclusion_have_no_default(function: object, names: tuple[str, ...]) -> None:
    """Mutation: give ``project_view`` or ``exclude_global_domains`` a default on any of these.

    A default is the way a new caller forgets the choice. ``ProjectView.unscoped()`` and the
    empty set are written where they are meant, in plain sight.
    """

    parameters = inspect.signature(function).parameters  # type: ignore[arg-type]
    for name in names:
        assert name in parameters, f"{name} is missing from {function}"
        assert parameters[name].default is inspect.Parameter.empty, f"{name} has a default on {function}"
        assert parameters[name].kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )


def test_the_resolved_source_scope_has_no_default_for_the_exclusion() -> None:
    """Mutation: give ``_ResolvedRetrievalScope.exclude_global_domains`` a default.

    The field is how the brief holds back global sensitive-domain sources in every leg of the
    excerpt search. A site that builds a scope must state the choice, so a new site cannot
    quietly leave the rule off.
    """

    fields = {field.name: field for field in dataclasses.fields(_ResolvedRetrievalScope)}
    exclusion = fields["exclude_global_domains"]
    assert exclusion.default is dataclasses.MISSING
    assert exclusion.default_factory is dataclasses.MISSING


def test_the_unscoped_view_is_the_only_view_a_call_site_may_write_inline() -> None:
    """Mutation: let ``ProjectView.unscoped()`` default to the project view, or to a status outcome.

    The call sites on the list write it to say "no view on purpose". It must carry no outcome,
    so it prints no status line, and no project, so it fences nothing.
    """

    view = ProjectView.unscoped()
    assert view.mode == "unscoped"
    assert view.scope == ()
    assert view.project is None
    assert view.outcome is None
    assert view.write_project is None
