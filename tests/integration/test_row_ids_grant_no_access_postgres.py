"""Knowing the id of a row never gives a key access to it.

A readable report or memory can list, in its own metadata, the id of a row the same key may not read (a candidate that
was relabelled after the report was made, a source archived since). So the question for every door that takes an id is
what a key gets when it names such a row. Every case below builds a vault in which rows of each kind are hidden for each
reason, gives a real key of each profile, and calls every door with every id that profile may not read.

For each call:

* the call adds and removes exactly the rows a missing id does, in every stored table and with generated ids and instants
  replaced. The event log and the agent records are tables of their own here, because a key reads them back in its own
  telemetry. For a door that acts on a row that is nothing, and for a door that only cites a row it is the new row;
* nothing of the row (its title, text, summary, raw text, chunk or claim) comes back;
* the answer is the one a missing id gets: the same status and the same body, or for a tool the same error. A door
  that names the row to act on does not answer a row above the caller's limits with the policy refusal (HTTP 403, or
  the tool's not-permitted error), because that refusal is built from the labels of the row and would repeat them.

A search door takes the id as the text of a query. A query is read for dates, and a random id that holds a year asks for
that year's memories, so two ids can honestly get different results. A search door is held to the first two rules only.

A positive control runs the same doors on rows the profile may read, so the matrix cannot pass because every door
refuses everything.
"""
from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.hidden_ids_postgres_support import (
    ALL_DOORS,
    HIDDEN_FOR,
    PROFILES,
    READ_DOORS,
    REFERENCE_DOORS,
    WRITE_DOORS,
    Env,
    build_vault,
    changes,
    normalize,
    snapshot,
)

# Four groups keep each case well inside the per-test time limit of the integration job.
DOOR_GROUPS = {
    "reads": READ_DOORS,
    "http_writes": tuple(door for door in WRITE_DOORS if not door.name.startswith("tool")),
    "tool_writes": tuple(door for door in WRITE_DOORS if door.name.startswith("tool")),
    "references": REFERENCE_DOORS,
}


def _key(h, vault, profile):
    permission, bound = PROFILES[profile]
    return h.key(permission, project=vault.alpha if bound else None)


@pytest.mark.parametrize("group", list(DOOR_GROUPS))
@pytest.mark.parametrize("profile", list(PROFILES))
def test_a_hidden_id_gives_no_access_through_any_door(label_harness, monkeypatch, tmp_path, profile, group):
    h = label_harness
    vault = build_vault(h)
    key = _key(h, vault, profile)
    env = Env(h, monkeypatch, h.urls["app"], str(tmp_path), vault=vault)
    hidden = vault.hidden_ids(profile)
    assert hidden, profile
    assert (("project", "beta") in hidden) == PROFILES[profile][1]  # a project the binding leaves out
    failures: list[str] = []
    calls = expected = 0
    for door in DOOR_GROUPS[group]:
        before = snapshot(h)
        missing = door.call(env, key, str(uuid4()))
        missing_changed = changes(before, snapshot(h))
        for (kind, reason), row_id in hidden.items():
            if not door.takes(kind):
                continue
            expected += 1
            before = snapshot(h)
            got = door.call(env, key, row_id)
            calls += 1
            label = f"{profile} / {door.name} / {kind} hidden as {reason}"
            changed = changes(before, snapshot(h))
            if changed != missing_changed:
                failures.append(
                    f"{label}: changed {json.dumps(changed)[:300]} where a missing id changes {json.dumps(missing_changed)[:300]}"
                )
            leaked = [word for word in vault.secrets.get((kind, reason), []) if word in got.body]
            if leaked:
                failures.append(f"{label}: answered with {leaked}")
            if not door.search and got != missing:
                failures.append(f"{label}: {got} differs from a missing id: {missing}")
    assert calls == expected, (calls, expected)
    assert not failures, "\n".join(failures[:25])


def _counts(value, path=""):
    """Every number in a telemetry summary, keyed by where it stands and what it counts."""
    if isinstance(value, bool):
        return {}
    if isinstance(value, int):
        return {path: value}
    result = {}
    if isinstance(value, dict):
        for key, child in value.items():
            result.update(_counts(child, f"{path}/{key}"))
    elif isinstance(value, list):
        for child in value:
            label = json.dumps({k: v for k, v in child.items() if k != "count"}, sort_keys=True) if isinstance(child, dict) else str(child)
            result.update(_counts(child.get("count") if isinstance(child, dict) and "count" in child else child, f"{path}[{label}]"))
    return result


def _moved(before, after):
    first, second = _counts(before), _counts(after)
    return {key: second.get(key, 0) - first.get(key, 0) for key in {*first, *second} if second.get(key, 0) != first.get(key, 0)}


def test_a_call_on_a_hidden_id_moves_the_keys_telemetry_as_a_call_on_a_missing_id_does(label_harness, monkeypatch, tmp_path):
    """The policy events and the agent record are events a key reads back in its own telemetry.

    A call on a row the key may not read must write what a call on a missing id writes, or the telemetry would tell the
    key that the id names a row. The matrix compares every table; this test adds the key's own view. It runs every door
    with a missing id and then with every hidden id, and compares how far the counts of the telemetry moved. The control
    is a call on a row the key may read and is refused: it moves the counts further than a missing id does, so the
    comparison cannot hold because the telemetry sees nothing.
    """
    h = label_harness
    vault = build_vault(h)
    trusted = h.key("trusted_local_agent")
    env = Env(h, monkeypatch, h.urls["app"], str(tmp_path), vault=vault)

    def telemetry():
        status, body, _ = h.request("GET", "/v0/vnext/agents/policy-telemetry", key=trusted)
        assert status == 200, body
        return body["summary"]

    doors = [door for door in (*READ_DOORS, *WRITE_DOORS) if not door.search]
    hidden = list(vault.hidden_ids("trusted").values())
    telemetry()  # the first call of the key writes its own agent record
    t0 = telemetry()
    for door in doors:
        for _ in hidden:
            door.call(env, trusted, str(uuid4()))
    t1 = telemetry()
    for door in doors:
        for row_id in hidden:
            door.call(env, trusted, row_id)
    t2 = telemetry()
    assert _moved(t1, t2) == _moved(t0, t1)
    # The control: the same key is refused on a row it may read, and the counts move by more.
    doors_by_name = {door.name: door for door in doors}
    assert str(doors_by_name["memory review accept"].call(env, trusted, vault.ids[("memory", "visible")]).status) == "403"
    t3 = telemetry()
    assert _moved(t2, t3) != _moved(t1, t2)


CONTROLS = (
    # profile, door, row that profile may read, status the door answers
    ("trusted", "GET source", ("source", "visible"), "200"),
    ("trusted", "GET artifact", ("artifact", "visible"), "200"),
    ("trusted", "GET source trace", ("source", "visible"), "200"),
    ("trusted", "GET memory audit", ("memory", "visible"), "200"),
    ("trusted", "GET belief state", ("belief", "visible"), "200"),
    ("trusted", "belief review", ("belief", "visible"), "200"),
    ("trusted", "edge review", ("edge", "visible"), "200"),
    ("trusted", "review source", ("source", "visible"), "200"),
    ("trusted", "tool explain memory_id", ("memory", "visible"), "tool-ok"),
    ("trusted", "loop review close", ("loop", "visible"), "200"),
    ("admin", "artifact review reject", ("artifact", "confidential"), "200"),
    ("admin", "belief review", ("belief", "confidential"), "200"),
    ("admin", "edge review", ("edge", "confidential"), "200"),
    ("admin", "GET source", ("source", "confidential"), "200"),
    ("admin", "memory review accept", ("memory", "confidential"), "200"),
    ("read_only", "tool explain memory_id", ("memory", "visible"), "tool-ok"),
    ("read_only", "tool review item", ("memory", "visible"), "tool-ok"),
    ("memory_proposal", "tool explain memory_id", ("memory", "private"), "tool-ok"),
    ("trusted_bound", "tool explain memory_id", ("memory", "visible"), "tool-ok"),
    ("alpha_only", "tool explain memory_id", ("memory", "visible"), "tool-ok"),
    ("admin_bound", "memory review accept", ("memory", "confidential"), "200"),
    ("admin_bound", "GET artifact", ("artifact", "confidential"), "200"),
    # A row the profile may read keeps the refusals it had, so a refusal is not the answer for every row.
    ("read_only", "artifact review promote", ("artifact", "visible"), "403"),
    ("read_only", "loop review close", ("loop", "visible"), "403"),
    ("trusted", "memory review accept", ("memory", "visible"), "403"),
    ("trusted", "memory redact", ("memory", "visible"), "403"),
    ("trusted", "tool manage redact", ("memory", "visible"), "tool-error:MCPNotPermittedError"),
    ("trusted", "tool correct approve", ("memory", "visible"), "tool-error:MCPNotPermittedError"),
    ("read_only", "tool open loop close", ("loop", "visible"), "tool-error:MCPNotPermittedError"),
    # The reference doors file a row under a project or point it at a successor. Each works when the target is readable.
    ("trusted", "create loop in project", ("project", "beta"), "201"),
    ("trusted_bound", "create loop in project", ("project", "visible"), "201"),
    ("admin_bound", "create loop in project", ("project", "visible"), "201"),
    ("admin_bound", "assign memory to project", ("project", "visible"), "200"),
    ("alpha_only", "capture source in project", ("project", "visible"), "201"),
    ("alpha_only", "propose memory in project", ("project", "visible"), "201"),
    ("admin", "belief review superseded_by", ("belief", "confidential"), "200"),
    ("admin", "tool manage undo superseded_by", ("memory", "confidential"), "tool-ok"),
)


@pytest.mark.parametrize("profile", sorted({control[0] for control in CONTROLS}))
def test_the_same_doors_serve_a_row_the_profile_may_read(label_harness, monkeypatch, tmp_path, profile):
    h = label_harness
    vault = build_vault(h)
    key = _key(h, vault, profile)
    env = Env(h, monkeypatch, h.urls["app"], str(tmp_path), vault=vault)
    doors = {door.name: door for door in ALL_DOORS}
    for control_profile, door_name, row, status in CONTROLS:
        if control_profile != profile:
            continue
        assert profile not in HIDDEN_FOR.get(row[1], frozenset()), (profile, row)
        answer = doors[door_name].call(env, key, vault.ids[row])
        assert str(answer.status) == status, (profile, door_name, row, answer)


def test_the_belief_and_edge_doors_apply_the_callers_limits(label_harness, monkeypatch):
    """The two doors that gave a key with limits the whole row and let it change it, before the fix."""
    h = label_harness
    vault = build_vault(h)
    trusted = h.key("trusted_local_agent")
    admin = h.key("admin_agent")
    missing_belief = f"/v0/vnext/beliefs/{uuid4()}/review"
    missing_edge = f"/v0/vnext/graph/edges/{uuid4()}/review"
    gone_belief = h.request("POST", missing_belief, payload={"action": "retire"}, key=trusted)
    gone_edge = h.request("POST", missing_edge, payload={"action": "reject"}, key=trusted)
    assert gone_belief[0] == 404 and gone_belief[1] == {"detail": "vNext belief was not found"}
    assert gone_edge[0] == 404 and gone_edge[1] == {"detail": "vNext graph edge was not found"}
    for route, action, row, absent in (
        ("beliefs", "retire", ("belief", "confidential"), gone_belief),
        ("graph/edges", "reject", ("edge", "confidential"), gone_edge),
    ):
        before = snapshot(h)
        refused = h.request("POST", f"/v0/vnext/{route}/{vault.ids[row]}/review", payload={"action": action}, key=trusted)
        assert refused[:2] == absent[:2], (route, refused)
        assert snapshot(h) == before, route
        assert "CLAIM-confidential" not in json.dumps(refused[1]) and "EDGE-confidential" not in json.dumps(refused[1])
        done = h.request("POST", f"/v0/vnext/{route}/{vault.ids[row]}/review", payload={"action": action}, key=admin)
        assert done[0] == 200, (route, done)
        assert snapshot(h) != before, route
    # A belief the caller may read cannot be replaced by one it may not read.
    before = snapshot(h)
    refused = h.request(
        "POST", f"/v0/vnext/beliefs/{vault.ids[('belief', 'visible')]}/review",
        payload={"action": "supersede", "superseded_by": vault.ids[("belief", "confidential")]}, key=trusted,
    )
    assert refused[:2] == gone_belief[:2], refused
    assert snapshot(h) == before


def test_an_edge_with_an_end_the_caller_cannot_read_or_find_is_not_the_callers_to_review(label_harness):
    """Each kind of end is checked: a source, a memory and a belief by their own fence, and an end nobody can show."""
    h = label_harness
    vault = build_vault(h)
    visible_source, visible_memory = vault.ids[("source", "visible")], vault.ids[("memory", "visible")]

    def edge(from_type, from_id, to_type, to_id, note):
        with h.store() as store:
            return store.create_edge(
                {
                    "from_type": from_type, "from_id": from_id, "to_type": to_type, "to_id": to_id,
                    "edge_type": "similar_to", "confidence": 0.5, "explanation": f"EDGE-{note}",
                    "created_by": "vnext_connection_finder", "metadata_json": {"status": "candidate", "candidate": True},
                }
            )

    refused = {
        "source end above the ceiling": edge("source", vault.ids[("source", "confidential")], "memory", visible_memory, "a"),
        "memory end above the ceiling": edge("source", visible_source, "memory", vault.ids[("memory", "confidential")], "b"),
        "belief end above the ceiling": edge("source", visible_source, "belief", vault.ids[("belief", "confidential")], "c"),
        "unverified memory end": edge("source", visible_source, "memory", vault.ids[("memory", "unverified")], "d"),
        "memory end that does not exist": edge("source", visible_source, "memory", str(uuid4()), "e"),
        "source end that does not exist": edge("source", str(uuid4()), "memory", visible_memory, "f"),
        "end of a kind with no check": edge("source", visible_source, "artifact", vault.ids[("artifact", "visible")], "g"),
    }
    trusted = h.key("trusted_local_agent")
    for name, row in refused.items():
        before = snapshot(h)
        status, body, _ = h.request("POST", f"/v0/vnext/graph/edges/{row['id']}/review", payload={"action": "reject"}, key=trusted)
        assert (status, body) == (404, {"detail": "vNext graph edge was not found"}), name
        assert snapshot(h) == before, name
    # Readable ends, and an end that is an entity (which has no label), are the caller's to review.
    with h.store() as store:
        entity = store.create_entity({"name": "Atlas", "entity_type": "project"})
    served = {
        "readable ends": vault.ids[("edge", "visible")],
        "entity end": str(edge("source", visible_source, "entity", str(entity["id"]), "h")["id"]),
    }
    for name, edge_id in served.items():
        status, body, _ = h.request("POST", f"/v0/vnext/graph/edges/{edge_id}/review", payload={"action": "accept"}, key=trusted)
        assert status == 200, (name, body)


def test_an_id_that_is_not_well_formed_is_a_missing_belief_or_edge_and_a_spelling_of_a_hidden_id_is_still_hidden(label_harness):
    """No spelling of an id reaches a database cast, and none gets round the check: a malformed id and every spelling of a hidden id."""
    h = label_harness
    vault = build_vault(h)
    trusted, admin = h.key("trusted_local_agent"), h.key("admin_agent")
    hidden_belief = vault.ids[("belief", "confidential")]
    visible_belief = vault.ids[("belief", "visible")]
    hidden_edge = vault.ids[("edge", "confidential")]
    spellings = [
        "not-a-uuid", "", " ", "0" * 31, "g" * 36, hidden_belief[:-1], hidden_belief + "0",
    ]
    for caller in (trusted, admin):
        for route, payload in (("beliefs", {"action": "retire"}), ("graph/edges", {"action": "reject"})):
            absent = h.request("POST", f"/v0/vnext/{route}/{uuid4()}/review", payload=payload, key=caller)
            for spelling in spellings:
                if not spelling.strip():
                    continue  # an empty path segment is a different route
                before = snapshot(h)
                got = h.request("POST", f"/v0/vnext/{route}/{spelling}/review", payload=payload, key=caller)
                assert got[:2] == absent[:2] == (absent[0], absent[1]) and got[0] == 404, (route, spelling, got)
                assert snapshot(h) == before, (route, spelling)
    # A hidden belief named as the replacement in any spelling of its id is still a belief the caller may not read.
    variants = [hidden_belief.upper(), f"urn:uuid:{hidden_belief}", f"{{{hidden_belief}}}", f" {hidden_belief} ", hidden_belief.replace("-", "")]
    gone = h.request("POST", f"/v0/vnext/beliefs/{visible_belief}/review", payload={"action": "supersede", "superseded_by": str(uuid4())}, key=trusted)
    assert gone[0] == 404
    for variant in variants:
        before = snapshot(h)
        got = h.request("POST", f"/v0/vnext/beliefs/{visible_belief}/review", payload={"action": "supersede", "superseded_by": variant}, key=trusted)
        assert got[:2] == gone[:2], (variant, got)
        assert snapshot(h) == before, variant
    # The same spellings of the id of a hidden edge or belief, named as the row to review, are as hidden as the id itself.
    for route, row_id, payload in (("beliefs", hidden_belief, {"action": "retire"}), ("graph/edges", hidden_edge, {"action": "reject"})):
        missing = h.request("POST", f"/v0/vnext/{route}/{uuid4()}/review", payload=payload, key=trusted)
        for variant in (row_id.upper(), f"urn:uuid:{row_id}", f"{{{row_id}}}", row_id.replace("-", "")):
            before = snapshot(h)
            got = h.request("POST", f"/v0/vnext/{route}/{variant}/review", payload=payload, key=trusted)
            assert got[:2] == missing[:2], (route, variant, got)
            assert snapshot(h) == before, (route, variant)


def test_an_open_loop_project_id_outside_the_binding_or_not_well_formed_is_a_missing_project(label_harness):
    """The project a loop is filed under is held to the key's binding in every spelling of its id, and never reaches a cast."""
    h = label_harness
    vault = build_vault(h)
    bound = h.key("trusted_local_agent", project=vault.alpha)
    missing = (404, {"detail": "vNext project was not found"})
    outside = [
        str(uuid4()), "not-a-uuid", " ", "g" * 36, vault.beta, vault.beta.upper(), f" {vault.beta} ",
        f"urn:uuid:{vault.beta}", f"{{{vault.beta}}}", vault.beta.replace("-", ""),
    ]
    for project_id in outside:
        before = snapshot(h)
        status, body, _ = h.request("POST", "/v0/vnext/open-loops", payload={"title": "PLANTED", "project_id": project_id}, key=bound)
        assert (status, body) == missing, (project_id, status, body)
        # Nothing is filed. The create itself was authorized for the key's own scope before the project was judged, so the
        # agent record and the policy events of that check are all that is written, as for a project that does not exist.
        written = changes(before, snapshot(h))
        assert "open_loops" not in written, (project_id, written)
        assert set(written) <= {"event_log", "agent_identities"}, (project_id, written)
    # A project inside the binding is filed under in any spelling, and the id stored is the canonical one.
    for project_id in (vault.alpha, vault.alpha.upper(), f" {vault.alpha} ", vault.alpha.replace("-", "")):
        status, body, _ = h.request("POST", "/v0/vnext/open-loops", payload={"title": "KEPT", "project_id": project_id}, key=bound)
        assert status == 201, (project_id, body)
        assert body["open_loop"]["project_id"] == vault.alpha, project_id


def test_the_owner_keeps_review_of_every_belief_and_edge(label_harness):
    """No agent key exists yet, so these calls are the owner's. Only a missing row is refused."""
    h = label_harness
    vault = build_vault(h)
    for route, action, row in (
        ("beliefs", "retire", ("belief", "confidential")),
        ("graph/edges", "reject", ("edge", "confidential")),
    ):
        status, body, _ = h.request("POST", f"/v0/vnext/{route}/{vault.ids[row]}/review", payload={"action": action})
        assert status == 200, (route, body)
    status, body, _ = h.request("POST", f"/v0/vnext/beliefs/{uuid4()}/review", payload={"action": "retire"})
    assert (status, body) == (404, {"detail": "vNext belief was not found"})
    status, body, _ = h.request("POST", f"/v0/vnext/graph/edges/{uuid4()}/review", payload={"action": "reject"})
    assert (status, body) == (404, {"detail": "vNext graph edge was not found"})
    # An id that is not well formed is a missing row for the owner too, and not a database error.
    for route, detail in (("beliefs", "vNext belief was not found"), ("graph/edges", "vNext graph edge was not found")):
        status, body, _ = h.request("POST", f"/v0/vnext/{route}/not-a-uuid/review", payload={"action": "retire" if route == "beliefs" else "reject"})
        assert (status, body) == (404, {"detail": detail}), route


def test_the_graph_neighborhood_returns_edges_and_none_of_the_row(label_harness):
    """Edge explanations stay outside the ceiling, which the notes already state. The row itself must not come back."""
    h = label_harness
    vault = build_vault(h)
    trusted = h.key("trusted_local_agent")
    hidden_memory = vault.ids[("memory", "confidential")]
    status, body, _ = h.request("GET", f"/v0/vnext/graph/neighborhood/{hidden_memory}", key=trusted)
    assert status == 200
    assert set(body) == {"target_id", "from_edges", "to_edges", "edge_count"}
    text = json.dumps(body)
    for word in vault.secrets[("memory", "confidential")]:
        assert word not in text
    for edge in (*body["from_edges"], *body["to_edges"]):
        assert set(edge) <= {
            "id", "user_id", "from_type", "from_id", "to_type", "to_id", "edge_type", "confidence", "explanation",
            "created_by", "observed_at", "valid_from", "valid_to", "metadata_json", "created_at",
        }
    assert UUID(body["target_id"]) == UUID(hidden_memory)


def test_a_commit_that_cites_a_hidden_source_answers_as_for_a_missing_one_except_under_a_prefix_that_is_not_source(label_harness):
    """The third answer the security note lists: a ref under another prefix is withheld from the answer, once, for a hidden source.

    A ref written as a bare id or with the ``source:`` prefix names a source, so a hidden source and a missing one are
    refused alike (HTTP 404). A ref under another prefix (``artifact:``, ``belief:``, ``open_loop:``, in any case) names
    no source, is stored as sent, and is read back like any text that may hold an id: the answer leaves the entry out
    when the id names a source the key cannot read. That tells a key whether an id it already holds names such a source,
    and gives it nothing of the source.
    """
    h = label_harness
    vault = build_vault(h)
    trusted = h.key("trusted_local_agent")

    def commit(ref: str):
        status, body, _ = h.request(
            "POST", "/v0/vnext/memories/commit", key=trusted,
            payload={"title": "C", "canonical_text": "C" + str(uuid4()), "source_refs": [ref], "confidence": 0.95, "domain": "project", "sensitivity": "public"},
        )
        memory = body.get("memory") or {}
        shown = (memory.get("metadata_json") or {}).get("agentic_memory", {}).get("source_refs")
        return status, body, shown, memory.get("id")

    hidden_sources = [vault.ids[("source", "confidential")], vault.ids[("source", "archived")]]
    for prefix in ("", "source:"):
        absent = commit(prefix + str(uuid4()))
        for source_id in hidden_sources:
            got = commit(prefix + source_id)
            assert (got[0], normalize(got[1])) == (absent[0], normalize(absent[1])) == (404, normalize(absent[1])), prefix
    visible = vault.ids[("source", "visible")]
    for prefix in ("artifact:", "belief:", "open_loop:", "ARTIFACT:"):
        status, _body, shown, _memory_id = commit(prefix + str(uuid4()))
        assert status == 201 and shown and len(shown) == 1, prefix
        assert commit(prefix + visible)[2], prefix
        for source_id in hidden_sources:
            status, body, shown, memory_id = commit(prefix + source_id)
            text = json.dumps(body)
            assert status == 201 and shown == [], (prefix, shown)  # the one bit
            assert not any(word in text for key in (("source", "confidential"), ("source", "archived")) for word in vault.secrets[key])
            with h.store() as store:
                stored = store.get_memory(memory_id)
            assert prefix + source_id in json.dumps(stored["metadata_json"])  # stored as sent: nothing is lost

