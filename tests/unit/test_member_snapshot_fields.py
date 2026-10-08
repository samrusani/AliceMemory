"""What a consolidation candidate stores next to the id of each member it covers.

A candidate memory keeps ``consolidation.member_snapshots``, one entry per member, so the acceptance step can tell
whether a member changed after the candidate was made. The security note says what sits next to a member id: its
status, its update time and a 16-character digest, and none of its text. These checks keep that sentence true.

Mutation: add ``"title": memory.get("title")`` to the dictionary ``memory_version_snapshot`` returns.
"""

from __future__ import annotations

import json
import re

from alicebot_api.vnext_memory_version import memory_content_digest, memory_version_snapshot


def test_a_member_snapshot_holds_the_id_the_status_the_update_time_and_a_digest_only() -> None:
    row = {
        "id": "7f1d1b52-6f64-4a63-8d6e-2f1c8d9a0b11",
        "status": "active",
        "updated_at": "2026-10-05T09:00:00Z",
        "title": "TITLE-SECRET",
        "canonical_text": "TEXT-SECRET",
        "summary": "SUMMARY-SECRET",
        "value": {"text": "VALUE-SECRET"},
        "domain": "health",
        "sensitivity": "confidential",
        "metadata_json": {"project_scope": ["PROJECT-SECRET"]},
    }
    snapshot = memory_version_snapshot(row)
    assert set(snapshot) == {"id", "status", "updated_at", "content_digest"}
    assert re.fullmatch(r"[0-9a-f]{16}", snapshot["content_digest"])
    rendered = json.dumps(snapshot)
    for word in ("TITLE-SECRET", "TEXT-SECRET", "SUMMARY-SECRET", "VALUE-SECRET", "PROJECT-SECRET", "health", "confidential"):
        assert word not in rendered


def test_the_digest_follows_the_text_and_nothing_else() -> None:
    row = {"title": "t", "canonical_text": "c", "summary": "s", "value": {"text": "v"}}
    assert memory_content_digest(row) == memory_content_digest({**row, "status": "deleted", "updated_at": "later", "sensitivity": "sacred"})
    assert memory_content_digest(row) != memory_content_digest({**row, "canonical_text": "other"})
