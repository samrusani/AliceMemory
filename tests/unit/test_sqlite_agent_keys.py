"""Agent keys on the SQLite vault: minting, listing, revoking, and the
keyless refusal once a key exists.

Guards, each killed by a mutation case in the manifest:
- the keyless refusal in `_agent_identity_from_arguments` (a vault with an
  active key refuses every core tool call without `ALICE_AGENT_API_KEY`,
  reads included; the existing not-permitted answer);
- the refusal counts active keys only (a revoked key is not one);
- the brief runs as the key's agent once keys exist, prints nothing keyless,
  and is unchanged on a keyless vault;
- `keys list` never carries a hash or a raw key;
- the audit event never carries a raw key;
- `keys create` keeps the project binding;
- install writes the key's environment reference, never a raw key.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
USER_ID = "00000000-0000-0000-0000-000000000001"

ACME_NOTE = "# Acme sprint\n\nThe deployment cadence for acme is weekly, every Thursday afternoon.\n"
OTHER_NOTE = "# Other project\n\nThe cadence for the other project is daily, every morning.\n"
SECRET_KEY = ""


def _vault(tmp_path: Path) -> Path:
    from alicebot_api.onramp import bootstrap_database, resolve_db_path

    database = resolve_db_path(data_dir=str(tmp_path / "vault"), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return database


def _context(database: Path):
    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext
    from alicebot_api.onramp import sqlite_url_for_path

    os.environ.pop(AGENT_API_KEY_ENV, None)
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _keys(args: list[str], *, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{REPO_ROOT}/apps/api/src:{REPO_ROOT}"
    env.pop("ALICE_AGENT_API_KEY", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "alicebot_api.onramp", *args],
        capture_output=True, text=True, env=env,
    )


def _create_key(database: Path, *, agent_id: str, profile: str, project: str | None = None, label: str | None = None) -> str:
    args = [
        "keys", "create", "--agent-id", agent_id, "--profile", profile,
        "--db", str(database), "--user-id", USER_ID,
    ]
    if project:
        args += ["--project", project]
    if label:
        args += ["--label", label]
    proc = _keys(args)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()[0]


def _call(context, name: str, arguments: dict, *, key: str | None = None):
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV

    if key is None:
        os.environ.pop(AGENT_API_KEY_ENV, None)
    else:
        os.environ[AGENT_API_KEY_ENV] = key
    try:
        return call_mcp_tool(context, name=name, arguments=arguments)
    finally:
        os.environ.pop(AGENT_API_KEY_ENV, None)


def _capture_source(context, *, title: str, text: str, project: str | None = None, sensitivity: str = "private", key: str | None = None) -> str:
    from alicebot_api.mcp.registry import call_mcp_tool

    arguments = {"raw_text": text, "title": title, "domain": "project", "sensitivity": sensitivity}
    if project:
        arguments["project_scope"] = [project]
    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV

    if key:
        os.environ[AGENT_API_KEY_ENV] = key
    try:
        answer = call_mcp_tool(context, name="alice_capture", arguments=arguments)
    finally:
        os.environ.pop(AGENT_API_KEY_ENV, None)
    return str(answer["source_id"])


def _prefixes(database: Path) -> list[str]:
    proc = _keys(["keys", "list", "--db", str(database), "--user-id", USER_ID, "--json"])
    return [str(item["key_prefix"]) for item in json.loads(proc.stdout)["items"]]


def _json_answer(answer: object) -> str:
    return json.dumps(answer, ensure_ascii=True, sort_keys=True, default=str)


# --- The keyless refusal ------------------------------------------------------


def test_a_keyless_call_is_refused_once_a_key_exists(tmp_path: Path) -> None:
    """Guard: the active-key count in `_agent_identity_from_arguments`. With
    one active key on the vault, a keyless MCP call is refused for a read and
    for a write, with the existing not-permitted answer."""

    database = _vault(tmp_path)
    context = _context(database)
    _create_key(database, agent_id="reader", profile="read_only_agent")
    for name, arguments in (
        ("alice_recall", {"query": "cadence"}),
        ("alice_capture", {"raw_text": "the commit gate notes one fact", "title": "Note", "domain": "project"}),
    ):
        from alicebot_api.mcp.types import MCPNotPermittedError

        with pytest.raises(MCPNotPermittedError) as excinfo:
            _call(context, name, arguments)
        assert "alice-memory keys create" in str(excinfo.value)


def test_a_keyless_vault_behaves_as_before(tmp_path: Path) -> None:
    """With no key on the vault the keyless call reads with the payload
    identity, exactly as v0.22.0 did."""

    database = _vault(tmp_path)
    context = _context(database)
    source_id = _capture_source(context, title="Acme", text=ACME_NOTE, project="acme")
    answer = _call(context, "alice_recall", {"query": "deployment cadence", "include_sources": True})
    assert answer["count"] >= 1 or answer["source_count"] >= 1
    # The captured source is really there for the owner's standing.
    assert str(source_id) in _json_answer(answer)


def test_only_a_revoked_key_present_means_keyless(tmp_path: Path) -> None:
    """Guard: the count counts ACTIVE keys only; a revoked key is not one, so
    revoking the last active key returns the vault to the keyless behaviour."""

    from alicebot_api.mcp.types import MCPNotPermittedError

    database = _vault(tmp_path)
    context = _context(database)
    raw = _create_key(database, agent_id="reader", profile="read_only_agent")
    prefix = raw[:12]
    with pytest.raises(MCPNotPermittedError):
        _call(context, "alice_recall", {"query": "cadence"})
    proc = _keys(["keys", "revoke", prefix, "--db", str(database), "--user-id", USER_ID])
    assert proc.returncode == 0, proc.stderr
    # Keyless again: the refusal lifts by itself.
    _capture_source(context, title="Acme", text=ACME_NOTE, project="acme")
    answer = _call(context, "alice_recall", {"query": "deployment cadence"})
    assert answer["count"] >= 1 or answer["source_count"] >= 1


def test_a_bad_key_answers_as_today(tmp_path: Path) -> None:
    from alicebot_api.mcp.types import MCPNotPermittedError

    database = _vault(tmp_path)
    context = _context(database)
    _create_key(database, agent_id="reader", profile="read_only_agent")
    with pytest.raises(MCPNotPermittedError):
        _call(context, "alice_recall", {"query": "cadence"}, key="alice_sk_notarealkey000000000000000000000")


def test_a_revoked_key_is_refused_on_its_next_call(tmp_path: Path) -> None:
    from alicebot_api.mcp.types import MCPNotPermittedError

    database = _vault(tmp_path)
    context = _context(database)
    other = _create_key(database, agent_id="keeper", profile="read_only_agent")
    raw = _create_key(database, agent_id="reader", profile="read_only_agent")
    prefix = raw[:12]
    proc = _keys(["keys", "revoke", prefix, "--db", str(database), "--user-id", USER_ID])
    assert proc.returncode == 0
    with pytest.raises(MCPNotPermittedError):
        _call(context, "alice_recall", {"query": "cadence"}, key=raw)
    # The other active key still works.
    os.environ["ALICE_AGENT_API_KEY"] = other
    try:
        answer = _call(context, "alice_recall", {"query": "cadence"}, key=other)
        assert answer["count"] >= 0
    finally:
        os.environ.pop("ALICE_AGENT_API_KEY", None)


# --- The keys commands --------------------------------------------------------


def test_keys_list_shows_never_a_hash_or_a_raw_key(tmp_path: Path) -> None:
    """Guard: `_agent_key_public_record` in the onramp. The list carries the
    prefix, agent id, profile, label, project, created, last used and revoked
    state; never a hash and never a raw key, in JSON and in the human lines."""

    database = _vault(tmp_path)
    raw = _create_key(database, agent_id="reader", profile="read_only_agent", project="acme", label="Reader")
    proc = _keys(["keys", "list", "--db", str(database), "--user-id", USER_ID, "--json"])
    payload = json.loads(proc.stdout)
    assert payload["count"] == 1
    item = payload["items"][0]
    for field in ("key_prefix", "agent_id", "permission_profile", "project_scope", "label", "created_at", "last_used_at", "revoked_at", "revoked"):
        assert field in item, field
    text = _json_answer(payload)
    assert "key_hash" not in text, "the list carried a hash"
    assert raw not in text, "the list carried the raw key"
    human = _keys(["keys", "list", "--db", str(database), "--user-id", USER_ID]).stdout
    assert raw not in human and "key_hash" not in human


def test_the_raw_key_appears_only_in_the_create_output(tmp_path: Path) -> None:
    """Guard: the raw key never reaches the audit trail, the list, the export
    or a second command's output. A full run's outputs are grepped for it."""

    database = _vault(tmp_path)
    context = _context(database)
    _capture_source(context, title="Acme", text=ACME_NOTE, project="acme")
    raw = _create_key(database, agent_id="reader", profile="read_only_agent")
    list_out = _keys(["keys", "list", "--db", str(database), "--user-id", USER_ID, "--json"]).stdout
    export_out = _keys(["export", "--db", str(database), "--user-id", USER_ID, "--out", str(tmp_path / "export.jsonl")]).stdout
    with sqlite3.connect(database) as conn:
        events = json.dumps(
            [list(row) for row in conn.execute("SELECT * FROM event_log")], default=str
        )
    import hashlib as _hashlib

    raw_hash = _hashlib.sha256(raw.encode("utf-8")).hexdigest()
    for name, blob in (("list", list_out), ("export file", (tmp_path / "export.jsonl").read_text()), ("export stdout", export_out), ("audit trail", events)):
        assert raw not in blob, f"the raw key appeared in {name}"
        assert raw_hash not in blob, f"the raw key's hash appeared in {name}"


def test_the_command_binds_the_project(tmp_path: Path) -> None:
    """Guard: `keys create --project` keeps the binding (dropped at create, a
    bound key would read like an unbound one)."""

    database = _vault(tmp_path)
    _create_key(database, agent_id="scoped", profile="project_scoped_agent", project="acme")
    payload = json.loads(_keys(["keys", "list", "--db", str(database), "--user-id", USER_ID, "--json"]).stdout)
    assert payload["items"][0]["project_scope"] == "acme"


def test_the_command_refuses_what_the_postgres_command_refuses(tmp_path: Path) -> None:
    proc = _keys(["keys", "create", "--agent-id", "x", "--profile", "nope", "--db", str(tmp_path / "v"), "--user-id", USER_ID])
    assert proc.returncode == 2
    blank = _keys(["keys", "create", "--agent-id", "x", "--profile", "read_only_agent", "--project", "   ", "--db", str(tmp_path / "v"), "--user-id", USER_ID])
    assert blank.returncode == 2
    assert "project_scope must not be blank" in blank.stderr


def test_install_writes_never_a_raw_key(tmp_path: Path) -> None:
    """Guard: install never writes a raw key into any file, and no flag takes
    the key itself on the command line. The host's key comes from its launch
    environment, which the owner sets by hand (install writes no key at
    all); the docs say so per host."""

    home = tmp_path / "home"
    proc = _keys(["install", "--host", "hermes", "--home", str(home)])
    assert proc.returncode == 0, proc.stderr
    config = (home / ".hermes" / "config.yaml").read_text()
    assert "alice_sk_" not in config
    assert "ALICE_AGENT_API_KEY" not in config
    # No flag takes the key itself on the command line.
    refused = _keys(["install", "--host", "hermes", "--agent-key", "alice_sk_whatever", "--home", str(home)])
    assert refused.returncode != 0


# --- The brief ----------------------------------------------------------------


def _brief(database: Path, *, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    args = ["brief", "--db", str(database), "--user-id", USER_ID]
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{REPO_ROOT}/apps/api/src:{REPO_ROOT}"
    env.pop("ALICE_AGENT_API_KEY", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, "-m", "alicebot_api.onramp", *args], capture_output=True, text=True, env=env)


def test_the_keyless_brief_prints_nothing_once_keys_exist(tmp_path: Path) -> None:
    """Guard: the brief's active-key rule. Once the vault holds an active key
    a brief without `ALICE_AGENT_API_KEY` prints nothing and exits 0 (a host
    hook never fails a session); without keys it prints as today."""

    database = _vault(tmp_path)
    context = _context(database)
    _capture_source(context, title="Acme", text=ACME_NOTE, project="acme")
    before = _brief(database)
    assert before.returncode == 0 and before.stdout.strip(), before.stdout
    _create_key(database, agent_id="reader", profile="read_only_agent")
    silent = _brief(database)
    assert silent.returncode == 0, silent.stderr
    assert silent.stdout.strip() == "", silent.stdout[:200]


def test_a_limited_keys_brief_shows_only_what_it_may_read(tmp_path: Path) -> None:
    """Guard: the keyed brief compiles under the key's own fence: a
    project-scoped key's brief carries the bound project's memory and not the
    other project's, where the owner's brief carries both."""

    from alicebot_api.mcp_tools import _sqlite_path_from_url
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_label_writes import without_insert_floor

    database = _vault(tmp_path)
    context = _context(database)
    acme = _capture_source(context, title="Acme", text=ACME_NOTE, project="acme")
    other = _capture_source(context, title="Other", text=OTHER_NOTE, project="other")
    with sqlite_user_connection(_sqlite_path_from_url(context.database_url), USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        with without_insert_floor():
            store.create_memory({
                "memory_key": "acme-note", "canonical_text": "alpha beta acme cadence note", "title": "Acme note",
                "memory_type": "decision", "status": "active", "domain": "project", "sensitivity": "public",
                "metadata_json": {"source_id": acme},
            })
            store.create_memory({
                "memory_key": "other-note", "canonical_text": "alpha beta other cadence note", "title": "Other note",
                "memory_type": "decision", "status": "active", "domain": "project", "sensitivity": "public",
                "metadata_json": {"source_id": other},
            })
    key = _create_key(database, agent_id="scoped", profile="project_scoped_agent", project="acme")
    brief = _brief(database, env_extra={"ALICE_AGENT_API_KEY": key})
    assert brief.returncode == 0, brief.stderr
    assert "other project" not in brief.stdout.lower(), brief.stdout[:500]
    assert "acme sprint" in brief.stdout.lower(), brief.stdout[:500]


def test_a_keyless_vaults_brief_is_unchanged(tmp_path: Path) -> None:
    database = _vault(tmp_path)
    context = _context(database)
    _capture_source(context, title="Acme", text=ACME_NOTE, project="acme")
    brief = _brief(database)
    assert brief.returncode == 0 and brief.stdout.strip()
    assert "acme" in brief.stdout.lower()


def test_a_bad_key_on_the_brief_prints_nothing(tmp_path: Path) -> None:
    database = _vault(tmp_path)
    context = _context(database)
    _capture_source(context, title="Acme", text=ACME_NOTE, project="acme")
    _create_key(database, agent_id="reader", profile="read_only_agent")
    brief = _brief(database, env_extra={"ALICE_AGENT_API_KEY": "alice_sk_notarealkey000000000000000000000"})
    assert brief.returncode == 0
    assert brief.stdout.strip() == ""


# --- The fence checks ---------------------------------------------------------


def _seed_fence_vault(context) -> None:
    for index, sensitivity in enumerate(("public", "internal", "private", "unknown")):
        _capture_source(context, title=f"Acme {sensitivity}", text=ACME_NOTE + f" Note {index}.", project="acme", sensitivity=sensitivity)
    _capture_source(context, title="Other", text=OTHER_NOTE, project="other")


def _normalise(value):
    volatile = {"retrieval_run_id", "trace_id", "run_id", "duration_ms", "elapsed_ms", "latency_ms", "last_used_at"}
    if isinstance(value, dict):
        return {k: _normalise(child) for k, child in value.items() if k not in volatile}
    if isinstance(value, list):
        return [_normalise(child) for child in value]
    return value


def test_a_bound_admin_key_reads_within_its_binding(tmp_path: Path) -> None:
    """Guard: the fence with a bound admin_agent key, against the matching
    read: recall and commit are the binding's reads, and the outside project
    stays out."""

    from alicebot_api.mcp.types import MCPNotPermittedError

    database = _vault(tmp_path)
    context = _context(database)
    _seed_fence_vault(context)
    key = _create_key(database, agent_id="bound-admin", profile="admin_agent", project="acme")
    answer = _call(context, "alice_recall", {"query": "cadence deployment", "include_sources": True}, key=key)
    titles = json.dumps(answer, default=str)
    # The positive control: the binding's own sources are returned.
    assert "Acme" in titles and answer["source_count"] >= 1, titles[:300]
    assert "Other project" not in titles, titles[:400]
    with pytest.raises(MCPNotPermittedError):
        _call(context, "alice_capture", {"raw_text": "outside note text", "title": "Outside", "domain": "project", "sensitivity": "public", "project_scope": ["other"]}, key=key)


def test_profile_limits_hold_on_sqlite(tmp_path: Path) -> None:
    """Guard: each profile's limits hold when the key was minted by the new
    command: a read_only_agent key cannot commit, and a memory_proposal_agent
    key does not see the memory whose input sits in the restricted health
    domain, while the plain memory is returned."""

    from alicebot_api.mcp.types import MCPNotPermittedError
    from alicebot_api.vnext_label_writes import without_insert_floor
    from alicebot_api.mcp_tools import _sqlite_path_from_url
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection

    database = _vault(tmp_path)
    context = _context(database)
    from alicebot_api.mcp_tools import _sqlite_path_from_url as _sp
    from alicebot_api.sqlite_store import SQLiteVNextStore as _S, sqlite_user_connection as _c
    _capture_source(context, title="Plain parent", text="The plain note records the cadence.", project="acme", sensitivity="public")
    with _c(_sp(context.database_url), USER_ID) as _conn:
        health_parent = str(_S(_conn, USER_ID).create_source({
            "source_type": "note", "title": "Health parent", "content_hash": "health-parent-1",
            "domain": "health", "sensitivity": "public", "metadata_json": {},
        })["id"])
    with sqlite_user_connection(_sqlite_path_from_url(context.database_url), USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        with without_insert_floor():
            for key, text in (("plain", "alpha beta plain note"), ("health", "alpha beta checkup note")):
                parent = health_parent if key == "health" else None
                metadata = {"source_id": parent} if parent else {}
                store.create_memory({
                    "memory_key": f"note-{key}", "canonical_text": text, "title": key,
                    "memory_type": "decision", "status": "active",
                    "domain": "health" if key == "health" else "project",
                    "sensitivity": "public", "metadata_json": metadata,
                })
    reader = _create_key(database, agent_id="reader", profile="read_only_agent")
    proposal = _create_key(database, agent_id="proposer", profile="memory_proposal_agent")
    with pytest.raises(MCPNotPermittedError):
        _call(context, "alice_capture", {"raw_text": "a write the reader cannot make", "title": "W", "domain": "project"}, key=reader)
    answer = _call(context, "alice_recall", {"query": "alpha beta"}, key=proposal)
    texts = _json_answer(answer)
    assert "checkup note" not in texts, texts[:400]
    assert "plain note" in texts, texts[:400]


def test_hidden_equals_missing_on_the_bytes_at_every_status(tmp_path: Path) -> None:
    """Guard: for a key with limits, the bytes with an unreadable source
    present equal the bytes with it deleted, at every status: a source the
    key may not read (outside its binding) is captured at each of the four
    statuses, hidden, and compared against its own deletion."""

    database = _vault(tmp_path)
    context = _context(database)
    _capture_source(context, title="Acme", text=ACME_NOTE, project="acme")
    key = _create_key(database, agent_id="scoped", profile="project_scoped_agent", project="acme")
    owner_equal = _create_key(database, agent_id="owner-equal", profile="admin_agent")
    for status in ("public", "internal", "private", "unknown"):
        _capture_source(context, title=f"Other {status}", text=OTHER_NOTE + " Note for the status.", project="other", sensitivity=status, key=owner_equal)
        hidden = _json_answer(_normalise(_call(
            context, "alice_recall", {"query": "cadence deployment", "include_sources": True}, key=key)))
        with sqlite3.connect(database) as conn:
            conn.execute("DELETE FROM sources WHERE title = ?", (f"Other {status}",))
            conn.commit()
        missing = _json_answer(_normalise(_call(
            context, "alice_recall", {"query": "cadence deployment", "include_sources": True}, key=key)))
        assert hidden == missing, (status, hidden[:200], missing[:200])


def test_passage_and_vectors_modes_for_a_limited_key_stay_as_today(tmp_path: Path, monkeypatch) -> None:
    """Guard: the scoped vectors stay scoped in vectors mode for a key with
    limits, and the passage mode stays the passage mode."""

    monkeypatch.setenv("ALICE_SEARCH_QUALITY", "vectors")
    database = _vault(tmp_path)
    context = _context(database)
    _seed_fence_vault(context)
    key = _create_key(database, agent_id="scoped", profile="project_scoped_agent", project="acme")
    vectors_answer = _call(context, "alice_recall", {"query": "cadence deployment", "include_sources": True}, key=key)
    assert "Other project" not in _json_answer(vectors_answer)
    monkeypatch.setenv("ALICE_SEARCH_QUALITY", "off")
    document_answer = _call(context, "alice_recall", {"query": "cadence deployment", "include_sources": True}, key=key)
    assert "Other project" not in _json_answer(document_answer)


# --- Concurrent writers --------------------------------------------------------


def test_four_writers_keep_every_row(tmp_path: Path) -> None:
    """Guard: several agents at once. Four writer processes, each with its own
    key, commit and capture concurrently through the MCP surface; nothing is
    lost and no locked-database error reaches a caller (SQLite's connect
    timeout is the busy timeout: 5 seconds)."""

    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV

    database = _vault(tmp_path)
    context = _context(database)
    keys = [_create_key(database, agent_id=f"writer-{n}", profile="trusted_local_agent") for n in range(4)]
    per_writer = 6
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{REPO_ROOT}/apps/api/src:{REPO_ROOT}"
    env["ALICE_DATABASE"] = str(database)
    worker = tmp_path / "writer.py"
    worker.write_text(
            "\n".join([
                "import json, os, sys",
                "from alicebot_api.mcp.registry import call_mcp_tool",
                "from alicebot_api.mcp.types import MCPRuntimeContext",
                "from alicebot_api.mcp_tools import AGENT_API_KEY_ENV",
                "from alicebot_api.onramp import sqlite_url_for_path",
                "database, key, count, tag = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]",
                "os.environ[AGENT_API_KEY_ENV] = key",
                "from pathlib import Path",
                "context = MCPRuntimeContext(database_url=sqlite_url_for_path(Path(database)), user_id=sys.argv[5])",
                "for index in range(count):",
                "    answer = call_mcp_tool(context, name='alice_capture', arguments={",
                "        'raw_text': f'# {tag}-{index}\\n\\nThe {tag} note {index} records the cadence for the {tag} archive.',",
                "        'title': f'{tag}-{index}', 'domain': 'project', 'sensitivity': 'public',",
                "    })",
                "    done = call_mcp_tool(context, name='alice_memory_commit', arguments={",
                "        'title': f'{tag} commit {index}', 'canonical_text': f'alpha beta {tag} committed note {index}',",
                "        'memory_type': 'decision', 'domain': 'project', 'sensitivity': 'public', 'confidence': 0.99,",
                "    })",
                "    assert not done.get('is_error'), done",
                "    print(json.dumps({'source_id': str(answer.get('source_id')), 'memory_id': str(done.get('memory', {}).get('id'))}))",
            ]) + "\n",
            encoding="utf-8",
        )
    procs = []
    for number, key in enumerate(keys):
        procs.append(subprocess.Popen(
            [sys.executable, str(worker), str(database), key, str(per_writer), f"w{number}", USER_ID],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ))
    outcomes = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=300)
        outcomes.append((proc.returncode, stdout, stderr))
    for returncode, stdout, stderr in outcomes:
        if returncode != 0 or "committed note" not in str(stdout):
            import sys as _s
            print("WORKER DBG rc", returncode, "out:", stdout[:300], "err:", stderr[-300:], file=_s.stderr)
        assert returncode == 0, stderr[-600:]
        assert "database is locked" not in stderr, stderr[-300:]
        assert len(stdout.strip().splitlines()) == per_writer
    with sqlite3.connect(database) as conn:
        stored = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
        committed = conn.execute("SELECT count(*) FROM memories WHERE canonical_text LIKE '%committed note%'").fetchone()[0]
        if committed != 4 * per_writer:
            import sys as _s
            print("MEM DBG:", conn.execute("SELECT count(*) FROM memories").fetchone()[0],
                  conn.execute("SELECT substr(canonical_text, 1, 40) FROM memories LIMIT 5").fetchall(), file=_s.stderr)
    assert stored == 4 * per_writer, stored
    assert committed == 4 * per_writer, committed


# --- The legacy tools and the hook (round-two) ---------------------------------


def test_a_legacy_tool_is_refused_keyless_once_a_key_exists(tmp_path: Path, monkeypatch) -> None:
    """Guard: the legacy refusal in `call_mcp_tool` (the keyless refusal covers
    every MCP call, the legacy surfaces included, reads and writes)."""

    from alicebot_api.mcp.types import MCPNotPermittedError

    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    database = _vault(tmp_path)
    context = _context(database)
    _capture_source(context, title="Acme", text=ACME_NOTE, project="acme")
    _create_key(database, agent_id="reader", profile="read_only_agent")
    for tool, arguments in (
        ("alice_open_loop_extract", {"person_name": "someone"}),
        ("alice_graph_neighborhood", {"target_id": str(__import__("uuid").uuid4())}),
    ):
        with pytest.raises(MCPNotPermittedError):
            _call(context, tool, arguments)


def test_the_session_start_hook_prints_nothing_keyless_once_keys_exist(tmp_path: Path, monkeypatch) -> None:
    """Guard: the hook's active-key rule, the same helper the brief command
    runs. On a vault that holds an active key, a keyless hook prints nothing
    and exits 0; with a limited key it shows only what that key may read."""

    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV

    database = _vault(tmp_path)
    context = _context(database)
    acme = _capture_source(context, title="Acme", text=ACME_NOTE, project="acme")
    other = _capture_source(context, title="Other", text=OTHER_NOTE, project="other")
    from alicebot_api.mcp_tools import _sqlite_path_from_url
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_label_writes import without_insert_floor
    with sqlite_user_connection(_sqlite_path_from_url(context.database_url), USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        with without_insert_floor():
            store.create_memory({
                "memory_key": "acme-hook", "canonical_text": "alpha beta acme cadence note", "title": "Acme note",
                "memory_type": "decision", "status": "active", "domain": "project", "sensitivity": "public",
                "metadata_json": {"source_id": acme},
            })
            store.create_memory({
                "memory_key": "other-hook", "canonical_text": "alpha beta other cadence note", "title": "Other note",
                "memory_type": "decision", "status": "active", "domain": "project", "sensitivity": "public",
                "metadata_json": {"source_id": other},
            })
    def hook(env_extra: dict[str, str] | None = None):
        env = dict(os.environ)
        env["PYTHONPATH"] = f"{REPO_ROOT}/apps/api/src:{REPO_ROOT}"
        env.pop(AGENT_API_KEY_ENV, None)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, "-m", "alicebot_api.session_start_hook",
             "--format", "markdown", "--data-dir", str(database.parent)],
            capture_output=True, text=True, env=env,
        )

    before_keys = hook()
    assert before_keys.returncode == 0 and before_keys.stdout.strip()
    key = _create_key(database, agent_id="scoped", profile="project_scoped_agent", project="acme")
    silent = hook()
    assert silent.returncode == 0, silent.stderr
    assert silent.stdout.strip() == "", silent.stdout[:200]
    keyed = hook({"ALICE_AGENT_API_KEY": key})
    assert keyed.returncode == 0, keyed.stderr
    assert "other project" not in keyed.stdout.lower(), keyed.stdout[:400]
    assert "acme sprint" in keyed.stdout.lower(), keyed.stdout[:400]


def test_an_unreadable_vault_refuses_the_keyless_call(tmp_path: Path, monkeypatch) -> None:
    """Guard: the count's error branch. A vault that cannot be read (a corrupt
    file) refuses the keyless call instead of answering it with the owner's
    standing."""

    from alicebot_api.mcp.types import MCPNotPermittedError

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"this is not a database file" * 100)
    context = _context(corrupt)
    with pytest.raises(MCPNotPermittedError):
        _call(context, "alice_recall", {"query": "cadence"})


def test_the_key_count_check_survives_odd_folder_names(tmp_path: Path, monkeypatch) -> None:
    """Guard: the read-only count URI is built the way the onramp builds one,
    so a `#`, `?` or `%` in the data directory's name cannot make SQLite open
    a different path (or create a stray file in the parent)."""

    from alicebot_api.mcp.types import MCPNotPermittedError

    for mark in ("#", "?", "%41"):
        vault_dir = tmp_path / f"odd{mark}dir"
        database = _vault(tmp_path / f"v{abs(hash(mark))}")
        # move the vault into the odd-named directory
        vault_dir.mkdir()
        moved = vault_dir / "vault.sqlite3"
        import shutil
        shutil.move(str(database), moved)
        context = _context(moved)
        _create_key(moved, agent_id="reader", profile="read_only_agent")
        with pytest.raises(MCPNotPermittedError):
            _call(context, "alice_recall", {"query": "cadence"})
        # no stray file appeared in the parent folder: only the vault and its
        # write-ahead pair, nothing the odd name could have redirected to
        names = sorted(p.name for p in vault_dir.iterdir())
        assert set(names) <= {"vault.sqlite3", "vault.sqlite3-wal", "vault.sqlite3-shm"}, (mark, names)


def test_revoke_echoes_at_most_the_prefix(tmp_path: Path) -> None:
    """Guard: the refusal message echoes at most the 12-character prefix, so a
    raw key handed to revoke is never echoed whole."""

    database = _vault(tmp_path)
    context = _context(database)
    _create_key(database, agent_id="keeper", profile="read_only_agent")
    raw = "alice_sk_9m2kd real-key-shape-that-is-not-a-real-key-value"
    proc = _keys(["keys", "revoke", raw, "--db", str(database), "--user-id", USER_ID])
    assert proc.returncode == 2
    assert raw not in proc.stderr, proc.stderr
    assert raw[:12] in proc.stderr


def test_keys_list_covers_every_key_and_shows_the_id(tmp_path: Path) -> None:
    """Guard: the list's default returns every key and each item carries the
    id the revoke message points at."""

    database = _vault(tmp_path)
    for index in range(4):
        _create_key(database, agent_id=f"reader-{index}", profile="read_only_agent")
    payload = json.loads(_keys(["keys", "list", "--db", str(database), "--user-id", USER_ID, "--json"]).stdout)
    assert payload["count"] == 4
    assert all(str(item["id"]) for item in payload["items"])
    # a prefix that matches several keys is refused with the id pointer
    proc = _keys(["keys", "revoke", "alice_sk", "--db", str(database), "--user-id", USER_ID])
    assert proc.returncode == 2
    assert "key id shown by keys list" in proc.stderr
