"""Knowing the id of a row never gives a key access to it.

A readable report or memory can list, in its own metadata, the id of a row the same key may not read (a candidate that
was relabelled after the report was made, a source archived since). So the question for every door that takes an id is
what a key gets when it names such a row. Every case below builds a vault in which rows of each kind are hidden for each
reason, gives a real key of each profile, and calls every door with every id that profile may not read.

For each call:

* the call changes exactly the tables a missing id changes, which for a door that acts on a row is none, and for a
  door that only cites a row in a new one is the table of that new row;
* nothing of the row (its title, text, summary, raw text, chunk or claim) comes back;
* the answer is the one a missing id gets, except for the doors in ``POLICY_REFUSAL_DOORS``. Those name the row to act
  on and refuse a row above the caller's limits with the policy refusal (HTTP 403, or the tool's not-permitted error)
  where a missing row gets a not-found answer. That refusal confirms that the row exists and can repeat its labels. It
  is the known exception that the security note names, and this list is its boundary: a door that is not on it must
  answer a hidden id exactly as it answers a missing one, and a door on it must still refuse.

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
    POLICY_REFUSAL_DOORS,
    PROFILES,
    Env,
    build_vault,
    snapshot,
)


def _key(h, vault, profile):
    permission, bound = PROFILES[profile]
    return h.key(permission, project=vault.alpha if bound else None)


@pytest.mark.parametrize("profile", list(PROFILES))
def test_a_hidden_id_gives_no_access_through_any_door(label_harness, monkeypatch, tmp_path, profile):
    h = label_harness
    vault = build_vault(h)
    key = _key(h, vault, profile)
    env = Env(h, monkeypatch, h.urls["app"], str(tmp_path))
    hidden = vault.hidden_ids(profile)
    if PROFILES[profile][1]:
        hidden[("project", "beta")] = vault.beta  # a project the binding leaves out
    assert hidden, profile
    failures: list[str] = []
    calls = 0
    for door in ALL_DOORS:
        before = snapshot(h) if door.write else None
        missing = door.call(env, key, str(uuid4()))
        after = snapshot(h) if door.write else None
        missing_changed = {t for t in (before or {}) if before[t] != after[t]}
        for (kind, reason), row_id in hidden.items():
            before = snapshot(h) if door.write else None
            got = door.call(env, key, row_id)
            after = snapshot(h) if door.write else None
            calls += 1
            label = f"{profile} / {door.name} / {kind} hidden as {reason}"
            changed = {t for t in (before or {}) if before[t] != after[t]}
            if changed != missing_changed:
                failures.append(f"{label}: changed {sorted(changed)} where a missing id changes {sorted(missing_changed)}")
            leaked = [word for word in vault.secrets.get((kind, reason), []) if word in got.body]
            if leaked:
                failures.append(f"{label}: answered with {leaked}")
            if got != missing and not (door.name in POLICY_REFUSAL_DOORS and got.refused):
                failures.append(f"{label}: {got} differs from a missing id: {missing}")
    assert calls == len(ALL_DOORS) * len(hidden), calls
    assert not failures, "\n".join(failures[:25])


def test_the_listed_policy_refusals_exist_and_the_rest_are_exact(label_harness, monkeypatch, tmp_path):
    """The exception list is neither padded nor stale: each door on it refuses some hidden id differently from a missing id."""
    h = label_harness
    vault = build_vault(h)
    env = Env(h, monkeypatch, h.urls["app"], str(tmp_path))
    differing: set[str] = set()
    for profile in ("read_only", "trusted"):
        key = _key(h, vault, profile)
        for door in ALL_DOORS:
            if not door.write and not door.name.startswith(("GET artifact", "GET memory", "tool review")):
                continue
            missing = door.call(env, key, str(uuid4()))
            for (kind, reason), row_id in vault.hidden_ids(profile).items():
                if door.call(env, key, row_id) != missing:
                    differing.add(door.name)
    assert differing == POLICY_REFUSAL_DOORS, sorted(differing ^ POLICY_REFUSAL_DOORS)


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
)


@pytest.mark.parametrize("profile", sorted({control[0] for control in CONTROLS}))
def test_the_same_doors_serve_a_row_the_profile_may_read(label_harness, monkeypatch, tmp_path, profile):
    h = label_harness
    vault = build_vault(h)
    key = _key(h, vault, profile)
    env = Env(h, monkeypatch, h.urls["app"], str(tmp_path))
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
    h = label_harness
    vault = build_vault(h)
    with h.store() as store:
        dangling = store.create_edge(
            {
                "from_type": "source", "from_id": vault.ids[("source", "visible")], "to_type": "memory",
                "to_id": str(uuid4()), "edge_type": "similar_to", "confidence": 0.5, "explanation": "EDGE-dangling",
                "created_by": "vnext_connection_finder", "metadata_json": {"status": "candidate", "candidate": True},
            }
        )
        beyond = store.create_edge(
            {
                "from_type": "source", "from_id": vault.ids[("source", "visible")], "to_type": "memory",
                "to_id": vault.ids[("memory", "unverified")], "edge_type": "similar_to", "confidence": 0.5,
                "explanation": "EDGE-unverified", "created_by": "vnext_connection_finder",
                "metadata_json": {"status": "candidate", "candidate": True},
            }
        )
    trusted = h.key("trusted_local_agent")
    for edge in (dangling, beyond):
        before = snapshot(h)
        status, body, _ = h.request("POST", f"/v0/vnext/graph/edges/{edge['id']}/review", payload={"action": "reject"}, key=trusted)
        assert (status, body) == (404, {"detail": "vNext graph edge was not found"})
        assert snapshot(h) == before


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
