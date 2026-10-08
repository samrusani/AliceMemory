"""Source review, update, assign, archive, delete and regenerate apply the caller's limits.

Every call goes through the mounted application on PostgreSQL. The rows are built through the store, the keys are real,
and the answer, the body and the stored rows are read back after each call. A caller who may not read a source gets the
answer a missing source gets, sees none of its text anywhere in the body, and changes nothing.
"""
from __future__ import annotations

import json
import threading
import time
from uuid import UUID, uuid4

import pytest

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)

# profile name -> (permission profile, bound to a project)
PROFILES = {
    "owner": (None, False),
    "admin": ("admin_agent", False),
    "trusted": ("trusted_local_agent", False),
    "read_only": ("read_only_agent", False),
    "memory_proposal": ("memory_proposal_agent", False),
    "trusted_bound": ("trusted_local_agent", True),
    "alpha_only": ("project_scoped_agent", True),
}
VERBS = ("review", "update", "assign_project", "archive", "delete", "regenerate")
KINDS = ("visible", "confidential", "other_project", "missing")
TABLES = (
    "sources", "source_chunks", "memories", "open_loops", "generated_artifacts", "projects", "graph_edges",
    "provenance_links", "event_log",
)
RENAMED = "RENAMED-BY-THE-CALLER"
GATE_DETAIL = "agent policy blocked this action"
REGENERATE_DETAIL = "source regeneration requires the owner or an unbound admin"


def _not_found(source_id) -> dict[str, str]:
    return {"detail": f"vNext source {source_id} was not found"}


class World:
    """Three stored sources with a chunk each, and rows around them that name them."""

    def __init__(self, harness, tag: str, *, hidden_sensitivity: str = "confidential") -> None:
        self.h = harness
        self.tag = tag
        self.alpha, self.beta, self.gamma = (str(uuid4()) for _ in range(3))
        with harness.store() as store:
            for identifier in (self.alpha, self.beta, self.gamma):
                store.create_project({"id": identifier, "name": identifier, "slug": identifier})
        self.sources = {
            "visible": self._source("visible", scope=(self.alpha,), sensitivity="public"),
            "confidential": self._source("confidential", scope=(self.alpha,), sensitivity=hidden_sensitivity),
            "other_project": self._source("other_project", scope=(self.beta,), sensitivity="public"),
        }
        self.ids = {kind: str(row["id"]) for kind, row in self.sources.items()}
        self.ids["missing"] = str(uuid4())

    def _source(self, kind: str, *, scope, sensitivity: str):
        with self.h.store() as store:
            row = store.create_source(
                {
                    "source_type": "note",
                    "title": f"TITLE-{self.tag}-{kind}",
                    "content_hash": str(uuid4()),
                    "domain": "project",
                    "sensitivity": sensitivity,
                    "metadata_json": {"project_scope": list(scope), "raw_text": f"RAW-{self.tag}-{kind}"},
                }
            )
            store.create_source_chunk({"source_id": str(row["id"]), "chunk_index": 0, "text": f"CHUNK-{self.tag}-{kind}"})
            source_id = str(row["id"])
            # A readable candidate and three rows above the trusted ceiling, all naming the source.
            store.create_memory(
                {
                    "memory_key": str(uuid4()), "canonical_text": f"MEMORY-OPEN-{self.tag}-{kind}", "status": "candidate",
                    "domain": "project", "sensitivity": "public",
                    "metadata_json": {"project_scope": list(scope), "source_id": source_id},
                }
            )
            store.create_memory(
                {
                    "memory_key": str(uuid4()), "canonical_text": f"MEMORY-SECRET-{self.tag}-{kind}", "status": "candidate",
                    "domain": "project", "sensitivity": "confidential",
                    "metadata_json": {"project_scope": list(scope), "source_id": source_id},
                }
            )
            store.create_open_loop(
                {
                    "title": f"LOOP-SECRET-{self.tag}-{kind}", "source_id": source_id, "domain": "project",
                    "sensitivity": "confidential", "metadata_json": {"project_scope": list(scope)},
                }
            )
            store.create_artifact(
                {
                    "artifact_type": "daily_brief", "title": f"ARTIFACT-SECRET-{self.tag}-{kind}",
                    "content_markdown": f"ARTIFACT-SECRET-{self.tag}-{kind}", "domain": "project", "sensitivity": "confidential",
                    "metadata_json": {"project_scope": list(scope), "source_refs": [f"source:{source_id}"]},
                }
            )
            return row

    def call(self, verb: str, kind: str, key):
        source_id = self.ids[kind]
        base = f"/v0/vnext/sources/{source_id}"
        request = self.h.request
        if verb == "review":
            return request("POST", base + "/review", payload={"action": "review", "review_note": "checked"}, key=key)
        if verb == "update":
            return request("POST", base + "/review", payload={"action": "update", "title": RENAMED}, key=key)
        if verb == "assign_project":
            payload = {"action": "assign_project", "project_id": self.gamma, "confirm_label_hide": True}
            return request("POST", base + "/review", payload=payload, key=key)
        if verb == "archive":
            return request("POST", base + "/review", payload={"action": "archive"}, key=key)
        if verb == "delete":
            return request("DELETE", base, key=key)
        if verb == "regenerate":
            return request("POST", base + "/regenerate", payload={}, key=key)
        raise AssertionError(verb)

    def secrets(self, kind: str) -> list[str]:
        tag = f"{self.tag}-{kind}"
        return [
            f"RAW-{tag}", f"TITLE-{tag}", f"CHUNK-{tag}", f"MEMORY-OPEN-{tag}", f"MEMORY-SECRET-{tag}",
            f"LOOP-SECRET-{tag}", f"ARTIFACT-SECRET-{tag}",
        ]


def _snapshot(harness) -> dict[str, list]:
    result = {}
    with harness.store() as store, store.conn.cursor() as cur:
        for table in TABLES:
            cur.execute(f"SELECT row_to_json(t) FROM {table} t ORDER BY id")  # closed table names
            result[table] = [row["row_to_json"] for row in cur.fetchall()]
    return result


def _expected(profile: str, verb: str, kind: str) -> int:
    if profile in {"owner", "admin"}:
        if kind == "missing":
            return 404
        return 201 if verb == "regenerate" else 200
    if profile == "trusted":
        if verb == "regenerate":
            return 403
        return 404 if kind in {"confidential", "missing"} else 200
    return 403


def _stored(harness, source_id: str):
    with harness.store() as store:
        return store.get_source(source_id)


def _key(harness, profile: str, world: World):
    permission, bound = PROFILES[profile]
    if permission is None:
        return None
    return harness.key(permission, project=world.alpha if bound else None)


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("profile", list(PROFILES))
def test_every_profile_verb_and_source(label_harness, profile, verb):
    h = label_harness
    world = World(h, f"{profile}-{verb}")
    key = _key(h, profile, world)
    for kind in KINDS:
        before = _snapshot(h)
        status, body, _headers = world.call(verb, kind, key)
        after = _snapshot(h)
        want = _expected(profile, verb, kind)
        text = json.dumps(body)
        assert status == want, (profile, verb, kind, body)
        if want == 404:
            # Regenerate is refused to every limited caller before the lookup, so only the owner and an unbound admin see its 404.
            # A review of a missing source keeps the owner's and the unbound admin's earlier text; a key with a ceiling gets the GET body.
            plain = verb == "regenerate" or (verb != "delete" and profile in {"owner", "admin"})
            expected_body = {"detail": "vNext source was not found"} if plain else _not_found(world.ids[kind])
            assert body == expected_body, (profile, verb, kind)
        if want in {403, 404}:
            if kind != "missing":
                for secret in world.secrets(kind):
                    assert secret not in text, (profile, verb, kind, secret)
            if want == 404 or (profile == "trusted" and verb == "regenerate"):
                assert after == before, (profile, verb, kind)
            else:
                # The central operator gate records its own refusal and nothing else.
                for table in TABLES:
                    if table != "event_log":
                        assert after[table] == before[table], (profile, verb, kind, table)
                added = [row for row in after["event_log"] if row not in before["event_log"]]
                assert added and {row["target_type"] for row in added} == {"http_route"}, (profile, verb, kind)
            if want == 403 and verb == "regenerate" and profile == "trusted":
                assert body == {"detail": REGENERATE_DETAIL}
            elif want == 403:
                assert body["detail"] == GATE_DETAIL
            continue
        stored = _stored(h, world.ids[kind])
        if verb == "regenerate":
            assert body["source_id"] == world.ids[kind]
            assert stored is not None
        elif verb in {"archive", "delete"}:
            assert stored is None
            assert body.get("archived", True) is True if verb == "archive" else body["id"] == world.ids[kind]
        elif verb == "review":
            assert stored["metadata_json"]["review_status"] == "reviewed"
        elif verb == "update":
            assert stored["title"] == RENAMED
        else:
            assert stored["metadata_json"]["project_scope"] == [world.gamma]
        # What the owner and an unbound admin are shown stays the whole source and the whole trace.
        if verb in {"review", "update", "assign_project", "archive"}:
            assert body["trace"]["source"]["id"] == world.ids[kind]
            traced = json.dumps(body["trace"])
            full = profile in {"owner", "admin"}
            hidden_rows = [f"MEMORY-SECRET-{world.tag}-{kind}", f"ARTIFACT-SECRET-{world.tag}-{kind}"]
            if verb != "archive":  # the trace of an archived source no longer lists its open loops, for the owner too
                hidden_rows.append(f"LOOP-SECRET-{world.tag}-{kind}")
            for secret in hidden_rows:
                assert (secret in traced) is full, (profile, verb, kind, secret)
            assert f"MEMORY-OPEN-{world.tag}-{kind}" in traced
            assert f"CHUNK-{world.tag}-{kind}" in traced


@pytest.mark.parametrize("sensitivity", ["confidential", "highly_sensitive", "sacred", "regulated"])
@pytest.mark.parametrize("verb", ["review", "update", "archive", "delete"])
def test_a_trusted_key_cannot_reach_any_restricted_sensitivity(label_harness, sensitivity, verb):
    h = label_harness
    world = World(h, f"{sensitivity}-{verb}", hidden_sensitivity=sensitivity)
    key = h.key("trusted_local_agent")
    before = _snapshot(h)
    status, body, _headers = world.call(verb, "confidential", key)
    assert status == 404 and body == _not_found(world.ids["confidential"])
    assert _snapshot(h) == before
    for secret in world.secrets("confidential"):
        assert secret not in json.dumps(body)


@pytest.mark.parametrize("verb", ["review", "update", "assign_project", "archive", "delete"])
def test_the_denial_is_the_answer_of_the_single_source_view(label_harness, verb):
    h = label_harness
    world = World(h, f"same-{verb}")
    key = h.key("trusted_local_agent")
    answers = []
    for kind in ("confidential", "missing"):
        view = h.request("GET", "/v0/vnext/sources/" + world.ids[kind], key=key)
        verb_answer = world.call(verb, kind, key)
        assert view[0] == verb_answer[0] == 404
        assert view[1] == verb_answer[1] == _not_found(world.ids[kind])
        answers.append(verb_answer[1]["detail"].replace(world.ids[kind], "<id>"))
    # A hidden source and a missing one are the same answer, so an id says nothing about the row behind it.
    assert answers[0] == answers[1]


def test_the_owner_changes_a_confidential_source_and_a_key_is_refused_it_afterwards(label_harness):
    h = label_harness
    world = World(h, "owner-first")
    status, body, _ = world.call("update", "confidential", None)
    assert status == 200 and body["source"]["title"] == RENAMED
    assert body["source"]["metadata_json"]["raw_text"] == "RAW-owner-first-confidential"
    assert any("MEMORY-SECRET-owner-first-confidential" in json.dumps(row) for row in body["trace"]["candidate_memories"])
    key = h.key("trusted_local_agent")
    status, body, _ = world.call("review", "confidential", key)
    assert status == 404
    assert _stored(h, world.ids["confidential"])["title"] == RENAMED


def test_a_trusted_key_can_still_review_and_move_a_readable_source_into_a_project(label_harness):
    h = label_harness
    world = World(h, "readable")
    key = h.key("trusted_local_agent")
    status, body, _ = world.call("assign_project", "visible", key)
    assert status == 200
    assert body["source"]["metadata_json"]["project_scope"] == [world.gamma]
    status, body, _ = world.call("review", "visible", key)
    assert status == 200 and body["source"]["metadata_json"]["review_status"] == "reviewed"
    assert "MEMORY-SECRET-readable-visible" not in json.dumps(body)
    assert "MEMORY-OPEN-readable-visible" in json.dumps(body)


def test_a_trusted_key_that_raises_its_own_source_above_its_ceiling_gets_no_trace(label_harness):
    h = label_harness
    world = World(h, "raise")
    key = h.key("trusted_local_agent")
    status, body, _ = h.request(
        "POST", f"/v0/vnext/sources/{world.ids['visible']}/review",
        payload={"action": "update", "sensitivity": "confidential"}, key=key,
    )
    assert status == 200
    assert body["source"]["sensitivity"] == "confidential"
    assert body["trace"] == {}
    assert _stored(h, world.ids["visible"])["sensitivity"] == "confidential"
    status, body, _ = h.request("GET", "/v0/vnext/sources/" + world.ids["visible"], key=key)
    assert status == 404


@pytest.mark.parametrize("profile", ["owner", "admin", "trusted"])
def test_the_source_trace_route_applies_the_same_limits(label_harness, profile):
    h = label_harness
    world = World(h, f"trace-{profile}")
    key = _key(h, profile, world)
    for kind in KINDS:
        status, body, _ = h.request("GET", "/v0/vnext/traces/sources/" + world.ids[kind], key=key)
        hidden = profile == "trusted" and kind in {"confidential", "missing"}
        missing = kind == "missing" or hidden
        assert status == (404 if missing else 200), (profile, kind, body)
        text = json.dumps(body)
        if missing:
            assert body == {"detail": "vNext source was not found"}
            continue
        assert (f"MEMORY-SECRET-{world.tag}-{kind}" in text) is (profile != "trusted")
        assert f"MEMORY-OPEN-{world.tag}-{kind}" in text


def test_a_delete_waits_for_a_relabel_in_flight_and_then_judges_the_new_label(label_harness):
    """The check and the delete are one step: a source made confidential meanwhile is refused, not deleted and shown."""
    h = label_harness
    world = World(h, "race")
    key = h.key("trusted_local_agent")
    source_id = world.ids["visible"]
    holding = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []

    def relabel() -> None:
        try:
            with h.store() as store:
                store.lock_label_writes(exclusive=True)
                with store.conn.cursor() as cur:
                    cur.execute("UPDATE sources SET sensitivity = 'confidential' WHERE id = %s::uuid", (source_id,))
                holding.set()
                assert release.wait(10)
        except BaseException as exc:  # pragma: no cover - reported through the assertion below
            failures.append(exc)
            holding.set()

    relabeler = threading.Thread(target=relabel)
    relabeler.start()
    assert holding.wait(10) and not failures
    answers: list[tuple] = []
    deleter = threading.Thread(target=lambda: answers.append(world.call("delete", "visible", key)))
    deleter.start()
    time.sleep(0.6)
    assert deleter.is_alive(), "the delete did not wait for the relabel in flight"
    release.set()
    relabeler.join(10)
    deleter.join(10)
    assert not failures and not relabeler.is_alive() and not deleter.is_alive()
    status, body, _ = answers[0]
    assert status == 404 and body == _not_found(source_id)
    assert "RAW-race-visible" not in json.dumps(body)
    stored = _stored(h, source_id)
    assert stored is not None and stored["sensitivity"] == "confidential"


def test_a_call_inside_the_process_without_a_header_is_the_owner_and_http_without_a_key_is_refused(label_harness):
    """The owner's tools call the handlers directly; over HTTP an install with keys refuses a call with no key."""
    from alicebot_api.routers import vnext_memories as router

    h = label_harness
    world = World(h, "inprocess")
    key = h.key("trusted_local_agent")
    status, body, _ = h.request("POST", f"/v0/vnext/sources/{world.ids['visible']}/review", payload={"action": "review"})
    assert status == 401 and "RAW-inprocess-visible" not in json.dumps(body)
    status, body, _ = h.request("DELETE", f"/v0/vnext/sources/{world.ids['visible']}")
    assert status == 401 and _stored(h, world.ids["visible"]) is not None
    status, body, _ = h.request("DELETE", f"/v0/vnext/sources/{world.ids['visible']}", key=key[:-3] + "xyz")
    assert status == 401 and _stored(h, world.ids["visible"]) is not None
    request = router.VNextSourceReviewRequest(user_id=h.user_id, action="update", title=RENAMED)
    response = router.review_vnext_source(UUID(world.ids["confidential"]), request)
    assert response.status_code == 200
    assert _stored(h, world.ids["confidential"])["title"] == RENAMED
    assert router.delete_vnext_source(UUID(world.ids["confidential"]), h.user_id).status_code == 200
    assert _stored(h, world.ids["confidential"]) is None


@pytest.mark.parametrize("profile,expected", [("trusted_local_agent", 404), ("admin_agent", 200), ("read_only_agent", 403)])
def test_a_profile_declared_on_an_install_without_keys_is_held_to_it(label_harness, profile, expected):
    h = label_harness
    world = World(h, f"declared-{profile}")
    claim = {"agent_id": "declared-agent", "agent_type": "coding_agent", "permission_profile": profile}
    status, body, _ = h.request(
        "POST", f"/v0/vnext/sources/{world.ids['confidential']}/review", payload={"action": "update", "title": RENAMED, **claim}
    )
    assert status == expected, body
    changed = _stored(h, world.ids["confidential"])["title"] == RENAMED
    assert changed is (expected == 200)
    if expected != 200:
        assert "RAW-declared" not in json.dumps(body)


def test_claims_that_contradict_each_other_are_refused_before_anything_changes(label_harness):
    h = label_harness
    world = World(h, "claims")
    before = _snapshot(h)
    claims = {
        "agent_id": "first-agent",
        "agent_identity": {"agent_id": "second-agent", "agent_type": "coding_agent", "permission_profile": "admin_agent"},
    }
    status, body, _ = h.request(
        "POST", f"/v0/vnext/sources/{world.ids['confidential']}/review", payload={"action": "update", "title": RENAMED, **claims}
    )
    assert status == 400 and body == {"detail": "vNext agent identity claims are invalid"}
    assert _snapshot(h) == before


def _movable_source(h, tag: str, alpha: str, *, readable: int, hidden: int) -> str:
    """A public source in alpha, ``readable`` public memories that name it and ``hidden`` confidential rows that name it.

    The hidden rows are memories and artifacts in turn, the two kinds a source move counts. Every text is unique, so no two sources collide.
    """
    with h.store() as store:
        row = store.create_source(
            {
                "source_type": "note", "title": f"MOVE-{tag}", "content_hash": str(uuid4()), "domain": "project",
                "sensitivity": "public", "metadata_json": {"project_scope": [alpha], "raw_text": f"RAW-MOVE-{tag}"},
            }
        )
        source_id = str(row["id"])
        for index in range(readable):
            store.create_memory(
                {
                    "memory_key": str(uuid4()), "canonical_text": f"MOVE-OPEN-{tag}-{index}", "status": "candidate",
                    "domain": "project", "sensitivity": "public",
                    "metadata_json": {"project_scope": [alpha], "source_id": source_id},
                }
            )
        for index in range(hidden):
            if index % 2 == 0:
                store.create_memory(
                    {
                        "memory_key": str(uuid4()), "canonical_text": f"MOVE-SECRET-{tag}-{index}", "status": "candidate",
                        "domain": "project", "sensitivity": "confidential",
                        "metadata_json": {"project_scope": [alpha], "source_id": source_id},
                    }
                )
            else:
                store.create_artifact(
                    {
                        "artifact_type": "daily_brief", "title": f"MOVE-SECRET-{tag}-{index}",
                        "content_markdown": f"MOVE-SECRET-{tag}-{index}", "domain": "project", "sensitivity": "confidential",
                        "metadata_json": {"project_scope": [alpha], "source_ids": [source_id]},
                    }
                )
    return source_id


def _projects(h) -> tuple[str, str]:
    alpha, beta = str(uuid4()), str(uuid4())
    with h.store() as store:
        for identifier in (alpha, beta):
            store.create_project({"id": identifier, "name": identifier, "slug": identifier})
    return alpha, beta


def _move(h, source_id: str, beta: str, key, *, confirm: bool = False):
    payload = {"action": "assign_project", "project_id": beta}
    if confirm:
        payload["confirm_label_hide"] = True
    return h.request("POST", f"/v0/vnext/sources/{source_id}/review", payload=payload, key=key)


def _answer_without_source(body: dict) -> dict:
    return {name: value for name, value in body.items() if name not in {"source", "trace"}}


@pytest.mark.parametrize("hidden", [1, 2, 3, 5])
def test_the_move_preview_does_not_count_rows_above_the_callers_ceiling(label_harness, hidden):
    """A source with no readable dependant moves plainly whatever it has above the ceiling, so the answer holds no count."""
    h = label_harness
    alpha, beta = _projects(h)
    key = h.key("trusted_local_agent")
    reference = _movable_source(h, f"ref-{hidden}", alpha, readable=0, hidden=0)
    with_hidden = _movable_source(h, f"hid-{hidden}", alpha, readable=0, hidden=hidden)
    first = _move(h, reference, beta, key)
    second = _move(h, with_hidden, beta, key)
    assert first[0] == second[0] == 200
    assert _answer_without_source(first[1]) == _answer_without_source(second[1]) == {"archived": False}
    assert "preview" not in second[1] and "derived_rows_hidden_from_project_keys" not in json.dumps(second[1])
    assert _stored(h, with_hidden)["metadata_json"]["project_scope"] == [beta]
    assert "MOVE-SECRET" not in json.dumps(second[1])


def test_the_move_preview_counts_only_the_rows_the_caller_may_read(label_harness):
    h = label_harness
    alpha, beta = _projects(h)
    key = h.key("trusted_local_agent")
    source_id = _movable_source(h, "mixed", alpha, readable=2, hidden=3)
    before = _snapshot(h)
    status, body, _ = _move(h, source_id, beta, key)
    assert status == 200
    assert body == {"preview": True, "derived_rows_hidden_from_project_keys": 2, "confirm_required": True}
    assert _snapshot(h) == before
    assert "MOVE-SECRET" not in json.dumps(body)
    status, body, _ = _move(h, source_id, beta, key, confirm=True)
    assert status == 200 and body["source"]["metadata_json"]["project_scope"] == [beta]
    assert "MOVE-SECRET" not in json.dumps(body)
    assert "MOVE-OPEN-mixed" in json.dumps(body["trace"])


@pytest.mark.parametrize("profile", ["owner", "admin"])
def test_the_owner_and_an_unbound_admin_see_the_exact_preview_count(label_harness, profile):
    h = label_harness
    alpha, beta = _projects(h)
    key = None if profile == "owner" else h.key("admin_agent")
    source_id = _movable_source(h, f"exact-{profile}", alpha, readable=2, hidden=3)
    before = _snapshot(h)
    status, body, _ = _move(h, source_id, beta, key)
    assert status == 200
    assert body == {"preview": True, "derived_rows_hidden_from_project_keys": 5, "confirm_required": True}
    assert _snapshot(h) == before
    status, body, _ = _move(h, source_id, beta, key, confirm=True)
    assert status == 200 and body["source"]["metadata_json"]["project_scope"] == [beta]
    assert any("MOVE-SECRET" in json.dumps(row) for row in body["trace"]["candidate_memories"])


def test_a_missing_source_keeps_the_text_the_owner_and_an_unbound_admin_always_had(label_harness):
    h = label_harness
    missing = str(uuid4())
    # The owner calls before any key exists; once an install has keys, a call with none is refused.
    status, body, _ = h.request("POST", f"/v0/vnext/sources/{missing}/review", payload={"action": "review"})
    assert status == 404 and body == {"detail": "vNext source was not found"}
    status, body, _ = h.request(
        "POST", f"/v0/vnext/sources/{missing}/review", payload={"action": "review"}, key=h.key("admin_agent")
    )
    assert status == 404 and body == {"detail": "vNext source was not found"}
    status, body, _ = h.request(
        "POST", f"/v0/vnext/sources/{missing}/review", payload={"action": "review"}, key=h.key("trusted_local_agent")
    )
    assert status == 404 and body == _not_found(missing)


def test_the_move_preview_judges_a_row_on_its_effective_labels(label_harness):
    """A report stored as public that also names a confidential source is above the ceiling, and is not counted."""
    h = label_harness
    alpha, beta = _projects(h)
    key = h.key("trusted_local_agent")
    source_id = _movable_source(h, "effective", alpha, readable=1, hidden=0)
    with h.store() as store:
        other = store.create_source(
            {
                "source_type": "note", "title": "MOVE-SECRET-other", "content_hash": str(uuid4()), "domain": "project",
                "sensitivity": "confidential", "metadata_json": {"project_scope": [alpha], "raw_text": "RAW-MOVE-other"},
            }
        )
        report = store.create_artifact(
            {
                "artifact_type": "daily_brief", "title": "MOVE-SECRET-report", "content_markdown": "MOVE-SECRET-report",
                "domain": "project", "sensitivity": "confidential",
                "metadata_json": {"project_scope": [alpha], "source_ids": [source_id, str(other["id"])]},
            }
        )
        # The stored label is public; only the label settled from its inputs puts it above the ceiling.
        with store.conn.cursor() as cur:
            cur.execute("UPDATE generated_artifacts SET sensitivity = 'public' WHERE id = %s::uuid", (str(report["id"]),))
        assert store.get_artifact(str(report["id"]))["sensitivity"] == "public"
    status, body, _ = _move(h, source_id, beta, key)
    assert status == 200
    assert body == {"preview": True, "derived_rows_hidden_from_project_keys": 1, "confirm_required": True}
    status, body, _ = _move(h, source_id, beta, h.key("admin_agent"))
    assert status == 200
    assert body == {"preview": True, "derived_rows_hidden_from_project_keys": 2, "confirm_required": True}
