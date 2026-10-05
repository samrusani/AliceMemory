"""The reverse lookup remains a superset of canonical encoded references."""
import json
from uuid import uuid4

import pytest

from alicebot_api.onramp import bootstrap_database
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_derived_labels import dependencies_of
from alicebot_api.vnext_label_writes import count_rows_hidden_by_scope_move, walk_dependants
from tests.unit.test_derived_domain_fence import USER

ALPHA = "prj_" + "a" * 16
BETA = "prj_" + "b" * 16


def encoded_object(key, row_id):
    escaped = "".join("\\u%04x" % ord(character) for character in row_id)
    return '{"' + key + '":"' + escaped + '"}'


def test_an_encoded_source_is_previewed_and_raised_without_losing_its_reference(tmp_path):
    path = tmp_path / "encoded.sqlite3"
    bootstrap_database(path, user_id=USER, user_email="synthetic@example.test")
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        source = store.create_source({"source_type": "note", "title": "Synthetic", "content_hash": "encoded", "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [ALPHA]}})
        reference = encoded_object("source_id", str(source["id"]))
        copy = store.create_memory({"memory_key": "encoded", "canonical_text": "Synthetic", "status": "active", "domain": "project", "sensitivity": "public", "metadata_json": {"source_id": reference, "project_scope": [ALPHA]}})
        unrelated = store.create_memory({"memory_key": "unrelated", "canonical_text": "Synthetic", "domain": "project", "sensitivity": "public", "metadata_json": {"note": encoded_object("source_id", str(uuid4()))}})
        before = {table: list(conn.execute(f"SELECT * FROM {table} ORDER BY id")) for table in ("sources", "memories", "event_log", "provenance_links")}
        assert ("source", str(source["id"])) in dependencies_of("memory", copy)
        assert {str(row["id"]) for row in walk_dependants(store, [str(source["id"])])} == {str(copy["id"])}
        assert count_rows_hidden_by_scope_move(store, source, [BETA]) == 1
        assert {table: list(conn.execute(f"SELECT * FROM {table} ORDER BY id")) for table in before} == before
        store.update_source(source_id=str(source["id"]), patch={"sensitivity": "confidential", "metadata_json": {"project_scope": [BETA]}})
        stored = store.get_memory(str(copy["id"]))
        assert stored["sensitivity"] == "confidential"
        assert BETA in stored["metadata_json"]["project_floor"]
        assert stored["metadata_json"]["source_id"] == reference
        assert store.get_memory(str(unrelated["id"]))["sensitivity"] == "public"


@pytest.mark.parametrize("key,kind", [("source_id", "source"), ("memory_id", "memory"), ("artifact_id", "artifact"), ("belief_ids", "belief")])
def test_unicode_encoded_metadata_keys_and_ids_are_not_cut_before_the_parser(tmp_path, key, kind):
    path = tmp_path / "encoded-keys.sqlite3"
    bootstrap_database(path, user_id=USER, user_email="synthetic@example.test")
    row_id = str(uuid4())
    value = [row_id] if key.endswith("ids") else row_id
    metadata = json.dumps({key: value, "consolidation": {}})
    encoded = "".join("\\u%04x" % ord(character) if character.isalnum() or character == "_" else character for character in metadata)
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        row = store.create_memory({"memory_key": "raw-encoded", "canonical_text": "Synthetic", "domain": "project", "sensitivity": "public"})
        conn.execute("UPDATE memories SET metadata_json = ? WHERE id = ?", (encoded, row["id"]))
        stored = store.get_memory(str(row["id"]))
        assert (kind, row_id) in dependencies_of("memory", stored)
        assert {str(item["id"]) for item in walk_dependants(store, [row_id])} == {str(row["id"])}


@pytest.mark.parametrize("key,kind", [("source_id", "source"), ("artifact_id", "artifact")])
def test_encoded_value_object_keeps_the_same_reverse_edge(tmp_path, key, kind):
    path = tmp_path / "encoded-value.sqlite3"
    bootstrap_database(path, user_id=USER, user_email="synthetic@example.test")
    row_id = str(uuid4())
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        row = store.create_memory({"memory_key": "encoded-value", "canonical_text": "Synthetic", "domain": "project", "sensitivity": "public"})
        # The dependency lives in the encoded value alone. A marker without
        # the root id states the row class without making SQL's id term pass.
        metadata = {"consolidation": {}}
        conn.execute("UPDATE memories SET value = ?, metadata_json = ? WHERE id = ?", (json.dumps(encoded_object(key, row_id)), json.dumps(metadata), row["id"]))
        stored = store.get_memory(str(row["id"]))
        assert (kind, row_id) in dependencies_of("memory", stored)
        assert {str(item["id"]) for item in walk_dependants(store, [row_id])} == {str(row["id"])}

