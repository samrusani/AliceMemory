"""Each door that refills after the label drop gives a key with limits a full page when hidden rows sit in front, on PostgreSQL.

New after v0.20.2. The scenarios of ``tests/unit/test_label_refill_doors.py`` on a real PostgreSQL store. Recall and the
context pack run through the MCP tool registry and a minted key, with a loopback embeddings endpoint. ``GET /v0/vnext/projects``,
``GET /v0/vnext/context-tree`` and ``GET /v0/vnext/workspace`` are the mounted HTTP routes, called with a minted key.

A trusted key reads rows up to ``private``, and a row that sits three memories below a confidential source is above that
ceiling although its own stored labels are not, so the store's filters keep it and only the final check takes it out. An
unbound admin key is the control: it is read once, at the limit of each list, and is shown the rows first.

Each test names the mutation that must fail it, and the same edits fail the SQLite and stub-store tests of the unit file.
Replacing the flag with ``True`` at the projects route or in the workspace changes only how many rows the store is asked for
when the caller is the owner or an unbound admin key, which no answer shows, so the unit tests pin it by the limit the stub
store records.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import anyio
import pytest

import alicebot_api.main as main_module
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.test_recall_fallback_hidden_rows_postgres import _Postgres
from tests.unit.test_label_refill_doors import (
    embeddings,
    pack_gives_a_full_temporal_stage,
    pack_gives_a_full_vector_stage,
    recall_gives_a_full_page,
    stored_memory,
)
from alicebot_api.vnext_label_writes import without_insert_floor
from tests.unit.test_recall_fallback_hidden_rows import _memory, _source


def test_recall_gives_a_key_with_limits_a_full_page_when_hidden_nearest_rows_sit_in_front(
    label_harness, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Mutation: the edit of the SQLite test of this name."""

    vault = _Postgres(label_harness, monkeypatch, "read_only")
    with embeddings(monkeypatch) as provider:
        recall_gives_a_full_page(label_harness, vault.call, label_harness.store, provider, vault.key)


def test_the_context_pack_vector_stage_is_full_for_a_key_with_limits_when_hidden_nearest_rows_sit_in_front(
    label_harness, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Mutation: the edit of the SQLite test of this name."""

    vault = _Postgres(label_harness, monkeypatch, "read_only")
    with embeddings(monkeypatch) as provider:
        pack_gives_a_full_vector_stage(label_harness, vault.call, label_harness.store, provider, vault.key)


def test_the_context_pack_temporal_stage_is_full_for_a_key_with_limits_when_hidden_nearest_rows_sit_in_front(
    label_harness, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Mutation: the edit of the SQLite test of this name."""

    vault = _Postgres(label_harness, monkeypatch, "read_only")
    pack_gives_a_full_temporal_stage(label_harness, vault.call, label_harness.store, vault.key)


def _get(harness: Any, path: str, key: str, **query: str) -> tuple[int, dict[str, Any]]:
    """``GET`` ``path`` through the mounted application, with a query string the harness does not take."""

    messages: list[dict[str, Any]] = []
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": urlencode({"user_id": str(harness.user_id), **query}).encode(),
        "headers": [(b"host", b"127.0.0.1:8000"), (b"authorization", f"Bearer {key}".encode())],
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80), "root_path": "",
    }
    anyio.run(main_module.app, scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    raw = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return int(start["status"]), json.loads(raw)


def test_the_projects_route_gives_a_key_with_limits_a_full_page_when_hidden_projects_sit_in_front(
    label_harness,  # noqa: F811
) -> None:
    """``GET /v0/vnext/projects`` (limit 20) lists the 20 newest projects a trusted key may read when 30 projects above its
    ceiling were updated more recently. An unbound admin key is listed the 20 newest of all.

    Mutation: the edits of the stub-store test of this name.
    """

    trusted, admin = label_harness.key("trusted_local_agent"), label_harness.key("admin_agent")
    with label_harness.store() as store:
        for index in range(25):
            store.create_project({"name": f"visible-{index}", "slug": f"visible-{index}", "domain": "project", "sensitivity": "public"})
    with label_harness.store() as store:
        for index in range(30):
            store.create_project({"name": f"zqhidden-{index}", "slug": f"zqhidden-{index}", "domain": "project", "sensitivity": "confidential"})

    status, body, _headers = label_harness.request("GET", "/v0/vnext/projects", key=trusted)
    assert status == 200
    assert body["count"] == 20
    assert all(item["name"].startswith("visible-") for item in body["items"]), body["items"]
    assert "zqhidden" not in json.dumps(body)
    status, body, _headers = label_harness.request("GET", "/v0/vnext/projects", key=admin)
    assert status == 200
    assert body["count"] == 20
    assert all(item["name"].startswith("zqhidden-") for item in body["items"]), body["items"]


def _chain_below_a_confidential_source(store: Any) -> str:
    """The id of a memory three memories below a confidential source: public by its own labels, above a trusted key's ceiling."""

    parent = _source(store, "parent-confidential", sensitivity="confidential")
    first = _memory(store, "chain-first", "chain first", {"source_id": str(parent["id"])})
    second = _memory(store, "chain-second", "chain second", {"consolidation": {"cluster_member_ids": [str(first["id"])]}})
    return str(second["id"])


def test_the_context_tree_route_gives_a_key_with_limits_a_full_page_of_memories_and_events(
    label_harness,  # noqa: F811
) -> None:
    """``GET /v0/vnext/context-tree`` (limit 12) lists the 12 memories that match the query and the 12 newest events a trusted
    key may read when 40 memories below a confidential source, and the events about them, come first. An unbound admin key
    is read once, at 12, with the levels of its request: it is shown no memory and one event.

    Mutation: the edits of the stub-store test of this name.
    """

    trusted, admin = label_harness.key("trusted_local_agent"), label_harness.key("admin_agent")
    with label_harness.store() as store:
        for index in range(14):
            _memory(store, f"visible-{index}", f"zqtree visible {index}", {})
    with label_harness.store() as store:
        second = _chain_below_a_confidential_source(store)
    with label_harness.store() as store:
        for index in range(40):
            _memory(store, f"hidden-{index}", f"zqtree zqhidden {index}", {"consolidation": {"cluster_member_ids": [second]}})

    def labels(body: dict[str, Any], root: str) -> list[str]:
        return [child["label"] for node in body["roots"] if node["id"] == f"root:{root}" for child in node["children"]]

    status, body = _get(label_harness, "/v0/vnext/context-tree", trusted, query="zqtree")
    assert status == 200
    assert len(labels(body, "memories")) == 12
    assert all(label.startswith("visible-") for label in labels(body, "memories")), labels(body, "memories")
    assert body["summary"]["events"] == 12
    assert "zqhidden" not in json.dumps(body)
    status, body = _get(label_harness, "/v0/vnext/context-tree", admin, query="zqtree")
    assert status == 200
    assert body["summary"]["memories"] == 0
    # The twelve newest events are the events about the hidden memories, which the levels of the request leave out, and the
    # event the first request wrote about its own tree. The page is not widened to older events.
    assert labels(body, "events") == ["context_tree.generated"]


def test_the_workspace_gives_a_key_with_limits_a_full_page_of_the_lists_hidden_rows_come_first_in(
    label_harness,  # noqa: F811
) -> None:
    """``GET /v0/vnext/workspace`` lists the 30 newest review memories and the 30 newest open loops a trusted key may read
    when 40 of each below a confidential source were written after them. An unbound admin key is listed the 30 newest of all.

    Mutation: the edits of the stub-store test of this name (the lists of the review memories and of the open loops).
    """

    trusted, admin = label_harness.key("trusted_local_agent"), label_harness.key("admin_agent")
    with label_harness.store() as store:
        for index in range(32):
            stored_memory(store, f"visible-{index}", f"visible review memory {index}", {}, status="candidate")
            store.create_open_loop({"title": f"visible-{index}", "domain": "project", "sensitivity": "public", "status": "open", "metadata_json": {}})
    with label_harness.store() as store:
        second = _chain_below_a_confidential_source(store)
    with label_harness.store() as store:
        for index in range(40):
            leaf = stored_memory(store, f"zqhidden-{index}", f"zqhidden review memory {index}",
                                 {"consolidation": {"cluster_member_ids": [second]}}, status="candidate")
            with without_insert_floor():
                store.create_open_loop({
                    "title": f"zqhidden-{index}", "domain": "project", "sensitivity": "public", "status": "open",
                    "metadata_json": {"derived_from": {"memories": [str(leaf["id"])], "counts": {"memories": 1}}},
                })

    status, body, _headers = label_harness.request("GET", "/v0/vnext/workspace", key=trusted)
    assert status == 200
    assert len(body["review_memories"]) == 30 and len(body["open_loops"]) == 30
    assert all(row["title"].startswith("visible-") for row in body["review_memories"]), [row["title"] for row in body["review_memories"]]
    assert all(row["title"].startswith("visible-") for row in body["open_loops"])
    assert "zqhidden" not in json.dumps(body)
    status, body, _headers = label_harness.request("GET", "/v0/vnext/workspace", key=admin)
    assert status == 200
    assert all(row["title"].startswith("zqhidden-") for row in body["review_memories"]), [row["title"] for row in body["review_memories"]]
    assert all(row["title"].startswith("zqhidden-") for row in body["open_loops"])
