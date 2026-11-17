"""Pin the v0.24.0 notes to the release records and their checked behaviour."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

import scripts.release_check as release_check

ROOT = Path(__file__).resolve().parents[2]
NOTES = ROOT / "docs/release/v0.24.0-release-notes.md"
REPOSITORY = Path(os.environ.get("BENCH_TEST_SOURCE", ROOT))
BASE = "51f606bad13cce58c89e24c48e225ce2f69a7d90"


def _tag(path: str) -> str:
    return subprocess.check_output(["git", "show", f"v0.23.1:{path}"], cwd=REPOSITORY, text=True)


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
    pytest.param('Checks and install', 'Codex 0.166.0 and later skip such a hooks file, while 0.164.0 loaded the tested inputs; install follows the version it is tested against, 0.169.0.', id="sentence-21"),
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


def test_release_identity_and_pending_state() -> None:
    """Mutations, each alone: change one candidate version site or remove the README candidate line."""
    assert tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"] == "0.24.0"
    assert tomllib.loads(_tag("pyproject.toml"))["project"]["version"] == "0.23.1"
    metadata, errors = release_check.validate_metadata(ROOT)
    assert not errors and metadata.version == "0.24.0"
    assert release_check.validate_release_document_state(ROOT, version=metadata.version, require_finalized=True) == []
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())["plugins"][0]["source"]
    assert market["ref"] == "v0.23.1"
    assert "`v0.24.0` is the current release candidate. It is not published." in (ROOT / "README.md").read_text()


def test_lessons_default_surface_is_new_in_the_candidate(tmp_path: Path) -> None:
    """Mutation: remove the sentence naming alice_lessons on the default MCP surface."""
    import sys

    archive = tmp_path / "prior.zip"
    with archive.open("wb") as output:
        subprocess.run(["git", "archive", "--format=zip", "v0.23.1"], cwd=REPOSITORY, stdout=output, check=True)
    source = "from alicebot_api.mcp.registry import list_mcp_tools; print(','.join(x['name'] for x in list_mcp_tools()))"
    found = []
    for pythonpath in (os.pathsep.join((str(archive) + "/apps/api/src", str(archive))),
                       os.pathsep.join((str(ROOT / "apps/api/src"), str(ROOT)))):
        env = {key: value for key, value in os.environ.items() if not key.startswith("ALICE_")}
        env["PYTHONPATH"] = pythonpath
        output = subprocess.check_output([sys.executable, "-c", source], cwd=tmp_path, env=env, text=True)
        found.append(output.strip().split(","))
    assert "alice_lessons" not in found[0] and "alice_lessons" in found[1]
    assert "The default MCP surface adds `alice_lessons`." in NOTES.read_text()


def test_no_marker_for_the_unreleased_state_is_left_in_the_documents_the_code_or_the_manifest() -> None:
    """Mutation: restore the shared lessons page's unreleased marker."""
    marker = re.compile(r"Unreleased\s*\(\s*on\s+main\s*,\s*not\s+in\s+v0\.23\.1")
    paths = [ROOT / name for name in ("README.md", "SECURITY.md", "CURRENT_STATE.md", "CHANGELOG.md", "ROADMAP.md", "RELEASING.md")]
    for folder in ("docs", "plugins", "agent-skills", "eval"):
        paths.extend((ROOT / folder).rglob("*.md"))
    for folder in ("apps/api/src", "tests", "scripts"):
        paths.extend((ROOT / folder).rglob("*.py"))
    paths.append(ROOT / "scripts/derived_label_mutations.json")
    assert not [str(path.relative_to(ROOT)) for path in paths if marker.search(path.read_text())]
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
    }
    for name, text in retired.items():
        assert text not in (ROOT / name).read_text(), name
    assert "On main," not in (ROOT / "docs/alpha/known-limitations.md").read_text()



def _run_key_probe(tmp_path: Path, source: str) -> list[dict]:
    """Run the same synthetic keyed MCP request on the tagged and candidate sources."""
    import sys

    archive = tmp_path / "prior.zip"
    with archive.open("wb") as output:
        subprocess.run(["git", "archive", "--format=zip", "v0.23.1"], cwd=REPOSITORY, stdout=output, check=True)
    results = []
    for index, pythonpath in enumerate((os.pathsep.join((str(archive) + "/apps/api/src", str(archive))),
                                        os.pathsep.join((str(ROOT / "apps/api/src"), str(ROOT))))):
        env = {key: value for key, value in os.environ.items() if not key.startswith("ALICE_")}
        env.update(PYTHONPATH=pythonpath, ALICE_MCP_FULL_TOOLS="1", ALICE_SOURCE_EMBEDDINGS="off")
        result = subprocess.run(
            [sys.executable, "-c", source, str(tmp_path / f"vault-{index}.db")],
            cwd=tmp_path, env=env, text=True, capture_output=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        results.append(json.loads(result.stdout))
    return results


def test_capture_ceiling_on_the_tag_and_candidate(tmp_path: Path) -> None:
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
    old, now = _run_key_probe(tmp_path, source)
    assert old == {"error": False, "sources": 1}
    assert now == {"error": True, "sources": 0}
    assert "An agent source capture above its sensitivity ceiling is refused without storing the source; v0.23.1 stored it." in NOTES.read_text()


def test_identity_code_on_the_tag_and_candidate(tmp_path: Path) -> None:
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
    old, now = _run_key_probe(tmp_path, source)
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
    """Mutation: move the key paragraph above the numbered list."""
    page = (ROOT / "docs/alpha/projects.md").read_text()
    assert page.index("changes in four ways.") < page.index("1. After the frame line")
    assert page.index("1. After the frame line") < page.index("4. Notes of another project never show.")
    assert page.index("4. Notes of another project never show.") < page.index("When an agent key is presented and a project is found")


def test_project_evidence_names_the_pinned_codex_version() -> None:
    """Mutation: change the project's recorded Codex version to 0.158.0."""
    page = (ROOT / "docs/alpha/projects.md").read_text()
    evidence = (ROOT / "scripts/real_host_evidence.py").read_text()
    assert "Codex 0.169.0 send the folder the session started in as `cwd`" in page
    assert '"codex": "codex-cli 0.169.0"' in evidence


def test_codex_hook_number_guidance_names_the_tested_versions() -> None:
    """Mutation: change the documented first skipping version to 0.167.0."""
    page = (ROOT / "docs/integrations/codex.md").read_text()
    assert "Codex 0.166.0 and later skip such a file; 0.164.0 loaded it." in page
    assert "Install follows the Codex version it is tested against." in page


def test_context_event_query_on_the_tag_and_candidate(tmp_path: Path) -> None:
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
    old, now = _run_key_probe(tmp_path, source)
    assert old == ["x" * 200]
    assert now == ["x" * 128]
    assert "Context-pack events keep a cleaned query of at most 128 characters and withhold credential-shaped text; v0.23.1 stored the full query." in NOTES.read_text()


def test_key_revocation_search_on_the_tag_and_candidate(tmp_path: Path) -> None:
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
    old, now = _run_key_probe(tmp_path, source)
    assert old == 2 and now == 0
    assert "Key revocation searches all keys by id or prefix, including revoked keys for prefix ambiguity; v0.23.1 searched only the 200 newest." in NOTES.read_text()


def test_refused_identity_event_on_the_tag_and_candidate(tmp_path: Path) -> None:
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
    old, now = _run_key_probe(tmp_path, source)
    assert old == 0 and now == 1
    assert "A valid key's refused overclaim records an `agent.key_escalation_rejected` event after rollback, with repeated equivalent claims within 60 seconds sharing one event; v0.23.1 rolled the event back or recorded repeated events from a brief." in NOTES.read_text()


def test_pending_write_retirement_on_the_tag_and_candidate(tmp_path: Path) -> None:
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
    old, now = _run_key_probe(tmp_path, source)
    assert old == {"error": False, "code": None}
    assert now == {"error": True, "code": "not_permitted"}
    assert "Retirement of a pending write requires standing to decide it, while v0.23.1 allowed project and trusted keys to retire another agent's pending write." in NOTES.read_text()


def test_upgrade_and_install_commands() -> None:
    """Mutations, each alone: remove the upgrade command or change one install command."""
    notes = NOTES.read_text()
    assert "`uv tool upgrade alice-memory` or `pip install -U alice-memory`" in notes
    assert "pip install alice-memory==0.24.0 && alice-memory install" in notes
    assert "With uv: `uvx alice-memory install`." in notes
