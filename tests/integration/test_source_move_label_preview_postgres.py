"""Owner source moves preview real admission loss and regenerate fresh candidates."""
import json
from copy import deepcopy
from uuid import UUID, uuid4

import anyio
import pytest

import alicebot_api.main as main
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.routers import vnext_memories as router
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_capture import VNextCaptureService
from alicebot_api.vnext_store import PostgresVNextStore

ALPHA = "prj_" + "a" * 16
BETA = "prj_" + "b" * 16


def _request(path, payload, raw_key):
    messages = []
    body = json.dumps(payload).encode()
    received = False
    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}
    async def send(message):
        messages.append(message)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "headers": [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json"), (b"authorization", f"Bearer {raw_key}".encode())], "client": ("127.0.0.1", 50000), "server": ("testserver", 80), "root_path": ""}
    anyio.run(main.app, scope, receive, send)
    status = next(item["status"] for item in messages if item["type"] == "http.response.start")
    return status, b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")


def _fixture(url):
    user = uuid4()
    with user_connection(url, user) as conn:
        ContinuityStore(conn).create_user(user, f"move-{user}@example.test", "Synthetic")
        store = PostgresVNextStore(conn)
        result = VNextCaptureService(store, defer_embeddings=True).capture_text("I prefer synthetic blue ink.\nTODO: Review the synthetic draft", domain="project", sensitivity="public", project_scope=[ALPHA])
        source = store.get_source(str(result.source_id))
        report = store.create_artifact({"artifact_type": "daily_brief", "title": "Synthetic report", "content_markdown": "Synthetic report", "domain": "project", "sensitivity": "public", "metadata_json": {"source_ids": [str(result.source_id)], "project_scope": [ALPHA]}})
    return user, source, report


def _snapshot(url, user, source):
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        rows = {table: store._fetch_all(f"SELECT * FROM {table} ORDER BY id") for table in ("sources", "memories", "open_loops", "generated_artifacts", "provenance_links", "event_log", "graph_edges")}
    return rows


def test_source_move_preview_is_zero_write_then_confirmation_preserves_provenance(migrated_database_urls, monkeypatch):
    url = migrated_database_urls["app"]
    user, source, report = _fixture(url)
    monkeypatch.setattr(router, "get_settings", lambda: Settings(database_url=url))
    before = _snapshot(url, user, source)
    request = router.VNextSourceReviewRequest(user_id=user, action="assign_project", project_id=BETA)
    preview = router.review_vnext_source(UUID(str(source["id"])), request)
    payload = json.loads(preview.body)
    assert preview.status_code == 200 and payload["preview"] is True
    assert payload["derived_rows_hidden_from_project_keys"] == len(before["memories"]) + 1
    assert payload["confirm_required"] is True
    assert _snapshot(url, user, source) == before
    confirmed = router.review_vnext_source(UUID(str(source["id"])), request.model_copy(update={"confirm_label_hide": True}))
    assert confirmed.status_code == 200
    after = _snapshot(url, user, source)
    assert after["provenance_links"] == before["provenance_links"]
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        assert store.get_source(str(source["id"]))["metadata_json"]["project_scope"] == [BETA]
        moved_report = store.get_artifact(str(report["id"]))
        assert BETA in moved_report["metadata_json"]["project_floor"]
        assert moved_report["metadata_json"]["source_ids"] == report["metadata_json"]["source_ids"]


def test_regeneration_uses_moved_source_without_lowering_or_rewriting_originals(migrated_database_urls, monkeypatch):
    url = migrated_database_urls["app"]
    user, source, report = _fixture(url)
    monkeypatch.setattr(router, "get_settings", lambda: Settings(database_url=url))
    move = router.VNextSourceReviewRequest(user_id=user, action="assign_project", project_id=BETA, confirm_label_hide=True, sensitivity="confidential", domain="health")
    assert router.review_vnext_source(UUID(str(source["id"])), move).status_code == 200
    before = _snapshot(url, user, source)
    result = router.regenerate_vnext_source(UUID(str(source["id"])), router.VNextSourceRegenerateRequest(user_id=user), authorization=None)
    payload = json.loads(result.body)
    assert result.status_code == 201
    assert payload["memory_count"] >= 1 and payload["open_loop_count"] == 1
    after = _snapshot(url, user, source)
    assert after["sources"] == before["sources"]
    assert after["generated_artifacts"] == before["generated_artifacts"]
    for table in ("memories", "open_loops", "provenance_links"):
        by_id = {row["id"]: row for row in after[table]}
        assert all(by_id[row["id"]] == row for row in before[table])
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        for kind, ids in (("memory", payload["memory_ids"]), ("open_loop", payload["open_loop_ids"])):
            rows = store.read_label_rows(kind, ids)
            assert len(rows) == len(ids)
            for row in rows:
                assert row["domain"] == "health" and row["sensitivity"] == "confidential"
                assert row["metadata_json"]["project_scope"] == [BETA]
                assert row["metadata_json"]["project_floor"] == [BETA]
                links = store._fetch_all("SELECT source_id FROM provenance_links WHERE target_id = %s", (str(row["id"]),))
                assert {str(link["source_id"]) for link in links} == {str(source["id"])}


@pytest.mark.parametrize("profile,scope,expected", [("trusted_local_agent", None, 403), ("admin_agent", ALPHA, 403), ("admin_agent", None, 201)])
def test_regeneration_authenticates_and_refuses_trusted_or_bound_keys(migrated_database_urls, monkeypatch, profile, scope, expected):
    url = migrated_database_urls["app"]
    user, source, _report = _fixture(url)
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        _key, raw = create_agent_key(store, user_id=user, agent_id="synthetic-reader", permission_profile=profile, project_scope=scope)
    settings = Settings(database_url=url, app_env="test")
    monkeypatch.setattr(router, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    before = _snapshot(url, user, source)
    status, response = _request(f"/v0/vnext/sources/{source['id']}/regenerate", {"user_id": str(user)}, raw)
    assert status == expected, response
    if expected == 403:
        after = _snapshot(url, user, source)
        for table in ("sources", "memories", "open_loops", "generated_artifacts", "provenance_links"):
            assert after[table] == before[table]
