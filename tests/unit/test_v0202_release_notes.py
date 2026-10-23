"""The v0.20.2 release notes carry the upgrade steps, the security wording and the open limitations.

Every claim in the notes comes from an entry of the changelog. These tests pin the parts a later edit could quietly
weaken: the release identity, the security wording, the steps an upgrading user must act on (the PostgreSQL
migrations, the one-time SQLite repair, the label check and the sentence an owner reads after the upgrade), the three
statements about hidden events, the repair event and the weekly-synthesis memory on SQLite, the behaviour an
integrator sees, and the lists of what is still open and what this release closed. The names the notes give for the
repair (the state key, the cause, the migrations) are read from the code, so a change of the code that leaves the
notes behind fails here. Each test names the mutation that must fail it.

The notes are published and the post-publication change recorded the digests, moved the marketplace file to the
tag and added the dated 2026-10-17 updates. The v0.21.0 post-publication change moved the marketplace file on to the
v0.21.0 tag and added the dated 2026-10-22 updates, which say beside each statement that v0.21.0 made untrue what
changed and where it stops. The v0.21.1 post-publication change moved the marketplace file on to the v0.21.1 tag and
added a dated 2026-10-23 follow-up after each marketplace update. The tagged lines of the notes and of the changelog
section are held by ``test_published_release_notes_stay_immutable.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from alicebot_api.vnext_label_repair import REPAIR_CAUSE, REPAIR_STATE_KEY
from alicebot_api.vnext_label_sql import READABLE_SCAN_CAP

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_PATH = "docs/release/v0.20.2-release-notes.md"
HEADING = "## v0.20.2 \u2014 2026-10-17"
NOTE_PATH = "docs/release/derived-labels-security-note-draft.md"
CHECKSUMS_PATH = "docs/release/v0.20.2-checksums.txt"
TAG_COMMIT = "8d5606153fa4900b0cbbc8ce60a65e342f7fe640"
LATEST_TAG_COMMIT = "8d0bac271164ad494687ab599a96499ddfbcce66"
WHEEL_SHA256 = "efc4da0e7512faa8e357a0eaec83bb6e5bdf5f7333a3809fd15a118f9e90c0c2"
SDIST_SHA256 = "31da21f7a84f1dde61f2d871cece6f33538a234cd8559a5036ada4519a62332a"

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


def test_the_notes_are_published_with_the_exact_title_state_and_a_checksums_record() -> None:
    """Mutations, each one alone: change the title, move the state comment, set either status back to pending,
    delete the checksums file.
    """

    lines = _text(NOTES_PATH).splitlines()
    assert lines[0] == "# Alice v0.20.2 Release Notes"
    assert lines[1] == (
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1","version":"0.20.2",'
        '"publication_status":"published","checksums_status":"recorded"} -->'
    )
    assert sum("alice-release-state" in line for line in lines) == 1
    assert (REPO_ROOT / CHECKSUMS_PATH).is_file()


def test_the_checksums_file_records_the_two_published_digests_in_the_format_of_the_last_release() -> None:
    """Mutations, each one alone: change one hex digit of either digest; swap the two file names; add a third
    line; write another version into the header or into the verify commands; delete a header line.

    The digests are the ones PyPI serves for 0.20.2 and the GitHub Release lists for its assets, read on
    2026-10-17 (wheel 1,891,233 bytes, source distribution 1,664,365 bytes). The header and the layout are
    the v0.20.0 file's with the version replaced, so the two files stay in one format. Nothing offline compares
    the digests with PyPI: ``release_check.py --tag v0.20.2 --verify-pypi-artifacts`` does, with the two files
    downloaded.
    """

    old = _text("docs/release/v0.20.0-checksums.txt").splitlines()
    new = _text(CHECKSUMS_PATH).splitlines()
    assert len(old) == len(new) == 19
    assert [line.replace("0.20.0", "0.20.2") for line in old[:17]] == new[:17]
    assert new[0] == "# Alice v0.20.2: published artifact SHA-256 digests"
    assert new[17:] == [
        f"{WHEEL_SHA256}  alice_memory-0.20.2-py3-none-any.whl",
        f"{SDIST_SHA256}  alice_memory-0.20.2.tar.gz",
    ]
    assert _text(CHECKSUMS_PATH).endswith(".tar.gz\n")


def test_the_marketplace_pins_the_published_release_and_the_notes_carry_its_install_lines() -> None:
    """Mutations, each one alone: set the marketplace ``ref`` back to v0.20.2; change one hex digit of the marketplace
    ``sha``; change the install or upgrade command in the notes.

    The v0.20.2 post-publication change moved the marketplace file to the v0.20.2 tag, the v0.21.0
    post-publication change moved it on to the v0.21.0 tag, and the v0.21.1 post-publication change moved it on to
    the v0.21.1 tag and the commit that tag points at, so it names the latest published release. The tree has moved
    on to the version sites of the next release, which ``test_v0211_release_notes.py`` pins, so this test no
    longer reads them.
    """

    marketplace = json.loads(_text(".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    assert marketplace["ref"] == "v0.21.1"
    assert marketplace["sha"] == LATEST_TAG_COMMIT
    notes = _flat(NOTES_PATH)
    assert "pip install alice-memory==0.20.2 && alice-memory install" in notes
    assert "pip install -U alice-memory==0.20.2" in notes
    assert "uv tool upgrade alice-memory" in notes
    assert "uvx alice-memory@latest --version" in notes
    assert "`uvx alice-memory --version` (or `alice-memory --version` for pip) must print 0.20.2" in notes
    # A pinned uvx check does not show what the hosts' plain `uvx alice-memory` runs.
    assert "it prints the version the hosts get" not in notes
    assert "pins `alice-memory==0.20.0` and carries none of the fixes in this release" in notes


def test_the_changelog_has_the_dated_heading_right_after_the_unreleased_section() -> None:
    """Mutations, each one alone: drop the heading; change its date form; put another section between
    v0.20.2 and v0.20.0; put an unreleased marker inside the v0.20.2 section.

    The Unreleased section was empty on the release commit. It may hold entries now that main has
    moved on, so this test no longer asks for it to be empty. It still asks that the v0.21.1, v0.21.0, v0.20.2 and
    v0.20.0 sections follow it in that order, one heading each.
    """

    changelog = _text("CHANGELOG.md")
    sections = changelog.split("\n## ")
    assert sections[1].startswith("Unreleased\n")
    assert sections[1].endswith("\n")
    assert sections[2].startswith("v0.21.1 \u2014 2026-10-23\n")
    assert sections[3].startswith("v0.21.0 \u2014 2026-10-22\n")
    assert sections[4].startswith("v0.20.2 \u2014 2026-10-17\n")
    assert sections[5].startswith("v0.20.0 \u2014 2026-10-02\n")
    assert changelog.count(HEADING) == 1
    assert "Unreleased (on main" not in sections[4]


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


PIN_UPDATE = (
    "**Update (2026-10-17):** v0.20.2 is published, and the marketplace file on `main` now pins the v0.20.2 tag "
    "commit, so the marketplace install runs v0.20.2. The copy inside the v0.20.2 tag still pins v0.20.0, so add "
    "the marketplace from the repository, not from a checkout of the tag."
)
PIN_FOLLOW_UP = (
    "**Update (2026-10-22):** v0.21.0 is published, and the marketplace file on `main` now pins the v0.21.0 tag "
    "commit, so the marketplace install runs v0.21.0. The copy inside the v0.20.2 tag still pins v0.20.0 and the "
    "copy inside the v0.21.0 tag still pins v0.20.2, so add the marketplace from the repository, not from a "
    "checkout of a tag."
)
PIN_FOLLOW_UP_V0211 = (
    "**Update (2026-10-23):** v0.21.1 is published, and the marketplace file on `main` now pins the v0.21.1 tag "
    "commit, so the marketplace install runs v0.21.1. The copy inside the v0.20.2 tag still pins v0.20.0, the copy "
    "inside the v0.21.0 tag still pins v0.20.2 and the copy inside the v0.21.1 tag still pins v0.21.0, so add the "
    "marketplace from the repository, not from a checkout of a tag."
)
_UPDATE_2026_10_22 = re.compile(r"^\*\*Update \(2026-10-22\):\*\* ")


def _changelog_section_of(version: str) -> str:
    """The body of the ``## vX.Y.Z`` section of the changelog, up to the next ``## `` heading."""

    changelog = _text("CHANGELOG.md")
    start = re.search(rf"^## {re.escape(version)} \u2014 .*$", changelog, flags=re.MULTILINE)
    assert start is not None, version
    rest = changelog[start.end() :]
    end = re.search(r"^## ", rest, flags=re.MULTILINE)
    return rest if end is None else rest[: end.start()]


def _blocks(path: str) -> list[str]:
    """Paragraphs of a file with quote markers and indentation removed, each joined onto one line."""

    blocks: list[list[str]] = [[]]
    for line in _text(path).splitlines():
        stripped = re.sub(r"^[\s>]+", "", line)
        if stripped:
            blocks[-1].append(stripped)
        else:
            blocks.append([])
    return [" ".join(block) for block in blocks if block]


def test_the_notes_say_main_now_pins_v0202_beside_each_marketplace_statement() -> None:
    """Mutations, each one alone: delete one of the three updates; name v0.20.0 as what ``main`` pins in one of
    them; move one away from its statement; reword the tagged marketplace sentence it follows.

    The three statements that the marketplace file pins v0.20.0 until a later change (upgrade step 12, the
    plugin section and the known limitation) stay as they were tagged. Each is followed by a dated update that
    says ``main`` pins the v0.20.2 tag commit and that the copy inside the tag does not, so a reader of the notes
    does not take the tagged sentence for current.
    """

    blocks = _blocks(NOTES_PATH)
    positions = [index for index, block in enumerate(blocks) if block == PIN_UPDATE]
    assert len(positions) == 3, positions
    assert sum("Update (2026-10-17)" in block for block in blocks) == 3
    step, plugin, limitation = (blocks[index - 1] for index in positions)
    assert step.endswith(
        '12. **Claude Code plugin:** the marketplace file pins the v0.20.0 tag commit until a change after this '
        'release moves it, so the marketplace install runs v0.20.0 code and carries none of these fixes. '
        'See "Claude Code plugin".'
    )
    assert plugin.startswith("- **The marketplace file pins v0.20.0.**")
    assert plugin.endswith(
        "installs plugin 0.20.0, which pins `alice-memory==0.20.0` and carries none of the fixes in this release."
    )
    assert limitation.endswith(
        "- **The marketplace install runs v0.20.0.** Until a change after this release moves the pin in "
        "`.claude-plugin/marketplace.json`, `claude plugin install alice-memory@alicememory` installs plugin 0.20.0, "
        "which pins `alice-memory==0.20.0` and carries none of the fixes in this release."
    )
    # Each update ends the statement's own list item or bullet. The 2026-10-22 follow-up comes next, then the
    # 2026-10-23 follow-up, and after them the block that followed as tagged.
    assert [blocks[index + 1] for index in positions] == [PIN_FOLLOW_UP] * 3
    assert [blocks[index + 2] for index in positions] == [PIN_FOLLOW_UP_V0211] * 3
    assert blocks[positions[0] + 3] == "## Derived rows follow their inputs"
    assert blocks[positions[1] + 3].startswith(
        "- Use the plugin or `alice-memory install --host claude-code`, not both, as in v0.20.0."
    )
    assert blocks[positions[2] + 3].startswith("- **The commit routes trust the role a client sends.**")


def test_the_notes_say_main_now_pins_v0210_after_each_2026_10_17_marketplace_update() -> None:
    """Mutations, each one alone: delete one of the three follow-ups; name v0.20.2 as what ``main`` pins in one of
    them; drop the sentence about the copy inside a tag; move one away from the 2026-10-17 update it follows; glue
    one to the end of another paragraph.

    Each of the three marketplace statements has a 2026-10-17 update that says ``main`` pins the v0.20.2 tag
    commit. ``main`` pins the v0.21.0 tag commit now, so a follow-up sits after each of them. The copy inside the
    v0.20.2 tag pins v0.20.0 and the copy inside the v0.21.0 tag pins v0.20.2.
    """

    blocks = _blocks(NOTES_PATH)
    positions = [index for index, block in enumerate(blocks) if block == PIN_FOLLOW_UP]
    assert len(positions) == 3, positions
    for index in positions:
        assert blocks[index - 1] == PIN_UPDATE, index
    assert "v0.21.0 tag commit" in PIN_FOLLOW_UP and "v0.20.0 tag commit" not in PIN_FOLLOW_UP


def test_the_notes_say_main_now_pins_v0211_after_each_2026_10_22_marketplace_update() -> None:
    """Mutations, each one alone: delete one of the three follow-ups; name v0.21.0 as what ``main`` pins in one of
    them; drop the sentence about the copy inside a tag; leave out the v0.21.1 tag from it; move one away from the
    2026-10-22 follow-up it follows; glue one to the end of another paragraph.

    Each of the three marketplace statements has a 2026-10-17 update and a 2026-10-22 follow-up that say ``main``
    pins the v0.20.2 and then the v0.21.0 tag commit. ``main`` pins the v0.21.1 tag commit now, so a third update
    sits after the second. The copy inside the v0.20.2 tag pins v0.20.0, the copy inside the v0.21.0 tag pins v0.20.2
    and the copy inside the v0.21.1 tag pins v0.21.0.
    """

    blocks = _blocks(NOTES_PATH)
    positions = [index for index, block in enumerate(blocks) if block == PIN_FOLLOW_UP_V0211]
    assert len(positions) == 3, positions
    for index in positions:
        assert blocks[index - 1] == PIN_FOLLOW_UP, index
    assert sum(block.startswith("**Update (2026-10-23):**") for block in blocks) == 3
    # An update glued to the end of another paragraph, with no blank line, is no paragraph of its own.
    assert _text(NOTES_PATH).count("Update (2026-10-23)") == 3
    assert "v0.21.1 tag commit" in PIN_FOLLOW_UP_V0211 and "v0.21.0 tag commit" not in PIN_FOLLOW_UP_V0211
    assert "the copy inside the v0.21.1 tag still pins v0.21.0" in PIN_FOLLOW_UP_V0211


# Each v0.20.2 statement that v0.21.0 made untrue or changed, other than the marketplace pin, with text its
# 2026-10-22 update must carry. The update is the next paragraph, except for the memory audit item, which has the
# 2026-10-21 correction between the item and the update.
V0202_ITEMS_CHANGED_IN_V0210: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "- **Rows a key can read may list the ids of rows it cannot read.**",
        (
            "Fixed in v0.21.0 for a key with limits.",
            "an HTTP answer, a tool result or command output",
            "in any spelling of a UUID",
            "`memory:`, `source:`, `artifact:` or `open_loop:` prefix",
            "recomputed or dropped",
            "whether the row is hidden or does not exist",
            "The owner and an unbound admin key keep every id.",
            "An id inside free text, such as the text of a readable memory, is not removed",
            "shown as written",
            "does not claim that no hidden id is left in an answer",
        ),
    ),
    (
        "- **The memory audit route lists the archived members of a derived memory,**",
        (
            "Changed in v0.21.0 for the ids this item lists.",
            "`source_id` and `source_event_ids`",
            "`artifact_id` of the scheduler's run records",
            "when it may not read the row they name",
            "keep every id",
            "`capture_content_hash` of a memory is still returned after its source is archived",
            "as the correction above records",
        ),
    ),
    (
        "- **The vector stage of recall does not refill after hiding rows.**",
        (
            "Fixed in v0.21.0.",
            "temporal-anchor stage of the context pack",
            "read once, at 128 rows (`READABLE_SCAN_CAP`)",
            "118 hidden rows for `alice_recall` at limit 5",
            "nine candidates at 119, one at 127 and none from 128",
            "candidate counts of the two stages no longer move with hidden rows",
            "BM25 score of a visible row is still computed over the whole index, hidden rows included",
            "The owner and an unbound admin key are read as before.",
        ),
    ),
    (
        "- **The cited-source fence does not cover everything.**",
        (
            "Changed in v0.21.0 for the first clause only.",
            "`--source-ref` flag of `alicebot vnext memories commit`",
            "an object, a number, a boolean, null, a nested array, a blank string",
            "text of a JSON object or array, and write nothing",
            "still stored without checking the id it holds",
            "label that starts with `[` or `{` and is not JSON",
            "The other gaps in this item remain, and the known limitations page lists them.",
            "still restores",
        ),
    ),
    (
        "- **A key with limits gets short list pages.**",
        (
            "Fixed in v0.21.0.",
            "`GET /v0/vnext/projects`",
            "read once, at 128 rows",
            "128 minus the rows the list is asked for",
            "short past that bound",
            "The owner and an unbound admin key are read as before.",
        ),
    ),
    (
        "- **Configured folder paths in the connector status.**",
        (
            "v0.21.0 is published and does not contain this fix.",
            "`trusted_local_agent` key still reads the configured local-folder paths",
            "`config.config_json.paths`",
        ),
    ),
    (
        "- **A killed restore can leave a working copy.**",
        (
            "Fixed in v0.21.0.",
            "next `alice-memory import` into that vault",
            "next open of an existing vault, such as `doctor` or the MCP server",
            "`-wal` and `-shm` files",
            "an open leaves its copy alone and does not wait for it",
            "`vault_not_found`",
            "`doctor` creates the vault and removes it",
        ),
    ),
)

# v0.20.2 limitations that v0.21.0 leaves as they were. The known limitations page of v0.21.0 still lists each of
# them, so none gets an update.
V0202_ITEMS_UNCHANGED_IN_V0210: tuple[tuple[str, str], ...] = (
    ("- **Some events still name rows without a label.**", "a connector keeps one cursor"),
    ("- **A caller with limits that reaches 128 rows of a list**", "`READABLE_SCAN_CAP`"),
    ("- **The context pack still names the id of an expired memory**", "still names the id of an expired memory"),
    (
        "- **The `max_tokens` budget prices the full stored row, and the agent receives a compact row.**",
        "`max_tokens` budget prices the full stored row",
    ),
)


def test_the_notes_say_beside_each_item_that_v0210_fixed_or_changed_it() -> None:
    """Mutations, each one alone: delete one of the seven updates; change its version; move it away from its item;
    drop the limit of the fix from it; put an update beside a limitation that v0.21.0 leaves as it was (the events
    that name rows, the 128-row bound, the id of an expired memory in the context pack, the ``max_tokens`` budget);
    glue an update to the end of another paragraph.

    The v0.20.2 notes are published, so each statement that something is open or planned for v0.21.0 stays as it was
    tagged and a dated update follows it. Each update names v0.21.0 and where the fix stops. The memory audit item
    keeps its 2026-10-21 correction directly after it, and its update follows the correction. The statement about
    the folder paths in the connector status said a fix was planned for v0.21.0, and v0.21.0 does not contain it.
    """

    blocks = _blocks(NOTES_PATH)
    for opening, needles in V0202_ITEMS_CHANGED_IN_V0210:
        # Bullets with no blank line between them are one block, so the item must be the last bullet of the block it
        # opens or sits in, and the update is the next block (the correction is the next block for the audit item).
        positions = [index for index, block in enumerate(blocks) if opening in block]
        assert len(positions) == 1, (opening, positions)
        block = blocks[positions[0]]
        assert " - **" not in block[block.index(opening) + len(opening) :], opening
        following = positions[0] + 1
        if opening.startswith("- **The memory audit route"):
            assert blocks[following].startswith("**Correction (2026-10-21):** Withholding `capture_content_hash`"), opening
            following += 1
        update = blocks[following]
        assert _UPDATE_2026_10_22.match(update), (opening, update[:80])
        for needle in needles:
            assert needle in update, (opening, needle)
        assert "v0.21.0" in update
        assert "v0.21.1" not in update and "v0.22.0" not in update
    updates = [block for block in _blocks(NOTES_PATH) if _UPDATE_2026_10_22.match(block)]
    assert len(updates) == len(V0202_ITEMS_CHANGED_IN_V0210) + 3, len(updates)
    # An update glued to the end of another paragraph, with no blank line, is no paragraph of its own.
    assert _text(NOTES_PATH).count("Update (2026-10-22)") == len(updates)

    limits_page = _flat("docs/alpha/known-limitations.md")
    for opening, still_listed in V0202_ITEMS_UNCHANGED_IN_V0210:
        positions = [index for index, block in enumerate(blocks) if opening in block]
        assert len(positions) == 1, (opening, positions)
        block = blocks[positions[0]]
        if " - **" not in block[block.index(opening) + len(opening) :]:
            assert not _UPDATE_2026_10_22.match(blocks[positions[0] + 1]), opening
        assert still_listed in limits_page, still_listed


def test_the_2026_10_22_updates_carry_the_limits_the_v0210_changelog_and_notes_state() -> None:
    """Mutations, each one alone: change the 118, the 119, 127 or 128 of the refill update; change the 20,000 or the
    128 in the changelog; drop the BM25 sentence, the ``vault_not_found`` sentence or the free-text sentence from the
    changelog or the notes.

    The updates restate numbers and exceptions that the v0.21.0 changelog section and the v0.21.0 notes hold, so the
    two cannot drift apart: each fact an update states is also in the changelog or the notes.
    """

    changelog = " ".join(_changelog_section_of("v0.21.0").split())
    notes = _flat("docs/release/v0.21.0-release-notes.md")
    facts_in_changelog = (
        "so at limit 5 that is 118 hidden rows: from 119 the stage returns fewer candidates (nine at 119, one at 127, none from 128)",
        "On SQLite the BM25 score of a visible row is still computed over the whole index, hidden rows included.",
        "`READABLE_SCAN_CAP`",
        "The owner and an unbound admin key are read as before",
        "An id inside free text is not looked for",
        "At a position that a field name or a prefix types as one of these rows, an id that names no row the key may read is withheld whether the row is hidden or missing",
        "a label that starts with `[` or `{` and is not JSON",
        "A killed `alice-memory import` no longer leaves its working copy in the data directory.",
        "`labels check` and the `sources` commands stop with `vault_not_found` before they open anything and leave the copy",
        "A command that creates the empty vault, such as `doctor`, removes it.",
        "the next open of an existing vault",
        "a backup from that release restores",
    )
    for fact in facts_in_changelog:
        assert fact in changelog, fact
    assert "an id that names no row at a position the product does not type is shown as written" in _flat(
        "docs/alpha/known-limitations.md"
    )
    facts_in_notes = (
        "An id inside free text, such as the text of a readable memory, is not removed.",
        "It does not claim that no hidden id is left in an answer.",
        "While an import runs in the data directory, an open leaves its copy alone and does not wait for it.",
        "Backups made by v0.20.0 and v0.20.2 that hold an object entry still restore.",
        "The owner and an unbound admin key keep every id.",
        "The page is still short when the hidden rows in front of it number more than 128 minus the rows the stage asks for.",
    )
    for fact in facts_in_notes:
        assert fact in notes, fact
