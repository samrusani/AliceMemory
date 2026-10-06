"""Atomic source capture identity against a real PostgreSQL database."""

from __future__ import annotations

import threading
from uuid import uuid4

import pytest

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore, ContinuityStoreInvariantError
from alicebot_api.vnext_capture import (
    VNextCaptureService,
    capture_dedupe_key_for_text,
    content_hash_for_text,
)
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.conftest import lock_label_fixture


def test_two_connections_claim_one_source_dedupe_identity(
    migrated_database_urls,
) -> None:
    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"capture-{user_id}@example.invalid",
            "Capture dedupe",
        )

    start = threading.Barrier(2)
    outcomes: list[tuple[str, bool] | Exception] = []
    outcomes_lock = threading.Lock()
    source = {
        "source_type": "note",
        "title": "Concurrent capture",
        "content_hash": "raw-text-digest",
        "dedupe_key": "project-scope-dedupe-identity",
        "domain": "project",
        "sensitivity": "internal",
        "metadata_json": {"project_scope": ["alpha"]},
    }

    def capture() -> None:
        try:
            with user_connection(app_url, user_id) as conn:
                start.wait(timeout=10)
                row, created = PostgresVNextStore(conn).get_or_create_source(source)
                outcome: tuple[str, bool] | Exception = (str(row["id"]), created)
        except Exception as exc:  # noqa: BLE001 - surfaced by assertions below
            outcome = exc
        with outcomes_lock:
            outcomes.append(outcome)

    workers = [threading.Thread(target=capture), threading.Thread(target=capture)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
        assert not worker.is_alive(), "capture dedupe worker did not finish"

    assert not any(isinstance(outcome, Exception) for outcome in outcomes), outcomes
    rows = [outcome for outcome in outcomes if isinstance(outcome, tuple)]
    assert len(rows) == 2
    assert len({row_id for row_id, _created in rows}) == 1
    assert sorted(created for _row_id, created in rows) == [False, True]

    with user_connection(app_url, user_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS count FROM sources WHERE dedupe_key = %s",
                (source["dedupe_key"],),
            )
            count = int(cur.fetchone()["count"])
    assert count == 1


def test_exact_recapture_with_changed_classification_preserves_postgres_source_and_candidate(
    migrated_database_urls,
) -> None:
    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"capture-classification-{user_id}@example.invalid",
            "Capture classification",
        )
        store = PostgresVNextStore(conn)
        service = VNextCaptureService(store)
        text = "Fact: The release rehearsal is scheduled for Friday."

        first = service.capture_text(
            text,
            domain="project",
            sensitivity="public",
            project_scope=("Alpha",),
        )
        reclassified = service.capture_text(
            text,
            domain="professional",
            sensitivity="private",
            project_scope=("Alpha",),
        )
        repeated = service.capture_text(
            text,
            domain="professional",
            sensitivity="private",
            project_scope=("Alpha",),
        )

        assert first.status == "imported"
        assert reclassified.status == "imported"
        assert reclassified.source_id != first.source_id
        assert repeated.status == "duplicate"
        assert repeated.source_id == reclassified.source_id

        first_source = store.get_source(first.source_id)
        reclassified_source = store.get_source(reclassified.source_id)
        assert first_source is not None and reclassified_source is not None
        assert (first_source["domain"], first_source["sensitivity"]) == ("project", "public")
        assert (reclassified_source["domain"], reclassified_source["sensitivity"]) == (
            "professional",
            "private",
        )

        classified_candidates = {
            (str(row["domain"]), str(row["sensitivity"]))
            for row in store.list_memories(status="candidate")
            if row["metadata_json"].get("source_id") in {first.source_id, reclassified.source_id}
        }
        assert classified_candidates == {("project", "public"), ("professional", "private")}


def test_source_scope_mutation_rotates_postgres_identity_and_releases_old_capture(
    migrated_database_urls,
) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"capture-mutation-{user_id}@example.invalid",
            "Capture mutation",
        )
        store = PostgresVNextStore(conn)
        lock_label_fixture(store)
        service = VNextCaptureService(store)
        text = "Fact: Reviewed source scope changes must rotate identity atomically."
        first = service.capture_text(
            text,
            domain="project",
            sensitivity="private",
            project_scope=("Alpha",),
        )
        source = store.get_source(str(first.source_id))
        assert source is not None
        updated = store.update_source(
            source_id=str(first.source_id),
            patch={
                "metadata_json": {
                    **source["metadata_json"],
                    "project_scope": ["Beta"],
                }
            },
        )

        assert updated["dedupe_key"] == capture_dedupe_key_for_text(
            text,
            ("Beta",),
            domain="project",
            sensitivity="private",
        )
        beta_repeat = service.capture_text(
            text,
            domain="project",
            sensitivity="private",
            project_scope=("Beta",),
        )
        alpha_replacement, alpha_created = store.get_or_create_source(
            {
                "source_type": "manual_text",
                "content_hash": content_hash_for_text(text, ("Alpha",)),
                "dedupe_key": capture_dedupe_key_for_text(
                    text,
                    ("Alpha",),
                    domain="project",
                    sensitivity="private",
                ),
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {"raw_text": text, "project_scope": ["Alpha"]},
            }
        )

        assert beta_repeat.status == "duplicate"
        assert beta_repeat.source_id == first.source_id
        assert alpha_created is True
        assert str(alpha_replacement["id"]) != first.source_id


def test_source_scope_mutation_collision_rolls_back_postgres_row(
    migrated_database_urls,
) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"capture-collision-{user_id}@example.invalid",
            "Capture collision",
        )
        store = PostgresVNextStore(conn)
        lock_label_fixture(store)
        service = VNextCaptureService(store)
        text = "Fact: Collision rollback leaves both source identities intact."
        alpha = service.capture_text(
            text,
            domain="project",
            sensitivity="private",
            project_scope=("Alpha",),
        )
        service.capture_text(
            text,
            domain="project",
            sensitivity="private",
            project_scope=("Beta",),
        )
        before = store.get_source(str(alpha.source_id))
        assert before is not None

        with pytest.raises(ContinuityStoreInvariantError, match="already belongs"):
            store.update_source(
                source_id=str(alpha.source_id),
                patch={
                    "metadata_json": {
                        **before["metadata_json"],
                        "project_scope": ["Beta"],
                    }
                },
            )

        after = store.get_source(str(alpha.source_id))
        assert after is not None
        assert after["dedupe_key"] == before["dedupe_key"]
        assert after["metadata_json"] == before["metadata_json"]
