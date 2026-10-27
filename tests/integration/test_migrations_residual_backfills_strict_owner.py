"""The residual backfill migrations must also work for the documented table owner.

Migrations 0072, 0076, 0077, 0083, 0084, 0085, 0086, 0090 and 0091 write data under forced
row-level security whose owner policies bind ``app.current_user_id()``. For the documented owner
(a ``LOGIN NOSUPERUSER NOBYPASSRLS`` role that owns its database) that binding is unset during an
upgrade, so every backfill silently matched no rows while a superuser upgrading the same vault
wrote them. Each migration now brackets its data statements with the owner bypass, as 0067, 0070,
0092, 0093 and 0095 to 0097 do. These tests seed the legacy rows each backfill exists for, at the
revision the backfill runs from, upgrade one database as such an owner and its twin as a
superuser, and compare the end states.
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

USER_A = "00000000-0000-0000-0000-000000000501"
USER_B = "00000000-0000-0000-0000-000000000502"


def _bound(url: str) -> bool:
    with psycopg.connect(url) as conn:
        return not bool(conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0])


def _seed_users(url: str) -> None:
    bound = _bound(url)
    with psycopg.connect(url) as conn:
        for user_id, email in ((USER_A, "residual-a@example.com"), (USER_B, "residual-b@example.com")):
            if bound:
                conn.execute("SELECT set_config(%s, %s, %s)", ("app.current_user_id", user_id, True))
            conn.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (user_id, email, user_id[-2:]),
            )
        conn.commit()


def _upgrade_pair(strict_owner_urls, superuser_reference_urls, *, seed_revision: str, seeder, reader=None, upgrade_target: str = "head") -> tuple[tuple, tuple]:
    """Seed both twins at ``seed_revision`` and upgrade both to head.

    The twins' end states are read as the cluster superuser: the forced
    row-level security of the touched tables hides rows from an unbound
    owner by design, and the comparison here is about the rows the upgrade
    wrote.
    """

    owner_config = make_alembic_config(strict_owner_urls["strict"])
    reference_config = make_alembic_config(superuser_reference_urls["admin"])
    command.upgrade(owner_config, seed_revision)
    command.upgrade(reference_config, seed_revision)
    _seed_users(strict_owner_urls["strict"])
    _seed_users(superuser_reference_urls["admin"])
    seeder(strict_owner_urls["strict"])
    seeder(superuser_reference_urls["admin"])
    command.upgrade(owner_config, upgrade_target)
    command.upgrade(reference_config, upgrade_target)
    read = reader if reader is not None else _read_back
    return read(strict_owner_urls["admin"]), read(superuser_reference_urls["admin"])


def _read_back(admin_url: str) -> tuple:
    """The columns every backfill in this file touches, read as the superuser.

    The end state is compared at the storage level because the forced
    row-level security of these tables hides rows from an unbound owner by
    design; a strict owner reading its own vault binds its identity, and the
    comparison here is about the rows the upgrade wrote.
    """

    with psycopg.connect(admin_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT commit_digest, confirmation_id, project_id, created_by_agent_id, run_id,
                       superseded_by::text, supersedes::text,
                       metadata_json #>> '{lifecycle_migration,duplicate_commit_digest_canonical_memory_id}',
                       metadata_json #>> '{lifecycle_migration,duplicate_confirmation_id_canonical_memory_id}',
                       metadata_json #>> '{agentic_memory,idempotency_key}',
                       metadata_json -> 'project_scope'
                FROM memories ORDER BY id
                """
            )
            return tuple(map(tuple, cur.fetchall()))


import json as _json


def _seed_agentic_memories(url: str) -> None:
    """Memories whose metadata carries the keys 0072 and 0076 and 0077 backfill."""

    bound = _bound(url)
    rows = (
        (USER_A, "00000000-0000-0000-0000-000000000511", "agent.note.one",
         {"agentic_memory": {"idempotency_key": "idem-a", "confirmation": {"confirmation_id": "conf-a"},
                             "agent_identity": {"agent_id": "hermes", "agent_run_id": "run-a"}},
          "project_id": "P1", "agent_id": "hermes", "agent_run_id": "run-a",
          "superseded_by": "00000000-0000-0000-0000-000000000512",
          "supersedes": "00000000-0000-0000-0000-000000000513"}),
        (USER_B, "00000000-0000-0000-0000-000000000512", "agent.note.two",
         {"agentic_memory": {"idempotency_key": "idem-b",
                             "agent_identity": {"agent_id": "atlas", "agent_run_id": "run-b"}},
          "project_id": "P2"}),
    )
    with psycopg.connect(url) as conn:
        for user_id, memory_id, memory_key, metadata_value in rows:
            if bound:
                conn.execute("SELECT set_config(%s, %s, %s)", ("app.current_user_id", user_id, True))
            metadata = _json.dumps(metadata_value)
            conn.execute(
                """
                INSERT INTO memories (id, user_id, memory_key, value, status, source_event_ids, metadata_json)
                VALUES (%s, %s, %s, '{}'::jsonb, 'active', '[]'::jsonb, %s::jsonb)
                """,
                (memory_id, user_id, memory_key, metadata),
            )
        conn.commit()


def test_strict_owner_agentic_backfills_match_superuser(strict_owner_urls, superuser_reference_urls):
    """0072's commit-digest and confirmation backfills and 0076/0077's column backfills."""

    owner_rows, reference_rows = _upgrade_pair(
        strict_owner_urls, superuser_reference_urls,
        seed_revision="20260621_0071", seeder=_seed_agentic_memories,
    )
    assert owner_rows == reference_rows
    assert any(row[0] == "idem-a" for row in owner_rows)
    assert any(row[1] == "conf-a" for row in owner_rows)
    assert any(row[2] == "P1" and row[3] == "hermes" and row[4] == "run-a" for row in owner_rows)
    assert any(row[3] == "atlas" and row[4] == "run-b" for row in owner_rows)
    assert any(row[5] == "00000000-0000-0000-0000-000000000512" for row in owner_rows)


def _seed_duplicate_commits(url: str) -> None:
    """The duplicate agentic-commit pair 0083 deduplicates, with legacy scopes."""

    bound = _bound(url)
    rows = (
        (USER_A, "00000000-0000-0000-0000-000000000521", "dup.a.first", "digest-a"),
        (USER_A, "00000000-0000-0000-0000-000000000522", "dup.a.second", "digest-a"),
        (USER_B, "00000000-0000-0000-0000-000000000523", "dup.b.first", "digest-b"),
        (USER_B, "00000000-0000-0000-0000-000000000524", "dup.b.second", "digest-b"),
    )
    with psycopg.connect(url) as conn:
        for user_id, memory_id, memory_key, digest in rows:
            if bound:
                conn.execute("SELECT set_config(%s, %s, %s)", ("app.current_user_id", user_id, True))
            conn.execute(
                """
                INSERT INTO memories (id, user_id, memory_key, value, status, source_event_ids, commit_digest, metadata_json)
                VALUES (%s, %s, %s, '{}'::jsonb, 'active', '[]'::jsonb, %s, '{"agentic_memory": {"idempotency_key": "k"}}'::jsonb)
                """,
                (memory_id, user_id, memory_key, digest),
            )
        # A memory whose agentic metadata carries a legacy project_scope
        # array under agentic_memory, which 0083 lifts to project_scope.
        for user_id, memory_id in ((USER_A, "00000000-0000-0000-0000-000000000525"), (USER_B, "00000000-0000-0000-0000-000000000526")):
            if bound:
                conn.execute("SELECT set_config(%s, %s, %s)", ("app.current_user_id", user_id, True))
            conn.execute(
                """
                INSERT INTO memories (id, user_id, memory_key, value, status, source_event_ids, metadata_json)
                VALUES (%s, %s, 'legacy.scope', '{}'::jsonb, 'active', '[]'::jsonb,
                        '{"agentic_memory": {"project_scope": ["P1"]}}'::jsonb)
                """,
                (memory_id, user_id),
            )
        conn.commit()


def test_strict_owner_lifecycle_invariants_match_superuser(strict_owner_urls, superuser_reference_urls):
    """0083 lifts legacy scopes and clears duplicate commit digests for the owner too."""

    owner_rows, reference_rows = _upgrade_pair(
        strict_owner_urls, superuser_reference_urls,
        seed_revision="20260707_0082", seeder=_seed_duplicate_commits,
    )
    assert owner_rows == reference_rows
    # The scope lift happened for both users.
    assert any(row[10] is not None for row in owner_rows)
    # The duplicate rows lost their digest and carry the canonical pointer.
    assert any(row[0] is None and row[7] is not None for row in owner_rows)
    assert any(row[0] == "digest-a" for row in owner_rows)


def test_strict_owner_identifier_repair_matches_superuser(strict_owner_urls, superuser_reference_urls):
    """0084 moves a stranded identifier from the tombstone back onto the live row."""

    def seeder(url: str) -> None:
        bound = _bound(url)
        with psycopg.connect(url) as conn:
            for user_id, tombstone_id, live_id in (
                (USER_A, "00000000-0000-0000-0000-000000000531", "00000000-0000-0000-0000-000000000532"),
                (USER_B, "00000000-0000-0000-0000-000000000533", "00000000-0000-0000-0000-000000000534"),
            ):
                if bound:
                    conn.execute("SELECT set_config(%s, %s, %s)", ("app.current_user_id", user_id, True))
                # The tombstone still holds the identifier 0083 cleared on the
                # live row; the live row carries 0083's canonical back-pointer.
                conn.execute(
                    """
                    INSERT INTO memories (id, user_id, memory_key, value, status, source_event_ids,
                                          commit_digest, deleted_at, metadata_json)
                    VALUES (%s, %s, 'repair.tombstone', '{}'::jsonb, 'active', '[]'::jsonb,
                            'digest-stranded', now(),
                            '{"agentic_memory": {"idempotency_key": "k"}}'::jsonb)
                    """,
                    (tombstone_id, user_id),
                )
                conn.execute(
                    """
                    INSERT INTO memories (id, user_id, memory_key, value, status, source_event_ids,
                                          metadata_json)
                    VALUES (%s, %s, 'repair.live', '{}'::jsonb, 'active', '[]'::jsonb,
                            %s::jsonb)
                    """,
                    (live_id, user_id,
                     f'{{"lifecycle_migration": {{"duplicate_commit_digest_canonical_memory_id": "{tombstone_id}"}}}}'),
                )
            conn.commit()

    owner_rows, reference_rows = _upgrade_pair(
        strict_owner_urls, superuser_reference_urls,
        seed_revision="20260711_0083", seeder=seeder,
    )
    assert owner_rows == reference_rows
    # The live row carries the identifier; the tombstone lost it and
    # carries the canonical back-pointer to the live row.
    assert any(row[0] == "digest-stranded" for row in owner_rows)
    assert any(row[0] is None and row[7] == "00000000-0000-0000-0000-000000000532" for row in owner_rows)
    assert any(row[0] is None and row[7] == "00000000-0000-0000-0000-000000000534" for row in owner_rows)


def test_strict_owner_duplicate_pointer_repair_matches_superuser(strict_owner_urls, superuser_reference_urls):
    """0086 repoints a sibling's stale canonical pointer at the true canonical row."""

    def seeder(url: str) -> None:
        bound = _bound(url)
        with psycopg.connect(url) as conn:
            for user_id, former_holder_id, sibling_id, canonical_id in (
                (USER_A, "00000000-0000-0000-0000-000000000541", "00000000-0000-0000-0000-000000000542", "00000000-0000-0000-0000-000000000543"),
                (USER_B, "00000000-0000-0000-0000-000000000544", "00000000-0000-0000-0000-000000000545", "00000000-0000-0000-0000-000000000546"),
            ):
                if bound:
                    conn.execute("SELECT set_config(%s, %s, %s)", ("app.current_user_id", user_id, True))
                conn.execute(
                    """
                    INSERT INTO memories (id, user_id, memory_key, value, status, source_event_ids,
                                          deleted_at, metadata_json)
                    VALUES (%s, %s, 'pointer.former', '{}'::jsonb, 'active', '[]'::jsonb, now(),
                            %s::jsonb)
                    """,
                    (former_holder_id, user_id,
                     f'{{"lifecycle_migration": {{"duplicate_commit_digest_canonical_memory_id": "{canonical_id}"}}}}'),
                )
                conn.execute(
                    """
                    INSERT INTO memories (id, user_id, memory_key, value, status, source_event_ids,
                                          metadata_json)
                    VALUES (%s, %s, 'pointer.sibling', '{}'::jsonb, 'active', '[]'::jsonb,
                            %s::jsonb)
                    """,
                    (sibling_id, user_id,
                     f'{{"lifecycle_migration": {{"duplicate_commit_digest_canonical_memory_id": "{former_holder_id}"}}}}'),
                )
                conn.execute(
                    """
                    INSERT INTO memories (id, user_id, memory_key, value, status, source_event_ids,
                                          commit_digest, metadata_json)
                    VALUES (%s, %s, 'pointer.canonical', '{}'::jsonb, 'active', '[]'::jsonb,
                            'digest-canonical', '{"agentic_memory": {"idempotency_key": "k"}}'::jsonb)
                    """,
                    (canonical_id, user_id),
                )
            conn.commit()

    owner_rows, reference_rows = _upgrade_pair(
        strict_owner_urls, superuser_reference_urls,
        seed_revision="20260713_0085", seeder=seeder,
    )
    assert owner_rows == reference_rows
    # The sibling now points at the true canonical row.
    assert any(row[7] == "00000000-0000-0000-0000-000000000543" for row in owner_rows)
    assert any(row[7] == "00000000-0000-0000-0000-000000000546" for row in owner_rows)


def _source_rows(admin_url: str) -> tuple:
    with psycopg.connect(admin_url, connect_timeout=10) as conn:
        rows = conn.execute(
            """
            SELECT user_id::text, source_type, content_hash, dedupe_key, metadata_json->>'raw_text',
                   metadata_json -> 'project_scope'
            FROM sources ORDER BY id
            """
        ).fetchall()
        return tuple(map(tuple, rows))


def test_strict_owner_source_dedupe_backfill_matches_superuser(strict_owner_urls, superuser_reference_urls):
    """0085 computes dedupe keys for a vault that already carries sources."""

    def seeder(url: str) -> None:
        bound = _bound(url)
        with psycopg.connect(url) as conn:
            for user_id in (USER_A, USER_B):
                if bound:
                    conn.execute("SELECT set_config(%s, %s, %s)", ("app.current_user_id", user_id, True))
                for index, (title, raw_text) in enumerate((
                    ("Cobalt note", "the cobalt body"),
                    ("Cobalt note ", " the cobalt body "),
                )):
                    conn.execute(
                        """
                        INSERT INTO sources (id, user_id, source_type, title, content_hash, captured_at,
                                             domain, sensitivity, metadata_json)
                        VALUES (%s, %s, 'note', %s, %s, now(), 'project', 'internal',
                                %s::jsonb)
                        """,
                        (f"{user_id[-1]}0000000-0000-0000-0000-00000000055{index}",
                         user_id, title, f"hash-dup-{user_id[-1]}",
                         _json.dumps({"raw_text": raw_text})),
                    )

            conn.commit()

    owner_rows, reference_rows = _upgrade_pair(
        strict_owner_urls, superuser_reference_urls,
        seed_revision="20260712_0084", seeder=seeder, reader=_source_rows,
        # Compare at 0085 itself: 0090's bracketed identity recompute would
        # rewrite the dedupe keys again at head, masking 0085's own bracket.
        upgrade_target="20260713_0085",
    )
    assert owner_rows == reference_rows
    keys = [row[3] for row in owner_rows]
    assert len([k for k in keys if k is not None]) == len(owner_rows) // 2


def test_strict_owner_source_identity_recompute_matches_superuser(strict_owner_urls, superuser_reference_urls):
    """0090 recomputes live source identities for the owner too."""

    def seeder(url: str) -> None:
        bound = _bound(url)
        with psycopg.connect(url) as conn:
            for user_id in (USER_A, USER_B):
                if bound:
                    conn.execute("SELECT set_config(%s, %s, %s)", ("app.current_user_id", user_id, True))
                conn.execute(
                    """
                    INSERT INTO sources (id, user_id, source_type, title, content_hash, captured_at,
                                         domain, sensitivity, dedupe_key, metadata_json)
                    VALUES (%s, %s, 'note', 'Scoped note', 'hash-scope', now(), 'project', 'internal',
                            'old-key', '{"project_scope": ["P1"]}'::jsonb)
                    """,
                    (f"{user_id[-1]}0000000-0000-0000-0000-000000000561", user_id),
                )
            conn.commit()

    owner_rows, reference_rows = _upgrade_pair(
        strict_owner_urls, superuser_reference_urls,
        seed_revision="20260713_0089", seeder=seeder, reader=_source_rows,
    )
    assert owner_rows == reference_rows
    assert all(row[3] != "old-key" for row in owner_rows)
    assert all(row[3] is not None for row in owner_rows)


def test_strict_owner_whitespace_source_clear_matches_superuser(strict_owner_urls, superuser_reference_urls):
    """0091 clears the dedupe key of whitespace-only raw-text sources."""

    def seeder(url: str) -> None:
        bound = _bound(url)
        with psycopg.connect(url) as conn:
            for user_id, raw_text in ((USER_A, "   \u00a0 \t "), (USER_B, "\u2028 \n")):
                if bound:
                    conn.execute("SELECT set_config(%s, %s, %s)", ("app.current_user_id", user_id, True))
                conn.execute(
                    """
                    INSERT INTO sources (id, user_id, source_type, title, content_hash, captured_at,
                                         domain, sensitivity, dedupe_key, metadata_json)
                    VALUES (%s, %s, 'note', 'Whitespace note', 'hash-ws', now(), 'personal', 'private',
                            'key-to-clear', %s::jsonb)
                    """,
                    (f"{user_id[-1]}0000000-0000-0000-0000-000000000571", user_id,
                     _json.dumps({"raw_text": raw_text})),
                )
            conn.commit()

    owner_rows, reference_rows = _upgrade_pair(
        strict_owner_urls, superuser_reference_urls,
        seed_revision="20260714_0090", seeder=seeder, reader=_source_rows,
    )
    assert owner_rows == reference_rows
    assert all(row[3] is None for row in owner_rows)
