"""alice_lessons: published lessons for the task an agent is about to do.

Every test reaches the value through the MCP tool on a SQLite vault, with real
agent keys from ``create_agent_key`` where a key is the caller. Each docstring
names the guard and what fails when that guard is removed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from alicebot_api.mcp.definitions import _CORE_TOOL_DEFINITIONS
from alicebot_api.mcp.registry import call_mcp_tool, list_mcp_tools
from alicebot_api.mcp.types import MCPArgumentError, MCPToolNotFoundError
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.recall_framing import SESSION_BRIEF_FRAME
from alicebot_api.sqlite_store import sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.sqlite_store import SQLiteVNextStore

# The core digest before this tool, of every definition except alice_lessons.
_OTHER_CORE_DIGEST = "efe16c0ef2d36397c47ac2a3ba1951e257af5c7a9ae5fd735fcc01b276c6456e"

TASK = "about to rebase my feature branch onto main"
REBASE = {
    "applies_when": "rebasing a feature branch onto main",
    "what_happened": "The rebase rewrote commits teammates had already fetched.",
    "do_instead": "Merge main into the feature branch instead of rebasing it.",
    "paths": ["src/*.py"],
}
CALL_PATHS = ["src/feature.py"]


def _lesson(when: str, *, paths: list[str] | None = None, happened: str | None = None) -> dict[str, object]:
    block: dict[str, object] = {
        "applies_when": when,
        "what_happened": happened or "The step failed.",
        "do_instead": "Do the other step.",
    }
    if paths is not None:
        block["paths"] = paths
    return block


class Vault:
    """A SQLite vault reached through the real MCP tool."""

    def __init__(self, tmp_path: Path, name: str = "lessons.sqlite3") -> None:
        self.user_id = uuid4()
        self.path = tmp_path / name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        bootstrap_database(self.path, user_id=self.user_id, user_email="synthetic@example.invalid")

    def context(self) -> MCPRuntimeContext:
        return MCPRuntimeContext(database_url=sqlite_url_for_path(self.path), user_id=self.user_id)

    def key(self, agent_id: str, profile: str, project: str | None = None) -> str:
        with sqlite_user_connection(self.path, self.user_id) as conn:
            _record, raw = create_agent_key(
                SQLiteVNextStore(conn, self.user_id),
                user_id=self.user_id,
                agent_id=agent_id,
                permission_profile=profile,
                project_scope=project,
            )
        return raw

    def call(self, monkeypatch: pytest.MonkeyPatch, key: str | None, name: str, arguments: dict) -> dict:
        if key is None:
            monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        else:
            monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
        return json.loads(json.dumps(call_mcp_tool(self.context(), name=name, arguments=arguments), default=str))

    def checkpoint(self) -> None:
        with sqlite_user_connection(self.path, self.user_id) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def copy_to(self, dest: Path) -> Vault:
        self.checkpoint()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.path, dest)
        twin = object.__new__(Vault)
        twin.user_id = self.user_id
        twin.path = dest
        return twin


def _ident(agent_id: str) -> dict[str, str]:
    return {"agent_id": agent_id, "agent_type": "coding_agent"}


def _commit(vault: Vault, monkeypatch: pytest.MonkeyPatch, key: str | None, lesson: dict, **extra: object) -> dict:
    arguments: dict[str, object] = {"lesson": lesson, **extra}
    agent_id = extra.get("agent_id")
    if isinstance(agent_id, str):
        arguments["agent_type"] = "coding_agent"
    return vault.call(monkeypatch, key, "alice_memory_commit", arguments)


def _lessons(vault: Vault, monkeypatch: pytest.MonkeyPatch, key: str | None, **extra: object) -> dict:
    arguments: dict[str, object] = {"task": TASK, "paths": CALL_PATHS, **extra}
    agent_id = extra.get("agent_id")
    if isinstance(agent_id, str) and "agent_type" not in arguments:
        arguments["agent_type"] = "coding_agent"
    return vault.call(monkeypatch, key, "alice_lessons", arguments)


def _events(vault: Vault) -> list[dict]:
    with sqlite_user_connection(vault.path, vault.user_id) as conn:
        rows = conn.execute(
            """
            SELECT event_type, actor_type, actor_id, payload_json
            FROM event_log
            WHERE event_type = 'agent.lessons_requested'
            ORDER BY occurred_at ASC, id ASC
            """
        ).fetchall()
    found = []
    for row in rows:
        payload = row["payload_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        found.append({
            "event_type": row["event_type"],
            "actor_type": row["actor_type"],
            "actor_id": row["actor_id"],
            "payload": payload,
        })
    return found


def _revision(vault: Vault, memory_id: str, action: str) -> dict:
    with sqlite_user_connection(vault.path, vault.user_id) as conn:
        row = conn.execute(
            """
            SELECT action, actor_type, actor_id, created_at
            FROM memory_revisions
            WHERE memory_id = ? AND action = ?
            ORDER BY revision_number DESC, id DESC
            LIMIT 1
            """,
            (memory_id, action),
        ).fetchone()
    assert row is not None
    return dict(row)


def _memory_rows(vault: Vault) -> list[tuple]:
    with sqlite_user_connection(vault.path, vault.user_id) as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT id, status, value, canonical_text, updated_at FROM memories ORDER BY id"
        ).fetchall()]


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Vault:
    folder = tmp_path / "folder"
    folder.mkdir()
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(folder))
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    return Vault(tmp_path / "vault")


@pytest.fixture(autouse=True)
def _no_ambient(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ALICE_EMBEDDINGS_BASE_URL", "ALICE_EMBEDDINGS_MODEL", "ALICE_EMBEDDINGS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ALICE_MEMORY_PERSONA", raising=False)


def test_only_a_published_lesson_is_returned(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: ``_published_lesson`` keeps a row only when it is active and a publishing act holds the lesson.

    Mutation: drop the active-status check and treat any revision whose new value holds a lesson as
    approved. The waiting, rejected and unconfirmed rows are then returned and this fails.
    """

    owner = _commit(vault, monkeypatch, None, REBASE)
    assert owner["status"] == "committed"
    owner_id = owner["memory"]["id"]
    answer = _lessons(vault, monkeypatch, None)
    assert answer["count"] == 1
    item = answer["lessons"][0]
    owner_revision = _revision(vault, owner_id, "agentic_memory_commit")
    assert item["id"] == owner_id
    assert item["published"] == {"by": "owner", "at": owner_revision["created_at"], "how": "committed"}
    assert item["writer"] == {"id": "owner", "established": "declared_on_keyless_install"}
    assert item["applies_when"] == REBASE["applies_when"]
    assert item["paths"] == REBASE["paths"]
    assert set(item) == {"id", "applies_when", "what_happened", "do_instead", "paths", "writer", "published"}

    keyed = Vault(vault.path.parent / "keyed")
    admin = keyed.key("admin", "admin_agent", "alpha")
    writer = keyed.key("writer", "project_scoped_agent", "alpha")
    reader = keyed.key("reader", "project_scoped_agent", "alpha")
    identity = _ident("reader")
    waiting = _commit(
        keyed, monkeypatch, writer, _lesson("rebasing a feature branch onto main", paths=["src/*.py"]),
        agent_id="writer", domain="project", project_scope=["alpha"],
    )
    assert waiting["status"] == "review_required"
    pending = _commit(
        keyed, monkeypatch, admin, _lesson("rebasing a feature branch onto main", paths=["src/*.py"]),
        agent_id="admin", domain="project", project_scope=["alpha"], confidence=0.7,
    )
    assert pending["status"] == "confirmation_required"
    rejected = _commit(
        keyed, monkeypatch, writer, _lesson("rebasing a feature branch onto main", paths=["src/*.py"]),
        agent_id="writer", domain="project", project_scope=["alpha"],
    )
    keyed.call(monkeypatch, admin, "alice_memory_correct", {
        "action": "reject", "review_item_id": rejected["memory"]["id"], "reason": "not this one", **_ident("admin"),
    })
    before = _lessons(keyed, monkeypatch, reader, **identity)
    assert before["count"] == 0
    assert before["lessons"] == []

    committed = _commit(
        keyed, monkeypatch, admin, REBASE, agent_id="admin", domain="project", project_scope=["alpha"],
    )
    assert committed["status"] == "committed"
    committed_id = committed["memory"]["id"]
    confirmed = keyed.call(monkeypatch, admin, "alice_memory_commit", {
        "confirmation_id": pending["confirmation_id"], "confirmation_action": "confirm", **_ident("admin"),
    })
    assert confirmed["status"] == "committed"
    approved = keyed.call(monkeypatch, admin, "alice_memory_correct", {
        "action": "approve", "review_item_id": waiting["memory"]["id"], "reason": "publish it", **_ident("admin"),
    })
    assert approved["memory"]["status"] == "active"
    answer = _lessons(keyed, monkeypatch, reader, **identity)
    by_id = {item["id"]: item for item in answer["lessons"]}
    assert set(by_id) == {committed_id, pending["memory"]["id"], waiting["memory"]["id"]}
    commit_revision = _revision(keyed, committed_id, "agentic_memory_commit")
    confirm_revision = _revision(keyed, pending["memory"]["id"], "agentic_memory_confirm_confirm")
    approve_revision = _revision(keyed, waiting["memory"]["id"], "memory_correct_approve")
    assert by_id[committed_id]["published"] == {
        "by": "admin", "at": commit_revision["created_at"], "how": "committed",
    }
    assert by_id[pending["memory"]["id"]]["published"] == {
        "by": "admin", "at": confirm_revision["created_at"], "how": "confirmed",
    }
    assert by_id[waiting["memory"]["id"]]["published"] == {
        "by": "admin", "at": approve_revision["created_at"], "how": "approved",
    }
    assert by_id[waiting["memory"]["id"]]["writer"]["id"] == "writer"
    assert "rejected" not in {item["id"] for item in answer["lessons"]}
    assert rejected["memory"]["id"] not in by_id


def test_a_lesson_block_with_no_publishing_act_is_not_returned(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: a publishing revision counts only when its new value holds a lesson.

    Mutation: skip ``_revision_holds_lesson``. The plain procedure, whose commit revision has no
    lesson, is then returned and this fails.
    """

    published = _commit(vault, monkeypatch, None, REBASE)
    plain = vault.call(monkeypatch, None, "alice_memory_commit", {
        "title": "A plain procedure",
        "canonical_text": "Plain procedure text that does not mention the task.",
        "memory_type": "procedure",
    })
    assert plain["status"] == "committed"
    plain_id = plain["memory"]["id"]
    with sqlite_user_connection(vault.path, vault.user_id) as conn:
        row = conn.execute("SELECT value FROM memories WHERE id = ?", (plain_id,)).fetchone()
        value = json.loads(row["value"]) if isinstance(row["value"], str) else dict(row["value"])
        value["lesson"] = REBASE
        conn.execute("UPDATE memories SET value = ? WHERE id = ?", (json.dumps(value), plain_id))
    answer = _lessons(vault, monkeypatch, None)
    assert [item["id"] for item in answer["lessons"]] == [published["memory"]["id"]]


def test_a_bound_key_reads_the_same_lessons_when_hidden_rows_are_added(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Guard: the lessons read passes the caller's domains, sensitivity and project scope into the recall stages and the lesson list.

    Mutation: pass an empty project scope, every domain and every sensitivity into ``_lesson_stage_rows``
    and ``list_lesson_memories``. The copy that holds another project's lesson then answers differently
    and this fails.
    """

    cases = (
        ("project_scoped_agent", "scoped", True),
        ("admin_agent", "bound-admin", False),
        ("read_only_agent", "reader", True),
    )
    for profile, agent_id, hide_labels in cases:
        held = Vault(tmp_path / profile)
        publisher = held.key("publisher", "admin_agent")
        reader = held.key(agent_id, profile, "alpha")
        _commit(
            held, monkeypatch, publisher, REBASE,
            agent_id="publisher", domain="project", sensitivity="internal", project_scope=["alpha"],
        )
        twin = held.copy_to(tmp_path / f"{profile}-hidden.sqlite3")
        _commit(
            twin, monkeypatch, publisher, REBASE,
            agent_id="publisher", domain="project", sensitivity="internal", project_scope=["beta"],
        )
        if hide_labels:
            _commit(
                twin, monkeypatch, publisher, REBASE,
                agent_id="publisher", domain="project", sensitivity="confidential", project_scope=["alpha"],
            )
            _commit(
                twin, monkeypatch, publisher, REBASE,
                agent_id="publisher", domain="legal", sensitivity="internal", project_scope=["alpha"],
            )
        identity = _ident(agent_id)
        first = _lessons(held, monkeypatch, reader, **identity)
        second = _lessons(twin, monkeypatch, reader, **identity)
        assert first == second
        assert first["count"] == 1
        first_events = _events(held)
        second_events = _events(twin)
        assert len(first_events) == 1
        assert len(second_events) == 1
        assert first_events[0]["payload"] == second_events[0]["payload"]
        assert first_events[0]["actor_type"] == second_events[0]["actor_type"] == "agent"
        assert first_events[0]["actor_id"] == second_events[0]["actor_id"] == agent_id


def test_retired_lessons_are_not_returned_and_an_unexpired_one_is(
    vault: Vault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard: a row that is not active is dropped, and the lesson list uses the recall expiry clause.

    Mutation: remove ``status != "active"`` from ``_published_lesson``. The superseded lesson is
    returned and this fails. Mutation: remove the expiry clause from ``list_lesson_memories``. The
    expired lesson is returned and this fails.
    """

    live = _commit(vault, monkeypatch, None, _lesson("rebasing a feature branch onto main", paths=["src/*.py"]))
    superseded = _commit(vault, monkeypatch, None, _lesson("rebasing a feature branch onto main", paths=["src/*.py"]))
    forgotten = _commit(vault, monkeypatch, None, _lesson("rebasing a feature branch onto main", paths=["src/*.py"]))
    expired = _commit(vault, monkeypatch, None, _lesson("rebasing a feature branch onto main", paths=["src/*.py"]))
    redacted = _commit(vault, monkeypatch, None, _lesson("rebasing a feature branch onto main", paths=["src/*.py"]))
    vault.call(monkeypatch, None, "alice_memory_manage", {
        "action": "undo", "memory_id": superseded["memory"]["id"], "reason": "undo the lesson",
    })
    vault.call(monkeypatch, None, "alice_memory_manage", {
        "action": "forget", "memory_id": forgotten["memory"]["id"], "reason": "forget the lesson",
    })
    vault.call(monkeypatch, None, "alice_memory_manage", {
        "action": "expire", "memory_id": expired["memory"]["id"], "reason": "close the window",
        "valid_to": "2000-01-01T00:00:00Z",
    })
    vault.call(monkeypatch, None, "alice_memory_manage", {
        "action": "redact", "memory_id": redacted["memory"]["id"], "reason": "scrub the lesson",
    })
    answer = _lessons(vault, monkeypatch, None)
    assert [item["id"] for item in answer["lessons"]] == [live["memory"]["id"]]
    vault.call(monkeypatch, None, "alice_memory_manage", {
        "action": "unexpire", "memory_id": expired["memory"]["id"], "reason": "open the window",
    })
    restored = _lessons(vault, monkeypatch, None)
    assert {item["id"] for item in restored["lessons"]} == {live["memory"]["id"], expired["memory"]["id"]}


def test_lessons_match_on_text_or_path_and_keep_a_stable_order(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: a path match that also matches the text below the recall head is still ranked as both.

    Mutation: skip the ``only_ids`` stage that asks the recall full-text pass about path matches
    missing from the head. The weak lesson is then ordered as path-only, behind the path-only row,
    and the first id assertion fails.
    """

    neither = _commit(vault, monkeypatch, None, _lesson("glazing a pot", paths=["docs/*.md"]))
    path_only = _commit(vault, monkeypatch, None, _lesson("glazing a pot", paths=["src/*.py"]))
    both = _commit(vault, monkeypatch, None, _lesson("rebasing a feature branch onto main", paths=["src/*.py"]))
    text_only = _commit(vault, monkeypatch, None, _lesson("rebasing a feature branch onto main", paths=["docs/*.md"]))
    answer = _lessons(vault, monkeypatch, None)
    ids = [item["id"] for item in answer["lessons"]]
    assert ids == [both["memory"]["id"], path_only["memory"]["id"], text_only["memory"]["id"]]
    assert neither["memory"]["id"] not in ids
    empty = vault.call(monkeypatch, None, "alice_lessons", {"task": "rename a local variable in the helper"})
    assert empty == {"framing": SESSION_BRIEF_FRAME, "count": 0, "lessons": []}

    # More text matches than the recall head (limit times two), so the weak path-and-text lesson
    # is outside that head and the id-filtered pass is what marks it as a text match.
    for _index in range(11):
        _commit(vault, monkeypatch, None, _lesson("rebase", paths=["docs/*.md"]))
    path_only_crowd = _commit(vault, monkeypatch, None, _lesson("glazing a pot", paths=["crowd/*.py"]))
    weak = _commit(
        vault, monkeypatch, None,
        _lesson("rebase " + ("note " * 40), paths=["crowd/*.py"]),
    )
    with sqlite_user_connection(vault.path, vault.user_id) as conn:
        conn.execute(
            "UPDATE memories SET created_at = ?, updated_at = ? WHERE id = ?",
            ("2001-01-01T00:00:00Z", "2001-01-01T00:00:00Z", weak["memory"]["id"]),
        )
    crowd_paths = ["crowd/a.py"]
    crowded = vault.call(monkeypatch, None, "alice_lessons", {"task": "rebase", "paths": crowd_paths, "limit": 5})
    crowded_ids = [item["id"] for item in crowded["lessons"]]
    assert crowded_ids[0] == weak["memory"]["id"]
    assert crowded_ids[1] == path_only_crowd["memory"]["id"]
    again = vault.call(monkeypatch, None, "alice_lessons", {"task": "rebase", "paths": crowd_paths, "limit": 5})
    assert [item["id"] for item in again["lessons"]] == crowded_ids
    one = vault.call(monkeypatch, None, "alice_lessons", {"task": "rebase", "paths": crowd_paths, "limit": 1})
    assert [item["id"] for item in one["lessons"]] == [weak["memory"]["id"]]
    ten = vault.call(monkeypatch, None, "alice_lessons", {"task": "rebase", "paths": crowd_paths, "limit": 10})
    assert ten["count"] == 10
    default = vault.call(monkeypatch, None, "alice_lessons", {"task": "rebase", "paths": crowd_paths})
    assert default["count"] == 5

    # No text rank, so the newer path match leads and equal times break by id.
    def pottery() -> dict[str, object]:
        return _lesson("glazing a pot", paths=["ties/*.txt"])

    older = _commit(vault, monkeypatch, None, pottery())
    newer = _commit(vault, monkeypatch, None, pottery())
    tied_a = _commit(vault, monkeypatch, None, pottery())
    tied_b = _commit(vault, monkeypatch, None, pottery())
    with sqlite_user_connection(vault.path, vault.user_id) as conn:
        for memory_id, stamp in (
            (older["memory"]["id"], "2010-01-01T00:00:00Z"),
            (newer["memory"]["id"], "2020-01-01T00:00:00Z"),
            (tied_a["memory"]["id"], "2015-01-01T00:00:00Z"),
            (tied_b["memory"]["id"], "2015-01-01T00:00:00Z"),
        ):
            conn.execute(
                "UPDATE memories SET created_at = ?, updated_at = ? WHERE id = ?",
                (stamp, stamp, memory_id),
            )
    tied = vault.call(
        monkeypatch, None, "alice_lessons",
        {"task": "unrelated pottery wheel", "paths": ["ties/a.txt"], "limit": 10},
    )
    tied_ids = [item["id"] for item in tied["lessons"]]
    assert tied_ids == [
        newer["memory"]["id"],
        *sorted((tied_a["memory"]["id"], tied_b["memory"]["id"])),
        older["memory"]["id"],
    ]
    # A stronger text rank leads a newer but weaker text match.
    strong = _commit(vault, monkeypatch, None, _lesson("signalword"))
    weak_text = _commit(vault, monkeypatch, None, _lesson("signalword " + ("note " * 40)))
    with sqlite_user_connection(vault.path, vault.user_id) as conn:
        conn.execute(
            "UPDATE memories SET created_at = ?, updated_at = ? WHERE id = ?",
            ("2011-01-01T00:00:00Z", "2011-01-01T00:00:00Z", strong["memory"]["id"]),
        )
    ranked = vault.call(monkeypatch, None, "alice_lessons", {"task": "signalword", "limit": 2})
    assert [item["id"] for item in ranked["lessons"]] == [strong["memory"]["id"], weak_text["memory"]["id"]]


def test_a_malformed_published_block_is_skipped(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: ``_current_lesson`` skips a stored block that fails the lesson shape check, and the call still returns.

    Mutation: drop the ``block is None`` skip in ``_handle_alice_lessons``. Reading the broken block
    raises and this fails.
    """

    good = _commit(vault, monkeypatch, None, REBASE)
    bad = _commit(vault, monkeypatch, None, _lesson("rebasing a feature branch onto main", paths=["src/*.py"]))
    with sqlite_user_connection(vault.path, vault.user_id) as conn:
        row = conn.execute("SELECT value FROM memories WHERE id = ?", (bad["memory"]["id"],)).fetchone()
        value = json.loads(row["value"]) if isinstance(row["value"], str) else dict(row["value"])
        value["lesson"].pop("do_instead")
        conn.execute("UPDATE memories SET value = ? WHERE id = ?", (json.dumps(value), bad["memory"]["id"]))
    answer = _lessons(vault, monkeypatch, None)
    assert [item["id"] for item in answer["lessons"]] == [good["memory"]["id"]]


@pytest.mark.parametrize("paths", [
    ["/etc/passwd"],
    ["~/secrets"],
    ["../above"],
    ["src/../.."],
    ["C:/temp"],
    ["a\x00file"],
    ["x" * 201],
    ["..\\windows"],
    ["\\\\server\\share\\notes"],
    ["ok"] * 17,
    [""],
    [1],
    "src/feature.py",
])
def test_bad_paths_are_refused_the_way_a_lesson_refuses_them(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, paths: object,
) -> None:
    """Guard: the call uses ``_lesson_paths`` and raises its message.

    Mutation: accept ``paths`` without calling ``_lesson_paths``. These values no longer raise
    and this fails.
    """

    with pytest.raises(MCPArgumentError, match="lesson.paths"):
        vault.call(monkeypatch, None, "alice_lessons", {"task": TASK, "paths": paths})


def test_task_and_limit_and_unknown_arguments_are_refused(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: ``_parse_required_text`` for the task, the 1,000 character check, ``_parse_int`` for the limit, and ``additionalProperties: false``.

    Mutation: stop rejecting an empty task, a task past 1,000 characters, a limit outside 1 to 10,
    or an unknown argument. The matching assertion fails.
    """

    with pytest.raises(MCPArgumentError, match="task is required"):
        vault.call(monkeypatch, None, "alice_lessons", {})
    with pytest.raises(MCPArgumentError, match="task must not be empty"):
        vault.call(monkeypatch, None, "alice_lessons", {"task": "   "})
    with pytest.raises(MCPArgumentError, match="at most 1000"):
        vault.call(monkeypatch, None, "alice_lessons", {"task": "  " + ("x" * 1001)})
    with pytest.raises(MCPArgumentError, match="between 1 and 10"):
        vault.call(monkeypatch, None, "alice_lessons", {"task": TASK, "limit": 0})
    with pytest.raises(MCPArgumentError, match="between 1 and 10"):
        vault.call(monkeypatch, None, "alice_lessons", {"task": TASK, "limit": 11})
    with pytest.raises(MCPArgumentError, match="additional properties"):
        vault.call(monkeypatch, None, "alice_lessons", {"task": TASK, "include_sources": False})
    assert _events(vault) == []
    accepted = vault.call(monkeypatch, None, "alice_lessons", {"task": "x" * 1000})
    assert accepted["count"] == 0
    assert _events(vault)[0]["payload"] == {"returned": 0, "limit": 5, "paths_given": 0}


def test_each_answer_writes_one_event_and_the_export_has_no_task(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Guard: the handler writes one ``agent.lessons_requested`` event whose payload is the three numbers, and nothing stores the task.

    Mutation: add the task text to that payload. The export then contains it and this fails.
    Mutation: skip ``append_event``. The event list is empty and this fails.
    """

    task = "zz-unique-task-phrase-not-stored rebase"
    _commit(vault, monkeypatch, None, _lesson("rebase", paths=["src/*.py"]))
    before = _memory_rows(vault)
    answer = vault.call(monkeypatch, None, "alice_lessons", {"task": task, "paths": CALL_PATHS})
    assert answer["count"] == 1
    assert _memory_rows(vault) == before
    events = _events(vault)
    assert len(events) == 1
    assert events[0]["actor_type"] == "user"
    assert events[0]["actor_id"] is None
    assert events[0]["payload"] == {"returned": 1, "limit": 5, "paths_given": 1}
    vault.call(monkeypatch, None, "alice_lessons", {"task": task, "paths": CALL_PATHS})
    assert len(_events(vault)) == 2

    admin = vault.key("admin", "admin_agent")
    keyed = vault.call(monkeypatch, admin, "alice_lessons", {"task": task, "paths": CALL_PATHS, **_ident("admin")})
    assert keyed["count"] == 1
    keyed_events = [event for event in _events(vault) if event["actor_id"] == "admin"]
    assert len(keyed_events) == 1
    assert keyed_events[0]["actor_type"] == "agent"
    assert keyed_events[0]["payload"]["returned"] == 1

    export_path = tmp_path / "export.jsonl"
    subprocess.run(
        [sys.executable, "-m", "alicebot_api.onramp", "export", "--db", str(vault.path),
         "--user-id", str(vault.user_id), "--out", str(export_path)],
        capture_output=True, text=True, check=True,
    )
    assert "zz-unique-task-phrase-not-stored" not in export_path.read_text(encoding="utf-8")


def test_the_tool_is_on_the_full_list_only_and_other_definitions_stay(
    vault: Vault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard: alice_lessons is a full-surface core tool, and the other core definitions are the ones already on main.

    Mutation: add alice_lessons to ``_DEFAULT_CORE_TOOL_ORDER``. It appears on the default list and this fails.
    Mutation: change another core tool's definition. The digest of the other twelve no longer matches and this fails.
    """

    monkeypatch.delenv("ALICE_MCP_FULL_TOOLS", raising=False)
    monkeypatch.delenv("ALICE_MCP_LEGACY_TOOLS", raising=False)
    default_names = [str(tool["name"]) for tool in list_mcp_tools()]
    assert default_names == ["alice_memory_commit", "alice_recall", "alice_resume"]
    with pytest.raises(MCPToolNotFoundError):
        call_mcp_tool(vault.context(), name="alice_lessons", arguments={"task": TASK})
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    full = {str(tool["name"]): tool for tool in list_mcp_tools()}
    assert "alice_lessons" in full
    assert full["alice_lessons"]["annotations"] == full["alice_recall"]["annotations"]
    others = [tool for tool in _CORE_TOOL_DEFINITIONS if tool["name"] != "alice_lessons"]
    payload = json.dumps(others, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(payload).hexdigest() == _OTHER_CORE_DIGEST


def test_recall_resume_and_context_pack_do_not_gain_lesson_fields(
    vault: Vault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard: this unit does not change the three existing reads.

    The recall result keeps the keys it had, and none of the three answers carry a lesson block
    or a publisher. A change to those handlers that adds either field fails this.
    """

    _commit(vault, monkeypatch, None, REBASE)
    recall = vault.call(monkeypatch, None, "alice_recall", {"query": TASK, "limit": 5, "include_sources": False})
    assert set(recall["results"][0]) == {
        "confidence", "domain", "id", "provenance_count", "score", "status", "text", "type", "writer",
    }
    assert "lesson" not in json.dumps(recall)
    resume = vault.call(monkeypatch, None, "alice_resume", {})
    pack = vault.call(monkeypatch, None, "alice_context_pack", {"query": TASK})
    for payload in (resume, pack):
        assert "published" not in payload
        assert "applies_when" not in json.dumps(payload)

    admin = vault.key("admin", "admin_agent", "alpha")
    scoped = vault.key("scoped", "project_scoped_agent", "alpha")
    _commit(
        vault, monkeypatch, admin, REBASE,
        agent_id="admin", domain="project", sensitivity="internal", project_scope=["alpha"],
    )
    for key, agent_id in ((admin, "admin"), (scoped, "scoped")):
        identity = _ident(agent_id)
        recalled = vault.call(monkeypatch, key, "alice_recall", {"query": "rebase feature branch", **identity})
        assert set(recalled["results"][0]) == {
            "confidence", "domain", "id", "provenance_count", "score", "status", "text", "type", "writer",
        }
        resumed = vault.call(monkeypatch, key, "alice_resume", identity)
        packed = vault.call(monkeypatch, key, "alice_context_pack", {"query": "rebase feature branch", **identity})
        for payload in (recalled, resumed, packed):
            assert "applies_when" not in json.dumps(payload)


def test_one_call_runs_the_same_number_of_statements_at_two_vault_sizes(
    vault: Vault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard: candidate revisions are read in one statement, not one statement per lesson.

    Mutation: replace ``list_revisions_for_memories`` with a per-id ``list_revisions`` loop.
    The statement count then grows from the 100-lesson vault to the 2,000-lesson vault and this fails.
    """

    seeded = _commit(vault, monkeypatch, None, _lesson("budgetlesson", paths=["budget/*.py"]))
    _clone_published_lessons(vault, seeded["memory"]["id"], 99)
    small = _statement_count(vault, monkeypatch)
    _clone_published_lessons(vault, seeded["memory"]["id"], 1900)
    large = _statement_count(vault, monkeypatch)
    assert small == large
    assert small > 0


def _clone_published_lessons(vault: Vault, template_id: str, count: int) -> None:
    with sqlite_user_connection(vault.path, vault.user_id) as conn:
        memory = dict(conn.execute("SELECT * FROM memories WHERE id = ?", (template_id,)).fetchone())
        revision = dict(conn.execute(
            "SELECT * FROM memory_revisions WHERE memory_id = ? ORDER BY revision_number ASC LIMIT 1",
            (template_id,),
        ).fetchone())
        memory_columns = list(memory)
        revision_columns = list(revision)
        for index in range(count):
            memory_id = str(uuid4())
            revision_id = str(uuid4())
            copied = dict(memory)
            copied["id"] = memory_id
            copied["memory_key"] = f"budget-{memory_id}"
            copied["canonical_text"] = f"Applies when: budgetlesson {index}"
            copied["title"] = f"budgetlesson {index}"
            copied["created_at"] = f"2024-01-01T00:00:{index % 60:02d}Z"
            value = json.loads(copied["value"]) if isinstance(copied["value"], str) else dict(copied["value"])
            value["lesson"] = _lesson("budgetlesson", paths=["budget/*.py"])
            value["text"] = copied["canonical_text"]
            copied["value"] = json.dumps(value)
            conn.execute(
                f"INSERT INTO memories ({', '.join(memory_columns)}) VALUES ({', '.join('?' for _ in memory_columns)})",
                tuple(copied[column] for column in memory_columns),
            )
            copied_revision = dict(revision)
            copied_revision["id"] = revision_id
            copied_revision["memory_id"] = memory_id
            new_value = copied_revision.get("new_value")
            if isinstance(new_value, str):
                parsed = json.loads(new_value)
                parsed["lesson"] = value["lesson"]
                copied_revision["new_value"] = json.dumps(parsed)
            conn.execute(
                f"INSERT INTO memory_revisions ({', '.join(revision_columns)}) "
                f"VALUES ({', '.join('?' for _ in revision_columns)})",
                tuple(copied_revision[column] for column in revision_columns),
            )


def _statement_count(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> int:
    import alicebot_api.mcp.runtime as runtime

    statements: list[str] = []
    real = runtime.sqlite_user_connection

    @contextmanager
    def tracing(path, user_id):
        with real(path, user_id) as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(runtime, "sqlite_user_connection", tracing)
    answer = vault.call(
        monkeypatch, None, "alice_lessons",
        {"task": "budgetlesson", "paths": ["budget/a.py", "budget/b.py"], "limit": 5},
    )
    assert answer["count"] == 5
    # SQLite reports its own index reads with a leading comment. Those grow with the
    # index. The store statements the call sends do not.
    return sum(1 for sql in statements if not sql.lstrip().startswith("--"))
