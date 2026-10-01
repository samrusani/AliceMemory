"""The v0.19.2 notes and docs carry the corrections the claim checks asked for.

Each test names the sites that state one claim and pins the corrected wording at
every one of them, so a site that is edited back to the old wording fails. The
claims were checked by running v0.19.0 and the v0.19.1 commit side by side;
v0.19.2 carries that commit's product code unchanged. The
docstring of each test names the mutation it kills.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _flat(path: str) -> str:
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    return " ".join(text.split())


NOTES = "docs/release/v0.19.2-release-notes.md"
CHANGELOG = "CHANGELOG.md"


def _released_changelog() -> str:
    changelog = _flat(CHANGELOG)
    return changelog[changelog.index("## v0.19.2") : changelog.index("## v0.19.0")]


def test_resume_fails_on_a_long_query_once_any_active_memory_exists() -> None:
    """Mutation: say "stored decision" alone, at any one of the four sites.

    alice_resume fails at about 50,000 bytes once the vault holds an active
    memory of any type. alice_recent_decisions fails only with a stored decision.
    """

    for site, text in (
        ("notes", _flat(NOTES)),
        ("changelog", _released_changelog()),
        ("mcp-tools", _flat("docs/alpha/mcp-tools.md")),
        ("known-limitations", _flat("docs/alpha/known-limitations.md")),
    ):
        assert "an active memory of any type" in text, site
        assert "once it holds a stored decision" in text or "once the vault holds a stored decision" in text, site
        assert "there once the vault holds a stored decision" not in text, site
        assert "once the vault holds a stored decision. A query of 60,000" not in text, site


def test_import_lists_with_the_floor_and_the_doctor_uses_the_wider_verdict() -> None:
    """Mutation: say import lists what the commit door's verdict flags.

    A low-entropy key shaped like an AWS access key id in a chunk is flagged by
    the doctor and not listed by import.
    """

    notes = _flat(NOTES)
    assert "lists what the verdict flags" not in notes
    assert "so its listing is narrower than the commit door's verdict and than the doctor" in notes
    assert "The reading is the credential floor" in notes
    assert "import restores it and lists none" in notes
    assert "flagged by the doctor, and import restores it and lists none" in _released_changelog()
    backup = _flat("docs/alpha/backup-and-restore.md")
    assert "so it is broader than the import listing" in backup
    assert "with the credential floor, the check a memory row gets" in backup


def test_the_nesting_claim_names_the_carrier_keys_and_the_generic_error() -> None:
    """Mutation: say any JSON text too deep for the decoder gets restore_failed.

    A column that is itself too-deep JSON text fails with alice_memory_failed.
    In v0.19.0 only the deep JSON text makes recall and resume raise: a mapping
    nested a few hundred levels is stored and reads back.
    """

    for site, text in (
        ("notes", _flat(NOTES)),
        ("changelog", _released_changelog()),
        ("backup-and-restore", _flat("docs/alpha/backup-and-restore.md")),
        ("known-limitations", _flat("docs/alpha/known-limitations.md")),
    ):
        assert "`alice_memory_failed`" in text, site
    for site, text in (
        ("notes", _flat(NOTES)),
        ("changelog", _released_changelog()),
        ("backup-and-restore", _flat("docs/alpha/backup-and-restore.md")),
    ):
        assert "under an `agentic_memory` or `agent_identity` key" in text, site
    assert "is also refused and nothing is written, but with the generic `alice_memory_failed`" in _flat(NOTES)
    assert (
        "under `agentic_memory` in a memory row it makes `alice_recall` raise, and under `agent_identity` in an event"
        " row it makes `alice_resume` raise." in _flat(NOTES)
    )
    assert "which is decoded and held to the same 256 levels" in _flat(NOTES)
    assert "which is decoded and held to the same 256 levels" in _released_changelog()
    assert "a source, event or memory revision row with such a column gave `restore_failed`" in _flat(NOTES)


def test_the_credential_count_line_is_not_printed_with_quarantine() -> None:
    """Mutation: say the credential count line is printed every time, anywhere."""

    notes = _flat(NOTES)
    assert "The count line is printed every time except with `--quarantine`, where the receipt keeps" in notes
    assert "printed every time except with `--quarantine`. A deeply nested record is refused" in notes
    assert "(every time except with `--quarantine`)" in _released_changelog()
    assert "printed every time except with `--quarantine`" in _flat("docs/alpha/backup-and-restore.md")
    assert "`credential-shaped text in records import does not refuse: N`, each printed every" not in notes


def test_the_review_hint_is_only_listed_with_the_full_tool_set() -> None:
    """Mutation: drop the full tool set note from the notes or the changelog."""

    notes = _flat(NOTES)
    assert "so the new hint reaches you only with the full tool set" in notes
    assert "It is listed only with `ALICE_MCP_FULL_TOOLS=1`" in notes
    assert "With the full tool set, of the eleven core tools six are now read-only" in notes
    assert "Of the eleven core tools, six are now read-only" not in notes
    assert "so the new hint reaches a host only with the full tool set" in _released_changelog()


def test_the_headline_does_not_claim_no_features() -> None:
    """Mutation: restore "It adds no features" or drop the marketplace sentence."""

    notes = _flat(NOTES)
    lead = notes[: notes.index("## What to do after upgrading")]
    assert "adds no features" not in lead
    assert "It adds no tool or command and changes no schema." in lead
    assert "The tag also carries the Claude Code marketplace file, which pins v0.19.0" in lead


def test_the_marketplace_pin_is_a_known_limitation_and_an_upgrade_note() -> None:
    """Mutation: delete the limitation, the upgrade note or the alpha limitation."""

    notes = _flat(NOTES)
    limitations = notes[notes.index("## Known limitations") :]
    assert "**The marketplace install runs v0.19.0.**" in limitations
    assert "carries none of the fixes in this release, including the hook fix" in limitations
    steps = notes[notes.index("## What to do after upgrading") : notes.index("## Hermes provider")]
    assert "so the marketplace install runs v0.19.0 code and carries none of these fixes" in steps
    assert "the marketplace install runs v0.19.0 code until then" in _flat("docs/alpha/known-limitations.md")
    assert "so the marketplace install runs v0.19.0 code until then" in _released_changelog()


def test_the_upgrade_steps_cover_the_data_directory_variable() -> None:
    """Mutation: delete step 8."""

    notes = _flat(NOTES)
    steps = notes[notes.index("## What to do after upgrading") : notes.index("## Hermes provider")]
    assert "8. **Hosts that set `ALICE_MEMORY_DATA_DIR`:** give it an absolute path." in steps
    assert "created an empty vault under the directory the host started in" in steps
    assert "`Nothing stored yet.`" in steps


def test_the_metadata_copy_limitation_names_the_debug_pack_and_the_owner() -> None:
    """Mutation: say "the full pack" or that only key-bound agents see the copy."""

    for site, text in (
        ("notes", _flat(NOTES)),
        ("changelog", _released_changelog()),
        ("known-limitations", _flat("docs/alpha/known-limitations.md")),
        ("threat-model", _flat("docs/security/threat-model.md")),
    ):
        assert "`debug: true`" in text, site
        assert "only for key-bound agents" not in text, site
        assert "which the full pack returns" not in text, site
    for site, text in (
        ("notes", _flat(NOTES)),
        ("known-limitations", _flat("docs/alpha/known-limitations.md")),
        ("threat-model", _flat("docs/security/threat-model.md")),
    ):
        assert "keyless owner" in text, site


def test_the_pack_loses_validity_superseded_for_a_hidden_pointer_and_says_so() -> None:
    """Mutation: delete the asymmetry from the notes, the changelog or the limitations."""

    notes = _flat(NOTES)
    assert "also loses `validity.superseded` in the pack, which recall keeps" in notes
    assert "**The pack can lose `validity.superseded` for a hidden pointer.**" in notes
    assert "A pack memory can lose `validity.superseded` along with a pointer it drops." in notes
    assert "also loses `validity.superseded` in the pack, which recall keeps" in _released_changelog()
    assert "the context pack drops `validity.superseded` together with a `superseded_by` pointer" in _flat(
        "docs/alpha/known-limitations.md"
    )


def test_two_scheduled_ci_jobs_hold_issue_write_authority_in_the_notes_and_threat_model() -> None:
    """Mutation: name the weekly canary alone, in either place."""

    for site, text in (("notes", _flat(NOTES)), ("threat-model", _flat("docs/security/threat-model.md"))):
        assert "real-host canary and archive maintenance" in text, site
        assert "weekly CI job" not in text, site
    workflows = REPO_ROOT / ".github" / "workflows"
    assert "issues: write" in (workflows / "real-host-ci.yml").read_text(encoding="utf-8")
    assert "issues: write" in (workflows / "archive-maintenance.yml").read_text(encoding="utf-8")


def test_the_lone_surrogate_turn_is_still_not_saved_and_the_notes_say_so() -> None:
    """Mutation: drop the HTTP 500 sentence from the notes, the changelog or the limitations."""

    notes = _flat(NOTES)
    assert "That turn is still not saved: the server answers a request body that carries one with HTTP 500" in notes
    assert "**A turn that carries a lone surrogate is not saved.**" in notes
    assert "That turn is still not saved: the server answers a request body that carries a lone surrogate with HTTP 500" in (
        _released_changelog()
    )
    assert "so a Hermes turn that carries one is not saved" in _flat("docs/alpha/known-limitations.md")


def test_a_long_query_inside_the_limit_can_still_take_seconds() -> None:
    """Mutation: say the time at the limits was not measured, or drop the large vault."""

    notes = _flat(NOTES)
    assert "3.7 seconds at 499 distinct terms and 0.30 seconds for a two-word query" in notes
    assert "The time at the limits on a large vault was not measured" not in notes
    assert "3.7 seconds at 499 distinct terms, against 0.30 seconds for two words" in _flat("docs/alpha/mcp-tools.md")
    assert "3.7 seconds at 499 distinct terms, against 0.30 seconds for two words" in _flat(
        "docs/alpha/known-limitations.md"
    )


def test_the_hermes_operator_guide_names_the_released_and_the_unreleased_plugin_version() -> None:
    """The guide names 0.5.2 as v0.19.2's version and 0.5.3 as main's.

    Mutations: put 0.5.1 back as the released plugin version in the guide; drop
    the ``Unreleased (on main, not in v0.19.2): `0.5.3``` marker; leave
    ``plugin.yaml`` at 0.5.2 once main's plugin is 0.5.3.
    """

    guide = _flat("docs/integrations/hermes-bridge-operator-guide.md")
    assert "keeps its own `0.5.2` integration-contract version in `plugin.yaml`" in guide
    assert "Unreleased (on main, not in v0.19.2): `0.5.3`." in guide
    plugin = (
        REPO_ROOT / "docs" / "integrations" / "hermes-memory-provider" / "plugins" / "memory" / "alice" / "plugin.yaml"
    ).read_text(encoding="utf-8")
    assert "version: 0.5.3" in plugin


def test_no_tracked_file_names_the_owner_login_used_as_a_test_fixture() -> None:
    """Mutation: put a login-derived misspelling back in the author check test."""

    author_test = (REPO_ROOT / "tests" / "unit" / "test_commit_author_check.py").read_text(encoding="utf-8")
    assert "14844597+example-user@users.noreply.github.com" in author_test


def test_the_retired_v0191_number_is_explained_and_recorded() -> None:
    """v0.19.1 was tagged, never published, and v0.19.2 carries its content.

    The notes explain why the number moved, the changelog has no v0.19.1 section
    (v0.15.0 has none either) and still says v0.19.1 was never published, and
    both copies of the current-state document list the tag next to v0.13.0 and
    v0.15.0 with the reason.

    Mutations, each one alone: delete the "Why 0.19.2 and not 0.19.1" section or
    its "no PyPI artifact and no published GitHub Release" sentence from the
    notes; add a "## v0.19.1" heading to the changelog; drop v0.19.1 from the
    never-published list or its reason in either current-state copy.
    """

    notes = _flat(NOTES)
    assert "## Why 0.19.2 and not 0.19.1 `v0.19.1` was tagged" in notes
    assert "Nothing was published under it: no PyPI artifact and no published GitHub Release." in notes
    assert "The tag stays and the number is retired rather than reused." in notes
    assert "`v0.19.2` carries the same product changes as the `v0.19.1` commit" in notes
    assert notes.count("## Why 0.19.2 and not 0.19.1") == 1

    changelog = (REPO_ROOT / CHANGELOG).read_text(encoding="utf-8")
    assert "\n## v0.19.1" not in changelog
    assert "and v0.19.1 was never published." in _released_changelog()

    for name in ("CURRENT_STATE.md", ".ai/handoff/CURRENT_STATE.md"):
        state = _flat(name)
        assert "Three tags exist that were never published: `v0.13.0`, superseded by `v0.13.1`; `v0.15.0`; and `v0.19.1`, superseded by `v0.19.2`." in state, name
        assert "None has a PyPI artifact or a published GitHub Release." in state, name
        assert (
            "and `v0.19.1`, whose publish run failed at the draft readback because a release script "
            "imported the package in a job that does not install it, so it has no PyPI artifact and "
            "no published GitHub Release."
        ) in state, name
