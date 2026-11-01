"""The v0.21.1 release notes carry the upgrade steps, the security wording and the claims a reader can check.

Every claim in the notes comes from an entry of the changelog. These tests pin the parts a later edit could quietly
weaken: the release identity and its published state, the checksums record, the marketplace pin, the dated changelog
heading, the security wording, the steps an upgrading user must act on, the numbers that drift
(the PostgreSQL revision, the Next.js versions and the token floors, read at the v0.21.1 tag) and the lists of texts
whose verdict changes, which are run against the module and against v0.16.0's own code. Each test names the mutation
that must fail it.

The notes are published. The change that followed publication moved their state to ``published`` and ``recorded``, added
the checksums file, moved the marketplace file to the v0.21.1 tag and its commit, named v0.21.1 as the latest published
release in the control documents and removed the release candidate line, as the change after v0.21.0 did, and added one
dated 2026-10-23 update beside the notes' own marketplace step. The change after v0.22.0 moved the marketplace file
and the control documents on to v0.22.0, which ``test_v0220_release_notes.py`` pins, and added a dated 2026-10-27
follow-up after that update. The change after v0.23.0 moved them on to v0.23.0, which ``test_v0230_release_notes.py``
pins, and added a dated 2026-10-30 follow-up after those. The change after v0.23.1 moved them on to v0.23.1, which
``test_v0231_release_notes.py`` pins, and added a dated 2026-11-01 follow-up after those. The tagged lines of the notes
and of the changelog section are held by ``test_published_release_notes_stay_immutable.py``. The release check's pre-tag
mode refuses the tree now, and one test below pins the three reasons.
"""

from __future__ import annotations

import ast
import itertools
import json
import re
import string
from pathlib import Path

import pytest

import scripts.release_check as release_check
from alicebot_api import legacy_credential_check as legacy
from alicebot_api.credential_floor import credential_verdict
from tests.unit import legacy_v0160_credential_oracle as oracle
from tests.unit.test_published_release_notes_stay_immutable import _tagged
from tests.unit.test_v0210_release_notes import _tagged_listing

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_PATH = "docs/release/v0.21.1-release-notes.md"
RELEASE_TAG = "v0.21.1"
HEADING = "## v0.21.1 \u2014 2026-10-23"
# A patch release: the notes are about 9,800 characters, and 12,000 leaves room for a correction and no more.
NOTES_CEILING = 12_000

# The commit of the latest tag (v0.23.1), which the marketplace file pins now, and the digests of the two files
# published for v0.21.1.
CHECKSUMS_PATH = "docs/release/v0.21.1-checksums.txt"
LATEST_TAG_COMMIT = "2443e7cb6d8f89b4e625cec7adcd7b4a8dcd7276"
WHEEL_SHA256 = "49452a0d9726c06b8b1f1018527cc90a756b3f252cba6b266816eeefa2a931c3"
SDIST_SHA256 = "813459987dac7837ba2414a803937de2088cac3a1ce81c6687aa835ce12deb6d"

SECTIONS = [
    "What to do after upgrading",
    "Credential markers",
    "The web console and Next.js 16.4.1",
    "Behaviour changes an integrator will notice",
    "Security",
    "What this release does not claim",
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
    return changelog.split(HEADING)[1].split("\n## v0.21.0")[0]


def _entries() -> list[str]:
    return [" ".join(item.split()) for item in _changelog_section().split("\n- ")[1:]]


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


def test_the_notes_are_published_with_the_exact_title_state_and_a_checksums_record() -> None:
    """Mutations, each one alone: change the title, move the state comment, set either status back to pending,
    add a second state line, delete the checksums file.
    """

    lines = _text(NOTES_PATH).splitlines()
    assert lines[0] == "# Alice v0.21.1 Release Notes"
    assert lines[1] == (
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1","version":"0.21.1",'
        '"publication_status":"published","checksums_status":"recorded"} -->'
    )
    assert sum("alice-release-state" in line for line in lines) == 1
    assert (REPO_ROOT / CHECKSUMS_PATH).is_file()


def test_the_checksums_file_records_the_two_published_digests_in_the_format_of_the_last_release() -> None:
    """Mutations, each one alone: change one hex digit of either digest; swap the two file names; add a third
    line; write another version into the header or into the verify commands; delete a header line.

    The digests are the ones PyPI serves for 0.21.1 and the GitHub Release lists for its assets, read on
    2026-10-23 (wheel 1,929,955 bytes, source distribution 1,694,466 bytes). The header and the layout are the v0.21.0
    file's with the version replaced, so the two files stay in one format. Nothing offline compares the digests with
    PyPI: ``release_check.py --tag v0.21.1 --verify-pypi-artifacts`` does, with the two files downloaded.
    """

    old = _text("docs/release/v0.21.0-checksums.txt").splitlines()
    new = _text(CHECKSUMS_PATH).splitlines()
    assert len(old) == len(new) == 19
    assert [line.replace("0.21.0", "0.21.1") for line in old[:17]] == new[:17]
    assert new[0] == "# Alice v0.21.1: published artifact SHA-256 digests"
    assert new[17:] == [
        f"{WHEEL_SHA256}  alice_memory-0.21.1-py3-none-any.whl",
        f"{SDIST_SHA256}  alice_memory-0.21.1.tar.gz",
    ]
    assert _text(CHECKSUMS_PATH).endswith(".tar.gz\n")


def test_the_notes_stay_under_the_size_ceiling_and_keep_their_sections_in_order() -> None:
    """Mutations, each one alone: paste 3,000 characters into the notes; delete or reorder a section."""

    text = _text(NOTES_PATH)
    assert len(text) < NOTES_CEILING
    headings = re.findall(r"(?m)^## (.+?)\s*$", text)
    assert headings == SECTIONS


def test_the_marketplace_pins_the_latest_published_release_and_the_notes_carry_the_install_lines() -> None:
    """Mutations, each one alone: set the marketplace ``ref`` back to v0.23.0; change one hex digit of the marketplace
    ``sha``; change the install command in the notes.

    The post-publication change of v0.21.1 moved the marketplace file to the v0.21.1 tag, the one of v0.22.0 moved it
    on to the v0.22.0 tag, the one of v0.23.0 moved it on to the v0.23.0 tag, and the one of v0.23.1 moved it on to the
    v0.23.1 tag and the commit that tag points at, so it names the latest published release. The v0.21.1 tag itself
    still pins v0.21.0, and the plugin page says so. The tree has moved on to the version sites of the next release,
    which ``test_v0231_release_notes.py`` pins, so this test no longer reads them. The control documents and the
    install lines name v0.23.1 as the latest release, and that test pins them too.
    """

    marketplace = json.loads(_text(".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    assert marketplace["ref"] == "v0.23.1"
    assert marketplace["sha"] == LATEST_TAG_COMMIT
    notes = _flat(NOTES_PATH)
    assert "pip install alice-memory==0.21.1 && alice-memory install" in notes
    assert "With uv: `uvx alice-memory install`." in notes


def test_the_notes_say_beside_their_marketplace_step_that_main_now_pins_v0211_then_v0220_then_v0230_then_v0231() -> None:
    """Mutations, each one alone: delete one of the four dated updates; name v0.21.0 as what ``main`` pins in the
    first, v0.21.1 in the second, v0.22.0 in the third or v0.23.0 in the fourth; drop the sentence about the copy
    inside the tag; move them away from step 6 of the upgrade steps; swap two of them; glue one to the end of another
    paragraph; add a fifth dated update.

    Step 6 of the upgrade steps says the marketplace file on ``main`` pins the v0.21.0 tag commit until a change after
    the release moves it. That sentence stays as tagged, and the update after it says the change has been made, as the
    update beside the marketplace steps of the v0.20.2 notes did. The 2026-10-27 follow-up says the file was moved on
    to the v0.22.0 tag, and that the copy inside the v0.22.0 tag pins v0.21.1. The 2026-10-30 follow-up says it was moved
    on to the v0.23.0 tag, and that the copy inside the v0.23.0 tag pins v0.22.0. The 2026-11-01 follow-up says it was
    moved on to the v0.23.1 tag, and that the copy inside the v0.23.1 tag pins v0.23.0.
    """

    update = (
        "**Update (2026-10-23):** v0.21.1 is published, and the marketplace file on `main` now pins the v0.21.1 tag "
        "commit, so the marketplace install runs v0.21.1. The copy inside the v0.21.1 tag still pins v0.21.0, so add "
        "the marketplace from the repository, not from a checkout of the tag."
    )
    follow_up = (
        "**Update (2026-10-27):** v0.22.0 is published, and the marketplace file on `main` now pins the v0.22.0 tag "
        "commit, so the marketplace install runs v0.22.0. The copy inside the v0.21.1 tag still pins v0.21.0 and the "
        "copy inside the v0.22.0 tag still pins v0.21.1, so add the marketplace from the repository, not from a "
        "checkout of a tag."
    )
    second_follow_up = (
        "**Update (2026-10-30):** v0.23.0 is published, and the marketplace file on `main` now pins the v0.23.0 tag "
        "commit, so the marketplace install runs v0.23.0. The copy inside the v0.21.1 tag still pins v0.21.0, the copy "
        "inside the v0.22.0 tag still pins v0.21.1 and the copy inside the v0.23.0 tag still pins v0.22.0, so add the "
        "marketplace from the repository, not from a checkout of a tag."
    )
    third_follow_up = (
        "**Update (2026-11-01):** v0.23.1 is published, and the marketplace file on `main` now pins the v0.23.1 tag "
        "commit, so the marketplace install runs v0.23.1. The copy inside the v0.21.1 tag still pins v0.21.0, the copy "
        "inside the v0.22.0 tag still pins v0.21.1, the copy inside the v0.23.0 tag still pins v0.22.0 and the copy "
        "inside the v0.23.1 tag still pins v0.23.0, so add the marketplace from the repository, not from a checkout of "
        "a tag."
    )
    blocks = _blocks(NOTES_PATH)
    positions = [index for index, block in enumerate(blocks) if block == update]
    assert len(positions) == 1, positions
    index = positions[0]
    assert blocks[index - 1].endswith(
        "6. **Claude Code plugin.** The marketplace file on `main` pins the v0.21.0 tag commit until a change after "
        "this release moves it, so a marketplace install runs v0.21.0 code until then."
    )
    assert blocks[index + 1] == follow_up
    assert blocks[index + 2] == second_follow_up
    assert blocks[index + 3] == third_follow_up
    assert blocks[index + 4] == "## Credential markers"
    assert _text(NOTES_PATH).count("Update (2026-10-23)") == 1
    assert _text(NOTES_PATH).count("Update (2026-10-27)") == 1
    assert _text(NOTES_PATH).count("Update (2026-10-30)") == 1
    assert _text(NOTES_PATH).count("Update (2026-11-01)") == 1
    assert sum(block.startswith("**Update (") for block in blocks) == 4


def test_the_changelog_has_the_dated_heading_right_after_the_unreleased_section() -> None:
    """Mutations, each one alone: drop the heading; change its date; put another section between v0.21.1 and v0.21.0;
    put the marker for the unreleased state back into the v0.21.1 section.

    The Unreleased section is empty on the release commit. It may hold entries once main has moved on, so this test does
    not ask for it to be empty. It asks that the v0.23.1, v0.23.0, v0.22.0, v0.21.1, v0.21.0 and v0.20.2 sections follow
    it in that order, one heading each.
    """

    changelog = _text("CHANGELOG.md")
    sections = changelog.split("\n## ")
    assert sections[1].startswith("Unreleased\n")
    assert sections[2].startswith("v0.23.1 \u2014 2026-10-31\n")
    assert sections[3].startswith("v0.23.0 \u2014 2026-10-30\n")
    assert sections[4].startswith("v0.22.0 \u2014 2026-10-26\n")
    assert sections[5].startswith("v0.21.1 \u2014 2026-10-23\n")
    assert sections[6].startswith("v0.21.0 \u2014 2026-10-22\n")
    assert sections[7].startswith("v0.20.2 \u2014 2026-10-17\n")
    assert changelog.count(HEADING) == 1
    assert "Unreleased (on" not in sections[5]
    assert "Unreleased (on" not in sections[6]


def test_no_marker_for_the_unreleased_state_is_left_in_the_documents_the_code_or_the_manifest() -> None:
    """Mutation: put the marker for the unreleased state of v0.21.0 back into any document, docstring, test or case of
    the mutation manifest.

    The pattern is written as a regular expression so that this file does not hold the marker itself. The published
    v0.18.0 notes carry two dated corrections that say ``not in v0.19.0``; they are another marker and immutable.
    """

    pattern = re.compile(r"Unreleased\s*\(\s*on\s+main\s*,\s*not\s+in\s+v0\.21\.0")
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
    write independently, third-party or penetration; name an external review tool; write an advisory id; delete the
    review sentence, or only its ``internal adversarial review`` clause.
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
    assert "This release was checked with " + review + "." in flat
    lowered = flat.lower()
    for forbidden in ("independently", "third-party", "third party", "penetration", "pentest", "external review"):
        assert forbidden not in lowered, forbidden
    assert "ghsa-" not in lowered and "cve-" not in lowered
    assert "Security" in SECTIONS


def test_the_upgrade_steps_name_every_action_an_existing_user_takes_in_order() -> None:
    """Mutations, each one alone: delete one numbered step; swap two; drop the command, the revision or the flag a
    step names.
    """

    steps = _section(_text(NOTES_PATH), "What to do after upgrading")
    numbers = [int(number) for number in re.findall(r"(?:^| )(\d{1,2})\. \*\*", steps)]
    assert numbers == list(range(1, 7))
    for needle in (
        "`uv tool upgrade alice-memory`",
        "`pip install -U alice-memory`",
        "`alice-memory --version`",
        "Nothing else is required.",
        "**No migration.**",
        "A vault that v0.21.0 wrote opens in v0.21.1, and a vault that v0.21.1 has opened opens in v0.21.0.",
        "PostgreSQL stays at revision `20261011_0097`.",
        "Run `alice-memory import-markdown` on the folder again to import a file that v0.21.0 skipped for its name.",
        "preview with `--supersede --dry-run`, then run with `--supersede`",
        "**Run `alice-memory doctor`** on each SQLite vault.",
        "remove the source with `alice-memory sources delete <id>`",
        "`doctor` reads sources and not the text of a memory",
        "run `pnpm --dir apps/web install` and then `pnpm --dir apps/web build`",
        "keep the console reachable only from this machine",
        "The marketplace file on `main` pins the v0.21.0 tag commit until a change after this release moves it",
    ):
        assert needle in steps, needle
    assert steps.index("**No migration.**") < steps.index("**Notes with part or order codes.**") < steps.index("**Console.**")


def _tagged_token_bodies() -> dict[str, tuple[str, int]]:
    """``{marker: (character class, floor)}`` from the credential check as it is at the release tag.

    The table is read from the source at the tag with ``ast``, not from the module in the working tree, so a later
    change to the floors does not make the published notes wrong. The character classes are module level string
    constants that the rows name.
    """

    tree = ast.parse(_tagged(RELEASE_TAG, "apps/api/src/alicebot_api/legacy_credential_check.py"))
    constants: dict[str, str] = {}
    table: ast.Dict | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                constants[node.targets[0].id] = node.value.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "_TOKEN_BODIES":
            assert isinstance(node.value, ast.Dict)
            table = node.value
    assert table is not None
    bodies: dict[str, tuple[str, int]] = {}
    for key, value in zip(table.keys, table.values, strict=True):
        assert isinstance(key, ast.Constant) and isinstance(value, ast.Call)
        chars, floor = value.args[0], value.args[1]
        assert isinstance(chars, ast.Name) and isinstance(floor, ast.Constant)
        bodies[key.value] = (constants[chars.id], floor.value)
    return bodies


def test_the_numbers_in_the_notes_are_the_ones_the_release_tag_has() -> None:
    """The PostgreSQL revision, the Next.js versions and the token floors are read from the files at the ``v0.21.1``
    tag, because the notes are published and describe that tag; a later change to main does not make them wrong. The
    absence of a new tool is pinned as text: ``test_published_release_notes_stay_immutable.py`` keeps the published
    sentence fixed, and the tool counts (12, 74 and 77, unchanged from v0.21.0) were checked against the code while the
    release was a candidate.

    Mutations, each one alone: set ``RELEASE_TAG`` to ``v0.21.0`` (the tagged Next.js version is 16.3.8 then); change
    ``20261011_0097``, a Next.js version, a floor or a character class in the notes (the immutability test fails too);
    drop the sentence that says there is no new tool. Changing ``next`` in the working tree's ``apps/web/package.json``
    does not fail this test: it reads the tag, which is the point.
    """

    notes = _flat(NOTES_PATH)
    assert "There is no new tool, command or setting, and there is no migration." in notes

    listing = _tagged_listing(RELEASE_TAG, "apps/api/alembic/versions")
    head = sorted(name for name in listing if name.startswith("2") and name.endswith(".py"))[-1]
    assert head.startswith("20261011_0097_"), head
    assert "PostgreSQL stays at revision `20261011_0097`." in notes

    package = json.loads(_tagged(RELEASE_TAG, "apps/web/package.json"))
    assert package["dependencies"]["next"] == "16.4.1"
    assert package["devDependencies"]["eslint-config-next"] == "16.4.1"
    console = _section(_text(NOTES_PATH), "The web console and Next.js 16.4.1")
    assert "Next.js moves from 16.3.8 to 16.4.1, and `eslint-config-next` moves with it." in console
    assert "The PyPI package does not include the console" in console

    bodies = _tagged_token_bodies()
    assert {marker: floor for marker, (_chars, floor) in bodies.items()} == {
        "ghp_": 36,
        "gho_": 36,
        "ghs_": 36,
        "ghu_": 36,
        "github_pat_": 82,
        "glpat-": 20,
        "npm_": 36,
        "sk-": 20,
        "xoxb-": 10,
        "xoxp-": 10,
        "xoxa-": 10,
        "xoxs-": 10,
    }
    assert bodies["ghs_"][0] == bodies["ghu_"][0] == "0-9A-Za-z._-"
    for marker in ("ghp_", "gho_", "npm_"):
        assert bodies[marker][0] == "0-9A-Za-z", marker
    markers = _section(_text(NOTES_PATH), "Credential markers")
    for needle in (
        "(`ghp_`, `gho_`, `ghs_`, `ghu_`, `github_pat_`, `glpat-`, `npm_`, `sk-`, `xoxb-`, `xoxp-`, `xoxa-`, `xoxs-`)",
        "- 36 letters and digits for `ghp_`, `gho_` and `npm_`.",
        "- 36 letters, digits, dots, hyphens or underscores for `ghs_` and `ghu_`.",
        "- 82 for `github_pat_`, 20 for `glpat-` and `sk-`, and 10 for the four Slack markers.",
        "takes the floor of 20",
    ):
        assert needle in markers, needle


# ---------------------------------------------------------------------------
# The texts whose verdict changes, run against the module and against v0.16.0's own code. v0.21.0 read the bare
# marker as v0.16.0 did, and the comparison harness (test_legacy_credential_check.py) holds the module to
# v0.16.0's verdict on everything the floors and the lead do not touch, so the oracle stands for v0.21.0 on the texts
# below, none of which a carve-out excuses. The verdicts were also read from the v0.21.0 module itself.
# Synthetic tokens are built from pieces and a sequential run, so no whole scanner-shaped token sits in the source.
# ---------------------------------------------------------------------------

_MIXED = "".join(
    f"{upper}{lower}{digit}"
    for upper, lower, digit in zip(string.ascii_uppercase, string.ascii_lowercase, itertools.cycle(string.digits))
)


def _seq(size: int) -> str:
    return (_MIXED * (size // len(_MIXED) + 1))[:size]


def _refused(module: object, text: str) -> tuple[bool, bool]:
    """(commit gate, promotion credential floor) for a note with this text."""

    return (
        bool(module.commit_gate_refuses("Note", text)),  # type: ignore[attr-defined]
        bool(module.promotion_floor_refuses("Note", text)),  # type: ignore[attr-defined]
    )


# Refused in v0.21.0 at both doors, accepted now at both: the four texts of the comparison table that move, and the
# short codes and names the notes give.
_NOW_ACCEPTED = (
    "SK-LEARN is the old name",
    "sk-learn_v2 and friends",
    "sk-abcdefghijklmnopq",
    "sk-ssh-ed25519@openssh.co",
    "SK-2291",
    "sk-2291 is current",
    "SK-229",
    "npm_config_cache",
    "SK-20260301",
    "SK-2025-0618",
    "SK-2291-REV",
    "Parts: SK-2291, HD-40213, PO-55102, TB-12, NCR-0042",
    "2025-06-18-parts-note-legacy-seal-kit-sk-2192.md",
)


@pytest.mark.parametrize("text", _NOW_ACCEPTED)
def test_the_codes_and_names_the_notes_give_were_refused_by_v0210_and_are_accepted_now(text: str) -> None:
    """Mutations, each one alone: set a floor in ``legacy._TOKEN_BODIES`` to 0 (the bare marker comes back); put the
    ``sk-`` prefix pattern back to 8 characters; add a text to the tuple that the module still refuses.
    """

    assert _refused(oracle, text) == (True, True), text
    assert _refused(legacy, text) == (False, False), text


# The underscore led tokens the notes name: (text, whether the commit gate moves too). The commit gate's own prefix
# rules already read an underscore led ghp_, sk- and xoxb-, so only the promotion floor moves for those three.
_NOW_REFUSED = {
    "_ghs_ and 36 characters": ("_" + "gh" + "s_" + _seq(36), True),
    "x_ghs_ and 36 characters": ("x_" + "gh" + "s_" + _seq(36), True),
    "_ghu_ and 36 characters": ("_" + "gh" + "u_" + _seq(36), True),
    "_gho_ and 36 characters": ("_" + "gh" + "o_" + _seq(36), True),
    "TOKEN_npm_ and 36 characters": ("TOKEN_" + "np" + "m_" + _seq(36), True),
    "_glpat- and 20 characters": ("_glp" + "at-" + _seq(20), True),
    "_github_pat_ and 82 characters": ("_github" + "_pat_" + _seq(22) + "_" + _seq(59), True),
    "_ghp_ and 36 characters": ("_" + "gh" + "p_" + _seq(36), False),
    "_sk- and 20 characters": ("_sk" + "-" + _seq(20), False),
    "_xoxb- and a Slack body": ("_xo" + "xb-" + "1234567890-1234567890-" + _seq(24), False),
}


@pytest.mark.parametrize("label", sorted(_NOW_REFUSED))
def test_a_token_right_after_an_underscore_was_accepted_by_v0210_and_is_refused_now(label: str) -> None:
    """Mutations, each one alone: put the lead boundary of the floored markers back to v0.16.0's, which counts an
    underscore; remove a marker from the table of bodies; flip the commit gate flag of a row of the table.
    """

    text, commit_moves = _NOW_REFUSED[label]
    assert _refused(oracle, text) == (not commit_moves, False), label
    assert _refused(legacy, text) == (True, True), label


def test_the_notes_list_the_underscore_led_tokens_the_table_moves() -> None:
    """The two lists in the notes (commit gate and promotion floor; promotion floor only) are the keys of the table
    above, so a text added to or moved between the lists fails here.

    Mutations, each one alone: move ``_ghp_`` into the first list of the notes; delete ``_xoxb-`` from the second.
    """

    both = [label for label, (_text_, commit_moves) in _NOW_REFUSED.items() if commit_moves]
    floor_only = [label for label, (_text_, commit_moves) in _NOW_REFUSED.items() if not commit_moves]
    assert len(both) == 7 and len(floor_only) == 3
    behaviour = _section(_text(NOTES_PATH), "Behaviour changes an integrator will notice")
    assert (
        "Accepted in v0.21.0 and refused now: `_ghs_`, `x_ghs_`, `_ghu_`, `_gho_` and `TOKEN_npm_` followed by 36 "
        "characters, `_glpat-` followed by 20 and `_github_pat_` followed by 82, at the commit gate and the "
        "promotion credential floor; `_ghp_` followed by 36, `_sk-` followed by 20 and `_xoxb-` followed by a Slack "
        "token body, at the promotion credential floor only."
    ) in behaviour
    assert (
        "Refused in v0.21.0 and accepted now: `SK-LEARN is the old name`, `sk-learn_v2 and friends`, an `sk-` "
        "followed by 17 letters, a FIDO key type name cut short by one letter, and every short code and name above."
    ) in behaviour
    markers = _section(_text(NOTES_PATH), "Credential markers")
    assert (
        "The commit gate already refused `_ghp_`, `_sk-` and `_xoxb-` tokens through its own prefix rules, so for "
        "those three only the promotion credential floor changes."
    ) in markers


def test_the_boundary_that_stays_and_the_rules_that_do_not_change() -> None:
    """The texts the notes say are not read or are still refused give the same verdict as v0.21.0.

    Mutations, each one alone: drop the letter or digit from the lead boundary (a marker glued onto a word is read);
    take the ``ghs_`` class back to letters and digits (a long dotted body after ``ghs_`` stops being read); change the
    ``ghp_`` or ``xoxb-`` prefix rule from 8 to 9 characters; raise the minimum of the credential detector's ``sk-`` rule
    so that it stops reading the lower case code.
    """

    body = "ab.cd-ef_gh" * 4
    still_read = {
        "ghs_ followed by 36 class characters": "Use " + "gh" + "s_" + body[:36] + " for the deploy.",
        "ghu_ followed by 36 class characters": "Use " + "gh" + "u_" + body[:36] + " for the deploy.",
        "ghs_ followed by a long snake case name": "Use " + "gh" + "s_" + "weekly_import_job_configuration_name_one",
        "the stateless installation token": "gh" + "s_" + "1234" + "_" + "eyJ" + _seq(33) + "." + "eyJ" + _seq(127) + "." + _seq(342),
        "ghp_ and 8 characters": "Room " + "gh" + "p_" + "12345678" + " is booked.",
        "xoxb- and 8 characters": "Room " + "xo" + "xb-" + "12345678" + " is booked.",
        "an AWS key id": "Key " + "AK" + "IA" + "ABCDEFGH12345678" + " is listed.",
    }
    for label, text in still_read.items():
        assert _refused(oracle, text)[0], label
        assert _refused(legacy, text)[0], label
    assert _refused(legacy, still_read["the stateless installation token"]) == (True, True)
    # The credential detector is another module (``credential_floor``): it reads a lower case ``sk-`` followed by 8 or
    # more characters that hold a digit, and not the upper case code.
    assert credential_verdict("Order sk-20260301 today.") is not None
    assert credential_verdict("Order SK-20260301 today.") is None
    # Not read by v0.21.0 and not read now: a letter or a digit before the marker, and a short name after an underscore
    # (the underscore hid the marker in v0.21.0, and the floor keeps the name out now).
    same_as_v0210 = {
        "a letter and then a ghs_ token": "Use app token X" + "gh" + "s_" + _seq(36) + " for the deploy.",
        "a digit and then an npm_ token": "Use app token 7" + "np" + "m_" + _seq(36) + " for the deploy.",
        "PART_SK-2291": "PART_SK-2291",
        "deploy_ghu_config": "deploy_ghu_config is the setting.",
    }
    for label, text in same_as_v0210.items():
        assert _refused(oracle, text) == (False, False), label
        assert _refused(legacy, text) == (False, False), label
    # A public key line is excused by the carve-out of the module, which v0.16.0's own code does not have.
    assert _refused(legacy, "Deploy key: ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ deploy@host") == (False, False)


def test_the_notes_name_the_limits_they_say_matter() -> None:
    """Mutations, each one alone: delete a clause of the last section; say the floors are a published minimum; say
    ``doctor`` reads memory text.
    """

    section = _section(_text(NOTES_PATH), "What this release does not claim")
    for needle in (
        "It does not claim that every credential is read.",
        "A marker followed by fewer token characters than its floor is not read",
        "a marker glued onto a letter or a digit is not read",
        "The floors of `github_pat_`, `glpat-`, `npm_`, `sk-` and the Slack tokens are not a published minimum.",
        "lines that v0.21.0 withheld stay withheld",
        "a memory that was committed with a token right after an underscore stays stored",
        "`doctor` does not read memory text",
        "It does not claim that the console has no other advisory, only that the two named in the CHANGELOG are fixed.",
    ):
        assert needle in section, needle


def test_the_headline_changes_of_the_notes_each_have_a_changelog_entry() -> None:
    """Mutations, each one alone: delete the changelog entry of a change the notes name, or reword its first words;
    delete the sentence of the entry that names the underscore.
    """

    starts = (
        "The web console's Next.js moves from 16.3.8 to 16.4.1, and `eslint-config-next` with it.",
        "A short code that starts with a credential marker, such as `SK-2291` or `sk-2291 is current`,",
    )
    entries = _entries()
    assert len(entries) == 2
    for start in starts:
        assert len([entry for entry in entries if entry.startswith(start)]) == 1, start
    credential = [entry for entry in entries if entry.startswith(starts[1])][0]
    assert "A token right after an underscore (`_ghs_` and a token, `TOKEN_npm_` and 36 characters) is now read too" in credential
    assert credential.endswith("No migration is required.")
    console = [entry for entry in entries if entry.startswith(starts[0])][0]
    assert "The PyPI package does not include the web console." in console
    assert console.endswith("No migration is required.")
    notes = _flat(NOTES_PATH)
    assert "v0.21.1 accepts and keeps them." in notes
    assert "v0.21.1 refuses or withholds it." in notes


def test_the_release_check_accepts_the_tree_and_its_pre_tag_mode_refuses_the_published_notes() -> None:
    """Mutations, each one alone: set a status of the notes back to pending; delete the checksums file; delete the
    dated heading.

    ``release_check.py`` passes on the tree. Its ``--require-finalized-release-docs`` mode is the pre-tag rule and
    refuses a published tree on purpose, for the same three reasons as after v0.21.0: both statuses must be pending
    before the tag, and the checksums file must not exist on the pre-publication tag. The package version of the tree is
    the next release's (``test_v0220_release_notes.py`` pins it), so the rule is run for the version of these notes.
    """

    metadata, issues = release_check.validate_metadata(REPO_ROOT)
    assert issues == []
    version = "0.21.1"
    assert metadata.version != version
    assert release_check.validate_release_document_state(REPO_ROOT, version=version, require_finalized=False) == []
    published = [
        "release notes alice-release-state publication_status must be pending before tag",
        "release notes alice-release-state checksums_status must be pending before tag",
        "docs/release/v0.21.1-checksums.txt must not exist on the pre-publication tag",
    ]
    issues = release_check.validate_release_document_state(REPO_ROOT, version=version, require_finalized=True)
    assert [issue for issue in issues if issue in published] == published
    # Once main has an Unreleased CHANGELOG entry, the pre-tag mode also says the section must be empty for a tag.
    # That is true of every later commit and is the only other issue allowed here.
    assert [issue for issue in issues if issue not in published] in (
        [],
        ["CHANGELOG.md Unreleased section must be empty for the release tag"],
    )
