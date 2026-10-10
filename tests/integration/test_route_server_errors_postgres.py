"""Two operator routes that answered HTTP 500 on PostgreSQL, each asked by the owner, an admin key and a trusted key.

``POST /v0/vnext/projects`` with a slug in use failed on the unique constraint of the slug and nothing handled it. It
now answers HTTP 409, with the same words for every caller and for every holder of the slug, so the answer names no
project and a key with a ceiling is told no more than the owner is.

``POST /v0/vnext/queue/process-next`` failed for every caller because its claim statement named ``id`` twice and
PostgreSQL refused it as ambiguous. It now claims, processes and goes idle, and a key with a ceiling claims only a task
it may read.
"""
from __future__ import annotations

import json
import threading
from collections import Counter

import psycopg
import pytest

from alicebot_api.vnext_store import PROJECT_SLUG_CONSTRAINT, ProjectSlugConflictError
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.operator_route_probes import Call
from tests.integration.operator_route_runner import run_call
from tests.integration.operator_route_vault import Vault

CONFLICT = {"detail": "vNext project slug is already in use"}
PROJECTS = "/v0/vnext/projects"
PROCESS_NEXT = "/v0/vnext/queue/process-next"
TASKS = "/v0/vnext/queue/tasks"

# What an unbound trusted_local_agent key may read: these sensitivities, in every domain.
TRUSTED_LEVELS = {"public", "internal", "private", "unknown"}
ALL_LEVELS = ["public", "internal", "private", "confidential", "highly_sensitive", "sacred", "regulated", "unknown"]


# -- project slug conflicts ---------------------------------------------------------------------------------------------


def _row_counts(harness) -> dict[str, int]:
    """The number of rows in every table, to show a refused call wrote nothing."""

    with harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
        names = sorted(row["table_name"] for row in cur.fetchall())
        counts = {}
        for name in names:
            cur.execute(f"SELECT count(*) AS n FROM {name}")  # closed list of table names read from the catalog
            counts[name] = cur.fetchone()["n"]
        return counts


def _create_project(vault: Vault, key, **body):
    payload = {"name": "a project", "domain": "project", "sensitivity": "public", **body}
    return run_call(vault, "POST", PROJECTS, Call("ad hoc", body=payload), key)


@pytest.mark.parametrize("caller", ["owner", "admin", "trusted"])
def test_a_project_slug_in_use_answers_409_to_every_caller_whoever_holds_it(label_harness, caller):
    vault = Vault(label_harness, f"c{caller[:2]}", owner=caller == "owner").build()
    key = None if caller == "owner" else vault.keys[caller]
    # One project the trusted key may not read and one it may read.
    hidden_slug = f"{vault.tag}-dedupe-project".lower()
    shown_slug = f"{vault.tag}-project_shown".lower()
    # A first call by the key, so what it records on first use is not counted against the refusals below.
    assert run_call(vault, "GET", PROJECTS, Call("ad hoc"), key)[0] == 200
    before = _row_counts(label_harness)

    answers = []
    for slug in (hidden_slug, shown_slug):
        status, text = _create_project(vault, key, name="again", slug=slug)
        assert status == 409, (slug, status, text[:300])
        answers.append(text)
    # The same words for both, and no row, name, id or label of the project that has the slug.
    assert json.loads(answers[0]) == CONFLICT
    assert answers[0] == answers[1]
    for name in ("dedupe_project-name", "dedupe_project-description", "project_hidden-name", "project_shown-name"):
        assert vault.text(name) not in answers[0], name
    for name in ("dedupe_project", "project_hidden", "project_shown"):
        assert vault.ids[name] not in answers[0], name
    assert _row_counts(label_harness) == before

    # A slug in use is the only refusal: a free slug still creates a project.
    status, text = _create_project(vault, key, name="fresh", slug=f"{vault.tag}-free-slug".lower())
    assert status == 201, text[:300]
    assert json.loads(text)["project"]["slug"] == f"{vault.tag}-free-slug".lower()


def test_a_slug_made_from_the_name_conflicts_in_the_same_way(label_harness):
    vault = Vault(label_harness, "dn", owner=True).build()
    status, text = _create_project(vault, None, name="Alpha Beta")
    assert status == 201 and json.loads(text)["project"]["slug"] == "alpha-beta"
    status, text = _create_project(vault, None, name="alpha   BETA!")
    assert status == 409 and json.loads(text) == CONFLICT
    status, text = _create_project(vault, None, name="Another", slug="alpha-beta")
    assert status == 409 and json.loads(text) == CONFLICT


def test_a_slug_in_use_is_answered_alike_for_an_archived_project(label_harness):
    vault = Vault(label_harness, "ar", owner=True).build()
    status, text = _create_project(vault, None, name="Old", slug="old-project", status="archived")
    assert status == 201, text[:300]
    status, text = _create_project(vault, None, name="New", slug="old-project")
    assert status == 409 and json.loads(text) == CONFLICT


def test_concurrent_creates_of_one_slug_make_one_project_and_refuse_the_rest(label_harness):
    callers = 6
    results: list[tuple[int, dict]] = []
    failures: list[BaseException] = []
    barrier = threading.Barrier(callers)

    def create(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            status, body, _headers = label_harness.request(
                "POST", PROJECTS, payload={"name": f"racer {index}", "slug": "one-slug", "domain": "project", "sensitivity": "public"}
            )
            results.append((status, body))
        except BaseException as exc:  # reported below
            failures.append(exc)

    threads = [threading.Thread(target=create, args=(index,)) for index in range(callers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not failures, failures
    assert Counter(status for status, _body in results) == Counter({201: 1, 409: callers - 1})
    assert all(body == CONFLICT for status, body in results if status == 409)
    with label_harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM projects WHERE slug = 'one-slug'")
        assert cur.fetchone()["n"] == 1


def test_the_slug_constraint_is_the_one_the_store_reads(label_harness):
    with label_harness.store() as store, store.conn.cursor() as cur:
        cur.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'projects'::regclass AND contype = 'u' "
            "AND pg_get_constraintdef(oid) = 'UNIQUE (user_id, slug)'"
        )
        assert [row["conname"] for row in cur.fetchall()] == [PROJECT_SLUG_CONSTRAINT]


def test_a_unique_violation_on_another_constraint_is_not_read_as_a_slug_conflict(label_harness):
    with label_harness.store() as store:
        first = store.create_project({"name": "first", "slug": "first", "domain": "project", "sensitivity": "public"})
    with pytest.raises(psycopg.errors.UniqueViolation) as caught:
        with label_harness.store() as store:
            # The same id under a new slug breaks the primary key and not the slug.
            store.create_project(
                {"id": str(first["id"]), "name": "second", "slug": "second", "domain": "project", "sensitivity": "public"}
            )
    assert not isinstance(caught.value, ProjectSlugConflictError)
    # The id is the primary key and also half of a second unique key; either is a violation that is not the slug.
    assert caught.value.diag.constraint_name in {"projects_pkey", "projects_id_user_id_key"}
    assert caught.value.diag.constraint_name != PROJECT_SLUG_CONSTRAINT
    with pytest.raises(ProjectSlugConflictError):
        with label_harness.store() as store:
            store.create_project({"name": "third", "slug": "first", "domain": "project", "sensitivity": "public"})


# -- process-next -----------------------------------------------------------------------------------------------------


def _enqueue(harness, key, title: str, sensitivity: str, *, domain: str = "project") -> str:
    status, body, _headers = harness.request(
        "POST",
        TASKS,
        key=key,
        payload={"title": title, "task_type": "summarize", "instructions": f"do {title}", "domain": domain, "sensitivity": sensitivity},
    )
    assert status == 201, (status, str(body)[:300])
    return str(body["id"])


def _process(harness, key):
    status, body, _headers = harness.request("POST", PROCESS_NEXT, payload={}, key=key)
    return status, body


def _task_rows(harness) -> dict[str, dict]:
    with harness.store() as store, store.conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, status, started_at, completed_at, output_artifact_id, domain, sensitivity, updated_at "
            "FROM task_queue ORDER BY created_at, id"
        )
        return {str(row["id"]): row for row in cur.fetchall()}


def _artifact(harness, artifact_id: str) -> dict:
    with harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id, title, domain, sensitivity, status, generated_by FROM generated_artifacts WHERE id = %s::uuid", (artifact_id,))
        return cur.fetchone()


def _event_types(harness) -> Counter:
    with harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT event_type FROM event_log")
        return Counter(row["event_type"] for row in cur.fetchall())


IDLE = {"status": "idle", "task_id": None, "artifact_id": None, "error_code": None, "error_message": None}


@pytest.mark.parametrize("caller", ["owner", "admin"])
def test_process_next_claims_processes_and_goes_idle_for_a_caller_with_no_limits(label_harness, caller):
    key = None if caller == "owner" else label_harness.key("admin_agent")
    harness = label_harness
    # An empty queue is idle, and says so as an event.
    assert _process(harness, key) == (200, IDLE)
    assert _event_types(harness)["queue.worker_idle"] == 1

    # The oldest task first, whatever its label: the first is confidential and the second is public.
    first = _enqueue(harness, key, "first task", "confidential")
    second = _enqueue(harness, key, "second task", "public")
    status, body = _process(harness, key)
    assert status == 200 and body["status"] == "completed" and body["task_id"] == first
    assert body["error_code"] is None and body["error_message"] is None
    rows = _task_rows(harness)
    assert rows[first]["status"] == "completed" and str(rows[first]["output_artifact_id"]) == body["artifact_id"]
    assert rows[first]["started_at"] is not None and rows[first]["completed_at"] is not None
    assert rows[second]["status"] == "pending"
    artifact = _artifact(harness, body["artifact_id"])
    assert artifact["title"] == "first task" and artifact["sensitivity"] == "confidential"
    assert artifact["generated_by"] == "vnext_queue_worker"

    status, body = _process(harness, key)
    assert status == 200 and body["status"] == "completed" and body["task_id"] == second
    assert _process(harness, key) == (200, IDLE)
    events = _event_types(harness)
    assert events["task.claimed"] == 2 and events["queue.task_completed"] == 2 and events["queue.worker_idle"] == 2


def _events_naming(harness, row_id: str) -> list[dict]:
    with harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id, event_type FROM event_log WHERE target_id = %s ORDER BY id", (row_id,))
        return [{**row, "id": str(row["id"])} for row in cur.fetchall()]


def test_process_next_claims_for_a_key_with_a_ceiling_only_the_tasks_it_may_read(label_harness):
    harness = label_harness
    admin, trusted = harness.key("admin_agent"), harness.key("trusted_local_agent")
    hidden = _enqueue(harness, admin, "older hidden task", "confidential")
    shown = _enqueue(harness, admin, "newer shown task", "public")
    before = _task_rows(harness)
    events_before = _events_naming(harness, hidden)

    # The oldest task is above the ceiling, so the key is given the newer one it may read.
    status, body = _process(harness, trusted)
    assert status == 200 and body["status"] == "completed" and body["task_id"] == shown
    assert _artifact(harness, body["artifact_id"])["sensitivity"] == "public"
    answers = [body]
    # Nothing else is pending that it may read: it is told the queue is idle, as an empty queue is.
    for _ in range(2):
        status, body = _process(harness, trusted)
        assert (status, body) == (200, IDLE)
        answers.append(body)

    # The task above the ceiling was neither claimed nor changed, and no event names it. No answer names it either.
    rows = _task_rows(harness)
    assert rows[hidden] == before[hidden]
    assert rows[hidden]["status"] == "pending" and rows[hidden]["started_at"] is None
    assert _events_naming(harness, hidden) == events_before
    assert hidden not in json.dumps(answers)

    # The admin key still takes it, so the other key left it unclaimed and unlocked.
    status, body = _process(harness, admin)
    assert status == 200 and body["status"] == "completed" and body["task_id"] == hidden
    assert _artifact(harness, body["artifact_id"])["sensitivity"] == "confidential"
    assert _process(harness, admin) == (200, IDLE)


def test_process_next_for_a_key_with_a_ceiling_and_only_hidden_tasks_is_idle(label_harness):
    harness = label_harness
    admin, trusted = harness.key("admin_agent"), harness.key("trusted_local_agent")
    hidden = [_enqueue(harness, admin, f"hidden {level}", level) for level in ("confidential", "highly_sensitive", "sacred", "regulated")]
    before = _task_rows(harness)
    for _ in range(3):
        assert _process(harness, trusted) == (200, IDLE)
    assert _task_rows(harness) == before
    assert all(before[task_id]["status"] == "pending" for task_id in hidden)


def test_process_next_follows_the_labels_the_workspace_lists_to_the_same_key(label_harness):
    harness = label_harness
    admin, trusted = harness.key("admin_agent"), harness.key("trusted_local_agent")
    # Ten tasks, so the workspace list (the twelve newest) holds every one of them.
    labels = [(level, "project") for level in ALL_LEVELS] + [("private", "health"), ("confidential", "health")]
    titles = {}
    for level, domain in labels:
        title = f"{domain} {level}"
        titles[_enqueue(harness, admin, title, level, domain=domain)] = title
    status, listed, _headers = harness.request("GET", "/v0/vnext/workspace", key=trusted)
    assert status == 200
    listed_titles = {task["title"] for task in listed["tasks"]}
    claimed_titles = []
    while True:
        status, body = _process(harness, trusted)
        assert status == 200
        if body["status"] == "idle":
            break
        claimed_titles.append(titles[body["task_id"]])
    # Claimed in the order they were enqueued, and exactly the ones the key is shown.
    readable = [title for title in titles.values() if title.split()[1] in TRUSTED_LEVELS]
    assert claimed_titles == readable
    assert set(claimed_titles) == listed_titles
    rows = _task_rows(harness)
    still_pending = {row["title"] for row in rows.values() if row["status"] == "pending"}
    assert still_pending == set(titles.values()) - set(readable)


@pytest.mark.parametrize(
    "profile, bound",
    [
        ("read_only_agent", False),
        ("memory_proposal_agent", False),
        ("project_scoped_agent", True),
        ("trusted_local_agent", True),
        ("admin_agent", True),
    ],
)
def test_process_next_refuses_a_key_the_operator_gate_refuses_and_claims_nothing(label_harness, profile, bound):
    harness = label_harness
    admin = harness.key("admin_agent")
    _enqueue(harness, admin, "public task", "public")
    project = None
    if bound:
        with harness.store() as store:
            project = str(store.create_project({"name": "p", "slug": "p", "domain": "project", "sensitivity": "public"})["id"])
    key = harness.key(profile, project=project)
    before = _task_rows(harness)
    status, body = _process(harness, key)
    assert status == 403, (status, body)
    assert _task_rows(harness) == before


def test_a_key_locked_to_a_project_that_reaches_the_handler_claims_nothing(label_harness):
    from alicebot_api.routers import vnext_review

    harness = label_harness
    admin = harness.key("admin_agent")
    task = _enqueue(harness, admin, "public task", "public")
    with harness.store() as store:
        project = str(store.create_project({"name": "p", "slug": "p", "domain": "project", "sensitivity": "public"})["id"])
    bound = harness.key("trusted_local_agent", project=project)
    before = _task_rows(harness)
    # The central gate refuses this key first. This is the handler alone, as a call that bypassed the gate would be.
    answer = vnext_review.process_next_vnext_queue_task(
        vnext_review.VNextQueueProcessNextRequest(user_id=harness.user_id), authorization=f"Bearer {bound}"
    )
    assert answer.status_code == 200 and json.loads(answer.body) == IDLE
    assert _task_rows(harness) == before
    assert before[task]["status"] == "pending"


def test_two_workers_claim_two_different_tasks_and_a_third_finds_none(label_harness):
    harness = label_harness
    admin = harness.key("admin_agent")
    first = _enqueue(harness, admin, "one", "public")
    second = _enqueue(harness, admin, "two", "public")
    with harness.store() as store_a:
        claimed_a = store_a.claim_next_task()
        with harness.store() as store_b:
            claimed_b = store_b.claim_next_task()
            # The task the first worker holds is skipped, not waited for, and nothing else is pending.
            assert store_b.claim_next_task() is None
    assert [str(claimed_a["id"]), str(claimed_b["id"])] == [first, second]
    assert {row["status"] for row in _task_rows(harness).values()} == {"running"}


def test_a_task_scheduled_for_later_is_left_and_does_not_hold_up_the_queue(label_harness):
    harness = label_harness
    admin = harness.key("admin_agent")
    with harness.store() as store:
        later = store.create_task(
            {
                "title": "later",
                "task_type": "summarize",
                "instructions": "later",
                "domain": "project",
                "sensitivity": "public",
                "scheduled_for": "2999-01-01T00:00:00+00:00",
            }
        )
    now = _enqueue(harness, admin, "now", "public")
    status, body = _process(harness, admin)
    assert status == 200 and body["task_id"] == now
    assert _process(harness, admin) == (200, IDLE)
    assert _task_rows(harness)[str(later["id"])]["status"] == "pending"


def test_the_command_line_claim_works_through_the_same_service(label_harness):
    from alicebot_api.vnext_queue import VNextQueueService

    harness = label_harness
    with harness.store() as store:
        assert VNextQueueService(store).process_next_task().to_record() == IDLE
    with harness.store() as store:
        store.create_task({"title": "cli task", "task_type": "draft", "instructions": "write", "domain": "project", "sensitivity": "private"})
    with harness.store() as store:
        record = VNextQueueService(store).process_next_task().to_record()
    assert record["status"] == "completed" and record["task_id"] is not None
    assert _artifact(harness, record["artifact_id"])["title"] == "cli task"
