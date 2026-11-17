"""Pin the v0.24.0 notes to the release records and their checked behaviour."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
import tomllib
from io import BytesIO
from pathlib import Path

import pytest

import scripts.release_check as release_check

ROOT = Path(__file__).resolve().parents[2]
NOTES_PATH = "docs/release/v0.24.0-release-notes.md"
CHECKSUMS_PATH = "docs/release/v0.24.0-checksums.txt"
NOTES = ROOT / NOTES_PATH
RELEASE_TAG = "v0.24.0"
PRIOR_TAG = "v0.23.1"
TAG_COMMIT = "b2f280e4740c02f803783e932a4711080dc39585"
PRIOR_TAG_COMMIT = "2443e7cb6d8f89b4e625cec7adcd7b4a8dcd7276"
CANDIDATE_LINE = "`v0.24.0` is the current release candidate. It is not published."
REPOSITORY = Path(os.environ.get("BENCH_TEST_SOURCE", ROOT))
BASE = "51f606bad13cce58c89e24c48e225ce2f69a7d90"


def _tagged(path: str, tag: str = RELEASE_TAG) -> str:
    return subprocess.check_output(["git", "show", f"{tag}:{path}"], cwd=REPOSITORY, text=True)


@pytest.fixture(scope="module")
def release_trees(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    trees = []
    for tag in (PRIOR_TAG, RELEASE_TAG):
        target = tmp_path_factory.mktemp(tag)
        export = subprocess.check_output(["git", "archive", tag], cwd=REPOSITORY)
        with tarfile.open(fileobj=BytesIO(export)) as archive:
            archive.extractall(target, filter="data")
        trees.append(target)
    return trees[0], trees[1]


def _tag_python(source: Path, cwd: Path, code: str, *args: str, **settings: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("ALICE_")}
    env.update(PYTHONPATH=os.pathsep.join((str(source / "apps/api/src"), str(source))),
               PYTHONDONTWRITEBYTECODE="1", **settings)
    result = subprocess.run(
        [sys.executable, "-c", code, *args], cwd=cwd, env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _flat(path: str) -> str:
    return " ".join(_text(path).split())


# Every parameter is a sentence of the notes; removing that sentence is its guard edit.
CLAIMS = [
    pytest.param('Lead', 'Agents sharing a vault can now record structured lessons and find published lessons relevant to their work.', id="sentence-01"),
    pytest.param('Lead', 'A lesson written by an agent without publishing standing waits for approval before it is shared.', id="sentence-02"),
    pytest.param('Lead', 'This release also tightens agent key checks, repairs two shipped smokes and updates the installer.', id="sentence-03"),
    pytest.param('Lead', 'See "What to do after upgrading".', id="sentence-04"),
    pytest.param('What to do after upgrading', 'A claim beyond the key is refused.', id="sentence-05"),
    pytest.param('What to do after upgrading', 'The Claude Code marketplace on `main` pins v0.23.1 until the post-publication change moves it.', id="sentence-06"),
    pytest.param('What to do after upgrading', 'A marketplace install therefore runs v0.23.1 until then.', id="sentence-07"),
    pytest.param('Lessons', '`alice_memory_commit` and `POST /v0/vnext/memories/commit` accept an optional structured `lesson` with `applies_when`, `what_happened`, `do_instead` and optional repository-relative `paths`. v0.23.1 did not have this block.', id="sentence-08"),
    pytest.param('Lessons', 'An owner or an `admin_agent` can publish a lesson at once; other writers need approval through `alice_memory_correct`.', id="sentence-09"),
    pytest.param('Lessons', 'The instruction-shaped hard floor still applies, including to the owner.', id="sentence-10"),
    pytest.param('Lessons', '`alice_lessons` returns matching published lessons the caller may read, is on the default MCP surface, and can include the writer and publishing act.', id="sentence-11"),
    pytest.param('Lessons', 'The session brief can name the number of readable published lessons.', id="sentence-12"),
    pytest.param('Keys and identity', 'Keyed calls read flat and nested identity claims together and keep the stricter profile and scope. v0.23.1 ignored flat claims beside a nested identity.', id="sentence-13"),
    pytest.param('Keys and identity', "MCP now returns `identity_claim_refused` for a keyed caller's overclaim or blank scope item; v0.23.1 returned `not_permitted`.", id="sentence-14"),
    pytest.param('Reads', 'SQLite ranked reads for a key with read limits use statistics from readable ordinary memories or source chunks, while derived memories remain eligible. v0.23.1 used table-wide counts.', id="sentence-15"),
    pytest.param('Reads', 'Owner, unbound admin and keyless declared identity reads keep their existing ordering.', id="sentence-16"),
    pytest.param('Checks and install', 'The agent integration smoke gives its seed the requested project, and capture-to-brief uses the current report date.', id="sentence-17"),
    pytest.param('Checks and install', 'On a fresh user, the first failed from v0.9.2 through v0.23.1 because its seed lacked a project; the second failed from v0.10.3 through v0.23.1 on days other than 2026-05-11 because its input requested that fixed date.', id="sentence-18"),
    pytest.param('Checks and install', "These were faults in the checks' own inputs, so `alicebot vnext alpha check` could report failure on a healthy installation.", id="sentence-19"),
    pytest.param('Checks and install', 'Codex install refuses an `mcp_tool` input containing a whole number from 2^63 through 2^64-1, including nested values; v0.23.1 accepted it.', id="sentence-20"),
    pytest.param('Checks and install', 'Codex 0.166.0, 0.167.0 and 0.169.0 skipped the tested hooks files, while 0.164.0 loaded them; install follows the version it is tested against, 0.169.0.', id="sentence-21"),
    pytest.param('Checks and install', 'The showcase walkthrough runs as a plain command from a checkout, and the scripted demo records the June price as the step replaced by the November correction.', id="sentence-22"),
    pytest.param('Checks and install', "The README is rewritten around long-term memory with permissions for a company's teams of agents.", id="sentence-23"),
    pytest.param('Checks and install', 'The chunk-vector build avoids the extra matrix-sized peak seen at the first append on the measured Linux runner.', id="sentence-24"),
    pytest.param('Records and tooling', 'The [limitations page](../alpha/known-limitations.md) adds thirteen bullets about current limits.', id="sentence-25"),
    pytest.param('Records and tooling', 'The baseline scanner is pinned to 1.9.4.', id="sentence-26"),
    pytest.param('Records and tooling', 'Mutation replay rejects archives ignored by an enclosing Git repository, defaults `BENCH_TEST_SOURCE` to the source repository when unset, and removes stale mutant logs before a baseline.', id="sentence-27"),
    pytest.param('Records and tooling', 'The unit suite runs in eleven shards, and the publish and rehearsal limits are 300 minutes.', id="sentence-28"),
    pytest.param('Behaviour changes an integrator will notice', 'The default MCP surface adds `alice_lessons`.', id="sentence-29"),
    pytest.param('Behaviour changes an integrator will notice', 'A keyed identity overclaim returns `identity_claim_refused` rather than `not_permitted`; omit the identity fields or narrow the claim to the key and retry.', id="sentence-30"),
    pytest.param('Behaviour changes an integrator will notice', 'A lesson write that awaits approval answers `review_required` with reason `lesson_requires_approval` for a writer without publishing standing.', id="sentence-31"),
    pytest.param('Security', 'This release was checked with automated security scanning and internal adversarial review, findings triaged and fixed.', id="sentence-32"),
    pytest.param('Security', 'No one outside the project has audited the code.', id="sentence-33"),
    pytest.param('Security', "A valid key's refused overclaim records an `agent.key_escalation_rejected` event after rollback, with repeated equivalent claims within 60 seconds sharing one event; v0.23.1 rolled the event back or recorded repeated events from a brief.", id="sentence-34"),
    pytest.param('Security', "Retirement of a pending write requires standing to decide it, while v0.23.1 allowed project and trusted keys to retire another agent's pending write.", id="sentence-35"),
    pytest.param('Security', 'Retirement or restoration of a lesson requires publishing standing.', id="sentence-36"),
    pytest.param('Security', 'An agent source capture above its sensitivity ceiling is refused without storing the source; v0.23.1 stored it.', id="sentence-37"),
    pytest.param('Security', 'Key revocation searches all keys by id or prefix, including revoked keys for prefix ambiguity; v0.23.1 searched only the 200 newest.', id="sentence-38"),
    pytest.param('Security', 'Context-pack events keep a cleaned query of at most 128 characters and withhold credential-shaped text; v0.23.1 stored the full query.', id="sentence-39"),
    pytest.param('Security', 'No security advisory is planned for this release.', id="sentence-40"),
    pytest.param('What this release does not claim', 'Lessons written before the first key keep their standing after keys are issued.', id="sentence-41"),
    pytest.param('What this release does not claim', 'Every key of a project can read its waiting and rejected lessons through the full review and explain tools, and explain returns the decision reason.', id="sentence-42"),
    pytest.param('What this release does not claim', "A lesson's reader sees the writer and publishing agent ids, so choose neutral ids.", id="sentence-43"),
    pytest.param('What this release does not claim', '`alice_lessons` reads a projection of every lesson the caller may read, so its cost grows with that number.', id="sentence-44"),
    pytest.param('What this release does not claim', 'See the [known limitations](../alpha/known-limitations.md) for these and the other limits of the alpha.', id="sentence-45"),
]


@pytest.mark.parametrize(("section", "sentence"), CLAIMS)
def test_each_release_sentence_is_held(section: str, sentence: str) -> None:
    """Mutation: remove the sentence named by this parameter from the notes."""
    notes = NOTES.read_text()
    body = notes.split("\n## ", 1)[0] if section == "Lead" else notes.split("\n## " + section + "\n", 1)[1].split("\n## ", 1)[0]
    assert sentence in body


def test_the_changelog_moves_all_twenty_two_entries_without_changing_their_words() -> None:
    """Mutation: change one word of the released entry about capture ceilings."""
    main = subprocess.check_output(["git", "show", f"{BASE}:CHANGELOG.md"], cwd=REPOSITORY, text=True)
    old = main.split("\n## Unreleased\n", 1)[1].split("\n## ", 1)[0]
    now = (ROOT / "CHANGELOG.md").read_text().split("\n## v0.24.0 ", 1)[1].split("\n## ", 1)[0].split("\n", 1)[1]
    assert len(re.findall(r"(?m)^- ", old)) == 22
    released = old.replace("Unreleased (on " + "main, not in v0.23.1): ", "")
    released = re.sub(r"(?m)^- ([a-z])", lambda match: "- " + match.group(1).upper(), released)
    assert released.strip() == now.strip()


def test_the_notes_are_published_with_the_exact_title_state_and_a_checksums_record() -> None:
    """Mutation: set publication_status back to pending in the notes' second line."""
    lines = NOTES.read_text().splitlines()
    assert lines[0] == "# Alice v0.24.0 Release Notes"
    assert lines[1] == (
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1","version":"0.24.0",'
        '"publication_status":"published","checksums_status":"recorded"} -->'
    )
    assert sum("alice-release-state" in line for line in lines) == 1
    assert (ROOT / CHECKSUMS_PATH).is_file()


def test_the_tag_has_the_six_version_sites_and_the_finalized_release_documents(release_trees: tuple[Path, Path]) -> None:
    """Mutation: change the expected tagged version from 0.24.0 to 0.24.1."""
    _, released = release_trees
    assert tomllib.loads(_tagged("pyproject.toml", PRIOR_TAG))["project"]["version"] == "0.23.1"
    metadata, errors = release_check.validate_metadata(released)
    assert not errors and metadata.version == "0.24.0"
    assert release_check.validate_release_document_state(released, version=metadata.version, require_finalized=True) == []


def test_the_checksums_file_records_the_two_published_digests_in_the_format_of_the_last_release() -> None:
    """Mutation: change one hex digit in the wheel digest in the checksums record."""
    prior = _tagged("docs/release/v0.23.1-checksums.txt").splitlines(keepends=True)
    current = _text(CHECKSUMS_PATH).splitlines(keepends=True)
    assert len(prior) == len(current) == 19
    assert [line.replace("0.23.1", "0.24.0") for line in prior[:17]] == current[:17]
    assert current[17:] == [
        "c277821aa021d09301e97aaca643605c75fedaa566247ea3a3b8931380313572  alice_memory-0.24.0-py3-none-any.whl\n",
        "b24d88a9e8bd6ca56aa923d0d63fa0251b79c9e1ddf6e40a72f3d95c11f5af84  alice_memory-0.24.0.tar.gz\n",
    ]


def test_the_marketplace_pins_the_release_tag_and_the_tag_keeps_the_prior_pin() -> None:
    """Mutation: set the working marketplace ref back to v0.23.1."""
    marketplace_path = ".claude-plugin/marketplace.json"
    marketplace = json.loads(_text(marketplace_path))["plugins"][0]["source"]
    assert (marketplace["ref"], marketplace["sha"]) == (RELEASE_TAG, TAG_COMMIT)
    for tag, commit in ((RELEASE_TAG, TAG_COMMIT), (PRIOR_TAG, PRIOR_TAG_COMMIT)):
        actual = subprocess.check_output(["git", "rev-parse", f"{tag}^{{commit}}"], cwd=REPOSITORY, text=True)
        assert actual.strip() == commit
    inside = json.loads(_tagged(marketplace_path))["plugins"][0]["source"]
    assert (inside["ref"], inside["sha"]) == (PRIOR_TAG, PRIOR_TAG_COMMIT)


def test_the_notes_say_beside_upgrade_step_three_that_main_now_pins_v0240() -> None:
    """Mutation: remove the dated blockquote immediately after upgrade step 3."""
    update = (
        "   > **Update (2026-11-17):** v0.24.0 is published, and the marketplace file on `main` now pins the v0.24.0 tag "
        "commit, so the marketplace install runs v0.24.0. The copy inside the v0.24.0 tag still pins v0.23.1, so add "
        "the marketplace from the repository, not from a checkout of the tag."
    )
    notes = NOTES.read_text()
    upgrade = notes.split("\n## What to do after upgrading\n", 1)[1].split("\n## ", 1)[0]
    step = (
        "3. The Claude Code marketplace on `main` pins v0.23.1 until the post-publication change moves it. "
        "A marketplace install therefore runs v0.23.1 until then."
    )
    assert f"{step}\n\n{update}\n\nThis release needs no database migration:" in upgrade
    assert notes.count("**Update (2026-11-17):**") == 1


def test_lessons_default_surface_is_new_in_v0240(tmp_path: Path, release_trees: tuple[Path, Path]) -> None:
    """Mutation: remove the sentence naming alice_lessons on the default MCP surface."""
    code = "from alicebot_api.mcp.registry import list_mcp_tools; print(','.join(x['name'] for x in list_mcp_tools()))"
    prior, released = (_tag_python(tree, tmp_path, code).strip().split(",") for tree in release_trees)
    assert "alice_lessons" not in prior and "alice_lessons" in released
    assert "The default MCP surface adds `alice_lessons`." in NOTES.read_text()


def test_no_marker_for_the_unreleased_state_is_left_in_the_documents_the_code_or_the_manifest(release_trees: tuple[Path, Path]) -> None:
    """Mutation: change the last assertion to expect an on-main marker in the tagged limitations."""
    _, released = release_trees
    marker = re.compile(r"Unreleased\s*\(\s*on\s+main\s*,\s*not\s+in\s+v0\.23\.1")
    paths = [released / name for name in ("README.md", "SECURITY.md", "CURRENT_STATE.md", "CHANGELOG.md", "ROADMAP.md", "RELEASING.md", "PRODUCT_BRIEF.md", "ARCHITECTURE.md")]
    for folder in ("docs", "plugins", "agent-skills", "eval"):
        paths.extend((released / folder).rglob("*.md"))
    for folder in ("apps/api/src", "tests", "scripts"):
        paths.extend((released / folder).rglob("*.py"))
    paths.append(released / "scripts/derived_label_mutations.json")
    assert not [str(path.relative_to(released)) for path in paths if marker.search(path.read_text())]
    retired = {
        "docs/alpha/shared-lessons.md": "are on main and not in v0.23.1",
        "docs/alpha/agent-integration.md": "lessons on main, which are not in v0.23.1",
        "docs/alpha/mcp-tools.md": "thirteen core tools on main",
        "docs/integrations/mcp.md": "On main it also advertises `alice_lessons`",
        "docs/memory-operations-protocol.md": "On main, a keyed caller whose identity fields",
        "docs/overview.md": "On main, `alice_lessons` is also a core tool",
        "docs/showcase/walkthrough.md": "From this change the plain command works",
        "CURRENT_STATE.md": "On main, the full tool surface holds thirteen core tools",
        "README.md": "The README and documentation describe main, where `alice_lessons`",
        "PRODUCT_BRIEF.md": "On main, `ALICE_MCP_FULL_TOOLS=1` exposes all thirteen core tools.",
        "ARCHITECTURE.md": "On main, `ALICE_MCP_FULL_TOOLS=1` exposes all thirteen core tools.",
    }
    for name, text in retired.items():
        assert text not in " ".join((released / name).read_text().split()), name
    assert "On main," not in (released / "docs/alpha/known-limitations.md").read_text()


def _run_key_probe(tmp_path: Path, source: str, release_trees: tuple[Path, Path]) -> list:
    """Run the existing synthetic keyed requests against the two published exports."""
    return [
        json.loads(_tag_python(
            tree, tmp_path, source, str(tmp_path / f"vault-{index}.db"),
            ALICE_MCP_FULL_TOOLS="1", ALICE_SOURCE_EMBEDDINGS="off",
        ))
        for index, tree in enumerate(release_trees)
    ]


def test_capture_ceiling_at_the_two_release_tags(tmp_path: Path, release_trees: tuple[Path, Path]) -> None:
    """Mutation: remove the Security sentence about above-ceiling source capture."""
    source = '''
import json, os, sys
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.mcp_server import MCPServer
from alicebot_api.mcp.types import MCPRuntimeContext
p, user = Path(sys.argv[1]), uuid4()
bootstrap_database(p, user_id=user, user_email="fixture@example.invalid")
with sqlite_user_connection(p, user) as conn:
    _, token = create_agent_key(SQLiteVNextStore(conn, user), user_id=user, agent_id="writer", permission_profile="project_scoped_agent", project_scope="alpha")
os.environ["ALICE_AGENT_API_KEY"] = token
server = MCPServer(context=MCPRuntimeContext(database_url=sqlite_url_for_path(p), user_id=user), input_stream=BytesIO(), output_stream=BytesIO())
answer = server._handle_request({"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{"name":"alice_capture", "arguments":{"raw_text":"Synthetic source text.", "domain":"project", "sensitivity":"regulated"}}})["result"]
with sqlite_user_connection(p, user) as conn:
    count = conn.execute("SELECT count(*) AS total FROM sources").fetchone()["total"]
print(json.dumps({"error":answer["isError"], "sources":count}))
'''
    old, now = _run_key_probe(tmp_path, source, release_trees)
    assert old == {"error": False, "sources": 1}
    assert now == {"error": True, "sources": 0}
    assert "An agent source capture above its sensitivity ceiling is refused without storing the source; v0.23.1 stored it." in NOTES.read_text()


def test_identity_code_at_the_two_release_tags(tmp_path: Path, release_trees: tuple[Path, Path]) -> None:
    """Mutation: remove the integrator sentence about the identity refusal code."""
    source = '''
import json, os, sys
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.mcp_server import MCPServer
from alicebot_api.mcp.types import MCPRuntimeContext
p, user = Path(sys.argv[1]), uuid4()
bootstrap_database(p, user_id=user, user_email="fixture@example.invalid")
with sqlite_user_connection(p, user) as conn:
    _, token = create_agent_key(SQLiteVNextStore(conn, user), user_id=user, agent_id="writer", permission_profile="project_scoped_agent", project_scope="alpha")
os.environ["ALICE_AGENT_API_KEY"] = token
server = MCPServer(context=MCPRuntimeContext(database_url=sqlite_url_for_path(p), user_id=user), input_stream=BytesIO(), output_stream=BytesIO())
answer = server._handle_request({"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{"name":"alice_recall", "arguments":{"query":"fixture", "agent_id":"another"}}})["result"]
print(json.dumps(json.loads(answer["content"][0]["text"])["error"]["code"]))
'''
    old, now = _run_key_probe(tmp_path, source, release_trees)
    assert (old, now) == ("not_permitted", "identity_claim_refused")
    assert "A keyed identity overclaim returns `identity_claim_refused` rather than `not_permitted`; omit the identity fields or narrow the claim to the key and retry." in NOTES.read_text()


def test_release_sections_and_security_opening() -> None:
    """Mutation: rename the Lessons section or remove the opening Security sentence."""
    text = NOTES.read_text()
    assert re.findall(r"(?m)^## (.+)$", text) == [
        "What to do after upgrading", "Lessons", "Keys and identity", "Reads", "Checks and install",
        "Records and tooling", "Behaviour changes an integrator will notice", "Security",
        "What this release does not claim", "Install",
    ]
    security = text.split("\n## Security\n", 1)[1].split("\n## ", 1)[0].strip()
    assert security.startswith(
        "This release was checked with automated security scanning and internal adversarial review, findings triaged and fixed. "
        "No one outside the project has audited the code."
    )
    assert "\u2014" not in text and "\u2013" not in text


def test_project_key_note_follows_the_four_item_list() -> None:
    """Mutation: reverse the comparison that places the key paragraph after item four."""
    page = _tagged("docs/alpha/projects.md")
    assert page.index("changes in four ways.") < page.index("1. After the frame line")
    assert page.index("1. After the frame line") < page.index("4. Notes of another project never show.")
    assert page.index("4. Notes of another project never show.") < page.index("When an agent key is presented and a project is found")


def test_project_evidence_names_the_pinned_codex_version() -> None:
    """Mutation: change the expected host version in the project evidence to 0.158.0."""
    page = _tagged("docs/alpha/projects.md")
    evidence = _tagged("scripts/real_host_evidence.py")
    assert "Codex 0.169.0 send the folder the session started in as `cwd`" in page
    assert '"codex": "codex-cli 0.169.0"' in evidence


def test_codex_hook_number_guidance_names_the_tested_versions() -> None:
    """Mutation: remove Codex 0.166.0 from the expected skipping versions."""
    page = _tagged("docs/integrations/codex.md")
    assert "Codex 0.166.0, 0.167.0 and 0.169.0 skipped the tested files; 0.164.0 loaded them." in page
    assert "Install follows the Codex version it is tested against." in page


def test_context_event_query_at_the_two_release_tags(tmp_path: Path, release_trees: tuple[Path, Path]) -> None:
    """Mutation: remove the Security sentence about context-pack event queries."""
    source = '''
import json, os, sys
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.mcp_server import MCPServer
from alicebot_api.mcp.types import MCPRuntimeContext
p, user = Path(sys.argv[1]), uuid4()
bootstrap_database(p, user_id=user, user_email="fixture@example.invalid")
with sqlite_user_connection(p, user) as conn:
    _, token = create_agent_key(SQLiteVNextStore(conn, user), user_id=user, agent_id="reader", permission_profile="read_only_agent")
os.environ["ALICE_AGENT_API_KEY"] = token
server = MCPServer(context=MCPRuntimeContext(database_url=sqlite_url_for_path(p), user_id=user), input_stream=BytesIO(), output_stream=BytesIO())
answer = server._handle_request({"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{"name":"alice_context_pack", "arguments":{"query":"x" * 200}}})["result"]
assert not answer["isError"]
with sqlite_user_connection(p, user) as conn:
    events = SQLiteVNextStore(conn, user).list_events()
queries = [e["payload_json"]["query"] for e in events if e["event_type"] == "retrieval.context_pack_compiled"]
print(json.dumps(queries))
'''
    old, now = _run_key_probe(tmp_path, source, release_trees)
    assert old == ["x" * 200]
    assert now == ["x" * 128]
    assert "Context-pack events keep a cleaned query of at most 128 characters and withhold credential-shaped text; v0.23.1 stored the full query." in NOTES.read_text()


def test_key_revocation_search_at_the_two_release_tags(tmp_path: Path, release_trees: tuple[Path, Path]) -> None:
    """Mutation: remove the Security sentence about the key revocation search."""
    source = '''
import contextlib, io, json, sys
from pathlib import Path
from uuid import uuid4
from alicebot_api.onramp import bootstrap_database, main
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
p, user = Path(sys.argv[1]), uuid4()
bootstrap_database(p, user_id=user, user_email="fixture@example.invalid")
with sqlite_user_connection(p, user) as conn:
    store = SQLiteVNextStore(conn, user)
    oldest, _ = create_agent_key(store, user_id=user, agent_id="old", permission_profile="read_only_agent")
    for n in range(205):
        create_agent_key(store, user_id=user, agent_id=f"new-{n}", permission_profile="read_only_agent")
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    code = main(["keys", "revoke", str(oldest["id"]), "--db", str(p), "--user-id", str(user)])
print(json.dumps(code))
'''
    old, now = _run_key_probe(tmp_path, source, release_trees)
    assert old == 2 and now == 0
    assert "Key revocation searches all keys by id or prefix, including revoked keys for prefix ambiguity; v0.23.1 searched only the 200 newest." in NOTES.read_text()


def test_refused_identity_event_at_the_two_release_tags(tmp_path: Path, release_trees: tuple[Path, Path]) -> None:
    """Mutation: remove the Security sentence about the event for a refused claim."""
    source = '''
import json, os, sys
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.mcp_server import MCPServer
from alicebot_api.mcp.types import MCPRuntimeContext
p, user = Path(sys.argv[1]), uuid4()
bootstrap_database(p, user_id=user, user_email="fixture@example.invalid")
with sqlite_user_connection(p, user) as conn:
    _, token = create_agent_key(SQLiteVNextStore(conn, user), user_id=user, agent_id="reader", permission_profile="read_only_agent")
os.environ["ALICE_AGENT_API_KEY"] = token
server = MCPServer(context=MCPRuntimeContext(database_url=sqlite_url_for_path(p), user_id=user), input_stream=BytesIO(), output_stream=BytesIO())
answer = server._handle_request({"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{"name":"alice_recall", "arguments":{"query":"fixture", "agent_id":"other"}}})["result"]
assert answer["isError"]
with sqlite_user_connection(p, user) as conn:
    events = SQLiteVNextStore(conn, user).list_events()
print(json.dumps(sum(e["event_type"] == "agent.key_escalation_rejected" for e in events)))
'''
    old, now = _run_key_probe(tmp_path, source, release_trees)
    assert old == 0 and now == 1
    assert "A valid key's refused overclaim records an `agent.key_escalation_rejected` event after rollback, with repeated equivalent claims within 60 seconds sharing one event; v0.23.1 rolled the event back or recorded repeated events from a brief." in NOTES.read_text()


def test_pending_write_retirement_at_the_two_release_tags(tmp_path: Path, release_trees: tuple[Path, Path]) -> None:
    """Mutation: remove the Security sentence about retirement standing."""
    source = '''
import json, os, sys
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.mcp_server import MCPServer
from alicebot_api.mcp.types import MCPRuntimeContext
p, user = Path(sys.argv[1]), uuid4()
bootstrap_database(p, user_id=user, user_email="fixture@example.invalid")
with sqlite_user_connection(p, user) as conn:
    store = SQLiteVNextStore(conn, user)
    _, author = create_agent_key(store, user_id=user, agent_id="author", permission_profile="project_scoped_agent", project_scope="alpha")
    _, stranger = create_agent_key(store, user_id=user, agent_id="stranger", permission_profile="project_scoped_agent", project_scope="alpha")
server = MCPServer(context=MCPRuntimeContext(database_url=sqlite_url_for_path(p), user_id=user), input_stream=BytesIO(), output_stream=BytesIO())
def call(token, name, arguments):
    os.environ["ALICE_AGENT_API_KEY"] = token
    result = server._handle_request({"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{"name":name, "arguments":arguments}})["result"]
    return result["isError"], json.loads(result["content"][0]["text"])
failed, created = call(author, "alice_memory_commit", {"title":"Fixture", "canonical_text":"An ordinary procedure.", "domain":"project", "sensitivity":"internal", "project_scope":["alpha"], "confidence":0.7})
assert not failed and created["status"] == "confirmation_required"
failed, result = call(stranger, "alice_memory_manage", {"action":"forget", "memory_id":created["memory"]["id"], "reason":"Retire it"})
print(json.dumps({"error":failed, "code":result.get("error", {}).get("code")}))
'''
    old, now = _run_key_probe(tmp_path, source, release_trees)
    assert old == {"error": False, "code": None}
    assert now == {"error": True, "code": "not_permitted"}
    assert "Retirement of a pending write requires standing to decide it, while v0.23.1 allowed project and trusted keys to retire another agent's pending write." in NOTES.read_text()


def test_upgrade_and_install_commands() -> None:
    """Mutations, each alone: remove the upgrade command or change one install command."""
    notes = NOTES.read_text()
    assert "`uv tool upgrade alice-memory` or `pip install -U alice-memory`" in notes
    assert "pip install alice-memory==0.24.0 && alice-memory install" in notes
    assert "With uv: `uvx alice-memory install`." in notes


def test_no_database_migration_is_needed(tmp_path: Path, release_trees: tuple[Path, Path]) -> None:
    """Mutation: remove the notes sentence saying the stored schema and migration files stayed the same."""
    prefix = "apps/api/alembic/versions"

    def revisions(tree: Path) -> dict[str, bytes]:
        return {path.relative_to(tree).as_posix(): path.read_bytes() for path in (tree / prefix).rglob("*") if path.is_file()}

    prior, released = release_trees
    assert revisions(prior) == revisions(released)

    probe = '''
import json, sqlite3, sys
from pathlib import Path
from uuid import UUID
from alicebot_api.onramp import bootstrap_database
path = Path(sys.argv[1])
bootstrap_database(path, user_id=UUID("00000000-0000-4000-8000-000000000001"), user_email="fixture@example.invalid")
with sqlite3.connect(path) as conn:
    schema = conn.execute("SELECT type, name, tbl_name, sql FROM main.sqlite_master ORDER BY type, name").fetchall()
print(json.dumps(schema))
'''
    prior_schema = _tag_python(prior, tmp_path, probe, str(tmp_path / "prior.db"))
    released_schema = _tag_python(released, tmp_path, probe, str(tmp_path / "released.db"))
    assert json.loads(prior_schema) == json.loads(released_schema)
    section = NOTES.read_text().split("\n## What to do after upgrading\n", 1)[1].split("\n## ", 1)[0]
    assert "This release needs no database migration: the PostgreSQL migration files and the stored schema of the SQLite vault are those of v0.23.1." in section



def test_the_control_documents_name_v0240_as_the_latest_published_release_and_v0231_as_the_prior_one() -> None:
    """Mutation: restore the v0.24.0 candidate line in the README."""
    for name in ("ARCHITECTURE.md", "CURRENT_STATE.md", "PRODUCT_BRIEF.md", "README.md",
                 "RELEASING.md", "ROADMAP.md", "docs/vnext/README.md"):
        text = _flat(name)
        assert CANDIDATE_LINE not in text, name
        assert "`v0.24.0` is the latest published release" in text, name
        assert "`v0.23.1` is the latest published release" not in text, name
    for name in ("ARCHITECTURE.md", "CURRENT_STATE.md", "PRODUCT_BRIEF.md", "README.md", "ROADMAP.md"):
        assert "`v0.23.1` is the immediately prior published release" in _flat(name), name
    for name in ("ARCHITECTURE.md", "CURRENT_STATE.md", "PRODUCT_BRIEF.md", "ROADMAP.md"):
        assert CHECKSUMS_PATH in _text(name), name
    assert "artifact digests are in `docs/release/v0.23.1-checksums.txt`." in _flat("ARCHITECTURE.md")
    assert f"- [v0.24.0 release notes]({NOTES_PATH})" in _text("README.md")
    assert "[latest published release notes](../release/v0.24.0-release-notes.md)" in _flat("docs/vnext/README.md")
    assert "[v0.24.0 release notes](release/v0.24.0-release-notes.md)" in _text("docs/overview.md")
    assert f"`{NOTES_PATH}`, with artifact digests in `{CHECKSUMS_PATH}`." in _flat("ROADMAP.md")
    state = _text("CURRENT_STATE.md")
    assert f"- `{NOTES_PATH}`\n- `{CHECKSUMS_PATH}`\n" in state
    assert "`v0.23.1` is the immediately prior published release; its records are" in state
    assert "What `v0.24.0` Targets" not in state
    latest = "## What `v0.24.0` Shipped"
    previous = "## What `v0.23.1` Shipped"
    assert state.index(latest) < state.index(previous)
    assert f"[release notes]({NOTES_PATH})" in state.split(latest, 1)[1].split(previous, 1)[0]
    assert f"{previous}\n\n`v0.23.1` is the immediately prior published release." in state
    for earlier in ("v0.23.0", "v0.22.0", "v0.21.1", "v0.21.0"):
        assert f"## What `{earlier}` Shipped\n\n`{earlier}` is an earlier published release." in state
    history = " ".join(state.split())
    assert "pins the plugin to the `v0.24.0` tag commit, so the plugin installs from that marketplace." in history
    assert "`v0.24.0` tag has it still pinned to the `v0.23.1` tag commit." in history
    assert "`v0.23.1` tag has it still pinned to the `v0.23.0` tag commit," in history


def test_the_install_and_checkout_lines_name_v0240_as_the_latest_release() -> None:
    """Mutation: set the headless install guide's --tag back to v0.23.1."""
    guide = _flat("docs/alpha/headless-ubuntu-install.md")
    assert "use the latest published release tag (`v0.24.0`) for an immutable deployment." in guide
    assert "with the latest published release tag (`v0.24.0`): `--tag v0.24.0`." in guide
    assert "v0.23.1" not in guide
    for name in ("README.md", "docs/alpha/quickstart.md", "docs/overview.md"):
        page = _flat(name)
        assert "To run v0.24.0, run `git checkout v0.24.0` before `make setup`." in page, name
        assert "git checkout v0.23.1" not in page, name
    assert "on top of the latest published `v0.24.0` baseline." in _flat("docs/integrations/reference-paths.md")
