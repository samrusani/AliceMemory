"""A report made from a memory that is redacted afterwards is unverified until it is regenerated without it.

Redacting a memory replaces its text, but a report, a card or a project state made from it before keeps the words it copied.
This is temporary access containment, not removal: a row that recorded a redacted row as an input is read as an unverified row
is read, only by the owner and an unbound admin key, and so is every row built from it. The text stays in those rows, and the
owner and an unbound admin key still read it. Full removal of redacted text from reports is planned for v0.21.0.

The three owner tests are the first three. Each runs through the mounted application on a vault where every producer ran with
an admin key over a project with a memory that holds a sentinel string, and the sentinel is in the reports made from it.

Mutations that these tests must fail (the manifest replays each one):

* the kernel stops reading a redacted input;
* the per-origin guard settles a row from a redacted parent;
* the reduced rank proof stops counting a row with a redacted input at the top rank;
* a producer selects a redacted memory again.
"""
from __future__ import annotations

import json

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.vnext_label_guard import LabelGuard, label_read_scope
from alicebot_api.vnext_label_repair import REDACTED_INPUT_ADVICE, label_gap_report
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.redaction_containment_support import (
    RESTRICTED,
    World,
    contained_ids,
    generate,
    keyless_owner,
    leaks,
    own_ids,
    owner_artifact,
    stored_rows,
    sweep,
)

# The one door that returns the words of a redacted memory to a restricted profile: the edges of the graph have no labels, and a
# connection edge keeps the explanation it was made with. It is a known limit that predates this change, named in the security
# note and the known limitations page, and it is reached only by the owner and an unbound admin or trusted key.
GRAPH_DOOR = "graph neighborhood"
CLEAR = ("open_loop_review", "staleness")
BUILT_FROM_THE_MEMORY = ("daily", "weekly", "connections", "contradictions", "project_update", "consolidation")


@pytest.fixture
def world(label_harness):
    return World(label_harness)


def _status(h, key, artifact_id):
    return h.request("GET", f"/v0/vnext/artifacts/{artifact_id}", key=key)[0]


def _rollup_cards(h) -> list[str]:
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id::text AS id FROM memories WHERE memory_key LIKE 'vnext.rollup.%' ORDER BY created_at")
        return [row["id"] for row in cur.fetchall()]


def test_a_row_built_from_a_redacted_memory_is_unverified_through_every_door_including_chains_of_reports(world, label_harness, monkeypatch):
    """The rows that recorded the redacted memory, and the rows that read those, are read by the owner and an unbound admin key only."""
    h = label_harness
    keys = world.keys()
    stored = stored_rows(h)
    contained = contained_ids(h)
    redacted = set(world.redacted_ids)
    assert all(stored[row_id].redacted for row_id in redacted)

    direct = [world.reports[name]["id"] for name in BUILT_FROM_THE_MEMORY] + _rollup_cards(h)
    chain_reports = [world.chain["two"], world.chain["three"]]
    chain_memories = [world.chain["promoted"]]
    clear = [world.reports[name]["id"] for name in CLEAR]
    # The facts the test stands on come from the stored rows. Each direct row recorded a redacted row; each chain row recorded
    # none and only read a report that did; the clear reports recorded neither.
    for row_id in direct:
        assert stored[row_id].inputs & redacted, row_id
    for row_id in [*chain_reports, *chain_memories]:
        assert not stored[row_id].inputs & redacted, row_id
        assert stored[row_id].inputs & contained, row_id
    assert stored[world.chain["three"]].inputs == {world.chain["two"]}  # a report of a report of a report
    for row_id in clear:
        assert not stored[row_id].inputs & (redacted | contained), row_id
    assert {*direct, *chain_reports, *chain_memories} <= contained and not contained & set(clear)
    # The project holds the state the accepted update copied, so it recorded the update and is contained with it.
    assert world.alpha in contained and stored[world.alpha].inputs & set(direct)

    # The rows with their text, for the two readers that are not limited. A key with no limit finds the sentinel in them.
    owner_sees = {}
    with keyless_owner(h):
        for row_id in [*direct, *chain_reports]:
            if row_id in {item["id"] for item in world.reports.values()} | set(chain_reports):
                status, body = owner_artifact(h, row_id)
                assert status == 200, row_id
                owner_sees[row_id] = world.sentinel in json.dumps(body)
    assert any(owner_sees.values())  # the owner still reads the words: this is containment, not removal
    for row_id in [*direct, *chain_reports]:
        if row_id in owner_sees:
            assert _status(h, keys["admin"], row_id) == 200, row_id
    for card in _rollup_cards(h):
        assert h.request("GET", f"/v0/vnext/memories/{card}/audit", key=keys["admin"])[0] == 200

    for name in RESTRICTED:
        key = keys[name]
        # Doors that name one row: the artifact get and trace routes, the memory audit, and explain and review detail.
        for row_id in [*direct, *chain_reports]:
            if row_id in owner_sees:
                assert _status(h, key, row_id) != 200, (name, "artifact get", row_id)
                assert h.request("GET", f"/v0/vnext/traces/artifacts/{row_id}", key=key)[0] != 200, (name, "artifact trace", row_id)
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
        for memory_id in [*_rollup_cards(h), *chain_memories]:
            assert h.request("GET", f"/v0/vnext/memories/{memory_id}/audit", key=key)[0] != 200, (name, "memory audit", memory_id)
            for tool, arguments in (("alice_explain", {"memory_id": memory_id}), ("alice_memory_review", {"review_item_id": memory_id})):
                with pytest.raises(MCPToolError):
                    call_mcp_tool(context, name=tool, arguments=arguments)
        # Doors that list: no contained row stands as the id of an object in any answer the profile gets.
        responses = sweep(h, world, monkeypatch, {name: key}, only=(name,))
        monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        for item in responses:
            if item.door == GRAPH_DOOR:
                continue
            shown = own_ids(item.body) & contained
            assert not shown, (name, item.door, sorted(shown))
        # The profile still reads a report that recorded nothing redacted, wherever the door admits the profile at all.
        if name in {"trusted", "trusted_bound", "alpha_only", "admin_bound"}:
            assert _status(h, key, clear[0]) == 200, (name, "the clear report is readable")


@pytest.mark.parametrize("profile", RESTRICTED)
def test_no_restricted_profile_recovers_the_text_of_a_redacted_memory_through_any_door(world, label_harness, monkeypatch, profile):
    """A sentinel that stood only in the redacted memory is in no body a restricted key gets, and the counts follow what it can read."""
    h = label_harness
    keys = world.keys()
    held = world.stored_text()
    assert held, "the reports must hold the sentinel for this test to prove anything"
    assert f"projects:{world.alpha}" in held, "the project must hold the redacted words in its state"
    responses = sweep(h, world, monkeypatch, {profile: keys[profile]}, only=(profile,))
    assert len(responses) > 100
    # The profile's answers are real answers: it is admitted at some doors and refused at others, as its profile says.
    assert any(item.status in (200, "tool-ok") for item in responses)
    found = leaks(responses, world.sentinel)
    # The one pinned exception: the graph door, for the one restricted profile that reaches it.
    allowed = {(profile, GRAPH_DOOR, 200)} if profile == "trusted" else set()
    assert set(found) == allowed, found
    # What the key sees of the whole vault is smaller than what the owner sees, by exactly the contained rows.
    contained = contained_ids(h)
    stored = stored_rows(h)
    contained_artifacts = [row_id for row_id in contained if stored[row_id].kind == "artifact"]
    total_artifacts = sum(1 for row in stored.values() if row.kind == "artifact")
    assert len(contained_artifacts) >= 8
    admin = h.request("GET", "/v0/vnext/workspace", key=keys["admin"])[1]
    assert admin["summary"]["artifact_count"] == total_artifacts
    status, workspace, _ = h.request("GET", "/v0/vnext/workspace", key=keys[profile])
    status_dogfooding, dogfooding, _ = h.request("GET", "/v0/vnext/dogfooding", key=keys[profile])
    if profile == "trusted":
        expected = total_artifacts - len(contained_artifacts)
        assert status == 200 and status_dogfooding == 200
        assert workspace["summary"]["artifact_count"] == expected
        assert workspace["samples"]["artifacts"]["total_count"] == expected
        assert len(workspace["artifacts"]) == expected
        assert dogfooding["generated_artifacts_created"] == expected
        assert sum(dogfooding["artifact_status_counts"].values()) == expected
        # The memories it counts are those it can read: no card, promoted copy or candidate that read the memory.
        readable_memories = [
            row for row in stored.values()
            if row.kind == "memory" and not row.deleted and row.id not in contained
        ]
        by_status: dict[str, int] = {}
        for row in readable_memories:
            by_status[row.status] = by_status.get(row.status, 0) + 1
        assert workspace["summary"]["memory_status_counts"] == by_status
        assert dogfooding["memory_status_counts"] == by_status
        assert workspace["summary"]["event_count"] < admin["summary"]["event_count"]
    else:
        assert status == 403 and status_dogfooding in (200, 403)


def stored_text_of(h, artifact_id) -> str:
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT row_to_json(t)::text AS body FROM generated_artifacts t WHERE id = %s", (artifact_id,))
        return cur.fetchone()["body"]


def _readable_by(h, keys, artifact_id) -> dict[str, bool]:
    return {name: _status(h, key, artifact_id) == 200 for name, key in keys.items()}


def test_regenerating_a_report_restores_access_only_when_the_redacted_text_is_no_longer_included(world, label_harness, monkeypatch):
    """A new run of each producer is readable where the control report is, holds no sentinel, and leaves the old report restricted."""
    h = label_harness
    keys = world.keys()
    restricted_keys = {name: keys[name] for name in RESTRICTED}
    control = _readable_by(h, restricted_keys, world.reports["open_loop_review"]["id"])
    assert control["trusted"] and not any(_readable_by(h, restricted_keys, world.reports["daily"]["id"]).values())
    old = {name: report["id"] for name, report in world.reports.items()}
    # The dashboard of the project is hidden from a limited key while its state was copied from a report that read the memory.
    assert h.request("GET", f"/v0/vnext/projects/{world.alpha}/dashboard", key=keys["trusted"])[0] == 404
    redacted = set(world.redacted_ids)
    fresh = {}
    for producer in ("daily", "weekly", "connections", "contradictions", "project_update", "consolidation"):
        fresh[producer] = generate(h, producer, world.alpha, world.admin)
        assert fresh[producer]["id"] != old[producer], producer
    rows = stored_rows(h)
    contained = contained_ids(h)
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id::text AS id, row_to_json(t)::text AS body FROM generated_artifacts t")
        bodies = {row["id"]: row["body"] for row in cur.fetchall()}
    for producer, report in fresh.items():
        row_id = report["id"]
        # No producer selects the redacted memory again, and nothing it printed holds the words.
        assert not rows[row_id].inputs & redacted, producer
        assert world.sentinel not in bodies[row_id], producer
        assert row_id not in contained, producer
        # It is readable by the profiles that read the control report, and by nobody else.
        assert _readable_by(h, restricted_keys, row_id) == control, producer
        # The old report keeps its words for the two readers that are not limited, and no limited key reads it.
        assert not any(_readable_by(h, restricted_keys, old[producer]).values()), producer
        assert _status(h, keys["admin"], old[producer]) == 200
        with keyless_owner(h):
            assert owner_artifact(h, old[producer])[0] == 200
    assert world.sentinel in bodies[old["weekly"]] and world.sentinel in bodies[old["project_update"]]
    # Archiving the old report keeps its row and its words, so it stays restricted. Nothing deletes it but the database.
    status, body, _ = h.request("POST", f"/v0/vnext/artifacts/{old['connections']}/review", payload={"action": "archive"}, key=world.admin)
    assert status == 200, body
    assert not any(_readable_by(h, restricted_keys, old["connections"]).values())
    assert world.sentinel in stored_text_of(h, old["connections"])
    # The old cards stay restricted. The new roll-up card reads no redacted member and is read like any other memory.
    cards = _rollup_cards(h)
    old_cards = [card for card in cards if rows[card].inputs & redacted]
    new_cards = [card for card in cards if card not in old_cards]
    assert old_cards and new_cards
    for card in old_cards:
        assert h.request("GET", f"/v0/vnext/memories/{card}/audit", key=keys["trusted"])[0] != 200
        assert h.request("GET", f"/v0/vnext/memories/{card}/audit", key=keys["admin"])[0] == 200
    for card in new_cards:
        assert not rows[card].inputs & redacted
        assert card not in contained
        assert h.request("GET", f"/v0/vnext/memories/{card}/audit", key=keys["trusted"])[0] == 200
    # A new project update, accepted, replaces the state the project holds, and the project is read again.
    status, body, _ = h.request(
        "POST", f"/v0/vnext/projects/update-candidates/{fresh['project_update']['id']}/review", payload={"action": "accept"}, key=world.admin
    )
    assert status == 200, body
    assert h.request("GET", f"/v0/vnext/projects/{world.alpha}/dashboard", key=keys["trusted"])[0] == 200
    # Nothing a restricted profile gets from any door holds the sentinel, now that the reports it can read are all new.
    for name in ("trusted", "read_only", "alpha_only"):
        responses = sweep(h, world, monkeypatch, {name: keys[name]}, only=(name,))
        found = {(who, door) for who, door, _ in leaks(responses, world.sentinel)}
        assert found <= ({(name, GRAPH_DOOR)} if name == "trusted" else set()), found


ALL_SENSITIVITIES = ["public", "internal", "private", "unknown", "confidential", "highly_sensitive", "sacred", "regulated"]


def test_a_producer_reads_no_restricted_report_unless_it_is_asked_to_read_every_sensitivity_of_every_project(world, label_harness):
    """A producer filters its inputs by their effective labels, so a restricted report is not an input of the next report.

    The weekly report is regenerated alone, with the old brief and the other old reports in its window: it reads none of them
    and is readable. When the run is asked to read every sensitivity of every project, as only the owner and an unbound admin
    key may ask, it reads them, and the new report is restricted because of it, whatever it printed.
    """
    h = label_harness
    keys = world.keys()
    restricted_keys = {name: keys[name] for name in RESTRICTED}
    contained = contained_ids(h)
    weekly = generate(h, "weekly", world.alpha, world.admin)
    rows = stored_rows(h)
    assert not rows[weekly["id"]].inputs & (contained | set(world.redacted_ids))
    assert weekly["id"] not in contained_ids(h)
    assert _readable_by(h, restricted_keys, weekly["id"])["trusted"]
    forced = generate(h, "weekly", world.alpha, world.admin, options={"sensitivity_allowed": ALL_SENSITIVITIES}, projects=False)
    rows = stored_rows(h)
    assert forced["id"] != weekly["id"]
    assert rows[forced["id"]].inputs & contained, "the run was asked to read the restricted reports"
    assert forced["id"] in contained_ids(h)
    assert not any(_readable_by(h, restricted_keys, forced["id"]).values())
    assert _status(h, keys["admin"], forced["id"]) == 200


def test_archiving_a_memory_does_not_restrict_the_reports_built_from_it(label_harness, monkeypatch):
    """Archived memories keep the historical content of their reports. Only redaction restricts."""
    h = label_harness
    world = World(h, redact=False)
    keys = world.keys()
    before = {name: _status(h, keys["trusted"], report["id"]) for name, report in world.reports.items()}
    assert all(status == 200 for status in before.values())
    with h.store() as store:
        store.update_memory(memory_id=world.redacted, patch={"status": "archived"}, actor_type="system")
    status, body, _ = h.request("POST", "/v0/vnext/memories/forget", payload={"memory_id": str(world.contra["id"]), "reason": "r"}, key=world.admin)
    assert status == 200, body
    # A source is archived too, which the reports that listed it record as an input.
    with h.store() as store:
        store.delete_source(source_id=str(world.sources[1]["id"]), actor_type="user")
    assert contained_ids(h) == set()
    after = {name: _status(h, keys["trusted"], report["id"]) for name, report in world.reports.items()}
    assert after == before
    status, body, _ = h.request("GET", f"/v0/vnext/artifacts/{world.reports['weekly']['id']}", key=keys["trusted"])
    assert status == 200 and world.sentinel in json.dumps(body)
    assert not label_gap_report_unverified(h)


def label_gap_report_unverified(h) -> int:
    with h.store() as store:
        return label_gap_report(store).unverified


def test_redaction_clears_a_cached_label_inside_one_request_scope(label_harness):
    """A request reads a report's label, the memory is redacted in the same scope, and the next read sees the report unverified."""
    h = label_harness
    world = World(h, redact=False)
    report_id = world.reports["weekly"]["id"]
    with h.store() as store:
        report = store.get_artifact(report_id)
        with label_read_scope(store):
            guard = LabelGuard(store, active=True, sensitivity_allowed=("public", "internal", "unknown", "private"))
            assert guard.effective_row("artifact", report)["unverified"] is False
            assert [row["id"] for row in guard.admit_rows("artifact", [report])] == [report["id"]]
            assert guard.readable_status_counts("artifact")
            # Redact inside the scope, through the store method the redact route calls.
            store.lock_label_writes()
            store.redact_memory_bundle(memory_id=world.redacted, project_update_artifacts=[], actor_type="user")
            assert guard.effective_row("artifact", report)["unverified"] is True
            assert guard.admit_rows("artifact", [report]) == []
            total = sum(guard.readable_status_counts("artifact").values())
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM generated_artifacts")
        everything = cur.fetchone()["n"]
    assert 0 < total < everything


def test_the_doctor_counts_the_rows_as_unverified_and_says_to_regenerate_or_delete(world, label_harness):
    h = label_harness
    keys = world.keys()
    with h.store() as store:
        below, unverified, redacted_inputs = label_gap_report(store)
    contained = contained_ids(h)
    stored = stored_rows(h)
    assert below == 0
    assert unverified == len(contained)
    direct = {row_id for row_id in contained if stored[row_id].inputs & set(world.redacted_ids)}
    assert redacted_inputs == len(direct) > 0
    status, body, _ = h.request("GET", "/v0/vnext/doctor", key=keys["admin"])
    assert status == 200, body
    check = next(item for item in body["checks"] if item["name"] == "derived_labels")
    assert check["status"] != "ok"
    assert f"{unverified} unverified" in check["message"] and f"{redacted_inputs} built from a redacted memory" in check["message"]
    assert REDACTED_INPUT_ADVICE in check["message"]
    assert "regenerate or delete" in check["message"].lower() and "cannot fix" in check["message"]
    # The fix the check recommends is not the repair that cannot clear these rows.
    assert check["recommended_fix"] == "Regenerate or delete the reports built from a redacted memory."
    # A key with limits gets no content check, and so no count of rows it may not read.
    status, limited, _ = h.request("GET", "/v0/vnext/doctor", key=keys["trusted"])
    skipped = next(item for item in limited["checks"] if item["name"] == "derived_labels")
    assert skipped["status"] == "skipped" and "unverified" not in skipped["message"]


def test_labels_check_names_the_reason_and_labels_repair_leaves_the_rows_as_they_are(world, label_harness):
    from types import SimpleNamespace

    from alicebot_api.cli import labels

    h = label_harness
    context = SimpleNamespace(database_url=h.urls["app"], user_id=h.user_id)
    with pytest.raises(SystemExit) as raised:
        labels._run_vnext_labels_check(context, None)
    assert raised.value.code == 1
    with h.store() as store:
        before = label_gap_report(store)
    assert before.redacted_inputs > 0
    assert labels._run_vnext_labels_repair(context, None) == "labels repair updated 0"
    with h.store() as store:
        assert label_gap_report(store) == before


def test_the_text_of_a_redacted_memory_is_kept_in_the_reports_for_the_owner(world, label_harness):
    """Containment is not removal: the words are in the stored rows, and the owner reads them."""
    stored = world.stored_text()
    kinds = sorted(name.split(":")[0] for name in stored)
    # Which reports print the memory depends on the order of the vault's rows, so the count is a floor and not a number.
    assert kinds.count("generated_artifacts") >= 2 and kinds.count("memories") >= 1 and kinds.count("projects") == 1, stored.keys()
    with keyless_owner(label_harness):
        status, body = owner_artifact(label_harness, world.reports["weekly"]["id"])
    assert status == 200 and world.sentinel in json.dumps(body)


def test_a_restricted_key_cannot_review_a_project_update_that_read_the_redacted_memory(label_harness):
    h = label_harness
    world = World(h, accept_update=False)
    keys = world.keys()
    update = world.reports["project_update"]["id"]
    for name in RESTRICTED:
        status, body, _ = h.request("POST", f"/v0/vnext/projects/update-candidates/{update}/review", payload={"action": "reject"}, key=keys[name])
        assert status != 200, name
        assert world.sentinel not in json.dumps(body), name
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT status FROM generated_artifacts WHERE id = %s", (update,))
        assert cur.fetchone()["status"] == "needs_review"


def test_the_artifact_list_a_key_gets_shows_only_the_reports_it_can_read(world, label_harness):
    h = label_harness
    keys = world.keys()
    contained = contained_ids(h)
    status, body, _ = h.request("GET", "/v0/vnext/artifacts", key=keys["trusted"])
    assert status == 200
    shown = own_ids(body)
    assert shown & {world.reports[name]["id"] for name in CLEAR} == {world.reports[name]["id"] for name in CLEAR}
    assert not shown & contained
    status, everything, _ = h.request("GET", "/v0/vnext/artifacts", key=keys["admin"])
    assert {row_id for row_id in contained if stored_rows(h)[row_id].kind == "artifact"} <= own_ids(everything)


def test_redacting_the_candidate_memory_of_an_accepted_project_update_restricts_the_project(label_harness):
    """The verb that redacts a project update itself: the memory and the update artifact become skeletons, and the project
    that copied the update's state recorded both, so it is read by the owner and an unbound admin key only."""
    h = label_harness
    world = World(h, redact=False)
    keys = world.keys()
    candidate = world.reports["project_update"]["metadata_json"]["candidate_memory_id"]
    assert h.request("GET", f"/v0/vnext/projects/{world.alpha}/dashboard", key=keys["trusted"])[0] == 200
    status, body, _ = h.request("POST", "/v0/vnext/memories/redact", payload={"memory_id": candidate, "reason": "r"}, key=world.admin)
    assert status == 200, body
    stored = stored_rows(h)
    assert stored[candidate].redacted and stored[world.reports["project_update"]["id"]].redacted
    assert world.alpha in contained_ids(h)
    assert h.request("GET", f"/v0/vnext/projects/{world.alpha}/dashboard", key=keys["trusted"])[0] == 404
    assert h.request("GET", f"/v0/vnext/projects/{world.alpha}/dashboard", key=keys["admin"])[0] == 200
    listed = h.request("GET", "/v0/vnext/projects", key=keys["trusted"])[1]
    assert world.alpha not in own_ids(listed)
    assert world.alpha in own_ids(h.request("GET", "/v0/vnext/projects", key=keys["admin"])[1])


def test_a_belief_whose_backing_memory_is_redacted_is_read_by_the_owner_and_an_unbound_admin_key_only(label_harness):
    """A belief keeps the claim it copied from its memory, so redacting the memory leaves the words in the belief."""
    h = label_harness
    world = World(h, redact=False)
    keys = world.keys()
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id::text AS id, memory_id::text AS memory_id, claim FROM beliefs")
        belief = cur.fetchone()
    claim = belief["claim"]
    for name in ("admin", "trusted"):
        status, body, _ = h.request("GET", f"/v0/vnext/beliefs/{belief['id']}/state", key=keys[name])
        assert status == 200 and claim in json.dumps(body), name
    status, body, _ = h.request("POST", "/v0/vnext/memories/redact", payload={"memory_id": belief["memory_id"], "reason": "r"}, key=world.admin)
    assert status == 200, body
    status, body, _ = h.request("GET", f"/v0/vnext/beliefs/{belief['id']}/state", key=keys["admin"])
    assert status == 200 and claim in json.dumps(body), "containment is not removal: the claim stays, and the admin key reads it"
    status, body, _ = h.request("GET", f"/v0/vnext/beliefs/{belief['id']}/state", key=keys["trusted"])
    assert status == 404 and claim not in json.dumps(body)
    trusted_workspace = h.request("GET", "/v0/vnext/workspace", key=keys["trusted"])[1]
    assert belief["id"] not in own_ids(trusted_workspace) and claim not in json.dumps(trusted_workspace)


def test_a_restricted_key_cannot_export_rate_or_review_a_report_that_read_the_redacted_memory(label_harness, tmp_path):
    """The write doors that return a report's text, or change it, refuse a key with limits and change nothing."""
    h = label_harness
    world = World(h)
    keys = world.keys()
    report = world.reports["weekly"]["id"]

    def counts():
        with h.store() as store, store.conn.cursor() as cur:
            cur.execute("SELECT (SELECT count(*) FROM artifact_quality_ratings) AS ratings, (SELECT count(*) FROM event_log) AS events, "
                        "(SELECT status FROM generated_artifacts WHERE id = %s) AS status", (report,))
            row = cur.fetchone()
        return row["ratings"], row["status"]

    before = counts()
    for name in RESTRICTED:
        key = keys[name]
        for path, payload in (
            (f"/v0/vnext/artifacts/{report}/export", {"output_dir": str(tmp_path / name)}),
            (f"/v0/vnext/artifacts/{report}/review", {"action": "reject"}),
            (f"/v0/vnext/artifacts/{report}/quality-ratings", {"usefulness": 3, "accuracy": 3, "reviewer_id": "reader"}),
            (f"/v0/vnext/artifacts/{report}/insight-feedback", {"useful_insight": "yes", "comments": "ok"}),
        ):
            status, body, _ = h.request("POST", path, payload=payload, key=key)
            assert status not in (200, 201), (name, path, status)
            assert world.sentinel not in json.dumps(body), (name, path)
        assert not list(tmp_path.glob(f"{name}/*")), name
    assert counts() == before
    # The unbound admin key is not limited: it exports the report, and the file holds the words it kept.
    status, body, _ = h.request("POST", f"/v0/vnext/artifacts/{report}/export", payload={"output_dir": str(tmp_path / "admin")}, key=keys["admin"])
    assert status == 200, body
    assert any(world.sentinel in path.read_text() for path in (tmp_path / "admin").rglob("*") if path.is_file())


def test_a_staleness_report_that_listed_a_memory_is_contained_when_that_memory_is_redacted(label_harness):
    """The staleness report prints the titles of the memories it marked, and records their ids as ``stale_marked_memory_ids``."""
    h = label_harness
    world = World(h, redact=False)
    keys = world.keys()
    report = world.reports["staleness"]
    marked = report["metadata_json"]["stale_marked_memory_ids"]
    assert marked and "OLD" in report["content_markdown"]
    assert h.request("GET", f"/v0/vnext/artifacts/{report['id']}", key=keys["trusted"])[0] == 200
    status, body, _ = h.request("POST", "/v0/vnext/memories/redact", payload={"memory_id": marked[0], "reason": "r"}, key=world.admin)
    assert status == 200, body
    assert report["id"] in contained_ids(h)
    for name in RESTRICTED:
        assert _status(h, keys[name], report["id"]) != 200, name
    assert _status(h, keys["admin"], report["id"]) == 200
    assert report["id"] not in own_ids(h.request("GET", "/v0/vnext/artifacts", key=keys["trusted"])[1])
