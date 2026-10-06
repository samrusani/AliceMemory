"""SQL shortcuts cannot put a derived or uncertain row in the original partition."""
import json
import sqlite3

import pytest

from alicebot_api.sqlite_store import _direct_source_hint
from alicebot_api.vnext_derived_labels import MARKER_KEYS, DERIVED_ARTIFACT_TYPES, is_derived
from alicebot_api.vnext_label_sql import original_label_sql


@pytest.mark.parametrize("kind", ["source", "memory", "open_loop", "artifact", "project"])
def test_sqlite_original_partition_is_conservative_for_legacy_and_marker_shapes(kind):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE rows(metadata_json TEXT, artifact_type TEXT)")
    shapes = [{}, {"observation": 1}, [], None, "text", json.dumps({"source_id": "legacy-parent"})]
    shapes += [{key: value} for key in MARKER_KEYS for value in (None, "", "id", [], {}, True, 3)]
    shapes += [{"redacted": True, "source_id": "parent"}, {"derived_from": {"sources": ["parent"], "counts": {"sources": 1}}}]
    types = ["other", *sorted(DERIVED_ARTIFACT_TYPES)] if kind == "artifact" else ["other"]
    predicate = original_label_sql(kind, sqlite=True)
    for metadata in shapes:
        for artifact_type in types:
            raw = json.dumps(metadata)
            conn.execute("DELETE FROM rows")
            conn.execute("INSERT INTO rows VALUES(?,?)", (raw, artifact_type))
            original = conn.execute(f"SELECT {predicate} FROM rows").fetchone()[0]
            if original:
                assert not is_derived(kind, {"metadata_json": metadata, "artifact_type": artifact_type}), (kind, metadata, artifact_type)
    conn.close()


@pytest.mark.parametrize("metadata,expected", [
    ('{"source_id":"hidden","source_id":"visible"}', "visible"),
    ('{"source_\\u0069d":"hidden"}', "hidden"),
    (json.dumps(json.dumps({"source_id": "hidden"})), "hidden"),
    ('{"source_id":"hidden","redacted":true}', None),
    ('{"source_id":"hidden","redacted":"true"}', "hidden"),
    ('{"source_id":["hidden"]}', None),
    ('not JSON', None),
    ('{"source_id":"11111111111141118111111111111111"}', "11111111-1111-4111-8111-111111111111"),
])
def test_sqlite_parent_hint_matches_store_and_kernel_decoding(metadata, expected):
    assert _direct_source_hint(metadata) == expected
