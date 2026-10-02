"""Per-project memory S2: with scoping off, every output is what v0.20.0 printed.

The goldens in ``fixtures_v0200_per_project_goldens`` were produced by v0.20.0 code
from the vault ``per_project_s2_support.build_parity_vault`` builds. Each test
here runs today's code over a fresh copy of that vault, from a folder that WOULD
resolve to a project if scoping were on, and compares byte for byte.

Scoping is off by the release default until the flip (spec 17, S5), so the
default state is "no ``ALICE_PROJECT_SCOPING``, no vault row". Switched off on
purpose, the brief and the hook add the one plain status line and nothing else.

Each test names the edit that makes it fail.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from alicebot_api import session_start_hook
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.onramp import main as onramp_main
from alicebot_api.project_view import STATUS_LINE_OFF, ProjectView
from alicebot_api.session_briefing import compile_local_session_brief
from alicebot_api.vault_sleep import compile_sleep_proposal_listing, run_local_vault_sleep
from alicebot_api.vnext_agent_control import DEFAULT_AGENT_SENSITIVITY
from tests.unit.fixtures_v0200_per_project_goldens import GOLDENS
from tests.unit.per_project_s2_support import (
    USER_ID,
    build_parity_vault,
    db_path_for,
    normalize_result,
    repo_with_remote,
)

CLEARED_ENV = (
    "ALICE_PROJECT_SCOPING",
    "ALICE_PROJECT_DIR",
    "ALICE_MEMORY_DATA_DIR",
    "CLAUDE_PLUGIN_ROOT",
    "ALICE_AGENT_API_KEY",
)


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in CLEARED_ENV:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    repo = repo_with_remote(tmp_path / "repo")
    context = build_parity_vault(data_dir)
    monkeypatch.chdir(repo)
    return data_dir, repo, context


def _hook(monkeypatch: pytest.MonkeyPatch, capsys, data_dir: Path, repo: Path, fmt: str) -> str:
    payload = json.dumps({"cwd": str(repo), "hook_event_name": "SessionStart", "source": "startup"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert session_start_hook.main(["--format", fmt, "--data-dir", str(data_dir)]) == 0
    return capsys.readouterr().out


def _brief(data_dir: Path, query: str | None = None) -> str:
    return compile_local_session_brief(
        db_path_for(data_dir),
        user_id=USER_ID,
        query=query,
        project_view=ProjectView.unscoped(),
        exclude_global_domains=frozenset(),
    )


def test_the_library_brief_equals_the_v0200_golden(vault) -> None:
    """Mutation: add a line, a mark or a reorder to ``_render_brief`` when the view is unscoped.

    The library has no line (spec 14), so its text equals v0.20.0 exactly, with and without a query.
    """

    data_dir, _repo, _context = vault
    assert _brief(data_dir) == GOLDENS["brief_no_query"]
    assert _brief(data_dir, "release gate") == GOLDENS["brief_query"]


def test_the_hook_prints_the_v0200_brief_from_a_folder_that_would_resolve(
    vault, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Mutation: make the hook resolve a project while the switch holds its release default.

    The folder the host sends is a repository with a remote, and the working folder is
    the same repository. Scoping is off by default, so nothing is detected and the output
    is the v0.20.0 brief, in both formats.
    """

    data_dir, repo, _context = vault
    assert _hook(monkeypatch, capsys, data_dir, repo, "markdown") == GOLDENS["hook_markdown"]
    assert _hook(monkeypatch, capsys, data_dir, repo, "json") == GOLDENS["hook_json"]


def test_alice_memory_brief_prints_the_v0200_brief(vault, capsys) -> None:
    """Mutation: let ``alice-memory brief`` follow the working folder while scoping is at its default."""

    data_dir, _repo, _context = vault
    assert onramp_main(["brief", "--data-dir", str(data_dir)]) == 0
    assert capsys.readouterr().out == GOLDENS["cli_brief"]


def test_scope_and_project_dir_are_accepted_and_ignored_while_scoping_is_off(vault, capsys) -> None:
    """Mutation: apply ``--scope global`` or ``--project-dir`` while scoping is off."""

    data_dir, repo, _context = vault
    for flags in (["--scope", "global"], ["--scope", "project_only"], ["--project-dir", str(repo)]):
        assert onramp_main(["brief", "--data-dir", str(data_dir), *flags]) == 0
        assert capsys.readouterr().out == GOLDENS["cli_brief"]


def test_alice_resume_equals_the_v0200_golden(vault) -> None:
    """Mutation: let ``alice_resume`` read the project view while scoping is off.

    Ids and timestamps are normalized, ``generated_at`` is dropped. The working folder is
    a repository with a remote.
    """

    _data_dir, _repo, context = vault
    assert normalize_result(call_mcp_tool(context, name="alice_resume", arguments={})) == GOLDENS["resume"]
    assert (
        normalize_result(
            call_mcp_tool(context, name="alice_resume", arguments={"max_open_loops": 3, "max_recent_changes": 3})
        )
        == GOLDENS["resume_filters"]
    )


@pytest.mark.parametrize(
    ("tool", "arguments", "golden"),
    [
        ("alice_recent_decisions", {}, "recent_decisions"),
        ("alice_open_loops", {}, "open_loops"),
        ("alice_recall", {"query": "payments retries backoff"}, "recall"),
        ("alice_context_pack", {"query": "release gate and payments"}, "context_pack"),
    ],
)
def test_the_other_readers_equal_the_v0200_golden(vault, tool: str, arguments: dict, golden: str) -> None:
    """Mutation: pass a view other than unscoped to ``_policy_checked`` from one of these readers."""

    _data_dir, _repo, context = vault
    assert normalize_result(call_mcp_tool(context, name=tool, arguments=arguments)) == GOLDENS[golden]


def test_sleep_proposals_equal_the_v0200_golden(vault) -> None:
    """Mutation: change the order of the fence and the committed-fact count for a view that is not ``project``."""

    data_dir, _repo, _context = vault
    db = db_path_for(data_dir)
    run_local_vault_sleep(db, user_id=USER_ID)
    listing = compile_sleep_proposal_listing(
        db,
        user_id=USER_ID,
        effective_domains=(),
        effective_sensitivity_allowed=DEFAULT_AGENT_SENSITIVITY,
        effective_project_scope=(),
        project_view=ProjectView.unscoped(),
        exclude_global_domains=frozenset(),
    )
    assert normalize_result(listing) == GOLDENS["sleep_proposals"]


def test_switched_off_on_purpose_adds_one_line_and_nothing_else(
    vault, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Mutation: print the off line for the release default, or add anything else to an explicit off.

    ``ALICE_PROJECT_SCOPING=off`` is an owner's choice. The hook, the CLI and
    ``alice_resume`` still print the v0.20.0 text, with the one status line of spec 6.4 after the
    frame in the brief.
    """

    data_dir, repo, context = vault
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    expected = GOLDENS["brief_no_query"]
    assert isinstance(expected, str)
    frame, rest = expected.split("\n", 1)
    with_line = f"{frame}\n{STATUS_LINE_OFF}\n{rest}"
    assert _hook(monkeypatch, capsys, data_dir, repo, "markdown") == with_line + "\n"
    assert onramp_main(["brief", "--data-dir", str(data_dir)]) == 0
    assert capsys.readouterr().out == with_line + "\n"
    assert normalize_result(call_mcp_tool(context, name="alice_resume", arguments={})) == GOLDENS["resume"]
    # The library has no line, so a call with no view on purpose still equals v0.20.0.
    assert _brief(data_dir) == expected


def test_the_vault_row_off_is_off_and_the_environment_beats_it(
    vault, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Mutation: read the vault row after the environment, or ignore the row."""

    data_dir, repo, _context = vault
    assert onramp_main(["project", "scoping", "off", "--data-dir", str(data_dir)]) == 0
    capsys.readouterr()
    expected = GOLDENS["brief_no_query"]
    assert isinstance(expected, str)
    frame, rest = expected.split("\n", 1)
    out = _hook(monkeypatch, capsys, data_dir, repo, "markdown")
    assert out == f"{frame}\n{STATUS_LINE_OFF}\n{rest}\n"
