"""The v0.20.2 release notes carry the upgrade steps, the security wording and the open limitations.

Every claim in the notes comes from an entry of the changelog. These tests pin the parts a later edit could quietly
weaken: the release identity, the security wording, the steps an upgrading user must act on (the PostgreSQL
migrations, the one-time SQLite repair, the label check and the sentence an owner reads after the upgrade), the three
statements about hidden events, the repair event and the weekly-synthesis memory on SQLite, the behaviour an
integrator sees, and the lists of what is still open and what this release closed. The names the notes give for the
repair (the state key, the cause, the migrations) are read from the code, so a change of the code that leaves the
notes behind fails here. Each test names the mutation that must fail it.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from alicebot_api.vnext_label_repair import REPAIR_CAUSE, REPAIR_STATE_KEY
from alicebot_api.vnext_label_sql import READABLE_SCAN_CAP

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_PATH = "docs/release/v0.20.2-release-notes.md"
HEADING = "## v0.20.2 \u2014 2026-10-17"
NOTE_PATH = "docs/release/derived-labels-security-note-draft.md"

OWNER_SENTENCE = (
    "After the upgrade, a derived row whose inputs are confidential is labelled confidential, and a derived row with no "
    "record is unverified. Either one leaves the owner's default recall, pack and resume, like any confidential row did "
    "in v0.20.0. The owner still reads it by id, with a request that names every sensitivity, and through `labels "
    "check`, which lists unverified rows."
)
HIDDEN_EVENTS_SENTENCE = (
    "Events of a row the key may not read that sit inside the five-event window of `recent_changes` in the context pack "
    "or in `alice_resume` are skipped and older events fill the list, so the list does not show that hidden events exist."
)
WEEKLY_SQLITE_SENTENCE = (
    "On SQLite there is no report table, so a weekly-synthesis memory there can only arrive by import or a direct "
    "insert; it has no report to name it, stays unverified and is listed by `labels check`."
)
LABELS_RAISED = "`memory.labels_raised`"


def _text(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _flat(path: str) -> str:
    return " ".join(_text(path).split())


def _section(text: str, heading: str) -> str:
    """The body of a ``## `` section of ``text``, whitespace collapsed."""

    match = re.search(rf"^## {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^## |\Z)", text, flags=re.MULTILINE | re.DOTALL)
    assert match is not None, heading
    return " ".join(match.group("body").split())


def _changelog_section() -> str:
    changelog = _text("CHANGELOG.md")
    return changelog.split(HEADING)[1].split("\n## v0.20.0")[0]


def test_the_notes_are_a_pending_candidate_with_the_exact_title_and_state() -> None:
    """Mutations, each one alone: change the title, move the state comment, set either status to published or
    recorded, add a checksums file.
    """

    lines = _text(NOTES_PATH).splitlines()
    assert lines[0] == "# Alice v0.20.2 Release Notes"
    assert lines[1] == (
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1","version":"0.20.2",'
        '"publication_status":"pending","checksums_status":"pending"} -->'
    )
    assert sum("alice-release-state" in line for line in lines) == 1
    assert not (REPO_ROOT / "docs/release/v0.20.2-checksums.txt").exists()


def test_every_version_site_names_0201_and_the_marketplace_still_pins_the_published_release() -> None:
    """Mutations, each one alone: set one version site or one plugin pin back to 0.20.0; move the marketplace pin.

    The marketplace file is the post-publication change's to move, so it still names the latest
    published release and its tag commit.
    """

    pyproject = tomllib.loads(_text("pyproject.toml"))["project"]["version"]
    assert pyproject == "0.20.2"
    assert json.loads(_text("apps/web/package.json"))["version"] == pyproject
    assert json.loads(_text("packaging/mcpb/manifest.json"))["version"] == pyproject
    assert json.loads(_text("plugins/alice-memory/.claude-plugin/plugin.json"))["version"] == pyproject
    for name in ("plugins/alice-memory/.mcp.json", "plugins/alice-memory/hooks/hooks.json"):
        assert f"alice-memory=={pyproject}" in _text(name), name
        assert "alice-memory==0.20.0" not in _text(name), name
    marketplace = json.loads(_text(".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    assert marketplace["ref"] == "v0.20.0"
    assert marketplace["sha"] == "10034879439f583514cf23f373e998656ff80cac"
    notes = _flat(NOTES_PATH)
    assert "pip install alice-memory==0.20.2 && alice-memory install" in notes
    assert "pip install -U alice-memory==0.20.2" in notes
    assert "uv tool upgrade alice-memory" in notes
    assert "uvx alice-memory@latest --version" in notes
    assert "`uvx alice-memory --version` (or `alice-memory --version` for pip) must print 0.20.2" in notes
    # A pinned uvx check does not show what the hosts' plain `uvx alice-memory` runs.
    assert "it prints the version the hosts get" not in notes
    assert "pins `alice-memory==0.20.0` and carries none of the fixes in this release" in notes


def test_the_changelog_has_the_dated_heading_and_an_empty_unreleased_section() -> None:
    """Mutations, each one alone: put an entry under Unreleased; drop the heading; change its date form; put
    another section between v0.20.2 and v0.20.0.
    """

    changelog = _text("CHANGELOG.md")
    sections = changelog.split("\n## ")
    assert sections[1] == "Unreleased\n"
    assert sections[2].startswith("v0.20.2 \u2014 2026-10-17\n")
    assert sections[3].startswith("v0.20.0 \u2014 2026-10-02\n")
    assert changelog.count(HEADING) == 1


def test_no_marker_for_the_unreleased_state_is_left_in_the_documents_or_the_code() -> None:
    """Mutation: put the marker for the unreleased state of v0.20.0 back into any document or docstring the
    release describes.

    The pattern is written as a regular expression so that this file does not hold the marker itself. The
    published v0.18.0 notes carry two dated corrections that say ``not in v0.19.0``; they are another marker and
    immutable.
    """

    pattern = re.compile(r"Unreleased\s*\(\s*on\s+main\s*,\s*not\s+in\s+v0\.20\.0")
    paths = [
        REPO_ROOT / name
        for name in ("README.md", "SECURITY.md", "CURRENT_STATE.md", "CHANGELOG.md", "ROADMAP.md", "RELEASING.md")
    ]
    for root in ("docs", "plugins", "agent-skills", "eval"):
        paths.extend(path for path in (REPO_ROOT / root).rglob("*.md"))
    for root in ("apps/api/src", "tests", "scripts"):
        paths.extend(path for path in (REPO_ROOT / root).rglob("*.py"))
    hits = [str(path.relative_to(REPO_ROOT)) for path in paths if pattern.search(path.read_text(encoding="utf-8"))]
    assert hits == []


def test_the_notes_use_no_dash_and_only_the_allowed_security_wording() -> None:
    """Mutations, each one alone: write a dash into the notes; say "audited" outside the disclosure sentence;
    write independently, third-party or penetration; name an external review tool; write an advisory id.
    """

    text = _text(NOTES_PATH)
    assert "\u2014" not in text
    assert "\u2013" not in text
    flat = " ".join(text.split())
    disclosure = "No one outside the project has audited the code."
    assert flat.count(disclosure) == 2
    assert flat.count("audited") == flat.count(disclosure)
    review = "automated security scanning and internal adversarial review, findings triaged and fixed"
    assert flat.count(review) == 2
    lowered = flat.lower()
    for forbidden in ("independently", "third-party", "third party", "penetration", "pentest", "external review"):
        assert forbidden not in lowered, forbidden
    assert "the security advisories published with this release" in lowered
    assert "ghsa-" not in lowered and "cve-" not in lowered


def test_the_upgrade_steps_name_every_action_an_existing_user_takes_in_order() -> None:
    """Mutations, each one alone: delete one numbered step; swap two; drop the command, the revision or the setting a
    step names.
    """

    steps = _section(_text(NOTES_PATH), "What to do after upgrading")
    numbers = [int(number) for number in re.findall(r"(?:^| )(\d{1,2})\. \*\*", steps)]
    assert numbers == list(range(1, 13))
    for needle in (
        "`pip install -U alice-memory==0.20.2`",
        "`uv tool upgrade alice-memory`",
        "`uvx alice-memory@latest --version`",
        "A v0.20.0 database is at revision `20260721_0094`",
        "`20261004_0095`, `20261005_0096` and `20261011_0097`, in that order",
        "A database that goes from 0094 to the head is repaired by 0096 and finds nothing to change in 0097",
        "the documented table owner, `alicebot_admin`, which is `NOSUPERUSER NOBYPASSRLS`",
        "turns FORCE row level security off on the tables it reads and writes",
        "rolls the relabels, their audit events and the FORCE change back together",
        "The downgrade keeps the repaired labels",
        "run `alicebot vnext labels check` after such a restore, and `alicebot vnext labels repair` if it lists a row",
        f"stamps the vault with the state key `{REPAIR_STATE_KEY}`",
        f"with the cause `{REPAIR_CAUSE}`",
        "The open pass never stops a vault from opening",
        "`alicebot vnext labels check` (PostgreSQL) and `alice-memory labels check` (SQLite)",
        "Check reads a snapshot and does not upgrade the live vault",
        "`labels repair` raises the rows that are below their inputs",
        "report a check that could not read the store as unavailable, never as zero",
        "`labels repair` cannot fix it: restore the input, or regenerate or delete the report",
        "makes one new report, because the run digest now covers those sources",
        "`alice-memory labels check --db PATH`",
        "stops the import with `restore_failed` before publication and writes nothing",
        "`alicebot agent keys list`",
        "`alicebot agent keys revoke <id>`",
        "`not_permitted`, `not_found` and `precondition_failed`",
        "refuses a memory whose text is over 20,000 characters",
        "HTTP 404",
        "`ALICE_PROJECT_DIR`, `ALICE_PROJECT_SCOPING` and `ALICE_MCP_COMMIT_RESULT`",
        "pins the v0.20.0 tag commit",
    ):
        assert needle in steps, needle
    assert steps.index("`20261004_0095`") < steps.index("`labels check`") < steps.index("`~global`")


def test_the_owner_facing_sentence_and_the_three_additions_are_in_the_notes_the_changelog_and_the_pages() -> None:
    """The sentence an owner reads after the upgrade, the statement about hidden events in a five-event window, the
    name of the repair event and the statement about a weekly-synthesis memory on SQLite are each in the notes, in the
    changelog and in the security note, and the event name is in the guides that describe the repair.

    Mutations, each one alone: delete one of the three sentences from the notes, the changelog or the security note;
    delete ``memory.labels_raised`` from a guide; delete ``repair_v4`` from the notes.
    """

    notes = _flat(NOTES_PATH)
    changelog = " ".join(_changelog_section().split())
    note = _flat(NOTE_PATH)
    assert OWNER_SENTENCE in notes
    assert OWNER_SENTENCE in note
    assert (
        "After the upgrade, a derived row whose inputs are confidential is labelled confidential, and a derived row with no record is unverified."
        in changelog
    )
    assert (
        "Either one leaves the owner's default recall, pack and resume, like any confidential row did in v0.20.0."
        in changelog
    )
    for name, text in (("notes", notes), ("changelog", changelog), ("security note", note)):
        assert HIDDEN_EVENTS_SENTENCE in text, name
        assert WEEKLY_SQLITE_SENTENCE in text, name
        assert LABELS_RAISED in text, name
        assert f"`{REPAIR_CAUSE}`" in text, name
    for name in ("docs/alpha/backup-and-restore.md", "docs/alpha/mcp-tools.md"):
        text = _flat(name)
        assert LABELS_RAISED in text, name
        assert f"`{REPAIR_CAUSE}`" in text, name
    assert WEEKLY_SQLITE_SENTENCE in _flat("docs/alpha/mcp-tools.md")


def test_the_names_the_notes_give_for_the_repair_are_the_ones_the_code_has() -> None:
    """The state key, the cause, the bound of a read and the migrations are read from the code.

    Mutations, each one alone: change ``REPAIR_STATE_KEY`` or ``REPAIR_CAUSE`` in ``vnext_label_repair.py``; change
    ``READABLE_SCAN_CAP``; rename a migration file; add a later migration.
    """

    assert REPAIR_STATE_KEY == "derived_labels_v4"
    assert REPAIR_CAUSE == "repair_v4"
    assert READABLE_SCAN_CAP == 128
    notes = _flat(NOTES_PATH)
    assert f"`{REPAIR_STATE_KEY}`" in notes and f"`{REPAIR_CAUSE}`" in notes
    assert "`READABLE_SCAN_CAP`" in notes and f"at most {READABLE_SCAN_CAP} rows" in notes
    versions = REPO_ROOT / "apps" / "api" / "alembic" / "versions"
    names = sorted(path.name for path in versions.glob("*.py"))
    assert names[-4:] == [
        "20260721_0094_browser_clip_capabilities.py",
        "20261004_0095_derived_restricted_domains.py",
        "20261005_0096_derived_label_floor.py",
        "20261011_0097_derived_label_citations.py",
    ]
    for old, new in (
        ("20260721_0094", "20261004_0095"),
        ("20261004_0095", "20261005_0096"),
        ("20261005_0096", "20261011_0097"),
    ):
        migration = next(path for path in versions.glob(f"{new}_*.py"))
        assert f'down_revision = "{old}"' in migration.read_text(encoding="utf-8"), new


def test_the_security_section_describes_effects_and_refers_to_the_advisories() -> None:
    """Mutations, each one alone: delete the paragraph of one effect; name an advisory by id; say a release had a
    problem that only the unreleased code had.
    """

    security = _section(_text(NOTES_PATH), "Security")
    assert "The security advisories published with this release describe the problems below." in security
    for effect in (
        "**A derived row could be read below its inputs.**",
        "**An unbound trusted key could read and change rows above its ceiling**",
        "**A refusal told the key what the row was.**",
        "**Event feeds and traces named hidden rows.**",
        "**Saved quotes and ids outlived the row's label.**",
        "**A redacted memory's words stayed readable in reports.**",
        "**A restricted caller that named no domain read all five held-back domains.**",
        "**A snapshot of the vault could outlive a killed command**",
    ):
        assert effect in security, effect
    assert "This is containment, not removal." in security
    # A key bound to `~global` read other projects' rows only in unreleased code, so no release note says it did.
    assert "A key bound to `~global` read" not in security


def test_the_notes_describe_the_behaviour_changes_an_integrator_will_notice() -> None:
    """Mutations, each one alone: delete the bullet of one group, or a status code, event or setting from it."""

    changes = _section(_text(NOTES_PATH), "Behaviour changes an integrator will notice")
    for needle in (
        "HTTP 404 for a row above a key's limits, where v0.20.0 answered HTTP 403 with a policy decision",
        "HTTP 409 for a project slug in use",
        "HTTP 503 with `Retry-After: 2`",
        "`finite_number` and `json_too_deep`",
        "`not_permitted`, `not_found` and `precondition_failed`",
        "`alice_memory_commit` refuses text over 20,000 characters",
        "`~global` is refused as a project",
        "A request with no `domains` is held to the profile",
        "`memory.labels_raised`, `open_loop.labels_raised` and `artifact.labels_raised`",
        "`scoping.changed`",
        "`alice-memory project`",
        "`sources prune --superseded`",
        "`import-markdown --supersede`",
        "`ALICE_PROJECT_DIR`, `ALICE_PROJECT_SCOPING` and `ALICE_MCP_COMMIT_RESULT`",
        "`derived labels: N below their inputs, M unverified`",
    ):
        assert needle in changes, needle
    schema = _section(_text(NOTES_PATH), "Schema and migration")
    assert schema.startswith("There is no new table, column or index.")
    assert "it never lowers a label" in schema


def test_the_known_limitations_list_what_is_still_open() -> None:
    """Mutations, each one alone: delete one of the items this release leaves open, or its reason."""

    limitations = _section(_text(NOTES_PATH), "Known limitations")
    for item in (
        "**Redacted words stay in the reports.**",
        "**An open loop made over a memory keeps what its caller typed.**",
        "**Rows a key can read may list the ids of rows it cannot read.**",
        "**A report keeps the words it was made with.**",
        "**The memory audit route lists the archived members of a derived memory,**",
        "**The vector stage of recall does not refill after hiding rows.**",
        "**Some events still name rows without a label.**",
        "**The cited-source fence does not cover everything.**",
        "**A caller with limits that reaches 128 rows of a list**",
        "**The context pack still names the id of an expired memory**",
        "**SQLite deletion keeps some data.**",
        "**A backup export refuses a row nested about 1,000 levels or more,**",
        "**A source that v0.20.0 left half built**",
        "**A native SQLite backup carries no generated artifacts,**",
        "**The `max_tokens` budget prices the full stored row, and the agent receives a compact row.**",
        "**The marketplace install runs v0.20.0.**",
        "**Confirming a pending write is not a human gate (DB-007).**",
    ):
        assert item in limitations, item
    assert "removing it is planned for v0.21.0" in limitations
    assert "withholding them is tracked for v0.21.0" in limitations
    assert "This is a known exception, not a way of meeting the requirement to filter hidden ids" in limitations
    assert (
        "The legacy `/v0` routes that stay mounted list rows whatever their label while `APP_ENV` is `development` or `test`"
        in limitations
    )


def test_the_known_limitations_drop_what_this_release_fixes() -> None:
    """Mutation: put a v0.20.0 open item that this release fixes back into the list.

    v0.20.0 listed each of these as open. They are fixed here, so they appear in the notes only as what changed.
    """

    limitations = _section(_text(NOTES_PATH), "Known limitations")
    for fixed in (
        "**The session brief, `alice_resume` and `alice_recent_decisions` still show an expired active memory.**",
        "**Consolidation and the roll-up semantic tier can send an expired memory's text to the embeddings endpoint.**",
        "**The roll-up pass reads accepted roll-up cards without checking `valid_to`.**",
        "**Promoting a reviewed artifact into a memory does not embed it.**",
        "**No command removes a stored source on SQLite.**",
        "**A refusal reaches an MCP client as `tool_request_failed,`**",
        "**The marketplace install runs v0.19.2.**",
    ):
        assert fixed not in limitations, fixed


def test_the_changelog_entries_of_this_release_do_not_describe_the_unreleased_code_as_a_release() -> None:
    """Mutation: put back a sentence that describes the state of the code before a fix as if a release had it, where
    the entry itself says no release had it.

    The sentences were removed from the entries on the weekly-synthesis memory, the 1,024-string bound, the project
    update scan, the reserved project name, source deletion, the JSON bounds, the hidden-row reads, the saved
    quotes, the memory audit and the per-project recent changes. Entries that compare with a CI baseline still name
    the pinned commit 48873b03.
    """

    section = " ".join(_changelog_section().split())
    for phrase in (
        "On main until now",
        "on main until now",
        "on main before this change",
        "regression of main and no release had it",
        "new on main",
        "exists only on main",
        "can be tried on main",
        "Until this correction, on main",
        "The first per-project slice on main",
        "undo three regressions that the derived-labels change put on main",
    ):
        assert phrase not in section, phrase
    assert "Unreleased (on" not in section
    assert "pinned main 48873b03" in section


def test_notes_say_why_the_number_is_0_20_2_and_that_nothing_was_published_as_0_20_1() -> None:
    """Mutation: drop the section, or say v0.20.1 reached PyPI, and this fails."""

    notes = (REPO_ROOT / NOTES_PATH).read_text(encoding="utf-8")
    sections = re.split(r"(?m)^## ", notes)
    why = [section for section in sections if section.startswith("Why 0.20.2 and not 0.20.1\n")]
    assert len(why) == 1
    text = " ".join(why[0].split())
    assert "Nothing was published under it: no PyPI artifact and no GitHub Release." in text
    assert "raises that job's limit to 120 minutes" in text
    assert notes.index("## Why 0.20.2 and not 0.20.1") < notes.index("## Install")


def test_the_retired_v0201_number_is_recorded_in_the_current_state() -> None:
    """Mutation: drop `v0.20.1` from the never-published list, or keep the count at five, and this fails."""

    state = " ".join((REPO_ROOT / "CURRENT_STATE.md").read_text(encoding="utf-8").split())
    assert "`v0.19.1`, superseded by `v0.19.2`; and `v0.20.1`, superseded by `v0.20.2`." in state
    assert state.count("Six tags exist that were never published") == 2
    assert "Five tags exist" not in state
