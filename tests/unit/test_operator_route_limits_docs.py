"""The pages name each operator route that applies the caller's limits, and the limits the sweep found and left.

The sentences are pinned by phrase. The behaviour is tested in ``tests/integration/test_operator_routes_limits_postgres.py``
and, for the recent-commits list on SQLite, in ``tests/unit/test_recent_commits_limits_sqlite.py``.

Mutations, each one alone: delete a route from the table of the tool reference; delete ``reading at most 2,000 commits``
from the tool reference; delete ``answers as no charter`` from the tool reference, the changelog or the security note;
delete ``the sweep holds that route as an expected failure`` from the changelog; delete ``take no agent key`` from the
security note; delete the row for the workspace event feed from the tool reference; delete ``the four connector events`` from
the changelog or the security note; delete the sentence that the event feed still lists the id of a queued task, a person
and the charter from any of the three pages; say again that the clipper capture route answers cursors to a capability
holder; delete ``so a trusted key cannot learn how many confidential commits exist`` from the security note; say
again that the refused charter save writes nothing; delete the sentence that the refusal tells the key a charter exists.
"""

from __future__ import annotations

from pathlib import Path

from alicebot_api.vnext_label_guard import ROW_FEED_SCAN_LIMIT

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.20.0):"

#: The routes this change closes, as the pages name them.
ROUTES = (
    "GET /v0/vnext/memories/recent-commits",
    "GET /v0/vnext/context-tree",
    "POST /v0/vnext/open-loops/extract",
    "GET /v0/vnext/settings/brain-charter",
    "PUT /v0/vnext/settings/brain-charter",
    "GET /v0/vnext/workspace",
    "GET /v0/vnext/connectors/{connector_name}/status",
    "GET /v0/vnext/connectors",
    "GET /v0/vnext/connectors/health",
    "GET /v0/vnext/dogfooding",
    "GET /v0/vnext/doctor",
    "POST /v0/vnext/doctor/run",
    "POST /v0/vnext/connectors/{connector_name}/sync",
)


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_the_tool_reference_names_every_route_and_what_a_key_with_a_ceiling_is_shown() -> None:
    tools = _text("docs/alpha/mcp-tools.md")
    paragraph = tools.split(f"{MARK} thirteen operator routes, and one legacy tool, apply the caller's limits", 1)[1]
    paragraph = paragraph.split("\n\n## ", 1)[0]
    for route in ROUTES:
        assert f"`{route}`" in paragraph, route
    assert "`alice_vnext_recent_memory_commits`" in paragraph
    assert "judged on effective labels (the stored label raised to the labels of every input)" in paragraph
    assert "`count` is the length of that list" in paragraph
    assert f"reading at most {ROW_FEED_SCAN_LIMIT:,} commits to fill it" in paragraph
    assert "A listed commit loses the saved quote of a source the caller may not read" in paragraph
    assert "holds a call that declares a permission profile to that profile" in paragraph
    assert "select what to show and never widen it" in paragraph
    assert 'answers as no charter, `{"brain_charter": null}`' in paragraph
    assert "HTTP 403 when the stored charter is above the key's ceiling" in paragraph
    assert "No charter row changes, the refusal repeats none of the charter's labels, and the policy event of the refusal is recorded" in paragraph
    assert "The refusal tells the key that a charter above its ceiling exists" in paragraph
    assert "Nothing is written" not in paragraph and "writes nothing" not in paragraph
    assert "The read of the stored charter and the save run under one lock" in paragraph
    assert "`last_captured_item` (the source id and the external id of the last import, which for a file is its path)" in paragraph
    assert "`cursor_state` (the cursor of the last imported item, which for a file is also its path)" in paragraph
    assert "A cursor that no import carries cannot be tied to a source and is not shown" in paragraph
    assert "`previous_cursor` and `sync_cursor` of the answer follow the same rule" in paragraph
    assert "a connector keeps one cursor, and an item at or below it is skipped and counted in `skipped_count`" in paragraph
    assert "browser-clipper" not in paragraph
    assert "`recent_events` of `GET /v0/vnext/workspace`, and the traceability items built from it" in paragraph
    for event in ("connector.state_updated", "connector.sync_started", "connector.sync_completed", "connector.sync_failed"):
        assert f"`{event}`" in paragraph, event
    assert "`cursor_value`" in paragraph and "`previous_cursor` and `sync_cursor`" in paragraph
    assert "The events are still listed and counted, and the stored event is not edited" in paragraph
    assert "the workspace event feed still lists the id of a queued task, a person and the charter above the ceiling" in paragraph
    assert "the owner and an unbound `admin_agent` key are shown what they were" in paragraph
    assert "A route added without a probe fails the test" in paragraph
    assert "`GET /v0/vnext/graph/neighborhood/{target_id}` still lists an edge whose far end the key may not read" in paragraph
    assert "`recent_failures` of the connector status names the id the importer gave to an item that failed" in paragraph


def test_the_saved_quotes_list_no_longer_says_the_recent_commits_tool_has_no_fence() -> None:
    tools = _text("docs/alpha/mcp-tools.md")
    assert "lists commit rows with no row-level fence" not in tools


def test_the_changelog_has_one_entry_for_the_operator_routes() -> None:
    entries = [line.removeprefix("- ") for line in _text("CHANGELOG.md").splitlines() if line.startswith("- ")]
    matches = [entry for entry in entries if entry.startswith(f"{MARK} thirteen operator routes and one legacy tool apply")]
    assert len(matches) == 1
    entry = matches[0]
    assert "v0.19.2" in entry and "v0.20.0" in entry
    for route in ROUTES:
        assert f"`{route}`" in entry, route
    assert "`alice_vnext_recent_memory_commits`" in entry
    assert "a charter above the ceiling answers as no charter" in entry
    assert "changes no charter row and records the policy event of the refusal" in entry
    assert "the refusal tells the key that a charter above its ceiling exists" in entry
    assert "and writes nothing" not in entry
    assert "the read of the stored charter and the save run under one lock" in entry
    assert "the same cursors were in the answer of `POST /v0/vnext/connectors/{connector_name}/sync`" in entry
    assert "On main 13 of the 72 routes answered with a sentinel or changed a hidden row" in entry
    assert "`--limit` must be at least 1" in entry
    assert "skipped_count" in entry and "browser-clipper" not in entry
    assert "in the event feed of `GET /v0/vnext/workspace` (`recent_events`, and the traceability items built from it)" in entry
    assert "of the failed syncs and of the four connector events in the workspace feed are shown only when the key may read the source" in entry
    assert "an event with a cursor that is not shown is copied and the stored event is not edited, and the event count does not change" in entry
    assert "the workspace event feed still lists the id of a queued task, a person and the charter above the ceiling" in entry
    assert "`agent.task_created`, `queue.task_enqueued`, `task.created`, `person.created`, `brain_charter.upserted`" in entry
    assert "takes `identity` with no default" in entry
    assert "the sweep holds that route as an expected failure" in entry
    assert "`POST /v0/vnext/queue/process-next` answers HTTP 500 to every caller on PostgreSQL" in entry
    assert "take no agent key" in entry
    assert entry.endswith("No migration is required.")


def test_the_security_note_covers_disclosure_the_charter_write_and_the_limits_it_leaves() -> None:
    note = _text("docs/release/derived-labels-security-note-draft.md")
    paragraph = note.split(f"{MARK} thirteen operator routes and one legacy tool now apply the caller's limits", 1)[1].split("\n", 1)[0]
    for route in ROUTES[:3] + ("GET /v0/vnext/settings/brain-charter", "GET /v0/vnext/connectors/{connector_name}/status"):
        assert route in paragraph, route
    assert "identical in v0.19.2 and v0.20.0, so none is a regression of this set" in paragraph
    assert "The exposure covers disclosure, and for the charter unauthorized modification" in paragraph
    assert "so a trusted key cannot learn how many confidential commits exist" in paragraph
    assert "A charter above the ceiling answers as no charter" in paragraph
    assert "no charter row changes and the policy event of the refusal is recorded" in paragraph
    assert "the refusal tells the key that a charter above its ceiling exists" in paragraph
    assert "The read of the stored charter and the save run under one lock" in paragraph
    assert "are shown to a key only when it may read the source they come from, and are `null` otherwise" in paragraph
    assert "its external id, which for a file is its path" in paragraph
    assert "so a key that syncs can learn whether an item it sent sorts below a cursor it may not read" in paragraph
    assert "browser-clipper" not in paragraph
    assert "and in the event feed of `GET /v0/vnext/workspace`, whose `connector.state_updated`, `connector.sync_started`" in paragraph
    assert "after any later sync, its own included, the path the sync started from" in paragraph
    assert "in the health block, in the sync answer, in the failed syncs and in the four connector events of the workspace feed" in paragraph
    assert "the stored event is not edited, and the event count does not change" in paragraph
    assert "the workspace event feed still lists the id of a queued task, a person and the charter above the ceiling" in paragraph
    assert "the events hold the id and the names of the fields and no text" in paragraph
    assert "the probe table must list exactly the routes of the application" in paragraph
    assert "graph neighborhood still lists an edge whose far end the key may not read" in paragraph
    assert "such as a file path" in paragraph
    assert "take no agent key and list rows whatever their label while `APP_ENV` is `development` or `test`" in paragraph
    assert "answer HTTP 404 in any other environment" in paragraph
    assert "`POST /v0/vnext/queue/process-next` answers HTTP 500 to every caller on PostgreSQL" in paragraph
