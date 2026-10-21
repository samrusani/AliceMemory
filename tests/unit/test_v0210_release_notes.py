"""The v0.21.0 release notes carry the upgrade steps, the security wording and the claims a reader can check.

Every claim in the notes comes from an entry of the changelog. These tests pin the parts a later edit could quietly
weaken: the release identity and its pending state, the version sites, the dated changelog heading, the security
wording, the steps an upgrading user must act on, the three limits the notes name, the numbers that drift with the code
(the tool counts, the PostgreSQL revision, the sizes of the README and of the known limitations page) and the one
change to the publish workflow. Each test names the mutation that must fail it.

The notes are not published yet. Their state is ``pending`` and there is no checksums file, because the release check
requires that before the tag. The change that follows publication moves the state to ``published`` and ``recorded``,
adds the checksums file and moves the marketplace pin to the tag, and it changes the first test and the version-site
test below to say so, as the change after v0.20.2 did.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

import scripts.release_check as release_check
from alicebot_api.mcp.registry import list_mcp_tools

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_PATH = "docs/release/v0.21.0-release-notes.md"
HEADING = "## v0.21.0 \u2014 2026-10-22"
NOTES_CEILING = 15_000

CANDIDATE_LINE = "`v0.21.0` is the current release candidate. It is not published."
CANDIDATE_DOCS = (
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


def test_the_notes_have_the_exact_title_and_the_pending_state_and_no_checksums_file() -> None:
    """Mutations, each one alone: change the title, move the state comment, set either status to published or
    recorded, add a second state line, add ``docs/release/v0.21.0-checksums.txt``.

    The release check requires both statuses to be pending and the checksums file to be absent before the tag.
    """

    lines = _text(NOTES_PATH).splitlines()
    assert lines[0] == "# Alice v0.21.0 Release Notes"
    assert lines[1] == (
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1","version":"0.21.0",'
        '"publication_status":"pending","checksums_status":"pending"} -->'
    )
    assert sum("alice-release-state" in line for line in lines) == 1
    assert not (REPO_ROOT / "docs/release/v0.21.0-checksums.txt").exists()


def test_the_notes_stay_under_the_size_ceiling_and_keep_their_sections_in_order() -> None:
    """Mutations, each one alone: paste 3,000 characters into the notes; delete or reorder a section."""

    text = _text(NOTES_PATH)
    assert len(text) < NOTES_CEILING
    headings = re.findall(r"(?m)^## (.+?)\s*$", text)
    assert headings == SECTIONS


def test_every_version_site_names_0210_and_the_marketplace_still_pins_the_published_release() -> None:
    """Mutations, each one alone: set one version site or one plugin pin back to 0.20.2; move the marketplace ``ref``
    to v0.21.0; change the install command in the notes.

    The marketplace file moves to the v0.21.0 tag only after publication, in the change that records the digests, so
    here it still names the latest published release.
    """

    pyproject = tomllib.loads(_text("pyproject.toml"))["project"]["version"]
    assert pyproject == "0.21.0"
    assert json.loads(_text("apps/web/package.json"))["version"] == pyproject
    assert json.loads(_text("packaging/mcpb/manifest.json"))["version"] == pyproject
    assert json.loads(_text("plugins/alice-memory/.claude-plugin/plugin.json"))["version"] == pyproject
    for name in ("plugins/alice-memory/.mcp.json", "plugins/alice-memory/hooks/hooks.json"):
        assert f"alice-memory=={pyproject}" in _text(name), name
        assert "alice-memory==0.20.2" not in _text(name), name
    marketplace = json.loads(_text(".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    assert marketplace["ref"] == "v0.20.2"
    notes = _flat(NOTES_PATH)
    assert "pip install alice-memory==0.21.0 && alice-memory install" in notes
    assert "With uv: `uvx alice-memory install`." in notes


def test_the_control_documents_name_the_candidate_and_keep_v0202_as_the_latest_published_release() -> None:
    """Mutations, each one alone: delete the candidate line from one control document; name v0.21.0 as the latest
    published release; delete the ``What `v0.21.0` Targets`` section of the current state.

    The statements about the latest published release, the baseline and the checksums change after publication.
    """

    for name in CANDIDATE_DOCS:
        text = _text(name)
        assert CANDIDATE_LINE in text, name
        assert "`v0.20.2` is the latest published release" in text, name
    readme = _flat("README.md")
    assert "`v0.20.2` is the latest published release and remains the install, checksum, and release-note baseline." in readme
    assert CANDIDATE_LINE in readme
    state = _text("CURRENT_STATE.md")
    assert "\n## What `v0.21.0` Targets\n\n" + CANDIDATE_LINE + "\n" in state
    assert "\n## What `v0.20.2` Shipped\n\n`v0.20.2` is the latest published release" in state
    assert "- `v0.20.2` is the latest published release." in state


def test_the_changelog_has_the_dated_heading_right_after_the_unreleased_section() -> None:
    """Mutations, each one alone: drop the heading; change its date form; put another section between v0.21.0 and
    v0.20.2; put the marker for the unreleased state back into the v0.21.0 section.

    The Unreleased section is empty on the release commit. It may hold entries once main has moved on, so this test does
    not ask for it to be empty. It asks that the v0.21.0 and v0.20.2 sections follow it in that order, one heading each.
    """

    changelog = _text("CHANGELOG.md")
    sections = changelog.split("\n## ")
    assert sections[1].startswith("Unreleased\n")
    assert sections[2].startswith("v0.21.0 \u2014 2026-10-22\n")
    assert sections[3].startswith("v0.20.2 \u2014 2026-10-17\n")
    assert changelog.count(HEADING) == 1
    assert "Unreleased (on" not in sections[2]
    assert "Unreleased (on" not in sections[3]


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


def test_the_numbers_that_drift_are_the_ones_the_code_and_the_files_have(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool counts, the PostgreSQL revision, the sizes of the README and of the known limitations page, and the
    Vite version are read from the code and the files, so a change that leaves the notes behind fails here.

    Mutations, each one alone: add a thirteenth core tool definition; add a migration after 20261011_0097; paste 700
    characters into the README or into the known limitations page; change the Vite version in ``apps/web/package.json``.
    """

    for name in ("ALICE_AGENT_API_KEY", "ALICE_MCP_FULL_TOOLS", "ALICE_MCP_LEGACY_TOOLS", "ALICE_LEGACY_SURFACES"):
        monkeypatch.delenv(name, raising=False)
    counts: dict[str, int] = {}
    for label, flags in (
        ("default", {}),
        ("full", {"ALICE_MCP_FULL_TOOLS": "1"}),
        ("legacy", {"ALICE_MCP_FULL_TOOLS": "1", "ALICE_MCP_LEGACY_TOOLS": "1"}),
        ("all", {"ALICE_MCP_FULL_TOOLS": "1", "ALICE_MCP_LEGACY_TOOLS": "1", "ALICE_LEGACY_SURFACES": "1"}),
    ):
        for name, value in flags.items():
            monkeypatch.setenv(name, value)
        counts[label] = len(list_mcp_tools())
        for name in flags:
            monkeypatch.delenv(name, raising=False)
    assert counts == {"default": 3, "full": 12, "legacy": 74, "all": 77}
    notes = _flat(NOTES_PATH)
    assert "`ALICE_MCP_FULL_TOOLS=1` lists 12 core tools, and with `ALICE_MCP_LEGACY_TOOLS=1` 74, or 77 with `ALICE_LEGACY_SURFACES=1`." in notes
    assert "The default surface is still three tools." in notes
    assert "now holds 12 core tools" in notes

    versions = REPO_ROOT / "apps" / "api" / "alembic" / "versions"
    head = sorted(path.name for path in versions.glob("2*.py"))[-1]
    assert head.startswith("20261011_0097_"), head
    assert "PostgreSQL stays at revision `20261011_0097`." in notes
    assert "There is no migration." in notes

    assert json.loads(_text("apps/web/package.json"))["pnpm"]["overrides"]["vite"] == "6.4.4"
    assert "moves from 6.4.3 to 6.4.4" in notes

    docs_section = _section(_text(NOTES_PATH), "Docs and tooling")
    readme_size = int(re.search(r"The README is about ([\d,]+) characters", docs_section).group(1).replace(",", ""))  # type: ignore[union-attr]
    limits_size = int(  # type: ignore[union-attr]
        re.search(r"known limitations page is about ([\d,]+) characters", docs_section).group(1).replace(",", "")  # type: ignore[union-attr]
    )
    assert abs(len(_text("README.md")) - readme_size) <= 150, (len(_text("README.md")), readme_size)
    assert abs(len(_text("docs/alpha/known-limitations.md")) - limits_size) <= 150, (
        len(_text("docs/alpha/known-limitations.md")),
        limits_size,
    )
    assert "down from about 28,600" in docs_section and "down from about 18,700" in docs_section


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


def test_the_release_check_accepts_the_notes_and_the_changelog_before_the_tag() -> None:
    """Mutations, each one alone: set a status of the notes to published; add the checksums file; put an entry under
    ``## Unreleased``; delete the dated heading.

    This runs the release check's own pre-tag rule on the real tree, so a change that the check would refuse at the tag
    fails on the pull request instead.
    """

    metadata, issues = release_check.validate_metadata(REPO_ROOT)
    assert issues == []
    assert metadata.version == "0.21.0"
    assert release_check.validate_release_document_state(REPO_ROOT, version=metadata.version, require_finalized=True) == []
