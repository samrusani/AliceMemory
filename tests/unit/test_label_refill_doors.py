"""Each door that refills after the label drop gives a key with limits a full page when hidden rows sit in front.

New after v0.20.2. ``test_label_page_refill.py`` runs ``page_after_label_drop`` and the stage methods with the flag set.
That leaves the place where each door sets the flag unchecked: a door that stops passing it still passes every test that
sets the flag by hand. The tests here go in by the door. A key with limits is shown the page it would be shown by the same
vault without the hidden rows, and a control shows the hidden rows sit in front, because the owner or an unbound admin key,
who are read once at the limit they asked for, is shown them first.

* recall (``alice_recall``), the vector stage of the context pack and its temporal-anchor stage: a real SQLite vault, a
  minted key and the MCP tool registry. A hidden row is made from a source in a domain the key may not read, and its own
  stored labels are inside the key's limits, so the store's filters keep it and only the final check takes it out. The
  embeddings endpoint is a loopback server whose vectors put every hidden row nearest to the query.
* ``GET /v0/vnext/projects``, ``GET /v0/vnext/context-tree`` and the workspace: PostgreSQL routes, so the routes run whole
  on a stub store whose lists put hidden rows first. ``tests/integration/test_label_refill_doors_postgres.py`` runs the
  same doors on a real store.

The last test pins the boundary. The page is the first rows the key may read among the 128 nearest, so it is full while the
hidden rows in front number at most 128 minus the rows the stage asks for. ``alice_recall`` asks its vector stage for twice
its limit, so at limit 5 the answer and the trace match the vault without the hidden rows up to 118 hidden rows, differ at
119, fall to one candidate at 127 and to none at 128.

Each test names the mutation that must fail it.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from alicebot_api.config import Settings
from alicebot_api.routers import workspaces
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY, AgentIdentity
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MAX_INPUT_CHARS_ENV,
    EMBEDDINGS_MODEL_ENV,
    get_embedding_provider,
    memory_embedding_text,
    signed_memory_embedding_update,
)
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_label_sql import READABLE_SCAN_CAP
from alicebot_api.vnext_label_writes import without_insert_floor
from tests.unit.test_complete_readable_counts import PopulationStore, _quiet_services, _row
from tests.unit.test_recall_fallback_hidden_rows import _memory, _source, _Vault
from tests.unit.test_vnext_main import FakeVNextStore, _install_fake_vnext_store, _invoke_vnext_request

HIDDEN = 50
NEAR = "zqnear"
LIMIT = 5
RECALL_WINDOW = LIMIT * 2
PACK_ITEMS = 6
PACK_WINDOW = PACK_ITEMS * 2
Call = Callable[..., dict[str, Any]]
Stores = Callable[[], AbstractContextManager[Any]]


def vector_of(text: str) -> list[float]:
    """The query and every hidden row hold ``NEAR``, so they share one vector. A readable row says ``farN`` and sits a
    little further out for each N; a text with neither is the furthest."""

    if NEAR in text:
        return [1.0, 0.0, 0.0]
    found = re.search(r"far(\d+)", text)
    if found:
        return [1.0, (int(found.group(1)) + 1) / 100.0, 0.0]
    return [0.0, 1.0, 0.0]


class Endpoint:
    """An OpenAI-shaped ``/v1/embeddings`` endpoint on 127.0.0.1. It answers ``vector_of`` and nothing else."""

    def __init__(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                inputs = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))["input"]
                body = json.dumps(
                    {"data": [{"index": index, "embedding": vector_of(text)} for index, text in enumerate(inputs)]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    def __enter__(self) -> Endpoint:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@contextmanager
def embeddings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Point the process at the loopback endpoint and give back the provider the retrieval service will build."""

    with Endpoint() as endpoint:
        monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, endpoint.base_url)
        monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, "synthetic-embed")
        monkeypatch.delenv(EMBEDDINGS_API_KEY_ENV, raising=False)
        monkeypatch.delenv(EMBEDDINGS_MAX_INPUT_CHARS_ENV, raising=False)
        provider = get_embedding_provider()
        assert provider is not None
        yield provider


def embedded_memory(store: Any, provider: Any, key: str, text: str, metadata: dict[str, object]) -> dict[str, object]:
    """A memory the store cannot hide by its own labels, with the vector ``vector_of`` gives its text."""

    row = _memory(store, key, text, metadata)
    store.update_memory_embedding(
        **signed_memory_embedding_update(row, vector_of(memory_embedding_text(row)), provider=provider)
    )
    return row


def stored_memory(store: Any, key: str, text: str, metadata: dict[str, object], **fields: object) -> dict[str, object]:
    """A public project memory written the way ``_memory`` writes one, with the fields of ``fields`` set on the row."""

    with without_insert_floor():
        row = store.create_memory({
            "memory_key": key, "canonical_text": text, "title": key, "memory_type": "decision", "status": "active",
            "domain": "project", "sensitivity": "public", "metadata_json": metadata, **fields,
        })
    append_event(store, event_type="memory.labels_raised", actor_type="system",
                 target_type="memory", target_id=str(row["id"]), payload={"cause": "repair_v3"})
    return dict(row)


def add_visible(stores: Stores, provider: Any, count: int = 14) -> None:
    with stores() as store:
        for index in range(count):
            embedded_memory(store, provider, f"visible-{index}", f"ledger far{index}", {})


def add_hidden(stores: Stores, provider: Any, count: int, *, first: int = 0) -> list[str]:
    """Memories with the stored labels of a readable row and the text of the query, below a source in a domain the
    ``read_only`` key may not read. Only the final check can take them out."""

    with stores() as store:
        parent = _source(store, f"parent-health-{first}", domain="health")
        return [
            str(embedded_memory(store, provider, f"hidden-{index}", f"{NEAR} hidden {index}", {"source_id": str(parent["id"])})["id"])
            for index in range(first, first + count)
        ]


def recall(call: Call, key: str | None, **extra: object) -> dict[str, Any]:
    return call(key, "alice_recall", query=NEAR, limit=LIMIT, debug=True, **extra)


def pack(call: Call, key: str | None, query: str = NEAR, **extra: object) -> dict[str, Any]:
    return call(key, "alice_context_pack", query=query, max_items=PACK_ITEMS, debug=True, **extra)


def recall_gives_a_full_page(call: Call, stores: Stores, provider: Any, key: str, hidden: int = HIDDEN) -> None:
    add_visible(stores, provider)
    before = recall(call, key)
    assert before["count"] == LIMIT
    assert before["retrieval"]["stages"]["vector"] == {"status": "enabled", "candidate_count": RECALL_WINDOW}
    ids = add_hidden(stores, provider, hidden)
    owner = recall(call, None, sensitivity_allowed=list(ALL_SENSITIVITY))
    assert {item["id"] for item in owner["results"]} <= set(ids), "the hidden rows are the nearest, so the owner is shown them"
    after = recall(call, key)
    assert after == before
    assert not set(ids) & set(re.findall(r"[0-9a-f-]{36}", json.dumps(after, default=str)))


def pack_gives_a_full_vector_stage(call: Call, stores: Stores, provider: Any, key: str, hidden: int = HIDDEN) -> None:
    add_visible(stores, provider)
    before = pack(call, key)
    assert len(before["memories"]) == PACK_ITEMS
    assert before["trace"]["stages"]["vector"] == {"status": "enabled", "candidate_count": PACK_WINDOW}
    ids = add_hidden(stores, provider, hidden)
    owner = pack(call, None, sensitivity_allowed=list(ALL_SENSITIVITY))
    assert {item["id"] for item in owner["memories"]} <= set(ids), "the hidden rows are the nearest, so the owner is shown them"
    after = pack(call, key)
    assert after["trace"]["stages"]["vector"] == before["trace"]["stages"]["vector"]
    assert [item["id"] for item in after["memories"]] == [item["id"] for item in before["memories"]]
    assert not set(ids) & set(re.findall(r"[0-9a-f-]{36}", json.dumps(after, default=str)))


TIME_QUERY = "what happened in March 2025"


def pack_gives_a_full_temporal_stage(call: Call, stores: Stores, key: str, hidden: int = HIDDEN) -> None:
    """Rows dated at the middle of March are nearest to the window of the query. The readable rows are dated earlier in
    the month, so they follow. No embeddings endpoint is set, so the vector stage is off, and no row shares a word with the
    query, so only the temporal-anchor stage finds them."""

    with stores() as store:
        for index in range(14):
            stored_memory(store, f"visible-{index}", f"ledger entry {index}", {}, valid_from=f"2025-03-{2 + index:02d}T00:00:00+00:00")
    before = pack(call, key, TIME_QUERY)
    assert len(before["memories"]) == PACK_ITEMS
    assert before["trace"]["stages"]["temporal_anchor"]["candidate_count"] == PACK_WINDOW
    assert before["trace"]["stages"]["vector"]["candidate_count"] == 0
    with stores() as store:
        parent = _source(store, "parent-health", domain="health")
        ids = [
            str(stored_memory(store, f"hidden-{index}", f"quiet note {index}", {"source_id": str(parent["id"])},
                              valid_from="2025-03-16T12:00:00+00:00")["id"])
            for index in range(hidden)
        ]
    owner = pack(call, None, TIME_QUERY, sensitivity_allowed=list(ALL_SENSITIVITY))
    assert {item["id"] for item in owner["memories"]} <= set(ids), "the hidden rows are the nearest, so the owner is shown them"
    after = pack(call, key, TIME_QUERY)
    assert after["trace"]["stages"]["temporal_anchor"] == before["trace"]["stages"]["temporal_anchor"]
    assert [item["id"] for item in after["memories"]] == [item["id"] for item in before["memories"]]
    assert not set(ids) & set(re.findall(r"[0-9a-f-]{36}", json.dumps(after, default=str)))


def test_recall_gives_a_key_with_limits_a_full_page_when_hidden_nearest_rows_sit_in_front(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With 50 hidden rows nearest to the query, ``alice_recall`` at limit 5 answers the five results and the trace it
    answers in a vault without them.

    Mutation: delete ``label_refill=source_fence.entity_read_fenced,`` from the ``_memory_vector_rows`` call in
    ``_handle_alice_recall`` (``mcp/retrieval.py``). The vector stage then takes ten rows, drops all of them, and recall
    answers no result where the vault without the hidden rows answers five.
    """

    vault = _Vault(tmp_path, monkeypatch)
    with embeddings(monkeypatch) as provider:
        recall_gives_a_full_page(vault.call, vault.store, provider, vault.key("read_only"))


def test_the_context_pack_vector_stage_is_full_for_a_key_with_limits_when_hidden_nearest_rows_sit_in_front(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With 50 hidden rows nearest to the query, the vector stage of the context pack keeps its twelve candidates.

    Mutation: delete ``label_refill=source_fence.entity_read_fenced,`` from the ``_memory_vector_rows`` call in
    ``compile_context_pack`` (``vnext_retrieval.py``). The stage then reports no candidate where the vault without the
    hidden rows reports twelve.
    """

    vault = _Vault(tmp_path, monkeypatch)
    with embeddings(monkeypatch) as provider:
        pack_gives_a_full_vector_stage(vault.call, vault.store, provider, vault.key("read_only"))


def test_the_context_pack_temporal_stage_is_full_for_a_key_with_limits_when_hidden_nearest_rows_sit_in_front(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With 50 hidden rows dated at the middle of the window, the temporal-anchor stage of the context pack keeps its twelve
    candidates.

    Mutation: delete ``label_refill=source_fence.entity_read_fenced,`` from the ``_memory_temporal_rows`` call in
    ``compile_context_pack`` (``vnext_retrieval.py``). The stage then reports no candidate where the vault without the
    hidden rows reports twelve.
    """

    vault = _Vault(tmp_path, monkeypatch)
    pack_gives_a_full_temporal_stage(vault.call, vault.store, vault.key("read_only"))


def test_the_boundary_is_the_cap_minus_the_rows_the_stage_asks_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recall at limit 5 asks its vector stage for ten rows, so the hidden rows in front of the page may number 118, which is
    128 minus ten. At 118 the answer and the trace are those of the vault without them. At 119 the window holds nine
    readable rows and the trace says nine. At 127 it holds one, and at 128 none. In no case is a hidden row shown.

    Mutation: replace ``window = cap if refill and want < cap else want`` in ``page_after_label_drop`` with
    ``window = cap + 1 if refill and want < cap else want``. The window then holds one readable row more than 128 minus the
    hidden rows leave, so the 119 case reports ten candidates (the same trace as the vault without the hidden rows).
    """

    vault = _Vault(tmp_path, monkeypatch)
    with embeddings(monkeypatch) as provider:
        key = vault.key("read_only")
        add_visible(vault.store, provider, count=RECALL_WINDOW + 2)
        before = recall(vault.call, key)
        assert before["count"] == LIMIT
        hidden_ids = add_hidden(vault.store, provider, READABLE_SCAN_CAP - RECALL_WINDOW)
        assert recall(vault.call, key) == before, "118 hidden rows: the last count at which the page is the same"
        hidden_ids += add_hidden(vault.store, provider, 1, first=len(hidden_ids))
        at_119 = recall(vault.call, key)
        assert at_119 != before, "119 hidden rows: the first count at which the window is short"
        assert at_119["retrieval"]["stages"]["vector"] == {"status": "enabled", "candidate_count": RECALL_WINDOW - 1}
        hidden_ids += add_hidden(vault.store, provider, 8, first=len(hidden_ids))
        at_127 = recall(vault.call, key)
        assert at_127["retrieval"]["stages"]["vector"]["candidate_count"] == 1
        assert at_127["count"] == 1
        hidden_ids += add_hidden(vault.store, provider, 1, first=len(hidden_ids))
        at_128 = recall(vault.call, key)
        assert at_128["retrieval"]["stages"]["vector"]["candidate_count"] == 0
        assert at_128["count"] == 0 and at_128["results"] == []
        for answer in (at_119, at_127, at_128):
            assert not set(hidden_ids) & set(re.findall(r"[0-9a-f-]{36}", json.dumps(answer, default=str)))


# -- the routes that need PostgreSQL, run whole on a stub store ----------------------------------------------------------


def _fake_row(name: str, *, hidden: bool, **fields: object) -> dict[str, object]:
    return {
        "id": str(uuid4()), "name": name, "title": name, "slug": name, "domain": "project",
        "sensitivity": "confidential" if hidden else "public", "status": "active", "metadata_json": {}, **fields,
    }


def _route(store: FakeVNextStore, path: str, **query: str) -> Callable[[str], tuple[int, dict[str, Any]]]:
    user_id = uuid4()
    _record, trusted = create_agent_key(store, user_id=user_id, agent_id="reader", permission_profile="trusted_local_agent")
    _record, admin = create_agent_key(store, user_id=user_id, agent_id="admin", permission_profile="admin_agent")
    keys = {"trusted": trusted, "admin": admin}

    def get(who: str) -> tuple[int, dict[str, Any]]:
        status, body = _invoke_vnext_request(
            "GET", path, query={"user_id": str(user_id), **query}, authorization=f"Bearer {keys[who]}"
        )
        return status, body  # type: ignore[return-value]

    return get


def test_the_projects_route_gives_a_key_with_limits_a_full_page_when_hidden_projects_sit_in_front(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /v0/vnext/projects?limit=5`` lists five projects a trusted key may read when 30 projects above its ceiling were
    updated more recently, and reads the store once, at 128 rows. An unbound admin key is read once, at five, and is listed
    the five newest.

    Mutation: replace ``refill=SourceReadFence.for_identity(identity).entity_read_fenced`` with ``refill=False`` in
    ``list_vnext_projects`` (``routers/vnext_projects.py``): the trusted key is listed no project. Replace it with
    ``refill=True``: the admin key is listed the same five, and the store is asked for 128 rows instead of five.
    """

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    for index in range(30):
        row = _fake_row(f"hidden-{index}", hidden=True)
        store.projects[str(row["id"])] = row
    for index in range(8):
        row = _fake_row(f"visible-{index}", hidden=False)
        store.projects[str(row["id"])] = row
    limits: list[int] = []
    listed = store.list_projects

    def recording(**kwargs: Any) -> list[dict[str, object]]:
        limits.append(kwargs["limit"])
        return listed(**kwargs)

    monkeypatch.setattr(store, "list_projects", recording)
    get = _route(store, "/v0/vnext/projects", limit="5")

    status, body = get("trusted")
    assert status == 200
    assert [item["name"] for item in body["items"]] == [f"visible-{index}" for index in range(5)]
    assert body["count"] == 5
    assert limits == [READABLE_SCAN_CAP]
    limits.clear()
    status, body = get("admin")
    assert status == 200
    assert [item["name"] for item in body["items"]] == [f"hidden-{index}" for index in range(5)]
    assert limits == [5]


def test_the_context_tree_route_gives_a_key_with_limits_a_full_page_of_every_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /v0/vnext/context-tree?limit=5`` lists five projects, memories, sources, open loops, artifacts and events a
    trusted key may read when 30 rows of each above its ceiling come first. An unbound admin key is read once, at five.
    Its default levels leave out the five hidden rows of each list, so it is shown none, and the page is not widened.

    Mutation: replace ``refill=request.caller_limited`` with ``refill=False`` in ``filled`` (it serves the projects, memories,
    sources, open loops and artifacts) or in the call that serves the events (``vnext_context_tree.py``): those lists of the
    trusted key are empty. Replace it with ``refill=True``, or replace ``caller_limited=SourceReadFence.for_identity(identity)
    .entity_read_fenced`` with ``caller_limited=True`` in ``get_vnext_context_tree`` (``routers/vnext_retrieval.py``): the
    admin key is shown five rows of each list.
    """

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    memory_ids: dict[bool, list[str]] = {True: [], False: []}
    for hidden in (True, False):
        for index in range(30 if hidden else 8):
            tag = "hidden" if hidden else "visible"
            project = _fake_row(f"{tag}-{index}", hidden=hidden)
            store.projects[str(project["id"])] = project
            source = _fake_row(f"{tag}-{index}", hidden=hidden, source_type="note", content_hash=f"{tag}-{index}")
            store.sources[str(source["id"])] = source
            artifact = _fake_row(f"{tag}-{index}", hidden=hidden, artifact_type="note")
            store.artifacts[str(artifact["id"])] = artifact
            store.open_loops.append(_fake_row(f"{tag}-{index}", hidden=hidden, status="open"))
            memory = _fake_row(f"{tag}-{index}", hidden=hidden, canonical_text=f"{tag} {index}")
            store.memories.append(memory)
            memory_ids[hidden].append(str(memory["id"]))
    # An event about a memory is read by the label of that memory, so the events of the hidden memories come first.
    for hidden in (True, False):
        for memory_id in memory_ids[hidden]:
            store.events.append({
                "id": str(uuid4()), "event_type": f"memory.{'hidden' if hidden else 'visible'}", "target_type": "memory",
                "target_id": memory_id, "payload_json": {}, "occurred_at": "2026-10-10T08:00:00+00:00",
            })
    get = _route(store, "/v0/vnext/context-tree", limit="5", include_events="true")

    def labels(body: dict[str, Any]) -> dict[str, list[str]]:
        return {root["id"].removeprefix("root:"): [child["label"] for child in root["children"]] for root in body["roots"]}

    status, body = get("trusted")
    assert status == 200
    shown = labels(body)
    for name in ("projects", "memories", "sources", "open_loops", "artifacts"):
        assert shown[name] == [f"visible-{index}" for index in range(5)], name
    assert shown["events"] == ["memory.visible"] * 5
    assert body["summary"] == {"projects": 5, "memories": 5, "sources": 5, "open_loops": 5, "artifacts": 5, "events": 5}
    status, body = get("admin")
    assert status == 200
    assert body["summary"] == {"projects": 0, "memories": 0, "sources": 0, "open_loops": 0, "artifacts": 0, "events": 0}


# The rows each list of the workspace is asked for.
WORKSPACE_WANTS = {
    "sources": 20, "review_memories": 30, "artifacts": 30, "quality_evals": 50, "projects": 20, "open_loops": 30,
    "beliefs": 12, "tasks": 12, "recent_commits": 20, "inline_confirmations": 20,
}


class WorkspaceStore(PopulationStore):
    """The lists of the workspace, each with hidden rows first. The lists filter nothing, as a store does for a row whose
    stored labels are inside the limits and whose effective labels are not."""

    def __init__(self) -> None:
        super().__init__()
        self.ratings: list[dict[str, object]] = []
        self.beliefs: list[dict[str, object]] = []
        self.tasks: list[dict[str, object]] = []
        self.commits: list[dict[str, object]] = []
        self.confirmations: list[dict[str, object]] = []
        # The limits each list was asked for, by the name the workspace gives the list.
        self.limits: dict[str, list[int]] = {name: [] for name in WORKSPACE_WANTS}

    def _page(self, name: str, rows: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
        self.limits[name].append(limit)
        return rows[:limit]

    def list_sources(self, *, limit, **_kwargs): return self._page("sources", self.rows["source"], limit)  # noqa: E704
    def list_artifacts(self, *, limit, **_kwargs): return self._page("artifacts", self.rows["artifact"], limit)  # noqa: E704
    def list_projects(self, *, limit, **_kwargs): return self._page("projects", self.rows["project"], limit)  # noqa: E704
    def list_open_loops(self, *, limit, **_kwargs): return self._page("open_loops", self.rows["open_loop"], limit)  # noqa: E704
    def list_memories_by_statuses(self, *, limit, **_kwargs): return self._page("review_memories", self.rows["memory"], limit)  # noqa: E704
    def list_artifact_quality_ratings(self, *, limit): return self._page("quality_evals", self.ratings, limit)  # noqa: E704
    def list_beliefs(self, *, limit, **_kwargs): return self._page("beliefs", self.beliefs, limit)  # noqa: E704
    def list_tasks(self, *, limit, **_kwargs): return self._page("tasks", self.tasks, limit)  # noqa: E704
    def list_recent_agentic_commits(self, *, limit): return self._page("recent_commits", self.commits, limit)  # noqa: E704
    def list_pending_inline_confirmations(self, *, limit): return self._page("inline_confirmations", self.confirmations, limit)  # noqa: E704
    def iter_label_ratings(self): return iter([self.ratings])  # noqa: E704
    def count_memories_by_status(self, **_kwargs): return {"candidate": len(self.rows["memory"])}  # noqa: E704
    def count_artifacts_by_status(self, **_kwargs): return {"candidate": len(self.rows["artifact"])}  # noqa: E704
    def count_open_loops_by_status(self, **_kwargs): return {"open": len(self.rows["open_loop"])}  # noqa: E704


def _workspace_lists(body: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        "sources": body["sources"],
        "review_memories": body["review_memories"],
        "artifacts": body["artifacts"],
        "quality_evals": body["quality_evals"],
        "projects": body["projects"],
        "open_loops": body["open_loops"],
        "beliefs": body["beliefs"],
        "tasks": body["tasks"],
        "recent_commits": body["agent_activity"]["recent_commits"],
        "inline_confirmations": body["agent_activity"]["inline_confirmations"],
    }


def _workspace_store() -> WorkspaceStore:
    """Sixty rows above the ceiling of a trusted key come first in every list, and sixty it may read follow."""

    store = WorkspaceStore()

    def rows(prefix: str, *, hidden: bool, status: str = "candidate") -> list[dict[str, object]]:
        return [_row(f"{prefix}-{'hidden' if hidden else 'visible'}-{index}", "confidential" if hidden else "public", status=status) for index in range(60)]

    for kind in ("source", "memory", "artifact", "project"):
        store.rows[kind] = rows(kind, hidden=True) + rows(kind, hidden=False)
    store.rows["open_loop"] = rows("open_loop", hidden=True, status="open") + rows("open_loop", hidden=False, status="open")
    store.ratings = [
        {"id": f"rating-{tag}-{index}", "artifact_id": f"artifact-{tag}-{index}", "metadata_json": {}}
        for tag in ("hidden", "visible") for index in range(60)
    ]
    store.beliefs = [
        {"id": f"belief-{tag}-{index}", "memory_id": f"memory-{tag}-{index}"} for tag in ("hidden", "visible") for index in range(60)
    ]
    store.tasks = rows("task", hidden=True) + rows("task", hidden=False)
    store.commits = rows("commit", hidden=True) + rows("commit", hidden=False)
    store.confirmations = rows("confirmation", hidden=True) + rows("confirmation", hidden=False)
    return store


def _workspace_body(store: WorkspaceStore, monkeypatch: pytest.MonkeyPatch, identity: AgentIdentity) -> dict[str, Any]:
    _quiet_services(monkeypatch)

    @contextmanager
    def connection(_url: str, _user_id: object) -> Iterator[object]:
        yield SimpleNamespace(execute=lambda *args, **kwargs: None)

    monkeypatch.setattr(workspaces, "get_settings", lambda: Settings(database_url="postgresql://db"))
    monkeypatch.setattr(workspaces, "user_connection", connection)
    monkeypatch.setattr(workspaces, "PostgresVNextStore", lambda _conn: store)
    monkeypatch.setattr(workspaces, "resolve_protected_agent_identity", lambda _store, **_kwargs: identity)
    response = workspaces.get_vnext_workspace(user_id=uuid4(), authorization=None)
    assert response.status_code == 200
    return json.loads(response.body)


@pytest.mark.parametrize("name", list(WORKSPACE_WANTS))
def test_the_workspace_gives_a_key_with_limits_a_full_page_of_each_list(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """``GET /v0/vnext/workspace`` fills each of its lists for a trusted key when 60 rows above its ceiling come first, and
    names none of them, reading each list once at 128 rows. An unbound admin key is read once, at the limit of each list, and is
    shown the hidden rows.

    Mutations: replace ``refill=not unfenced`` with ``refill=False`` in ``listed`` (``routers/workspaces.py``): every case
    fails. Replace the call ``listed(...)`` of one list with the call it wraps (for the projects, ``guard.admit_rows("project",
    store.list_projects(status=None, sensitivity_allowed=sensitivity_allowed, limit=20))``): the case of that list fails and
    the others pass. Replace it with ``refill=True``: every admin case fails, because the lists are read at 128 rows.
    """

    want = WORKSPACE_WANTS[name]
    trusted = AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent")
    admin = AgentIdentity(agent_id="admin", permission_profile="admin_agent")
    limited_store, owner_store = _workspace_store(), _workspace_store()
    rows = _workspace_lists(_workspace_body(limited_store, monkeypatch, trusted))[name]
    assert len(rows) == want
    assert all("visible" in str(row["id"]) for row in rows), rows[:2]
    assert not any("hidden" in json.dumps(row) for row in rows)
    assert READABLE_SCAN_CAP in limited_store.limits[name]
    owner_rows = _workspace_lists(_workspace_body(owner_store, monkeypatch, admin))[name]
    assert len(owner_rows) == want
    assert all("hidden" in str(row["id"]) for row in owner_rows), owner_rows[:2]
    assert want in owner_store.limits[name] and READABLE_SCAN_CAP not in owner_store.limits[name]
