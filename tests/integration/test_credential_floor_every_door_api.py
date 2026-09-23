"""The credential floor holds on the Postgres doors, over HTTP, at the store.

Why this file exists. On 2026-09-22 (ticket S4.4) these were reproduced on
the shipped code against a throwaway Postgres 16: POST
/v0/vnext/memories/correct and the review edit wrote a GitHub token into an
active row (door 3); /v1/memory/operations/candidates/generate plus commit
turned "Decision: rotate the deploy token to <token>" into an active Decision
that /v1/continuity/brief and /v0/continuity/recall then returned (door 4);
and promoting an artifact made the token an active human_curated memory
(door 5).

How they escaped. The floor's tests assert at evaluate_promotion, and none
of these routes calls it. Every test here asserts at the store after the
request: the row that must not exist, or the row that must not change.
The unit twins, for the paths SQLite also runs, are in
tests/unit/test_credential_floor_every_door.py.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.credential_floor import (
    RATIONALE_WITHHELD_PLACEHOLDER,
    TEXT_WITHHELD_PLACEHOLDER,
    CredentialActivationRefused,
)
from alicebot_api.db import user_connection
from alicebot_api.routers import continuity as continuity_router
from alicebot_api.routers import vnext_memories as vnext_memories_router
from alicebot_api.routers import vnext_review as vnext_review_router
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_projects import ProjectAutomationRequest, VNextProjectService
from alicebot_api.vnext_store import PostgresVNextStore

from tests.integration.test_memory_mutations_api import identity_header, invoke_request, seed_user


# Built rather than written out so the source carries no scanner-shaped token.
PAT = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
PROJECT_KEY = "sk-" + "proj-" + "Tq3Z0vW8rK4nW8sL3zB6cD1fG5hJ0kLa9M2x"


def _use_database(monkeypatch, database_url: str) -> None:
    for module in (main_module, continuity_router, vnext_memories_router, vnext_review_router):
        monkeypatch.setattr(module, "get_settings", lambda: Settings(database_url=database_url))


def _scalar(conn: Any, statement: str) -> Any:
    with conn.cursor() as cur:
        cur.execute(statement)
        row = cur.fetchone()
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def _count(database_url: str, user_id: UUID, table: str) -> int:
    with user_connection(database_url, user_id) as conn:
        return int(_scalar(conn, f"SELECT count(*) FROM {table}"))


def _pat_rows(database_url: str, user_id: UUID) -> int:
    """Rows anywhere in the memory and continuity tables whose text carries the token."""

    with user_connection(database_url, user_id) as conn:
        return int(
            _scalar(
                conn,
                "SELECT"
                f" (SELECT count(*) FROM memories WHERE concat_ws(' ', title, canonical_text, summary, value::text)"
                f" LIKE '%{PAT}%')"
                f" + (SELECT count(*) FROM continuity_objects WHERE concat_ws(' ', title, body::text) LIKE '%{PAT}%')"
                f" + (SELECT count(*) FROM memory_operation_candidates WHERE candidate_payload::text LIKE '%{PAT}%')"
                f" + (SELECT count(*) FROM continuity_capture_events WHERE raw_content LIKE '%{PAT}%')",
            )
        )


def _memory(database_url: str, user_id: UUID, memory_id: str) -> dict[str, Any]:
    with user_connection(database_url, user_id) as conn:
        row = PostgresVNextStore(conn).get_memory(memory_id)
    assert row is not None
    return dict(row)


# ---------------------------------------------------------------------------
# Door 2 and door 3 over HTTP.
# ---------------------------------------------------------------------------


def test_door2_split_credential_commit_over_http_leaves_no_row(migrated_database_urls, monkeypatch) -> None:
    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-door2@example.com")

    status, payload = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={"user_id": str(user_id), "title": "id AK", "canonical_text": "IAIOSFODNN7EXAMPLE is the access key id"},
    )

    assert status == 200
    assert payload["status"] == "rejected"
    assert _count(app_url, user_id, "memories") == 0


def test_door3_http_correct_confirm_and_review_edit_leave_rows_unchanged(migrated_database_urls, monkeypatch) -> None:
    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-door3@example.com")
    uid = str(user_id)

    status, clean = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={"user_id": uid, "title": "deploy", "canonical_text": "Deploys go out on Tuesdays."},
    )
    assert status == 201
    active_id = clean["memory"]["id"]
    status, pending = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={
            "user_id": uid,
            "title": "deploy day",
            "canonical_text": "Deploys may move to Wednesdays.",
            "confidence": 0.8,
        },
    )
    assert status == 201
    assert pending["status"] == "confirmation_required"
    pending_id = pending["memory"]["id"]
    active_before = _memory(app_url, user_id, active_id)
    pending_before = _memory(app_url, user_id, pending_id)

    status, _ = invoke_request(
        "POST",
        "/v0/vnext/memories/correct",
        payload={"user_id": uid, "memory_id": active_id, "canonical_text": f"use {PAT} for deploys"},
    )
    assert status == 400
    status, _ = invoke_request(
        "POST",
        f"/v0/vnext/memories/{active_id}/review",
        payload={"user_id": uid, "action": "edit", "canonical_text": f"use {PAT} for deploys"},
    )
    assert status == 400
    status, _ = invoke_request(
        "POST",
        "/v0/vnext/memories/confirm",
        payload={
            "user_id": uid,
            "confirmation_id": pending["confirmation_id"],
            "action": "edit",
            "canonical_text": f"use {PAT} for deploys",
        },
    )
    assert status == 400

    assert _memory(app_url, user_id, active_id) == active_before
    assert _memory(app_url, user_id, pending_id) == pending_before
    assert _pat_rows(app_url, user_id) == 0

    # Guards the guard: the same three routes accept ordinary text, so the
    # 400s above are the floor and not a malformed request.
    status, _ = invoke_request(
        "POST",
        "/v0/vnext/memories/correct",
        payload={"user_id": uid, "memory_id": active_id, "canonical_text": "Deploys go out on Mondays."},
    )
    assert status == 200
    status, _ = invoke_request(
        "POST",
        f"/v0/vnext/memories/{active_id}/review",
        payload={"user_id": uid, "action": "edit", "canonical_text": "Deploys go out on Fridays."},
    )
    assert status == 200
    status, confirmed = invoke_request(
        "POST",
        "/v0/vnext/memories/confirm",
        payload={
            "user_id": uid,
            "confirmation_id": pending["confirmation_id"],
            "action": "edit",
            "canonical_text": "Deploys move to Thursdays.",
        },
    )
    assert status == 200
    assert confirmed["status"] == "committed"


def test_round2_a_title_only_review_edit_is_read_against_the_stored_body(migrated_database_urls, monkeypatch) -> None:
    """Design ruling T1-F2 (2026-09-23): the row as it will be stored."""

    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-f2-review@example.com")
    uid = str(user_id)
    status, committed = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={"user_id": uid, "title": "deploy", "canonical_text": "IAIOSFODNN7EXAMPLE is the deploy key id"},
    )
    assert status == 201
    memory_id = committed["memory"]["id"]
    before = _memory(app_url, user_id, memory_id)

    status, _ = invoke_request(
        "POST",
        f"/v0/vnext/memories/{memory_id}/review",
        payload={"user_id": uid, "action": "edit", "title": "id AK"},
    )

    assert status == 400
    assert _memory(app_url, user_id, memory_id) == before
    # Guards the guard: a clean title-only edit goes through.
    status, _ = invoke_request(
        "POST",
        f"/v0/vnext/memories/{memory_id}/review",
        payload={"user_id": uid, "action": "edit", "title": "Deploy key id"},
    )
    assert status == 200


def test_round2_commit_reads_the_persisted_identifiers(migrated_database_urls, monkeypatch) -> None:
    """Design ruling, extended fields: idempotency_key, trace_id, project_scope."""

    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-extended@example.com")
    for extra in ({"idempotency_key": PAT}, {"trace_id": PAT}, {"project_scope": ["alice", PAT]}):
        status, payload = invoke_request(
            "POST",
            "/v0/vnext/memories/commit",
            payload={"user_id": str(user_id), "title": "deploy", "canonical_text": "Deploys go out on Tuesdays.", **extra},
        )
        assert payload.get("status") == "rejected", (extra, status, payload)
    assert _count(app_url, user_id, "memories") == 0
    assert _pat_rows(app_url, user_id) == 0


# ---------------------------------------------------------------------------
# Door 4: /v1 memory operations and the legacy continuity writes.
# ---------------------------------------------------------------------------


def test_door4_generate_refuses_and_persists_nothing(migrated_database_urls, monkeypatch) -> None:
    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-door4-generate@example.com")

    # Guards the guard: the same route turns a clean decision into a stored
    # candidate, so the refusal below is not a broken request.
    status, clean = invoke_request(
        "POST",
        "/v1/memory/operations/candidates/generate",
        payload={"user_content": "Decision: ship on Tuesdays", "mode": "assist", "sync_fingerprint": "clean"},
        headers=identity_header(user_id),
    )
    assert status == 200
    assert clean["summary"]["candidate_count"] == 1
    assert _count(app_url, user_id, "memory_operation_candidates") == 1

    status, _ = invoke_request(
        "POST",
        "/v1/memory/operations/candidates/generate",
        payload={
            "user_content": f"Decision: rotate the deploy token to {PAT}",
            "mode": "assist",
            "sync_fingerprint": "door-4",
        },
        headers=identity_header(user_id),
    )

    assert status == 400
    assert _count(app_url, user_id, "memory_operation_candidates") == 1
    assert _count(app_url, user_id, "continuity_objects") == 0
    assert _pat_rows(app_url, user_id) == 0


def test_door4_commit_refuses_a_stored_credential_candidate_and_it_is_never_recalled(
    migrated_database_urls, monkeypatch
) -> None:
    """A candidate row written before this fix, or straight into the table."""

    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-door4-commit@example.com")
    payload = {
        "candidate_id": "planted",
        "candidate_type": "decision",
        "object_type": "Decision",
        "normalized_text": f"rotate the deploy token to {PAT}",
        "confidence": 0.98,
        "trust_class": "deterministic",
        "evidence_snippet": f"rotate the deploy token to {PAT}",
        "explicit": True,
        "source_role": "user",
        "admission_reason": "explicit_prefix_decision",
        "proposed_action": "auto_save_candidate",
    }
    with user_connection(app_url, user_id) as conn:
        planted = ContinuityStore(conn).create_memory_operation_candidate(
            sync_fingerprint="planted",
            source_kind="sync_turn",
            source_candidate_id="planted",
            source_candidate_type="decision",
            candidate_payload=payload,
            source_scope={},
            operation_type="ADD",
            operation_reason="no_current_target_match",
            policy_action="auto_apply",
            policy_reason="assist_mode_allowlist_explicit_high_confidence",
            target_continuity_object_id=None,
            target_snapshot={},
        )

    status, _ = invoke_request(
        "POST",
        "/v1/memory/operations/commit",
        payload={"candidate_ids": [str(planted["id"])]},
        headers=identity_header(user_id),
    )

    assert status == 400
    assert _count(app_url, user_id, "continuity_objects") == 0
    assert _count(app_url, user_id, "continuity_capture_events") == 0
    assert _count(app_url, user_id, "memory_operations") == 0
    status, brief = invoke_request(
        "POST",
        "/v1/continuity/brief",
        payload={"brief_type": "general", "query": "deploy token"},
        headers=identity_header(user_id),
    )
    assert status == 200
    assert PAT not in str(brief)
    status, recall = invoke_request(
        "GET", "/v0/continuity/recall", query_params={"user_id": str(user_id), "query": "deploy token"}
    )
    assert status == 200
    assert PAT not in str(recall)


def test_door4_capture_commit_and_explicit_capture_refuse_and_roll_back(migrated_database_urls, monkeypatch) -> None:
    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-door4-capture@example.com")

    status, _ = invoke_request(
        "POST",
        "/v0/continuity/captures/commit",
        payload={
            "user_id": str(user_id),
            "mode": "assist",
            "candidates": [
                {
                    "candidate_type": "decision",
                    "object_type": "Decision",
                    "normalized_text": f"rotate the deploy token to {PAT}",
                    "confidence": 0.98,
                    "explicit": True,
                }
            ],
        },
    )
    assert status == 400
    status, _ = invoke_request(
        "POST",
        "/v0/continuity/captures",
        payload={
            "user_id": str(user_id),
            "raw_content": f"rotate the deploy token to {PAT}",
            "explicit_signal": "decision",
        },
    )
    assert status == 400

    # The capture event is written before the object; the refusal rolls it back.
    assert _count(app_url, user_id, "continuity_capture_events") == 0
    assert _count(app_url, user_id, "continuity_objects") == 0
    assert _pat_rows(app_url, user_id) == 0


def test_door4_continuity_correction_refuses_and_leaves_the_object(migrated_database_urls, monkeypatch) -> None:
    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-door4-correction@example.com")
    status, captured = invoke_request(
        "POST",
        "/v0/continuity/captures",
        payload={"user_id": str(user_id), "raw_content": "ship on Tuesdays", "explicit_signal": "decision"},
    )
    assert status == 201
    object_id = captured["capture"]["derived_object"]["id"]
    with user_connection(app_url, user_id) as conn:
        before = dict(ContinuityStore(conn).get_continuity_object_optional(UUID(object_id)) or {})

    for body in (
        {"action": "edit", "body": {"decision_text": f"rotate the deploy token to {PAT}"}},
        {"action": "supersede", "replacement_title": f"Decision: rotate to {PAT}"},
        # Design ruling T1-F2: a title-only edit is read against the stored
        # body. The stored body is "ship on Tuesdays", so a title that
        # completes a token only with it would pass; this title carries one.
        {"action": "edit", "title": f"Decision: {PAT}"},
    ):
        status, _ = invoke_request(
            "POST",
            f"/v0/continuity/review-queue/{object_id}/corrections",
            payload={"user_id": str(user_id), **body},
        )
        assert status == 400, body

    with user_connection(app_url, user_id) as conn:
        after = dict(ContinuityStore(conn).get_continuity_object_optional(UUID(object_id)) or {})
    assert after == before
    assert _count(app_url, user_id, "continuity_objects") == 1
    assert _pat_rows(app_url, user_id) == 0


def test_round2_a_title_only_continuity_edit_is_read_against_the_stored_body(migrated_database_urls, monkeypatch) -> None:
    """Design ruling T1-F2 through the review-queue corrections route (alice_review_apply)."""

    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-f2-continuity@example.com")
    status, captured = invoke_request(
        "POST",
        "/v0/continuity/captures",
        payload={
            "user_id": str(user_id),
            "raw_content": "IAIOSFODNN7EXAMPLE is the deploy key id",
            "explicit_signal": "decision",
        },
    )
    assert status == 201
    object_id = captured["capture"]["derived_object"]["id"]
    with user_connection(app_url, user_id) as conn:
        before = dict(ContinuityStore(conn).get_continuity_object_optional(UUID(object_id)) or {})

    status, _ = invoke_request(
        "POST",
        f"/v0/continuity/review-queue/{object_id}/corrections",
        payload={"user_id": str(user_id), "action": "edit", "title": "id AK"},
    )

    assert status == 400
    with user_connection(app_url, user_id) as conn:
        assert dict(ContinuityStore(conn).get_continuity_object_optional(UUID(object_id)) or {}) == before
    status, _ = invoke_request(
        "POST",
        f"/v0/continuity/review-queue/{object_id}/corrections",
        payload={"user_id": str(user_id), "action": "edit", "title": "Deploy key id"},
    )
    assert status == 200


def test_round2_finding8_a_large_correction_is_answered_before_it_is_scanned(migrated_database_urls, monkeypatch) -> None:
    """Review finding 8: 3 MB for a missing id cost 44 s of CPU; v0.16.0 took 0.01 s."""

    import time

    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-finding8@example.com")
    big = {"decision_text": "x" * (3 * 1024 * 1024)}

    started = time.perf_counter()
    status, _ = invoke_request(
        "POST",
        f"/v0/continuity/review-queue/{UUID(int=5)}/corrections",
        payload={"user_id": str(user_id), "action": "edit", "body": big},
    )
    assert status == 404
    assert time.perf_counter() - started < 5.0

    status, captured = invoke_request(
        "POST",
        "/v0/continuity/captures",
        payload={"user_id": str(user_id), "raw_content": "ship on Tuesdays", "explicit_signal": "decision"},
    )
    object_id = captured["capture"]["derived_object"]["id"]
    started = time.perf_counter()
    status, _ = invoke_request(
        "POST",
        f"/v0/continuity/review-queue/{object_id}/corrections",
        payload={"user_id": str(user_id), "action": "edit", "body": big},
    )
    assert status == 400
    assert time.perf_counter() - started < 5.0


# ---------------------------------------------------------------------------
# Door 5: artifact promotion over HTTP.
# ---------------------------------------------------------------------------


def test_door5_artifact_promote_over_http_creates_no_memory(migrated_database_urls, monkeypatch) -> None:
    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-door5@example.com")
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        artifact = store.create_artifact(
            {
                "artifact_type": "draft",
                "title": "Deploy notes",
                "content_markdown": f"Deploy with {PAT}",
                "status": "draft",
                "domain": "project",
                "sensitivity": "private",
            }
        )
        clean = store.create_artifact(
            {
                "artifact_type": "draft",
                "title": "Deploy notes",
                "content_markdown": "Deploys go out on Tuesdays.",
                "status": "draft",
                "domain": "project",
                "sensitivity": "private",
            }
        )

    status, _ = invoke_request(
        "POST",
        f"/v0/vnext/artifacts/{artifact['id']}/review",
        payload={"user_id": str(user_id), "action": "promote"},
    )

    assert status == 400
    assert _count(app_url, user_id, "memories") == 0
    with user_connection(app_url, user_id) as conn:
        assert (PostgresVNextStore(conn).get_artifact(str(artifact["id"])) or {}).get("status") == "draft"

    # Guards the guard: the same route promotes clean content into a memory.
    status, promoted = invoke_request(
        "POST",
        f"/v0/vnext/artifacts/{clean['id']}/review",
        payload={"user_id": str(user_id), "action": "promote"},
    )
    assert status == 200
    assert _memory(app_url, user_id, promoted["promoted_memory_id"])["status"] == "active"
    assert _pat_rows(app_url, user_id) == 0


# ---------------------------------------------------------------------------
# S4.4 round 2, part 3 (rulings C2, C6 and R1, 2026-09-23): approve, accept,
# propose and reject on the Postgres doors. The SQLite twins are in
# tests/unit/test_credential_activation_and_withhold.py.
# ---------------------------------------------------------------------------


def _pat_in(database_url: str, user_id: UUID, table: str) -> int:
    with user_connection(database_url, user_id) as conn:
        return int(_scalar(conn, f"SELECT count(*) FROM {table} AS t WHERE t::text LIKE '%{PAT}%'"))


def _seed_pg_candidate(database_url: str, user_id: UUID, text: str) -> str:
    """A candidate as a pre-floor vault or an unchecked background writer holds one."""

    with user_connection(database_url, user_id) as conn:
        row = PostgresVNextStore(conn).create_memory(
            {
                "memory_key": f"legacy.seed.{uuid4().hex[:8]}",
                "status": "candidate",
                "title": "deploy token",
                "canonical_text": text,
                "domain": "unknown",
                "sensitivity": "unknown",
            }
        )
    return str(row["id"])


def test_round2_c2_the_postgres_store_refuses_to_activate_a_credential(migrated_database_urls) -> None:
    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="floor-c2-store@example.com")
    with pytest.raises(CredentialActivationRefused):
        with user_connection(app_url, user_id) as conn:
            PostgresVNextStore(conn).create_memory(
                {"memory_key": "direct.write", "status": "active", "title": "deploy", "canonical_text": f"use {PAT}"}
            )
    assert _count(app_url, user_id, "memories") == 0
    seeded = _seed_pg_candidate(app_url, user_id, f"use {PAT} for deploys")
    with pytest.raises(CredentialActivationRefused):
        with user_connection(app_url, user_id) as conn:
            PostgresVNextStore(conn).update_memory(memory_id=seeded, patch={"status": "active"})
    assert _memory(app_url, user_id, seeded)["status"] == "candidate"
    # Guards the guard: a clean candidate activates.
    clean = _seed_pg_candidate(app_url, user_id, "Deploys go out on Tuesdays.")
    with user_connection(app_url, user_id) as conn:
        PostgresVNextStore(conn).update_memory(memory_id=clean, patch={"status": "active"})
    assert _memory(app_url, user_id, clean)["status"] == "active"


def test_round2_c2_http_review_accept_and_promote_refuse_a_credential_candidate(
    migrated_database_urls, monkeypatch
) -> None:
    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-c2-review@example.com")
    dirty = _seed_pg_candidate(app_url, user_id, f"use {PAT} for deploys")
    clean = _seed_pg_candidate(app_url, user_id, "Deploys go out on Tuesdays.")

    for action in ("accept", "promote"):
        status, _ = invoke_request(
            "POST", f"/v0/vnext/memories/{dirty}/review", payload={"user_id": str(user_id), "action": action}
        )
        assert status == 400, action
        # The reason is persisted with the row, so it is read with it.
        status, _ = invoke_request(
            "POST",
            f"/v0/vnext/memories/{clean}/review",
            payload={"user_id": str(user_id), "action": action, "reason": f"approved, token {PAT}"},
        )
        assert status == 400, action
    assert _memory(app_url, user_id, dirty)["status"] == "candidate"
    assert _memory(app_url, user_id, clean)["status"] == "candidate"
    assert _pat_in(app_url, user_id, "memory_revisions") == 0
    assert _pat_in(app_url, user_id, "event_log") == 0

    # Guards the guard.
    status, accepted = invoke_request(
        "POST", f"/v0/vnext/memories/{clean}/review", payload={"user_id": str(user_id), "action": "accept"}
    )
    assert status == 200
    assert accepted["memory"]["status"] == "active"


def test_round2_c2_project_update_accept_refuses_a_credential_state(migrated_database_urls, monkeypatch) -> None:
    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-c2-project@example.com")
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        project = store.create_project(
            {
                "name": "Deploy pipeline",
                "slug": f"deploy-{uuid4().hex[:8]}",
                "status": "active",
                "current_state": "Deploys go out on Tuesdays.",
                "domain": "project",
                "sensitivity": "private",
            }
        )
        project_id = str(project["id"])
        store.create_source(
            {
                "source_type": "manual_text",
                "title": "Deploy notes",
                "content_hash": f"sha256:{uuid4().hex}",
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {"project_scope": [project_id], "raw_text": "Decision: deploys move to Fridays."},
            }
        )
        candidate = VNextProjectService(store).generate_project_update_candidate(
            ProjectAutomationRequest(project_id=project_id, domains=("project",))
        )
    artifact_id = str(candidate["id"])
    memory_id = str(candidate["metadata_json"]["candidate_memory_id"])  # type: ignore[index]

    status, _ = invoke_request(
        "POST",
        f"/v0/vnext/projects/update-candidates/{artifact_id}/review",
        payload={"user_id": str(user_id), "action": "edit", "edited_current_state": f"Deploy with {PAT}"},
    )

    assert status == 400
    assert _memory(app_url, user_id, memory_id)["status"] == "candidate"
    with user_connection(app_url, user_id) as conn:
        assert (PostgresVNextStore(conn).get_project(project_id) or {}).get("current_state") == (
            "Deploys go out on Tuesdays."
        )
    assert _pat_rows(app_url, user_id) == 0
    assert _pat_in(app_url, user_id, "projects") == 0
    # Guards the guard: the same route applies a clean edit.
    status, _ = invoke_request(
        "POST",
        f"/v0/vnext/projects/update-candidates/{artifact_id}/review",
        payload={"user_id": str(user_id), "action": "edit", "edited_current_state": "Deploys go out on Fridays."},
    )
    assert status == 200
    assert _memory(app_url, user_id, memory_id)["status"] == "active"


_OPENCLAW = {
    "agent_id": "openclaw",
    "agent_type": "coding_agent",
    "agent_run_id": "floor-run-1",
    "task_id": "floor-task-1",
    "project_scope": [],
    "permission_profile": "trusted_local_agent",
}


def test_round2_c2_http_propose_refuses_before_anything_is_written(migrated_database_urls, monkeypatch) -> None:
    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-c2-propose@example.com")
    base = {
        "user_id": str(user_id),
        "agent_identity": _OPENCLAW,
        "proposal_type": "candidate_memory",
        "title": "Deploy cadence",
        "canonical_text": "The team deploys on Thursdays.",
        "domain": "project",
        "sensitivity": "private",
    }
    events_before = _count(app_url, user_id, "event_log")
    for field, value in (
        ("title", f"token {PAT}"),
        ("canonical_text", f"use {PAT} for deploys"),
        ("rationale", f"because {PAT} is the deploy token"),
        ("source_refs", [f"note:{PAT}"]),
    ):
        status, _ = invoke_request("POST", "/v0/vnext/memory-proposals", payload={**base, field: value})
        assert status == 400, field
    assert _count(app_url, user_id, "memories") == 0
    assert _count(app_url, user_id, "event_log") == events_before
    # Guards the guard.
    status, proposed = invoke_request("POST", "/v0/vnext/memory-proposals", payload=base)
    assert status == 201
    assert proposed["proposal"]["status"] == "candidate"


def test_round2_c6_http_review_reject_completes_and_withholds(migrated_database_urls, monkeypatch) -> None:
    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-c6-reject@example.com")
    first = _seed_pg_candidate(app_url, user_id, "Deploys may move to Wednesdays.")
    second = _seed_pg_candidate(app_url, user_id, "Deploys may move to Thursdays.")

    status, rejected = invoke_request(
        "POST",
        f"/v0/vnext/memories/{first}/review",
        payload={"user_id": str(user_id), "action": "reject", "reason": f"it leaked {PAT}"},
    )
    assert status == 200
    assert (rejected["memory"]["status"], rejected["rationale_withheld"], rejected["text_withheld"]) == (
        "rejected",
        True,
        False,
    )
    status, rejected = invoke_request(
        "POST",
        f"/v0/vnext/memories/{second}/review",
        payload={"user_id": str(user_id), "action": "reject", "canonical_text": f"use {PAT} for deploys"},
    )
    assert status == 200
    assert (rejected["rationale_withheld"], rejected["text_withheld"]) == (False, True)
    assert _memory(app_url, user_id, second)["canonical_text"] == TEXT_WITHHELD_PLACEHOLDER
    for table in ("memories", "memory_revisions", "event_log"):
        assert _pat_in(app_url, user_id, table) == 0, table
    with user_connection(app_url, user_id) as conn:
        reasons = [revision["reason"] for revision in PostgresVNextStore(conn).list_revisions(first)]
    assert RATIONALE_WITHHELD_PLACEHOLDER in reasons


def _plant_continuity_body(database_url: str, user_id: UUID, object_id: str, body: dict[str, object]) -> None:
    """The object as a pre-floor vault holds it."""

    with user_connection(database_url, user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE continuity_objects SET body = %s::jsonb WHERE id = %s::uuid", (json.dumps(body), object_id)
            )


def test_round2_c2_c6_legacy_continuity_confirm_refuses_and_delete_withholds(
    migrated_database_urls, monkeypatch
) -> None:
    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-c2-continuity@example.com")
    object_ids = []
    for text in ("ship on Tuesdays", "ship on Fridays"):
        status, captured = invoke_request(
            "POST",
            "/v0/continuity/captures",
            payload={"user_id": str(user_id), "raw_content": text, "explicit_signal": "decision"},
        )
        assert status == 201
        object_ids.append(captured["capture"]["derived_object"]["id"])
    dirty, clean = object_ids
    _plant_continuity_body(app_url, user_id, dirty, {"decision_text": f"rotate to {PAT}"})

    status, _ = invoke_request(
        "POST", f"/v0/continuity/review-queue/{dirty}/corrections", payload={"user_id": str(user_id), "action": "confirm"}
    )
    assert status == 400
    assert _count(app_url, user_id, "continuity_correction_events") == 0

    status, deleted = invoke_request(
        "POST",
        f"/v0/continuity/review-queue/{clean}/corrections",
        payload={"user_id": str(user_id), "action": "delete", "reason": f"it leaked {PAT}"},
    )
    assert status == 200
    assert deleted["rationale_withheld"] is True
    assert deleted["continuity_object"]["status"] == "deleted"
    assert _pat_in(app_url, user_id, "continuity_correction_events") == 0


# ---------------------------------------------------------------------------
# Owner ruling R1: the four legacy admit routes.
# ---------------------------------------------------------------------------


def _admit_setup(database_url: str, monkeypatch) -> tuple[UUID, list[str]]:
    from alicebot_api.routers import memories_legacy as memories_legacy_router

    for module in (main_module, memories_legacy_router):
        monkeypatch.setattr(module, "get_settings", lambda: Settings(database_url=database_url))
    user_id = uuid4()
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, f"floor-r1-{user_id.hex[:8]}@example.com", "Owner")
        thread = store.create_thread("Memory thread")
        session = store.create_session(thread["id"], status="active")
        event_ids = [
            str(store.append_event(thread["id"], session["id"], "message.user", {"text": text})["id"])
            # The explicit-signal extractors only admit tokens of letters,
            # digits and a few marks, so the second event carries a project
            # key rather than the GitHub token, whose underscore they drop.
            for text in ("likes black coffee", f"Remind me to rotate {PROJECT_KEY}.")
        ]
    return user_id, event_ids


def _admit(user_id: UUID, event_id: str, **fields: object) -> tuple[int, dict[str, Any]]:
    return invoke_request(
        "POST",
        "/v0/memories/admit",
        payload={"user_id": str(user_id), "source_event_ids": [event_id], **fields},
    )


def test_round2_r1_admit_add_refuses_a_credential_value(migrated_database_urls, monkeypatch) -> None:
    app_url = migrated_database_urls["app"]
    user_id, events = _admit_setup(app_url, monkeypatch)
    status, _ = _admit(user_id, events[0], memory_key="user.preference.token", value={"text": f"use {PAT}"})
    assert status == 400
    assert _count(app_url, user_id, "memories") == 0
    assert _pat_rows(app_url, user_id) == 0


def test_round2_r1_admit_update_refuses_a_credential_replacing_a_clean_value(
    migrated_database_urls, monkeypatch
) -> None:
    app_url = migrated_database_urls["app"]
    user_id, events = _admit_setup(app_url, monkeypatch)
    status, added = _admit(user_id, events[0], memory_key="user.preference.coffee", value={"likes": "black"})
    assert status == 200
    assert added["decision"] == "ADD"
    status, _ = _admit(user_id, events[0], memory_key="user.preference.coffee", value={"likes": f"use {PAT}"})
    assert status == 400
    assert _pat_rows(app_url, user_id) == 0
    assert _count(app_url, user_id, "memory_revisions") == 1


def test_round2_r1_admit_noop_refuses_a_credential_open_loop_title(migrated_database_urls, monkeypatch) -> None:
    app_url = migrated_database_urls["app"]
    user_id, events = _admit_setup(app_url, monkeypatch)
    status, _ = _admit(user_id, events[0], memory_key="user.preference.coffee", value={"likes": "black"})
    assert status == 200
    status, _ = _admit(
        user_id,
        events[0],
        memory_key="user.preference.coffee",
        value={"likes": "black"},
        open_loop={"title": f"rotate {PAT}"},
    )
    assert status == 400
    assert _pat_in(app_url, user_id, "open_loops") == 0
    # Guards the guard: the NOOP branch writes a clean open-loop title.
    status, noop = _admit(
        user_id,
        events[0],
        memory_key="user.preference.coffee",
        value={"likes": "black"},
        open_loop={"title": "reorder coffee"},
    )
    assert status == 200
    assert noop["decision"] == "NOOP"
    assert noop["open_loop"]["title"] == "reorder coffee"


def test_round2_r1_capture_explicit_signals_rolls_the_whole_request_back(migrated_database_urls, monkeypatch) -> None:
    app_url = migrated_database_urls["app"]
    user_id, events = _admit_setup(app_url, monkeypatch)
    status, _ = invoke_request(
        "POST",
        "/v0/memories/capture-explicit-signals",
        payload={"user_id": str(user_id), "source_event_id": events[1]},
    )
    assert status == 400
    assert _count(app_url, user_id, "memories") == 0
    assert _count(app_url, user_id, "memory_revisions") == 0
    assert _count(app_url, user_id, "open_loops") == 0


# ---------------------------------------------------------------------------
# Owner ruling R4: the source_refs bound on a memory commit, over HTTP.
# ---------------------------------------------------------------------------


def test_round2_r4_http_commit_measures_a_string_ref_as_sent(migrated_database_urls, monkeypatch) -> None:
    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-r4@example.com")

    def commit(ref: str, text: str) -> int:
        status, _ = invoke_request(
            "POST",
            "/v0/vnext/memories/commit",
            payload={"user_id": str(user_id), "title": "deploy", "canonical_text": text, "source_refs": [ref]},
        )
        return status

    assert commit("x" * 4_001, "Deploys go out on Tuesdays.") == 400
    assert _count(app_url, user_id, "memories") == 0
    assert commit("x" * 4_000, "Deploys go out on Tuesdays.") == 201
    # Quote- and newline-heavy refs at 4,000 raw characters: refused before
    # this change, because their JSON form is twice as long.
    assert commit('"\n' * 2_000, "Deploys go out on Wednesdays.") == 201
    assert _count(app_url, user_id, "memories") == 2


# ---------------------------------------------------------------------------
# S4.4 round 3: the approval reason (P2 item 11) and the open-loop
# review-action door (P2 item 8), on Postgres.
# ---------------------------------------------------------------------------


def test_round3_http_confirm_and_correct_read_the_reason(migrated_database_urls, monkeypatch) -> None:
    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-r3-reason@example.com")
    uid = str(user_id)
    status, pending = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={"user_id": uid, "title": "deploy day", "canonical_text": "Deploys may move to Wednesdays.", "confidence": 0.8},
    )
    assert status == 201 and pending["status"] == "confirmation_required"
    status, _ = invoke_request(
        "POST",
        "/v0/vnext/memories/confirm",
        payload={
            "user_id": uid,
            "confirmation_id": pending["confirmation_id"],
            "action": "confirm",
            "rationale": f"approved, token {PAT}",
        },
    )
    assert status == 400
    assert _memory(app_url, user_id, pending["memory"]["id"])["status"] == "needs_review"

    status, active = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={"user_id": uid, "title": "deploy", "canonical_text": "Deploys go out on Tuesdays."},
    )
    assert status == 201
    status, _ = invoke_request(
        "POST",
        "/v0/vnext/memories/correct",
        payload={
            "user_id": uid,
            "memory_id": active["memory"]["id"],
            "canonical_text": "Deploys go out on Thursdays.",
            "reason": f"rotated, token {PAT}",
        },
    )
    assert status == 400
    assert _memory(app_url, user_id, active["memory"]["id"])["canonical_text"] == "Deploys go out on Tuesdays."
    for table in ("memories", "memory_revisions", "event_log"):
        assert _pat_in(app_url, user_id, table) == 0, table


def test_round3_open_loop_review_action_checks_activation_and_withholds_the_note(
    migrated_database_urls, monkeypatch
) -> None:
    app_url = migrated_database_urls["app"]
    _use_database(monkeypatch, app_url)
    user_id = seed_user(app_url, email="floor-r3-open-loop@example.com")
    object_ids = []
    for text in ("waiting on the vendor contract", "waiting on the security review"):
        status, captured = invoke_request(
            "POST",
            "/v0/continuity/captures",
            payload={"user_id": str(user_id), "raw_content": text, "explicit_signal": "blocker"},
        )
        assert status == 201
        object_ids.append(captured["capture"]["derived_object"]["id"])
    dirty, clean = object_ids
    _plant_continuity_body(app_url, user_id, dirty, {"blocking_reason": f"waiting on {PAT} rotation"})

    status, _ = invoke_request(
        "POST",
        f"/v0/continuity/open-loops/{dirty}/review-action",
        payload={"user_id": str(user_id), "action": "still_blocked"},
    )
    assert status == 400
    assert _count(app_url, user_id, "continuity_correction_events") == 0

    status, deferred = invoke_request(
        "POST",
        f"/v0/continuity/open-loops/{clean}/review-action",
        payload={"user_id": str(user_id), "action": "deferred", "note": f"moved, token {PAT}"},
    )
    assert status == 200
    assert deferred["rationale_withheld"] is True
    assert _pat_in(app_url, user_id, "continuity_correction_events") == 0
