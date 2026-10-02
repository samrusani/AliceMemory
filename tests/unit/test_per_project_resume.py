"""Per-project memory S2: ``alice_resume`` reads the project view and holds back global sensitive notes (spec 6.6, ruling Q25a).

On a host with no SessionStart hook (Hermes, OpenCode) ``alice_resume`` is the agent's automatic first call, so it
does the brief's job. With scoping on and a project found, a call that names no project reads this project's
items first and global items after, and leaves out global notes in the sensitive domains. A call that names a
project, an identity that declares a scope and a failed detection keep what they had. With scoping off, which is
the default until the flip, the result is what v0.20.0 returned (the parity test pins it).

Each test names the edit that makes it fail.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from alicebot_api.mcp import retrieval as mcp_retrieval
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.onramp import main as onramp_main
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.project_identity import detect_project
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_memory_commit import SENSITIVE_DOMAINS
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


#: Global notes in each sensitive domain, newest in the vault, so they head every list that does not leave them out.
HELD_BACK_NOTES = (
    ("memory", "Global health fact", "health"),
    ("memory", "Global family fact", "family"),
    ("loop", "Global legal loop", "legal"),
    ("loop", "Global financial loop", "financial"),
    ("loop", "Global spiritual loop", "spiritual"),
)


def _event_vault(tmp_path: Path, repo: Path, *, project_notes: bool) -> Path:
    """A vault whose newest events all belong to held-back global notes.

    Oldest first: a plain global fact and loop, then (optionally) the repository's own fact and loop and another
    project's, then one global note in each sensitive domain. Every create writes an event, and an event row holds
    only ids, so the tests below map each ``target_id`` back to the note it points at.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    project_id = _project_ids(repo)[0]
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        add_memory(store, key="e.plain", text="Global plain fact")
        add_loop(store, title="Global plain loop")
        if project_notes:
            add_memory(store, key="e.project", text="Project fact here", scope=(project_id,))
            add_loop(store, title="Project loop here", scope=(project_id,))
            add_memory(store, key="e.other", text="Other project fact", scope=("prj_" + "b2" * 8,))
            add_loop(store, title="Other project loop", scope=("prj_" + "b2" * 8,))
        for kind, title, domain in HELD_BACK_NOTES:
            if kind == "memory":
                add_memory(store, key=f"e.{domain}", text=title, domain=domain, sensitivity="private")
            else:
                add_loop(store, title=title, domain=domain, sensitivity="private")
    return data_dir


def _pointed_at(data_dir: Path, brief: dict) -> list[tuple[str, str]]:
    """What each ``recent_changes`` row points at, as ``(kind, title)``, read by id the way a client would."""

    pointed: list[tuple[str, str]] = []
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for event in brief["recent_changes"]:
            target_id = event["target_id"]
            if event["target_type"] == "memory":
                row = store.get_memory(target_id)
                pointed.append(("memory", str(row["canonical_text"]) if row else "missing"))
            else:
                row = store.get_open_loop(target_id)
                pointed.append(("loop", str(row["title"]) if row else "missing"))
    return pointed


def test_recent_changes_never_point_at_a_held_back_global_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: drop the held-back rule from both layers at once, so that held-back ids reach the result.

    The two layers are the event queries (``exclude_global_domains=()`` in the two event reads of ``read_events``
    in ``_vnext_resume``, or ``global_excluded_domains=()`` in the two store event readers) and the by-id check (the
    ``_resource_is_held_back_global`` line of ``_resume_event_honours_policy_fence``). Each layer alone is caught by
    the two tests below, because the other layer hides the leak. With both gone the ids get through, and this test
    sees them.

    The by-id tools ignore the project view, so an id in ``recent_changes`` is a pointer to the note it names. The
    vault's five newest events belong to global notes in the five sensitive domains, and none of them may be
    pointed at in the project view. The control is the same call with scoping off, which points at every one of the
    five, so the fixture really holds what the assertion says is absent.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _event_vault(tmp_path, repo, project_notes=True)
    held_back = {(kind, title) for kind, title, _domain in HELD_BACK_NOTES}
    control = _pointed_at(data_dir, _resume(data_dir, repo, max_recent_changes=20))
    assert held_back <= set(control), "the control must point at every held-back note, or the fixture is vacuous"
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    brief = _resume(data_dir, repo, max_recent_changes=20)
    pointed = _pointed_at(data_dir, brief)
    assert not held_back & set(pointed), pointed
    assert ("memory", "Other project fact") not in pointed and ("loop", "Other project loop") not in pointed
    assert set(pointed) == {
        ("memory", "Project fact here"),
        ("loop", "Project loop here"),
        ("memory", "Global plain fact"),
        ("loop", "Global plain loop"),
    }, pointed


def test_a_held_back_event_does_not_use_up_a_place_in_the_recent_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: pass ``exclude_global_domains=()`` in the memory event read, or in the loop event read, of
    ``read_events`` in ``_vnext_resume``, or ``global_excluded_domains=()`` in ``list_resume_memory_events`` or in
    ``list_open_loop_events``.

    With two places, the two newest global events of each kind are held-back notes. The queries leave them out
    before the limit, so the plain global fact and the plain global loop, which are older, fill the two places. If
    one query kept them, the by-id check would still drop them, and the places they took would stay empty: one
    of the two plain events would be missing. Each of the four edits removes one of the two.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _event_vault(tmp_path, repo, project_notes=False)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    brief = _resume(data_dir, repo, max_recent_changes=2)
    assert sorted(_pointed_at(data_dir, brief)) == [("loop", "Global plain loop"), ("memory", "Global plain fact")]


def test_the_by_id_check_of_resume_events_holds_back_global_notes_and_only_global_ones(tmp_path: Path) -> None:
    """Mutation: delete the ``_resource_is_held_back_global`` line of ``_resume_event_honours_policy_fence``, or test the
    domain without testing that the note is global.

    The check is the second layer behind the event queries: it reads the note an event points at and refuses a
    global one in a held-back domain. A note of the project in the same domain stays (the rule is about what follows
    a person into every project), a global note in another domain stays, and with nothing to hold back
    everything stays.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    project_id = _project_ids(repo)[0]
    held_back = frozenset(SENSITIVE_DOMAINS)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        rows = {
            "global_health_memory": add_memory(store, key="c.gh", text="gh", domain="health", sensitivity="private"),
            "project_health_memory": add_memory(
                store, key="c.ph", text="ph", domain="health", sensitivity="private", scope=(project_id,)
            ),
            "free_form_health_memory": add_memory(
                store, key="c.fh", text="fh", domain="health", sensitivity="private", scope=("acme",)
            ),
            "global_plain_memory": add_memory(store, key="c.gp", text="gp"),
            "global_health_loop": add_loop(store, title="gh", domain="health", sensitivity="private"),
            "project_health_loop": add_loop(store, title="ph", domain="health", sensitivity="private", scope=(project_id,)),
            "global_plain_loop": add_loop(store, title="gp"),
        }

        def honoured(name: str, exclude: frozenset[str]) -> bool:
            event = {
                "target_type": "open_loop" if name.endswith("loop") else "memory",
                "target_id": str(rows[name]["id"]),
            }
            return mcp_retrieval._resume_event_honours_policy_fence(  # type: ignore[attr-defined]
                store,
                event,
                effective_domains=(),
                effective_sensitivity_allowed=("public", "internal", "private", "unknown"),
                exclude_global_domains=exclude,
            )

        for name in rows:
            held = name.startswith("global_health") or name.startswith("free_form_health")
            assert honoured(name, held_back) is (not held), name
            assert honoured(name, frozenset()) is True, name


def test_the_mcp_command_hands_its_project_dir_to_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: drop ``project_dir=args.project_dir`` from the ``MCPRuntimeContext`` call in ``_run_mcp``.

    A server started from a folder outside any repository, with ``--project-dir`` pointing at a repository,
    answers ``alice_resume`` for that repository: its own items first and a plain global one, and nothing from
    another project or held back. Without the argument the server reads the whole vault, which has the other
    project's loop and the global health loop in it. The server is the real one, run through the command with
    its standard streams replaced by buffers.
    """

    repo = repo_with_remote(tmp_path / "repo")
    data_dir = _vault(tmp_path, repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "alice_resume", "arguments": {}}},
    ]
    stdin = io.TextIOWrapper(io.BytesIO("".join(json.dumps(item) + "\n" for item in requests).encode("utf-8")))
    stdout = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    assert onramp_main(["mcp", "--data-dir", str(data_dir), "--project-dir", str(repo)]) == 0
    stdout.flush()
    replies = [json.loads(line) for line in stdout.buffer.getvalue().decode("utf-8").splitlines()]
    answer = next(item for item in replies if item.get("id") == 2)
    brief = json.loads(answer["result"]["content"][0]["text"])["brief"]
    assert _loop_titles(brief) == ["Project loop here", "Global loop plain"]
    assert _decision(brief) == "Project decision here"
    assert "Other project" not in str(brief) and "Global health" not in str(brief)
