"""Live PostgreSQL regression for the embedding input cap label.

The SQL text-length expression in ``list_memories_missing_embeddings`` must
agree with ``len(memory_embedding_text(row))`` on every boundary the digest
expression already agrees on: Unicode whitespace the Python ``strip`` removes,
blank fields, and a field repeated in another. Each case signs a vector with
the label a vector made under a cap would carry and requires the list to leave
it alone, then signs the opposite label and requires the list to return it.

Mutation: drop the cap term from ``list_memories_missing_embeddings`` in
``vnext_stores/postgres/embedding_cas.py``. The wrong-label rows are then not
listed and every case here fails.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_embeddings import (
    memory_embedding_content_sha256,
    memory_embedding_text,
    pad_embedding_vector,
)
from alicebot_api.vnext_store import PostgresVNextStore

SIGNATURE = {
    "embedding_provider": "openai_compatible",
    "embedding_model": "embed-v1",
    "embedding_endpoint": "host-a",
    "embedding_signature_version": 2,
}


@pytest.mark.parametrize(
    ("boundary", "title", "canonical_text", "summary"),
    [
        ("plain", "Signed memory", "A fact long enough to be cut at several caps.", None),
        ("nbsp", " Signed memory ", " Whitespace fact of some length ", " Whitespace fact of some length "),
        ("u001c", "\u001cSigned memory\u001c", "\u001cWhitespace fact of some length\u001c", "\u001cWhitespace fact of some length\u001c"),
        ("repeated_field", "Same text in two fields", "Same text in two fields", "Same text in two fields"),
        ("blank_title", " \u001c", "\u001cMixed whitespace fact ", " Mixed whitespace fact\u001c"),
    ],
)
def test_cap_label_comparison_matches_python_text_length(
    migrated_database_urls,
    boundary: str,
    title: str,
    canonical_text: str,
    summary: str | None,
) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"embedding-cap-{boundary}-{user_id}@example.invalid",
            f"Embedding cap {boundary}",
        )
        store = PostgresVNextStore(conn)
        memory = store.create_memory(
            {
                "memory_key": f"embedding.cap.{uuid4()}",
                "value": {"text": "Cap fact"},
                "status": "active",
                "title": title,
                "canonical_text": canonical_text,
                "summary": summary,
                "domain": "project",
                "sensitivity": "private",
            }
        )
        memory_id = str(memory["id"])
        digest = memory_embedding_content_sha256(memory)
        length = len(memory_embedding_text(memory))
        assert length >= 10

        def sign(label: int | None) -> None:
            updated = store.update_memory_embedding(
                memory_id=memory_id,
                vector=pad_embedding_vector([1.0, 0.0]),
                provider="openai_compatible",
                model="embed-v1",
                endpoint="host-a",
                content_sha256=digest,
                signature_version=2,
                truncated_to_chars=label,
            )
            assert updated is not None

        for cap in (length - 1, length, length + 1):
            cut = length > cap
            sign(cap if cut else None)
            assert store.list_memories_missing_embeddings(**SIGNATURE, embedding_input_cap=cap) == []

            sign(None if cut else cap)
            listed = store.list_memories_missing_embeddings(**SIGNATURE, embedding_input_cap=cap)
            assert [str(row["id"]) for row in listed] == [memory_id]
            assert listed[0]["embedding_present"] is True

        # without a cap the label is not compared, as before
        assert store.list_memories_missing_embeddings(**SIGNATURE) == []
