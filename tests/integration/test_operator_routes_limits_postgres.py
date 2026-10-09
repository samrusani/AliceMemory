"""Every /v0/vnext route, asked by a key with a ceiling for the most it will give, answers nothing above the ceiling.

The probe table (``operator_route_probes.py``) holds one probe per route the mounted application has. The unit test
``tests/unit/test_operator_route_inventory.py`` requires its keys to be exactly the routes of the application, read at
run time, so a route added without a probe fails there. Each probe asks as much as the route lets a caller ask: the id
of a hidden row, every sensitivity where the route takes a filter, the exact text of a hidden row where the route
deduplicates.

Each call here runs on PostgreSQL through the mounted application, with real keys, over a vault that holds a hidden and
a visible row of every kind and a hidden row built from a hidden input. The trusted key (ceiling: public, internal,
private, unknown, no project) must get no sentinel in any answer and change no hidden row. The unbound admin key and the
owner, who have no ceiling, must still be shown the rows, so a probe that reaches nothing cannot pass.
"""
from __future__ import annotations

import json

import pytest

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.operator_route_probes import PENDING, PROBES, Call, control_routes
from tests.integration.operator_route_runner import carried, run_call
from tests.integration.operator_route_vault import Vault


def _route_param(route):
    marks = []
    if route in PENDING:
        marks.append(pytest.mark.xfail(strict=True, reason=PENDING[route]))
    return pytest.param(route, id=f"{route[0]} {route[1]}", marks=marks)


def _hidden_in(vault: Vault, text: str) -> list[str]:
    return [name for name in carried(vault, text) if vault.is_hidden(name)]


@pytest.mark.parametrize("route", [_route_param(route) for route in sorted(PROBES)])
def test_a_key_with_a_ceiling_is_shown_nothing_above_it(label_harness, route):
    vault = Vault(label_harness, "t").build()
    method, template = route
    before = vault.hidden_rows()
    for call in PROBES[route](vault):
        status, text = run_call(vault, method, template, call, vault.keys["trusted"])
        assert _hidden_in(vault, text) == [], (call.name, status)
        if call.statuses:
            assert status in call.statuses, (call.name, status, text[:300])
        assert [name for name in call.trusted_sees if vault.text(name) not in text] == [], (call.name, text[:300])
        if call.mutates:
            assert vault.hidden_rows() == before, call.name
    assert vault.hidden_rows() == before


@pytest.mark.parametrize("route", control_routes(), ids=lambda route: f"{route[0]} {route[1]}")
def test_the_unbound_admin_key_is_still_shown_the_hidden_rows(label_harness, route):
    vault = Vault(label_harness, "a").build()
    method, template = route
    for call in PROBES[route](vault):
        if not call.admin_sees:
            continue
        _status, text = run_call(vault, method, template, call, vault.keys["admin"])
        assert [name for name in call.admin_sees if vault.text(name) not in text] == [], (call.name, text[:300])


@pytest.mark.parametrize("route", control_routes(), ids=lambda route: f"{route[0]} {route[1]}")
def test_the_owner_is_still_shown_the_hidden_rows(label_harness, route):
    vault = Vault(label_harness, "o", owner=True).build()
    method, template = route
    for call in PROBES[route](vault):
        if not call.admin_sees:
            continue
        _status, text = run_call(vault, method, template, call, None)
        assert [name for name in call.admin_sees if vault.text(name) not in text] == [], (call.name, text[:300])


# -- the routes this change closes, one by one ----------------------------------------------------------------------

PROFILES = {
    # profile name -> (permission profile, bound to a project)
    "admin": ("admin_agent", False),
    "admin_bound": ("admin_agent", True),
    "trusted": ("trusted_local_agent", False),
    "read_only": ("read_only_agent", False),
    "memory_proposal": ("memory_proposal_agent", False),
    "trusted_bound": ("trusted_local_agent", True),
    "alpha_only": ("project_scoped_agent", True),
}


def _get(vault: Vault, path: str, key, **query):
    return run_call(vault, "GET", path, Call("ad hoc", query=query), key)


def test_recent_commits_lists_and_counts_only_what_each_profile_may_read(label_harness):
    vault = Vault(label_harness, "r").build()
    project = vault.ids["project_shown"]
    keys = {
        name: label_harness.key(permission, project=project if bound else None)
        for name, (permission, bound) in PROFILES.items()
        if name not in {"admin", "trusted"}
    }
    keys["admin"], keys["trusted"] = vault.keys["admin"], vault.keys["trusted"]
    path = "/v0/vnext/memories/recent-commits"
    for name in PROFILES:
        status, text = _get(vault, path, keys[name], limit=100)
        if name == "admin":
            body = json.loads(text)
            assert status == 200
            assert vault.text("commit_hidden-text") in text and vault.text("commit_derived-text") in text
            assert body["count"] == len(body["recent_commits"])
        elif name == "trusted":
            body = json.loads(text)
            assert status == 200
            texts = [row["canonical_text"] for row in body["recent_commits"]]
            # The public commit is shown, and every commit above the ceiling or built from one is not.
            assert texts == [vault.text("commit_shown-text")]
            assert body["count"] == 1
            assert _hidden_in(vault, text) == []
            # The newest commit is hidden from this key, so a one-row list is filled from older commits.
            status, text = _get(vault, path, keys[name], limit=1)
            body = json.loads(text)
            assert [row["canonical_text"] for row in body["recent_commits"]] == [vault.text("commit_shown-text")]
            assert body["count"] == 1
        else:
            # The central gate refuses a key that is not an unbound trusted or admin key, before the handler.
            assert status == 403, (name, text[:200])
            assert _hidden_in(vault, text) == []


def test_recent_commits_for_an_owner_without_keys_lists_every_commit(label_harness):
    vault = Vault(label_harness, "ro", owner=True).build()
    status, text = _get(vault, "/v0/vnext/memories/recent-commits", None, limit=100)
    body = json.loads(text)
    assert status == 200 and body["count"] == len(body["recent_commits"])
    for name in ("commit_hidden-text", "commit_confirmed_hidden-text", "commit_derived-text", "commit_shown-text"):
        assert vault.text(name) in text


def _event_rows(vault: Vault) -> list[dict]:
    with vault.harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id, event_type, target_type, target_id, actor_id FROM event_log")
        return [{**row, "id": str(row["id"])} for row in cur.fetchall()]


def _event_ids(vault: Vault) -> set[str]:
    return {row["id"] for row in _event_rows(vault)}


def _row_counts(vault: Vault) -> dict[str, int]:
    """The number of rows in every table of the vault's schema, to show a call wrote nothing but what it should."""

    with vault.harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
        names = sorted(row["table_name"] for row in cur.fetchall())
        counts = {}
        for name in names:
            cur.execute(f"SELECT count(*) AS n FROM {name}")  # closed list of table names read from the catalog
            counts[name] = cur.fetchone()["n"]
        return counts


def test_the_charter_is_one_row_and_a_key_that_cannot_read_it_cannot_replace_it(label_harness):
    vault = Vault(label_harness, "c").build()
    path = "/v0/vnext/settings/brain-charter"
    trusted, admin = vault.keys["trusted"], vault.keys["admin"]
    before = vault.hidden_rows()
    status, text = _get(vault, path, trusted)
    assert (status, json.loads(text)) == (200, {"brain_charter": None})
    rows_before = _row_counts(vault)
    events_before = _event_ids(vault)
    status, text = run_call(vault, "PUT", path, Call("ad hoc", body={"content_markdown": "mine"}), trusted)
    assert status == 403 and _hidden_in(vault, text) == []
    assert vault.hidden_rows() == before
    # The refusal records its policy event: the decision and the block, for this route and this key, and no other row.
    added = [row for row in _event_rows(vault) if row["id"] not in events_before]
    assert sorted(row["event_type"] for row in added) == ["agent.policy_blocked", "policy.decision"]
    assert {(row["target_type"], row["target_id"], row["actor_id"]) for row in added} == {
        ("http_route", path, vault.agent_ids["trusted_local_agent"])
    }
    after = _row_counts(vault)
    assert {table: after[table] - rows_before[table] for table in after if after[table] != rows_before[table]} == {
        "event_log": 2
    }
    # The unbound admin key still reads it and replaces it, and once it is private the trusted key does both.
    status, text = _get(vault, path, admin)
    assert status == 200 and vault.text("charter-body") in text
    status, text = run_call(
        vault, "PUT", path, Call("ad hoc", body={"content_markdown": "now private", "sensitivity": "private"}), admin
    )
    assert status == 200
    status, text = _get(vault, path, trusted)
    assert status == 200 and json.loads(text)["brain_charter"]["content_markdown"] == "now private"
    status, text = run_call(vault, "PUT", path, Call("ad hoc", body={"content_markdown": "by the key"}), trusted)
    assert status == 200 and json.loads(text)["brain_charter"]["content_markdown"] == "by the key"


def test_the_workspace_lists_the_tasks_and_the_charter_a_key_may_read(label_harness):
    vault = Vault(label_harness, "w").build()
    path = "/v0/vnext/workspace"
    status, text = _get(vault, path, vault.keys["trusted"])
    body = json.loads(text)
    assert status == 200
    assert [task["title"] for task in body["tasks"]] == [vault.text("task_shown-title")]
    assert body["brain_charter"] is None
    status, text = _get(vault, path, vault.keys["admin"])
    body = json.loads(text)
    assert sorted(task["title"] for task in body["tasks"]) == sorted(
        [vault.text("task_shown-title"), vault.text("task_hidden-title")]
    )
    assert body["brain_charter"]["content_markdown"] == vault.text("charter-body")


def test_the_connector_status_lists_the_captures_a_key_may_read(label_harness):
    vault = Vault(label_harness, "s").build()
    path = "/v0/vnext/connectors/local_folder/status"
    status, text = _get(vault, path, vault.keys["trusted"])
    titles = [row["title"] for row in json.loads(text)["recent_captures"]]
    assert status == 200 and vault.text("capture_shown-title") in titles
    assert not any(vault.is_hidden(name) and vault.text(name) in " ".join(titles) for name in vault._texts)
    status, text = _get(vault, path, vault.keys["admin"])
    titles = [row["title"] for row in json.loads(text)["recent_captures"]]
    assert vault.text("capture_hidden-title") in titles and vault.text("capture_shown-title") in titles


def _sync(vault: Vault, key, items, *, sensitivity="public", path="/v0/vnext/connectors/local_folder/sync"):
    return run_call(
        vault, "POST", path, Call("ad hoc", body={"default_sensitivity": sensitivity, "items": items}), key
    )


def _health(vault: Vault, key) -> dict:
    status, text = _get(vault, "/v0/vnext/connectors/local_folder/status", key)
    assert status == 200
    return json.loads(text)


def test_the_connector_screens_name_a_capture_and_a_cursor_only_to_a_caller_who_may_read_the_source(label_harness):
    vault = Vault(label_harness, "h").build()
    trusted, admin = vault.keys["trusted"], vault.keys["admin"]
    hidden_path = f"/vault/{vault.text('synced-file')}.md"
    hidden_source = vault.ids["synced_source"]

    # The last import is a confidential file, and its path is the cursor. Only the unbound admin key is shown either.
    seen = _health(vault, admin)["health"]
    assert seen["last_captured_item"]["external_id"] == hidden_path and seen["last_captured_item"]["source_id"] == hidden_source
    assert seen["cursor_state"] == hidden_path
    held = _health(vault, trusted)
    assert held["health"]["last_captured_item"] is None and held["health"]["cursor_state"] is None
    assert held["health"]["items_captured"] == seen["items_captured"]
    # The failed sync recorded both cursors. The key is shown the failure and not the cursors.
    failed = [row for row in held["recent_failures"] if row["event_type"] == "connector.sync_failed"]
    assert failed and all(row["payload_json"]["previous_cursor"] is None for row in failed)
    assert all(row["payload_json"]["sync_cursor"] is None for row in failed)
    assert vault.text("synced-file") not in json.dumps(held)
    unlimited = [row for row in _health(vault, admin)["recent_failures"] if row["event_type"] == "connector.sync_failed"]
    assert unlimited and all(row["payload_json"]["sync_cursor"] == hidden_path for row in unlimited)

    # A sync by the key answers with the cursors it may read: its item sorts below the cursor, so it is skipped.
    status, text = _sync(vault, trusted, [{"path": "/vault/A.md", "title": "a", "text": "a note the key sent"}])
    answer = json.loads(text)
    assert status == 201 and answer["skipped_count"] == 1
    assert answer["previous_cursor"] is None and answer["sync_cursor"] is None
    assert vault.text("synced-file") not in text
    status, text = _sync(vault, admin, [{"path": "/vault/B.md", "title": "b", "text": "a note the admin sent"}])
    assert status == 201 and json.loads(text)["previous_cursor"] == hidden_path

    # A failed item holds the cursor back, so the cursor still names the confidential file while the last import is a
    # public one. The two are judged apart.
    public_a = f"/vault/{vault.shown('synced-a')}.md"
    status, text = _sync(
        vault, admin,
        [{"path": "", "external_id": "x"}, {"path": public_a, "title": "t", "text": "public a", "mtime_ns": 7}],
    )
    assert status == 207, text
    seen = _health(vault, admin)["health"]
    assert seen["last_captured_item"]["external_id"] == public_a and seen["cursor_state"] == hidden_path
    held = _health(vault, trusted)["health"]
    assert held["last_captured_item"]["external_id"] == public_a
    assert held["cursor_state"] is None

    # Once a public import moves the cursor, the key is shown it.
    public_b = f"/vault/{vault.shown('synced-b')}.md"
    status, text = _sync(vault, admin, [{"path": public_b, "title": "t", "text": "public b", "mtime_ns": 9}])
    assert status == 201, text
    held = _health(vault, trusted)["health"]
    assert held["cursor_state"] == f"9:{public_b}" and held["last_captured_item"]["external_id"] == public_b
    assert vault.text("synced-file") not in json.dumps(_health(vault, trusted))


def test_the_connector_health_of_the_owner_is_the_block_as_stored(label_harness):
    vault = Vault(label_harness, "ho", owner=True).build()
    hidden_path = f"/vault/{vault.text('synced-file')}.md"
    seen = _health(vault, None)["health"]
    assert seen["last_captured_item"]["external_id"] == hidden_path and seen["cursor_state"] == hidden_path
    status, text = _get(vault, "/v0/vnext/connectors/health", None)
    local = next(item for item in json.loads(text)["items"] if item["connector_name"] == "local_folder")
    assert status == 200 and local["last_captured_item"]["external_id"] == hidden_path
    assert local["cursor_state"] == hidden_path


_CURSOR_FIELDS = ("cursor_value", "previous_cursor", "sync_cursor")


def _connector_events(vault: Vault, key) -> tuple[str, list[dict]]:
    """The text of the workspace answer and the connector events in its event feed."""

    status, text = _get(vault, "/v0/vnext/workspace", key)
    assert status == 200
    return text, [event for event in json.loads(text)["recent_events"] if str(event["event_type"]).startswith("connector.")]


def _cursors(events: list[dict]) -> list[str]:
    return [event["payload_json"][name] for event in events for name in _CURSOR_FIELDS if event["payload_json"].get(name)]


def _keys_only_vault(label_harness, tag: str) -> Vault:
    """A vault with the two keys and nothing else, so the first events in it are the ones a test makes."""

    vault = Vault(label_harness, tag)
    vault.keys["admin"] = label_harness.key("admin_agent")
    vault.keys["trusted"] = label_harness.key("trusted_local_agent")
    return vault


def test_the_workspace_event_feed_shows_no_path_of_a_confidential_import_to_a_key_with_a_ceiling(label_harness):
    vault = _keys_only_vault(label_harness, "ev")
    trusted, admin = vault.keys["trusted"], vault.keys["admin"]
    hidden_path = f"/vault/{vault._text('synced-file', hidden=True)}.md"

    # Right after a confidential import: the connector status already holds the path back from the key, and so must the feed.
    status, text = _sync(vault, admin, [{"path": hidden_path, "title": "t", "text": "a confidential note"}], sensitivity="confidential")
    assert status == 201, text
    assert _health(vault, trusted)["health"]["cursor_state"] is None
    text, held = _connector_events(vault, trusted)
    assert vault.text("synced-file") not in text
    # The event is in the feed and does not carry the cursor, so the check above has something to fail on.
    assert [event["event_type"] for event in held] == ["connector.state_updated", "connector.sync_started"]
    assert _cursors(held) == []
    text, seen = _connector_events(vault, admin)
    assert hidden_path in _cursors(seen) and vault.text("synced-file") in text

    # A sync by the key itself: it is told no previous cursor, and the feed does not tell it either.
    own = "/zz/after.md"
    status, text = _sync(vault, trusted, [{"path": own, "title": "a", "text": "b", "mtime_ns": 5}])
    assert status == 201 and json.loads(text)["previous_cursor"] is None, text
    text, held = _connector_events(vault, trusted)
    assert vault.text("synced-file") not in text
    started = [event for event in held if event["event_type"] == "connector.sync_started"]
    assert len(started) == 2 and all(event["payload_json"]["previous_cursor"] is None for event in started)
    # The cursor its own public import set is shown to it, so the feed is held back only where it must be.
    assert f"5:{own}" in _cursors(held)
    assert [event["payload_json"]["sync_cursor"] for event in held if event["event_type"] == "connector.sync_completed"] == [f"5:{own}"]
    # The unbound admin key still reads the previous cursor in the feed.
    _text, seen = _connector_events(vault, admin)
    assert hidden_path in [
        event["payload_json"]["previous_cursor"] for event in seen if event["event_type"] == "connector.sync_started"
    ]


def test_the_workspace_event_feed_after_later_syncs_shows_a_key_no_cursor_it_may_not_read(label_harness):
    vault = Vault(label_harness, "ef").build()
    hidden_path = f"/vault/{vault.text('synced-file')}.md"
    # The scheduler runs of the vault come after the first import, and the last sync of the vault is the newest event.
    text, held = _connector_events(vault, vault.keys["trusted"])
    assert {event["event_type"] for event in held} >= {"connector.sync_started", "connector.state_updated", "connector.sync_completed"}
    assert _cursors(held) == [] and vault.text("synced-file") not in text
    # The key's own sync adds events that name the confidential file as the previous cursor. They name it to no one but the admin.
    status, answer = _sync(vault, vault.keys["trusted"], [{"path": "/zz/b.md", "title": "b", "text": "c", "mtime_ns": 5}])
    assert status == 201 and json.loads(answer)["previous_cursor"] is None
    text, held = _connector_events(vault, vault.keys["trusted"])
    assert vault.text("synced-file") not in text and f"5:/zz/b.md" in _cursors(held)
    _text, seen = _connector_events(vault, vault.keys["admin"])
    assert hidden_path in _cursors(seen)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the event guard judges events about a source, memory, open loop, artifact, project or belief, and not about a "
        "queued task, a person or the charter, so their creation events keep the id of a row above the ceiling in the "
        "workspace feed (ids and field names only, no text); the security note lists it, and the day those events are "
        "judged on the label of their target this test fails and the entry is deleted"
    ),
)
def test_the_workspace_event_feed_names_no_task_person_or_charter_above_the_ceiling(label_harness):
    vault = Vault(label_harness, "ids").build()
    hidden = [vault.ids["task_hidden"], vault.ids["person_hidden"], *vault.hidden_ids["brain_charters"]]
    text, _events = _connector_events(vault, vault.keys["trusted"])
    assert [row_id for row_id in hidden if row_id in text] == []


def test_the_context_tree_reads_a_filter_as_a_selection_and_never_as_a_grant(label_harness):
    vault = Vault(label_harness, "x").build()
    path = "/v0/vnext/context-tree"
    trusted = vault.keys["trusted"]
    default = json.loads(_get(vault, path, trusted)[1])
    asked = json.loads(_get(vault, path, trusted, sensitivity_allowed=["confidential", "highly_sensitive"])[1])
    everything = json.loads(
        _get(vault, path, trusted, sensitivity_allowed=["public", "internal", "private", "confidential", "unknown"])[1]
    )

    def labels(tree):
        # The events root lists the newest events, which the calls above made.
        return sorted(
            child["label"]
            for root in tree["roots"]
            if root["id"] != "root:events"
            for child in root.get("children", [])
        )

    # A level above the ceiling is read at the key's own levels, so asking for more changes nothing.
    assert labels(asked) == labels(default) == labels(everything)
    admin_all = json.loads(_get(vault, path, vault.keys["admin"], sensitivity_allowed=["confidential"])[1])
    assert vault.text("project_hidden-name") in " ".join(labels(admin_all))


def test_open_loop_extraction_reads_no_source_above_the_ceiling(label_harness):
    vault = Vault(label_harness, "e").build()
    path = "/v0/vnext/open-loops/extract"
    everything = {"scope": {}, "options": {"sensitivity_allowed": ["public", "confidential", "unknown"], "max_items": 50}}
    before = vault.hidden_rows()
    status, text = run_call(vault, "POST", path, Call("ad hoc", body=everything), vault.keys["trusted"])
    body = json.loads(text)
    assert status == 201 and _hidden_in(vault, text) == []
    titles = [loop["title"] for loop in body["open_loops"]]
    assert vault.text("source_shown-todo") in titles
    # No loop was made from a source the key may not read, and no hidden row changed.
    with label_harness.store() as store, store.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM open_loops WHERE source_id = ANY(%s::uuid[]) AND title LIKE %s",
            (vault.hidden_ids["sources"], "SENTINEL-HIDDEN-e-%todo"),
        )
        assert cur.fetchone()["n"] == 0
    assert vault.hidden_rows() == before
    status, text = run_call(vault, "POST", path, Call("ad hoc", body=everything), vault.keys["admin"])
    assert vault.text("source_hidden-todo") in text


# -- the legacy /v0 reads that stay mounted -------------------------------------------------------------------------


def _legacy_v0_get_routes() -> list[str]:
    from tests.unit.test_operator_route_inventory import _app_routes

    return sorted(
        path for method, path in _app_routes() if method == "GET" and path.startswith("/v0/") and not path.startswith("/v0/vnext")
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the legacy /v0 routes that stay mounted take no agent key and list rows whatever their label while APP_ENV is "
        "development or test; whether to close them there is a product decision, and the security note lists it"
    ),
)
def test_the_retained_legacy_v0_reads_show_a_key_nothing_above_its_ceiling(label_harness, monkeypatch):
    import re

    from alicebot_api.config import Settings
    from alicebot_api.routers import continuity, legacy_gated, memories_legacy, providers
    from tests.integration.operator_route_runner import invoke

    settings = Settings(database_url=label_harness.urls["app"])
    for module in (continuity, legacy_gated, memories_legacy, providers):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    vault = Vault(label_harness, "l").build()
    pool = [vault.ids[name] for name in ("memory_hidden", "commit_hidden", "loop_hidden", "source_hidden", "artifact_hidden")]
    leaked = []
    for template in _legacy_v0_get_routes():
        wants_id = bool(re.search(r"{\w+}", template))
        for row_id in pool if wants_id else [None]:
            path = re.sub(r"{\w+}", row_id, template) if wants_id else template
            try:
                _status, text = invoke("GET", path, user_id=label_harness.user_id, key=vault.keys["trusted"])
            except Exception:
                continue
            leaked += [(template, name) for name in _hidden_in(vault, text)]
    assert leaked == []
