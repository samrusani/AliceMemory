"""The producers that need PostgreSQL store strings in ``source_refs`` too, so the list of field names a withheld entry keeps is complete.

Unreleased (on main, not in v0.20.0). ``PRODUCT_REF_KEYS`` (``vnext_source_fence.py``) is the set of field names the reader of
saved quotes keeps in an entry that names a memory the caller may not read; any other name is dropped with its value because a
writer can type words as a name. The SQLite twin, ``tests/unit/test_saved_quote_producer_ref_keys.py``, runs the doors that work
on SQLite. This test runs the rest on PostgreSQL: it builds the vault of the operator route sweep (sources, memories, a commit
through the HTTP route, the scheduler's daily brief, weekly synthesis, open-loop review, memory consolidation and project update
scan, a connector sync), then a memory proposal over HTTP and a commit that cites a source and a URL by string, and reads every
JSON column of the database for ``source_refs``. Every entry a producer stored must be a string, and no field name outside the
list may be stored.

The vault plants a few rows by hand (a derived report whose ``source_refs`` hold ids); they are strings as well. The test does not
count a client's object: the routes that take one (the commit route, the proposal route and the agent-output ingest) are sent
strings here, and the entries found are the producers' own.

Mutation: have the daily brief's ``_source_refs`` (``vnext_brain.py``) return ``{"source_id": ..., "label": ...}`` entries instead of
strings: the test fails on the object. It is in the manifest.
"""
from __future__ import annotations

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.operator_route_vault import Vault
from tests.unit.test_saved_quote_producer_ref_keys import _lists_under_source_refs, _names_in

from alicebot_api.vnext_source_fence import PRODUCT_REF_KEYS


def _stored_entries(harness) -> list[tuple[str, str, object]]:
    """``(table, column, entry)`` for every entry of every ``source_refs`` list in a JSON column of any table."""

    found: list[tuple[str, str, object]] = []
    with harness.store() as store, store.conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND data_type IN ('json', 'jsonb') ORDER BY table_name, column_name"
        )
        columns = [(row["table_name"], row["column_name"]) for row in cur.fetchall()]
        for table, column in columns:
            cur.execute(f'SELECT "{column}" AS body FROM "{table}" WHERE "{column}" IS NOT NULL')  # names come from the catalog
            for row in cur.fetchall():
                for refs in _lists_under_source_refs(row["body"]):
                    found.extend((table, column, entry) for entry in refs)
    return found


def test_the_producers_that_need_postgres_store_strings_and_no_name_outside_the_list(label_harness) -> None:
    """Every ``source_refs`` entry stored by the producers of the vault is a string, and the names inside the entries (a JSON text
    is decoded) are all in ``PRODUCT_REF_KEYS``. The test requires that the scheduler's work and a memory the commit route made are
    among the rows it read, so a scan that finds nothing cannot pass.
    """

    vault = Vault(label_harness, "pk").build()
    source = vault.ids["source_shown"]
    status, body = vault.admin_request(
        "POST",
        "/v0/vnext/memories/commit",
        {
            "title": "Kiln schedule", "canonical_text": "The alpha kiln is fired on Mondays.", "memory_type": "fact",
            "domain": "project", "sensitivity": "public", "confidence": 0.99, "source_type": "agent",
            "source_refs": [f"source:{source}", "https://example.test/kiln"],
        },
    )
    assert status in {200, 201}, (status, str(body)[:300])
    status, body, _headers = label_harness.request(
        "POST",
        "/v0/vnext/memory-proposals",
        payload={
            "title": "Thermocouple", "canonical_text": "The alpha kiln needs a new thermocouple.",
            "source_refs": [f"source:{source}"], "domain": "project", "sensitivity": "public",
        },
        key=vault.keys["admin"],
    )
    assert status in {200, 201}, (status, str(body)[:300])
    entries = _stored_entries(label_harness)
    where = {(table, column) for table, column, _entry in entries}
    assert ("memories", "metadata_json") in where and ("generated_artifacts", "metadata_json") in where, sorted(where)
    assert len(entries) >= 10, "the producers stored refs; a scan that finds none proves nothing"
    objects = [(table, column) for table, column, entry in entries if not isinstance(entry, str)]
    assert objects == [], sorted(set(objects))
    names = set().union(*(_names_in(entry) for _table, _column, entry in entries))
    assert names - PRODUCT_REF_KEYS == set(), sorted(names - PRODUCT_REF_KEYS)
