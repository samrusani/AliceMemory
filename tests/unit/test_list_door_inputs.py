"""A producer's input list drops a row whose effective label is above the request."""

from __future__ import annotations

from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from tests.unit.test_vnext_brain import InMemoryVNextBrainStore

SOURCE_ID = "11111111-1111-1111-1111-111111111111"


def test_a_brief_drops_a_public_copy_of_a_confidential_source() -> None:
    store = InMemoryVNextBrainStore()
    source = {
        "id": SOURCE_ID,
        "domain": "health",
        "sensitivity": "confidential",
        "metadata_json": {},
    }
    store.sources.append(
        {
            **source,
            "title": "Ordinary note",
            "content_hash": "sha256:abc",
            "captured_at": "2026-05-10T08:00:00Z",
            "domain": "project",
            "sensitivity": "public",
        }
    )
    store.memories.append(
        {
            "id": "memory-secret",
            "canonical_text": "SENTINEL confidential fact",
            "status": "active",
            "created_at": "2026-05-10T09:00:00Z",
            "domain": "project",
            "sensitivity": "public",
            "metadata_json": {
                "source_id": SOURCE_ID,
                "derived_from": {
                    "v": 1,
                    "sources": [SOURCE_ID],
                    "memories": [],
                    "open_loops": [],
                    "artifacts": [],
                    "beliefs": [],
                    "counts": {"sources": 1, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0},
                },
            },
        }
    )

    def read_label_rows(kind: str, ids: list[str]) -> list[dict[str, object]]:
        if kind == "source" and SOURCE_ID in ids:
            return [source]
        return []

    store.read_label_rows = read_label_rows  # type: ignore[attr-defined]
    artifact = VNextBrainService(store).generate_daily_brief(
        BrainArtifactRequest(generated_for="2026-05-10", domains=("project",))
    )
    assert "SENTINEL confidential fact" not in artifact["content_markdown"]
    derived = artifact["metadata_json"]["derived_from"]
    assert "memory-secret" not in derived["memories"]
