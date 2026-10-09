"""Redacting a memory that a promotion made, with or without an undo first.

Promoting a daily brief to a memory appends ``artifact.promoted_to_memory``, an event aimed at the brief whose whole
payload is the id of the new memory. The append-only trigger lets a redaction rewrite an event aimed at an artifact only
for a project update coupled to the memory, so the redaction of a promoted copy used to abort on that event with
``event_log is append-only`` and answer a server error. These tests run the routes and the tool of the mounted
application with an admin key, and ask the whole database, not a list of tables, whether the text of the copy is still
anywhere after the redaction.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import psycopg
import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_stores.memory_lifecycle_common import REDACTION_MARKER, is_redacted_memory
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401

# Words of the copy that a search can match. The sentinels are never in a query, because a door echoes its query.
WORDS = "zebracrossing marmalade"
STATES = ("live", "forgotten", "undone")


class Copy:
    """One memory that holds a sentinel only it holds, and the ids that name it."""

    def __init__(self, harness, key: str, memory_id: str, *, artifact_id: str | None) -> None:
        self.h = harness
        self.key = key
        self.memory_id = memory_id
        self.artifact_id = artifact_id
        self.text = ""
        self.reason = ""
        # A line of the brief the copy was made from. The brief keeps it; no other row should once the copy is redacted.
        self.initial = ""

    def edit(self) -> None:
        """Give the memory words that neither the brief nor any other row holds."""
        self.text = f"ZQXTEXT{uuid4().hex[:12]}"
        self.reason = f"ZQXWHY{uuid4().hex[:12]}"
        status, body, _ = self.h.request(
            "POST",
            f"/v0/vnext/memories/{self.memory_id}/review",
            payload={"action": "edit", "canonical_text": f"Copy note {self.text} about {WORDS}", "title": f"Title {self.text}"},
            key=self.key,
        )
        assert status == 200, body

    def retire(self, state: str) -> None:
        if state == "live":
            return
        verb = {"forgotten": "forget", "undone": "undo"}[state]
        status, body, _ = self.h.request(
            "POST", f"/v0/vnext/memories/{verb}", payload={"memory_id": self.memory_id, "reason": f"why {self.reason}"}, key=self.key
        )
        assert status == 200, body

    def redact_over_http(self) -> dict:
        status, body, _ = self.h.request(
            "POST", "/v0/vnext/memories/redact", payload={"memory_id": self.memory_id, "reason": f"why {self.reason}"}, key=self.key
        )
        assert status == 200, body
        return body

    def redact_over_tool(self, monkeypatch) -> dict:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", self.key)
        context = MCPRuntimeContext(database_url=self.h.urls["app"], user_id=UUID(str(self.h.user_id)))
        return call_mcp_tool(
            context,
            name="alice_memory_manage",
            arguments={"action": "redact", "memory_id": self.memory_id, "reason": f"why {self.reason}"},
        )

    def memory(self) -> dict:
        with self.h.store() as store:
            return store.get_memory_for_redaction(self.memory_id)


class Vault:
    """A project with a daily brief made through the route, and an admin key."""

    def __init__(self, harness) -> None:
        self.h = harness
        self.alpha = str(uuid4())
        with harness.store() as store:
            store.create_project({"id": self.alpha, "name": "Atlas", "slug": "atlas", "domain": "project", "sensitivity": "public"})
            for index in range(3):
                text = f"Atlas played Hades for {25 + index} hours. MEMORY{index}"
                store.create_memory(
                    {
                        "memory_key": f"alpha.game.{index}", "memory_type": "episode", "title": text, "canonical_text": text,
                        "summary": text, "value": {"text": text}, "status": "active", "domain": "project",
                        "sensitivity": "public", "created_at": "2026-10-05T09:00:00Z",
                        "metadata_json": {"project_scope": [self.alpha], "session_date": f"2026-10-0{index + 1}"},
                    }
                )
        self.admin = harness.key("admin_agent")

    def promoted_copy(self) -> Copy:
        status, body, _ = self.h.request(
            "POST",
            "/v0/vnext/artifacts/generate/daily-brief",
            payload={
                "scope": {"projects": [self.alpha]},
                "options": {"generated_for": "2026-10-05", "reference_time": "2026-10-05T12:00:00Z", "memory_limit": 50},
            },
            key=self.admin,
        )
        assert status == 201, body
        artifact = body.get("artifact", body)
        assert artifact["artifact_type"] == "daily_brief"
        status, body, _ = self.h.request(
            "POST", f"/v0/vnext/artifacts/{artifact['id']}/review", payload={"action": "promote"}, key=self.admin
        )
        assert status == 200, body
        copy = Copy(self.h, self.admin, str(body["promoted_memory_id"]), artifact_id=str(artifact["id"]))
        copy.initial = max(str(artifact["content_markdown"]).splitlines(), key=len)
        assert len(copy.initial) > 40
        copy.edit()
        return copy

    def plain_copy(self) -> Copy:
        with self.h.store() as store:
            memory = store.create_memory(
                {
                    "memory_key": f"plain.{uuid4()}", "memory_type": "semantic", "title": "Plain", "canonical_text": "Plain",
                    "status": "active", "domain": "project", "sensitivity": "public",
                    "metadata_json": {"project_scope": [self.alpha]},
                }
            )
        copy = Copy(self.h, self.admin, str(memory["id"]), artifact_id=None)
        copy.edit()
        return copy


def holders(urls: dict[str, str], needle: str) -> dict[str, int]:
    """Every table that holds the text in any column of any row, found by the whole database and not by a list."""
    found: dict[str, int] = {}
    with psycopg.connect(urls["admin"], autocommit=True) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        ]
        for table in tables:
            count = conn.execute(f'SELECT count(*) FROM "{table}" AS t WHERE row_to_json(t)::text LIKE %s', (f"%{needle}%",)).fetchone()[0]
            if count:
                found[table] = count
    return found


def promotion_events(h, artifact_id: str) -> list[dict]:
    with h.store() as store:
        return [
            event for event in store.list_events(target_type="artifact", target_id=artifact_id)
            if event["event_type"] == "artifact.promoted_to_memory"
        ]


def trigger_source(urls: dict[str, str]) -> str:
    with psycopg.connect(urls["admin"], autocommit=True) as conn:
        return conn.execute("SELECT pg_get_functiondef('app.reject_event_log_mutation()'::regprocedure)").fetchone()[0]


@pytest.mark.parametrize("door", ("http", "tool"))
@pytest.mark.parametrize("state", STATES)
def test_a_promoted_copy_is_redacted_whatever_state_it_is_in(label_harness, monkeypatch, state, door):
    h = label_harness
    copy = Vault(h).promoted_copy()
    assert {"memories", "memory_revisions", "event_log"} <= set(holders(h.urls, copy.text)), "the scan must see the text"
    assert {"generated_artifacts", "memory_revisions"} <= set(holders(h.urls, copy.initial)), "the scan must see the brief text"
    copy.retire(state)
    if state != "live":
        assert holders(h.urls, copy.reason), "the scan must see the reason"

    result = copy.redact_over_http() if door == "http" else copy.redact_over_tool(monkeypatch)

    assert result["status"] == "redacted"
    assert result["forgotten_first"] is (state == "live")
    assert result["idempotent_replay"] is False
    assert holders(h.urls, copy.text) == {}
    assert holders(h.urls, copy.reason) == {}
    # The text the copy was made with is still in the brief it was copied from, as the text of a plain memory is still in
    # its source, and in no other row.
    assert set(holders(h.urls, copy.initial)) == {"generated_artifacts"}
    memory = copy.memory()
    assert is_redacted_memory(memory)
    assert memory["canonical_text"] == REDACTION_MARKER and memory["title"] == REDACTION_MARKER


def test_the_plain_path_leaves_nothing_in_the_same_state(label_harness):
    """The control: a memory that no promotion made is redacted after an undo and leaves nothing either."""
    h = label_harness
    copy = Vault(h).plain_copy()
    copy.retire("undone")
    assert holders(h.urls, copy.text)
    assert copy.redact_over_http()["status"] == "redacted"
    assert holders(h.urls, copy.text) == {}
    assert holders(h.urls, copy.reason) == {}


def test_the_promotion_record_and_the_trigger_are_left_as_they_were(label_harness):
    h = label_harness
    copy = Vault(h).promoted_copy()
    trigger_before = trigger_source(h.urls)
    (record_before,) = promotion_events(h, copy.artifact_id)
    assert record_before["payload_json"] == {"memory_id": copy.memory_id}
    copy.retire("undone")

    copy.redact_over_http()

    assert trigger_source(h.urls) == trigger_before
    (record_after,) = promotion_events(h, copy.artifact_id)
    assert record_after == record_before
    # The record holds the id of the memory and no text, so leaving it is not leaving the copy.
    assert copy.text not in json.dumps(record_after, default=str)
    # The trigger still refuses to rewrite it, even in redaction mode and in the exact shape a redaction writes.
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SAVEPOINT rewrite_the_promotion_record")
        try:
            cur.execute("SELECT set_config('app.redaction_in_progress', 'on', false)")
            with pytest.raises(psycopg.errors.RaiseException, match="event_log is append-only"):
                cur.execute(
                    "UPDATE event_log SET payload_json = jsonb_build_object("
                    "'redacted', true, 'memory_id', %s::text, 'event_type', event_type), integrity_hash = NULL "
                    "WHERE id = %s::uuid",
                    (copy.memory_id, str(record_after["id"])),
                )
        finally:
            cur.execute("ROLLBACK TO SAVEPOINT rewrite_the_promotion_record")
            cur.execute("RELEASE SAVEPOINT rewrite_the_promotion_record")


@pytest.mark.parametrize("door", ("http", "tool"))
def test_a_second_redaction_of_a_promoted_copy_is_a_replay_that_writes_nothing(label_harness, monkeypatch, door):
    h = label_harness
    copy = Vault(h).promoted_copy()
    copy.retire("undone")
    redact = copy.redact_over_http if door == "http" else (lambda: copy.redact_over_tool(monkeypatch))
    first = redact()
    assert first["idempotent_replay"] is False
    before = h.snapshot()

    second = redact()

    assert second["status"] == "redacted"
    assert second["idempotent_replay"] is True
    assert second["redacted_events"] == 0
    # An admin key's replay appends no policy event and no receipt: nothing in the vault changes.
    assert h.snapshot() == before


def test_no_door_returns_the_text_of_a_redacted_promoted_copy(label_harness, monkeypatch):
    h = label_harness
    vault = Vault(h)
    copy = vault.promoted_copy()
    monkeypatch.setenv("ALICE_AGENT_API_KEY", vault.admin)
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=UUID(str(h.user_id)))

    def doors() -> dict[str, str]:
        answers = {}
        for name, arguments in (
            ("alice_recall", {"query": WORDS}),
            ("alice_context_pack", {"query": WORDS}),
            ("alice_resume", {"query": WORDS}),
            ("alice_recent_decisions", {"query": WORDS}),
            ("alice_explain", {"memory_id": copy.memory_id}),
        ):
            try:
                answers[f"tool {name}"] = json.dumps(call_mcp_tool(context, name=name, arguments=arguments), default=str)
            except Exception as exc:  # a refusal or a missing row holds no text either
                answers[f"tool {name}"] = type(exc).__name__ + str(exc)
        for name, method, path, payload in (
            ("memory audit", "GET", f"/v0/vnext/memories/{copy.memory_id}/audit", None),
            ("context pack", "POST", "/v0/vnext/context-packs", {"query": WORDS}),
            ("workspace", "GET", "/v0/vnext/workspace", None),
            ("memory recent commits", "GET", "/v0/vnext/memories/recent-commits", None),
        ):
            try:
                status, body, _ = h.request(method, path, payload=payload, key=vault.admin)
            except Exception as exc:
                answers[f"http {name}"] = type(exc).__name__ + str(exc)
                continue
            answers[f"http {name}"] = json.dumps({"status": status, "body": body}, default=str)
        return answers

    live = doors()
    assert any(copy.text in answer for answer in live.values()), "a door must be able to show the text before the redaction"
    copy.retire("undone")
    copy.redact_over_http()

    for name, answer in doors().items():
        assert copy.text not in answer, name
        assert copy.reason not in answer, name


def test_an_artifact_event_that_holds_more_than_the_id_is_not_left_behind(label_harness):
    """The record is left because it holds nothing but an id. An artifact event that holds text is not left."""
    h = label_harness
    copy = Vault(h).promoted_copy()
    leaked = f"ZQXNOTE{uuid4().hex[:12]}"
    with h.store() as store:
        append_event(
            store,
            event_type="artifact.noted",
            actor_type="user",
            target_type="artifact",
            target_id=copy.artifact_id,
            payload={"memory_id": copy.memory_id, "note": leaked},
        )
    before = copy.memory()

    with pytest.raises(psycopg.errors.RaiseException, match="event_log is append-only"):
        copy.redact_over_http()

    # The redaction is all or nothing: it left the memory as it was and wrote no receipt.
    assert copy.memory() == before
    assert holders(h.urls, copy.text)
    with h.store() as store:
        assert not [event for event in store.list_events(target_type="memory", target_id=copy.memory_id) if event["event_type"] == "memory.redacted"]
