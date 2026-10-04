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


def test_the_notes_are_published_with_the_exact_title_state_and_a_checksums_record() -> None:
    """Mutations, each one alone: change the title, move the state comment, set either status back to pending, delete the checksums file."""

    lines = _text(NOTES_PATH).splitlines()
    assert lines[0] == "# Alice v0.20.0 Release Notes"
    assert lines[1] == (
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1","version":"0.20.0",'
        '"publication_status":"published","checksums_status":"recorded"} -->'
    )
    assert sum("alice-release-state" in line for line in lines) == 1
    assert (REPO_ROOT / "docs/release/v0.20.0-checksums.txt").is_file()


def test_every_version_site_names_0200_and_the_marketplace_pins_the_published_release() -> None:
    """Mutations, each one alone: set one version site or one plugin pin back to 0.19.2; move the marketplace pin.

    The post-publication change moved the marketplace file to the v0.20.0 tag and the commit
    that tag points at.
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
    assert marketplace["ref"] == "v0.20.0"
    assert marketplace["sha"] == "10034879439f583514cf23f373e998656ff80cac"
    notes = _flat(NOTES_PATH)
    assert "pip install alice-memory==0.20.0 && alice-memory install" in notes
    assert "pip install -U alice-memory==0.20.0" in notes
    assert "uvx alice-memory@0.20.0 --version" in notes


def test_the_changelog_has_the_dated_heading_right_after_the_unreleased_section() -> None:
    """Mutations, each one alone: drop the heading; change its date form; put another section between
    Unreleased and v0.20.0; remove the blank line between the last Unreleased entry and the v0.20.0 heading.

    The Unreleased section was empty on the release commit. It may hold entries now that main has
    moved on, so this test no longer asks for it to be empty. It still asks that the v0.20.0 and
    v0.19.2 sections follow it in that order, one heading each.
    """

    changelog = _text("CHANGELOG.md")
    sections = changelog.split("\n## ")
    assert sections[1].startswith("Unreleased\n")
    assert sections[1].endswith("\n")
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

    The vault held five memories committed under v0.19.2 with a recording endpoint: texts
    of 100, 600, 7,900, 8,800 and 15,000 characters, so embedded texts (title, text and a
    summary of the first 280 characters of the text) of 117, 899, 8,199, 9,099 and 15,299
    characters. The doctor showed 3 after the upgrade and made no request, the first
    reindex sent one batch of 3 texts of 8,000 characters each, and the second sent
    nothing. A cap of 20000 showed 0, and a cap of 12000 sent one text.
    """

    steps = _section(_text(NOTES_PATH), "What to do after upgrading")
    assert "nothing is re-embedded by the upgrade itself" in steps
    assert "texts of 100, 600, 7,900, 8,800 and 15,000 characters, so embedded texts of 117, 899, 8,199, 9,099 and 15,299 characters" in steps
    assert "208, 689" not in steps
    assert "the doctor showed 3 right after the upgrade and made no request" in steps
    assert "one batch of 3 texts of 8,000 characters each, and the second reindex sent nothing" in steps
    assert "`ALICE_EMBEDDINGS_MAX_INPUT_CHARS=20000` the same vault showed 0 and sent nothing" in steps
    assert "with 12000 only the memory whose embedded text was over 12,000 characters was sent, cut to 12,000" in steps
    assert "the cost is at most the cap in characters for each such memory" in steps
    assert "A vault in which no memory's embedded text is over 8,000 characters pays nothing." in steps
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


def _changelog_section() -> str:
    changelog = _text("CHANGELOG.md")
    return changelog.split(HEADING)[1].split("\n## v0.19.2")[0]


def test_the_embedded_text_is_described_with_its_summary_and_no_twenty_thousand_limit() -> None:
    """Mutations, each one alone: write "8,001 to 20,000 characters" or "a commit accepts up to 20,000" back
    into the notes, the changelog, the README or the MCP guide; drop the sentence that gives the summary's size
    or the one that says SQLite has no length limit.

    The embedded text of a memory is its title, its text and its summary, and the summary of a committed
    memory is the first 280 characters of its text, so a text of about 7,700 characters can cross the cap. The
    20,000 limit belongs to the Postgres HTTP commit models only: `alice_memory_commit` on SQLite stored a
    2,000,000 character memory whole, in v0.19.2 and in this release.
    """

    notes = _flat(NOTES_PATH)
    changelog = " ".join(_changelog_section().split())
    readme = _flat("README.md")
    mcp = _flat("docs/integrations/mcp.md")
    for name, text in (("notes", notes), ("changelog", changelog), ("readme", readme), ("mcp guide", mcp)):
        assert "8,001 to 20,000" not in text, name
        assert "a commit accepts up to 20,000" not in text, name
    assert "The summary of a committed memory is the first 280 characters of its text" in notes
    assert "a memory with text of about 7,700 characters or more can be over the default cap" in notes
    assert "the first 280 characters of its text), so a memory whose text is about 7,700 characters or more" in changelog
    assert "a memory whose embedded text (its title, text and summary) is over 8,000 characters is embedded from its first 8,000" in readme
    assert "A memory whose embedded text (its title, text and summary) is over 8,000 characters is embedded from its first 8,000" in mcp
    limitations = _section(_text(NOTES_PATH), "Known limitations")
    assert "`alice_memory_commit` on SQLite has no length limit" in limitations
    assert "The Postgres HTTP commit routes take up to 20,000 characters of text" in limitations
    assert "has no length limit" in _flat("docs/alpha/known-limitations.md")


def test_the_pack_lead_admits_an_empty_pack_and_the_notes_name_the_tool_set() -> None:
    """Mutations, each one alone: drop the 710-token clause from the lead; drop either sentence about
    `ALICE_MCP_FULL_TOOLS=1` from the pack section or from the recall section.
    """

    text = _text(NOTES_PATH)
    lead = " ".join(text.split("## What to do after upgrading")[0].split())
    assert "A committed memory costs about 710 tokens before any of its text" in lead
    assert "the pack can still be empty at the tool's 500-token minimum" in lead
    pack = _section(text, "Context pack at a small budget")
    assert "`alice_context_pack` and `alice_recent_decisions` are listed and callable only with `ALICE_MCP_FULL_TOOLS=1`" in pack
    recall = _section(text, "Recall, the context pack and `alice_resume`")
    assert "`alice_recent_decisions` is listed and callable only with `ALICE_MCP_FULL_TOOLS=1`" in recall


def test_the_changelog_and_the_notes_claim_nothing_that_v0192_never_had_or_did() -> None:
    """Mutations, each one alone, put the old sentence back: "the doctor counted 3"; "capture-file still has no
    limit"; "the reason `has the same id but different content`"; "no longer return such a memory"; "v0.19.2
    answered HTTP 500 and HTTP 422"; "Every outbound call goes through one function"; "9.0 seconds"; "to a file and
    to standard output, and leaves no output file"; "Re-running Hermes or OpenCode `install` keeps".

    Each was found false by running v0.19.2 and this branch side by side. v0.19.2 had no doctor count, so
    "counted" is wrong for it. Every vnext file command takes `--max-file-mib`. Nothing prints the skip reason.
    Expired memories were already absent from recall and the pack. The 500 on the commit route in v0.19.2 was
    not about the body size. The headless alpha check opens URLs with `urlopen`. A strict `opencode.json` kept
    every key install did not write in v0.19.2.
    """

    notes = _flat(NOTES_PATH)
    changelog = " ".join(_changelog_section().split())
    backup = _flat("docs/alpha/backup-and-restore.md")
    limits = _flat("docs/alpha/known-limitations.md")
    opencode = _flat("docs/integrations/opencode.md")
    importers = _flat("docs/integrations/importers.md")

    assert "the doctor counted 3" not in changelog
    assert "reindex sent 3 texts in v0.19.2, which had no doctor count, and the doctor counts 2 and reindex sends 2 now" in changelog
    assert "still has no limit" not in changelog
    assert (
        "`alicebot vnext sources capture-file`, `alicebot vnext connectors browser-clipper capture --file` and "
        "`alicebot vnext agents ingest-output --file` take the same 16 MiB limit and the same option"
    ) in changelog
    for name, text in (("changelog", changelog), ("notes", notes), ("backup guide", backup)):
        assert "same id but different content" not in text, name
    assert "of every packed row together, cuts a branch nested past" in changelog

    assert "no longer return such a memory" not in notes
    assert "Recall and the context pack do not return such a memory, as in v0.19.2" in notes
    assert "no longer return it" not in limits
    assert "while recall and the context pack do not return it, as in v0.19.2" in limits

    assert "where v0.19.2 answered HTTP 500 and HTTP 422" not in notes
    assert "where v0.19.2 has no cap and reads the whole body" in notes

    assert "Every outbound call goes through" not in notes
    assert (
        "Every provider, embeddings, reranker, fact-key, brain, Gmail and Calendar call goes through one function"
    ) in notes
    assert "the reachability probe of `alicebot vnext alpha check --headless`" in notes

    assert "Re-running Hermes or OpenCode `install` keeps" not in notes
    assert "Re-running Hermes or OpenCode `install` keeps" not in changelog
    for text in (notes, changelog):
        assert "or OpenCode `install` on an `opencode.jsonc` or an `opencode.json` that is not strict JSON" in text
        assert "A strict `opencode.json` kept every key that install did not write in both versions" in text
    assert "is refused on this path. A strict `opencode.json` kept every key that install did not write" in opencode

    assert "The process log gets the traceback." not in importers
    assert "the traceback only at debug level" in importers


def test_a_failed_export_to_standard_output_is_not_said_to_leave_nothing() -> None:
    """Mutations, each one alone: write "to a file and to standard output, and leaves no output file" back;
    drop the sentence about standard output from the notes, the changelog or the backup guide.

    `--out` leaves no file. A redirect of standard output holds the records written before the failure and
    no footer, in v0.19.2 and in this release, and `alice-memory import` refuses that file. The limitations page
    states the export failure and links the backup guide, which holds the standard output half, so this test pins
    that half on the guide (the page that explains export) and on the dated records, not on the page.
    """

    notes = _flat(NOTES_PATH)
    changelog = " ".join(_changelog_section().split())
    backup = _flat("docs/alpha/backup-and-restore.md")
    for name, text in (("notes", notes), ("changelog", changelog), ("backup guide", backup)):
        assert "to a file and to standard output, and leaves no output file" not in text, name
        assert "to a file or to standard output, then prints" not in text, name
        assert "To standard output it has already written records by then and stops with no footer" in text, name
        assert "a shell redirect keeps a partial file that import refuses" in text, name
        assert "With `--out` it leaves no output file" in text, name
    limits = _flat("docs/alpha/known-limitations.md")
    assert "to a file and to standard output, and leaves no output file" not in limits
    assert "to a file or to standard output, then prints" not in limits
    assert "and its standard output was partial in the same way" in notes
    assert "Its standard output was partial in the same way." in changelog
    assert "The standard output of v0.19.2 was partial in the same way." in backup
    assert "which `alice-memory import` refused with `import_validation_failed`" in notes
    assert "and writes no file" not in notes
    assert "With `--out` it writes no file, and to standard output it leaves a partial stream that import refuses" in notes
    limitations = _section(_text(NOTES_PATH), "Known limitations")
    assert "An export to standard output that fails has already written part of the stream, with no footer" in limitations


def test_the_recall_timings_are_one_set_and_the_long_query_figure_is_not_a_slowdown() -> None:
    """Mutations, each one alone: change one timing in the notes or in the changelog; put "9.0 seconds" back;
    drop the sentence that says v0.19.2 took the same time.

    The changelog and the notes carry the same measurement, and a long query took the same time in v0.19.2.
    Measured with `scripts/measure_recall_source_lookup.py` (median of seven) and a timing of `alice_recall` at
    499 distinct terms (median of three): 3.9 seconds for terms taken from the vault's words, 8.9 seconds for terms
    that occur nowhere in it, on v0.19.2 and on this branch alike.
    """

    notes = _flat(NOTES_PATH)
    changelog = " ".join(_changelog_section().split())
    for needle in (
        "took 93 ms in v0.19.2 and 76 ms now",
        "`limit` 50 took 226 ms and 86 ms",
        "took 97 ms and 76 ms",
        "`limit` 50 took 893 ms and 143 ms",
        "(median of seven",
    ):
        assert needle in notes, needle
    for needle in (
        "was 93 ms in v0.19.2 and 76 ms here",
        "226 ms and 86 ms with `limit` 50",
        "97 ms and 76 ms for `alice_context_pack`",
        "took 893 ms in v0.19.2 and 143 ms here",
        "v0.18.0 took 69, 79 and 71 ms",
        "the median of seven calls",
    ):
        assert needle in changelog, needle
    assert "9.0 seconds" not in notes
    assert "3.9 seconds at 499 distinct terms taken from the vault's own words, 8.9 seconds at 499 terms that occur nowhere in the vault" in notes
    assert "v0.19.2 took the same 3.9 and 8.9 seconds, and 0.09 seconds for two words, so this release did not change it" in notes


V0192_NOTES_PATH = "docs/release/v0.19.2-release-notes.md"
_UPDATE_2026_10_02 = re.compile(r"^\*\*Update \(2026-10-02\):\*\* ")

# Each v0.19.2 statement that a review finding is not fixed or is open, with text its
# 2026-10-02 update must carry. The update is the next paragraph after the one named.
V0192_ITEMS_FIXED_IN_V0200: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "The internal review of v0.19.0 recorded twelve findings",
        (
            "DB-005, DB-006, DB-008, DB-009, DB-010 and DB-011 are fixed in v0.20.0",
            "`contents: read` only",
            "closes DB-012",
        ),
    ),
    (
        "- **The HTTP API reads a request body before it checks credentials (DB-006).**",
        ("Fixed in v0.20.0 (DB-006)", "HTTP 413", "HTTP 422", "the cap is no rate limit"),
    ),
    (
        "- **A keyless API does not check the Host header (DB-005).**",
        ("Fixed in v0.20.0 (DB-005)", "`ALICEBOT_ALLOWED_HOSTS`", "HTTP 401", "not from a real browser"),
    ),
    (
        "- **Provider calls follow redirects (DB-009).**",
        ("Fixed in v0.20.0 (DB-009)", "follows a redirect", "loopback or private address"),
    ),
    (
        "- **The local-folder scan reads each matching file whole, with no size limit",
        ("Fixed in v0.20.0 (DB-011)", "at most 2 MiB", "`refused_count`"),
    ),
    (
        "- **The local-folder scan can read a file swapped for a link (DB-010).**",
        ("Fixed in v0.20.0 (DB-010)", "without following a link", "still read"),
    ),
    (
        "- **Memory ids in `metadata_json` are not fenced.**",
        ("Fixed in v0.20.0.", "`metadata_json`", "36-character UUID"),
    ),
    (
        "- **The pack can lose `validity.superseded` for a hidden pointer.**",
        ("Fixed in v0.20.0.", "`validity.superseded: true`"),
    ),
    (
        "- **`alice_resume` and `alice_recent_decisions` return `tool_execution_failed`",
        ("Fixed in v0.20.0.", "40,000 UTF-8 bytes", "`invalid_request`"),
    ),
    (
        "- **A column that is itself JSON text too deep for the decoder fails import with a generic error.**",
        ("Fixed in v0.20.0.", "`restore_failed`", "`export_failed`"),
    ),
    (
        "- **A turn that carries a lone surrogate is not saved.**",
        ("Fixed in v0.20.0.", "HTTP 422", "Hermes provider 0.5.3", "`--force`"),
    ),
)


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


def test_the_v0192_notes_say_beside_each_open_item_that_v0200_fixed_it() -> None:
    """Mutations, each one alone: delete one of the eleven updates; change its version; move it away from
    its item; put an update beside the long-query limitation, which v0.20.0 did not change.

    The v0.19.2 notes are published, so each statement that a review finding is not fixed stays as it
    was tagged and a dated update follows it. Every update names v0.20.0 and the limit of the fix.
    """

    blocks = _blocks(V0192_NOTES_PATH)
    for opening, needles in V0192_ITEMS_FIXED_IN_V0200:
        # Bullets with no blank line between them are one block, so the item must be the last
        # bullet of the block it opens or sits in, and the update is the next block.
        positions = [index for index, block in enumerate(blocks) if opening in block]
        assert len(positions) == 1, (opening, positions)
        block = blocks[positions[0]]
        assert " - **" not in block[block.index(opening) + len(opening) :], opening
        update = blocks[positions[0] + 1]
        assert _UPDATE_2026_10_02.match(update), (opening, update[:80])
        for needle in needles:
            assert needle in update, (opening, needle)
        assert "v0.20.0" in update
    long_query = [
        index for index, block in enumerate(blocks) if "- **A long query is slower than a short one.**" in block
    ]
    assert len(long_query) == 1
    # Its bullet runs straight into the next bullet of the same block, so no update sits beside it.
    tail = blocks[long_query[0]].split("- **A long query is slower than a short one.**")[1]
    assert " - **A column that is itself JSON text" in tail
    assert "Update (2026-10-02)" not in tail
    updates = [block for block in blocks if _UPDATE_2026_10_02.match(block)]
    assert len(updates) == len(V0192_ITEMS_FIXED_IN_V0200) + 4, len(updates)


def test_both_notes_say_main_now_pins_v0200_beside_each_marketplace_statement() -> None:
    """Mutations, each one alone: delete one of the seven marketplace updates; name v0.19.2 in one of them.

    The marketplace statements in the two published notes stay as they were tagged. Each one is
    followed by a dated update that says `main` pins the v0.20.0 tag commit and that the copy
    inside the tag does not, so a reader of the notes does not take the tagged sentence for current.
    """

    pin = "v0.20.0 is published, and the marketplace file on `main` now pins the v0.20.0 tag commit, so the marketplace install runs v0.20.0."
    for path, copy_sentence, count in (
        (
            V0192_NOTES_PATH,
            "The copy inside the v0.19.2 tag still pins v0.19.0, so add the marketplace from the repository, not from a checkout of the tag.",
            4,
        ),
        (
            NOTES_PATH,
            "The copy inside the v0.20.0 tag still pins v0.19.2, so add the marketplace from the repository, not from a checkout of the tag.",
            3,
        ),
    ):
        found = [block for block in _blocks(path) if block.startswith(f"**Update (2026-10-02):** {pin}")]
        assert len(found) == count, (path, len(found))
        for block in found:
            assert block == f"**Update (2026-10-02):** {pin} {copy_sentence}", (path, block)
    # In the v0.19.2 notes each one follows either the dated 2026-10-01 update about the v0.19.2
    # pin, or one of the two tagged statements that the marketplace install runs v0.19.0
    # (upgrade step 7 and the plugin packaging limitation).
    blocks = _blocks(V0192_NOTES_PATH)
    for index, block in enumerate(blocks):
        if block.startswith(f"**Update (2026-10-02):** {pin}"):
            before = blocks[index - 1]
            assert (
                "**Update (2026-10-01):** v0.19.2 is published" in before
                or "so the marketplace install runs v0.19.0 code" in before
                or "installs plugin 0.19.0" in before
            ), index
