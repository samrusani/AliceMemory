"""A report is read behind the label it was stored with, so the label must cover what it names.

A consolidation run that proposes only roll-up cards over confidential memories used to store its report with
sensitivity ``unknown``: the label was taken over the near-duplicate clusters, which such a run does not have. The
report prints the card topic and the ids of the memories behind it, so a ``trusted_local_agent`` key (a ceiling
below confidential) read both through ``GET /v0/vnext/artifacts/{id}``.

These tests run the real scheduler path on role-separated Postgres and read the stored report back over HTTP as a
key with a ceiling below the memories and as a key with no ceiling.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from uuid import UUID, uuid4

import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.routers import vnext_review as vnext_review_router
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_scheduler import SchedulerRunRequest
from alicebot_api.vnext_scheduler_runtime import run_now_durable
from alicebot_api.vnext_store import PostgresVNextStore

from tests.integration.test_vnext_live_workspace_api import invoke_request, seed_user

ALLOWED_WITH_CONFIDENTIAL = ("public", "internal", "private", "confidential", "unknown")
GAME_TEXTS = (
    ("I played Hollow Knight for 25 hours", "2023-06-02"),
    ("I played Stardew Valley for 85 hours", "2023-06-20"),
    ("I played Celeste for 10 hours", "2023-07-01"),
)


@pytest.fixture(autouse=True)
def _no_embedding_provider(monkeypatch):
    for name in ("ALICE_EMBEDDINGS_BASE_URL", "ALICE_EMBEDDINGS_MODEL", "ALICE_EMBEDDINGS_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _point_routes_at(monkeypatch, database_url: str) -> None:
    settings = Settings(database_url=database_url)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(vnext_review_router, "get_settings", lambda: settings)


def _seed_games(store: PostgresVNextStore, *, sensitivity: str, domain: str = "personal") -> list[dict]:
    rows = []
    for index, (text, day) in enumerate(GAME_TEXTS):
        rows.append(
            store.create_memory(
                {
                    "memory_key": f"games.{sensitivity}.{domain}.{index}.{uuid4().hex[:8]}",
                    "memory_type": "episode",
                    "title": text,
                    "canonical_text": text,
                    "summary": text,
                    "status": "active",
                    "value": {"text": text},
                    "domain": domain,
                    "sensitivity": sensitivity,
                    "metadata_json": {"session_date": day},
                }
            )
        )
    return rows


def _keys(database_url: str, user_id: UUID) -> tuple[str, str]:
    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        _trusted_record, trusted_key = create_agent_key(
            store, user_id=user_id, agent_id="trusted-reader", permission_profile="trusted_local_agent"
        )
        _admin_record, admin_key = create_agent_key(
            store, user_id=user_id, agent_id="admin-reader", permission_profile="admin_agent"
        )
    return trusted_key, admin_key


def _get_artifact(artifact_id: str, user_id: UUID, key: str) -> tuple[int, dict]:
    return invoke_request(
        "GET",
        f"/v0/vnext/artifacts/{artifact_id}",
        authorization=f"Bearer {key}",
        query_params={"user_id": str(user_id)},
    )


def _run_consolidation(database_url: str, user_id: UUID) -> dict:
    result = run_now_durable(
        database_url=database_url,
        user_id=user_id,
        request=SchedulerRunRequest(
            workflow_type="memory_consolidation",
            sensitivity_allowed=ALLOWED_WITH_CONFIDENTIAL,
            generated_for=datetime.now(UTC).date().isoformat(),
        ),
    )
    assert result["run"]["status"] == "succeeded", result
    return result["artifact"]


def test_rollup_only_report_over_confidential_memories_is_refused_to_a_trusted_agent(
    migrated_database_urls, monkeypatch
) -> None:
    database_url = migrated_database_urls["app"]
    _point_routes_at(monkeypatch, database_url)
    user_id = seed_user(database_url, email="rollup-report-label@example.com")
    with user_connection(database_url, user_id) as conn:
        members = _seed_games(PostgresVNextStore(conn), sensitivity="confidential")
    trusted_key, admin_key = _keys(database_url, user_id)

    artifact = _run_consolidation(database_url, user_id)

    rollups = artifact["metadata_json"]["rollups"]
    assert len(rollups["proposals"]) == 1, "the run must propose a roll-up card and nothing else"
    assert artifact["metadata_json"]["consolidation"]["cluster_membership"] == []
    assert "hours played" in artifact["content_markdown"]
    member_ids = {str(row["id"]) for row in members}
    assert member_ids <= set(rollups["groups"][0]["member_ids"])

    artifact_id = str(artifact["id"])
    status, body = _get_artifact(artifact_id, user_id, trusted_key)
    assert status == 403, body
    assert "restricted_sensitivity_filtered" in body["policy_decision"]["reasons"]
    leaked = json.dumps(body)
    assert "hours played" not in leaked
    assert not any(member_id in leaked for member_id in member_ids)

    status, body = _get_artifact(artifact_id, user_id, admin_key)
    assert status == 200, body
    assert "hours played" in body["content_markdown"]
    assert artifact["sensitivity"] == "confidential"


def test_rollup_only_report_over_private_memories_is_refused_to_a_read_only_agent(
    migrated_database_urls, monkeypatch
) -> None:
    """The same gap at the default ceiling: no opt-in to confidential is needed for a read-only key
    (public, internal, unknown) to read a topic built from private memories."""
    database_url = migrated_database_urls["app"]
    _point_routes_at(monkeypatch, database_url)
    user_id = seed_user(database_url, email="rollup-report-private@example.com")
    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        _seed_games(store, sensitivity="private")
        _record, read_only_key = create_agent_key(
            store, user_id=user_id, agent_id="read-only-reader", permission_profile="read_only_agent"
        )
        _record, trusted_key = create_agent_key(
            store, user_id=user_id, agent_id="trusted-reader", permission_profile="trusted_local_agent"
        )

    result = run_now_durable(
        database_url=database_url,
        user_id=user_id,
        request=SchedulerRunRequest(
            workflow_type="memory_consolidation", generated_for=datetime.now(UTC).date().isoformat()
        ),
    )
    artifact = result["artifact"]
    assert len(artifact["metadata_json"]["rollups"]["proposals"]) == 1

    status, body = _get_artifact(str(artifact["id"]), user_id, read_only_key)
    assert status == 403, body
    assert "hours played" not in json.dumps(body)
    status, body = _get_artifact(str(artifact["id"]), user_id, trusted_key)
    assert status == 200, body
    assert artifact["sensitivity"] == "private"


def test_open_loop_review_naming_a_confidential_source_is_refused_to_a_trusted_agent(
    migrated_database_urls, monkeypatch
) -> None:
    """Same class in the open-loop review: the report prints the id of the source each loop links, and the loop
    can be less sensitive than its source."""
    database_url = migrated_database_urls["app"]
    _point_routes_at(monkeypatch, database_url)
    user_id = seed_user(database_url, email="open-loop-review-label@example.com")
    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        source = store.create_source(
            {
                "source_type": "note",
                "title": "Counsel note",
                "content_hash": "sha256:" + uuid4().hex,
                "domain": "personal",
                "sensitivity": "confidential",
            }
        )
        store.create_open_loop(
            {
                "title": "Reply to the filing",
                "status": "open",
                "domain": "personal",
                "sensitivity": "internal",
                "source_id": str(source["id"]),
            },
            actor_type="user",
        )
    trusted_key, admin_key = _keys(database_url, user_id)

    result = run_now_durable(
        database_url=database_url,
        user_id=user_id,
        request=SchedulerRunRequest(
            workflow_type="open_loop_review",
            sensitivity_allowed=ALLOWED_WITH_CONFIDENTIAL,
            generated_for=datetime.now(UTC).date().isoformat(),
        ),
    )
    artifact = result["artifact"]
    source_id = str(source["id"])
    assert result["run"]["status"] == "succeeded", result
    assert f"source:{source_id}" in artifact["content_markdown"]

    status, body = _get_artifact(str(artifact["id"]), user_id, trusted_key)
    assert status == 403, body
    assert source_id not in json.dumps(body)

    status, body = _get_artifact(str(artifact["id"]), user_id, admin_key)
    assert status == 200, body
    assert f"source:{source_id}" in body["content_markdown"]
    assert artifact["sensitivity"] == "confidential"
