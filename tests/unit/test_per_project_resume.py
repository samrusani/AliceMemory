"""Per-project memory S2: ``alice_resume`` reads the project view and holds back global sensitive notes (spec 6.6, ruling Q25a).

On a host with no SessionStart hook (Hermes, OpenCode) ``alice_resume`` is the agent's automatic first call, so it
does the brief's job. With scoping on and a project found, a call that names no project reads this project's
items first and global items after, and leaves out global notes in the sensitive domains. A call that names a
project, an identity that declares a scope and a failed detection keep what they had. With scoping off, which is
the default until the flip, the result is what v0.20.0 returned (the parity test pins it).

Each test names the edit that makes it fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.project_identity import detect_project
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.unit.per_project_s2_support import add_loop, add_memory, context_for, db_path_for, repo_with_remote
from tests.unit.project_identity_support import config_text, make_repo

USER_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("ALICE_PROJECT_SCOPING", "ALICE_PROJECT_DIR", "ALICE_AGENT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def _project_ids(repo: Path) -> tuple[str, ...]:
    detection = detect_project(argument=str(repo))
    assert detection.context is not None
    return detection.context.ids


def _vault(tmp_path: Path, repo: Path, *, project_decision: bool = True) -> Path:
    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    project_id = _project_ids(repo)[0]
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        add_memory(store, key="d.global", text="Global decision plain", memory_type="decision")
        add_memory(
            store, key="d.health", text="Global health decision", memory_type="decision", domain="health", sensitivity="private"
        )
        if project_decision:
            add_memory(store, key="d.proj", text="Project decision here", memory_type="decision", scope=(project_id,))
        add_memory(store, key="d.other", text="Other project decision", memory_type="decision", scope=("prj_" + "b2" * 8,))
        add_loop(store, title="Global loop plain")
        add_loop(store, title="Global health loop", domain="health", sensitivity="private")
        add_loop(store, title="Project loop here", scope=(project_id,))
        add_loop(store, title="Other project loop", scope=("prj_" + "b2" * 8,))
    return data_dir


def _resume(data_dir: Path, repo: Path | None, **arguments: object) -> dict:
    context = MCPRuntimeContext(
        database_url=sqlite_url_for_path(db_path_for(data_dir)),
        user_id=USER_ID,
        project_dir=str(repo) if repo is not None else None,
    )
    return call_mcp_tool(context, name="alice_resume", arguments=dict(arguments))["brief"]


def _loop_titles(brief: dict) -> list[str]:
    return [str(row["title"]).strip('"') for row in brief["open_loops"]]


def _decision(brief: dict) -> str | None:
    decision = brief.get("last_decision")
    return str(decision["canonical_text"]).strip('"') if decision else None


def test_with_scoping_on_resume_returns_the_projects_items_first_and_holds_back_global_sensitive_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: leave ``alice_resume`` on the unscoped view, or drop the exclusion from its reads.

    Scoping is on and the working folder is a repository. The last decision is the project's, the open loops are
    the project's first and a plain global one second, the global health loop is held back and so is another
    project's loop. Without scoping the same call returns the newest items of the whole vault, health included.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _vault(tmp_path, repo)
    off = _resume(data_dir, repo)
    assert "Global health loop" in _loop_titles(off) and "Other project loop" in _loop_titles(off)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    on = _resume(data_dir, repo)
    assert _decision(on) == "Project decision here"
    assert _loop_titles(on) == ["Project loop here", "Global loop plain"]
    assert on["next_action"]["title"].strip('"') == "Project loop here"
    assert "Global health loop" not in str(on)
    assert "Other project" not in str(on)


def test_resume_falls_back_to_a_global_decision_when_the_project_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: return no decision when the project has none, or take the held-back health decision.

    The newest global decision is a health note, so it is held back, and the fallback is the plain global one.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _vault(tmp_path, repo, project_decision=False)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    brief = _resume(data_dir, repo)
    assert _decision(brief) == "Global decision plain"


def test_a_call_that_names_a_project_keeps_todays_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: add global notes to a call that named a project.

    ``project`` and ``project_scope`` are explicit: the caller gets exactly that project and no global note,
    as before per-project memory.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _vault(tmp_path, repo)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    project_id = _project_ids(repo)[0]
    for arguments in ({"project": project_id}, {"project_scope": [project_id]}):
        brief = _resume(data_dir, repo, **arguments)
        assert _loop_titles(brief) == ["Project loop here"], arguments
        assert _decision(brief) == "Project decision here"
    named = _resume(data_dir, repo, project_scope=["prj_" + "b2" * 8])
    assert _loop_titles(named) == ["Other project loop"]


def test_an_identity_that_declares_a_scope_gets_exactly_that_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: apply the view to a caller whose identity declares a project scope.

    A declared scope is explicit, as it is today: the caller gets that scope and no global notes added, and the
    detected project is not a grant.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _vault(tmp_path, repo)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    brief = _resume(
        data_dir,
        repo,
        agent_identity={
            "agent_id": "declared",
            "permission_profile": "trusted_local_agent",
            "project_scope": ["prj_" + "b2" * 8],
        },
    )
    assert _loop_titles(brief) == ["Other project loop"]
    assert "Project loop here" not in str(brief)


def test_with_no_project_found_resume_reads_the_whole_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: hold back sensitive global notes when no project is found.

    A folder outside any repository gives no project, so the call reads what it always read, health included
    (spec 4.7).
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _vault(tmp_path, repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    brief = _resume(data_dir, outside)
    assert set(_loop_titles(brief)) == {"Global loop plain", "Global health loop", "Project loop here", "Other project loop"}


@pytest.mark.parametrize(
    "break_repo",
    [
        pytest.param(lambda git: (git / "config").write_text("x" * (256 * 1024 + 1)), id="config-too-big"),
        pytest.param(lambda git: (git / "config").unlink(), id="config-missing"),
    ],
)
def test_a_failed_detection_does_not_hold_back_sensitive_global_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, break_repo
) -> None:
    """Mutation: apply the exclusion to a view whose outcome is ``failed`` (ruling Q25b).

    A failed detection reads the whole vault, so holding back only global sensitive notes would be inconsistent. The
    brief's status line says detection failed.
    """

    repo = make_repo(tmp_path / "broken", config=config_text("https://example.com/acme/payments.git"))
    break_repo(repo / ".git")
    data_dir = _vault(tmp_path, repo_with_remote(tmp_path / "good"))
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    brief = _resume(data_dir, repo)
    assert "Global health loop" in _loop_titles(brief)
    assert "Other project loop" in _loop_titles(brief)
