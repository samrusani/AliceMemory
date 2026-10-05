"""Synthetic, role-separated fixtures for the label atomicity acceptance tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import time
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import psycopg
from psycopg.rows import dict_row
import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_store import PostgresVNextStore


def invoke(method, path, *, user_id, payload=None, key=None):
    """Invoke the mounted HTTP application, preserving response headers."""
    messages = []
    body = json.dumps(payload).encode() if payload is not None else b""
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    headers = [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")]
    if key:
        headers.append((b"authorization", f"Bearer {key}".encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": urlencode({"user_id": str(user_id)}).encode(),
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    anyio.run(main_module.app, scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    raw = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return int(start["status"]), json.loads(raw), dict(start.get("headers", []))


@dataclass
class LabelHarness:
    urls: dict[str, str]
    user_id: UUID

    @contextmanager
    def store(self) -> Iterator[PostgresVNextStore]:
        with user_connection(self.urls["app"], self.user_id) as conn:
            yield PostgresVNextStore(conn)

    def request(self, method, path, *, payload=None, key=None):
        if payload is not None:
            payload = {"user_id": str(self.user_id), **payload}
        return invoke(method, path, user_id=self.user_id, payload=payload, key=key)

    def relabel(self, kind, row_id, **labels):
        # Call the real owner handler independently of the middleware's key-provisioning gate.
        from alicebot_api.routers import vnext_memories as router

        if kind == "source":
            result = router.review_vnext_source(
                UUID(str(row_id)), router.VNextSourceReviewRequest(user_id=self.user_id, action="update", **labels)
            )
        else:
            result = router.review_vnext_memory(
                UUID(str(row_id)),
                router.VNextMemoryReviewRequest(user_id=self.user_id, action="edit", **labels),
                authorization=None,
            )
        return (
            result.status_code,
            json.loads(result.body),
            {key.encode(): value.encode() for key, value in result.headers.items()},
        )

    def key(self, profile, *, project=None):
        with self.store() as store:
            _record, raw = create_agent_key(
                store, user_id=self.user_id, agent_id=str(uuid4()), permission_profile=profile, project_scope=project
            )
        return raw

    def source(self, *, scope=(), text="TODO: Synthetic acceptance task", sensitivity="public"):
        with self.store() as store:
            return store.create_source(
                {
                    "source_type": "note",
                    "title": "Synthetic acceptance",
                    "content_hash": str(uuid4()),
                    "domain": "project",
                    "sensitivity": sensitivity,
                    "metadata_json": {"project_scope": list(scope), "raw_text": text},
                }
            )

    def memory(self, *, source=None, parents=(), scope=()):
        metadata = {"project_scope": list(scope)}
        if source is not None:
            metadata["source_id"] = str(source["id"])
        if parents:
            metadata["consolidation"] = {"cluster_member_ids": [str(row["id"]) for row in parents]}
        with self.store() as store:
            return store.create_memory(
                {
                    "memory_key": str(uuid4()),
                    "canonical_text": "Synthetic acceptance memory",
                    "status": "active",
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": metadata,
                }
            )

    def snapshot(self):
        with self.store() as store:
            result = {}
            with store.conn.cursor() as cur:
                for table in ("sources", "memories", "open_loops", "generated_artifacts", "projects", "event_log"):
                    cur.execute(f"SELECT row_to_json(t) FROM {table} t ORDER BY id")  # closed table names
                    result[table] = [row["row_to_json"] for row in cur.fetchall()]
            return result

    def label_locks(self):
        # Monitoring through a fresh app connection avoids disturbing the transaction under test.
        with psycopg.connect(self.urls["app"], autocommit=True, row_factory=dict_row) as conn:
            return conn.execute(
                "SELECT pid, mode, granted FROM pg_locks WHERE locktype='advisory' AND classid=(hashtext('vnext_labels')::bigint & 4294967295) AND objid=(hashtext(%s)::bigint & 4294967295)",
                (str(self.user_id),),
            ).fetchall()

    def wait_exclusive(self, *, timeout=2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            rows = self.label_locks()
            if any(row["mode"] == "ExclusiveLock" and not row["granted"] for row in rows):
                return rows
            time.sleep(0.01)
        raise AssertionError(f"no waiting exclusive label lock in pg_locks: {self.label_locks()}")

    def wait_relabel(self, *, timeout=2.0):
        """A review may serialise the relabel on S or L, in that order."""
        end = time.monotonic() + timeout
        with psycopg.connect(self.urls["app"], autocommit=True, row_factory=dict_row) as conn:
            while time.monotonic() < end:
                rows = conn.execute(
                    "SELECT pid, mode, granted FROM pg_locks WHERE locktype='advisory' "
                    "AND classid IN ((hashtext('vnext_labels')::bigint & 4294967295), "
                    "(hashtext('vnext_supersession')::bigint & 4294967295)) "
                    "AND objid=(hashtext(%s)::bigint & 4294967295)",
                    (str(self.user_id),),
                ).fetchall()
                if any(row["mode"] == "ExclusiveLock" and not row["granted"] for row in rows):
                    return rows
                time.sleep(0.01)
        raise AssertionError(f"no waiting graph or label relabel lock: {rows}")


@pytest.fixture
def label_harness(migrated_database_urls, monkeypatch):
    from alicebot_api import vnext_label_writes
    from alicebot_api.routers import vnext_memories, vnext_projects, vnext_retrieval, vnext_review, workspaces

    settings = Settings(database_url=migrated_database_urls["app"])
    for module in (main_module, vnext_memories, vnext_projects, vnext_retrieval, vnext_review, workspaces):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(vnext_label_writes, "STRICT_LOCK_ORDER", True)
    monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    harness = LabelHarness(migrated_database_urls, uuid4())
    with harness.store() as store:
        ContinuityStore(store.conn).create_user(
            harness.user_id, f"labels-{harness.user_id}@example.invalid", "Synthetic acceptance"
        )
    return harness


def today():
    return datetime.now(UTC).date().isoformat()


def assert_raised(row, *, domain="health", sensitivity="confidential"):
    assert row["domain"] == domain
    assert row["sensitivity"] == sensitivity


def join_thread(thread, failures, *, timeout=6.0):
    thread.join(timeout)
    assert not thread.is_alive(), "worker did not finish within the acceptance deadline"
    assert not failures, repr(failures)
