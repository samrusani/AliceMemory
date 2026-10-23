"""The v0.21.0 release notes carry the upgrade steps, the security wording and the claims a reader can check.

Every claim in the notes comes from an entry of the changelog. These tests pin the parts a later edit could quietly
weaken: the release identity and its pending state, the version sites, the dated changelog heading, the security
wording, the steps an upgrading user must act on, the three limits the notes name, the numbers that drift with the code
(the tool counts, the PostgreSQL revision, the sizes of the README and of the known limitations page) and the one
change to the publish workflow. Each test names the mutation that must fail it.

The notes are published. The change that followed publication moved their state to ``published`` and ``recorded``, added
the checksums file, moved the marketplace file to the v0.21.0 tag and its commit, named v0.21.0 as the latest published
release in the control documents and removed the release candidate line, as the change after v0.20.2 did. The tagged
lines of the notes and of the changelog section are held by ``test_published_release_notes_stay_immutable.py``. The
release check's pre-tag mode refuses the tree now, and one test below pins the three reasons.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

import scripts.release_check as release_check
from tests.unit.test_published_release_notes_stay_immutable import _tagged

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_PATH = "docs/release/v0.21.0-release-notes.md"
RELEASE_TAG = "v0.21.0"
HEADING = "## v0.21.0 \u2014 2026-10-22"
NOTES_CEILING = 15_000

CHECKSUMS_PATH = "docs/release/v0.21.0-checksums.txt"
TAG_COMMIT = "c4757e626405a4bfe77a0039244fb62b483650f7"
WHEEL_SHA256 = "0f3377871981e98a1a64a85e82ad1bcef26fc23c322e3a48820d1a1834fe5bbf"
SDIST_SHA256 = "49232235fc85f677b1f78551268d0574b5a45e30a2974a81ddcaa63d7721aecc"

CANDIDATE_LINE = "`v0.21.0` is the current release candidate. It is not published."
LATEST_DOCS = (
    "ARCHITECTURE.md",
    "PRODUCT_BRIEF.md",
    "ROADMAP.md",
    "RELEASING.md",
    "docs/vnext/README.md",
)

SECTIONS = [
    "What to do after upgrading",
    "Passage ranking is the default of `alice_recall` on SQLite",
    "Rows a key with limits may not read",
    "`source_refs` shapes are refused at write time",
    "What changed",
    "Restore working copies",
    "Behaviour changes an integrator will notice",
    "Docs and tooling",
    "What this release does not claim",
    "Security",
    "Install",
]


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
    return changelog.split(HEADING)[1].split("\n## v0.20.2")[0]


def _entries() -> list[str]:
    return [" ".join(item.split()) for item in _changelog_section().split("\n- ")[1:]]


def test_the_notes_are_published_with_the_exact_title_state_and_a_checksums_record() -> None:
    """Mutations, each one alone: change the title, move the state comment, set either status back to pending,
    delete the checksums file.
    """

    lines = _text(NOTES_PATH).splitlines()
    assert lines[0] == "# Alice v0.21.0 Release Notes"
    assert lines[1] == (
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1","version":"0.21.0",'
        '"publication_status":"published","checksums_status":"recorded"} -->'
    )
    assert sum("alice-release-state" in line for line in lines) == 1
    assert (REPO_ROOT / CHECKSUMS_PATH).is_file()


def test_the_checksums_file_records_the_two_published_digests_in_the_format_of_the_last_release() -> None:
    """Mutations, each one alone: change one hex digit of either digest; swap the two file names; add a third
    line; write another version into the header or into the verify commands; delete a header line.

    The digests are the ones PyPI serves for 0.21.0 and the GitHub Release lists for its assets, read on
    2026-10-22 (wheel 1,926,152 bytes, source distribution 1,690,284 bytes), and they agree with a local hash of the
    two files downloaded from PyPI that day. The header and the layout are the v0.20.2 file's with the version
    replaced, so the two files stay in one format. Nothing offline compares the digests with PyPI:
    ``release_check.py --tag v0.21.0 --verify-pypi-artifacts`` does, with the two files downloaded.
    """

    old = _text("docs/release/v0.20.2-checksums.txt").splitlines()
    new = _text(CHECKSUMS_PATH).splitlines()
    assert len(old) == len(new) == 19
    assert [line.replace("0.20.2", "0.21.0") for line in old[:17]] == new[:17]
    assert new[0] == "# Alice v0.21.0: published artifact SHA-256 digests"
    assert new[17:] == [
        f"{WHEEL_SHA256}  alice_memory-0.21.0-py3-none-any.whl",
        f"{SDIST_SHA256}  alice_memory-0.21.0.tar.gz",
    ]
    assert _text(CHECKSUMS_PATH).endswith(".tar.gz\n")


def test_the_notes_stay_under_the_size_ceiling_and_keep_their_sections_in_order() -> None:
    """Mutations, each one alone: paste 3,000 characters into the notes; delete or reorder a section."""

    text = _text(NOTES_PATH)
    assert len(text) < NOTES_CEILING
    headings = re.findall(r"(?m)^## (.+?)\s*$", text)
    assert headings == SECTIONS


def test_the_marketplace_pins_the_published_release_and_the_notes_carry_its_install_lines() -> None:
    """Mutations, each one alone: set the marketplace ``ref`` back to v0.20.2; change one hex digit of the marketplace
    ``sha``; change the install command in the notes.

    The post-publication change moved the marketplace file to the v0.21.0 tag and the commit that tag points at, so it
    names the latest published release. The v0.21.0 tag itself still pins v0.20.2, and the plugin page says so. The
    tree has moved on to the version sites of the next release, which ``test_v0211_release_notes.py`` pins, so this test
    no longer reads them.
    """

    marketplace = json.loads(_text(".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    assert marketplace["ref"] == "v0.21.0"
    assert marketplace["sha"] == TAG_COMMIT
    notes = _flat(NOTES_PATH)
    assert "pip install alice-memory==0.21.0 && alice-memory install" in notes
    assert "With uv: `uvx alice-memory install`." in notes


def test_the_control_documents_name_v0210_as_the_latest_published_release_and_v0202_as_the_prior_one() -> None:
    """Mutations, each one alone: put the candidate line back into one control document; name v0.20.2 as the latest
    published release in one of them; name v0.20.0 as the immediately prior release; set a checksums or release notes
    pointer back to v0.20.2; put ``Targets`` back in the heading of the current state; point the release notes link
    of the README or the vNext README at another release.

    The candidate line of a later release (v0.21.1) names that release and is pinned by ``test_v0211_release_notes.py``,
    so only the v0.21.0 candidate line is refused here.
    """

    for name in LATEST_DOCS:
        text = _text(name)
        assert CANDIDATE_LINE not in text, name
        assert "`v0.21.0` is the latest published release" in text, name
        assert "`v0.20.2` is the latest published release" not in text, name
    readme = _flat("README.md")
    assert (
        "`v0.21.0` is the latest published release and remains the install, checksum, and release-note baseline. "
        "`v0.20.2` is the immediately prior published release."
    ) in readme
    assert CANDIDATE_LINE not in readme
    assert "- [v0.21.0 release notes](docs/release/v0.21.0-release-notes.md)" in _text("README.md")
    assert "[latest published release notes](../release/v0.21.0-release-notes.md)" in _flat("docs/vnext/README.md")
    architecture = _flat("ARCHITECTURE.md")
    assert "`docs/release/v0.21.0-checksums.txt`. - **Prior published:** `v0.20.2` is the immediately prior published release" in architecture
    assert "artifact digests are in `docs/release/v0.20.2-checksums.txt`." in architecture
    brief = _flat("PRODUCT_BRIEF.md")
    assert "`v0.20.2` is the immediately prior published release" in brief
    assert "exact digests in `docs/release/v0.21.0-checksums.txt`." in brief
    roadmap = _flat("ROADMAP.md")
    assert (
        "`docs/release/v0.21.0-release-notes.md`, with artifact digests in `docs/release/v0.21.0-checksums.txt`. "
        "`v0.20.2` is the immediately prior published release."
    ) in roadmap
    state = _text("CURRENT_STATE.md")
    assert "What `v0.21.0` Targets" not in state
    assert (
        "\n## What `v0.21.0` Shipped\n\n`v0.21.0` is the latest published release and remains the install, checksum,\n"
        "and baseline reference.\n\nIt shipped the fixes and additions on `main` after `v0.20.2`."
    ) in state
    assert "\n## What `v0.20.2` Shipped\n\n`v0.20.2` is the immediately prior published release.\n" in state
    assert "\n## What `v0.20.0` Shipped\n\n`v0.20.0` is an earlier published release.\n" in state
    assert "- `v0.21.0` is the latest published release." in state
    assert "`v0.20.2` is the immediately prior\n  published release." in state
    assert "`docs/release/v0.21.0-release-notes.md`\n- `docs/release/v0.21.0-checksums.txt`\n" in state
    assert "`v0.20.2` is the immediately prior published release; its records are" in state


def test_the_install_and_checkout_lines_name_v0210_as_the_latest_release() -> None:
    """Mutations, each one alone: set the ``--tag`` of the headless install guide, the latest release tag it names, the
    checkout line of the quickstart or the baseline named by the reference paths page back to v0.20.2.

    The README's checkout line is pinned in ``test_readme_facts_live_in_the_docs.py``. The lines pinned here name the
    release a reader installs or checks out, so they move with each release. A comparison with v0.20.2 elsewhere in the
    docs is history and stays.
    """

    guide = _flat("docs/alpha/headless-ubuntu-install.md")
    assert "use the latest published release tag (`v0.21.0`) for an immutable deployment." in guide
    assert "with the latest published release tag (`v0.21.0`): `--tag v0.21.0`." in guide
    assert "v0.20.2" not in guide
    quickstart = _flat("docs/alpha/quickstart.md")
    assert "To run v0.21.0, run `git checkout v0.21.0` before `make setup`." in quickstart
    assert "git checkout v0.20.2" not in quickstart
    assert "on top of the latest published `v0.21.0` baseline." in _flat("docs/integrations/reference-paths.md")


def test_the_changelog_has_the_dated_heading_right_after_the_unreleased_section() -> None:
    """Mutations, each one alone: drop the heading; change its date form; put another section between v0.21.0 and
    v0.20.2; put the marker for the unreleased state back into the v0.21.0 section.

    The Unreleased section is empty on the release commit. It may hold entries once main has moved on, so this test does
    not ask for it to be empty. It asks that the v0.21.1, v0.21.0 and v0.20.2 sections follow it in that order, one
    heading each.
    """

    changelog = _text("CHANGELOG.md")
    sections = changelog.split("\n## ")
    assert sections[1].startswith("Unreleased\n")
    assert sections[2].startswith("v0.21.1 \u2014 2026-10-23\n")
    assert sections[3].startswith("v0.21.0 \u2014 2026-10-22\n")
    assert sections[4].startswith("v0.20.2 \u2014 2026-10-17\n")
    assert changelog.count(HEADING) == 1
    assert "Unreleased (on" not in sections[3]
    assert "Unreleased (on" not in sections[4]


def test_no_marker_for_the_unreleased_state_is_left_in_the_documents_the_code_or_the_manifest() -> None:
    """Mutation: put the marker for the unreleased state of v0.20.2 back into any document, docstring, test or case of the
    mutation manifest.

    The pattern is written as a regular expression so that this file does not hold the marker itself. The published
    v0.18.0 notes carry two dated corrections that say ``not in v0.19.0``; they are another marker and immutable.
    """

    pattern = re.compile(r"Unreleased\s*\(\s*on\s+main\s*,\s*not\s+in\s+v0\.20\.2")
    paths = [
        REPO_ROOT / name
        for name in ("README.md", "SECURITY.md", "CURRENT_STATE.md", "CHANGELOG.md", "ROADMAP.md", "RELEASING.md")
    ]
    for root in ("docs", "plugins", "agent-skills", "eval"):
        paths.extend(path for path in (REPO_ROOT / root).rglob("*.md"))
    for root in ("apps/api/src", "tests", "scripts"):
        paths.extend(path for path in (REPO_ROOT / root).rglob("*.py"))
    paths.append(REPO_ROOT / "scripts/derived_label_mutations.json")
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
    assert flat.count(disclosure) == 1
    assert flat.count("audited") == flat.count(disclosure)
    review = "automated security scanning and internal adversarial review, findings triaged and fixed"
    assert flat.count(review) == 1
    lowered = flat.lower()
    for forbidden in ("independently", "third-party", "third party", "penetration", "pentest", "external review"):
        assert forbidden not in lowered, forbidden
    assert "ghsa-" not in lowered and "cve-" not in lowered
    assert "Security" in SECTIONS


def test_the_upgrade_steps_name_every_action_an_existing_user_takes_in_order() -> None:
    """Mutations, each one alone: delete one numbered step; swap two; drop the command, the revision or the setting a
    step names.
    """

    steps = _section(_text(NOTES_PATH), "What to do after upgrading")
    numbers = [int(number) for number in re.findall(r"(?:^| )(\d{1,2})\. \*\*", steps)]
    assert numbers == list(range(1, 8))
    for needle in (
        "`uv tool upgrade alice-memory`",
        "`pip install -U alice-memory`",
        "`alice-memory --version`",
        "**No migration.**",
        "A vault that v0.20.2 wrote opens in v0.21.0, and a vault that v0.21.0 has opened opens in v0.20.2.",
        "PostgreSQL stays at revision `20261011_0097`.",
        "set `ALICE_SEARCH_QUALITY=off` in the environment of the MCP server",
        "Any value other than `off` or `passage`, an empty value and `on` included, makes `alice_recall` answer `tool_request_failed`.",
        "**Clients that write `source_refs`** must send strings",
        "now get shorter lists and null fields where an id is withheld",
        "`alice-memory doctor` opens the vault, so it removes a leftover working copy now.",
    ):
        assert needle in steps, needle
    assert steps.index("**No migration.**") < steps.index("ALICE_SEARCH_QUALITY=off") < steps.index("**Killed imports.**")


def test_the_numbers_in_the_notes_are_the_ones_the_release_tag_has() -> None:
    """The PostgreSQL revision, the sizes of the README and of the known limitations page, and the Vite version are
    read from the files at the ``v0.21.0`` tag, because the notes are published and describe that tag; a later change
    to main does not make them wrong. The tool counts (12, 74, 77) were checked against the code by the release pull
    request, and ``test_published_release_notes_stay_immutable.py`` keeps the published text from changing, so here
    they are pinned as text only.

    Mutations, each one alone: change a number in the notes (the immutability test fails too); read the page sizes
    from the working tree instead of the tag after adding a paragraph to the page (this test fails).
    """

    notes = _flat(NOTES_PATH)
    assert "`ALICE_MCP_FULL_TOOLS=1` lists 12 core tools, and with `ALICE_MCP_LEGACY_TOOLS=1` 74, or 77 with `ALICE_LEGACY_SURFACES=1`." in notes
    assert "The default surface is still three tools." in notes
    assert "now holds 12 core tools" in notes

    listing = _tagged_listing(RELEASE_TAG, "apps/api/alembic/versions")
    head = sorted(name for name in listing if name.startswith("2") and name.endswith(".py"))[-1]
    assert head.startswith("20261011_0097_"), head
    assert "PostgreSQL stays at revision `20261011_0097`." in notes
    assert "There is no migration." in notes

    assert json.loads(_tagged(RELEASE_TAG, "apps/web/package.json"))["pnpm"]["overrides"]["vite"] == "6.4.4"
    assert "moves from 6.4.3 to 6.4.4" in notes

    docs_section = _section(_text(NOTES_PATH), "Docs and tooling")
    readme_size = int(re.search(r"The README is about ([\d,]+) characters", docs_section).group(1).replace(",", ""))  # type: ignore[union-attr]
    limits_size = int(  # type: ignore[union-attr]
        re.search(r"known limitations page is about ([\d,]+) characters", docs_section).group(1).replace(",", "")  # type: ignore[union-attr]
    )
    tagged_readme = len(_tagged(RELEASE_TAG, "README.md"))
    tagged_limits = len(_tagged(RELEASE_TAG, "docs/alpha/known-limitations.md"))
    assert abs(tagged_readme - readme_size) <= 150, (tagged_readme, readme_size)
    assert abs(tagged_limits - limits_size) <= 150, (tagged_limits, limits_size)
    assert "down from about 28,600" in docs_section and "down from about 18,700" in docs_section


def _tagged_listing(tag: str, folder: str) -> list[str]:
    """The file names in ``folder`` at ``tag``, with the shallow-clone rule of ``_tagged``."""

    completed = subprocess.run(
        ["git", "ls-tree", "--name-only", f"{tag}:{folder}"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        message = f"git ls-tree {tag}:{folder} failed: {completed.stderr.strip()}"
        if os.environ.get("CI"):
            pytest.fail(message + ". The checkout must include tags (fetch-depth: 0).")
        pytest.skip(message)
    return completed.stdout.split()


def test_the_notes_name_the_three_limits_they_say_matter_most() -> None:
    """Mutations, each one alone: delete one of the three bullets; say passage mode is the default on PostgreSQL."""

    section = _section(_text(NOTES_PATH), "What this release does not claim")
    for needle in (
        "- PostgreSQL keeps document mode. The passage default is a SQLite change.",
        "- An id inside free text, such as the text of a readable memory, is not removed.",
        "- Redaction restricts reports and does not remove the words from them.",
    ):
        assert needle in section, needle
    assert "It does not claim that no hidden id is left in an answer." in section
    passage = _section(_text(NOTES_PATH), "Passage ranking is the default of `alice_recall` on SQLite")
    assert "PostgreSQL keeps document mode." in passage
    assert "a key with limits stays on one passage per document even then" in passage


def test_the_notes_state_the_publish_limit_and_the_workflows_say_the_same() -> None:
    """The note under "Docs and tooling" gives the new limit and the measured time, the changelog has an entry for it,
    ``RELEASING.md`` states it, and both workflow files hold 180 minutes.

    Mutations, each one alone: set ``timeout-minutes`` of ``build-and-verify`` or of the rehearsal back to 120; set
    ``JOB_TIMEOUT_MINUTES`` of the rehearsal to another number; delete the note from the notes, the entry from the
    changelog or the sentence from ``RELEASING.md``.
    """

    note = _section(_text(NOTES_PATH), "Docs and tooling")
    assert (
        "The first job of the publish workflow, `build-and-verify`, has a time limit of 180 minutes, up from 120. "
        "It reruns the whole unit suite in one process, and that step now takes about 65 minutes (3,876 s on a runner), "
        "while a release step must take less than half of its job's limit. "
        "`publish-recheck-rehearsal.yml` has the same limit."
    ) in note
    publish = yaml.safe_load(_text(".github/workflows/publish-pypi.yml"))["jobs"]["build-and-verify"]
    rehearsal_jobs = yaml.safe_load(_text(".github/workflows/publish-recheck-rehearsal.yml"))["jobs"]
    assert publish["timeout-minutes"] == 180
    assert [job["timeout-minutes"] for job in rehearsal_jobs.values()] == [180]
    report = [step for job in rehearsal_jobs.values() for step in job["steps"] if "JOB_TIMEOUT_MINUTES" in (step.get("env") or {})]
    assert [str(step["env"]["JOB_TIMEOUT_MINUTES"]) for step in report] == ["180"]
    # 3,876 s is under half of 180 minutes (5,400 s) and over half of 120 minutes (3,600 s).
    assert 3_600 < 3_876 < 5_400
    limit_entries = [entry for entry in _entries() if entry.startswith("Internal change, no behaviour change: the first job of `publish-pypi.yml`")]
    assert len(limit_entries) == 1
    assert "now has a `timeout-minutes` of 180, up from 120" in limit_entries[0]
    assert "that step took 3,876 s on a runner, about 65 minutes" in limit_entries[0]
    assert limit_entries[0].endswith("No migration is required.")
    assert "The limit is 180 minutes, because the one-process rerun took 3,876 s on a runner" in _flat("RELEASING.md")


def test_the_headline_changes_of_the_notes_each_have_a_changelog_entry() -> None:
    """Mutations, each one alone: delete the changelog entry of a change the notes name, or reword its first words."""

    starts = (
        "On the SQLite store, `alice_recall` runs passage mode by default",
        "A key with limits is not shown the id of a memory, a source, an artifact or an open loop it may not read.",
        "The vector stage of `alice_recall` and of the context pack, and the temporal-anchor stage of the context pack, refill",
        "A write of `source_refs` accepts the strings the product's producers store and refuses anything else.",
        "`alice_what_changed`, `POST /v0/vnext/changes` and the continuity page answer one topic",
        "A killed `alice-memory import` no longer leaves its working copy in the data directory.",
        "The web console's development build tool Vite moves from 6.4.3 to 6.4.4",
        "Internal change, no behaviour change: the first job of `publish-pypi.yml`",
    )
    entries = _entries()
    for start in starts:
        assert len([entry for entry in entries if entry.startswith(start)]) == 1, start
    notes = _flat(NOTES_PATH)
    assert "`ALICE_SEARCH_QUALITY=off` restores the old ranking." in notes
    assert "v0.21.0 withholds them." in notes


def test_the_release_check_accepts_the_tree_and_its_pre_tag_mode_refuses_the_published_notes() -> None:
    """Mutations, each one alone: set a status of the notes back to pending; delete the checksums file; delete the
    dated heading.

    ``release_check.py`` passes on the tree. Its ``--require-finalized-release-docs`` mode is the pre-tag rule and
    refuses a published tree on purpose, for the same three reasons as after v0.20.2: both statuses must be pending
    before the tag, and the checksums file must not exist on the pre-publication tag. The package version of the tree is
    the next release's (``test_v0211_release_notes.py`` pins it), so the rule is run for the version of these notes.
    """

    metadata, issues = release_check.validate_metadata(REPO_ROOT)
    assert issues == []
    version = "0.21.0"
    assert metadata.version != version
    assert release_check.validate_release_document_state(REPO_ROOT, version=version, require_finalized=False) == []
    published = [
        "release notes alice-release-state publication_status must be pending before tag",
        "release notes alice-release-state checksums_status must be pending before tag",
        "docs/release/v0.21.0-checksums.txt must not exist on the pre-publication tag",
    ]
    issues = release_check.validate_release_document_state(REPO_ROOT, version=version, require_finalized=True)
    assert [issue for issue in issues if issue in published] == published
    # Once main has an Unreleased CHANGELOG entry, the pre-tag mode also says the section must be empty for a tag.
    # That is true of every later commit and is the only other issue allowed here.
    assert [issue for issue in issues if issue not in published] in (
        [],
        ["CHANGELOG.md Unreleased section must be empty for the release tag"],
    )
