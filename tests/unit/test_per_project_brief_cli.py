"""Per-project memory S2: ``alice-memory brief`` and ``sleep-proposals`` follow the folder (spec 6.4, 6.8, 13, tests 37 and 75).

``brief`` and ``sleep-proposals`` gain ``--project-dir`` and ``--scope project|project_only|global|all``. With scoping
on, run from inside a repository, they show the project's notes first. ``--scope all`` is the old whole-vault output for
one run and ``--scope global`` shows only notes that belong to no project, both with the global sensitive-domain notes
the project view holds back. With scoping off, which is the default until the flip, the flags are accepted and ignored.
``sleep`` itself is vault-wide always. Each test names the edit that makes it fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alicebot_api.onramp import main as onramp_main
from alicebot_api.project_identity import detect_project
from alicebot_api.project_view import STATUS_LINE_NONE, STATUS_LINE_OFF
from alicebot_api.session_briefing import SESSION_BRIEF_FRAME
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vault_sleep import SLEEP_PROPOSAL_CAP, sleep_proposals_path
from tests.unit.per_project_s2_support import (
    add_memory,
    capture,
    context_for,
    db_path_for,
    repo_with_remote,
)
from tests.unit.per_project_view_support import USER_ID

PROJECT_B = "prj_" + "b2" * 8


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("ALICE_PROJECT_SCOPING", "ALICE_PROJECT_DIR", "ALICE_MEMORY_DATA_DIR", "ALICE_AGENT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def _project_id(repo: Path) -> str:
    detection = detect_project(argument=str(repo))
    assert detection.context is not None
    return detection.context.ids[0]


def _vault(tmp_path: Path, repo: Path) -> Path:
    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    project_id = _project_id(repo)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        add_memory(store, key="fact.global", text="Plain global fact qzplain")
        add_memory(store, key="fact.health", text="Global health fact qzhealth", domain="health", sensitivity="private")
        add_memory(store, key="fact.own", text="Own project fact qzown", scope=(project_id,))
        add_memory(store, key="fact.other", text="Other project fact qzother", scope=(PROJECT_B,))
    return data_dir


def _brief(capsys: pytest.CaptureFixture[str], data_dir: Path, *flags: str, command: str = "brief") -> tuple[int, str, str]:
    code = onramp_main([command, "--data-dir", str(data_dir), *flags])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_brief_follows_the_working_folder_when_scoping_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: leave ``alice-memory brief`` on the unscoped view, or read the working folder before ``--project-dir``.

    Run from inside a repository subfolder with scoping on, the brief opens with the project line, the project's
    notes come first, global notes follow with their mark, the global health note is held back and another
    project's note never shows. ``--project-dir`` beats the working folder.
    """

    repo = repo_with_remote(tmp_path / "payments")
    sub = repo / "pkg"
    sub.mkdir()
    data_dir = _vault(tmp_path, repo)
    monkeypatch.chdir(sub)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    code, out, _err = _brief(capsys, data_dir)
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == SESSION_BRIEF_FRAME
    assert lines[1].startswith('Alice project: "payments"')
    assert lines[2] == '**fact**: "Own project fact qzown"'
    assert '**fact** (global): "Plain global fact qzplain"' in out
    assert "qzhealth" not in out
    assert "qzother" not in out
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    code, out_none, _err = _brief(capsys, data_dir)
    assert out_none.splitlines()[1] == STATUS_LINE_NONE
    code, out_arg, _err = _brief(capsys, data_dir, "--project-dir", str(repo))
    assert out_arg == out


def test_scope_all_is_the_old_whole_vault_output_and_global_shows_only_global_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: key the exclusion to the scope tuple, or print the project line for ``all``.

    ``--scope all`` equals the brief with scoping off, byte for byte. ``--scope global`` shows the global notes,
    the held-back health note among them, and no note of any project.
    """

    repo = repo_with_remote(tmp_path / "payments")
    data_dir = _vault(tmp_path, repo)
    monkeypatch.chdir(repo)
    _code, off, _err = _brief(capsys, data_dir)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    _code, everything, _err = _brief(capsys, data_dir, "--scope", "all")
    assert everything == off
    assert "qzhealth" in everything and "qzother" in everything and "qzown" in everything
    _code, only_global, _err = _brief(capsys, data_dir, "--scope", "global")
    assert "qzhealth" in only_global and "qzplain" in only_global
    assert "qzown" not in only_global and "qzother" not in only_global
    assert "Alice project:" not in only_global


def test_scope_project_only_shows_the_projects_notes_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: let ``project_only`` read global notes."""

    repo = repo_with_remote(tmp_path / "payments")
    data_dir = _vault(tmp_path, repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    code, out, _err = _brief(capsys, data_dir, "--scope", "project_only")
    assert code == 0
    assert "qzown" in out
    assert "qzplain" not in out and "qzhealth" not in out and "qzother" not in out


def test_scope_project_only_with_no_project_is_refused_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: answer a question about one project's notes with every note when no project is found.

    That quiet answer is the failure this feature exists to prevent. The command exits 2 and names the way out.
    With scoping off the flag is ignored, so a script that passes it works either way.
    """

    repo = repo_with_remote(tmp_path / "payments")
    data_dir = _vault(tmp_path, repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    code, out, err = _brief(capsys, data_dir, "--scope", "project_only")
    assert code == 2
    assert out == ""
    assert json.loads(err)["error"]["code"] == "project_not_found"
    assert "--scope all" in json.loads(err)["error"]["message"]
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    code, out_off, _err = _brief(capsys, data_dir, "--scope", "project_only")
    assert code == 0 and "qzown" in out_off


def test_scope_project_with_no_project_found_is_the_whole_vault_with_the_status_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: refuse ``--scope project`` when no project is found.

    ``project`` is the default view, and with no project it reads the whole vault and says so (spec 4.7).
    """

    repo = repo_with_remote(tmp_path / "payments")
    data_dir = _vault(tmp_path, repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    code, out, _err = _brief(capsys, data_dir, "--scope", "project")
    assert code == 0
    assert out.splitlines()[1] == STATUS_LINE_NONE
    assert "qzhealth" in out


def test_an_explicit_off_adds_the_status_line_and_the_default_adds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: print the off line for the release default.

    Until the flip the release default is off and every output is v0.20.0's. An owner who switched it off on purpose
    gets the one line that says why nothing is filtered.
    """

    repo = repo_with_remote(tmp_path / "payments")
    data_dir = _vault(tmp_path, repo)
    monkeypatch.chdir(repo)
    _code, default, _err = _brief(capsys, data_dir)
    assert STATUS_LINE_OFF not in default and "Alice project:" not in default
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    _code, explicit, _err = _brief(capsys, data_dir)
    assert explicit.splitlines()[1] == STATUS_LINE_OFF
    assert "\n".join([explicit.splitlines()[0], *explicit.splitlines()[2:]]) + "\n" == default


def _sleep_vault(tmp_path: Path, repo: Path) -> Path:
    """Eight sources, the oldest first: three global sensitive, this project's two, another project's two, one plain."""

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context = context_for(data_dir)
    project_id = _project_id(repo)
    for domain in ("health", "family", "legal"):
        capture(context, f"# {domain}\n\nHeld back {domain} source qzsleep{domain}.\n", title=f"{domain} source", domain=domain, sensitivity="private")
    capture(context, "# own\n\nOwn plain source qzsleepown.\n", title="own source", project_scope=(project_id,))
    capture(context, "# own health\n\nOwn health source qzsleepownhealth.\n", title="own health source", domain="health", sensitivity="private", project_scope=(project_id,))
    capture(context, "# other\n\nOther project source qzsleepother.\n", title="other source", project_scope=(PROJECT_B,))
    capture(context, "# other two\n\nOther project source two qzsleepothertwo.\n", title="other source two", project_scope=(PROJECT_B,))
    capture(context, "# plain\n\nPlain global source qzsleepplain.\n", title="plain source")
    return data_dir


def _rows_not_shown(listing: str) -> int:
    for line in listing.splitlines():
        if line.startswith("rows not shown:"):
            return int(line.split(":", 1)[1])
    return 0  # the line is printed only when a row was left out


def test_sleep_writes_the_same_sidecar_from_inside_any_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: apply the view to the sleep writer.

    ``alice-memory sleep`` proposes the oldest unlinked sources of the whole vault, whatever folder it runs in and
    whether scoping is on. The sidecar is byte for byte the same from a repository, from outside one and with
    scoping on.
    """

    repo = repo_with_remote(tmp_path / "payments")
    outside = tmp_path / "outside"
    outside.mkdir()
    sidecars: list[str] = []
    for index, (folder, scoping) in enumerate(((outside, None), (repo, None), (repo, "on"), (outside, "on"))):
        monkeypatch.chdir(folder)
        if scoping:
            monkeypatch.setenv("ALICE_PROJECT_SCOPING", scoping)
        else:
            monkeypatch.delenv("ALICE_PROJECT_SCOPING", raising=False)
        run_root = tmp_path / f"run{index}"
        run_root.mkdir()
        data_dir = _sleep_vault(run_root, repo)
        code = onramp_main(["sleep", "--data-dir", str(data_dir)])
        capsys.readouterr()
        assert code == 0
        sidecar = sleep_proposals_path(db_path_for(data_dir)).read_text(encoding="utf-8")
        rows = [json.loads(line) for line in sidecar.splitlines() if line.strip()]
        sidecars.append("\n".join(str(row.get("excerpt", "")) for row in rows))
    assert len(set(sidecars)) == 1
    assert "qzsleephealth" in sidecars[0] and "qzsleepother" in sidecars[0], "the writer is vault-wide"


def test_sleep_proposals_in_a_project_lists_only_this_projects_and_global_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: apply the view to the writer, leave it out of the listing, or count a source the project fence left out.

    In the project view the listing shows this project's sources and plain global ones. It leaves out global
    sources in the five sensitive domains (spec 6.4 item 8) and another project's sources, and its
    ``rows not shown`` line counts neither. In the unscoped legacy fence, the same sources are counted.
    """

    repo = repo_with_remote(tmp_path / "payments")
    data_dir = _sleep_vault(tmp_path, repo)
    monkeypatch.chdir(repo)
    assert onramp_main(["sleep", "--data-dir", str(data_dir)]) == 0
    capsys.readouterr()
    assert SLEEP_PROPOSAL_CAP >= 8
    _code, everything, _err = _brief(capsys, data_dir, command="sleep-proposals")
    for word in ("qzsleephealth", "qzsleepfamily", "qzsleeplegal", "qzsleepother", "qzsleepown", "qzsleepplain"):
        assert word in everything, word
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    _code, listing, _err = _brief(capsys, data_dir, command="sleep-proposals")
    assert "qzsleepown" in listing and "qzsleepownhealth" in listing and "qzsleepplain" in listing
    for word in ("qzsleephealth", "qzsleepfamily", "qzsleeplegal", "qzsleepother"):
        assert word not in listing, word
    assert _rows_not_shown(listing) == 0, "another project's rows and held-back rows are not counted"
    _code, all_view, _err = _brief(capsys, data_dir, "--scope", "all", command="sleep-proposals")
    assert "qzsleephealth" in all_view and "qzsleepother" in all_view
    _code, global_view, _err = _brief(capsys, data_dir, "--scope", "global", command="sleep-proposals")
    assert "qzsleephealth" in global_view and "qzsleepown" not in global_view


def test_the_legacy_fence_still_counts_the_sources_it_leaves_out(tmp_path: Path) -> None:
    """Mutation: skip the count for every view, not only the project view.

    With an explicit project fence and no project view, the listing is what it was: ``rows not shown`` counts every
    source the fence leaves out. The silence is for the project view only, because the number of another project's
    rows is metadata that view does not give.
    """

    from alicebot_api.project_view import ProjectView
    from alicebot_api.vault_sleep import compile_sleep_proposal_listing, run_local_vault_sleep

    repo = repo_with_remote(tmp_path / "payments")
    data_dir = _sleep_vault(tmp_path, repo)
    database = db_path_for(data_dir)
    run_local_vault_sleep(database, user_id=USER_ID)
    listing = compile_sleep_proposal_listing(
        database,
        user_id=USER_ID,
        effective_domains=(),
        effective_sensitivity_allowed=("public", "internal", "private", "unknown"),
        effective_project_scope=(_project_id(repo),),
        project_view=ProjectView.unscoped(),
        exclude_global_domains=frozenset(),
    )
    assert "qzsleepown" in listing and "qzsleepother" not in listing
    assert _rows_not_shown(listing) == 6


def test_a_source_with_a_committed_fact_is_still_counted_in_a_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: skip the committed-fact count in the project view.

    The listing still counts a source of this project that already has a committed memory, as v0.20.0 did, and a
    held-back source with one is counted by nothing.
    """

    repo = repo_with_remote(tmp_path / "payments")
    data_dir = _sleep_vault(tmp_path, repo)
    database = db_path_for(data_dir)
    monkeypatch.chdir(repo)
    assert onramp_main(["sleep", "--data-dir", str(data_dir)]) == 0
    capsys.readouterr()
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        sources = {str(row["title"]): str(row["id"]) for row in store.search_sources(query="source", limit=50)}
        for title in ("own source", "health source"):
            memory = add_memory(store, key=f"committed.{title}", text=f"Committed from {title}")
            store.create_provenance_link(
                {"target_type": "memory", "target_id": memory["id"], "source_id": sources[title], "evidence_role": "supports"}
            )
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    _code, listing, _err = _brief(capsys, data_dir, command="sleep-proposals")
    assert _rows_not_shown(listing) == 1, "own source counted, the held-back one with a committed fact is not"
