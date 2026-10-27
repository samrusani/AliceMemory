"""Migrations 0070 and 0092 must also work for the documented table owner.

Both migrations write data under forced row-level security whose owner policies bind
``app.current_user_id()``. For the documented owner (a ``LOGIN NOSUPERUSER NOBYPASSRLS`` role that
owns its database) that binding is unset during an upgrade, so 0070's connector seed silently
wrote nothing and 0092's redaction backfill silently redacted nothing, while a superuser
upgrading the same vault wrote the rows. Each migration now brackets its data statements with the
owner bypass, as 0067, 0093 and 0095 to 0097 do, and these tests upgrade data-bearing vaults as
such an owner and as a superuser and compare the resulting rows.
"""

from __future__ import annotations

import psycopg
import pytest
from alembic import command

from alicebot_api.migrations import make_alembic_config
from tests.integration.test_migrations_0067_strict_owner import (  # noqa: F401  (fixtures)
    strict_owner_urls,
    superuser_reference_urls,
)

USER_A = "00000000-0000-0000-0000-000000000401"
USER_B = "00000000-0000-0000-0000-000000000402"
USER_B_EMAIL = "second-owner@example.com"

REDACTED_AT = "2026-07-01T12:00:00+00:00"
MEMORY_A = "00000000-0000-0000-0000-000000000411"
MEMORY_B = "00000000-0000-0000-0000-000000000412"
ARTIFACT_A = "00000000-0000-0000-0000-000000000421"
RATING_ID = "00000000-0000-0000-0000-000000000431"


def _seed_users(url: str, *, bound: bool) -> None:
    with psycopg.connect(url) as conn:
        for user_id, email in ((USER_A, "owner-a@example.com"), (USER_B, USER_B_EMAIL)):
            if bound:
                conn.execute("SELECT set_config(%s, %s, %s)", ("app.current_user_id", user_id, True))
            conn.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (user_id, email, user_id[-2:]),
            )
        conn.commit()


def _seed_connector_vault(url: str) -> None:
    _seed_users(url, bound=not _is_superuser(url))


def _is_superuser(url: str) -> bool:
    with psycopg.connect(url) as conn:
        return bool(conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0])


def _connector_rows(admin_url: str) -> tuple:
    """Read the end state as the cluster superuser: the row-level security of
    the forced tables hides the seeded rows from an unbound owner, by design."""

    url = admin_url
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id::text, connector_name, default_domain, default_sensitivity, sync_mode,
                       poll_interval_seconds
                FROM connector_settings
                ORDER BY user_id, connector_name
                """
            )
            settings_rows = tuple(map(tuple, cur.fetchall()))
            cur.execute(
                """
                SELECT user_id::text, connector_name, cursor_type, state_json->>'initialized_by'
                FROM connector_state
                ORDER BY user_id, connector_name, cursor_type
                """
            )
            return settings_rows + tuple(map(tuple, cur.fetchall()))


def test_strict_owner_connector_seed_matches_superuser(strict_owner_urls, superuser_reference_urls):
    """0070 seeds every stored user's connector rows on the owner path too."""

    owner_config = make_alembic_config(strict_owner_urls["strict"])
    reference_config = make_alembic_config(superuser_reference_urls["admin"])
    command.upgrade(owner_config, "20260511_0069")
    command.upgrade(reference_config, "20260511_0069")
    _seed_connector_vault(strict_owner_urls["strict"])
    _seed_connector_vault(superuser_reference_urls["admin"])

    command.upgrade(owner_config, "head")
    command.upgrade(reference_config, "head")

    owner_rows = _connector_rows(strict_owner_urls["admin"])
    reference_rows = _connector_rows(superuser_reference_urls["admin"])
    assert owner_rows == reference_rows
    assert len(owner_rows) > 0


def _seed_redaction_vault(url: str) -> None:
    """The rows a pre-0092 vault carries for one authorized redaction, per user."""

    bound = not _is_superuser(url)
    for user_id, memory_id, email in (
        (USER_A, MEMORY_A, "redaction-a@example.com"),
        (USER_B, MEMORY_B, USER_B_EMAIL),
    ):
        with psycopg.connect(url) as conn:
            if bound:
                conn.execute("SELECT set_config(%s, %s, %s)", ("app.current_user_id", user_id, True))
            conn.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (user_id, email, user_id[-2:]),
            )
            conn.execute(
                """
                INSERT INTO memories (
                  id, user_id, memory_key, canonical_text, title, summary, trust_reason,
                  value, status, source_event_ids, metadata_json, deleted_at
                )
                VALUES (
                  %s, %s, 'redaction.seed', '[REDACTED]', NULL, NULL, NULL,
                  '{"redacted": true}'::jsonb, 'archived', '[]'::jsonb,
                  %s::jsonb, now()
                )
                """,
                (memory_id, user_id, f'{{"redacted": true, "redacted_at": "{REDACTED_AT}"}}'),
            )
            conn.execute(
                """
                INSERT INTO memory_revisions (
                  id, user_id, memory_id, sequence_no, action, memory_key,
                  previous_value, new_value, source_event_ids, candidate,
                  revision_number, revision_type, text_before, text_after, reason, metadata_json
                )
                VALUES (
                  %s, %s, %s, 1, 'DELETE', 'redaction.seed',
                  '{"text": "before"}'::jsonb, NULL, '["event-1"]'::jsonb, '{"note": "c"}'::jsonb,
                  1, 'archived', '[REDACTED] before', '[REDACTED] after', 'user request', '{}'::jsonb
                )
                """,
                (f"00000000-0000-0000-0000-00000000044{user_id[-1]}", user_id, memory_id),
            )
            conn.execute(
                """
                INSERT INTO event_log (
                  id, user_id, event_type, target_type, target_id, actor_type, payload_json, integrity_hash
                )
                VALUES (
                  %s, %s, 'memory.redacted', 'memory', %s, 'user',
                  jsonb_build_object('redacted', true, 'memory_id', %s::text), 'sha256:old'
                )
                """,
                (f"00000000-0000-0000-0000-00000000045{user_id[-1]}", user_id, memory_id, memory_id),
            )
            conn.execute(
                """
                INSERT INTO generated_artifacts (
                  id, user_id, artifact_type, title, content_markdown, status,
                  generated_by, prompt_hash, model_info_json, metadata_json
                )
                VALUES (
                  %s, %s, 'project_update', 'Weekly update', 'The weekly update body.',
                  'accepted', 'hermes', 'prompt-hash', '{"model": "m"}'::jsonb,
                  %s::jsonb
                )
                """,
                (ARTIFACT_A if user_id == USER_A else f"00000000-0000-0000-0000-000000000422",
                 user_id,
                 '{"candidate_memory_id": "%s", "workflow": "project_auto_update", "project_id": "p1", "project_scope": ["p1"], "review_action": "accept"}' % memory_id),
            )
            conn.execute(
                """
                INSERT INTO artifact_quality_ratings (
                  id, user_id, artifact_id, missed_context, comments, metadata_json
                )
                VALUES (%s, %s, %s, 'missed the budget note', 'otherwise fine', '{}'::jsonb)
                """,
                (RATING_ID if user_id == USER_A else f"00000000-0000-0000-0000-000000000432",
                 user_id,
                 ARTIFACT_A if user_id == USER_A else f"00000000-0000-0000-0000-000000000422"),
            )
            conn.execute(
                """
                INSERT INTO provenance_links (
                  id, user_id, target_type, target_id, evidence_role, quote
                )
                VALUES (%s, %s, 'memory', %s, 'quoted_from', 'a quoted sentence')
                """,
                (f"00000000-0000-0000-0000-00000000046{user_id[-1]}", user_id, memory_id),
            )
            conn.commit()


def _redaction_rows(admin_url: str) -> tuple:
    """Read the end state as the cluster superuser; see _connector_rows."""

    url = admin_url
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT title, content_markdown, prompt_hash, metadata_json->'redacted',
                       metadata_json->>'redacted_at'
                FROM generated_artifacts ORDER BY id
                """
            )
            artifacts = tuple(map(tuple, cur.fetchall()))
            cur.execute(
                """
                SELECT missed_context, comments, metadata_json::text
                FROM artifact_quality_ratings ORDER BY id
                """
            )
            ratings = tuple(map(tuple, cur.fetchall()))
            cur.execute("SELECT quote FROM provenance_links ORDER BY id")
            links = tuple(map(tuple, cur.fetchall()))
            cur.execute(
                """
                SELECT memory_key, previous_value::text, new_value::text, text_before, text_after,
                       reason, metadata_json::text, source_event_ids::text, candidate::text
                FROM memory_revisions ORDER BY id
                """
            )
            revisions = tuple(map(tuple, cur.fetchall()))
            cur.execute(
                """
                SELECT payload_json->'redacted', payload_json->>'memory_id', integrity_hash
                FROM event_log ORDER BY id
                """
            )
            events = tuple(map(tuple, cur.fetchall()))
            cur.execute(
                """
                SELECT memory_key, canonical_text, title, summary, trust_reason, value::text,
                       commit_digest, confirmation_id, metadata_json->>'redacted_at'
                FROM memories ORDER BY id
                """
            )
            memories_rows = tuple(map(tuple, cur.fetchall()))
    return artifacts + ratings + links + revisions + events + memories_rows


def test_strict_owner_redaction_backfill_matches_superuser(strict_owner_urls, superuser_reference_urls):
    """0092 redacts the persisted coupled copies on the owner path too."""

    owner_config = make_alembic_config(strict_owner_urls["strict"])
    reference_config = make_alembic_config(superuser_reference_urls["admin"])
    command.upgrade(owner_config, "20260715_0091")
    command.upgrade(reference_config, "20260715_0091")
    _seed_redaction_vault(strict_owner_urls["strict"])
    _seed_redaction_vault(superuser_reference_urls["admin"])

    command.upgrade(owner_config, "head")
    command.upgrade(reference_config, "head")

    owner_rows = _redaction_rows(strict_owner_urls["admin"])
    reference_rows = _redaction_rows(superuser_reference_urls["admin"])
    assert owner_rows == reference_rows
    # The backfill really ran on both paths: the artifact was redacted with
    # the stored redacted_at, and the rating carries the marker.
    assert ("[REDACTED]", "[REDACTED]", None, True, REDACTED_AT) in owner_rows[:2]
    assert (("[REDACTED]", "[REDACTED]", '{"redacted": true}') in owner_rows[2:4])
