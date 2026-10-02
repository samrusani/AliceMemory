"""The v0.20.0 release notes carry the upgrade steps, the finding ids and the open limitations.

The notes were claim-checked by running v0.19.2 and this branch side by side, with
local fakes, and each "Checked" sentence in them names what was run. These tests
pin the parts a later edit could quietly weaken: the release identity, the
security wording, the steps an upgrading user must act on, the numbers of the
embeddings upgrade check, and the lists of what is open and what is fixed. Each
test names the mutation that must fail it.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_PATH = "docs/release/v0.20.0-release-notes.md"
HEADING = "## v0.20.0 \u2014 2026-10-02"


def _text(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _flat(path: str) -> str:
    return " ".join(_text(path).split())


def _section(text: str, heading: str) -> str:
    """The body of a ``## `` section of ``text``, whitespace collapsed."""

    match = re.search(rf"^## {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^## |\Z)", text, flags=re.MULTILINE | re.DOTALL)
    assert match is not None, heading
    return " ".join(match.group("body").split())


def test_the_notes_are_a_pending_candidate_with_the_exact_title_and_state() -> None:
    """Mutations, each one alone: change the title, move the state comment, set either status to published or recorded."""

    lines = _text(NOTES_PATH).splitlines()
    assert lines[0] == "# Alice v0.20.0 Release Notes"
    assert lines[1] == (
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1","version":"0.20.0",'
        '"publication_status":"pending","checksums_status":"pending"} -->'
    )
    assert sum("alice-release-state" in line for line in lines) == 1
    assert not (REPO_ROOT / "docs/release/v0.20.0-checksums.txt").exists()


def test_every_version_site_names_0200_and_the_marketplace_still_pins_the_published_release() -> None:
    """Mutations, each one alone: set one version site or one plugin pin back to 0.19.2; move the marketplace pin.

    The marketplace file is the post-publication change's to move, so it still names the
    latest published release and its tag commit.
    """

    pyproject = tomllib.loads(_text("pyproject.toml"))["project"]["version"]
    assert pyproject == "0.20.0"
    assert json.loads(_text("apps/web/package.json"))["version"] == pyproject
    assert json.loads(_text("packaging/mcpb/manifest.json"))["version"] == pyproject
    assert json.loads(_text("plugins/alice-memory/.claude-plugin/plugin.json"))["version"] == pyproject
    for name in ("plugins/alice-memory/.mcp.json", "plugins/alice-memory/hooks/hooks.json"):
        assert f"alice-memory=={pyproject}" in _text(name), name
        assert "alice-memory==0.19.2" not in _text(name), name
    marketplace = json.loads(_text(".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    assert marketplace["ref"] == "v0.19.2"
    notes = _flat(NOTES_PATH)
    assert "pip install alice-memory==0.20.0 && alice-memory install" in notes
    assert "pip install -U alice-memory==0.20.0" in notes
    assert "uvx alice-memory@0.20.0 --version" in notes


def test_the_changelog_has_the_dated_heading_and_an_empty_unreleased_section() -> None:
    """Mutations, each one alone: put an entry under Unreleased; drop the heading; change its date form."""

    changelog = _text("CHANGELOG.md")
    sections = changelog.split("\n## ")
    assert sections[1] == "Unreleased\n"
    assert sections[2].startswith("v0.20.0 \u2014 2026-10-02\n")
    assert sections[3].startswith("v0.19.2 \u2014 2026-10-01\n")
    assert changelog.count(HEADING) == 1
    assert "Unreleased (on main" not in changelog.split(HEADING)[1].split("\n## v0.19.2")[0]


def test_no_marker_for_the_unreleased_state_is_left_in_the_docs() -> None:
    """Mutation: put ``Unreleased (on main, not in v0.19.2):`` back into any document the release describes.

    The only hits for the older markers are the two dated corrections in the published
    v0.18.0 notes, which say ``not in v0.19.0`` and are immutable.
    """

    pattern = re.compile(r"Unreleased\s*\(\s*on\s+main\s*,\s*not\s+in\s+v0\.19\.2|not\s+in\s+v0\.19\.2")
    hits: list[str] = []
    paths = [REPO_ROOT / "README.md", REPO_ROOT / "SECURITY.md", REPO_ROOT / "CURRENT_STATE.md"]
    for root in ("docs", "plugins"):
        paths.extend(path for path in (REPO_ROOT / root).rglob("*.md"))
    for path in paths:
        if pattern.search(path.read_text(encoding="utf-8")):
            hits.append(str(path.relative_to(REPO_ROOT)))
    assert hits == []


def test_the_notes_use_no_dash_and_only_the_allowed_security_wording() -> None:
    """Mutations, each one alone: write a dash into the notes; say "audited" outside the disclosure sentence;
    write independently, third-party or penetration; name an external review tool.
    """

    text = _text(NOTES_PATH)
    assert "\u2014" not in text
    assert "\u2013" not in text
    flat = " ".join(text.split())
    disclosure = "No one outside the project has audited the code."
    assert flat.count(disclosure) == 2
    assert flat.count("audited") == flat.count(disclosure)
    assert "automated security scanning and internal adversarial review, findings triaged and fixed" in flat
    lowered = flat.lower()
    for forbidden in ("independently", "third-party", "third party", "penetration", "pentest", "external review"):
        assert forbidden not in lowered, forbidden
    assert "internal security review of v0.19.0" in flat


def test_the_security_section_names_each_finding_and_what_a_user_sees() -> None:
    """Mutations, each one alone: delete the paragraph of one finding; drop its id; drop the effect sentence."""

    security = _section(_text(NOTES_PATH), "Security")
    for finding in ("DB-005", "DB-006", "DB-008", "DB-009", "DB-010", "DB-011"):
        assert f"- **{finding}, " in security, finding
    assert "DB-012" in security
    assert "HTTP 401 `authentication_failed`" in security
    assert "`ALICEBOT_ALLOWED_HOSTS`" in security
    assert "HTTP 413" in security and "request_too_large" in security
    assert "`json_too_deep`" in security
    assert "follows no redirect" in security
    assert "`refused_count`" in security and "`truncated`" in security
    assert "`canary-alert`" in security and "`archive-alert`" in security
    assert "DB-007 is a documentation fix from v0.19.2 and stays one" in security


def test_the_upgrade_steps_name_every_action_an_existing_user_takes_in_order() -> None:
    """Mutations, each one alone: delete one numbered step; swap two; drop the command or the setting a step names."""

    steps = _section(_text(NOTES_PATH), "What to do after upgrading")
    numbers = [int(number) for number in re.findall(r"(?:^| )(\d{1,2})\. \*\*", steps)]
    assert numbers == list(range(1, 13))
    for needle in (
        "./scripts/install_hermes_alice_memory_provider.py --force",
        "destination already exists",
        "version: 0.5.3",
        "redirects are not followed; set base_url to the final URL",
        "ALICE_EMBEDDINGS_BASE_URL",
        "`ALICEBOT_ALLOWED_HOSTS`",
        "request_body { max_size 4MB }",
        "caddy_request_body_limit_missing",
        "`ALICEBOT_MAX_REQUEST_BODY_BYTES`",
        "`ALICEBOT_MAX_CONNECTOR_SYNC_BODY_BYTES`",
        "`ALICE_EMBEDDINGS_MAX_INPUT_CHARS`",
        "`memories without a current vector`",
        "`--max-file-mib N`",
        "`import_file_too_large`",
        "v0.19.2 answers it with `invalid_request` and exit 2",
        "`refused_count`",
        "the scan reads at most 2 MiB of a file",
        "`invalid_request`",
        "`restore_failed`",
        "`export_failed`",
        "pins the v0.19.2 tag commit",
    ):
        assert needle in steps, needle


def test_the_embeddings_upgrade_check_keeps_its_numbers_and_the_cost_statement() -> None:
    """Mutations, each one alone: change a character count, the doctor count, the batch size or a cap value.

    The vault held five memories embedded under v0.19.2 with embedded text of 208, 689,
    8,293, 9,289 and 15,290 characters. The doctor showed 3 after the upgrade and made no
    request, the first reindex sent one batch of 3 texts of 8,000 characters each, and the
    second sent nothing. A cap of 20000 showed 0, and a cap of 12000 sent one text.
    """

    steps = _section(_text(NOTES_PATH), "What to do after upgrading")
    assert "nothing is re-embedded by the upgrade itself" in steps
    assert "208, 689, 8,293, 9,289 and 15,290 characters" in steps
    assert "the doctor showed 3 right after the upgrade and made no request" in steps
    assert "one batch of 3 texts of 8,000 characters each, and the second reindex sent nothing" in steps
    assert "`ALICE_EMBEDDINGS_MAX_INPUT_CHARS=20000` the same vault showed 0 and sent nothing" in steps
    assert "with 12000 only the memory over 12,000 characters was sent, cut to 12,000" in steps
    assert "the cost is at most the cap in characters for each such memory" in steps
    assert "A vault with no memory over 8,000 characters pays nothing." in steps
    assert "whose vector carries no cut label, which is every vector that v0.19.2 made for such a memory" in steps


def test_the_known_limitations_list_what_is_still_open() -> None:
    """Mutations, each one alone: delete one of the five items the release leaves open, or its reason."""

    limitations = _section(_text(NOTES_PATH), "Known limitations")
    assert (
        "**The session brief, `alice_resume` and `alice_recent_decisions` still show an expired active memory.**"
    ) in limitations
    assert "`alice_memory_manage` with `action: expire` sets `valid_to` and leaves the status `active`" in limitations
    assert (
        "**Consolidation and the roll-up semantic tier can send an expired memory's text to the embeddings endpoint.**"
    ) in limitations
    assert "they do not check `valid_to`" in limitations
    assert "**The roll-up pass reads accepted roll-up cards without checking `valid_to`.**" in limitations
    assert "`list_accepted_rollup_cards`" in limitations
    assert (
        "**Promoting a reviewed artifact into a memory does not embed it.**"
    ) in limitations
    assert "no vector until the next `alice-memory reindex-embeddings`" in limitations
    assert (
        "**The `max_tokens` budget prices the full stored row, and the agent receives a compact row.**"
    ) in limitations
    assert "`token_estimate` 784" in limitations and "`serialized_token_estimate` 443" in limitations
    assert "**A vault that already holds deep JSON text cannot be exported.**" in limitations
    assert "**The marketplace install runs v0.19.2.**" in limitations
    assert "**Confirming a pending write is not a human gate (DB-007).**" in limitations


def test_the_known_limitations_drop_what_this_release_fixes() -> None:
    """Mutation: put a v0.19.2 open item that this release fixes back into the list.

    v0.19.2 listed each of these as open. They are fixed here, so they appear in the
    notes only as what changed.
    """

    limitations = _section(_text(NOTES_PATH), "Known limitations")
    for fixed in (
        "**The HTTP API reads a request body before it checks credentials (DB-006).**",
        "**A keyless API does not check the Host header (DB-005).**",
        "**Provider calls follow redirects (DB-009).**",
        "**The local-folder scan reads each matching file whole, with no size limit (DB-011).**",
        "**The local-folder scan can read a file swapped for a link (DB-010).**",
        "**Memory ids in `metadata_json` are not fenced.**",
        "**The pack can lose `validity.superseded` for a hidden pointer.**",
        "**A column that is itself JSON text too deep for the decoder fails import with a generic error.**",
        "**A turn that carries a lone surrogate is not saved.**",
        "**The marketplace install runs v0.19.0.**",
    ):
        assert fixed not in limitations, fixed
    assert "return `tool_execution_failed` for a query of about 50,000 bytes or more" not in limitations
    assert "The oversized query on `alice_recall`, `alice_context_pack`, `alice_resume` and `alice_recent_decisions`" in (
        limitations
    )


def test_the_notes_describe_the_behaviour_changes_an_integrator_will_notice() -> None:
    """Mutations, each one alone: delete the bullet of one group, or a status code or error code from it."""

    changes = _section(_text(NOTES_PATH), "Behaviour changes an integrator will notice")
    for needle in (
        "HTTP 413 with `request_too_large`",
        "type `json_too_deep`",
        "HTTP 401 `authentication_failed`",
        "`invalid_request`",
        "`token_report.cut_item_count`",
        "`failed_ids`",
        "`import_file_too_large`",
        "`restore_failed`",
        "`export_failed`",
        "`refused_count` and `truncated`",
        "`ALICEBOT_ALLOWED_HOSTS`",
        "`list_memories_missing_embeddings` requires `statuses`",
    ):
        assert needle in changes, needle
    schema = _section(_text(NOTES_PATH), "Schema and migration")
    assert schema.startswith("There is no schema change: no new Alembic revision")
    assert "No dependency pin in `pyproject.toml` moves." in schema
