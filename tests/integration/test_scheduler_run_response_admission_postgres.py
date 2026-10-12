"""The answer of an on-demand workflow run is judged on the caller's limits, as the artifact read is.

A key bound to a project that ran ``POST /v0/vnext/scheduler/workflows/memory_consolidation/run-now`` over a cluster
whose owner-written member quotes a confidential memory used to get HTTP 201 with the report and the quote in the body,
and then HTTP 404 from ``GET /v0/vnext/artifacts/{id}`` for the same report: the stored label protected the later read,
and the answer of the run was never judged. The same holds for a report found again under the digest of the run (the
owner raised it after it was made), for the runs a scan starts, and for the tools and the command that run a workflow.

The answer now carries the report only when the caller of the run may read it now (``readable_artifact``, the check the
artifact read applies). A report the caller may not read is left out, as a missing report is: ``artifact`` is ``null``
and the run is returned without the id it recorded. The owner and an unbound admin key are answered as before.

Mutations, each one alone (replayed by ``scripts/verify_derived_label_mutations.py``): make ``readable_artifact`` admit
every report; make ``withhold_unreadable_artifact`` hand back the report it was given; leave the judgement out of
``publish_started_workflow``; leave it out of the answer of a scan (``run_due_workflows_durable``).
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

import alicebot_api.vnext_consolidation as consolidation
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_embeddings import pad_embedding_vector
from alicebot_api.vnext_label_writes import acquire_exclusive_label_lock
from alicebot_api.vnext_scheduler import VNextSchedulerService
from tests.integration.derived_labels_postgres_support import LabelHarness, label_harness, today  # noqa: F401  (fixtures)

RUN_NOW = "/v0/vnext/scheduler/workflows/{}/run-now"
QUOTE = "CONFIDENTIAL-QUOTE-the-merger-closes-friday"
HIDDEN = "CONF-SENTINEL-kiln-acquisition-price"
REPLAYED = (
    "daily_brief", "weekly_synthesis", "connection_report", "contradiction_report", "open_loop_review",
    "project_update_scan", "memory_consolidation",
)
EVERY_WORKFLOW = (*REPLAYED, "staleness_sweep")


class _OneVector:
    """An embedding provider that puts every text at the same point, so identical memories form one cluster."""

    provider = "test_embeddings"
    model = "test-embed-1"

    def embed_text(self, text):
        return list(pad_embedding_vector([0.5, 0.1, 0.2]))

    def embed_batch(self, texts):
        return [self.embed_text(text) for text in texts]


@pytest.fixture(autouse=True)
def _cluster_provider(monkeypatch):
    monkeypatch.setattr(consolidation, "get_embedding_provider", lambda: _OneVector())


# -- the vault ------------------------------------------------------------------------------------------------------


def _project(h, name="Kiln"):
    with h.store() as store:
        row = store.create_project(
            {"name": name, "slug": f"{name.lower()}-{uuid4().hex[:8]}", "description": "d", "current_state": "s",
             "domain": "project", "sensitivity": "public"}
        )
    return str(row["id"])


def _memory(h, project, *, sensitivity, text, refs=(), vector=None, valid_to=None):
    with h.store() as store:
        row = store.create_memory(
            {
                "memory_key": f"m.{uuid4().hex[:10]}",
                "memory_type": "semantic",
                "title": text,
                "canonical_text": text,
                "summary": text,
                "status": "active",
                "value": {"text": text},
                "domain": "project",
                "sensitivity": sensitivity,
                "valid_to": valid_to,
                "metadata_json": {"project_scope": [project], **({"source_refs": list(refs)} if refs else {})},
            }
        )
        if vector is not None:
            store.update_memory_embedding(memory_id=str(row["id"]), vector=pad_embedding_vector(vector))
    return row


def _source(h, project, *, sensitivity, text):
    return h.source(scope=(project,), text=text, sensitivity=sensitivity)


def _loop(h, project, source, *, sensitivity, title):
    with h.store() as store:
        return store.create_open_loop(
            {"title": title, "description": f"{title} details", "domain": "project", "sensitivity": sensitivity,
             "project_id": project, "source_id": str(source["id"]), "metadata_json": {"project_scope": [project]}}
        )


def _vault(h):
    """A project with readable rows of every kind, a stale memory, a cluster of three, and confidential twins of each."""

    project = _project(h)
    readable_source = _source(h, project, sensitivity="public", text="TODO: order kiln parts. Decision: use the blue glaze.")
    _loop(h, project, readable_source, sensitivity="public", title="Renew the kiln permit")
    _memory(h, project, sensitivity="internal", text="The kiln is fired on Mondays.", valid_to="2020-01-01T00:00:00+00:00")
    for _ in range(3):
        _memory(h, project, sensitivity="internal", text="The launch window moves to March after the review.",
                vector=[0.5, 0.1, 0.2])
    hidden_source = _source(h, project, sensitivity="confidential", text=f"TODO: wire the deposit. {HIDDEN}")
    _loop(h, project, hidden_source, sensitivity="confidential", title=f"Pay the deposit {HIDDEN}")
    _memory(h, project, sensitivity="confidential", text=f"The acquisition price is high. {HIDDEN}",
            valid_to="2020-01-01T00:00:00+00:00")
    return project


def _quoting_cluster(h, project):
    """Three near-duplicate memories, one of which quotes a confidential memory in its own source refs."""

    secret = _memory(h, project, sensitivity="confidential", text=f"The merger closes Friday. {QUOTE}")
    ref = json.dumps({"memory_id": str(secret["id"]), "quote": QUOTE})
    for index in range(3):
        _memory(h, project, sensitivity="internal", text="The launch window moves to March after the review.",
                refs=(ref,) if index == 0 else (), vector=[0.5, 0.1, 0.2])
    return secret


# -- helpers --------------------------------------------------------------------------------------------------------


def _run(h, workflow, key, project, **options):
    payload = {"scope": {"projects": [project]}, "options": {"generated_for": today(), **options}}
    return h.request("POST", RUN_NOW.format(workflow), payload=payload, key=key)


def _get_artifact(h, artifact_id, key):
    return h.request("GET", f"/v0/vnext/artifacts/{artifact_id}", key=key)


def _stored_artifacts(h, artifact_type=None):
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute(
            "SELECT id::text AS id, artifact_type, title, sensitivity, content_markdown, metadata_json "
            "FROM generated_artifacts " + ("WHERE artifact_type = %s " if artifact_type else "") + "ORDER BY created_at, id",
            (artifact_type,) if artifact_type else (),
        )
        return [dict(row) for row in cur.fetchall()]


def _stored_runs(h):
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id::text AS id, status, artifact_id::text AS artifact_id FROM scheduler_runs ORDER BY started_at, id")
        return [dict(row) for row in cur.fetchall()]


def _raise(h, table, ids):
    """The owner raises stored rows to confidential. The tables are a closed set."""

    assert table in {"generated_artifacts", "memories", "sources", "open_loops"}
    with h.store() as store:
        acquire_exclusive_label_lock(store)
        with store.conn.cursor() as cur:
            cur.execute(f"UPDATE {table} SET sensitivity = 'confidential' WHERE id = ANY(%s::uuid[])", (list(ids),))  # noqa: S608


def _raise_every_input(h):
    with h.store() as store, store.conn.cursor() as cur:
        found = {}
        for table in ("memories", "sources", "open_loops"):
            cur.execute(f"SELECT id::text AS id FROM {table}")  # noqa: S608  closed table names
            found[table] = [row["id"] for row in cur.fetchall()]
    for table, ids in found.items():
        _raise(h, table, ids)


def _traces(body, *needles):
    text = json.dumps(body, default=str)
    return [needle for needle in needles if needle and needle in text]


def _refused_answer(status, body, *, artifact_ids=(), needles=()):
    assert status == 201, body
    assert body["artifact"] is None, body
    assert body["run"]["status"] == "succeeded", body
    assert body["run"]["artifact_id"] is None, body
    assert "artifact_id" not in (body["run"].get("metadata_json") or {}), body
    assert _traces(body, *artifact_ids, *needles) == []


def _refused_or_scanned_again(h, key, workflow, status, body, *, artifact_id):
    """The answer to a run whose stored report the caller may no longer read.

    Most workflows find the report again under the run's digest and leave it out of the answer. The project update scan
    judges the stored candidate before it writes anything and, when the caller may not read it, makes the scan's own
    "no project matched" report instead, which the caller may read. Either way the answer names nothing of the report the
    caller may not read.
    """

    if workflow != "project_update_scan" or body.get("artifact") is None:
        _refused_answer(status, body, artifact_ids=[artifact_id])
        return
    assert status == 201, body
    made = body["artifact"]
    assert made["id"] != artifact_id and body["run"]["artifact_id"] == made["id"], body
    assert "No active project matched this scheduler scope." in made["content_markdown"], made
    assert _get_artifact(h, made["id"], key)[0] == 200
    assert _traces(body, artifact_id) == []


def _owner_harness(urls):
    """A vault with no key at all: a request with no key is the owner's only while the vault has no key."""

    owner_id = uuid4()
    harness = LabelHarness(urls, owner_id)
    with harness.store() as store:
        ContinuityStore(store.conn).create_user(owner_id, f"owner-{owner_id}@example.invalid", "Synthetic owner")
    return harness


# -- the reported case ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["project_scoped_agent", "trusted_local_agent"])
def test_a_cluster_that_quotes_a_confidential_memory_is_left_out_of_the_answer(label_harness, profile):
    h = label_harness
    project = _project(h)
    secret = _quoting_cluster(h, project)
    key = h.key(profile, project=project)

    status, body, _ = _run(h, "memory_consolidation", key, project)

    # The report was made and stored, carrying the quote, and it is above what this key may read.
    [stored] = _stored_artifacts(h, "memory_consolidation")
    assert QUOTE in json.dumps(stored["metadata_json"], default=str) and stored["sensitivity"] == "confidential"
    assert _get_artifact(h, stored["id"], key)[0] == 404
    # So the answer of the run does not carry it, nor the id it recorded, nor the quote, nor the memory quoted.
    _refused_answer(status, body, artifact_ids=[stored["id"]], needles=[QUOTE, str(secret["id"]), stored["title"]])
    # The stored run keeps the id for the owner.
    assert [run["artifact_id"] for run in _stored_runs(h)] == [stored["id"]]


@pytest.mark.parametrize("keyed", [True, False], ids=["unbound admin key", "owner"])
def test_the_owner_and_an_unbound_admin_key_are_answered_with_the_report_as_before(label_harness, migrated_database_urls, keyed):
    h = label_harness if keyed else _owner_harness(migrated_database_urls)
    key = h.key("admin_agent") if keyed else None
    project = _project(h)
    secret = _quoting_cluster(h, project)
    everything = ["public", "internal", "confidential"]

    status, body, _ = _run(h, "memory_consolidation", key, project, sensitivity_allowed=everything)

    assert status == 201, body
    [stored] = _stored_artifacts(h, "memory_consolidation")
    assert body["artifact"]["id"] == stored["id"] and body["run"]["artifact_id"] == stored["id"], body
    assert QUOTE in json.dumps(body["artifact"], default=str) and str(secret["id"]) in json.dumps(body["artifact"], default=str)
    assert _get_artifact(h, stored["id"], key)[0] == 200
    # A report found again by its digest is answered the same way, even after the owner raised it.
    _raise(h, "generated_artifacts", [stored["id"]])
    again_status, again, _ = _run(h, "memory_consolidation", key, project, sensitivity_allowed=everything)
    assert again_status == 201 and again["artifact"]["id"] == stored["id"], again
    assert again["run"]["artifact_id"] == stored["id"]


# -- a report found again under the digest of the run ---------------------------------------------------------------


@pytest.mark.parametrize("workflow", REPLAYED)
def test_a_report_found_again_that_the_caller_may_no_longer_read_is_left_out_of_the_answer(label_harness, workflow):
    h = label_harness
    project = _vault(h)
    key = h.key("trusted_local_agent", project=project)

    status, first, _ = _run(h, workflow, key, project)
    assert status == 201 and first["artifact"] is not None, first
    artifact_id = first["artifact"]["id"]
    assert _get_artifact(h, artifact_id, key)[0] == 200
    # The owner raises the report. A second run finds it again under the same digest.
    _raise(h, "generated_artifacts", [artifact_id])
    assert _get_artifact(h, artifact_id, key)[0] == 404

    status, again, _ = _run(h, workflow, key, project)

    _refused_or_scanned_again(h, key, workflow, status, again, artifact_id=artifact_id)
    assert artifact_id in {row["id"] for row in _stored_artifacts(h)}


@pytest.mark.parametrize("workflow", ["connection_report", "contradiction_report", "project_update_scan", "memory_consolidation"])
def test_the_same_holds_for_a_key_with_the_profile_of_a_project_agent(label_harness, workflow):
    h = label_harness
    project = _vault(h)
    key = h.key("project_scoped_agent", project=project)
    status, first, _ = _run(h, workflow, key, project)
    assert status == 201 and first["artifact"] is not None, first
    artifact_id = first["artifact"]["id"]
    _raise(h, "generated_artifacts", [artifact_id])
    status, again, _ = _run(h, workflow, key, project)
    _refused_or_scanned_again(h, key, workflow, status, again, artifact_id=artifact_id)


# -- a report made from rows that were raised while it was being made ---------------------------------------------


@pytest.mark.parametrize("workflow", EVERY_WORKFLOW)
def test_a_report_made_from_rows_raised_before_it_was_published_is_left_out_of_the_answer(label_harness, monkeypatch, workflow):
    h = label_harness
    project = _vault(h)
    key = h.key("trusted_local_agent", project=project)
    original = VNextSchedulerService.prepare_started_workflow

    def raced(self, request, *, run):
        plan = original(self, request, run=run)
        _raise_every_input(h)  # the owner raises what the report was made from, after it was read and before it is published
        return plan

    monkeypatch.setattr(VNextSchedulerService, "prepare_started_workflow", raced)

    status, body, _ = _run(h, workflow, key, project)

    [stored] = _stored_artifacts(h)
    assert _get_artifact(h, stored["id"], key)[0] == 404, (workflow, stored["sensitivity"])
    _refused_answer(status, body, artifact_ids=[stored["id"]], needles=[stored["title"]])


# -- a caller with limits still gets a report it may read ----------------------------------------------------------


@pytest.mark.parametrize("workflow", EVERY_WORKFLOW)
def test_a_key_with_limits_still_gets_the_report_it_may_read_and_not_the_rows_above_its_ceiling(label_harness, workflow):
    h = label_harness
    project = _vault(h)
    key = h.key("trusted_local_agent", project=project)

    status, body, _ = _run(h, workflow, key, project)

    assert status == 201 and body["run"]["status"] == "succeeded", body
    artifact = body["artifact"]
    assert artifact is not None and body["run"]["artifact_id"] == artifact["id"], body
    read_status, read, _ = _get_artifact(h, artifact["id"], key)
    assert read_status == 200 and read["id"] == artifact["id"]
    assert _traces(body, HIDDEN) == []


# -- a scan ---------------------------------------------------------------------------------------------------------


def _make_due(h, workflow):
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute(
            "UPDATE scheduler_workflows SET enabled = true, paused = false, next_run_at = now() - interval '1 minute' "
            "WHERE workflow_type = %s",
            (workflow,),
        )


def _run_due(h, key):
    return h.request("POST", "/v0/vnext/scheduler/run-due", payload={"limit": 10}, key=key)


def _scan_twice_with_the_report_raised(h, key, workflow):
    with h.store() as store:
        VNextSchedulerService(store).ensure_default_workflows()
    _make_due(h, workflow)
    status, first, _ = _run_due(h, key)
    assert status == 201 and first["runs"][0]["artifact"] is not None, first
    artifact_id = first["runs"][0]["artifact"]["id"]
    _raise(h, "generated_artifacts", [artifact_id])
    _make_due(h, workflow)
    status, again, _ = _run_due(h, key)
    assert status == 201 and again["due_count"] == 1, again
    return artifact_id, again


@pytest.mark.parametrize("workflow", ["daily_brief", "weekly_synthesis"])
def test_a_scan_leaves_out_a_report_found_again_that_the_caller_may_no_longer_read(label_harness, workflow):
    h = label_harness
    _vault(h)
    key = h.key("trusted_local_agent")

    artifact_id, again = _scan_twice_with_the_report_raised(h, key, workflow)

    [answered] = again["runs"]
    assert answered["artifact"] is None and answered["run"]["status"] == "succeeded", again
    assert answered["run"]["artifact_id"] is None and "artifact_id" not in (answered["run"].get("metadata_json") or {})
    assert _traces(again, artifact_id) == []
    assert _get_artifact(h, artifact_id, key)[0] == 404


def test_a_scan_answers_an_unbound_admin_key_with_the_report(label_harness):
    h = label_harness
    _vault(h)
    key = h.key("admin_agent")

    artifact_id, again = _scan_twice_with_the_report_raised(h, key, "daily_brief")

    [answered] = again["runs"]
    assert answered["artifact"]["id"] == artifact_id and answered["run"]["artifact_id"] == artifact_id


def test_a_scan_with_a_readable_report_answers_it(label_harness):
    h = label_harness
    _vault(h)
    key = h.key("trusted_local_agent")
    with h.store() as store:
        VNextSchedulerService(store).ensure_default_workflows()
    _make_due(h, "staleness_sweep")
    status, body, _ = _run_due(h, key)
    [answered] = body["runs"]
    assert status == 201 and answered["artifact"] is not None and answered["run"]["artifact_id"] == answered["artifact"]["id"]
    assert _get_artifact(h, answered["artifact"]["id"], key)[0] == 200


# -- the answer and the artifact read make the same decision --------------------------------------------------------


@pytest.mark.parametrize(
    "profile, bound",
    [
        ("project_scoped_agent", True), ("trusted_local_agent", True), ("trusted_local_agent", False),
        ("read_only_agent", False), ("read_only_agent", True), ("admin_agent", True), ("admin_agent", False),
    ],
)
def test_the_answer_and_a_later_read_of_the_same_report_make_the_same_decision(label_harness, profile, bound):
    """For every kind of key and every label a report can carry, the report is in the answer exactly when the artifact read
    would give it (HTTP 200), and is left out exactly when the read answers 404. The reports are stored directly, so the
    labels cover each side of each limit: sensitivity, domain, project scope, and an artifact made from a missing input."""

    from alicebot_api.vnext_agent_keys import resolve_agent_identity
    from alicebot_api.vnext_label_guard import readable_artifact

    h = label_harness
    mine, other = _project(h, "Mine"), _project(h, "Other")
    labels = [
        (domain, sensitivity, scope)
        for domain in ("project", "health")
        for sensitivity in ("public", "internal", "private", "confidential", "unknown")
        for scope in ([mine], [other], [], [mine, other])
    ]
    ids = []
    with h.store() as store:
        for index, (domain, sensitivity, scope) in enumerate(labels):
            row = store.create_artifact(
                {
                    "artifact_type": "daily_brief", "title": f"Report {index}", "content_markdown": f"# Report {index}",
                    "status": "needs_review", "domain": domain, "sensitivity": sensitivity, "generated_by": "scheduler",
                    "metadata_json": with_derived_from({"project_scope": scope}, {}),
                }
            )
            ids.append(str(row["id"]))
        # One report made from an input that is not stored: its label cannot be verified.
        orphan = store.create_artifact(
            {
                "artifact_type": "daily_brief", "title": "Orphan", "content_markdown": "# Orphan", "status": "needs_review",
                "domain": "project", "sensitivity": "public", "generated_by": "scheduler",
                "metadata_json": {"project_scope": [mine], "derived_from": {"v": 1, "memories": [str(uuid4())]}},
            }
        )
        ids.append(str(orphan["id"]))
    agent_id = f"grid-{uuid4().hex[:8]}"
    with h.store() as store:
        _record, raw = create_agent_key(
            store, user_id=h.user_id, agent_id=agent_id, permission_profile=profile, project_scope=mine if bound else None
        )
        identity = resolve_agent_identity(store, user_id=h.user_id, raw_key=raw, payload={})
    differing = []
    admitted = 0
    for artifact_id in ids:
        status, _body, _ = _get_artifact(h, artifact_id, raw)
        with h.store() as store:
            answered = readable_artifact(store, identity, {"id": artifact_id}) is not None
        admitted += answered
        if answered != (status == 200):
            differing.append((artifact_id, status, answered))
    assert differing == []
    # The grid reaches both sides for every key, except the one that reads everything.
    assert admitted > 0
    assert (admitted < len(ids)) == (not (profile == "admin_agent" and not bound))


def test_a_cluster_that_quotes_a_memory_the_key_may_read_is_answered_and_read_alike(label_harness):
    """The quote is not withheld from a key that may read the memory it quotes: the answer and the read carry the same
    report, quote included. The report's label covers the memories its refs name, so the rule is one rule."""

    h = label_harness
    project = _project(h)
    shared = _memory(h, project, sensitivity="internal", text=f"The merger is rumoured. {QUOTE}")
    ref = json.dumps({"memory_id": str(shared["id"]), "quote": QUOTE})
    for index in range(3):
        _memory(h, project, sensitivity="internal", text="The launch window moves to March after the review.",
                refs=(ref,) if index == 0 else (), vector=[0.5, 0.1, 0.2])
    key = h.key("project_scoped_agent", project=project)

    status, body, _ = _run(h, "memory_consolidation", key, project)

    assert status == 201 and body["artifact"] is not None, body
    read_status, read, _ = _get_artifact(h, body["artifact"]["id"], key)
    assert read_status == 200
    assert QUOTE in json.dumps(body["artifact"]["metadata_json"]) and QUOTE in json.dumps(read["metadata_json"])
    assert body["artifact"]["content_markdown"] == read["content_markdown"]
    assert body["artifact"]["metadata_json"]["source_refs"] == read["metadata_json"]["source_refs"]
    assert body["artifact"]["sensitivity"] == read["sensitivity"] == "internal"


def test_a_report_found_again_is_read_again_before_it_is_answered(label_harness, monkeypatch):
    """The plan of a run holds the report as it was read while the run was made, and a run can take seconds. The owner raises
    the report in that time, so the answer is judged on the stored row now, not on the row the plan holds."""

    h = label_harness
    project = _vault(h)
    key = h.key("trusted_local_agent", project=project)
    status, first, _ = _run(h, "connection_report", key, project)
    assert status == 201 and first["artifact"] is not None, first
    artifact_id = first["artifact"]["id"]
    original = VNextSchedulerService.prepare_started_workflow

    def raised_late(self, request, *, run):
        plan = original(self, request, run=run)
        assert str(plan.artifact["id"]) == artifact_id and plan.artifact["sensitivity"] != "confidential"
        _raise(h, "generated_artifacts", [artifact_id])  # after the plan read the report, before it is answered
        return plan

    monkeypatch.setattr(VNextSchedulerService, "prepare_started_workflow", raised_late)

    status, again, _ = _run(h, "connection_report", key, project)

    _refused_answer(status, again, artifact_ids=[artifact_id])
