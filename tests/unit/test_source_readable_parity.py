"""One predicate for what a caller may read: the passage stage, the title list and the vector mask agree.

``search_source_chunks`` (the passage stage), ``search_sources`` (the title and recency list) and
``readable_source_ids`` (the mask of the vector stage) all take their conditions on a source row from
``readable_source_clause``. This test asks the three the same question over a grid of domains, sensitivities,
project scopes, people and time windows, and fails when they stop agreeing.
"""

from __future__ import annotations

from pathlib import Path

from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.unit.passage_vector_support import USER_ID, add_note, fixed_id, make_vault



def test_the_two_search_reads_and_the_mask_agree_over_a_grid_of_labels_and_scopes(tmp_path: Path) -> None:
    """One predicate, three callers. Mutation: let ``search_sources`` write its own domain clause again."""

    from datetime import UTC, datetime

    from alicebot_api.vnext_stores.sqlite.source_readable import readable_source_ids

    database = make_vault(tmp_path)
    cases = [
        ("note1", "public", "project", {"project_scope": ["acme"]}, "2026-01-10T00:00:00Z"),
        ("note2", "internal", "project", {"project_scope": ["other"]}, "2026-03-10T00:00:00Z"),
        ("note3", "confidential", "project", {"project_scope": ["acme"]}, "2026-05-10T00:00:00Z"),
        ("note4", "public", "personal", {"project_scope": ["acme"], "people": ["bob"]}, "2026-07-10T00:00:00Z"),
        ("note5", "private", "unknown", {"people": ["Bob"]}, "2026-09-10T00:00:00Z"),
        ("note6", "public", "health", {}, "2026-11-10T00:00:00Z"),
        ("note7", "unknown", "project", {"project_scope": ["acme", "beta"]}, "2026-02-10T00:00:00Z"),
    ]
    for name, sensitivity, domain, metadata, captured in cases:
        add_note(database, name, [f"{name} shared parity word alpha"], sensitivity=sensitivity, domain=domain,
                 metadata=metadata, captured_at=captured)
    # One source gone, which no read may return.
    add_note(database, "gone", ["gone shared parity word alpha"])
    with sqlite_user_connection(database, USER_ID) as conn:
        conn.execute("UPDATE sources SET deleted_at = '2026-01-01T00:00:00Z' WHERE id = ?", (fixed_id("source", "gone"),))
        # A title every source holds, for the title read.
        conn.execute("UPDATE sources SET title = title || ' parity'")
        conn.execute("UPDATE sources SET title = 'gone parity' WHERE id = ?", (fixed_id("source", "gone"),))
    windows = [(None, None), (datetime(2026, 4, 1, tzinfo=UTC), None), (None, datetime(2026, 8, 1, tzinfo=UTC)),
               (datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))]
    compared = 0
    with sqlite_user_connection(database, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        for domains in (None, ["project"], ["project", "personal"], []):
            for sensitivity in (None, ["public"], ["public", "internal"], ["unknown", "private"]):
                for projects in ((), ("acme",), ("acme", "beta"), ("other",)):
                    for people in ((), ("bob",)):
                        for start, end in windows:
                            kwargs = dict(domains=domains, sensitivity_allowed=sensitivity, scope_projects=projects,
                                          scope_people=people, scope_window_start=start, scope_window_end=end)
                            mask = set(readable_source_ids(store, **kwargs))  # type: ignore[arg-type]
                            chunks = {str(r["source_id"]) for r in store.search_source_chunks(query="parity", limit=100, **kwargs)}  # type: ignore[arg-type]
                            titles = {str(r["id"]) for r in store.search_sources(query="parity", limit=100, **kwargs)}  # type: ignore[arg-type]
                            assert mask == chunks == titles, (kwargs, mask ^ chunks, mask ^ titles)
                            compared += 1
    assert compared == 4 * 4 * 4 * 2 * 4
    assert fixed_id("source", "gone") not in mask
