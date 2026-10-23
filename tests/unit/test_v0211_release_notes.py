"""The v0.21.1 release notes carry the upgrade steps, the security wording and the claims a reader can check.

Every claim in the notes comes from an entry of the changelog. These tests pin the parts a later edit could quietly
weaken: the release identity and its pending state, the six version sites, the dated changelog heading and the empty
Unreleased section, the security wording, the steps an upgrading user must act on, the numbers that drift with the
code (the tool counts, the PostgreSQL revision, the Next.js versions, the token floors) and the lists of texts whose
verdict changes, which are run against the module and against v0.16.0's own code. Each test names the mutation that
must fail it.

The notes are not published yet. Their state is ``pending`` and there is no checksums file, because the release check
requires that before the tag. The change that follows publication moves the state to ``published`` and ``recorded``,
adds the checksums file and moves the marketplace pin to the tag, and it changes the first test and the version-site
test below to say so, as the change after v0.21.0 did.
"""

from __future__ import annotations

import itertools
import json
import re
import string
import tomllib
from pathlib import Path

import pytest

import scripts.release_check as release_check
from alicebot_api import legacy_credential_check as legacy
from alicebot_api.credential_floor import credential_verdict
from alicebot_api.mcp.registry import list_mcp_tools
from tests.unit import legacy_v0160_credential_oracle as oracle

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_PATH = "docs/release/v0.21.1-release-notes.md"
HEADING = "## v0.21.1 \u2014 2026-10-23"
# A patch release: the notes are about 9,800 characters, and 12,000 leaves room for a correction and no more.
NOTES_CEILING = 12_000

# The v0.21.0 tag and the commit it points at: the marketplace file still pins them until the change after publication.
TAG_COMMIT = "c4757e626405a4bfe77a0039244fb62b483650f7"

CANDIDATE_LINE = "`v0.21.1` is the current release candidate. It is not published."
CANDIDATE_DOCS = (
    "ARCHITECTURE.md",
    "PRODUCT_BRIEF.md",
    "ROADMAP.md",
    "RELEASING.md",
    "docs/vnext/README.md",
)

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


def test_the_notes_have_the_exact_title_and_the_pending_state_and_no_checksums_file() -> None:
    """Mutations, each one alone: change the title, move the state comment, set either status to published or
    recorded, add a second state line, add ``docs/release/v0.21.1-checksums.txt``.

    The release check requires both statuses to be pending and the checksums file to be absent before the tag.
    """

    lines = _text(NOTES_PATH).splitlines()
    assert lines[0] == "# Alice v0.21.1 Release Notes"
    assert lines[1] == (
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1","version":"0.21.1",'
        '"publication_status":"pending","checksums_status":"pending"} -->'
    )
    assert sum("alice-release-state" in line for line in lines) == 1
    assert not (REPO_ROOT / "docs/release/v0.21.1-checksums.txt").exists()


def test_the_notes_stay_under_the_size_ceiling_and_keep_their_sections_in_order() -> None:
    """Mutations, each one alone: paste 3,000 characters into the notes; delete or reorder a section."""

    text = _text(NOTES_PATH)
    assert len(text) < NOTES_CEILING
    headings = re.findall(r"(?m)^## (.+?)\s*$", text)
    assert headings == SECTIONS


def test_every_version_site_names_0211_and_the_marketplace_still_pins_the_published_release() -> None:
    """Mutations, each one alone: set one version site or one plugin pin back to 0.21.0; move the marketplace ``ref``
    to v0.21.1; change one hex digit of the marketplace ``sha``; change the install command in the notes.

    The six sites are the ones ``scripts/release_check.py`` requires. The marketplace file moves to the v0.21.1 tag
    only after publication, in the change that records the digests, so here it still names the latest published
    release and the commit its tag points at.
    """

    pyproject = tomllib.loads(_text("pyproject.toml"))["project"]["version"]
    assert pyproject == "0.21.1"
    assert json.loads(_text("apps/web/package.json"))["version"] == pyproject
    assert json.loads(_text("packaging/mcpb/manifest.json"))["version"] == pyproject
    assert json.loads(_text("plugins/alice-memory/.claude-plugin/plugin.json"))["version"] == pyproject
    for name in ("plugins/alice-memory/.mcp.json", "plugins/alice-memory/hooks/hooks.json"):
        assert f"alice-memory=={pyproject}" in _text(name), name
        assert "alice-memory==0.21.0" not in _text(name), name
    marketplace = json.loads(_text(".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    assert marketplace["ref"] == "v0.21.0"
    assert marketplace["sha"] == TAG_COMMIT
    notes = _flat(NOTES_PATH)
    assert "pip install alice-memory==0.21.1 && alice-memory install" in notes
    assert "With uv: `uvx alice-memory install`." in notes


def test_the_control_documents_name_the_candidate_and_keep_v0210_as_the_latest_published_release() -> None:
    """Mutations, each one alone: delete the candidate line from one control document or from the README; name
    v0.21.1 as the latest published release; delete the ``What `v0.21.1` Targets`` section of the current state, or
    move it below the section of v0.21.0; point its release notes link at another release.

    The statements about the latest published release, the baseline and the checksums change after publication.
    """

    for name in CANDIDATE_DOCS:
        text = _text(name)
        assert text.endswith("\n\n" + CANDIDATE_LINE + "\n"), name
        assert "`v0.21.0` is the latest published release" in text, name
        assert "`v0.21.1` is the latest published release" not in text, name
    readme = _flat("README.md")
    assert (
        "`v0.21.0` is the latest published release and remains the install, checksum, and release-note baseline. "
        "`v0.20.2` is the immediately prior published release. " + CANDIDATE_LINE
    ) in readme
    state = _text("CURRENT_STATE.md")
    assert "\n## What `v0.21.1` Targets\n\n" + CANDIDATE_LINE + "\n" in state
    assert state.index("## What `v0.21.1` Targets") < state.index("## What `v0.21.0` Shipped")
    assert "\n## What `v0.21.0` Shipped\n\n`v0.21.0` is the latest published release" in state
    assert "- `v0.21.0` is the latest published release." in state
    assert "- [v0.21.1 release notes](https://github.com/samrusani/AliceMemory/blob/main/" + NOTES_PATH + ")" in state


def test_the_changelog_has_the_dated_heading_and_an_empty_unreleased_section_right_before_it() -> None:
    """Mutations, each one alone: drop the heading; change its date; put an entry under ``## Unreleased``; put another
    section between v0.21.1 and v0.21.0; put the marker for the unreleased state back into the v0.21.1 section.

    ``release_check.py --require-finalized-release-docs`` asks for the same heading and an empty Unreleased section.
    It is empty on the release commit and may hold entries once main has moved on, and the change after
    publication drops that demand from here.
    """

    changelog = _text("CHANGELOG.md")
    sections = changelog.split("\n## ")
    assert sections[1] == "Unreleased\n"
    assert sections[2].startswith("v0.21.1 \u2014 2026-10-23\n")
    assert sections[3].startswith("v0.21.0 \u2014 2026-10-22\n")
    assert sections[4].startswith("v0.20.2 \u2014 2026-10-17\n")
    assert changelog.count(HEADING) == 1
    assert "Unreleased (on" not in sections[2]
    assert "Unreleased (on" not in sections[3]


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


def test_the_numbers_that_drift_are_the_ones_the_code_and_the_files_have(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool counts, the PostgreSQL revision, the Next.js versions and the token floors are read from the code and
    the files, so a change that leaves the notes behind fails here.

    Mutations, each one alone: add a tool to the default surface (the notes say there is no new tool); add a migration
    after 20261011_0097; change ``next`` or ``eslint-config-next`` in ``apps/web/package.json``; change a floor in
    ``legacy_credential_check._TOKEN_BODIES``; change the character class of ``ghs_`` or ``ghu_``.
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
    # The counts of v0.21.0: this release adds no tool.
    assert counts == {"default": 3, "full": 12, "legacy": 74, "all": 77}
    notes = _flat(NOTES_PATH)
    assert "There is no new tool, command or setting, and there is no migration." in notes

    versions = REPO_ROOT / "apps" / "api" / "alembic" / "versions"
    head = sorted(path.name for path in versions.glob("2*.py"))[-1]
    assert head.startswith("20261011_0097_"), head
    assert "PostgreSQL stays at revision `20261011_0097`." in notes

    package = json.loads(_text("apps/web/package.json"))
    assert package["dependencies"]["next"] == "16.4.1"
    assert package["devDependencies"]["eslint-config-next"] == "16.4.1"
    console = _section(_text(NOTES_PATH), "The web console and Next.js 16.4.1")
    assert "Next.js moves from 16.3.8 to 16.4.1, and `eslint-config-next` moves with it." in console
    assert "The PyPI package does not include the console" in console

    floors = {marker: body.floor for marker, body in legacy._TOKEN_BODIES.items()}
    assert floors == {
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
    assert legacy._TOKEN_BODIES["ghs_"].chars == legacy._TOKEN_BODIES["ghu_"].chars == "0-9A-Za-z._-"
    for marker in ("ghp_", "gho_", "npm_"):
        assert legacy._TOKEN_BODIES[marker].chars == "0-9A-Za-z", marker
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


def test_the_release_check_accepts_the_notes_and_the_changelog_before_the_tag() -> None:
    """Mutations, each one alone: set a status of the notes to published; add the checksums file; put an entry under
    ``## Unreleased``; delete the dated heading; set one version site back to 0.21.0.

    This runs the release check's own pre-tag rule on the real tree, so a change that the check would refuse at the tag
    fails on the pull request instead.
    """

    metadata, issues = release_check.validate_metadata(REPO_ROOT)
    assert issues == []
    assert metadata.version == "0.21.1"
    assert release_check.validate_release_document_state(REPO_ROOT, version=metadata.version, require_finalized=True) == []
