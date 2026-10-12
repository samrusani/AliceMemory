"""A loop is filed only under a project its caller may read; a project it may not read answers as a project that is not stored.

``POST /v0/vnext/open-loops`` took the ``project_id`` of the request, loaded the project and held it to the key's project
binding only. An unbound ``trusted_local_agent`` key (ceiling ``public``, ``internal``, ``private`` and ``unknown``) that
named a confidential project was answered HTTP 201 with a loop filed under it, while a project that does not exist was
answered HTTP 404, so the key learned that the hidden project exists and could file work under it.

``POST /v0/vnext/open-loops/extract`` filed the loops it made under the project named in ``options.project_id`` the same
way, and a project that does not exist but is named in the scope of a source made the insert fail on the foreign key
(a server error), so the two answers also differed.

Both doors now load the project and judge it with the check that the project update candidate uses
(``project_readable``): the domain and sensitivity it has now, raised to its inputs, and the key's project binding. A
project the caller may not read is answered exactly as a project that is not stored: the create route answers HTTP 404
with ``{"detail": "vNext project was not found"}`` and files nothing, the extraction answers HTTP 201 with no loop. The
owner and an unbound admin key are answered as before.

The other doors that put a row under a project by id keep a free-form name in a scope and look no project up, so a project
the caller may not read and a project that is not stored are already answered alike. A test pins that for the source
and the memory assignment.

Mutations, each one alone (replayed by ``scripts/verify_derived_label_mutations.py``): leave the readability test out of
the create route; leave the project out of the extraction; let the extraction treat every named project as readable.
"""
from __future__ import annotations

import json
import re
from uuid import uuid4

import pytest

from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_label_writes import acquire_exclusive_label_lock
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)

CREATE = "/v0/vnext/open-loops"
EXTRACT = "/v0/vnext/open-loops/extract"
NOT_FOUND = (404, {"detail": "vNext project was not found"})
NO_LOOPS = (201, {"open_loops": [], "created_count": 0})
UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _project(harness, *, sensitivity: str = "public", domain: str = "project", name: str = "Quarterly Kiln Plans") -> dict:
    with harness.store() as store:
        return store.create_project(
            {
                "name": name,
                "slug": f"kiln-{uuid4().hex[:10]}",
                "description": "d",
                "current_state": "s",
                "domain": domain,
                "sensitivity": sensitivity,
            }
        )


def _raise_project_by_its_inputs(harness, project) -> None:
    """The project row stays public and records a confidential source as an input, so only its settled label is raised."""

    secret = harness.source(scope=(str(project["id"]),), text="Raised notes.", sensitivity="confidential")
    with harness.store() as store:
        acquire_exclusive_label_lock(store)
        with store.conn.cursor() as cur:
            cur.execute(
                "UPDATE projects SET metadata_json = %s::jsonb WHERE id = %s",
                (json.dumps(with_derived_from({}, {"sources": [secret]})), str(project["id"])),
            )
            assert cur.rowcount == 1
        assert store.get_project(str(project["id"]))["sensitivity"] == "public"


def _changed(before: dict, after: dict) -> set[str]:
    return {table for table in after if after[table] != before[table]}


def _create(harness, key, project_id) -> tuple[int, dict]:
    status, body, _headers = harness.request("POST", CREATE, payload={"title": "File the permit", "project_id": project_id}, key=key)
    return status, body


def _source(harness, project_id: str, *, todo: str = "renew the kiln permit"):
    with harness.store() as store:
        row = store.create_source(
            {
                "source_type": "note",
                "title": f"Planning notes {uuid4()}",
                "content_hash": str(uuid4()),
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"project_scope": [project_id], "raw_text": f"Planning notes.\nTODO: {todo}"},
            }
        )
        store.create_source_chunk({"source_id": str(row["id"]), "chunk_index": 0, "text": f"TODO: {todo}"})
    return row


def _extract(harness, key, project_id) -> tuple[int, dict]:
    options = {"project_id": project_id, "sensitivity_allowed": ["public", "internal", "private", "unknown"], "max_items": 50}
    status, body, _headers = harness.request("POST", EXTRACT, payload={"scope": {}, "options": options}, key=key)
    return status, body


def _loop_rows(harness) -> list[dict]:
    with harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id, title, project_id FROM open_loops ORDER BY created_at, id")
        return [{**row, "id": str(row["id"]), "project_id": str(row["project_id"]) if row["project_id"] else None} for row in cur.fetchall()]


def _extraction_events(harness) -> list[dict]:
    with harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT payload_json FROM event_log WHERE event_type = 'open_loop.extraction_completed' ORDER BY occurred_at, id")
        return [dict(row["payload_json"]) for row in cur.fetchall()]


# (profile, the key is bound to the project, sensitivity, domain)
HIDDEN = [
    pytest.param("trusted_local_agent", False, "confidential", "project", id="unbound trusted key, confidential project"),
    pytest.param("trusted_local_agent", False, "highly_sensitive", "project", id="unbound trusted key, highly sensitive"),
    pytest.param("trusted_local_agent", False, "regulated", "project", id="unbound trusted key, regulated"),
    pytest.param("trusted_local_agent", True, "confidential", "project", id="bound trusted key, confidential project"),
    pytest.param("project_scoped_agent", True, "confidential", "project", id="bound project key, confidential project"),
    pytest.param("project_scoped_agent", True, "public", "health", id="bound project key, health domain"),
    pytest.param("project_scoped_agent", True, "public", "legal", id="bound project key, legal domain"),
]


def _key(harness, profile: str, project, bound: bool):
    return harness.key(profile, project=str(project["id"]) if bound else None)


# -- the create route --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("profile", "bound", "sensitivity", "domain"), HIDDEN)
def test_a_loop_is_not_filed_under_a_project_the_key_may_not_read(label_harness, profile, bound, sensitivity, domain):
    project = _project(label_harness, sensitivity=sensitivity, domain=domain)
    key = _key(label_harness, profile, project, bound)
    # The same key, naming a project that is not stored: the answer, and what the call writes.
    before = label_harness.snapshot()
    assert _create(label_harness, key, str(uuid4())) == NOT_FOUND
    written_for_a_missing_project = _changed(before, label_harness.snapshot())
    assert "open_loops" not in written_for_a_missing_project

    before = label_harness.snapshot()
    answer = _create(label_harness, key, str(project["id"]))

    assert answer == NOT_FOUND, answer
    assert str(project["id"]) not in json.dumps(answer[1])
    assert "open_loops" not in _changed(before, label_harness.snapshot())
    assert _changed(before, label_harness.snapshot()) == written_for_a_missing_project
    assert _loop_rows(label_harness) == []


def test_every_spelling_of_the_id_of_a_hidden_project_is_a_missing_project(label_harness):
    project = _project(label_harness, sensitivity="confidential")
    key = label_harness.key("trusted_local_agent")
    pid = str(project["id"])
    for spelling in (pid, pid.upper(), f" {pid} ", pid.replace("-", ""), f"urn:uuid:{pid}", f"{{{pid}}}"):
        assert _create(label_harness, key, spelling) == NOT_FOUND, spelling
    assert _loop_rows(label_harness) == []


@pytest.mark.parametrize(
    ("profile", "bound", "sensitivity", "domain"),
    [
        pytest.param("trusted_local_agent", False, "private", "project", id="unbound trusted key, private project"),
        pytest.param("trusted_local_agent", False, "private", "health", id="unbound trusted key, health domain"),
        pytest.param("trusted_local_agent", True, "public", "project", id="bound trusted key, public project"),
        pytest.param("project_scoped_agent", True, "internal", "professional", id="bound project key, professional domain"),
    ],
)
def test_a_loop_is_still_filed_under_a_project_the_key_may_read(label_harness, profile, bound, sensitivity, domain):
    project = _project(label_harness, sensitivity=sensitivity, domain=domain)
    key = _key(label_harness, profile, project, bound)
    status, body = _create(label_harness, key, str(project["id"]))
    assert status == 201, body
    assert body["open_loop"]["project_id"] == str(project["id"])
    assert [row["project_id"] for row in _loop_rows(label_harness)] == [str(project["id"])]


@pytest.mark.parametrize("admin", [True, False], ids=["unbound admin key", "owner"])
def test_the_owner_and_an_unbound_admin_key_file_a_loop_under_a_confidential_project(label_harness, admin):
    """A request with no key is the owner's only while the vault has no key, so the owner case mints none."""

    project = _project(label_harness, sensitivity="highly_sensitive", domain="health")
    key = label_harness.key("admin_agent") if admin else None
    status, body = _create(label_harness, key, str(project["id"]))
    assert status == 201, body
    assert body["open_loop"]["project_id"] == str(project["id"])
    assert _create(label_harness, key, str(uuid4())) == NOT_FOUND


def test_a_project_raised_by_its_inputs_is_judged_at_the_label_it_has_now(label_harness):
    """The project row is stored public. Once it records a confidential source as an input its settled label is
    confidential, and a key whose ceiling is below that may not file a loop under it."""

    project = _project(label_harness)
    _raise_project_by_its_inputs(label_harness, project)
    assert _create(label_harness, label_harness.key("trusted_local_agent"), str(project["id"])) == NOT_FOUND
    status, body = _create(label_harness, label_harness.key("admin_agent"), str(project["id"]))
    assert status == 201 and body["open_loop"]["project_id"] == str(project["id"])


def test_a_hidden_project_does_not_hide_the_project_beside_it(label_harness):
    hidden = _project(label_harness, sensitivity="confidential", name="Hidden Kiln")
    shown = _project(label_harness, name="Shown Kiln")
    key = label_harness.key("trusted_local_agent")
    assert _create(label_harness, key, str(hidden["id"])) == NOT_FOUND
    status, body = _create(label_harness, key, str(shown["id"]))
    assert status == 201 and body["open_loop"]["project_id"] == str(shown["id"])


BOUND_ADMIN_KINDS = [
    pytest.param({"sensitivity": "confidential"}, id="confidential"),
    pytest.param({"sensitivity": "highly_sensitive"}, id="highly sensitive"),
    pytest.param({"sensitivity": "regulated"}, id="regulated"),
    pytest.param({"domain": "health"}, id="health domain"),
    pytest.param({"domain": "legal"}, id="legal domain"),
    pytest.param({"raised": True}, id="raised by an input"),
]


@pytest.mark.parametrize("labels", BOUND_ADMIN_KINDS)
def test_a_bound_admin_key_files_a_loop_under_its_own_project(label_harness, labels):
    """An admin key bound to one project is allowed every sensitivity and every domain inside it, so the project it is
    bound to is readable to it whatever its label, and the artifact get shows it the reports of that project."""

    labels = dict(labels)
    raised = labels.pop("raised", False)
    project = _project(label_harness, **labels)
    if raised:
        _raise_project_by_its_inputs(label_harness, project)
    key = _key(label_harness, "admin_agent", project, True)
    status, body = _create(label_harness, key, str(project["id"]))
    assert status == 201, body
    assert body["open_loop"]["project_id"] == str(project["id"])
    # Another project is outside its binding and answers as a project that is not stored.
    other = _project(label_harness, name="Other Kiln")
    assert _create(label_harness, key, str(other["id"])) == NOT_FOUND


def test_a_bound_admin_key_reaches_the_extraction_no_more_than_it_did(label_harness):
    """The extraction is an operator route: a key bound to a project is refused before any project is read."""

    project = _project(label_harness, sensitivity="confidential")
    _source(label_harness, str(project["id"]))
    key = _key(label_harness, "admin_agent", project, True)
    status, body = _extract(label_harness, key, str(project["id"]))
    assert status == 403, body
    assert _loop_rows(label_harness) == []


# -- the extraction ----------------------------------------------------------------------------------------------------


def test_extraction_files_no_loop_under_a_project_the_key_may_not_read(label_harness):
    """The source is one the key may read and it names the hidden project in its scope. The loop it holds would be filed
    under the project, so the extraction judges the project first and answers as it answers a project that is not stored,
    which also names a source in its scope."""

    hidden = _project(label_harness, sensitivity="confidential")
    ghost = str(uuid4())
    _source(label_harness, str(hidden["id"]))
    _source(label_harness, ghost)
    key = label_harness.key("trusted_local_agent")

    before = label_harness.snapshot()
    missing = _extract(label_harness, key, ghost)
    written_for_a_missing_project = _changed(before, label_harness.snapshot())
    assert missing == NO_LOOPS, missing

    before = label_harness.snapshot()
    answer = _extract(label_harness, key, str(hidden["id"]))

    assert answer == NO_LOOPS, answer
    assert _changed(before, label_harness.snapshot()) == written_for_a_missing_project
    assert _loop_rows(label_harness) == []
    # The completion events of the two calls record the same: no loop and no source.
    assert _extraction_events(label_harness) == [{"created_count": 0, "source_ids": []}] * 2


def test_extraction_still_files_loops_under_a_project_the_key_may_read(label_harness):
    shown = _project(label_harness, sensitivity="private")
    _source(label_harness, str(shown["id"]))
    key = label_harness.key("trusted_local_agent")
    status, body = _extract(label_harness, key, str(shown["id"]))
    assert status == 201 and body["created_count"] == 1, body
    assert [loop["project_id"] for loop in body["open_loops"]] == [str(shown["id"])]
    assert [row["project_id"] for row in _loop_rows(label_harness)] == [str(shown["id"])]


@pytest.mark.parametrize("admin", [True, False], ids=["unbound admin key", "owner"])
def test_extraction_files_loops_under_a_confidential_project_for_the_owner_and_an_unbound_admin_key(label_harness, admin):
    hidden = _project(label_harness, sensitivity="confidential")
    _source(label_harness, str(hidden["id"]))
    key = label_harness.key("admin_agent") if admin else None
    status, body = _extract(label_harness, key, str(hidden["id"]))
    assert status == 201 and body["created_count"] == 1, body
    assert [loop["project_id"] for loop in body["open_loops"]] == [str(hidden["id"])]


@pytest.mark.parametrize("admin", [True, False], ids=["unbound admin key", "owner"])
def test_a_project_that_is_not_stored_gives_no_loop_and_no_database_error(label_harness, admin):
    """A source can name any id in its scope. The insert of a loop under an id that is no project failed on the foreign
    key, a server error that told a caller the id was no project."""

    ghost = str(uuid4())
    _source(label_harness, ghost)
    _source(label_harness, "not-a-uuid")
    key = label_harness.key("admin_agent") if admin else None
    assert _extract(label_harness, key, ghost) == NO_LOOPS
    assert _extract(label_harness, key, "not-a-uuid") == NO_LOOPS
    assert _loop_rows(label_harness) == []


# -- the project a source names in its own scope ----------------------------------------------------------------------


def _capture(harness, key, project_scope: str) -> str:
    """Capture a source through the route, as a caller does, with one project in its scope."""

    status, body, _headers = harness.request(
        "POST",
        "/v0/vnext/sources",
        payload={
            "raw_text": "Planning notes.\nTODO: renew the kiln permit",
            "title": "Planning notes",
            "domain": "project",
            "sensitivity": "public",
            "project_scope": [project_scope],
        },
        key=key,
    )
    assert status == 201, body
    return str(body["source_id"])


def _extract_everything(harness, key) -> tuple[int, dict]:
    options = {"sensitivity_allowed": ["public", "internal", "private", "unknown"], "max_items": 50}
    status, body, _headers = harness.request("POST", EXTRACT, payload={"scope": {}, "options": options}, key=key)
    return status, body


# What a source can name in its scope that no loop may be filed under for the caller: a hidden project, a project that is
# not stored, a free-form name and an id that is not well formed.
UNFILED = [
    pytest.param("hidden", id="a project the key may not read"),
    pytest.param("missing", id="a project that is not stored"),
    pytest.param("free-form", id="a free-form name"),
    pytest.param("malformed", id="an id that is not well formed"),
]


def _scope_for(harness, which: str) -> str:
    return {
        "hidden": lambda: str(_project(harness, sensitivity="confidential")["id"]),
        "missing": lambda: str(uuid4()),
        "free-form": lambda: "Kiln Plans",
        "malformed": lambda: "not-a-uuid",
    }[which]()


@pytest.mark.parametrize("which", UNFILED)
def test_extraction_files_no_loop_under_a_project_only_a_source_scope_names(label_harness, which):
    """A source the key captured names one project in its scope. With no project in the request, a loop made from it is
    filed under that project, so the project is judged like a named one: a hidden project, a project that is not stored,
    a free-form name and a malformed id all file nothing, answer alike and raise nothing."""

    key = label_harness.key("trusted_local_agent")
    _capture(label_harness, key, _scope_for(label_harness, which))
    before = label_harness.snapshot()

    answer = _extract_everything(label_harness, key)

    assert answer == NO_LOOPS, answer
    assert _loop_rows(label_harness) == []
    assert "open_loops" not in _changed(before, label_harness.snapshot())
    assert [event["created_count"] for event in _extraction_events(label_harness)] == [0]


def test_extraction_answers_a_hidden_and_a_missing_project_named_in_a_source_scope_alike(label_harness):
    key = label_harness.key("trusted_local_agent")
    answers, written = [], []
    for which in ("hidden", "missing"):
        _capture(label_harness, key, _scope_for(label_harness, which))
        before = label_harness.snapshot()
        answers.append(_extract_everything(label_harness, key))
        written.append(_changed(before, label_harness.snapshot()))
    assert answers[0] == answers[1] == NO_LOOPS
    assert written[0] == written[1]


@pytest.mark.parametrize("which", ["missing", "free-form", "malformed"])
@pytest.mark.parametrize("admin", [True, False], ids=["unbound admin key", "owner"])
def test_extraction_files_no_loop_and_raises_nothing_for_a_scope_that_names_no_stored_project(label_harness, which, admin):
    key = label_harness.key("admin_agent") if admin else None
    _capture(label_harness, key, _scope_for(label_harness, which))
    assert _extract_everything(label_harness, key) == NO_LOOPS
    assert _loop_rows(label_harness) == []


@pytest.mark.parametrize("admin", [True, False], ids=["unbound admin key", "owner"])
def test_extraction_files_a_loop_under_the_confidential_project_a_source_names_for_the_owner_and_an_unbound_admin_key(
    label_harness, admin
):
    project = _project(label_harness, sensitivity="confidential")
    key = label_harness.key("admin_agent") if admin else None
    _capture(label_harness, key, str(project["id"]))
    status, body = _extract_everything(label_harness, key)
    assert status == 201 and body["created_count"] == 1, body
    assert [row["project_id"] for row in _loop_rows(label_harness)] == [str(project["id"])]


def test_extraction_files_a_loop_under_the_readable_project_a_source_names(label_harness):
    project = _project(label_harness, sensitivity="private")
    key = label_harness.key("trusted_local_agent")
    _capture(label_harness, key, str(project["id"]))
    status, body = _extract_everything(label_harness, key)
    assert status == 201 and body["created_count"] == 1, body
    assert [row["project_id"] for row in _loop_rows(label_harness)] == [str(project["id"])]


def test_a_hidden_project_in_one_source_does_not_hide_the_loop_of_another(label_harness):
    shown = _project(label_harness, sensitivity="private", name="Shown Kiln")
    hidden = _project(label_harness, sensitivity="confidential", name="Hidden Kiln")
    key = label_harness.key("trusted_local_agent")
    _capture(label_harness, key, str(hidden["id"]))
    _capture(label_harness, key, str(shown["id"]))
    status, body = _extract_everything(label_harness, key)
    assert status == 201 and body["created_count"] == 1, body
    assert [row["project_id"] for row in _loop_rows(label_harness)] == [str(shown["id"])]


def test_a_source_that_names_two_projects_is_still_filed_under_none(label_harness):
    """A loop is filed under a project only when the source names exactly one. With two it has no project, as before."""

    hidden = _project(label_harness, sensitivity="confidential")
    shown = _project(label_harness, sensitivity="private")
    key = label_harness.key("trusted_local_agent")
    status, body, _headers = label_harness.request(
        "POST",
        "/v0/vnext/sources",
        payload={"raw_text": "Planning notes.\nTODO: renew the kiln permit", "title": "Planning notes", "domain": "project",
                 "sensitivity": "public", "project_scope": [str(hidden["id"]), str(shown["id"])]},
        key=key,
    )
    assert status == 201, body
    status, body = _extract_everything(label_harness, key)
    assert status == 201, body
    assert body["created_count"] == 1, body
    assert [row["project_id"] for row in _loop_rows(label_harness)] == [None]


# -- doors that keep a free-form name in a scope and look no project up ----------------------------------------------


VOLATILE_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}T[0-9:.+Z-]+|sha256:[0-9a-f]{64}|capture-md5:[0-9a-f]{32}|[0-9a-f]{32,}")


def _masked(body: object) -> str:
    """The body with the ids, the timestamps and the hashes of the two calls masked, which are never equal."""

    text = UUID_TEXT.sub("<id>", json.dumps(body, sort_keys=True, default=str))
    return VOLATILE_TEXT.sub("<volatile>", text)


def test_assigning_a_source_to_a_hidden_project_is_answered_as_assigning_it_to_a_name_that_is_no_project(label_harness):
    """A scope is a free-form name, no project row is read, and so no answer tells a project that is hidden from one that
    is not stored. If a door ever looks the project up, it has to answer the two alike."""

    hidden = _project(label_harness, sensitivity="confidential")
    key = label_harness.key("trusted_local_agent")
    answers = []
    for project_id in (str(hidden["id"]), str(uuid4())):
        source = label_harness.source(scope=(), text="TODO: probe", sensitivity="public")
        status, body, _headers = label_harness.request(
            "POST", f"/v0/vnext/sources/{source['id']}/review",
            payload={"action": "assign_project", "project_id": project_id}, key=key,
        )
        answers.append((status, _masked(body).replace(str(source["id"]), "<source>")))
    assert answers[0] == answers[1], answers
    assert answers[0][0] == 200


def test_assigning_a_memory_to_a_hidden_project_is_answered_as_assigning_it_to_a_name_that_is_no_project(label_harness):
    hidden = _project(label_harness, sensitivity="confidential")
    key = label_harness.key("trusted_local_agent")
    answers = []
    for project_id in (str(hidden["id"]), str(uuid4())):
        memory = label_harness.memory(scope=())
        status, body, _headers = label_harness.request(
            "POST", f"/v0/vnext/memories/{memory['id']}/review",
            payload={"action": "assign_project", "project_id": project_id}, key=key,
        )
        answers.append((status, _masked(body).replace(str(memory["id"]), "<memory>")))
    assert answers[0] == answers[1], answers
