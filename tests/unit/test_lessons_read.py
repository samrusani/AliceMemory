"""Published lesson reads through the SQLite MCP tool."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool, list_mcp_tools
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key

LESSON = {
    "applies_when": "rebasing a feature branch",
    "what_happened": "A conflict hid a change.",
    "do_instead": "Inspect both sides.",
    "paths": ["src/*.py"],
}


class Vault:
    def __init__(self, path: Path):
        self.path = path
        self.user = uuid4()
        bootstrap_database(path, user_id=self.user, user_email="fixture@example.invalid")

    def key(self, name, profile, project=None):
        with sqlite_user_connection(self.path, self.user) as conn:
            _, raw = create_agent_key(
                SQLiteVNextStore(conn, self.user),
                user_id=self.user,
                agent_id=name,
                permission_profile=profile,
                project_scope=project,
            )
        return raw

    def call(self, monkeypatch, token, name, arguments):
        if token is None:
            monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        else:
            monkeypatch.setenv("ALICE_AGENT_API_KEY", token)
        monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
        context = MCPRuntimeContext(database_url=sqlite_url_for_path(self.path), user_id=self.user)
        return json.loads(json.dumps(call_mcp_tool(context, name=name, arguments=arguments), default=str))

    def commit(self, monkeypatch, token, agent=None, lesson=None, **kwargs):
        args = {"lesson": lesson or LESON(), **kwargs}
        if agent:
            args.update(agent_id=agent, agent_type="coding_agent")
        return self.call(monkeypatch, token, "alice_memory_commit", args)


def LESON():
    return dict(LESSON)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    return Vault(tmp_path / "vault.db")


def test_publication_paths_and_waiting_rows(vault, monkeypatch):
    """Guard: require a recorded publishing act before a lesson is returned."""
    admin = vault.key("admin", "admin_agent", "alpha")
    writer = vault.key("writer", "project_scoped_agent", "alpha")
    reader = vault.key("reader", "read_only_agent", "alpha")
    waiting = vault.commit(monkeypatch, writer, "writer")
    rejected = vault.commit(
        monkeypatch, writer, "writer", lesson={**LESSON, "applies_when": "rebasing a rejected branch"}
    )
    held = vault.commit(
        monkeypatch, admin, "admin", lesson={**LESSON, "applies_when": "rebasing a held branch"}, confidence=0.7
    )
    committed = vault.commit(
        monkeypatch, admin, "admin", lesson={**LESSON, "applies_when": "rebasing a committed branch"}
    )
    vault.call(
        monkeypatch,
        admin,
        "alice_memory_correct",
        {"action": "reject", "review_item_id": rejected["memory"]["id"], "agent_id": "admin"},
    )

    def read():
        return vault.call(
            monkeypatch, reader, "alice_lessons", {"task": "rebasing", "agent_id": "reader", "limit": 10}
        )["lessons"]

    assert [x["id"] for x in read()] == [committed["memory"]["id"]]
    approved = vault.call(
        monkeypatch,
        admin,
        "alice_memory_correct",
        {"action": "approve", "review_item_id": waiting["memory"]["id"], "agent_id": "admin"},
    )
    confirmed = vault.call(
        monkeypatch,
        admin,
        "alice_memory_manage",
        {
            "action": "confirm",
            "memory_id": held["memory"]["id"],
            "confirmation_id": held["confirmation_id"],
            "agent_id": "admin",
        },
    )
    assert approved["memory"]["status"] == confirmed["memory"]["status"] == "active"
    found = {x["id"]: x for x in read()}
    with sqlite_user_connection(vault.path, vault.user) as conn:
        records = {
            row["memory_id"]: dict(row)
            for row in conn.execute(
                "select memory_id, action, actor_id, created_at from memory_revisions where action in ('memory_correct_approve','agentic_memory_confirm_confirm','agentic_memory_commit')"
            )
        }
    for mid, how in [
        (waiting["memory"]["id"], "approved"),
        (held["memory"]["id"], "confirmed"),
        (committed["memory"]["id"], "committed"),
    ]:
        assert found[mid]["published"] == {"by": "admin", "at": records[mid]["created_at"], "how": how}
    assert rejected["memory"]["id"] not in found


def test_confirmation_by_matching_nonadmin_identity(vault, monkeypatch):
    """Guard: the stored writer profile, not the confirm key profile, establishes the publisher."""
    admin = vault.key("shared", "admin_agent", "alpha")
    limited = vault.key("shared", "project_scoped_agent", "alpha")
    held = vault.commit(monkeypatch, admin, "shared", confidence=0.7)
    vault.call(
        monkeypatch,
        limited,
        "alice_memory_manage",
        {
            "action": "confirm",
            "memory_id": held["memory"]["id"],
            "confirmation_id": held["confirmation_id"],
            "agent_id": "shared",
        },
    )
    answer = vault.call(monkeypatch, limited, "alice_lessons", {"task": "rebasing", "agent_id": "shared"})
    assert answer["count"] == 1
    assert answer["lessons"][0]["published"]["by"] == "shared"
    assert answer["lessons"][0]["published"]["how"] == "confirmed"


def test_confidential_lesson_follows_the_key_ceiling(vault, monkeypatch):
    """Guard: the policy is asked for all levels before it applies the key ceiling."""
    admin = vault.key("admin", "admin_agent", "alpha")
    limited = vault.key("reader", "project_scoped_agent", "alpha")
    held = vault.commit(monkeypatch, admin, "admin", sensitivity="confidential")
    assert held["status"] == "confirmation_required"
    vault.call(
        monkeypatch,
        admin,
        "alice_memory_manage",
        {
            "action": "confirm",
            "memory_id": held["memory"]["id"],
            "confirmation_id": held["confirmation_id"],
            "agent_id": "admin",
        },
    )
    for args in ({"task": "rebasing"}, {"task": "unrelatedtoken", "paths": ["src/a.py"]}):
        allowed = vault.call(monkeypatch, admin, "alice_lessons", {**args, "agent_id": "admin"})
        denied = vault.call(monkeypatch, limited, "alice_lessons", {**args, "agent_id": "reader"})
        assert [item["id"] for item in allowed["lessons"]] == [held["memory"]["id"]]
        assert denied["count"] == 0


def test_owner_and_malformed_block(vault, monkeypatch):
    """Guard: malformed lesson blocks are skipped without losing valid owner lessons."""
    good = vault.commit(monkeypatch, None)
    bad = vault.commit(monkeypatch, None, lesson={**LESSON, "applies_when": "rebasing another branch"})
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        row = store.get_memory(bad["memory"]["id"])
        value = dict(row["value"])
        value["lesson"] = {"applies_when": "broken"}
        store.update_memory(memory_id=bad["memory"]["id"], patch={"value": value})
        store.append_revision(
            {
                "memory_id": row["id"],
                "memory_key": row["memory_key"],
                "new_value": value,
                "action": "memory_correct_approve",
                "revision_type": "promoted",
                "actor_type": "user",
            }
        )
    answer = vault.call(monkeypatch, None, "alice_lessons", {"task": "rebasing", "limit": 10})
    assert [x["id"] for x in answer["lessons"]] == [good["memory"]["id"]]
    assert answer["lessons"][0]["published"]["by"] == "owner"


def test_legacy_import_and_full_restore(vault, monkeypatch, tmp_path):
    """Guard: the reader requires a publishing revision after either import format."""
    original = vault.commit(monkeypatch, None)
    before = vault.call(monkeypatch, None, "alice_lessons", {"task": "rebasing"})["lessons"][0]["published"]
    export = tmp_path / "full.jsonl"
    command = [sys.executable, "-m", "alicebot_api.onramp"]
    subprocess.run(
        [*command, "export", "--db", str(vault.path), "--user-id", str(vault.user), "--out", str(export)],
        check=True,
        capture_output=True,
    )
    rows = [json.loads(line) for line in export.read_text().splitlines()]
    target = tmp_path / "restored.db"
    subprocess.run(
        [*command, "import", "--db", str(target), "--user-id", str(vault.user), "--in", str(export)],
        check=True,
        capture_output=True,
    )
    restored = Vault.__new__(Vault)
    restored.path = target
    restored.user = vault.user
    assert restored.call(monkeypatch, None, "alice_lessons", {"task": "rebasing"})["lessons"][0]["published"] == before
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text("\n".join(json.dumps(row) for row in rows if row["record_type"] == "memory") + "\n")
    legacy_db = tmp_path / "legacy.db"
    subprocess.run(
        [*command, "import", "--db", str(legacy_db), "--user-id", str(vault.user), "--in", str(legacy)],
        check=True,
        capture_output=True,
    )
    legacy_vault = Vault.__new__(Vault)
    legacy_vault.path = legacy_db
    legacy_vault.user = vault.user
    assert legacy_vault.call(monkeypatch, None, "alice_lessons", {"task": "rebasing"})["count"] == 0
    with sqlite3.connect(legacy_db) as conn:
        assert (
            conn.execute(
                "select count(*) from memories where id = ? and status = 'active'", (original["memory"]["id"],)
            ).fetchone()[0]
            == 1
        )


def test_matching_groups_and_limit(vault, monkeypatch):
    """Guard: a text or path match is required and matching paths take precedence."""
    rows = []
    for block in [
        {
            "applies_when": "quartzfeature both",
            "what_happened": "A change was hidden.",
            "do_instead": "Inspect both.",
            "paths": ["src/*.py"],
        },
        {
            "applies_when": "a different path task",
            "what_happened": "A change was hidden.",
            "do_instead": "Inspect both.",
            "paths": ["src/*.py"],
        },
        {
            "applies_when": "quartzfeature text only",
            "what_happened": "A change was hidden.",
            "do_instead": "Inspect both.",
            "paths": ["docs/*.md"],
        },
        {
            "applies_when": "unrelated task",
            "what_happened": "A change was hidden.",
            "do_instead": "Inspect both.",
            "paths": ["other/*.txt"],
        },
    ]:
        rows.append(vault.commit(monkeypatch, None, lesson=block)["memory"]["id"])
    args = {"task": "quartzfeature", "paths": ["./src/a.py"]}
    answer = vault.call(monkeypatch, None, "alice_lessons", args)
    assert [x["id"] for x in answer["lessons"]] == rows[:3]
    assert vault.call(monkeypatch, None, "alice_lessons", args) == answer
    assert vault.call(monkeypatch, None, "alice_lessons", {**args, "limit": 1})["count"] == 1
    assert vault.call(monkeypatch, None, "alice_lessons", {"task": "xylophone", "paths": ["no/file"]})["count"] == 0


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"task": ""},
        {"task": "x" * 1001},
        {"task": "x", "paths": "bad"},
        {"task": "x", "paths": ["../bad"]},
        {"task": "x", "paths": ["/abs"]},
        {"task": "x", "paths": ["~bad"]},
        {"task": "x", "paths": ["C:foo"]},
        {"task": "x", "paths": ["bad\\path"]},
        {"task": "x", "paths": [""]},
        {"task": "x", "paths": ["x" * 201]},
        {"task": "x", "paths": ["x"] * 17},
        {"task": "x", "paths": [1]},
        {"task": "x", "paths": ["bad\x00path"]},
        {"task": "x", "limit": 0},
        {"task": "x", "limit": 11},
        {"task": "x", "extra": 1},
    ],
)
def test_invalid_inputs_are_refused(vault, monkeypatch, bad):
    """Guard: the argument schema and shared lesson path validator reject invalid calls."""
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    assert "alice_lessons" in {item["name"] for item in list_mcp_tools()}
    with pytest.raises(MCPToolError):
        vault.call(monkeypatch, None, "alice_lessons", bad)


def test_event_and_no_query_retention(vault, monkeypatch, tmp_path):
    """Guard: the request event records only the three permitted numbers."""
    vault.commit(monkeypatch, None)
    task = "rebasing feature branch unique-signal-79524"
    with sqlite3.connect(vault.path) as conn:
        before = conn.execute("select * from memories").fetchall()
    answer = vault.call(monkeypatch, None, "alice_lessons", {"task": task, "paths": ["src/a.py"]})
    with sqlite3.connect(vault.path) as conn:
        assert conn.execute("select * from memories").fetchall() == before
        rows = conn.execute(
            "select actor_type, actor_id, payload_json from event_log where event_type = 'agent.lessons_requested'"
        ).fetchall()
    assert len(rows) == 1 and rows[0][:2] == ("user", None)
    assert json.loads(rows[0][2]) == {"returned": answer["count"], "limit": 5, "paths_given": 1}
    export = tmp_path / "export.jsonl"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alicebot_api.onramp",
            "export",
            "--db",
            str(vault.path),
            "--user-id",
            str(vault.user),
            "--out",
            str(export),
        ],
        check=True,
        capture_output=True,
    )
    assert task not in export.read_text()


def test_surface(monkeypatch):
    """Guard: lessons is full-only and all other definitions match main."""
    import hashlib

    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_MCP_LEGACY_TOOLS", raising=False)
    monkeypatch.delenv("ALICE_LEGACY_SURFACES", raising=False)
    monkeypatch.delenv("ALICE_MCP_FULL_TOOLS", raising=False)
    assert "alice_lessons" not in [item["name"] for item in list_mcp_tools()]
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    tools = {item["name"]: item for item in list_mcp_tools()}
    assert tools["alice_lessons"]["annotations"] == tools["alice_recall"]["annotations"]
    old = [item for item in list_mcp_tools() if item["name"] != "alice_lessons"]
    digest = hashlib.sha256(json.dumps(old, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert digest == "efe16c0ef2d36397c47ac2a3ba1951e257af5c7a9ae5fd735fcc01b276c6456e"


def test_a_plain_row_with_a_planted_block_has_no_publisher(vault, monkeypatch):
    """Guard: a stored lesson block alone is not a publishing revision."""
    row = vault.call(
        monkeypatch,
        None,
        "alice_memory_commit",
        {"memory_type": "procedure", "title": "rebasing", "canonical_text": "rebasing feature branch"},
    )["memory"]
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        current = store.get_memory(row["id"])
        value = dict(current["value"])
        value["lesson"] = LESON()
        store.update_memory(memory_id=row["id"], patch={"value": value})
    # The plain commit act predates the lesson and is not a lesson publication.
    assert vault.call(monkeypatch, None, "alice_lessons", {"task": "rebasing"})["count"] == 0


def test_commit_publication_requires_created_revision(vault, monkeypatch):
    """Guard: a commit action with a different revision type is not a publishing act."""
    admin = vault.key("admin", "admin_agent", "alpha")
    row = vault.call(
        monkeypatch,
        admin,
        "alice_memory_commit",
        {"memory_type": "procedure", "title": "rebasing", "canonical_text": "rebasing", "agent_id": "admin"},
    )["memory"]
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        current = store.get_memory(row["id"])
        value = dict(current["value"])
        value["lesson"] = LESON()
        store.update_memory(memory_id=row["id"], patch={"value": value})
        store.append_revision(
            {"memory_id": row["id"], "memory_key": row["memory_key"], "new_value": value,
             "action": "agentic_memory_commit", "revision_type": "edited", "actor_type": "agent", "actor_id": "admin"}
        )
    result = vault.call(monkeypatch, admin, "alice_lessons", {"task": "rebasing", "agent_id": "admin"})
    assert result["count"] == 0


def test_path_only_results_respect_limit(vault, monkeypatch):
    """Guard: path-only matches stop after the caller's requested limit."""
    for index in range(4):
        vault.commit(monkeypatch, None, lesson={**LESSON, "applies_when": f"rebasing {index}"})
    answer = vault.call(monkeypatch, None, "alice_lessons", {"task": "unrelatedtoken", "paths": ["src/a.py"], "limit": 1})
    assert answer["count"] == len(answer["lessons"]) == 1


def test_path_only_ties_use_creation_time_before_id(vault, monkeypatch):
    """Guard: newer path-only lessons precede older lessons regardless of id."""
    ids = sorted(vault.commit(monkeypatch, None)["memory"]["id"] for _ in range(2))
    with sqlite3.connect(vault.path) as conn:
        conn.execute("update memories set created_at = '2020-01-01T00:00:00Z' where id = ?", (ids[0],))
        conn.execute("update memories set created_at = '2021-01-01T00:00:00Z' where id = ?", (ids[1],))
        conn.commit()
    answer = vault.call(monkeypatch, None, "alice_lessons", {"task": "unrelatedtoken", "paths": ["src/a.py"]})
    assert [item["id"] for item in answer["lessons"]] == ids[::-1]


def test_retired_and_unexpired_lessons(vault, monkeypatch):
    """Guard: only active, unexpired and unredacted rows enter the result."""
    ids = [
        vault.commit(monkeypatch, None, lesson={**LESSON, "applies_when": f"rebasing branch {i}"})["memory"]["id"]
        for i in range(4)
    ]
    for mid, action in zip(ids, ("expire", "forget", "undo", "redact"), strict=True):
        vault.call(monkeypatch, None, "alice_memory_manage", {"action": action, "memory_id": mid, "reason": "retired"})
    assert vault.call(monkeypatch, None, "alice_lessons", {"task": "rebasing", "limit": 10})["count"] == 0
    vault.call(
        monkeypatch, None, "alice_memory_manage", {"action": "unexpire", "memory_id": ids[0], "reason": "reopen"}
    )
    assert [x["id"] for x in vault.call(monkeypatch, None, "alice_lessons", {"task": "rebasing"})["lessons"]] == [
        ids[0]
    ]


@pytest.mark.parametrize("profile", ["project_scoped_agent", "admin_agent", "read_only_agent"])
def test_hidden_rows_do_not_change_the_answer_or_event(vault, monkeypatch, tmp_path, profile):
    """Guard: the recall fence prevents inaccessible rows from affecting the result."""
    admin = vault.key("publisher", "admin_agent")
    reader = vault.key("reader", profile, "alpha")
    visible = vault.commit(monkeypatch, admin, "publisher", project_scope=["alpha"])
    copy_path = tmp_path / "copy.db"
    with sqlite3.connect(vault.path) as src, sqlite3.connect(copy_path) as dst:
        src.backup(dst)
    other = Vault.__new__(Vault)
    other.path = copy_path
    other.user = vault.user
    hidden = [
        {"project_scope": ["beta"]},
        {"project_scope": ["beta"] if profile == "admin_agent" else ["alpha"], "sensitivity": "confidential"},
        {"project_scope": ["beta"] if profile == "admin_agent" else ["alpha"], "domain": "family"},
    ]
    for i, fields in enumerate(hidden):
        item = other.commit(
            monkeypatch, admin, "publisher", lesson={**LESSON, "applies_when": f"rebasing hidden {i}"}, **fields
        )
        if item["status"] == "confirmation_required":
            other.call(
                monkeypatch,
                admin,
                "alice_memory_manage",
                {
                    "action": "confirm",
                    "memory_id": item["memory"]["id"],
                    "confirmation_id": item["confirmation_id"],
                    "agent_id": "publisher",
                },
            )
        else:
            assert item["status"] == "committed"
    args = {"task": "rebasing feature branch", "paths": ["src/a.py"], "agent_id": "reader"}
    first = vault.call(monkeypatch, reader, "alice_lessons", args)
    second = other.call(monkeypatch, reader, "alice_lessons", args)
    assert first == second
    assert [x["id"] for x in first["lessons"]] == [visible["memory"]["id"]]
    for item in (vault, other):
        with sqlite3.connect(item.path) as conn:
            event = conn.execute(
                "select actor_type,actor_id,payload_json from event_log where event_type='agent.lessons_requested' order by occurred_at desc limit 1"
            ).fetchone()
        assert event[:2] == ("agent", "reader")
        assert json.loads(event[2]) == {"returned": 1, "limit": 5, "paths_given": 1}


def test_hidden_documents_do_not_change_text_rank(vault, monkeypatch, tmp_path):
    """Guard: lesson text rank must not depend on inaccessible documents in the shared FTS index."""
    import copy
    from uuid import uuid4

    admin = vault.key("admin", "admin_agent")
    reader = vault.key("reader", "project_scoped_agent", "alpha")

    def write(text):
        return vault.commit(
            monkeypatch, admin, "admin", lesson={**LESSON, "applies_when": text}, project_scope=["alpha"]
        )["memory"]["id"]

    second = write("luminousmigration")
    first = write("amberrebase")
    other_path = tmp_path / "hidden.db"
    with sqlite3.connect(vault.path) as src, sqlite3.connect(other_path) as dst:
        src.backup(dst)
    with sqlite3.connect(other_path) as conn:
        conn.row_factory = sqlite3.Row
        memory = dict(conn.execute("select * from memories where id = ?", (first,)).fetchone())
        revision = dict(conn.execute("select * from memory_revisions where memory_id = ?", (first,)).fetchone())
        columns = list(memory)
        revision_columns = list(revision)
        memory_sql = "insert into memories (" + ",".join(columns) + ") values (" + ",".join("?" for _ in columns) + ")"
        revision_sql = (
            "insert into memory_revisions ("
            + ",".join(revision_columns)
            + ") values ("
            + ",".join("?" for _ in revision_columns)
            + ")"
        )
        for i in range(100):
            row = copy.copy(memory)
            row["id"] = str(uuid4())
            row["memory_key"] = f"hidden.{i}"
            row["project_id"] = "beta"
            metadata = json.loads(row["metadata_json"])
            metadata["project_scope"] = ["beta"]
            metadata["agentic_memory"]["project_scope"] = ["beta"]
            row["metadata_json"] = json.dumps(metadata)
            rev = copy.copy(revision)
            rev["id"] = str(uuid4())
            rev["memory_id"] = row["id"]
            rev["memory_key"] = row["memory_key"]
            conn.execute(memory_sql, tuple(row[c] for c in columns))
            conn.execute(revision_sql, tuple(rev[c] for c in revision_columns))
        conn.commit()
    other = Vault.__new__(Vault)
    other.path = other_path
    other.user = vault.user
    args = {"task": "amberrebase luminousmigration", "paths": ["src/a.py"], "agent_id": "reader"}
    original = vault.call(monkeypatch, reader, "alice_lessons", args)
    with_hidden = other.call(monkeypatch, reader, "alice_lessons", args)
    assert original == with_hidden
    assert {x["id"] for x in original["lessons"]} == {first, second}


def test_statement_count_does_not_grow_with_lesson_count(vault, monkeypatch):
    """Guard: revisions are fetched in one batch rather than one statement per lesson."""
    import copy
    from alicebot_api.sqlite_store import SQLiteVNextStore

    admin = vault.key("admin", "admin_agent", "alpha")
    vault.commit(monkeypatch, admin, "admin")
    with sqlite3.connect(vault.path) as conn:
        conn.row_factory = sqlite3.Row
        memory = dict(conn.execute("select * from memories").fetchone())
        revision = dict(conn.execute("select * from memory_revisions").fetchone())
        columns = list(memory)
        rc = list(revision)
        memory_sql = "insert into memories (" + ",".join(columns) + ") values (" + ",".join("?" for _ in columns) + ")"
        revision_sql = "insert into memory_revisions (" + ",".join(rc) + ") values (" + ",".join("?" for _ in rc) + ")"

        def add(number, start):
            for i in range(start, start + number):
                row = copy.copy(memory)
                row["id"] = str(uuid4())
                row["memory_key"] = f"budget.{i}"
                rev = copy.copy(revision)
                rev["id"] = str(uuid4())
                rev["memory_id"] = row["id"]
                rev["memory_key"] = row["memory_key"]
                conn.execute(memory_sql, tuple(row[c] for c in columns))
                conn.execute(revision_sql, tuple(rev[c] for c in rc))
            conn.commit()

        add(99, 0)
    original = SQLiteVNextStore._execute
    statements = []

    def counted(self, query, params=()):
        statements.append(query)
        return original(self, query, params)

    monkeypatch.setattr(SQLiteVNextStore, "_execute", counted)

    def measured():
        statements.clear()
        answer = vault.call(
            monkeypatch, admin, "alice_lessons", {"task": "rebasing", "paths": ["src/a.py"], "agent_id": "admin"}
        )
        assert answer["count"] == 5
        assert sum("JOIN memory_revisions" in q for q in statements) == 1
        return len(statements)

    at_100 = measured()
    with sqlite3.connect(vault.path) as conn:

        def add_more():
            for i in range(1900):
                row = copy.copy(memory)
                row["id"] = str(uuid4())
                row["memory_key"] = f"budget.extra.{i}"
                rev = copy.copy(revision)
                rev["id"] = str(uuid4())
                rev["memory_id"] = row["id"]
                rev["memory_key"] = row["memory_key"]
                conn.execute(memory_sql, tuple(row[c] for c in columns))
                conn.execute(revision_sql, tuple(rev[c] for c in rc))
            conn.commit()

        add_more()
    at_2000 = measured()
    assert at_100 == at_2000


def test_revision_reads_exclude_other_projects(vault, monkeypatch):
    """Guard: a bound reader fetches publication acts only for its candidates."""
    publisher = vault.key("publisher", "admin_agent")
    reader = vault.key("reader", "project_scoped_agent", "alpha")
    visible = vault.commit(monkeypatch, publisher, "publisher", project_scope=["alpha"])["memory"]["id"]
    hidden = vault.commit(monkeypatch, publisher, "publisher", project_scope=["beta"])["memory"]["id"]
    original = SQLiteVNextStore.stream_lesson_revisions
    revision_ids = []

    def observed(self, memory_id):
        rows = original(self, memory_id)
        if not isinstance(memory_id, list):
            return rows

        def capture():
            for row in rows:
                revision_ids.append(str(row.get("memory_id")))
                yield row

        return capture()

    monkeypatch.setattr(SQLiteVNextStore, "stream_lesson_revisions", observed)
    answer = vault.call(monkeypatch, reader, "alice_lessons", {"task": "rebasing", "agent_id": "reader"})
    assert [item["id"] for item in answer["lessons"]] == [visible]
    assert visible in revision_ids and hidden not in revision_ids


def test_mixed_path_results_do_not_load_every_full_row(vault, monkeypatch):
    """Guard: mixed path matches do not force an unbounded full-memory read."""
    for index in range(12):
        vault.commit(
            monkeypatch,
            None,
            lesson={**LESSON, "paths": ["src/*.py"] if index % 2 else ["other/*.py"]},
        )
    original = SQLiteVNextStore.list_memories
    unbounded = []

    def observed(self, **kwargs):
        rows = original(self, **kwargs)
        if kwargs.get("lessons_only") and not kwargs.get("lesson_projection") and kwargs.get("limit") is None:
            unbounded.extend(rows)
        return rows

    monkeypatch.setattr(SQLiteVNextStore, "list_memories", observed)
    answer = vault.call(monkeypatch, None, "alice_lessons", {"task": "rebasing", "paths": ["src/a.py"]})
    assert answer["count"] == 5
    assert unbounded == []


def test_default_limit_and_stable_ties(vault, monkeypatch):
    """Guard: equally ranked lessons use creation time and id, with a default limit of five."""
    ids = [vault.commit(monkeypatch, None)["memory"]["id"] for _ in range(11)]
    with sqlite3.connect(vault.path) as conn:
        conn.execute("update memories set created_at = '2020-01-01T00:00:00Z'")
        conn.execute("update memories set created_at = '2021-01-01T00:00:00Z' where id = ?", (ids[0],))
        conn.commit()
    expected = [ids[0], *sorted(ids[1:])]
    args = {"task": "rebasing feature branch"}
    assert [row["id"] for row in vault.call(monkeypatch, None, "alice_lessons", args)["lessons"]] == expected[:5]
    assert [
        row["id"] for row in vault.call(monkeypatch, None, "alice_lessons", {**args, "limit": 10})["lessons"]
    ] == expected[:10]


def test_different_admin_confirms(vault, monkeypatch):
    """Guard: confirmation attributes publication to the confirming admin, not the writer."""
    author = vault.key("author", "admin_agent", "alpha")
    confirmer = vault.key("confirmer", "admin_agent", "alpha")
    held = vault.commit(monkeypatch, author, "author", confidence=0.7)
    vault.call(
        monkeypatch,
        confirmer,
        "alice_memory_manage",
        {
            "action": "confirm",
            "memory_id": held["memory"]["id"],
            "confirmation_id": held["confirmation_id"],
            "agent_id": "confirmer",
        },
    )
    answer = vault.call(monkeypatch, confirmer, "alice_lessons", {"task": "rebasing", "agent_id": "confirmer"})
    assert answer["lessons"][0]["published"]["by"] == "confirmer"
    assert answer["lessons"][0]["published"]["how"] == "confirmed"


def test_task_whitespace_and_upper_boundary(vault, monkeypatch):
    """Guard: task length is checked after collapsing whitespace."""
    vault.commit(monkeypatch, None)
    answer = vault.call(monkeypatch, None, "alice_lessons", {"task": "  rebasing   a\n feature  branch  "})
    assert answer["count"] == 1
    assert vault.call(monkeypatch, None, "alice_lessons", {"task": "x" * 1000})["count"] == 0
    assert vault.call(monkeypatch, None, "alice_lessons", {"task": "   ".join(["x"] * 500)})["count"] == 0


def test_vector_stage_searches_only_lessons(vault, monkeypatch):
    """Guard: vector matches use the fenced lesson stage rather than plain procedure rows."""
    import alicebot_api.vnext_retrieval as retrieval
    from alicebot_api.vnext_embeddings import EMBEDDING_SIGNATURE_VERSION, memory_embedding_content_sha256
    from tests.unit.test_vnext_retrieval import StubEmbeddingProvider

    lesson = vault.commit(monkeypatch, None)
    plain = vault.call(
        monkeypatch,
        None,
        "alice_memory_commit",
        {"title": "plain note", "canonical_text": "A plain procedure.", "memory_type": "procedure"},
    )
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        for item in (lesson, plain):
            row = store.get_memory(item["memory"]["id"])
            store.update_memory_embedding(
                memory_id=str(row["id"]),
                vector=[0.1] * 1536 if item is plain else [1.0, 0.0],
                provider="stub",
                model="stub-embedding",
                content_sha256=memory_embedding_content_sha256(row),
                signature_version=EMBEDDING_SIGNATURE_VERSION,
            )
    provider = StubEmbeddingProvider()
    monkeypatch.setattr(retrieval, "get_embedding_provider", lambda: provider)
    answer = vault.call(monkeypatch, None, "alice_lessons", {"task": "merge integration"})
    assert [item["id"] for item in answer["lessons"]] == [lesson["memory"]["id"]]
    assert provider.embedded_texts == ["merge integration"]


@pytest.mark.parametrize("mode", ["path", "text", "vector"])
def test_hidden_path_prefix_does_not_hide_a_visible_lesson(vault, monkeypatch, tmp_path, mode):
    """Guard: unreadable matches cannot consume the visible result."""
    import copy
    import struct

    admin = vault.key("publisher", "admin_agent")
    reader = vault.key("reader", "project_scoped_agent", "alpha")
    visible = vault.commit(monkeypatch, admin, "publisher", project_scope=["alpha"])["memory"]["id"]
    if mode == "vector":
        import alicebot_api.vnext_retrieval as retrieval
        from alicebot_api.vnext_embeddings import EMBEDDING_SIGNATURE_VERSION, memory_embedding_content_sha256
        from tests.unit.test_vnext_retrieval import StubEmbeddingProvider

        monkeypatch.setattr(retrieval, "get_embedding_provider", lambda: StubEmbeddingProvider())
        with sqlite_user_connection(vault.path, vault.user) as conn:
            store = SQLiteVNextStore(conn, vault.user)
            memory = store.get_memory(visible)
            store.update_memory_embedding(
                memory_id=visible, vector=[1.0, 0.0], provider="stub", model="stub-embedding",
                content_sha256=memory_embedding_content_sha256(memory), signature_version=EMBEDDING_SIGNATURE_VERSION,
            )
    target = tmp_path / "hidden-prefix.db"
    with sqlite3.connect(vault.path) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    with sqlite_user_connection(target, vault.user) as conn:
        source = SQLiteVNextStore(conn, vault.user).create_source(
            {
                "source_type": "note",
                "title": "Restricted input",
                "domain": "project",
                "sensitivity": "confidential",
                "content_hash": "sha256:restricted",
                "metadata_json": {"project_scope": ["alpha"]},
            }
        )
        public = SQLiteVNextStore(conn, vault.user).create_source(
            {"source_type": "note", "title": "Public input", "domain": "project", "sensitivity": "internal",
             "content_hash": "sha256:public-input", "metadata_json": {"project_scope": ["alpha"]}}
        )
    with sqlite3.connect(target) as conn:
        conn.row_factory = sqlite3.Row
        memory = dict(conn.execute("select * from memories where id=?", (visible,)).fetchone())
        revision = dict(conn.execute("select * from memory_revisions where memory_id=?", (visible,)).fetchone())
        columns = list(memory)
        rc = list(revision)
        memory_sql = "insert into memories (" + ",".join(columns) + ") values (" + ",".join("?" for _ in columns) + ")"
        revision_sql = "insert into memory_revisions (" + ",".join(rc) + ") values (" + ",".join("?" for _ in rc) + ")"
        for i in range(129):
            row = copy.copy(memory)
            row["id"] = str(uuid4())
            row["memory_key"] = f"hidden-prefix.{i}"
            row["created_at"] = "2099-01-01T00:00:00Z"
            if mode == "vector":
                row["embedding"] = struct.pack("<1536f", *([0.1] * 1536))
            metadata = json.loads(row["metadata_json"])
            metadata["derived_from"] = {"sources": [str(public["id"]), str(source["id"])]}
            row["metadata_json"] = json.dumps(metadata)
            rev = copy.copy(revision)
            rev["id"] = str(uuid4())
            rev["memory_id"] = row["id"]
            rev["memory_key"] = row["memory_key"]
            conn.execute(memory_sql, tuple(row[c] for c in columns))
            conn.execute(revision_sql, tuple(rev[c] for c in rc))
        conn.commit()
    other = Vault.__new__(Vault)
    other.path = target
    other.user = vault.user
    args = {"task": "rebasing" if mode == "text" else "unrelatedtoken", "agent_id": "reader"}
    if mode == "path":
        args["paths"] = ["src/a.py"]
    before = vault.call(monkeypatch, reader, "alice_lessons", args)
    after = other.call(monkeypatch, reader, "alice_lessons", args)
    assert before == after
    assert [item["id"] for item in after["lessons"]] == [visible]


@pytest.mark.parametrize("action", ["agentic_memory_commit", "agentic_memory_confirm_confirm"])
def test_nonadmin_writer_cannot_publish_by_commit_or_confirm(vault, monkeypatch, action):
    """Guard: commit and confirmation publication require the recorded writer to be owner or admin."""
    writer = vault.key("writer", "project_scoped_agent", "alpha")
    held = vault.commit(monkeypatch, writer, "writer")
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        row = store.get_memory(held["memory"]["id"])
        store.update_memory(memory_id=row["id"], patch={"status": "active"})
        store.append_revision(
            {
                "memory_id": row["id"],
                "memory_key": row["memory_key"],
                "new_value": row["value"],
                "action": action,
                "revision_type": "created" if action == "agentic_memory_commit" else "promoted",
                "actor_type": "agent",
                "actor_id": "writer",
            }
        )
    answer = vault.call(monkeypatch, writer, "alice_lessons", {"task": "rebasing", "agent_id": "writer"})
    assert answer["count"] == 0
