"""Per-project memory S2: a project brief leaves out global notes in the sensitive domains (spec 6.4, tests 75 and 76).

The owner ruled (Q8) that in a session with a detected project the brief leaves out every
global note whose stored domain is family, health, spiritual, legal or financial, from
every section it has. Global means the scope holds no Alice project id. A note of this
project in the same domain still shows. Explicit searches are unchanged.

``build_exclusion_vault`` gives every note a canary word, so each assertion says which
note reached the brief. Each test names the edit that makes it fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.project_view import ProjectView
from alicebot_api.session_briefing import sensitive_global_exclusion
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_retrieval import VNextRetrievalService
from tests.unit.per_project_s2_support import add_loop, add_memory, capture, context_for, db_path_for
from tests.unit.per_project_view_support import (
    PROJECT_A,
    SENSITIVE_DOMAIN_LABELS,
    USER_ID,
    build_exclusion_vault,
    compile_view_brief,
    project_context,
    project_view,
)

HELD_BACK = frozenset(SENSITIVE_DOMAIN_LABELS)


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in ("ALICE_PROJECT_SCOPING", "ALICE_PROJECT_DIR", "ALICE_AGENT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    built = build_exclusion_vault(data_dir)
    return data_dir, built


def _word(canaries: dict[str, str], kind: str, domain: str, side: str) -> str:
    return canaries[f"{kind}:{domain}:{side}"]


def test_every_section_leaves_out_global_sensitive_notes_and_keeps_the_projects_own(vault) -> None:
    """Mutation: apply the rule to the facts only, or exclude by domain in the project query too.

    Inside a detected project, no global family, health, spiritual, legal or financial fact,
    open loop or source excerpt appears. The project's own notes in the same five domains do,
    in the excerpts too, and so do plain global notes and the free-form name ``acme``. The
    same vault in the ``all`` view shows the held-back notes, so the assertions are not vacuous.
    """

    data_dir, built = vault
    canaries = built["canaries"]  # type: ignore[index]
    brief = compile_view_brief(data_dir, project_view(), query="harbour ledger")
    for domain in SENSITIVE_DOMAIN_LABELS:
        for kind in ("f", "l", "s"):
            assert _word(canaries, kind, domain, "g") not in brief, (kind, domain, brief)
        for kind in ("f", "l", "s"):
            assert _word(canaries, kind, domain, "p") in brief, (kind, domain, brief)
    assert _word(canaries, "f", "project", "a") in brief, "the free-form name acme is a global note and shows"
    assert _word(canaries, "f", "project", "g") in brief
    assert _word(canaries, "l", "project", "g") in brief
    assert _word(canaries, "s", "project", "g") in brief
    assert _word(canaries, "f", "project", "b") not in brief, "another project's note never shows"
    assert _word(canaries, "s", "project", "b") not in brief

    everything = compile_view_brief(data_dir, ProjectView.for_project(project_context(), "all"), query="harbour ledger")
    assert any(_word(canaries, "f", domain, "g") in everything for domain in SENSITIVE_DOMAIN_LABELS)
    assert any(_word(canaries, "l", domain, "g") in everything for domain in SENSITIVE_DOMAIN_LABELS)
    assert any(_word(canaries, "s", domain, "g") in everything for domain in SENSITIVE_DOMAIN_LABELS)


def test_global_and_all_views_show_the_held_back_notes(vault) -> None:
    """Mutation: key the rule to the scope tuple instead of the ``project`` view.

    ``--scope global`` and ``--scope all`` are the owner asking, so the global and all views
    carry an empty exclusion and show every global note the ceiling allows.
    """

    data_dir, built = vault
    canaries = built["canaries"]  # type: ignore[index]
    for choice in ("global", "all"):
        view = ProjectView.for_project(project_context(), choice)  # type: ignore[arg-type]
        assert sensitive_global_exclusion(view) == frozenset()
        brief = compile_view_brief(data_dir, view)
        assert any(_word(canaries, "f", domain, "g") in brief for domain in SENSITIVE_DOMAIN_LABELS), choice


def test_the_project_only_view_has_no_global_notes_to_hold_back(vault) -> None:
    """Mutation: let ``project_only`` read the global query.

    ``project_only`` is this project's notes alone, so no global note of any domain appears and
    the rule has nothing to do.
    """

    data_dir, built = vault
    canaries = built["canaries"]  # type: ignore[index]
    brief = compile_view_brief(data_dir, project_view(choice="project_only"), query="harbour ledger")
    assert _word(canaries, "f", "project", "p") in brief
    assert _word(canaries, "f", "project", "g") not in brief
    assert "(global)" not in brief


def test_recent_change_merges_leave_out_a_held_back_note(tmp_path: Path) -> None:
    """Mutation: drop the exclusion from the event queries or from ``_event_target_honours_fence``.

    An old global health fact and loop are touched after ten newer project notes, so only the
    recent-change merge can bring them into the brief. The ``all`` view shows both (the control),
    and the project view shows neither.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        old = add_memory(store, key="fact.old", text="Old global health fact qzrecentm", domain="health", sensitivity="private")
        old_loop = add_loop(store, title="Old global health loop qzrecentl", domain="health", sensitivity="private")
        for index in range(10):
            add_memory(store, key=f"fact.p{index}", text=f"Project fact number {index} alpha", scope=(PROJECT_A,))
            add_loop(store, title=f"Project loop number {index} alpha", scope=(PROJECT_A,))
        store.update_memory(memory_id=str(old["id"]), patch={"summary": "touched"})
        store.update_open_loop(loop_id=str(old_loop["id"]), patch={"description": "touched"})

    control = compile_view_brief(data_dir, ProjectView.for_project(project_context(), "all"))
    assert "qzrecentm" in control and "qzrecentl" in control, "the merge must reach both, or this test is vacuous"
    held = compile_view_brief(data_dir, project_view())
    assert "qzrecentm" not in held
    assert "qzrecentl" not in held


def _queries_sent_to_the_search(monkeypatch: pytest.MonkeyPatch, data_dir: Path, view: ProjectView) -> list[str]:
    seen: list[str] = []

    def stub(self, *, query, **_kwargs):  # type: ignore[no-untyped-def]
        seen.append(query)
        return [], {}

    with monkeypatch.context() as patch:
        patch.setattr(VNextRetrievalService, "search_source_excerpts", stub)
        compile_view_brief(data_dir, view)
    return seen


def test_a_held_back_fact_is_never_the_excerpt_query(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: derive the excerpt query from the rows before the exclusion filter.

    The newest facts in the vault are the global notes in the five domains. The query comes
    from the newest fact that is shown, which is this project's own.
    """

    data_dir, built = vault
    canaries = built["canaries"]  # type: ignore[index]
    sent = _queries_sent_to_the_search(monkeypatch, data_dir, project_view())
    assert len(sent) == 1
    for domain in SENSITIVE_DOMAIN_LABELS:
        assert _word(canaries, "f", domain, "g") not in sent[0]
        assert _word(canaries, "l", domain, "g") not in sent[0]
    assert _word(canaries, "f", "project", "p") in sent[0]


def test_a_held_back_source_is_never_the_excerpt_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: skip ``_source_honours_fence``'s exclusion in ``_resolve_excerpt_query``.

    With no fact and no loop, the query is a source title. The newest source is a global
    health source, so the query must come from the next one.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context = context_for(data_dir)
    capture(context, "# plain\n\nplain body text\n", title="Plain global source title")
    capture(
        context,
        "# clinic\n\nclinic body text\n",
        title="Clinic held back source title",
        domain="health",
        sensitivity="private",
    )
    sent = _queries_sent_to_the_search(monkeypatch, data_dir, project_view())
    assert sent == ["Plain global source title"]
    control = _queries_sent_to_the_search(monkeypatch, data_dir, ProjectView.for_project(project_context(), "all"))
    assert control == ["Clinic held back source title"], "the unfiltered brief asks about the newest source"


def _leg_vault(tmp_path: Path, *, title: str, text: str, query_fact: str | None = None):
    """A vault with one global health source, one project fact and one plain global fact."""

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context = context_for(data_dir)
    held = capture(context, text, title=title, domain="health", sensitivity="private")
    own = capture(
        context,
        "# own\n\nThe project runbook about gardening tools.\n",
        title="Own gardening source",
        domain="health",
        sensitivity="private",
        project_scope=(PROJECT_A,),
    )
    plain = capture(context, "# plain\n\nA plain global note about pottery.\n", title="Plain pottery source")
    return data_dir, held["source_id"], own["source_id"], plain["source_id"]


def test_the_chunk_leg_leaves_out_a_held_back_source(tmp_path: Path) -> None:
    """Mutation: apply the exclusion to the title leg and the provenance leg only.

    The held-back source matches by chunk text alone (its text holds the query word, its title
    does not). The unfiltered brief shows it, so the leg is real.
    """

    data_dir, _held, _own, _plain = _leg_vault(
        tmp_path,
        title="Clinic letter",
        text="# letter\n\nThe follow up visit is chunkonlyword in March.\n",
    )
    control = compile_view_brief(data_dir, ProjectView.for_project(project_context(), "all"), query="chunkonlyword")
    assert "chunkonlyword" in control
    held = compile_view_brief(data_dir, project_view(), query="chunkonlyword")
    assert "chunkonlyword" not in held


def test_the_title_leg_leaves_out_a_held_back_source(tmp_path: Path) -> None:
    """Mutation: apply the exclusion to the chunk leg and the provenance leg only.

    The held-back source matches by title alone: its title holds the query word and its chunk
    text does not. ``titleonlyword`` therefore reaches the brief, if it does, through the
    title and recency list, whose excerpt is the first chunk.
    """

    data_dir, _held, _own, _plain = _leg_vault(
        tmp_path,
        title="Titleonlyword clinic letter",
        text="# letter\n\nThe follow up visit is in March.\n",
    )
    control = compile_view_brief(data_dir, ProjectView.for_project(project_context(), "all"), query="titleonlyword")
    assert "follow up visit" in control, "the title leg must reach the source, or this test is vacuous"
    held = compile_view_brief(data_dir, project_view(), query="titleonlyword")
    assert "follow up visit" not in held


@pytest.mark.parametrize("citing", ["project_fact", "plain_global_fact"])
def test_the_provenance_leg_leaves_out_a_held_back_source(tmp_path: Path, citing: str) -> None:
    """Mutation: leave the provenance list on the scope fence alone.

    A fact that is shown cites the held-back source in its provenance, and the source's text
    and title share no word with the query. A project fact cites it in one case and a plain
    global fact in the other. The unfiltered brief shows the excerpt, the project view does not,
    and the project's own health source, cited the same way, still shows.
    """

    data_dir, held_id, own_id, _plain_id = _leg_vault(
        tmp_path,
        title="Clinic letter",
        text="# letter\n\nThe follow up visit is in March.\n",
    )
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        if citing == "project_fact":
            fact = add_memory(store, key="fact.cites", text="The citing fact about nothing in particular.", scope=(PROJECT_A,))
        else:
            fact = add_memory(store, key="fact.cites", text="The citing fact about nothing in particular.")
        for source_id in (held_id, own_id):
            store.create_provenance_link(
                {"target_type": "memory", "target_id": fact["id"], "source_id": source_id, "evidence_role": "supports"}
            )
    query = "zzzunmatchedtoken qqqunmatched"
    control = compile_view_brief(data_dir, ProjectView.for_project(project_context(), "all"), query=query)
    assert "follow up visit" in control, "the provenance leg must reach the source, or this test is vacuous"
    held = compile_view_brief(data_dir, project_view(), query=query)
    assert "follow up visit" not in held
    assert "gardening tools" in held, "the project's own source in the same domain is not held back"


def test_the_exclusion_does_not_hide_a_global_note_in_a_plain_domain(vault) -> None:
    """Mutation: test the domain without testing that the note is global.

    A global note in the ``project`` domain, a free-form name and a note of this project in a
    held-back domain are all shown (the first three tests assert the same from the other side).
    A global note in a held-back domain, and only that, is hidden.
    """

    data_dir, built = vault
    canaries = built["canaries"]  # type: ignore[index]
    brief = compile_view_brief(data_dir, project_view())
    assert _word(canaries, "f", "health", "p") in brief
    assert _word(canaries, "f", "health", "g") not in brief


def test_held_back_notes_do_not_use_up_places(tmp_path: Path) -> None:
    """Mutation: filter after the limit.

    The vault holds eight global notes, the five newest in held-back domains. The brief still
    shows all three plain global facts and no hole where the held-back ones would sit: the
    exclusion is part of the query, so the fill is full.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for index in range(3):
            add_memory(store, key=f"fact.plain.{index}", text=f"Plain global fact number {index} plain")
        for index, domain in enumerate(SENSITIVE_DOMAIN_LABELS):
            add_memory(
                store,
                key=f"fact.held.{index}",
                text=f"Held back fact number {index} {domain}",
                domain=domain,
                sensitivity="private",
            )
    brief = compile_view_brief(data_dir, project_view())
    assert brief.count("**fact** (global)") == 3
    assert "Held back fact" not in brief


def test_with_no_project_scoping_off_or_a_failed_detection_nothing_is_held_back(vault) -> None:
    """Mutation: apply the exclusion to a view with no project, or to the unscoped view.

    ``unscoped`` (no project found, scoping off) and its outcome variants carry no exclusion,
    so every note the ceiling allows is shown as before.
    """

    data_dir, built = vault
    canaries = built["canaries"]  # type: ignore[index]
    for outcome in (None, "none", "failed", "off"):
        view = ProjectView.unscoped(outcome)  # type: ignore[arg-type]
        assert sensitive_global_exclusion(view) == frozenset()
        brief = compile_view_brief(data_dir, view)
        assert any(_word(canaries, "f", domain, "g") in brief for domain in SENSITIVE_DOMAIN_LABELS), outcome


@pytest.mark.parametrize("domain", ["unknown", "personal", "relationship"])
def test_a_note_in_a_domain_outside_the_five_still_shows(tmp_path: Path, domain: str) -> None:
    """Mutation: add ``unknown``, ``personal`` or ``relationship`` to the excluded set.

    The owner ruled to reuse the one constant, which holds five names and not these.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        add_memory(store, key="fact.other", text=f"A global {domain} fact qzotherdomain", domain=domain)
    assert "qzotherdomain" in compile_view_brief(data_dir, project_view())


def test_an_empty_result_keeps_the_nothing_stored_line_and_prints_no_count(tmp_path: Path) -> None:
    """Mutation: print how many notes were held back.

    A vault whose only notes are global health notes gives a project brief of the project line and
    ``Nothing stored yet.``, with no frame and no number, which would be metadata about what the
    rule keeps out.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        add_memory(store, key="fact.h", text="A global health fact qzonlyheld", domain="health", sensitivity="private")
        add_loop(store, title="A global health loop qzonlyheld", domain="health", sensitivity="private")
    brief = compile_view_brief(data_dir, project_view())
    lines = brief.splitlines()
    assert len(lines) == 2, brief
    assert lines[0].startswith("Alice project:")
    assert lines[1] == "Nothing stored yet."
    assert not any(character.isdigit() for character in lines[1])


def test_recall_and_the_pack_still_return_held_back_notes(vault) -> None:
    """Mutation: apply the exclusion to recall or to the pack's source search.

    Explicit searches keep the permissions and sensitivity limits they already apply (spec 6.5),
    so a global health fact and a global health source are returned by both.
    """

    data_dir, built = vault
    canaries = built["canaries"]  # type: ignore[index]
    context = built["context"]  # type: ignore[index]
    fact_word = _word(canaries, "f", "health", "g")
    source_word = _word(canaries, "s", "health", "g")
    recall = call_mcp_tool(context, name="alice_recall", arguments={"query": fact_word})
    assert fact_word in str(recall["results"])
    recall_sources = call_mcp_tool(context, name="alice_recall", arguments={"query": source_word})
    assert source_word in str(recall_sources["sources"])
    pack = call_mcp_tool(context, name="alice_context_pack", arguments={"query": source_word})
    assert source_word in str(pack)


def test_the_brief_holds_back_by_the_stored_domain_label_only(tmp_path: Path) -> None:
    """Mutation: classify a note by its text.

    A health note filed as ``professional`` shows in a project brief, as the ruling says: the rule relies on
    stored labels and does not catch a mislabelled note.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        add_memory(store, key="fact.m", text="The owner takes a blood pressure reading qzmislabelled", domain="professional")
    assert "qzmislabelled" in compile_view_brief(data_dir, project_view())
