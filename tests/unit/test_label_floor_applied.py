"""An owner edit that would lower a derived memory is held at its inputs.

The handoff names this ``label_floor_applied`` on the review answer, with a
``labels_raised`` event whose cause is ``floor_clamped``.
"""

from __future__ import annotations

import inspect
import json

from alicebot_api.onramp import bootstrap_database
from alicebot_api.routers.vnext_memories import review_vnext_memory
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_label_writes import without_insert_floor
from tests.unit.test_derived_domain_fence import USER


def test_an_edit_below_the_inputs_is_clamped_and_named(tmp_path) -> None:
    """Mutation: ``clamp_owner_patch`` returns the patch unchanged. The stored sensitivity stays ``public``
    and no ``floor_clamped`` event is written.
    """

    path = tmp_path / "vault.db"
    bootstrap_database(path, user_id=USER, user_email="local@alice")
    with without_insert_floor(), sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        source = store.create_source(
            {
                "source_type": "note",
                "title": "Clinic note",
                "content_hash": "sha256:clinic",
                "domain": "health",
                "sensitivity": "confidential",
                "metadata_json": {},
            }
        )
        memory = store.create_memory(
            {
                "memory_key": "copy",
                "canonical_text": "A confidential observation",
                "status": "active",
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"source_id": source["id"]},
            }
        )
        updated = store.update_memory(
            memory_id=str(memory["id"]),
            patch={"sensitivity": "public", "domain": "project"},
            actor_type="user",
        )
        event = conn.execute(
            "SELECT payload_json FROM event_log WHERE event_type = 'memory.labels_raised' AND target_id = ?",
            (str(memory["id"]),),
        ).fetchone()
    assert updated["sensitivity"] == "confidential"
    assert store._label_floor_applied is True
    raw = event["payload_json"] if isinstance(event, dict) else event[0]
    payload = json.loads(raw)
    assert payload["cause"] == "floor_clamped"
    assert "title" not in json.dumps(payload)
    assert "A confidential observation" not in json.dumps(payload)
    assert 'review_payload["label_floor_applied"] = True' in inspect.getsource(review_vnext_memory)
